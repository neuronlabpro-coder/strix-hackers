"""Servicios de acceso a credenciales Git con aislamiento por organización.

## Por qué aquí hay dos caminos y solo uno renueva

`build_organization_client` lo usa la **petición HTTP**, que antes de abrir nada ya ha pasado por
`_exigir_credencial_vigente` y sabe qué hacer con un veredicto: hay un código HTTP que responder y
un panel al que contárselo.

`build_client_for_repository` lo usan los **workers** —el pipeline de PR, el autofix y los
webhooks—, que corren en un proceso aparte, con su propia sesión y **sin ninguna pantalla delante**.
Para ellos la caducidad del token no es un código de respuesta: es un `401` de GitHub dentro de un
pipeline que el panel sigue enseñando en verde. Por eso este camino renueva antes de construir el
cliente y, si no puede, lanza un error tipado que el worker sabe distinguir de un fallo pasajero.

## Por qué la renovación **no** va en la sesión del llamador

Porque `asegurar_credentialo_vigente` confirma la transacción al terminar la fase bloqueada, y en
un worker esa transacción sostiene el `FOR UPDATE` de la revisión y los hallazgos que se están
insertando. Está escrito con detalle en `token_refresh.py`; aquí lo que importa es que el
`commit()` es de la sesión ajena y no de esta.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.repositories.clients.base import BaseGitClient
from backend.apps.repositories.clients.factory import get_client_for_credential, get_git_client
from backend.apps.repositories.credentials import (
    GitCredentialNotFoundError,
    encrypt_organization_credential,
)
from backend.apps.repositories.models import GitCredential, GitProviderEnum, Repository
from backend.apps.repositories.token_refresh import (
    RefrescadorOAuth,
    asegurar_credentialo_vigente_en_sesion_ajena,
    credential_necesita_renovacion,
)

#: Superficie pública del módulo. `GitCredentialNotFoundError` y `encrypt_organization_credential`
#: viven ahora en `backend.apps.repositories.credentials` —porque `token_refresh.py` también las
#: necesita y meterlas aquí creaba un ciclo de imports que rompía el arranque—, pero se siguen
#: exportando desde aquí: `router.py`, `router_auth.py`, `pipeline.py` y `test_git_webhooks.py` las
#: importan desde este módulo, y mover el módulo de un nombre no debería obligar a tocar a sus
#: consumidores. Declararlo aquí es lo que hace explícita esa intención y lo que evita que un
#: `ruff` futuro lo lea como un import muerto y lo borre.
__all__ = [
    "GitCredentialNotFoundError",
    "build_client_for_repository",
    "build_organization_client",
    "encrypt_organization_credential",
    "get_organization_credential",
]


async def get_organization_credential(
    session: AsyncSession,
    organization_id: UUID,
    provider: GitProviderEnum,
) -> GitCredential:
    """Obtiene exactamente la credencial del tenant solicitado."""

    result = await session.execute(
        select(GitCredential).where(
            GitCredential.organization_id == organization_id,
            GitCredential.provider == provider,
        )
    )
    credential = result.scalar_one_or_none()
    if credential is None:
        raise GitCredentialNotFoundError(
            "La organización no tiene una credencial para el proveedor"
        )
    return credential


async def _credential_renovada(
    session: AsyncSession,
    repository: Repository,
) -> GitCredential:
    """Vuelve a leer la credencial del tenant después de que otra sesión la haya renovado.

    ## Por qué hace falta y por qué con `populate_existing`

    Porque la fila suele llevar **ya en el mapa de identidad** de esta sesión —la acaba de leer
    `get_organization_credential`, y en `pipeline.py` es además el `claim.credential` con el que se
    clona el repositorio— y SQLAlchemy no sobrescribe los atributos de una instancia que ya está
    ahí: devolvería el mismo objeto con el `encrypted_access_token` viejo. El cliente se
    construiría con un token caducado y el panel lo vería todo en verde hasta el primer `401`.

    Con `populate_existing=True` no se devuelve una instancia nueva: se **actualiza la que ya
    estaba**, que es justo lo que necesitan tanto el cliente que se va a construir como el
    `materialize_pr_workspace` que clona con ella.
    """

    result = await session.execute(
        select(GitCredential)
        .where(
            GitCredential.organization_id == repository.organization_id,
            GitCredential.provider == repository.provider,
        )
        .execution_options(populate_existing=True)
    )
    credential = result.scalar_one_or_none()
    if credential is None:
        # La fila se ha ido entre la renovación y esta lectura: solo puede ser un
        # `ON DELETE CASCADE` de la organización que alguien está borrando. Se dice con el mismo
        # error que usa el resto del módulo, que es el mismo hecho.
        raise GitCredentialNotFoundError(
            "La organización no tiene una credencial para el proveedor"
        )
    return credential


async def build_client_for_repository(
    session: AsyncSession,
    repository: Repository,
    *,
    api_base_url: str | None = None,
    http_client: httpx.Client | None = None,
    margen: timedelta | None = None,
    refrescador: RefrescadorOAuth | None = None,
) -> BaseGitClient:
    """Descifra el token solo en memoria y lo vincula al tenant del repositorio.

    ## Por qué antes de descifrar se decide si hay que renovar

    Porque descifrar un token caducado y gastarlo es el fallo que este camino no deja volver: el
    proveedor contesta `401`, y un `401` en un worker no lo ve ninguna pantalla. Se decide con la
    **misma** regla que usa la ruta del panel —`credential_necesita_renovacion`, la misma función y
    por tanto el mismo margen— y solo cuando esa regla dice «hay que renovar» se abre la sesión
    ajena.

    ## Por qué el caso frecuente no cuesta una conexión extra

    Porque la decisión se toma sobre la fila que `get_organization_credential` **ya ha leído**. Con
    el token en vigor no se abre ninguna sesión más, no se hace ninguna consulta nueva y no se
    llama al proveedor: el camino más común queda exactamente como estaba. Solo se paga el precio
    cuando de verdad hay una renovación, y una renovación es una por conexión cada ocho horas.

    ## Por qué el fallo sale como excepción y no como `None`

    Porque `None` es lo que devuelve `_try_open_client` cuando **no hay credencial utilizable** y su
    llamador —el borrado del webhook al desvincular un repositorio— quiere ese `None` para poder
    seguir con el desvínculo local. Aquí el `None` se convertiría en un «no se pudo validar» sin
    motivo, que es el diagnóstico que este arreglo existe para quitar. Lanza, y lanza la clase que
    dice si reintentar.
    """

    credential = await get_organization_credential(
        session,
        repository.organization_id,
        repository.provider,
    )
    if credential_necesita_renovacion(credential.token_expires_at, margen=margen):
        await asegurar_credentialo_vigente_en_sesion_ajena(
            repository.organization_id,
            repository.provider,
            margen=margen,
            refrescador=refrescador,
        )
        credential = await _credential_renovada(session, repository)
    return get_git_client(
        credential,
        repository,
        api_base_url=api_base_url,
        http_client=http_client,
    )


async def build_organization_client(
    session: AsyncSession,
    organization_id: UUID,
    provider: GitProviderEnum,
    *,
    allowed_repo_full_name: str | None = None,
    api_base_url: str | None = None,
    http_client: httpx.Client | None = None,
) -> BaseGitClient:
    """Construye un cliente acotado al tenant para inventario y gestión de repositorios."""

    credential = await get_organization_credential(session, organization_id, provider)
    return get_client_for_credential(
        credential,
        organization_id,
        provider,
        allowed_repo_full_name=allowed_repo_full_name,
        api_base_url=api_base_url,
        http_client=http_client,
    )
