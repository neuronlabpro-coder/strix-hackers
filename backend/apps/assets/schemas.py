"""Esquemas de la superficie de ataque: dominios verificados y activos descubiertos.

## Por qué el alta devuelve el valor TXT **completo** y no solo el token

Porque el panel tiene que poder copiar lo mismo que se va a verificar, sin recomponerlo. Si
devolviera el token suelto y el panel concatenara el prefijo, bastaría un cambio de prefijo en
el backend para que el panel publicara un valor que el servidor no acepta: el cliente
publicaría un registro, esperaría, y la verificación fallaría sin que ninguna de las dos
partes tuviera un error.

Con el valor completo viajando desde el servidor, la cadena que se publica y la que se
compara se construyen en el mismo sitio, y la única forma de que diverjan es que alguien
cambie una de las dos a mano.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.apps.assets.models import (
    AssetTypeEnum,
    DomainClaimConflict,
    DomainVerificationMethodEnum,
)
from backend.apps.assets.verifier import DnsLookupOutcome

#: Una etiqueta DNS: entre 1 y 63 caracteres, alfanuméricos y guiones, sin empezar ni
#: terminar en guion. Es el límite del RFC 1035 y es el mismo que impone un proveedor de
#: DNS, así que validarlo aquí evita un nombre que se acepta y luego no se puede publicar.
_ETIQUETA_DNS = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")

#: El dominio tiene que tener al menos dos etiquetas: `localhost` o un nombre de una sola
#: parte no se puede delegar y no hay a quién añadir un TXT.
_MIN_ETIQUETAS = 2

#: Tope del FQDN. 253 es el límite del protocolo; se deja 255 porque la columna es
#: `String(255)` y un valor más largo se truncaría **en silencio** en PostgreSQL.
_MAX_LONGITUD_DOMINIO = 253


def normalize_domain(value: str) -> str:
    """Normaliza un dominio a su forma canónica.

    Minúsculas, sin esquema, sin barra final y sin espacios. La comparación de dominios es de
    cadenas, así que la normalización tiene que ser exacta: `Empresa.COM` y `empresa.com` son
    el mismo nombre y tienen que ser la misma fila, o el mismo dominio aparecería dos veces
    con dos tokens distintos y "verificar" no tendría respuesta clara.

    El prefijo `www.` **no** se quita, y el motivo merece una línea: `www` es un subdominio
    real, con su propio DNS y su propio propietario. Quitarlo fusionaría `www.empresa.com` y
    `empresa.com` —que son hosts distintos— en uno solo, y el descubrimiento de activos no
    vería nunca ninguno de los dos.
    """

    limpio = value.strip().lower()
    for esquema in ("https://", "http://"):
        if limpio.startswith(esquema):
            limpio = limpio[len(esquema) :]
            break
    limpio = limpio.split("/", 1)[0]
    # El puerto solo aparece en una URL con esquema, que ya se ha quitado; se quita igual
    # por si el usuario pego `empresa.com:8443` sin esquema.
    if ":" in limpio and limpio.count(":") == 1 and cleaned_port(limpio):
        limpio = limpio.split(":", 1)[0]
    return limpio.strip().rstrip(".")


def cleaned_port(value: str) -> bool:
    """Si el sufijo tras `:` parece un puerto.

    Se usa para no recortar un `:` que forms parte de un IPv6 o de un nombre raro. Solo
    acepta dígitos, que es lo que un puerto es.
    """

    sufijo = value.split(":", 1)[1]
    return sufijo.isdigit()


def _validate_domain(value: str) -> str:
    """Valida que el valor sea un FQDN publicable."""

    dominio = normalize_domain(value)
    if not dominio:
        raise ValueError("El dominio no puede estar vacío")
    if len(dominio) > _MAX_LONGITUD_DOMINIO:
        raise ValueError(f"El dominio no puede superar {_MAX_LONGITUD_DOMINIO} caracteres")

    etiquetas = dominio.split(".")
    if len(etiquetas) < _MIN_ETIQUETAS:
        raise ValueError(
            "El dominio necesita al menos dos etiquetas, por ejemplo empresa.com"
        )
    for etiqueta in etiquetas:
        if not _ETIQUETA_DNS.match(etiqueta):
            raise ValueError(
                f"La etiqueta «{etiqueta}» no es válida: solo admite letras, números y "
                "guiones, y no puede empezar ni terminar en guion"
            )

    # La TLD tiene que ser alfabetica. Sin esta comprobacion `empresa.123` pasaria, y es un
    # nombre que no existe: no hay TLD numerico. El "dominio no encontrado" del proveedor de
    # DNS llega mucho despues y con un mensaje que no explica el problema.
    if etiquetas[-1].isdigit():
        raise ValueError("La extensión final del dominio debe ser alfabética")
    return dominio


class StrictSchema(BaseModel):
    """Base común para rechazar campos no declarados."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DomainCreate(StrictSchema):
    """Alta de un dominio para el workspace activo."""

    domain_name: str = Field(min_length=1, max_length=_MAX_LONGITUD_DOMINIO)
    verification_method: DomainVerificationMethodEnum = (
        DomainVerificationMethodEnum.DNS_TXT
    )

    @field_validator("domain_name")
    @classmethod
    def validate_domain_name(cls, value: str) -> str:
        return _validate_domain(value)


