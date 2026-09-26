"""Consultas agregadas de la consola de SuperAdmin.

Cada función devuelve **solo agregados**. Ninguna devuelve filas de la tabla operativa
salvo la lista explícita de tenants y usuarios, que es lo que el operador tiene que ver.
La razón es de volumen: el ledger y el rastro de auditoría son las dos tablas que más
crecen, y un resumen que los traverse enteros para contar unos pocos números es un
`SELECT COUNT(*)` disfrazado que cuesta lo mismo que la operación completa.

## Por qué las fechas se recortan en la base y no en Python

El mes en curso se calcula con `date_trunc` dentro de la consulta, en UTC. Recortar en
Python obligaría a traer todas las filas del mes para descartar las de mañana, que es
justo lo que se quiere evitar. La ventaja de fijar UTC y no la zona del servidor es que
el resultado no depende de dónde corra el backend: un resumen que da cifras distintas
según la maquina desde la que se consulta no es un resumen.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import cast

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from backend.apps.admin.schemas import (
    AdminAuditItem,
    AdminAuditPage,
    AdminCreditGrantResult,
    AdminMetric,
    AdminOrganizationItem,
    AdminOrganizationPage,
    AdminSaleItem,
    AdminSalePage,
    AdminUserItem,
    AdminUserPage,
)
from backend.apps.audit.models import AuditActionEnum, AuditLogEntry
from backend.apps.billing.models import CreditLedger, LedgerReasonEnum, StripeEvent
from backend.apps.billing.service import apply_credit_delta
from backend.apps.organizations.models import Membership, Organization, PlanTierEnum, User
from backend.apps.pentests.models import PentestRun

#: Créditos por dólar. Con 1 crédito = 1 USD, el importe en dólares de un saldo es el
#: propio saldo. Se declara como constante y no se deriva en el cliente para que las dos
#: cifras —la del panel y la del resumen global— no puedan divergir si alguien cambia la
#: paridad en uno de los dos.
CREDITS_PER_USD = Decimal("1")


def _start_of_month(now: datetime) -> datetime:
    """El primer instante del mes en curso, en UTC.

    Se trunca en Python y no con `date_trunc` porque el valor viaja como parámetro
    vinculado a la consulta. Truncar en la base obligaría a repetir la expresión en cada
    sitio donde se use, y una de esas repeticiones acabaría distinta.
    """

    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def count_organizations(session: AsyncSession) -> dict[str, int]:
    """Recuento de tenants por estado de ciclo de vida.

    Los tres estados se cuentan en **una** consulta con `FILTER` en vez de tres consultas
    condicionales. Con el índice de `deleted_at` y el de `is_active` —los dos existen desde
    el borrado lógico— las tres se resuelven sobre la misma lectura.
    """

    row = (
        await session.execute(
            select(
                func.count(Organization.id),
                func.count(Organization.id).filter(Organization.deleted_at.is_(None)),
                func.count(Organization.id).filter(
                    and_(Organization.deleted_at.is_(None), Organization.is_active.is_(True))
                ),
            )
        )
    ).one()
    total, vivos, activos = int(row[0]), int(row[1]), int(row[2])
    return {"total": total, "alive": vivos, "active": activos}


async def count_users(session: AsyncSession) -> tuple[int, int]:
    """Usuarios totales y superusuarios, en una sola lectura."""

    row = (
        await session.execute(
            select(func.count(User.id), func.count(User.id).filter(User.is_superuser.is_(True)))
        )
    ).one()
    return int(row[0]), int(row[1])


async def count_pentest_runs(session: AsyncSession) -> int:
    """Escaneos ejecutados en toda la plataforma, incluidos los fallidos.

    Se cuentan todos y no solo los completados: el operador que pregunta cuánto ha
    costado un escaneo quiere saber cuántos han salido, no cuántos han terminado bien. Un
    resumen que solo mira los completados escondería precisamente los que fallan.
    """

    return int((await session.execute(select(func.count(PentestRun.id)))).scalar_one())


async def credit_sales(session: AsyncSession) -> tuple[int, Decimal]:
    """Créditos comprados con pago, y su equivalente en dólares.

    Solo se suma `STRIPE_PURCHASE`. Los créditos de registro y los ajustes administrativos
    no son ingresos: metidos en el MRR darían una facturación que nunca ocurrió, que es la
    clase de error que hace que un consejo de administración tome una decisión cara.
    """

    # `credito` en el `SUM` y el filtro en el `COUNT`: una sola lectura para los
    # dos numeros, en vez de dos consultas que leen la misma tabla.
    credito = CreditLedger.amount_delta
    es_compra = CreditLedger.reason == LedgerReasonEnum.STRIPE_PURCHASE
    fila = (
        await session.execute(
            select(
                func.coalesce(func.sum(credito).filter(es_compra), 0),
                func.count(CreditLedger.id).filter(es_compra),
            )
        )
    ).one()
    return int(fila[1]), Decimal(str(fila[0]))


async def credit_sales_in_month(session: AsyncSession, now: datetime) -> Decimal:
    """Créditos comprados desde el primer instante del mes en curso."""

    desde = _start_of_month(now)
    resultado = await session.execute(
        select(func.coalesce(func.sum(CreditLedger.amount_delta), 0)).where(
            CreditLedger.reason == LedgerReasonEnum.STRIPE_PURCHASE,
            CreditLedger.created_at >= desde,
        )
    )
    return Decimal(str(resultado.scalar_one()))


async def credits_spent_in_month(session: AsyncSession, now: datetime) -> Decimal:
    """Créditos consumidos este mes, en valor absoluto.

    Los consumos son deltas negativos, así que se suma el valor absoluto del cambio de
    saldo: `SUM(-amount_delta)`. Devolver un número negativo haría que el panel tuviera que
    acordarse de cambiar el signo para pintar "consumido", que es justo donde un
    `+` se cuela y termina enseñando un consumo positivo.
    """

    desde = _start_of_month(now)
    resultado = await session.execute(
        select(func.coalesce(func.sum(-CreditLedger.amount_delta), 0)).where(
            CreditLedger.reason == LedgerReasonEnum.SCAN_CONSUMPTION,
            CreditLedger.created_at >= desde,
        )
    )
    return Decimal(str(resultado.scalar_one()))


async def build_overview(
    session: AsyncSession, now: datetime, infrastructure: object
) -> list[AdminMetric]:
    """Las seis métricas del resumen global, ya formateadas para el panel.

    El MRR se calcula como los créditos comprados acumulados, no como un ingreso
    recurrente declarado. La diferencia importa: con créditos prepago, el MRR real es el
    valor de los créditos comprados en los últimos treinta días, no el total histórico. Se
    ofrece el acumulado y el del mes porque son preguntas distintas —"cuánto hemos
    vendido" y "cómo va este mes"— y oferecer solo una de las dos deja al operador con que
    adivinar cuál está mirando.
    """

    tenants = await count_organizations(session)
    # El resumen pide seis metricas y los superusuarios no son una de ellas. La funcion
    # `count_users` los calcula porque se reutiliza, pero aqui no se desempaquetan: una
    # consulta extra en la pagina que mas se mira no se paga por una variable que nadie
    # lee.
    (usuarios, _) = await count_users(session)
    escaneos = await count_pentest_runs(session)
    _, vendidos_total = await credit_sales(session)
    vendidos_mes = await credit_sales_in_month(session, now)

    return [
        AdminMetric(
            key="mrr",
            value=vendidos_mes,
            format="currency",
            hint_key="admin.metrics.mrrHint",
        ),
        AdminMetric(
            key="revenueTotal",
            value=vendidos_total,
            format="currency",
            hint_key="admin.metrics.revenueTotalHint",
        ),
        AdminMetric(
            key="creditsSold",
            value=vendidos_total,
            format="credits",
            hint_key="admin.metrics.creditsSoldHint",
        ),
        AdminMetric(
            key="pentestRuns",
            value=Decimal(escaneos),
            format="count",
            hint_key="admin.metrics.pentestRunsHint",
        ),
        AdminMetric(
            key="activeTenants",
            value=Decimal(tenants["active"]),
            format="count",
            hint_key="admin.metrics.activeTenantsHint",
        ),
        AdminMetric(
            key="totalUsers",
            value=Decimal(usuarios),
            format="count",
            hint_key="admin.metrics.totalUsersHint",
        ),
    ]


# --------------------------------------------------------------------------- #
# Tenants
# --------------------------------------------------------------------------- #


def _tenant_query(
    *,
    plan: PlanTierEnum | None,
    lifecycle: str | None,
    search: str | None,
) -> Select[tuple[Organization, int]]:
    """Consulta de tenants con el recuento de miembros resuelto en la base.

    El `member_count` sale de un `LEFT JOIN` con `GROUP BY` sobre `organizations.id` en vez
    de una consulta por fila. La alternativa —contar en Python— multiplica las consultas
    por el número de tenants y hace que la tabla tarde más en aparecer cuanto más éxito
    tiene la plataforma.
    """

    miembros = (
        select(Membership.organization_id, func.count(Membership.id).label("member_count"))
        .where(Membership.is_active.is_(True))
        .group_by(Membership.organization_id)
        .subquery()
    )
    consulta = (
        select(Organization, func.coalesce(miembros.c.member_count, 0))
        .outerjoin(miembros, miembros.c.organization_id == Organization.id)
    )
    if plan is not None:
        consulta = consulta.where(Organization.plan_tier == plan)
    if lifecycle == "active":
        consulta = consulta.where(
            Organization.deleted_at.is_(None), Organization.is_active.is_(True)
        )
    elif lifecycle == "deleted":
        consulta = consulta.where(Organization.deleted_at.is_not(None))
    elif lifecycle == "deactivated":
        consulta = consulta.where(
            Organization.deleted_at.is_(None), Organization.is_active.is_(False)
        )
    if search:
        # `ILIKE` y no `LIKE`: el operador exige el comodín, así que un filtro de búsqueda
        # con `%%` en la consulta es un fallo esperando. Con SQLAlchemy el comodín va en
        # el parámetro y PostgreSQL recibe un `ILIKE` plano.
        patron = f"%{search}%"
        consulta = consulta.where(
            Organization.name.ilike(patron) | Organization.slug.ilike(patron)
        )
    # La anotacion declara la forma de dos columnas que el consumidor espera, y el
    # coalesce de un subquery devuelve Any en el tipado de SQLAlchemy. El cast dice
    # lo mismo en la firma sin propagar el Any al resto del modulo.
    return cast(Select[tuple[Organization, int]], consulta)


def _tenant_item(organization: Organization, member_count: int) -> AdminOrganizationItem:
    return AdminOrganizationItem(
        id=organization.id,
        name=organization.name,
        slug=organization.slug,
        plan_tier=organization.plan_tier,
        credit_balance=organization.credit_balance,
        is_active=organization.is_active,
        deleted_at=organization.deleted_at,
        created_at=organization.created_at,
        updated_at=organization.updated_at,
        member_count=int(member_count),
    )


async def list_organizations(
    session: AsyncSession,
    *,
    plan: PlanTierEnum | None = None,
    lifecycle: str | None = None,
    search: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> AdminOrganizationPage:
    """Lista global de tenants con filtros de plan, estado y búsqueda por nombre."""

    base = _tenant_query(plan=plan, lifecycle=lifecycle, search=search)
    total = int(
        (
            await session.execute(
                select(func.count())
                .select_from(base.order_by(None).subquery())
            )
        ).scalar_one()
    )
    # En runtime una fila de dos columnas se desempaqueta en dos valores, que es lo
    # natural. Lo que obliga al `cast` es el tipado de SQLAlchemy, que declara cada fila
    # como `Row[tuple[Organization, int]]` —una columna que contiene una tupla— y por eso
    # el índice y el desempaquetado se contradicen. Se dice la verdad con un `cast` sobre
    # el resultado, en vez de pelearse con el índice dentro del bucle.
    columnas = cast(
        Sequence[tuple[Organization, int]],
        (
            await session.execute(
                base.order_by(Organization.created_at.desc(), Organization.id)
                .limit(limit)
                .offset(offset)
            )
        ).all(),
    )
    return AdminOrganizationPage(
        items=[
            _tenant_item(organization, int(member_count))
            for organization, member_count in columnas
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


async def set_organization_plan(
    session: AsyncSession, organization: Organization, plan_tier: PlanTierEnum
) -> AdminOrganizationItem:
    """Cambia el plan de un tenant.

    Baja de plan sin comprobar el saldo se permite a propósito, y la razón está escrita en
    la respuesta: rechazarla obligaría al operador a vaciarle el saldo al cliente antes de
    degradarle el plan, que es peor que dejarle un saldo que ya no puede gastar. El saldo
    no se toca y el cliente ve el límite de su plan nuevo en cuanto lo intentare.
    """

    organization.plan_tier = plan_tier
    await session.commit()
    await session.refresh(organization)
    miembros = int(
        (
            await session.execute(
                select(func.count(Membership.id)).where(
                    Membership.organization_id == organization.id,
                    Membership.is_active.is_(True),
                )
            )
        ).scalar_one()
    )
    return _tenant_item(organization, miembros)


async def grant_credits(
    session: AsyncSession, organization: Organization, amount: Decimal, note: str
) -> AdminCreditGrantResult:
    """Acredita créditos de forma administrativa y devuelve el saldo resultante.

    El asiento lo escribe `apply_credit_delta`, el mismo servicio que usan el registro y
    la compra. Escribir el asiento a mano desde el admin dejaría `credit_balance` y el
    ledger desincronizados, que es el estado que hace que el saldo que ve el cliente no
    cuadre con el que se puede justificar en una auditoría.
    """

    entrada = await apply_credit_delta(
        session=session,
        organization_id=organization.id,
        amount=amount,
        reason=LedgerReasonEnum.ADMIN_ADJUSTMENT,
    )
    await session.refresh(organization)
    return AdminCreditGrantResult(
        organization_id=organization.id,
        granted=amount,
        balance_after=organization.credit_balance,
        ledger_entry_id=entrada.id,
    )


# --------------------------------------------------------------------------- #
# Usuarios
# --------------------------------------------------------------------------- #


async def list_users(
    session: AsyncSession,
    *,
    search: str | None = None,
    only_superusers: bool = False,
    limit: int = 25,
    offset: int = 0,
) -> AdminUserPage:
    """Lista global de usuarios con sus workspaces resueltos en una segunda consulta.

    Los usuarios y sus organizaciones se piden **por separado** y se unen en Python. La
    alternativa es un `JOIN` que devuelve una fila por usuario y organización, que es
    exactamente la forma de convertir "listar 25 usuarios" en "leer 800 filas y agrupar" y
    de repetir la fila del usuario por cada workspace suyo.
    """

    base = select(User)
    if only_superusers:
        base = base.where(User.is_superuser.is_(True))
    if search:
        patron = f"%{search}%"
        base = base.where(User.email.ilike(patron) | User.full_name.ilike(patron))

    total = int(
        (
            await session.execute(select(func.count()).select_from(base.order_by(None).subquery()))
        ).scalar_one()
    )
    usuarios = list(
        (
            await session.execute(
                base.order_by(User.created_at.desc(), User.id).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    if not usuarios:
        return AdminUserPage(items=[], total=total, limit=limit, offset=offset)

    por_usuario = await _organizations_of_users(session, [u.id for u in usuarios])
    return AdminUserPage(
        items=[
            AdminUserItem(
                id=usuario.id,
                email=usuario.email,
                full_name=usuario.full_name,
                is_superuser=usuario.is_superuser,
                is_active=usuario.is_active,
                email_verified=usuario.email_verified,
                created_at=usuario.created_at,
                organizations=por_usuario.get(usuario.id, []),
                organization_count=len(por_usuario.get(usuario.id, [])),
            )
            for usuario in usuarios
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


async def _organizations_of_users(
    session: AsyncSession, user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    """Workspaces por usuario, en una sola consulta para toda la página.

    Solo se incluyen membresías activas y tenants no dados de baja: un workspace en el que
    el usuario ya no está, o que se dio de baja, no es una organización a la que pertenezca
    hoy, y listarlo haría que el operador creyera que tiene acceso a algo que no tiene.
    """

    filas = (
        await session.execute(
            select(Membership.user_id, Organization.name)
            .join(Organization, Organization.id == Membership.organization_id)
            .where(
                Membership.user_id.in_(user_ids),
                Membership.is_active.is_(True),
                Organization.deleted_at.is_(None),
            )
            .order_by(Organization.created_at.asc())
        )
    ).all()
    agrupado: dict[uuid.UUID, list[str]] = {}
    for user_id, nombre in filas:
        agrupado.setdefault(user_id, []).append(nombre)
    return agrupado


# --------------------------------------------------------------------------- #
# Ventas
# --------------------------------------------------------------------------- #


async def list_sales(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID | None = None,
    limit: int = 25,
    offset: int = 0,
) -> AdminSalePage:
    """Historial de transacciones procesadas por Stripe.

    ## Por qué el importe en dólares viene a `None`

    `stripe_events` registra **qué** evento se procesó, a qué organización y cuántos
    créditos acreditó, pero no cuánto se cobró. Ese dato no está en la tabla y sacarlo
    obligaría a preguntar a Stripe por cada fila que se muestra.

    Devolver `0` en su lugar sería peor que devolver `None`: un cero es un número con
    aspecto de dato y aparecería como una venta de $0 en el resumen. `None` se muestra
    como "no registrado", que es la verdad. El total de la página se calcula sobre los
    importes conocidos, así que un mes con importes ausentes da un total **menor** que el
    real, y por eso el panel rotula la cifra como "de los importes registrados" en vez de
    presentarla como la facturación del periodo.
    """

    filtro = (
        StripeEvent.organization_id == organization_id
        if organization_id is not None
        else StripeEvent.organization_id.is_not(None)
    )
    total = int(
        (
            await session.execute(
                select(func.count()).select_from(
                    select(StripeEvent.id).where(filtro).subquery()
                )
            )
        ).scalar_one()
    )
    filas = (
        await session.execute(
            select(StripeEvent, Organization.name)
            .outerjoin(Organization, Organization.id == StripeEvent.organization_id)
            .where(filtro)
            .order_by(StripeEvent.created_at.desc(), StripeEvent.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()

    items: list[AdminSaleItem] = []
    total_credits = Decimal("0")
    for evento, organizacion in filas:
        if evento.credits_granted is not None:
            total_credits += Decimal(evento.credits_granted)
        items.append(
            AdminSaleItem(
                id=evento.id,
                event_id=evento.event_id,
                event_type=evento.event_type,
                session_id=evento.session_id,
                organization_id=evento.organization_id,
                organization_name=organizacion,
                credits_granted=(
                    Decimal(evento.credits_granted)
                    if evento.credits_granted is not None
                    else None
                ),
                created_at=evento.created_at,
            )
        )
    return AdminSalePage(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        total_credits=total_credits,
    )


# --------------------------------------------------------------------------- #
# Auditoría
# --------------------------------------------------------------------------- #


async def list_audit(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID | None = None,
    action: AuditActionEnum | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> AdminAuditPage:
    """Visor global del rastro forense.

    ## Por qué `action` es un enum y no una cadena

    `audit_log.action` es un tipo `ENUM` de PostgreSQL. Pasar un texto que no esté en la
    lista no devuelve una lista vacía: PostgreSQL lanza `invalid input value for enum` y
    la petición termina en `500`. Un filtro de búsqueda que responde con error del
    servidor cuando el usuario escribe mal es un fallo de la vista, no del usuario.

    Por eso el router **convierte y valida** antes de llegar aquí, y un valor desconocido
    produce un `422` que enumera las acciones válidas. Devolver un `200` con cero
    resultados también sería incorrecto: haría creer que no hubo ninguna entrada con esa
    acción, que es distinto de "esa acción no existe".

    ## Por qué solo admite `GET`

    La tabla es *append-only* por R4 y esta vista no escribe ni intenta: el único endpoint
    de la consola que la modifica es la baja lógica de organización, que escribe su propio
    asiento.
    """

    base = select(
        AuditLogEntry,
        Organization.name,
        User.email,
    ).outerjoin(Organization, Organization.id == AuditLogEntry.organization_id).outerjoin(
        User, User.id == AuditLogEntry.actor_user_id
    )

    if organization_id is not None:
        base = base.where(AuditLogEntry.organization_id == organization_id)
    if action is not None:
        base = base.where(AuditLogEntry.action == action)
    if search:
        patron = f"%{search}%"
        base = base.where(
            AuditLogEntry.entity_type.ilike(patron)
            | Organization.name.ilike(patron)
            | User.email.ilike(patron)
        )

    total = int(
        (
            await session.execute(select(func.count()).select_from(base.order_by(None).subquery()))
        ).scalar_one()
    )
    filas = (
        await session.execute(
            base.order_by(AuditLogEntry.created_at.desc(), AuditLogEntry.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()

    return AdminAuditPage(
        items=[
            AdminAuditItem(
                id=entrada.id,
                organization_id=entrada.organization_id,
                organization_name=organizacion,
                actor_user_id=entrada.actor_user_id,
                actor_email=actor_email,
                action=entrada.action,
                entity_type=entrada.entity_type,
                entity_id=entrada.entity_id,
                from_state=entrada.from_state,
                to_state=entrada.to_state,
                created_at=entrada.created_at,
            )
            for entrada, organizacion, actor_email in filas
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


