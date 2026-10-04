"""Renovación del token de acceso de una credencial OAuth antes de que caduque.

## El fallo que este módulo existe para no dejar volver

Un token `gho_` de GitHub dura `expires_in: 28800`, ocho horas. `git_credentials` guardaba el
`refresh_token` cifrado desde la fase 3 y **no lo usaba en ningún sitio**: la fila se escribía en
el callback de OAuth y nunca se volvía a leer. El efecto era que toda conexión OAuth del producto
caducaba a las ocho horas y no se recuperaba sola. Un cliente conecta su GitHub el lunes y el
martes ya no tiene integración, sin explicación y sin que nadie pueda arreglarlo desde el panel.

## Por qué el bloqueo es de fila y no un `Lock` de Python

Porque la unidad de ejecución no es el proceso. Uvicorn arranca varios workers, los workers de
Celery son procesos aparte, y el despliegue es un contenedor que se puede replicar. Un
`threading.Lock` o un `asyncio.Lock` solo serializan dentro de **un** proceso: con dos workers, dos
peticiones simultáneas siguen llamando al proveedor a la vez, cada una con el mismo
`refresh_token`. Y eso no es un fallo teórico —GitLab rota el `refresh_token` en cada refresco y el
anterior queda invalidado—, es exactamente el «a veces funciona y a veces no».

El bloqueo tiene que vivir en algo que compartan todos los procesos, y ese algo ya está en el
proyecto: PostgreSQL. `SELECT ... FOR UPDATE` sobre la fila de `git_credentials` es el mismo
mecanismo que usa `billing/service.py` para que dos pentests no gasten el mismo saldo, y
`webhook_events.py` para que dos entregas del mismo evento no se procesen dos veces. Además
`FOR UPDATE` tiene una propiedad que un mutex no tiene y que este caso necesita: **en READ
COMMITTED, el que espera vuelve a leer la fila ya actualizada por el que la tenía bloqueada**. El
segundo refresco ve el `token_expires_at` que el primero acaba de escribir y no llama al proveedor.

## Por qué la primera lectura va sin bloqueo

Porque el caso normal es que el token siga vivo, y en ese caso no hay nada que serializar. Tomar
el bloqueo de fila en cada petición de inventario dejaría todas las peticiones de una organización
encoladas detrás de la misma fila, y la última esperaría a la llamada a GitHub de la primera. Es el
patrón de doble comprobación: se lee sin bloquear para descartar el caso frecuente, y solo se
bloquea cuando de verdad puede haber un refresco.

## Por qué `populate_existing=True` no es opcional

Porque SQLAlchemy no sobrescribe los atributos de una instancia que ya está en el mapa de
identidad. La fila puede llevar ya un rato en la sesión —cualquier cosa que haya leído la credencial
antes la dejó ahí— y entonces la lectura bloqueante devolvería **la misma instancia con los valores
viejos**: el segundo concurrente creería que el token sigue caducado y refrescaría con el
`refresh_token` que el primero acaba de rotar. `supply_chain/service.py` documenta el mismo motivo
para el mismo parámetro.

## Por qué esto es especialmente traicionero aquí

Porque el mapa de identidad de SQLAlchemy guarda **referencias débiles**. Si la sesión no tiene
nadie sujetando la fila, el primer `gc` la saca del mapa y la lectura siguiente la trae fresca de la
base: el parámetro no hace falta. En cuanto alguien la sujeta —la ruta que lo llama, una vista que
la consultó antes, cualquier cosa— la fila se queda con su valor viejo y el parámetro sí hace
falta. Es decir: el defecto aparece y desaparece según cuándo pase el recolector, que es la peor
forma posible de un defecto. Por eso se pone siempre y no «cuando haga falta».

## Por qué `refrescador` es inyectable

Porque un refresco que solo se puede probar contra la red real no se puede probar: la batería del
proyecto no habla con GitHub ni con GitLab. Inyectar la función **no** cambia lo que se hace en
producción —el valor por defecto es `refresh_oauth_token`— y permite comprobar el caso de
concurrencia con dos conexiones reales de PostgreSQL y un proveedor falso que cuenta llamadas.

## Por qué esta función **confirma** la transacción de la sesión del llamador

Porque el bloqueo de fila se mantiene hasta que termina la transacción, y sin cerrarla el
`FOR UPDATE` seguiría sujetando la fila durante la llamada al proveedor y durante el resto de la
petición. Se confirma al salir de la fase bloqueada, tanto si se ha escrito algo como si no,
porque `expire_on_commit=False` deja el mapa de identidad intacto y un `commit()` sin cambios no
persiste nada.

Eso convierte en **precondición** que el llamador no tenga nada pendiente de escribir cuando llama
aquí. Su único punto de llamada —`_open_client`, en `router.py`— cumple: solo ha hecho lecturas.
Un `rollback()` sería la alternativa, y sería peor: `rollback()` expira todos los objetos de la
sesión, y `connect_repository` lee `repository.webhook_id` **después** de `_open_client`; un
atributo expirado fuera de un greenlet lanza `MissingGreenlet`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.repositories.models import GitCredential, GitProviderEnum
from backend.apps.repositories.oauth import (
    OAuthConfigurationError,
    OAuthProviderSettings,
    OAuthRefreshError,
    OAuthRefreshUnavailableError,
    OAuthToken,
    get_oauth_provider_settings,
    refresh_oauth_token,
)
from backend.apps.repositories.services import (
    GitCredentialNotFoundError,
    encrypt_organization_credential,
)
from backend.core.config import settings
from backend.core.crypto import CryptoError, decrypt_secret

logger = logging.getLogger(__name__)

#: Firma del paso de red, sustituible en las pruebas. El valor por defecto es el de producción.
RefrescadorOAuth = Callable[[OAuthProviderSettings, str], Awaitable[OAuthToken]]


class EstadoDeRefresh(StrEnum):
    """Qué ha pasado al intentar dejar la credencial en vigor."""

    #: No había nada que hacer: no tiene caducidad, o le queda más margen del configurado.
    VIGENTE = "vigente"
    #: Se ha renovado el token de acceso y se ha guardado con su nueva fecha.
    REFRESCADO = "refrescado"
    #: La credencial caducó y no hay `refresh_token` con el que renovarla.
    SIN_RENOVACION = "sin_refresh_token"
    #: El proveedor rechazó el `refresh_token`. Solo queda volver a conectar.
    RECHAZADO = "rechazado"
    #: Esta instalación no tiene `client_id`/`client_secret` del proveedor. Reconectar no arregla
    #: nada, y decirlo como si arreglara sería mandar al usuario a repetir un alta que va a fallar.
    NO_CONFIGURADO = "no_configurado"
    #: El proveedor no respondió. Es transitorio y la respuesta es `502`, no «reconecta».
    NO_DISPONIBLE = "no_disponible"


@dataclass(frozen=True, slots=True)
class ResultadoDeRefresh:
    """El veredicto y la fecha que lo explica, para que el mensaje pueda citarla."""

    estado: EstadoDeRefresh
    caduca_en: datetime | None = None


async def asegurar_credentialo_vigente(
    session: AsyncSession,
    organization_id: UUID,
    provider: GitProviderEnum,
    *,
    ahora: datetime | None = None,
    margen: timedelta | None = None,
    config: OAuthProviderSettings | None = None,
    refrescador: RefrescadorOAuth | None = None,
) -> ResultadoDeRefresh:
    """Deja la credencial del tenant con un token de acceso en vigor, o explica por qué no pudo.

    **Precondición:** la sesión del llamador no tiene nada pendiente de escribir. La función
    confirma la transacción al terminar la fase bloqueada, y un `commit()` con cambios a medias
    persistiría media petición.

    ## Por qué `token_expires_at IS NULL` no entra nunca por aquí

    Porque en esta tabla `NULL` es lo que deja `connect_personal_token`: un token personal de acceso
    no lleva fecha de caducidad conocida. No hay nada que renovar, y **no se intenta**: un PAT
    guardado junto a un `refresh_token` de una conexión OAuth anterior es una combinación real
    —`connect_personal_token` sustituye el token de acceso y pone la fecha a `NULL` sin borrar el
    `refresh_token`—, y si se intentara refrescar se cambiaría el PAT del usuario por un token OAuth
    sin avisar.

    ## Por qué el margen configurable existe

    Porque `token_expires_at` es un instante y una petición dura un rato. Renovar justo al expirar
    significa entregar al cliente GitHub un token que se caduca a mitad de la llamada, y el síntoma
    es un `401` del proveedor en una operación que hace un minuto funcionaba. El margen viene de la
    configuración —R1— y no de un literal aquí.

    ## Por qué el filtro es por `organization_id` primero

    Por R3, y por el mismo motivo que en el resto del módulo: la credencial que hay que renovar es
    la de esta organización, y `ix_git_credentials_org_provider` es único sobre esas dos columnas.
    """
    momento = ahora or datetime.now(UTC)
    margen_real = (
        margen
        if margen is not None
        else timedelta(seconds=settings.git_token_refresh_margin_seconds)
    )
    limite = momento + margen_real

    # Primera lectura, sin bloqueo: descarta el caso frecuente y no sujeta la fila.
    prelectura = await session.execute(
        select(
            GitCredential.token_expires_at,
            GitCredential.encrypted_refresh_token.is_not(None),
        ).where(
            GitCredential.organization_id == organization_id,
            GitCredential.provider == provider,
        )
    )
    fila = prelectura.one_or_none()
    if fila is None:
        raise GitCredentialNotFoundError(
            "La organización no tiene una credencial para el proveedor"
        )
    caduca = fila[0]
    if caduca is None or caduca > limite:
        return ResultadoDeRefresh(EstadoDeRefresh.VIGENTE, caduca)
    if not fila[1]:
        # Caducada y sin `refresh_token`: no hay nada que hacer ni a quién esperar. Ni se bloquea
        # ni se llama al proveedor; la respuesta es «vuelve a conectar».
        return ResultadoDeRefresh(EstadoDeRefresh.SIN_RENOVACION, caduca)

    # Segunda lectura, bloqueando la fila. Aquí se serializan los refrescos concurrentes.
    bloqueada = await session.execute(
        select(GitCredential)
        .where(
            GitCredential.organization_id == organization_id,
            GitCredential.provider == provider,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    credential = bloqueada.scalar_one_or_none()
    if credential is None:
        # La fila no puede desaparecer en medio de su propia petición: la borra un
        # `ON DELETE CASCADE` de la organización. Se trata como ausencia, y el `409` lo levanta
        # `_open_client` cuando no consigue construir el cliente.
        await session.commit()
        raise GitCredentialNotFoundError(
            "La organización no tiene una credencial para el proveedor"
        )

    # Bajo el bloqueo se decide de nuevo: si otro proceso ya renovó mientras se esperaba, su
    # `token_expires_at` es el que se lee aquí y no hay nada que renovar.
    caduca = credential.token_expires_at
    if caduca is not None and caduca > limite:
        await session.commit()
        return ResultadoDeRefresh(EstadoDeRefresh.VIGENTE, caduca)
    if credential.encrypted_refresh_token is None:
        await session.commit()
        return ResultadoDeRefresh(EstadoDeRefresh.SIN_RENOVACION, caduca)

    resultado = await _refrescar_bajo_bloqueo(
        credential,
        config if config is not None else get_oauth_provider_settings(provider),
        refrescador if refrescador is not None else refresh_oauth_token,
        momento,
    )
    # Se cierra la transacción pase lo que pase. En el camino de éxito es lo que persiste el token
    # nuevo y lo que suelta la fila; en los demás es lo que suelta la fila sin escribir nada.
    await session.commit()
    return resultado


async def _refrescar_bajo_bloqueo(
    credential: GitCredential,
    config: OAuthProviderSettings,
    refrescador: RefrescadorOAuth,
    momento: datetime,
) -> ResultadoDeRefresh:
    """Canjea el `refresh_token`, lo cifra y lo guarda. Se llama con la fila ya bloqueada.

    ## Por qué los fallos del proveedor no salen como excepciones

    Porque quien necesita distinguirlos es el router, que es donde se traduce a un código HTTP, y
    esa traducción depende de lo que va a ver el panel: `410` para «reconecta», `502` para «GitHub
    está caído», `503` para «esta instalación no tiene OAuth App de GitHub». Una excepción que se
    encadena con `from` acaba en la respuesta con un `500` genérico, que es justo lo que hay que
    evitar. `CryptoError` **sí** se propaga, porque ese caso ya está traducido a `500` en
    `_open_client` y es un fallo de servidor, no del proveedor.

    ## Por qué solo se registra el estado y el nombre de la clase del error

    Porque el `refresh_token` está descifrado en este marco. Los mensajes que llegan aquí ya vienen
    saneados por `oauth.py` —sin token y sin `error_description`—, y aun así se registra solo el
    nombre de la clase del error de `httpx`: es lo que distingue «se cayó la red» de «el TLS no
    valió», y no contiene ni la petición ni el cuerpo.
    """
    organization_id = credential.organization_id
    provider = credential.provider
    try:
        refresh_token = decrypt_secret(
            credential.encrypted_refresh_token or "",
            organization_id=str(organization_id),
            provider=provider.value,
            field="refresh",
        )
        token = await refrescador(config, refresh_token)
    except CryptoError as error:
        logger.error(
            "No se pudo descifrar el refresh token guardado: organization=%s provider=%s "
            "motivo=%s",
            organization_id,
            provider.value,
            type(error).__name__,
        )
        raise
    except OAuthConfigurationError as error:
        logger.error(
            "La OAuth App del proveedor no está configurada: organization=%s provider=%s "
            "motivo=%s",
            organization_id,
            provider.value,
            type(error).__name__,
        )
        return ResultadoDeRefresh(EstadoDeRefresh.NO_CONFIGURADO, credential.token_expires_at)
    except OAuthRefreshError as error:
        no_disponible = isinstance(error, OAuthRefreshUnavailableError)
        logger.warning(
            "El proveedor no devolvió un token renovado: organization=%s provider=%s "
            "rechazo=%s",
            organization_id,
            provider.value,
            type(error).__name__,
        )
        return ResultadoDeRefresh(
            EstadoDeRefresh.NO_DISPONIBLE if no_disponible else EstadoDeRefresh.RECHAZADO,
            credential.token_expires_at,
        )

    # A partir de aquí ya no hay excepciones: el token del proveedor está en la mano y hay que
    # dejarlo escrito. Lo que viene es cifrado y actualización de columnas.
    credential.encrypted_access_token = encrypt_organization_credential(
        token.access_token,
        organization_id,
        provider,
        field="access",
    )
    if token.refresh_token:
        credential.encrypted_refresh_token = encrypt_organization_credential(
            token.refresh_token,
            organization_id,
            provider,
            field="refresh",
        )
    # Sin `expires_in` no se puede saber hasta cuándo vale. Se guarda `None` —que en esta tabla
    # significa «no caduca»— y se avisa: el síntoma es que esa credencial deja de renovarse sola,
    # y un token que nadie vuelve a mirar es peor que uno que se sabe caducado.
    credential.token_expires_at = (
        momento + timedelta(seconds=token.expires_in)
        if token.expires_in is not None
        else None
    )
    nueva_caducidad = credential.token_expires_at
    if nueva_caducidad is None:
        # Sin `expires_in` no se puede saber hasta cuándo vale. Guardar `None` es lo único que se
        # puede hacer, y en esta tabla `None` significa «no caduca», así que el síntoma es que esa
        # credencial deja de renovarse sola. Se dice en voz alta: un token que nadie vuelve a mirar
        # es peor que uno que se sabe caducado, y esto no es un fallo de la petición.
        logger.warning(
            "El proveedor no devolvió expires_in al renovar la credencial: organization=%s "
            "provider=%s. Se queda sin fecha de caducidad y no se renovará sola.",
            organization_id,
            provider.value,
        )
    else:
        logger.info(
            "Credencial Git renovada: organization=%s provider=%s caduca_en=%s",
            organization_id,
            provider.value,
            nueva_caducidad.isoformat(),
        )
    return ResultadoDeRefresh(EstadoDeRefresh.REFRESCADO, credential.token_expires_at)