class DomainItem(BaseModel):
    """Un dominio del workspace, con su estado y sus instrucciones de verificación."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    domain_name: str
    is_verified: bool
    verified_at: datetime | None
    verification_method: DomainVerificationMethodEnum
    #: El valor TXT completo que hay que publicar. Viaja siempre, también para los dominios
    #: ya verificados: el panel lo muestra en el desplegable de un dominio verificado, que
    #: es la pregunta más frecuente cuando alguien suspects que el registro se ha borrado.
    txt_record_name: str
    txt_record_value: str
    created_at: datetime
    updated_at: datetime
    #: Número de activos descubiertos bajo este dominio. Lo trae el listado para que el panel
    #: pueda pintar el tamaño de la superficie sin una segunda llamada por fila.
    asset_count: int = Field(default=0, ge=0)


class DomainListResponse(BaseModel):
    """Dominios del workspace activo."""

    items: list[DomainItem]
    total: int = Field(ge=0)


class DomainConflictResponse(BaseModel):
    """Por qué se rechaza un alta de dominio.

    ## Por qué el motivo es un campo y no solo el `detail`

    Porque el panel tiene que ofrecer una acción distinta según el caso. Si el dominio ya
    está en este workspace, el botón correcto es "abrir el dominio"; si lo tiene otro, no
    hay ninguna acción posible y solo cabe un mensaje. Con un `detail` en prosa, el panel
    tendría que interpretar la frase —en español— para decidir, y en inglés no funcionaría.

    `already_verified` distingue los dos casos del conflicto ajeno: un reclamo pendiente y
    un dominio ya verificado se leen distinto, y el segundo explica por qué no se puede
    reclamar sin más.
    """

    detail: str
    motivo: DomainClaimConflict
    already_verified: bool = False
    #: El nombre en disputa, para que el panel pueda escribirlo sin tener que conservarlo
    #: del formulario que se acaba de rechazar.
    domain_name: str


class VerifyDomainResponse(BaseModel):
    """El resultado de comprobar el TXT de un dominio.

    ## Por qué el resultado es un **enum** y no un texto

    Porque los seis estados significan cosas distintas para el usuario y la acción que
    corresponde a cada uno es distinta: corregir el nombre, publicar el registro, esperar la
    propagación, o reintentar. Un texto libre enviado desde el backend tendría que traducirse
    en el panel, que es donde vive el i18n; el enum viaja y el panel elige el mensaje, con su
    clave y sus tres idiomas.

    `found_values` viaja con el `MISMATCH` para que el panel pueda mostrar lo que hay
    publicado. Sin eso, el usuario sabe que "no coincide" y no puede ver el token equivocado
    que tiene puesto, que es el 90 % de las veces lo que pasa.
    """

    outcome: DnsLookupOutcome
    is_verified: bool
    #: El valor exacto que se esperaba. Viaja para que el panel pueda reintentar sin volver a
    #: pedir nada a la API.
    expected_value: str
    #: Los valores encontrados en el TXT, vacío si el nombre no existe o no tiene TXT.
    found_values: list[str] = Field(default_factory=list)
    #: Clave de i18n del mensaje, sin namespace. La decide el backend porque el estado del
    #: DNS es suyo, y la traduce el panel porque las traducciones son suyas.
    message_key: str


class AssetItem(BaseModel):
    """Un activo descubierto."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    domain_id: UUID
    domain_name: str
    asset_type: AssetTypeEnum
    value: str
    service_name: str | None
    technologies: list[str] = Field(default_factory=list)
    last_scanned_at: datetime | None
    created_at: datetime


class AssetListResponse(BaseModel):
    """Página de activos del workspace activo."""

    items: list[AssetItem]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class DiscoveryEnqueuedResponse(BaseModel):
    """Confirmación de que el descubrimiento quedó encolado.

    ## Por qué devuelve un identificador de tarea y no un resultado

    Porque el descubrimiento es un escaneo de DNS sobre una lista de subdominios, y eso tarda
    de segundos a minutos. Una respuesta con el resultado sería un `POST` que se queda
    abierto, y un `POST` abierto se corta por el proxy, el tiempo de espera o la navegación
    atrás: el cliente creería que no se lanzó nada y volvería a lanzarlo, duplicando el
    trabajo contra el mismo DNS.

    El identificador permite consultar el estado sin que la respuesta dependa de que el
    trabajo termine.
    """

    task_id: str
    domain_name: str
    #: Los activos que ya había, para que el panel pueda decir "36 nuevos de 212" sin
    #: tener que esperar a que la tarea termine.
    existing_assets: int = Field(default=0, ge=0)


#: Tipo del límite de activos por página. `Annotated` con `Field` es lo que convierte la
#: restricción en un `422` del servidor en lugar de un recorte silencioso.
AssetLimit = Annotated[int, Field(ge=1, le=200)]


__all__ = [
    "AssetItem",
    "AssetLimit",
    "AssetListResponse",
    "AssetTypeEnum",
    "DiscoveryEnqueuedResponse",
    "DnsLookupOutcome",
    "DomainCreate",
    "DomainItem",
    "DomainListResponse",
    "DomainVerificationMethodEnum",
    "VerifyDomainResponse",
    "normalize_domain",
]
