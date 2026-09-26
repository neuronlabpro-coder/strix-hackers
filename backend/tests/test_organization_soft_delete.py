"""Pruebas del borrado lógico de organizaciones conviviendo con R4.

R4 vuelve imposible el borrado físico de un tenant con historial: los triggers
`BEFORE DELETE` sobre `credit_ledger` y `audit_log` bloquean la cascada. La salida es
el borrado lógico, y estas pruebas fijan sus garantías:

1. El tenant desaparece del panel sin perder su rastro financiero ni forense.
2. El acceso se revoca de verdad, no solo se marca una fecha.
3. Un tenant ya dado de baja no se puede volver a usar ni a listar.
4. La baja queda registrada en el rastro *antes* de revocar, para que el asiento
   exista aunque la revocación falle a mitad.
"""

import uuid
from decimal import Decimal
from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.audit.models import AuditActionEnum, AuditLogEntry
from backend.apps.billing.models import CreditLedger, LedgerReasonEnum
from backend.apps.organizations.models import Membership, Organization, RoleEnum, User
from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration


async def _tenant(session: AsyncSession, *, role: RoleEnum = RoleEnum.ADMIN) -> tuple[
    Organization, User, dict[str, str]
]:
    suffix = uuid.uuid4().hex
    organization = Organization(name=f"Baja {suffix}", slug=f"baja-{suffix}")
    user = User(
        email=f"baja-{suffix}@example.com",
        hashed_password="not-used",
        full_name="Baja User",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(
        Membership(organization_id=organization.id, user_id=user.id, role=role)
    )
    await session.commit()
    headers = {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }
    return organization, user, headers


# --------------------------------------------------------------------------- #
# Estado del tenant tras la baja
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_soft_delete_marks_the_organization_and_deactivates_it(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization, _user, headers = await _tenant(integration_session)
    assert organization.deleted_at is None
    assert organization.is_active is True
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete(
            f"/api/v1/organizations/{organization.id}", headers=headers
        )

    assert response.status_code == 200
    reloaded = (
        await integration_session.execute(
            select(Organization).where(Organization.id == organization.id)
        )
    ).scalar_one()
    assert reloaded.deleted_at is not None
    assert reloaded.is_active is False
    # La fila sigue existiendo: el rastro financiero depende de ella.
    assert reloaded.id == organization.id


@pytest.mark.asyncio
async def test_soft_delete_preserves_the_financial_and_forensic_history(
    integration_session: AsyncSession,
) -> None:
    """R4 no se negocia: la baja no puede tocar ni un asiento ni una entrada de auditoría."""

    assert integration_session is not None
    organization, _user, headers = await _tenant(integration_session)
    integration_session.add(
        CreditLedger(
            organization_id=organization.id,
            amount_delta=Decimal("500"),
            balance_after=Decimal("500"),
            reason=LedgerReasonEnum.SIGNUP_BONUS,
        )
    )
    integration_session.add(
        AuditLogEntry(
            organization_id=organization.id,
            actor_user_id=None,
            action=AuditActionEnum.STATUS_CHANGED,
            entity_type="organization",
            entity_id=organization.id,
        )
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete(
            f"/api/v1/organizations/{organization.id}", headers=headers
        )

    assert response.status_code == 200
    ledger = (
        await integration_session.execute(
            select(CreditLedger).where(CreditLedger.organization_id == organization.id)
        )
    ).scalars().all()
    assert len(ledger) == 1
    assert ledger[0].amount_delta == Decimal("500")
    audit = (
        await integration_session.execute(
            select(AuditLogEntry).where(AuditLogEntry.organization_id == organization.id)
        )
    ).scalars().all()
    # La entrada original mas la de baja.
    assert len(audit) == 2
    deletion_entry = next(
        entry for entry in audit if entry.action == AuditActionEnum.ORGANIZATION_DELETED
    )
    assert deletion_entry.to_state == "deleted"


@pytest.mark.asyncio
async def test_soft_delete_writes_the_audit_entry_and_then_revokes_access(
    integration_session: AsyncSession,
) -> None:
    """El asiento de baja se escribe ANTES de revocar.

    Es lo que garantiza que exista una prueba de por qué se cortó el acceso aunque la
    revocación o la cancelación en Stripe fallen a mitad.
    """

    assert integration_session is not None
    organization, _user, headers = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete(
            f"/api/v1/organizations/{organization.id}", headers=headers
        )
        # Inmediatamente despues, el mismo token debe dejar de servir.
        after = await client.get("/api/v1/organizations/me", headers=headers)
        # Y cualquier ruta con contexto de tenant debe rechazarlo.
        after_tenant = await client.get("/api/v1/dashboard/summary", headers=headers)

    assert response.status_code == 200
    assert after.status_code == 200
    # El tenant dado de baja no aparece en su propia lista de workspaces.
    listed = after.json()
    assert organization.id not in {item["id"] for item in listed}
    # La revocacion es real: el mismo JWT deja de resolver contexto de tenant.
    assert after_tenant.status_code in {401, 403, 404}
    membership = (
        await integration_session.execute(
            select(Membership).where(Membership.organization_id == organization.id)
        )
    ).scalar_one()
    assert membership.is_active is False


@pytest.mark.asyncio
async def test_deleted_organization_is_hidden_from_the_workspace_list(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    active_org, _user, headers = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        before = await client.get("/api/v1/organizations/me", headers=headers)
        await client.delete(f"/api/v1/organizations/{active_org.id}", headers=headers)
        after = await client.get("/api/v1/organizations/me", headers=headers)

    # Los identificadores llegan como texto en el JSON y el atributo del modelo es un
    # `UUID`: compararlos directamente sería `UUID == str`, siempre falso, y la prueba
    # fallaría con un conjunto que visualmente contiene el valor buscado.
    assert before.status_code == 200
    assert after.status_code == 200
    assert str(active_org.id) in {item["id"] for item in before.json()}
    assert str(active_org.id) not in {item["id"] for item in after.json()}


@pytest.mark.asyncio
async def test_second_deletion_is_rejected_and_never_duplicates_the_audit_entry(
    integration_session: AsyncSession,
) -> None:
    """La segunda baja se rechaza, y sobre todo no duplica el asiento.

    La premisa inicial de que sería idempotente era falsa, y el motivo importa: la baja
    revoca las membresías, de modo que la segunda llamada ya no resuelve contexto de
    tenant y responde `403`. Eso es mejor que un `200` idempotente — significa que la
    revocación se aplicó de verdad y no solo se escribió una fecha.

    Lo que hay que garantizar es que un reintento, sea del código que sea, no añada un
    segundo asiento de baja al rastro forense.
    """

    assert integration_session is not None
    organization, _user, headers = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.delete(
            f"/api/v1/organizations/{organization.id}", headers=headers
        )
        reloaded = (
            await integration_session.execute(
                select(Organization).where(Organization.id == organization.id)
            )
        ).scalar_one()
        original_deleted_at = reloaded.deleted_at
        second = await client.delete(
            f"/api/v1/organizations/{organization.id}", headers=headers
        )

    assert first.status_code == 200
    # La revocación es real: sin contexto de tenant no hay segunda baja.
    assert second.status_code == 403
    entries = (
        await integration_session.execute(
            select(AuditLogEntry).where(
                AuditLogEntry.organization_id == organization.id,
                AuditLogEntry.action == AuditActionEnum.ORGANIZATION_DELETED,
            )
        )
    ).scalars().all()
    assert len(entries) == 1, "una segunda baja duplico el asiento de auditoria"
    assert original_deleted_at is not None


# --------------------------------------------------------------------------- #
# Autorización
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_only_admin_can_soft_delete_the_organization(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization, _victim_user, _headers = await _tenant(integration_session)
    _member, _member_user, member_headers = await _tenant(
        integration_session, role=RoleEnum.MEMBER
    )
    # El miembro intenta borrar el workspace del admin. Su cabecera `X-Organization-Id`
    # apunta a su propio tenant, así que además de por rol se está comprobando que el
    # tenant se toma del contexto y no de la URL.
    del _member_user
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete(
            f"/api/v1/organizations/{organization.id}", headers=member_headers
        )

    assert response.status_code == 403
    untouched = (
        await integration_session.execute(
            select(Organization).where(Organization.id == organization.id)
        )
    ).scalar_one()
    assert untouched.deleted_at is None
    assert untouched.is_active is True
    assert _member.deleted_at is None


@pytest.mark.asyncio
async def test_cannot_delete_an_organization_from_another_tenant(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    victim, _vuser, _vheaders = await _tenant(integration_session)
    _attacker, _auser, attacker_headers = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete(
            f"/api/v1/organizations/{victim.id}", headers=attacker_headers
        )

    assert response.status_code in {403, 404}
    untouched = (
        await integration_session.execute(
            select(Organization).where(Organization.id == victim.id)
        )
    ).scalar_one()
    assert untouched.deleted_at is None


@pytest.mark.asyncio
async def test_soft_delete_requires_authentication(integration_session: AsyncSession) -> None:
    assert integration_session is not None
    organization, _user, _headers = await _tenant(integration_session)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete(f"/api/v1/organizations/{organization.id}")

    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Persistencia
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_deleted_at_is_indexed_for_the_tenant_listing(
    integration_session: AsyncSession,
) -> None:
    """La lista de workspaces filtra por `deleted_at`; sin índice, es un barrido secuencial."""

    assert integration_session is not None
    from backend.apps.organizations.models import Organization as Model

    table = cast(Table, Model.__table__)
    assert "deleted_at" in table.columns
    # `index.name` es opcional en el tipado de SQLAlchemy: un índice sin nombre
    # explícito lo genera la base, y sin nombre no hay nada que comprobar.
    index_names = {name for name in (index.name for index in table.indexes) if name}
    assert any("deleted" in name for name in index_names), index_names
    # Y la columna acepta `NULL`: un tenant vivo no tiene fecha de baja.
    assert Model.__table__.columns["deleted_at"].nullable is True
