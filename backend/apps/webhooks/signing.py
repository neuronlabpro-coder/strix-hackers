"""Firma de los payloads de webhook salientes.

El esquema es el de Stripe, y se copia por una razón concreta: es un formato que ya
implementan las bibliotecas de todos los lenguajes y que tiene dos propiedades que
necesitamos. La primera es que la firma cubre el **cuerpo exacto** y no un objeto
reconstruido, así que el receptor puede verificarla sin depender de como serializa.
La segunda es que el timestamp va **dentro** de lo firmado, lo que impide que un payload
capturado se pueda reenviar indefinidamente.

## Por qué el cuerpo se serializa una vez y para siempre

Se serializa a bytes, se firma **esos** bytes y se envían **esos** bytes. Serializar tres
veces —una para firmar, otra para enviar, otra para guardar en la base— produce cuerpos
que pueden diferir en el orden de las claves, y la firma dejaría de validar en el
receptor sin que nada en nuestro lado fallara. Por eso `SignedPayload` lleva los bytes ya
congelados, y la fila de entrega guarda esos mismos bytes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Final

#: Cabecera de la firma. El prefijo de la plataforma lo hace reconocible en un proxy.
SIGNATURE_HEADER: Final[str] = "X-MGF-Signature"

#: Cabecera del tipo de evento, para que el receptor pueda enrutar sin parsear el cuerpo.
EVENT_HEADER: Final[str] = "X-MGF-Event"

#: Cabecera del identificador de entrega. Es lo que permite idempotencia en el receptor:
#: dos entregas del mismo evento son dos `delivery_id` distintos, y un reintento del
#: mismo intento conserva el suyo.
DELIVERY_HEADER: Final[str] = "X-MGF-Delivery"

#: Versión del esquema de firma. Se incluye en la propia firma para que un cambio futuro
#: del formato no pueda producir una firma que un verificador viejo acepte por descuido.
SIGNATURE_VERSION: Final[str] = "v1"

#: Separador entre el timestamp y la firma dentro de la cabecera. Es el de Stripe y
#: coincide con la convención que ya usan otras plataformas, para que un integrador que
#: tenga un verificador hecho lo pueda reutilizar.
PAIR_SEPARATOR: Final[str] = ","


class WebhookSignatureError(ValueError):
    """El secreto no permite firmar. No se registra su valor en el mensaje.

    Es la diferencia entre un log que dice "fallo al firmar el webhook del tenant X" y
    uno que dice cual es la credencial del cliente.
    """


def canonical_json(payload: dict[str, Any]) -> bytes:
    """Serializa el payload de forma estable.

    `sort_keys` y los separadores compactos hacen que dos serializaciones del mismo
    objeto den los mismos bytes. Sin `sort_keys`, el orden de las claves depende de como
    se construyó el diccionario, y la misma información firmada dos veces produciría
    firmas distintas.
    """

    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def compute_signature(secret: str, timestamp: int, body: bytes) -> str:
    """Devuelve la firma HMAC-SHA256 en hexadecimal.

    ## Por qué SHA-256 y no una función lenta

    Igual que en los tokens de API, y por la misma razón: aquí no hay un secreto elegido
    por una persona al que haya que ralentizar el ataque. Un atacante que quiera
    descifrar la firma necesita conocerla, y el receptor no expone un endpoint que la
    acepte como contraseña. La lentitud no aporta nada y costaría CPU en cada entrega.

    ## Qué se firma

    `{timestamp}.{body}`. El timestamp va dentro porque es lo que hace que una captura no
    sirva para siempre: quien no acepte transacciones con una tolerancia de reloj no puede
    reenviar un payload antiguo, porque la firma se calculó con otro timestamp.
    """

    if not secret:
        raise WebhookSignatureError("El secreto de firma no puede estar vacío")
    if isinstance(secret, str):
        material = secret.encode("utf-8")
    else:  # pragma: no cover - defensivo ante un cambio de tipo
        raise WebhookSignatureError("El secreto de firma debe ser texto")
    mensaje = str(timestamp).encode("ascii") + b"." + body
    return hmac.new(material, mensaje, hashlib.sha256).hexdigest()


def signature_header(secret: str, body: bytes, *, timestamp: int | None = None) -> str:
    """Construye el valor completo de `X-MGF-Signature`.

    El formato es `t={segundos},v1={firma}`. Se admite más de una `v1` separada por
    comas porque es lo que hacen los demás y porque permite rotar el secreto sin que el
    receptor tenga que cambiar de código el mismo día.
    """

    instante = int(time.time()) if timestamp is None else timestamp
    firma = compute_signature(secret, instante, body)
    return f"t={instante},{SIGNATURE_VERSION}={firma}"


@dataclass(frozen=True, slots=True)
class SignedPayload:
    """Los bytes de la entrega, ya firmados, listos para enviarse y guardarse.

    Es inmutable a propósito: entre la firma y el envío no puede cambiar nada, y una
    estructura con campos mutables dejaría esa garantía en manos de quien la rellene.
    """

    body: bytes
    signature: str
    timestamp: int

    @property
    def json_text(self) -> str:
        """El cuerpo como texto, para guardarlo en la entrega y mostrarlo en el panel."""

        return self.body.decode("utf-8")

    def headers(self, event_type: str, delivery_id: str) -> dict[str, str]:
        """Las cabeceras completas de la entrega.

        Se lleva `X-MGF-Signature` y no `Authorization` porque el receptor es del cliente y
        no usa el esquema de la API: es un endpoint público suyo que espera un cuerpo
        firmado. Ponerlo en `Authorization` haría que un proxy o un intermediario lo
        reescriban o lo rechacen.
        """

        return {
            "Content-Type": "application/json",
            SIGNATURE_HEADER: self.signature,
            EVENT_HEADER: event_type,
            DELIVERY_HEADER: delivery_id,
            "User-Agent": "MindGuardFenix-Webhooks/1",
        }

    def verify(self, secret: str) -> bool:
        """Recalcula la firma con otro secreto y la compara.

        Extrae el valor `v1=` de la cabecera antes de comparar. La cabecera lleva el
        timestamp y el prefijo de versión delante, y compararla entera con un hexadecimal
        devuelve siempre `False`: no es que la firma no valiera, es que se compararon dos
        cosas distintas. La comparación es de tiempo constante con `compare_digest` para
        no filtrar por temporización cuántos bytes correctos lleva quien reintenta.

        Vive aquí para que la fórmula de la firma tenga **una** implementación. Una copia
        de la lógica en un receptor es una copia que puede quedarse vieja sin que nada
        falle, y es exactamente el fallo que hace que un cliente deje de verificar la
        firma y empiece a aceptarla sin comprobarla.
        """

        esperada = compute_signature(secret, self.timestamp, self.body)
        return hmac.compare_digest(esperada, self.signature_value())

    def signature_value(self) -> str:
        """El hexadecimal de la firma, sin el prefijo de versión ni el timestamp.

        Se separa de `verify` para que quien construya el receptor pueda leer el valor y
        compararlo con su propia implementación, en vez de tener que reimplementar el
        troceado de la cabecera.
        """

        _, _, resto = self.signature.partition(f"{SIGNATURE_VERSION}=")
        return resto.split(PAIR_SEPARATOR, 1)[0]


def sign_payload(
    secret: str, payload: dict[str, Any], *, timestamp: int | None = None
) -> SignedPayload:
    """Serializa, firma y devuelve la entrega lista."""

    cuerpo = canonical_json(payload)
    instante = int(time.time()) if timestamp is None else timestamp
    return SignedPayload(
        body=cuerpo,
        signature=signature_header(secret, cuerpo, timestamp=instante),
        timestamp=instante,
    )
