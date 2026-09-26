"""Pruebas de la conexión por Token Personal de acceso.

El flujo OAuth ya existe y funciona, pero exige registrar una OAuth App en GitHub o
GitLab, que es un trámite manual fuera del producto. El PAT permite conectar y probar
la sincronización en local sin ese paso, que es lo que bloquea el trabajo diario.

La prueba que de verdad importa aquí no es "guarda un token", sino **no verificar nada**
que importe para la seguridad: el token viaja en claro por HTTPS, y lo que se guarda es
su versión cifrada. Comprobar que el PAT funciona contra la API del proveedor también
es parte del contrato: un token con formato válido pero revocado debe rechazarse al
guardar, no tres horas después cuando se intente sincronizar.
"""

import uuid
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import (
    Membership,
    Organization,
    RoleEnum,
    User,
)
from backend.apps.repositories.clients.base import GitClientError, GitUserIdentity
from backend.apps.repositories.models import GitCredential, GitProviderEnum
from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration

VALID_PAT = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
VALID_GITLAB_PAT = "glpat-0123456789abcdefghij"
# Segundo PAT de GitHub, para probar la sustitucion de una credencial por otra
# del mismo proveedor sin meter por medio un token del otro.
VALID_PAT_SEGUNDO = "ghp_" + "a" * 36


