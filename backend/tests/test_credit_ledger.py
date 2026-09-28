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
    """Los asientos de una organización, **sin garantía de orden**.

    ## Por qué no lleva `ORDER BY`

    Porque no hay ningún campo por el que ordenar. `created_at` es `server_default=func.now()`,
    y en PostgreSQL `now()` es `transaction_timestamp()`: **la misma** para todas las filas de una
    transacción. Los tres asientos de esta prueba se escriben en la misma, así que comparten
    marca de tiempo hasta el microsegundo, y el `id` es un UUIDv4, que tampoco ordena por
    tiempo de inserción.

    Anadir un `ORDER BY created_at` daria una apariencia de orden sin ser una: el
    resultado seguiria dependiendo del plan de ejecucion, y una prueba que
    construyera hipotesis sobre las posiciones fallaria de forma intermitente.
    posiciones fallaría de forma intermitente y sin explicación útil.

    ## Qué hacer con las filas

    Identificarlas por su contenido —`reason`, `reference_id`, `amount_delta`— y no por su
    posición. Y para comprobar la **cadena** de saldos, usar los objetos que devuelve
    `apply_credit_delta`: ahí está el orden, porque lo calculó la función en el momento de
    insertar, y no hay que deducirlo de la tabla.
    """
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
    assert len(entries) == 3

    # Cada asiento se busca por lo que lo identifica, no por su posición.
    #
    # La versión anterior afirmaba `entries[0].reason == SIGNUP_BONUS`, `entries[1]...` y así
    # hasta el tercero. Parecía funcionar porque PostgreSQL devolvía las filas en el orden en
    # que se habían insertado, y eso es una casualidad de la implementation actual, no una
    # garantía: no hay `ORDER BY`, y como los tres asientos comparten `transaction_timestamp()`,
    # tampoco hay una columna por la que ordenar. Bastó con que el plan de ejecución cambiara para
    # que el bono de apertura saliera en otra posición y la prueba fallara sin que hubiera
    # cambiado nada del comportamiento que comprueba.
    #
    # La cadena de saldos sí se verifica, y de forma más fuerte que antes: los objetos que
    # devuelve `apply_credit_delta` llevan cada `balance_after` ya calculado, y son el orden real
    # de inserción. Eso no se deduce de la tabla, que es justamente lo que no se puede.
    # El bono de apertura lo siembra `_tenant` y no devuelve su handle, así que se identifica
    # por su razón, que en esta prueba es única: solo hay un asiento de ese tipo.
    bonos = [entry for entry in entries if entry.reason == LedgerReasonEnum.SIGNUP_BONUS]
    assert len(bonos) == 1
    bono = bonos[0]
    assert bono.amount_delta == Decimal("100")
    assert bono.balance_after == Decimal("100")

    por_id = {entry.id: entry for entry in entries}

    compra = por_id[purchase.id]
    assert compra.reason == LedgerReasonEnum.STRIPE_PURCHASE
    assert compra.reference_id == "cs_test_123"
    assert compra.balance_after == Decimal("350.50")

    gasto = por_id[consumption.id]
    assert gasto.reason == LedgerReasonEnum.SCAN_CONSUMPTION
    assert gasto.balance_after == Decimal("310.25")

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

    # Cada consumo se empareja con **su** escaneo por `reference_id`, y no por la posicion que
    # ocupa en la lista.
    #
    # `consumed[1] < consumed[0]` sostenia que el segundo asiento de la lista era el mas barato,
    # y que eso era por ser el escaneo rapido. Lo que media en realidad era el orden en que
    # PostgreSQL devolvia las filas, que no esta garantizado: los dos asientos se escriben en la
    # misma transaccion, comparten `transaction_timestamp()`, y el `id` es un UUIDv4 que no
    # ordena. Cuando el orden se invirtió, la prueba fallo diciendo que el escaneo rapido costaba
    # mas que el estandar, que es un resultado absurdo y era solo una casualidad del plan de
    # ejecucion.
    #
    # El `reference_id` es el identificador del escaneo, asi que emparejar por el convierte una
    # hipotesis sobre el orden en un hecho comprobable.
    consumos: dict[str, Decimal] = {}
    for entry in await _entries(integration_session, organization_id):
        if entry.reason == LedgerReasonEnum.SCAN_CONSUMPTION:
            assert entry.reference_id is not None
            consumos[entry.reference_id] = abs(entry.amount_delta)

    assert len(consumos) == 2
    estandar_id = standard.json()["id"]
    rapido_id = quick.json()["id"]
    assert consumos[rapido_id] < consumos[estandar_id], (
        f"el escaneo rapido costo {consumos[rapido_id]} y el estandar {consumos[estandar_id]}"
    )


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

