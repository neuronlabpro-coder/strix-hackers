"""Pruebas del ledger de créditos: atomicidad, inmutabilidad y rechazo por saldo.

Tras un `rollback()` cualquier instancia de ORM queda expirada, así que el
identificador de la organización se captura en un `UUID` plano antes de la
primera escritura. Acceder a `organization.id` después del rollback lanzaría un
`MissingGreenlet`, no un fallo de la lógica que se quiere probar.
"""

import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.models import CreditLedger, LedgerReasonEnum
from backend.apps.billing.service import (
    InsufficientCreditsError,
    apply_credit_delta,
    credit_balance_of,
)
from backend.apps.organizations.models import (
    Membership,
    Organization,
    RoleEnum,
    User,
)
from backend.apps.pentests.router import get_dispatch_pentest_run
from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration


async def _tenant(
    session: AsyncSession, *, balance: str = "0"
) -> tuple[Organization, User, dict[str, str]]:
    """Crea un tenant con su saldo inicial pasado por el ledger.

    Escribir `credit_balance` directamente dejaría el saldo denormalizado de la
    organización sin asiento que lo respalde, y `credit_balance_of` (que suma el
    ledger) no coincidiría con la columna. En producción el saldo de partida es
    siempre un bono de alta, nunca un `UPDATE` directo.
    """

    suffix = uuid.uuid4().hex
    organization = Organization(
        name=f"Ledger {suffix}",
        slug=f"ledger-{suffix}",
    )
    user = User(
        email=f"ledger-{suffix}@example.com",
        hashed_password="not-used",
        full_name="Ledger User",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(
        Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.ADMIN)
    )
    if Decimal(balance) > 0:
        session.add(
            CreditLedger(
                organization_id=organization.id,
                amount_delta=Decimal(balance),
                balance_after=Decimal(balance),
                reason=LedgerReasonEnum.SIGNUP_BONUS,
            )
        )
        organization.credit_balance = Decimal(balance)
    await session.commit()
    return organization, user, {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }


async def _entries(
    session: AsyncSession, organization_id: uuid.UUID
) -> list[CreditLedger]:
    result = await session.execute(
        select(CreditLedger).where(CreditLedger.organization_id == organization_id)
    )
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_credit_ledger_is_append_only(integration_session: AsyncSession) -> None:
    """R4: el libro mayor solo admite INSERT."""

    assert integration_session is not None
    organization, user, _headers = await _tenant(integration_session, balance="100")
    organization_id = organization.id
    await apply_credit_delta(
        session=integration_session,
        organization_id=organization_id,
        amount=Decimal("10.00"),
        reason=LedgerReasonEnum.SIGNUP_BONUS,
        actor_user_id=user.id,
    )
    entry_id = (await _entries(integration_session, organization_id))[0].id
    await integration_session.commit()

    for statement in (
        "UPDATE credit_ledger SET amount_delta = 999 WHERE id = :entry_id",
        "DELETE FROM credit_ledger WHERE id = :entry_id",
        "TRUNCATE credit_ledger",
    ):
        with pytest.raises(Exception) as rejection:
            async with integration_session.begin_nested():
                await integration_session.execute(
                    text(statement), {"entry_id": entry_id}
                )
        assert "append" in str(rejection.value).lower(), str(rejection.value)

    await integration_session.rollback()
    surviving = await _entries(integration_session, organization_id)
    # El bono de apertura de `_tenant` más el movimiento que acabamos de aplicar.
    assert len(surviving) == 2
    assert surviving[-1].amount_delta == Decimal("10.00")
    assert surviving[-1].reason == LedgerReasonEnum.SIGNUP_BONUS


