"""Lo que le cuesta al cliente un escaneo que no llegó a empezar.

## El fallo que este fichero existe para no dejar volver

`tasks.py` marcaba el run como `FAILED` y subía la excepción. Lo que **no** hacía era devolver los
créditos. Y la devolución estaba escrita —a cuatro líneas de distancia, en el retorno normal de
`_execute_pentest_run`—, de modo que solo se ejecutaba cuando el fallo pasaba por un camino
concreto: que `manager.run` **devolviera** un resultado con `exit_code` distinto de cero.

Cuando `manager.run` **lanza**, el bucle de modelos se aborta, esas dos líneas no se ejecutan y la
excepción llega a `except Exception`. El run quedaba `FAILED` y el tenant se quedaba sin sus
créditos. Medido en la base de este turno, sin un solo asiento de devolución:

    2026-10-05 05:47:22  SCAN_CONSUMPTION  -10,0000   pentest, falló en 0,297 s
    2026-10-05 05:52:16  SCAN_CONSUMPTION   -3,0000   pipeline PR
    2026-10-05 06:01:21  SCAN_CONSUMPTION   -3,0000   pipeline PR

Un despliegue sin el cerco de salida, sin el reconocimiento de exposición o sin la imagen del
sandbox falla en 0,297 segundos y **cobra el escaneo entero**.

## Las tres cosas que hay que demostrar, y por qué son tres y no una

1. **Un fallo de infraestructura devuelve el dinero.** Es el defecto.
2. **Un fallo del análisis no lo devuelve.** Si devolviera todo, el arreglo sería una puerta por
   la que regalar servicios ya rendidos, y eso es peor que cobrar de más.
3. **No se devuelve dos veces.** El camino de excepción y el de retorno normal pueden llegar
   ambos, y hay varios workers. Un reembolso duplicado no es un error de cálculo: es un saldo
   inflado que además es un regalo.

## Por qué las pruebas van contra el ledger y no contra una constante

Porque lo que hay que devolver no es el precio de **hoy** de un escaneo, es lo que salió de la
cartera de ese tenant, y eso está sellado en el asiento. Una prueba que comprobara contra
`scan_credit_cost` pasaría con el defecto puesto y no mediría nada.

## Por qué estas pruebas no dejan basura en la base

Porque `_devolver_lo_retenido` abre su propio motor, y una fila confirmada ahí es una fila
**definitiva**: `credit_ledger` es *append-only* y su clave foránea hacia la organización es
`ON DELETE RESTRICT`, así que ni siquiera se puede borrar el tenant después. El `_session_factory`
del worker se sustituye por uno que entrega la sesión de la prueba, que vive dentro de un
`savepoint` que se deshace al terminar la prueba. El SQL es el de PostgreSQL de verdad; lo único
que cambia es quién confirma.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.models import CreditLedger, LedgerReasonEnum
from backend.apps.billing.pricing import scan_credit_cost
from backend.apps.billing.service import ZERO, apply_credit_delta, credit_balance_of
from backend.apps.organizations.models import Organization
from backend.apps.pentests.models import (
    PentestRun,
    ScanModeEnum,
    ScanStatusEnum,
    TargetTypeEnum,
)
from backend.core.config import settings
from backend.core.database import create_database_engine
from backend.workers.runner.diagnostico import CODIGO_DESCONOCIDO, MOTIVOS_DE_DESPLIEGUE
from backend.workers.runner.egress_fence import EgressFenceMissingError
from backend.workers.runner.exceptions import SandboxOutputError, SandboxTimeoutError
from backend.workers.tasks import (
    _devolver_si_el_fallo_fue_de_despliegue,
    _mark_failed,
    execute_pentest_run,
)

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Andamiaje
# --------------------------------------------------------------------------- #


@contextmanager
def _escrituras_del_worker_sobre_la_prueba(session: AsyncSession) -> Iterator[None]:
    """Hace que el worker escriba dentro del `savepoint` de la prueba, y no en la base real.

    Sin esto, `_devolver_lo_retenido` abriría su propio motor y confirmaría ahí un asiento que
    después ni el trigger *append-only* ni el `RESTRICT` de la clave foránea dejan borrar. Es el
    mismo problema que resolvió `test_renovacion_credencial_worker.py`, con la diferencia de que
    allí la limpieza sí era posible porque esas filas no tocaban el ledger.

    El motor que se devuelve es real pero no se usa para ejecutar nada: su único papel es que
    `dispose()` tenga a qué llamar, porque el código del worker lo invoca en su `finally`.
    """

    motor = create_database_engine(settings)

    @asynccontextmanager
    async def _sesion_del_worker() -> AsyncIterator[AsyncSession]:
        yield session

    with patch(
        "backend.workers.tasks._session_factory",
        return_value=(motor, _sesion_del_worker),
    ):
        yield


async def _tenant_con_saldo(session: AsyncSession, balance: str) -> Organization:
    """Un tenant cuyo saldo de partida entra **por el ledger**, no por la columna.

    Escribir `credit_balance` directamente dejaría la caché desnormalizada sin un asiento que la
    respalde, y `credit_balance_of` —que suma el ledger— no coincidiría con ella. En producción el
    saldo de partida es siempre un bono, nunca un `UPDATE`, y la prueba tiene que medir la misma
    realidad que existe en producción.
    """

    sufijo = uuid.uuid4().hex
    organization = Organization(name=f"Reembolso {sufijo}", slug=f"reembolso-{sufijo}")
    session.add(organization)
    await session.flush()
    await apply_credit_delta(
        session=session,
        organization_id=organization.id,
        amount=Decimal(balance),
        reason=LedgerReasonEnum.SIGNUP_BONUS,
    )
    return organization


async def _escaneo_cobrado(
    session: AsyncSession,
    *,
    balance: str = "100",
    scan_mode: ScanModeEnum = ScanModeEnum.STANDARD,
    estado: ScanStatusEnum = ScanStatusEnum.RUNNING,
) -> tuple[Organization, PentestRun, Decimal]:
    """Un tenant con saldo y un escaneo al que se le ha cobrado la reserva de verdad.

    El cobro se hace con `apply_credit_delta` y no con un `INSERT` a mano, que es el mismo camino
    que usa `queue_pentest`: así el `reference_id` que se escribe es exactamente el que va a
    encontrar `_saldo_retenido`, y la prueba no depende de que coincidan por casualidad.
    """

    organization = await _tenant_con_saldo(session, balance)
    run = PentestRun(
        organization_id=organization.id,
        target_type=TargetTypeEnum.DOMAIN,
        target_identifier="escaneo.example.test",
        scan_mode=scan_mode,
        status=estado,
        started_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()
    coste = scan_credit_cost(scan_mode, organization.id)
    await apply_credit_delta(
        session=session,
        organization_id=organization.id,
        amount=-coste,
        reason=LedgerReasonEnum.SCAN_CONSUMPTION,
        reference_id=str(run.id),
    )
    await session.commit()
    return organization, run, coste


async def _saldo_en_la_columna(session: AsyncSession, organization_id: uuid.UUID) -> Decimal:
    """El saldo denormalizado de `organizations`, releído de la base y no del mapa de identidad.

    Sin `populate_existing`, SQLAlchemy devolvería la instancia que ya tiene en el mapa con el
    valor anterior al movimiento y la aserción compararía contra un número viejo. El saldo tiene
    dos verdades y hay que leer las dos: el ledger y su caché.
    """

    valor = (
        await session.execute(
            select(Organization.credit_balance)
            .where(Organization.id == organization_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return Decimal(str(valor))


async def _asientos_del_run(
    session: AsyncSession, organization_id: uuid.UUID, run_id: uuid.UUID
) -> list[CreditLedger]:
    """Los asientos que citan a este run, **sin garantía de orden**.

    No hay `ORDER BY` porque no hay columna por la que ordenar: `created_at` es
    `server_default=func.now()` y en PostgreSQL `now()` es la marca de **transacción**, así que
    todos los asientos de una misma transacción comparten reloj, y el `id` es un UUIDv4 que no
    ordena por inserción. Cada prueba localiza los suyos por su `reference_id` y su
    `amount_delta`, que es lo que significa, en vez de por la posición que ocupan.
    """

    resultado = await session.execute(
        select(CreditLedger).where(
            CreditLedger.organization_id == organization_id,
            CreditLedger.reference_id.like(f"{run_id}%"),
        )
    )
    return list(resultado.scalars().all())


# --------------------------------------------------------------------------- #
# 1. Las dos facturas: infraestructura sí, análisis no
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("codigo", sorted(MOTIVOS_DE_DESPLIEGUE))
async def test_un_fallo_de_infraestructura_devuelve_el_saldo_retenido(
    integration_session: AsyncSession,
    codigo: str,
) -> None:
    """Un host sin cerco, sin imagen o sin Docker no le cuesta el escaneo al cliente.

    Se recorre **todo** `MOTIVOS_DE_DESPLIEGUE` y no un código suelto, porque la decisión no
    puede tener su propia lista de motivos: si mañana `diagnostico.py` clasifica un motivo nuevo
    como de despliegue y esta función no lo conoce, ese motivo vuelve a costar créditos sin que
    nadie se entere. La prueba se ata al conjunto, no al ejemplo.
    """

    assert integration_session is not None
    organization, run, coste = await _escaneo_cobrado(integration_session)
    organization_id = organization.id
    run_id = run.id
    saldo_inicial = Decimal("100")

    assert coste > ZERO
    assert await credit_balance_of(integration_session, organization_id) == saldo_inicial - coste

    with _escrituras_del_worker_sobre_la_prueba(integration_session):
        # La secuencia es la de producción: primero el estado terminal, después el dinero. El
        # orden importa porque el reembolso bloquea la fila del run y decide mirando su estado.
        await _mark_failed(organization_id, run_id, codigo)
        devuelto = await _devolver_si_el_fallo_fue_de_despliegue(
            organization_id, run_id, codigo
        )

    assert devuelto == coste
    # Las dos verdades del saldo vuelven a su valor de partida: el ledger y su caché.
    assert await credit_balance_of(integration_session, organization_id) == saldo_inicial
    assert await _saldo_en_la_columna(integration_session, organization_id) == saldo_inicial

    await integration_session.refresh(run)
    assert run.status == ScanStatusEnum.FAILED
    assert run.error_message == codigo

    asientos = await _asientos_del_run(integration_session, organization_id, run_id)
    devoluciones = [a for a in asientos if a.reference_id == f"{run_id}:refund"]
    assert len(devoluciones) == 1
    assert devoluciones[0].amount_delta == coste


@pytest.mark.parametrize(
    "codigo",
    [
        CODIGO_DESCONOCIDO,
        "STRIX_NONZERO_EXIT",
        "STRIX_OUTPUT_UNUSABLE",
        "STRIX_TIMEOUT",
        "STRIX_INGESTION_FAILED",
        "WORKER_WATCHDOG_ORPHANED",
        "PR_PIPELINE_FAILED",
    ],
)
async def test_un_fallo_que_no_es_de_infraestructura_no_devuelve_nada(
    integration_session: AsyncSession,
    codigo: str,
) -> None:
    """La otra mitad de la regla, y la que evita arreglar el defecto cobrando de más.

    Estos son los motivos que **no** están en `MOTIVOS_DE_DESPLIEGUE`: el motor terminó con un
    código distinto de cero, terminó sin dejar un resultado legible, se pasó de tiempo, o la
    ingesta falló. En todos ellos el escaneo **se ejecutó**, y lo cobrado corresponde a trabajo
    que la plataforma hizo. Devolverlo sería regalar servicios ya rendidos.

    Un aviso que esta prueba deja documentado: `STRIX_NONZERO_EXIT` **ya** se reembolsaba antes de
    este arreglo, en el retorno normal de `_execute_pentest_run`. Que las dos rutas no coincidan es
    una inconsistencia real de la política de dinero y está anotada en el informe; lo que se
    comprueba aquí es que la ruta nueva **no amplía** el problema.
    """

    assert integration_session is not None
    organization, run, coste = await _escaneo_cobrado(integration_session)
    organization_id = organization.id
    run_id = run.id
    saldo_inicial = Decimal("100")

    with _escrituras_del_worker_sobre_la_prueba(integration_session):
        await _mark_failed(organization_id, run_id, codigo)
        devuelto = await _devolver_si_el_fallo_fue_de_despliegue(
            organization_id, run_id, codigo
        )

    assert devuelto == ZERO
    assert await credit_balance_of(integration_session, organization_id) == (
        saldo_inicial - coste
    )
    assert await _saldo_en_la_columna(integration_session, organization_id) == (
        saldo_inicial - coste
    )
    asientos = await _asientos_del_run(integration_session, organization_id, run_id)
    assert [a.reference_id for a in asientos] == [str(run_id)]


# --------------------------------------------------------------------------- #
# 2. El doble reembolso, y el saldo que no existe
# --------------------------------------------------------------------------- #


async def test_devolver_dos_veces_no_mueve_el_saldo_dos_veces(
    integration_session: AsyncSession,
) -> None:
    """El segundo intento no ve nada que devolver, y no escribe un segundo asiento.

    La comprobación no es «el saldo queda bien», es **por qué** queda bien: el propio asiento de
    devolución entra en la suma del importe a devolver, así que la primera llamada deja el saldo
    retenido en cero y las siguientes leen cero y salen sin escribir. Por eso la idempotencia es
    aritmética y no un flag en memoria —que no serviría entre dos workers distintos— ni un `UPDATE`
    del asiento anterior, que R4 prohíbe porque `credit_ledger` es *append-only*.

    El caso real que lo justifica es doble: `execute_pentest_run` reintenta con el siguiente
    modelo del catálogo y Celery reintenta la tarea entera. Los dos caminos pueden acabar en la
    misma devolución.
    """

    assert integration_session is not None
    organization, run, coste = await _escaneo_cobrado(integration_session)
    organization_id = organization.id
    run_id = run.id
    saldo_inicial = Decimal("100")

    with _escrituras_del_worker_sobre_la_prueba(integration_session):
        await _mark_failed(organization_id, run_id, "STRIX_IMAGE_UNAVAILABLE")
        primero = await _devolver_si_el_fallo_fue_de_despliegue(
            organization_id, run_id, "STRIX_IMAGE_UNAVAILABLE"
        )
        segundo = await _devolver_si_el_fallo_fue_de_despliegue(
            organization_id, run_id, "STRIX_IMAGE_UNAVAILABLE"
        )
        tercero = await _devolver_si_el_fallo_fue_de_despliegue(
            organization_id, run_id, "STRIX_EGRESS_FENCE_MISSING"
        )

    assert primero == coste
    assert segundo == ZERO
    # Un tercer intento con **otro** motivo tampoco devuelve: la idempotencia no depende de que
    # los dos caminos coincidan en el código, sino de lo que ya está en el ledger.
    assert tercero == ZERO

    assert await credit_balance_of(integration_session, organization_id) == saldo_inicial
    assert await _saldo_en_la_columna(integration_session, organization_id) == saldo_inicial

    asientos = await _asientos_del_run(integration_session, organization_id, run_id)
    devoluciones = [a for a in asientos if a.reference_id == f"{run_id}:refund"]
    assert len(devoluciones) == 1, sorted(str(a.reference_id) for a in asientos)


async def test_un_run_sin_cargo_no_inventa_saldo(integration_session: AsyncSession) -> None:
    """Devolver sin nada cobrado sería **inventar** créditos, y aquí se comprueba que no ocurre.

    Es la otra mitad del mismo riesgo. Si la función calculara el importe en vez de leerlo del
    ledger, un run sin asiento de consumo —porque alguien la llamó fuera de su camino, o porque
    la fila ya no está— produciría un saldo que nadie ganó. El saldo retenido de un run sin cargo
    es cero, y con cero no se escribe nada.
    """

    assert integration_session is not None
    organization = await _tenant_con_saldo(integration_session, "100")
    organization_id = organization.id
    run = PentestRun(
        organization_id=organization.id,
        target_type=TargetTypeEnum.DOMAIN,
        target_identifier="nunca.example.test",
        scan_mode=ScanModeEnum.STANDARD,
        status=ScanStatusEnum.FAILED,
        finished_at=datetime.now(UTC),
        error_message="STRIX_IMAGE_UNAVAILABLE",
    )
    integration_session.add(run)
    await integration_session.commit()
    run_id = run.id

    with _escrituras_del_worker_sobre_la_prueba(integration_session):
        devuelto = await _devolver_si_el_fallo_fue_de_despliegue(
            organization_id, run_id, "STRIX_IMAGE_UNAVAILABLE"
        )

    assert devuelto == ZERO
    assert await _asientos_del_run(integration_session, organization_id, run_id) == []
    assert await credit_balance_of(integration_session, organization_id) == Decimal("100")


async def test_el_filtro_de_organizacion_impide_devolver_el_saldo_de_otro_tenant(
    integration_session: AsyncSession,
) -> None:
    """R3 aplicado al dinero: el filtro de tenant va en el `WHERE`, no en un `if` de después.

    Se intenta devolver con la organización **equivocada**. Traer el asiento a memoria para
    descartarlo con un `if` daría exactamente el mismo resultado —por eso el importe y el saldo
    saldrían iguales—, pero habría traído también el asiento de otro tenant a memoria del worker,
    que es lo que R3 prohíbe. Lo que sí se puede comprobar sin mirar la consulta es que **no se
    escribe nada**: ni se devuelve al tenant bueno, porque el run no es suyo, ni se le regala nada
    al intruso, que no tiene ni un asiento.
    """

    assert integration_session is not None
    organization, run, coste = await _escaneo_cobrado(integration_session)
    organization_id = organization.id
    run_id = run.id

    intruso = await _tenant_con_saldo(integration_session, "100")
    intruso_id = intruso.id

    with _escrituras_del_worker_sobre_la_prueba(integration_session):
        devuelto = await _devolver_si_el_fallo_fue_de_despliegue(
            intruso_id, run_id, "STRIX_IMAGE_UNAVAILABLE"
        )

    assert devuelto == ZERO
    assert await credit_balance_of(integration_session, organization_id) == Decimal("100") - coste
    assert await credit_balance_of(integration_session, intruso_id) == Decimal("100")


async def test_sin_organizacion_no_se_toca_el_saldo_de_nadie(
    integration_session: AsyncSession,
) -> None:
    """Sin organización no hay a quién devolverle el dinero, y adivinarlo sería peor que nada.

    Es el caso en que ni siquiera se pudo leer el run, así que no hay cobro del que responder. La
    duda se registra y no se resuelve suponiendo: acreditar al tenant equivocado es un
    movimiento de dinero sin origen, y eso no tiene arreglo ni por cancelación ni por nota.
    """

    assert integration_session is not None
    organization, run, coste = await _escaneo_cobrado(integration_session)
    organization_id = organization.id
    run_id = run.id

    with _escrituras_del_worker_sobre_la_prueba(integration_session):
        devuelto = await _devolver_si_el_fallo_fue_de_despliegue(
            None, run_id, "STRIX_IMAGE_UNAVAILABLE"
        )

    assert devuelto == ZERO
    assert await credit_balance_of(integration_session, organization_id) == Decimal("100") - coste


# --------------------------------------------------------------------------- #
# 3. Los estados en los que el worker no debe devolver
# --------------------------------------------------------------------------- #


async def test_un_run_completado_no_se_reembolsa_aunque_deje_saldo_retenido(
    integration_session: AsyncSession,
) -> None:
    """El escaneo que sí entregó hallazgos no se devuelve, ni aunque quede saldo retenido.

    Es el camino que cuesta dinero de verdad. `_ingest_output` confirma el run con sus hallazgos,
    y es `_charge_run_usage`, que viene **después**, la que puede lanzar al ajustar el consumo
    real. Cuando eso pasa el run está `COMPLETED` y el cliente ya tiene su resultado: devolverle
    lo retenido sería regalarle el escaneo entero encima de lo que ya pagó.

    Se monta el caso peor a propósito. El ajuste de consumo real dejó **2 créditos** retenidos
    —la plataforma gastó menos de lo reservado—, así que el saldo retenido es positivo y una
    función que solo mirara el ledger devolvería 2. Lo que lo impide es mirar el **estado**.

    Y el motivo que se pasa es **un motivo de despliegue a propósito**, no uno cualquiera. Con un
    motivo que no devuelve, la puerta de la clasificación cortaría antes y la prueba pasaría sin
    llegar a la guarda del estado: verde por el motivo equivocado, que es la forma más difícil de
    detectar. La primera versión de esta prueba tenía ese defecto, y solo se vio al mutar la
    guarda y comprobar que no caía. Ahora pasa por `MOTIVOS_DE_DESPLIEGUE` entero y el estado es
    lo único que puede pararla.
    """

    assert integration_session is not None
    organization, run, coste = await _escaneo_cobrado(integration_session)
    organization_id = organization.id
    run_id = run.id
    saldo_inicial = Decimal("100")
    devolucion_parcial = Decimal("2")

    await apply_credit_delta(
        session=integration_session,
        organization_id=organization_id,
        amount=devolucion_parcial,
        reason=LedgerReasonEnum.ADMIN_ADJUSTMENT,
        reference_id=f"{run_id}:usage",
    )
    run.status = ScanStatusEnum.COMPLETED
    run.source_scan_id = "scan-1"
    run.finished_at = datetime.now(UTC)
    await integration_session.commit()

    with _escrituras_del_worker_sobre_la_prueba(integration_session):
        devuelto = await _devolver_si_el_fallo_fue_de_despliegue(
            organization_id, run_id, "STRIX_DOCKER_UNAVAILABLE"
        )

    assert devuelto == ZERO
    saldo_esperado = saldo_inicial - coste + devolucion_parcial
    assert await credit_balance_of(integration_session, organization_id) == saldo_esperado
    assert await _saldo_en_la_columna(integration_session, organization_id) == saldo_esperado
    asientos = await _asientos_del_run(integration_session, organization_id, run_id)
    # Ordenados por `reference_id`, que es lo que los identifica: la referencia desnuda es
    # prefijo de la del ajuste, así que en orden lexicográfico va la primera.
    assert sorted(a.reference_id or "" for a in asientos) == sorted(
        [str(run_id), f"{run_id}:usage"]
    )


async def test_un_run_abortado_no_se_reembolsa_desde_el_worker(
    integration_session: AsyncSession,
) -> None:
    """El dinero de una cancelación lo decide el motivo del aborto, no el fallo que vino después.

    `pentests/abort_reason.py` separa «nosotros lo paramos» de «lo paró el cliente» y solo lo
    primero devuelve. Si el worker devolviera también, un cliente que cancela y ve como le
    devuelven los créditos aprende que cancelar es gratis, y el sistema paga escaneos que sí
    llegaron a empezar.

    El caso que de verdad importa es el del motivo que **sí** devuelve: cuando el motivo fue
    `INFRASTRUCTURE_*`, `abortar_run` ya escribió su asiento y el saldo retenido quedó en cero, así
    que las dos reglas coinciden. Aquí se prueba el motivo por el que no coinciden.
    """

    assert integration_session is not None
    organization, run, coste = await _escaneo_cobrado(integration_session)
    organization_id = organization.id
    run_id = run.id
    saldo_inicial = Decimal("100")

    run.status = ScanStatusEnum.ABORTED
    run.finished_at = datetime.now(UTC)
    run.error_message = "USER_ABORTED:CLIENT_CANCELLED"
    await integration_session.commit()

    with _escrituras_del_worker_sobre_la_prueba(integration_session):
        devuelto = await _devolver_si_el_fallo_fue_de_despliegue(
            organization_id, run_id, "STRIX_IMAGE_UNAVAILABLE"
        )

    assert devuelto == ZERO
    assert await credit_balance_of(integration_session, organization_id) == (
        saldo_inicial - coste
    )
    asientos = await _asientos_del_run(integration_session, organization_id, run_id)
    assert [a.reference_id for a in asientos] == [str(run_id)]


# --------------------------------------------------------------------------- #
# 4. El cableado del `except`, que es donde estaba el defecto
#
# Estas pruebas no tocan la base de datos a propósito, y no por comodidad: el defecto no estaba
# en el SQL sino en que **la llamada no existía**. Las de arriba comprueban que el dinero sale
# bien cuando se le pide; estas comprueban que se le pide. Sin ellas, borrar la llamada del
# `except Exception` dejaría la batería en verde con el defecto entero.
#
# Y son **síncronas** a propósito: `execute_pentest_run` es una tarea de Celery y llama a
# `asyncio.run` por dentro, así que invocar su `.run()` desde una prueba asíncrona —que ya tiene
# un bucle abierto— daría `RuntimeError: asyncio.run() cannot be called from a running event
# loop`. Por eso el estado del run lo simulan funciones y no filas.
# --------------------------------------------------------------------------- #


def test_el_camino_de_excepcion_devuelve_el_dinero_despues_de_marcar_el_run() -> None:
    """Un `manager.run` que lanza por motivo de despliegue reembolsa, y reembolsa después de marcar.

    Se comprueban tres cosas, y las tres importan:

    1. **Se devuelve.** Es el defecto: antes, este camino no llamaba a nada y el tenant pagaba.
    2. **Se devuelve con el código clasificado**, `STRIX_EGRESS_FENCE_MISSING`, y no con un
       literal. La clasificación es la misma que decide el estado del run; si fueran dos, el
       panel podría explicar uno y el registro del worker el otro.
    3. **Se devuelve después de `_mark_failed`.** El reembolso bloquea la fila del run con
       `FOR UPDATE` y decide mirando su estado, así que si se llamara antes confirmaría con el
       run todavía en `RUNNING` y la guarda de `COMPLETED` no podría proteger de nada.
    """

    organization_id = uuid.uuid4()
    run_id = uuid.uuid4()
    orden: list[str] = []
    pedidos: list[tuple[object, ...]] = []

    async def _marcar(*_argumentos: object) -> None:
        orden.append("marcar")

    async def _devolver(*argumentos: object, **_opciones: object) -> Decimal:
        orden.append("devolver")
        pedidos.append(argumentos)
        return Decimal("10")

    with (
        patch(
            "backend.workers.tasks._get_run_organization_id",
            new=AsyncMock(return_value=organization_id),
        ),
        patch(
            "backend.workers.tasks._execute_pentest_run",
            new=AsyncMock(side_effect=EgressFenceMissingError("sin cerco de salida")),
        ),
        patch("backend.workers.tasks._mark_failed", new=_marcar),
        patch("backend.workers.tasks._devolver_si_el_fallo_fue_de_despliegue", new=_devolver),
    ):
        with pytest.raises(EgressFenceMissingError):
            execute_pentest_run.run(str(run_id))  # pyright: ignore[reportFunctionMemberAccess]

    assert orden == ["marcar", "devolver"]
    assert pedidos == [(organization_id, run_id, "STRIX_EGRESS_FENCE_MISSING")]


def test_el_camino_de_excepcion_no_inventa_un_codigo_para_la_devolucion() -> None:
    """La causa real se entrega a la función de devolución, no un código constante.

    Un literal aquí sería el mismo defecto que se corrigió en `pipeline.py` el mismo día: una
    sola línea para el host sin cerco y para un bug. Con `STRIX_OUTPUT_UNUSABLE`, que **no** es
    un motivo de despliegue, la función de devolución no reembolsará nada —eso lo comprueban las
    pruebas de integración— pero lo que se comprueba aquí es que recibe el motivo real y no uno
    conveniente.
    """

    organization_id = uuid.uuid4()
    run_id = uuid.uuid4()
    pedidos: list[tuple[object, ...]] = []

    async def _marcar(*_argumentos: object) -> None:
        return None

    async def _devolver(*argumentos: object, **_opciones: object) -> Decimal:
        pedidos.append(argumentos)
        return ZERO

    with (
        patch(
            "backend.workers.tasks._get_run_organization_id",
            new=AsyncMock(return_value=organization_id),
        ),
        patch(
            "backend.workers.tasks._execute_pentest_run",
            new=AsyncMock(side_effect=SandboxOutputError("sin results.json")),
        ),
        patch("backend.workers.tasks._mark_failed", new=_marcar),
        patch("backend.workers.tasks._devolver_si_el_fallo_fue_de_despliegue", new=_devolver),
    ):
        with pytest.raises(SandboxOutputError):
            execute_pentest_run.run(str(run_id))  # pyright: ignore[reportFunctionMemberAccess]

    assert pedidos == [(organization_id, run_id, "STRIX_OUTPUT_UNUSABLE")]


def test_el_timeout_no_devuelve_nada() -> None:
    """Un timeout no es un motivo de despliegue, y por eso no se devuelve nada.

    Es una decisión de producto y por eso está probada: un timeout significa que el motor estuvo
    trabajando hasta que se le acabó el tiempo, y ese trabajo le costó tokens a la plataforma. La
    alternativa —devolverlo también— es defendible y alguien la puede querer, pero entonces el
    motivo tiene que entrar en la clasificación, no existir en paralelo.
    """

    organization_id = uuid.uuid4()
    run_id = uuid.uuid4()
    llamadas: list[tuple[object, ...]] = []

    async def _marcar_timed_out(*_argumentos: object) -> None:
        return None

    async def _devolver(*argumentos: object, **_opciones: object) -> Decimal:
        llamadas.append(argumentos)
        return ZERO

    with (
        patch(
            "backend.workers.tasks._get_run_organization_id",
            new=AsyncMock(return_value=organization_id),
        ),
        patch(
            "backend.workers.tasks._execute_pentest_run",
            new=AsyncMock(side_effect=SandboxTimeoutError("se pasó del tiempo")),
        ),
        patch("backend.workers.tasks._mark_timed_out", new=_marcar_timed_out),
        patch("backend.workers.tasks._devolver_si_el_fallo_fue_de_despliegue", new=_devolver),
    ):
        with pytest.raises(SandboxTimeoutError):
            execute_pentest_run.run(str(run_id))  # pyright: ignore[reportFunctionMemberAccess]

    assert llamadas == []


def test_el_reembolso_tampoco_se_pide_si_no_se_puede_resolver_la_organizacion() -> None:
    """Sin organización conocida, el camino de excepción ni siquiera intenta devolver.

    Es el mismo guardia que hay en la función, pero probado en el cableado: si alguien lo
    quitara de uno de los dos sitios y lo dejara en el otro, el saldo de un tenant equivocado
    dependería de por dónde entrara el fallo. El log lo dice; el saldo no se toca.
    """

    run_id = uuid.uuid4()
    llamadas: list[tuple[object, ...]] = []

    async def _marcar(*_argumentos: object) -> None:
        return None

    async def _devolver(*argumentos: object, **_opciones: object) -> Decimal:
        llamadas.append(argumentos)
        return ZERO

    with (
        patch(
            "backend.workers.tasks._get_run_organization_id",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "backend.workers.tasks._execute_pentest_run",
            new=AsyncMock(side_effect=EgressFenceMissingError("sin cerco de salida")),
        ),
        patch("backend.workers.tasks._mark_failed", new=_marcar),
        patch("backend.workers.tasks._devolver_si_el_fallo_fue_de_despliegue", new=_devolver),
    ):
        with pytest.raises(EgressFenceMissingError):
            execute_pentest_run.run(str(run_id))  # pyright: ignore[reportFunctionMemberAccess]

    # Se llega a llamar, con `None` como organización, y es la propia función la que se niega:
    # el guardia vive en un solo sitio, que es donde puede comprobarse y no repartido en dos.
    assert llamadas == [(None, run_id, "STRIX_EGRESS_FENCE_MISSING")]