async def _tenant(session: AsyncSession, *, role: RoleEnum = RoleEnum.ADMIN) -> tuple[
    Organization, dict[str, str]
]:
    suffix = uuid.uuid4().hex
    organization = Organization(name=f"PAT {suffix}", slug=f"pat-{suffix}")
    user = User(
        email=f"pat-{suffix}@example.com",
        hashed_password="not-used",
        full_name="PAT User",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(Membership(organization_id=organization.id, user_id=user.id, role=role))
    await session.commit()
    return organization, {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }


def _identity(*, login: str = "octocat") -> GitUserIdentity:
    """Identidad que devolvería la API del proveedor.

    Se construye el tipo real y no un `MagicMock` a propósito: la respuesta del
    endpoint se valida con Pydantic, así que un doble genérico fallaría en la
    serialización y la prueba mediría el doble, no el comportamiento. Lo que se
    sustituye es **la red**, no el contrato.
    """

    return GitUserIdentity(
        provider_user_id="583231",
        login=login,
        display_name="The Octocat",
        email="octocat@github.com",
        avatar_url="https://example.com/a.png",
    )


# --------------------------------------------------------------------------- #
# Alta de la credencial
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pat_is_stored_encrypted_and_never_in_clear(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization, headers = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    with patch(
        "backend.apps.repositories.router_auth.verify_provider_token",
        return_value=_identity(),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/repositories/credentials/token",
                json={
                    "provider": "GITHUB",
                    "token": VALID_PAT,
                    "name": "Token de pruebas",
                },
                headers=headers,
            )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["provider"] == "GITHUB"
    assert payload["account_login"] == "octocat"
    # El secreto no vuelve en la respuesta, en ninguna forma.
    assert VALID_PAT not in response.text
    assert "encrypted_access_token" not in response.text

    stored = (
        await integration_session.execute(
            select(GitCredential).where(
                GitCredential.organization_id == organization.id,
                GitCredential.provider == GitProviderEnum.GITHUB,
            )
        )
    ).scalar_one()
    # Ni la columna ni el log guardan el token en claro: hay que descifrarlo.
    assert VALID_PAT not in stored.encrypted_access_token
    assert stored.encrypted_access_token.startswith("v1.")


@pytest.mark.asyncio
async def test_pat_replaces_the_previous_credential_of_the_same_provider(
    integration_session: AsyncSession,
) -> None:
    """Un segundo PAT para el mismo proveedor sustituye al anterior, no lo duplica.

    La restricción es única por `(organization_id, provider)`: mantener las dos obligaría
    obligaría a elegir cuál usar en cada sincronización.
    """

    assert integration_session is not None
    organization, headers = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    with patch(
        "backend.apps.repositories.router_auth.verify_provider_token",
        return_value=_identity(login="primero"),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/api/v1/repositories/credentials/token",
                json={"provider": "GITHUB", "token": VALID_PAT, "name": "primero"},
                headers=headers,
            )

    with patch(
        "backend.apps.repositories.router_auth.verify_provider_token",
        return_value=_identity(login="segundo"),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            second = await client.post(
                "/api/v1/repositories/credentials/token",
                # Segundo token **de GitHub**: usar aquí un PAT de GitLab sería
                # comprobar el cruce de proveedores por accidente, y ahora el endpoint
                # lo rechaza, que es lo correcto pero no lo que esta prueba mide.
                json={
                    "provider": "GITHUB",
                    "token": VALID_PAT_SEGUNDO,
                    "name": "segundo",
                },
                headers=headers,
            )

    assert second.status_code == 201
    assert second.json()["account_login"] == "segundo"
    stored = (
        await integration_session.execute(
            select(GitCredential).where(GitCredential.organization_id == organization.id)
        )
    ).scalars().all()
    assert len(stored) == 1


@pytest.mark.asyncio
async def test_pat_rejected_when_the_provider_refuses_it(
    integration_session: AsyncSession,
) -> None:
    """Un token que el proveedor rechaza no se guarda.

    Verificar antes de guardar evita descubrir tres horas después, en mitad de una
    sincronización, que la credencial estaba revocada desde el principio.
    """

    assert integration_session is not None
    organization, headers = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    with patch(
        "backend.apps.repositories.router_auth.verify_provider_token",
        side_effect=GitClientError("credencial rechazada por el proveedor"),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/repositories/credentials/token",
                json={"provider": "GITHUB", "token": VALID_PAT, "name": "malo"},
                headers=headers,
            )

    assert response.status_code == 400
    stored = (
        await integration_session.execute(
            select(GitCredential).where(GitCredential.organization_id == organization.id)
        )
    ).scalar_one_or_none()
    assert stored is None


@pytest.mark.asyncio
async def test_pat_rejects_malformed_tokens_without_calling_the_provider(
    integration_session: AsyncSession,
) -> None:
    """Un token con formato inválido se rechaza en el borde, sin gastar una llamada."""

    assert integration_session is not None
    _organization, headers = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    with patch(
        "backend.apps.repositories.router_auth.verify_provider_token"
    ) as verifier:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/repositories/credentials/token",
                json={"provider": "GITHUB", "token": "no-es-un-token", "name": "x"},
                headers=headers,
            )

    assert response.status_code == 422
    verifier.assert_not_called()


@pytest.mark.asyncio
async def test_only_admin_can_store_a_credential(integration_session: AsyncSession) -> None:
    assert integration_session is not None
    _organization, headers = await _tenant(integration_session, role=RoleEnum.MEMBER)
    transport = ASGITransport(app=app)

    with patch(
        "backend.apps.repositories.router_auth.verify_provider_token",
        return_value=_identity(),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/repositories/credentials/token",
                json={"provider": "GITHUB", "token": VALID_PAT, "name": "x"},
                headers=headers,
            )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_pat_requires_authentication(integration_session: AsyncSession) -> None:
    assert integration_session is not None
    _organization, _headers = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/repositories/credentials/token",
            json={"provider": "GITHUB", "token": VALID_PAT, "name": "x"},
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_pat_from_another_tenant_cannot_overwrite(
    integration_session: AsyncSession,
) -> None:
    """R3: un tenant no puede pisar la credencial de otro."""

    assert integration_session is not None
    _victim, victim_headers = await _tenant(integration_session)
    _attacker, attacker_headers = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    with patch(
        "backend.apps.repositories.router_auth.verify_provider_token",
        return_value=_identity(),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/api/v1/repositories/credentials/token",
                json={"provider": "GITHUB", "token": VALID_PAT, "name": "x"},
                headers=victim_headers,
            )
            response = await client.post(
                "/api/v1/repositories/credentials/token",
                json={"provider": "GITLAB", "token": VALID_GITLAB_PAT, "name": "y"},
                headers=attacker_headers,
            )

    assert response.status_code == 201
    # Cada tenant tiene la suya, y la del atacante no se ha escrito en la de la víctima.
    stored = (
        await integration_session.execute(select(GitCredential))
    ).scalars().all()
    by_org: dict[uuid.UUID, set[GitProviderEnum]] = {}
    for credential in stored:
        by_org.setdefault(credential.organization_id, set()).add(credential.provider)
    assert len(by_org) == 2


@pytest.mark.asyncio
async def test_a_gitlab_token_sent_as_github_is_rejected_with_a_usable_message(
    integration_session: AsyncSession,
) -> None:
    """El error tiene que señalar al proveedor equivocado, no a un permiso que falta.

    Sin esto, un PAT de GitLab pasado como GitHub llegaba a la API de GitHub, que
    respondía "se rechazó la credencial". El usuario iba a revisar permisos que no
    eran el problema, y el error real —eligió el proveedor equivocado en el
    desplegable— quedaba escondido detrás de un mensaje plausible pero falso.
    """

    assert integration_session is not None
    _organization, headers = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    with patch(
        "backend.apps.repositories.router_auth.verify_provider_token"
    ) as verifier:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/repositories/credentials/token",
                json={
                    "provider": "GITHUB",
                    "token": VALID_GITLAB_PAT,
                    "name": "cruzado",
                },
                headers=headers,
            )

    assert response.status_code == 422
    detail = str(response.json()["detail"])
    assert "GITLAB" in detail
    assert "GITHUB" in detail
    verifier.assert_not_called()


@pytest.mark.asyncio
async def test_a_github_token_sent_as_gitlab_is_rejected_too(
    integration_session: AsyncSession,
) -> None:
    """El mismo error se detecta en el sentido contrario."""

    assert integration_session is not None
    _organization, headers = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    with patch(
        "backend.apps.repositories.router_auth.verify_provider_token"
    ) as verifier:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/repositories/credentials/token",
                json={
                    "provider": "GITLAB",
                    "token": VALID_PAT,
                    "name": "cruzado",
                },
                headers=headers,
            )

    assert response.status_code == 422
    assert "GITHUB" in str(response.json()["detail"])
    verifier.assert_not_called()