@pytest.mark.asyncio
async def test_apply_credit_delta_updates_balance_and_records_snapshot(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization, user, _headers = await _tenant(integration_session, balance="100")
    organization_id = organization.id

    purchase = await apply_credit_delta(
        session=integration_session,
        organization_id=organization_id,
        amount=Decimal("250.50"),
        reason=LedgerReasonEnum.STRIPE_PURCHASE,
        actor_user_id=user.id,
        reference_id="cs_test_123",
    )
    consumption = await apply_credit_delta(
        session=integration_session,
        organization_id=organization_id,
        amount=Decimal("-40.25"),
        reason=LedgerReasonEnum.SCAN_CONSUMPTION,
        reference_id=str(uuid.uuid4()),
    )
    await integration_session.commit()

    assert purchase.balance_after == Decimal("350.50")
    assert consumption.balance_after == Decimal("310.25")
    assert consumption.amount_delta == Decimal("-40.25")
    assert await credit_balance_of(integration_session, organization_id) == Decimal("310.25")

    entries = await _entries(integration_session, organization_id)
    # El primero es el bono de apertura que siembra `_tenant`.
    assert len(entries) == 3
    assert entries[0].reason == LedgerReasonEnum.SIGNUP_BONUS
    assert entries[1].reason == LedgerReasonEnum.STRIPE_PURCHASE
    assert entries[1].reference_id == "cs_test_123"
    assert entries[1].balance_after == Decimal("350.50")
    assert entries[2].reason == LedgerReasonEnum.SCAN_CONSUMPTION
    # La suma de los deltas reconstruye el saldo sin recalcular nada.
    assert sum((entry.amount_delta for entry in entries), Decimal(0)) == Decimal("310.25")


@pytest.mark.asyncio
async def test_insufficient_credits_aborts_the_deduction(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization, _user, _headers = await _tenant(integration_session, balance="5")
    organization_id = organization.id

    with pytest.raises(InsufficientCreditsError) as shortage:
        await apply_credit_delta(
            session=integration_session,
            organization_id=organization_id,
            amount=Decimal("-10.00"),
            reason=LedgerReasonEnum.SCAN_CONSUMPTION,
            reference_id=str(uuid.uuid4()),
        )

    assert shortage.value.required == Decimal("10.00")
    assert shortage.value.available == Decimal("5")
    await integration_session.rollback()

    assert await credit_balance_of(integration_session, organization_id) == Decimal("5")
    consumption = [
        entry
        for entry in await _entries(integration_session, organization_id)
        if entry.reason == LedgerReasonEnum.SCAN_CONSUMPTION
    ]
    assert consumption == []


@pytest.mark.asyncio
async def test_rejected_deductions_never_change_the_balance(
    integration_session: AsyncSession,
) -> None:
    """Varios intentos fallidos seguidos no acumulan saldo ni asientos."""

    assert integration_session is not None
    organization, _user, _headers = await _tenant(integration_session, balance="0")
    organization_id = organization.id

    for _attempt in range(3):
        with pytest.raises(InsufficientCreditsError):
            await apply_credit_delta(
                session=integration_session,
                organization_id=organization_id,
                amount=Decimal("-1.00"),
                reason=LedgerReasonEnum.SCAN_CONSUMPTION,
                reference_id=str(uuid.uuid4()),
            )
        await integration_session.rollback()

    assert await credit_balance_of(integration_session, organization_id) == Decimal("0")
    assert await _entries(integration_session, organization_id) == []


@pytest.mark.asyncio
async def test_overdraw_is_impossible_even_with_partial_funding(
    integration_session: AsyncSession,
) -> None:
    """El saldo nunca queda negativo: lo que no cubre el saldo no se cobra."""

    assert integration_session is not None
    organization, _user, _headers = await _tenant(integration_session, balance="10")
    organization_id = organization.id

    # 10 créditos cubren un escaneo de 8: pasa y deja 2.
    await apply_credit_delta(
        session=integration_session,
        organization_id=organization_id,
        amount=Decimal("-8.00"),
        reason=LedgerReasonEnum.SCAN_CONSUMPTION,
        reference_id=str(uuid.uuid4()),
    )
    await integration_session.commit()
    assert await credit_balance_of(integration_session, organization_id) == Decimal("2")

    # Con 2 créditos, uno de 12 se rechaza entero: no hay saldo parcial.
    with pytest.raises(InsufficientCreditsError) as shortage:
        await apply_credit_delta(
            session=integration_session,
            organization_id=organization_id,
            amount=Decimal("-12.00"),
            reason=LedgerReasonEnum.SCAN_CONSUMPTION,
            reference_id=str(uuid.uuid4()),
        )
    assert shortage.value.available == Decimal("2")
    await integration_session.rollback()

    assert await credit_balance_of(integration_session, organization_id) == Decimal("2")
    consumption = [
        entry.amount_delta
        for entry in await _entries(integration_session, organization_id)
        if entry.reason == LedgerReasonEnum.SCAN_CONSUMPTION
    ]
    assert consumption == [Decimal("-8.00")]


@pytest.mark.asyncio
async def test_pentest_creation_is_rejected_with_402_when_balance_is_insufficient(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization, _user, headers = await _tenant(integration_session, balance="0")
    organization_id = organization.id
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/pentests/",
            json={
                "target_type": "DOMAIN",
                "target_identifier": "app.example.com",
                "scan_mode": "STANDARD",
            },
            headers=headers,
        )

    assert response.status_code == 402
    assert "crédito" in response.text.lower() or "credito" in response.text.lower()

    from backend.apps.pentests.models import PentestRun

    runs = (
        await integration_session.execute(
            select(PentestRun).where(PentestRun.organization_id == organization_id)
        )
    ).scalars().all()
    assert runs == []
    assert await _entries(integration_session, organization_id) == []


@pytest.mark.asyncio
async def test_pentest_creation_deducts_credits_when_balance_is_sufficient(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization, _user, headers = await _tenant(integration_session, balance="1000")
    organization_id = organization.id
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/pentests/",
            json={
                "target_type": "DOMAIN",
                "target_identifier": "app.example.com",
                "scan_mode": "STANDARD",
            },
            headers=headers,
        )

    assert response.status_code == 201
    entries = [
        entry
        for entry in await _entries(integration_session, organization_id)
        if entry.reason == LedgerReasonEnum.SCAN_CONSUMPTION
    ]
    assert len(entries) == 1
    assert entries[0].amount_delta < 0
    # El asiento cita al run que lo originó, para que el consumo sea trazable
    # desde la ficha del escaneo hasta el ledger.
    assert entries[0].reference_id == response.json()["id"]
    balance = await credit_balance_of(integration_session, organization_id)
    assert Decimal(0) < balance < Decimal("1000")


@pytest.mark.asyncio
async def test_quick_scan_costs_less_than_a_standard_scan(
    integration_session: AsyncSession,
) -> None:
    """El modo `QUICK` se cobra proporcionalmente porque consume menos tokens."""

    assert integration_session is not None
    organization, _user, headers = await _tenant(integration_session, balance="1000")
    organization_id = organization.id
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        standard = await client.post(
            "/api/v1/pentests/",
            json={
                "target_type": "DOMAIN",
                "target_identifier": "a.example.com",
                "scan_mode": "STANDARD",
            },
            headers=headers,
        )
        quick = await client.post(
            "/api/v1/pentests/",
            json={
                "target_type": "DOMAIN",
                "target_identifier": "b.example.com",
                "scan_mode": "QUICK",
            },
            headers=headers,
        )

    assert standard.status_code == 201
    assert quick.status_code == 201
    consumed = [
        abs(entry.amount_delta)
        for entry in await _entries(integration_session, organization_id)
        if entry.reason == LedgerReasonEnum.SCAN_CONSUMPTION
    ]
    assert len(consumed) == 2
    assert consumed[1] < consumed[0]


@pytest.mark.asyncio
async def test_failed_dispatch_refunds_the_reserved_credits(
    integration_session: AsyncSession,
) -> None:
    """Si el escaneo no llega a la cola, los créditos vuelven al tenant."""

    assert integration_session is not None
    organization, _user, headers = await _tenant(integration_session, balance="100")
    organization_id = organization.id
    transport = ASGITransport(app=app)

    # El override debe ser un cero-argumentos que *devuelve* el despachador,
    # que es la firma que espera `get_dispatch_pentest_run`.
    app.dependency_overrides[get_dispatch_pentest_run] = lambda: _raise_dispatch
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/pentests/",
                json={
                    "target_type": "DOMAIN",
                    "target_identifier": "app.example.com",
                    "scan_mode": "STANDARD",
                },
                headers=headers,
            )
    finally:
        app.dependency_overrides.pop(get_dispatch_pentest_run, None)

    assert response.status_code == 503
    entries = await _entries(integration_session, organization_id)
    # Consumo y reembolso se compensan: el saldo vuelve a su valor inicial.
    assert await credit_balance_of(integration_session, organization_id) == Decimal("100")
    consumption = [entry for entry in entries if entry.reason == LedgerReasonEnum.SCAN_CONSUMPTION]
    assert len(consumption) == 2
    assert consumption[0].amount_delta < 0
    assert consumption[1].amount_delta == -consumption[0].amount_delta
    assert consumption[1].reference_id == f"{consumption[0].reference_id}:refund"


def _raise_dispatch(_run_id: str) -> str:
    raise RuntimeError("cola de Celery no disponible")

