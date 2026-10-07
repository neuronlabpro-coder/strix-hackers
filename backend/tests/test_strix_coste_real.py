"""El coste real del proveedor, el del catálogo y la divergencia entre los dos.

## El problema que estas pruebas vigilan

El motor publica en `run.json` lo que el proveedor **cobró de verdad**. La plataforma cobra según
`LLMModelConfig`, que es una **estimación**. Son dos números y no son lo mismo, y hasta ahora solo
se guardaba el segundo: el primero se leía, se usaba para nada y se perdía con el workspace (R5).

Medido sobre el run real de este proyecto, `mindguard-site_23ee`, contra el catálogo de
`z-ai/glm-5.3` que hay en la base:

| Magnitud                          | Valor          |
| :-------------------------------- | :------------- |
| Tokens de entrada declarados      | 16.819.076     |
| Tokens de salida declarados       | 46.417         |
| `run.json.llm_usage.cost`         | **1,85 USD**   |
| Coste base según el catálogo      | **6,80 USD**   |
| Precio al cliente (recargo 200 %) | **20,41 USD**  |

## Lo que NO se arregla aquí

No se cambia lo que se cobra. El importe está sellado en el `credit_ledger`, que es *append-only*
(R4), y la política de precios es una decisión comercial. Lo que este módulo hace es **mostrar los
dos y marcar la divergencia**, que es lo que permite decidir con los números delante en vez de a
ciegas. Las pruebas de este fichero no cambian ningún precio, y lo comprueban: el saldo del tenant
tras el replay y tras un escaneo tarificado es el que la aritmética del ledger dice.
"""

from __future__ import annotations

import shutil
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.models import CreditLedger, LedgerReasonEnum
from backend.apps.billing.service import apply_credit_delta
from backend.apps.organizations.models import Organization
from backend.apps.pentests.models import PentestRun, ScanModeEnum, ScanStatusEnum, TargetTypeEnum
from backend.apps.pentests.schemas import ScanCost
from backend.workers.runner.strix_artefactos import leer_ejecucion
from backend.workers.tasks import _persistir_artefactos_del_motor

pytestmark = pytest.mark.integration

ORIGEN_REAL = Path(__file__).parent / "fixtures" / "strix_run_completado_real"


async def crear_tenant(session: AsyncSession) -> Organization:
    organization = Organization(
        name=f"Coste {uuid.uuid4().hex}",
        slug=f"coste-{uuid.uuid4().hex}",
        credit_balance=Decimal("1000"),
    )
    session.add(organization)
    await session.flush()
    await session.commit()
    return organization


async def crear_run(session: AsyncSession, organization: Organization) -> PentestRun:
    run = PentestRun(
        organization_id=organization.id,
        target_type=TargetTypeEnum.DOMAIN,
        target_identifier="mindguard.site",
        scan_mode=ScanModeEnum.QUICK,
        status=ScanStatusEnum.RUNNING,
    )
    session.add(run)
    await session.flush()
    await session.commit()
    return run


def ejecucion_real(tmp_path: Path):
    """Los cuatro artefactos del run real, en un workspace con la forma que espera el parser."""

    destino = tmp_path / "strix_runs" / "mindguard-site_23ee"
    destino.mkdir(parents=True, exist_ok=True)
    for nombre in ("run.json", "findings.sarif", "coverage.json", "penetration_test_report.md"):
        shutil.copyfile(ORIGEN_REAL / nombre, destino / nombre)
    return leer_ejecucion(tmp_path)


# --------------------------------------------------------------------------- #
# El número del proveedor se guarda
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_el_coste_real_del_proveedor_queda_guardado_en_el_run(
    integration_session: AsyncSession, tmp_path: Path
) -> None:
    """El importe que el proveedor cobró se persiste, con sus tokens.

    ## Por qué esto importa más de lo que parece

    Porque ese número **solo** existe en `run.json`, y el workspace se purga al terminar el
    escaneo (R5). Sin esta columna, el coste real de cada escaneo de este proyecto se pierde para
    siempre y la pregunta «¿cuánto nos cuesta de verdad un pentest?» no tiene respuesta. Y esa
    pregunta es la que decide si el catálogo de precios está bien.
    """

    assert integration_session is not None
    organization = await crear_tenant(integration_session)
    run = await crear_run(integration_session, organization)

    ejecucion = ejecucion_real(tmp_path)
    assert ejecucion.coste == Decimal("1.84830111")
    assert ejecucion.total_tokens == 16_865_493

    await _persistir_artefactos_del_motor(
        integration_session,
        run,
        ejecucion,
        exit_code="0",
        referencia="replay:prueba",
    )
    await integration_session.refresh(run)

    assert run.provider_cost_usd == Decimal("1.84830111")
    assert run.provider_tokens == 16_865_493


@pytest.mark.asyncio
async def test_un_run_sin_consumo_no_inventa_un_cero(
    integration_session: AsyncSession, tmp_path: Path
) -> None:
    """`NULL` es «no lo sé» y `0` es «no costó nada»: la misma diferencia que con la cobertura.

    Un run cuyo artefacto no trae `llm_usage.cost` tiene que quedar con la columna a `NULL`. Poner
    `0` affirmaría que el proveedor no cobró nada, que es una afirmación sobre el dinero que nadie
    ha hecho.
    """

    assert integration_session is not None
    organization = await crear_tenant(integration_session)
    run = await crear_run(integration_session, organization)

    destino = tmp_path / "strix_runs" / "mindguard-site_23ee"
    destino.mkdir(parents=True)
    shutil.copyfile(ORIGEN_REAL / "findings.sarif", destino / "findings.sarif")
    (destino / "run.json").write_text(
        '{"status":"completed","run_id":"sin-consumo","scan_mode":"quick"}', encoding="utf-8"
    )
    ejecucion = leer_ejecucion(tmp_path)
    assert ejecucion.coste is None

    await _persistir_artefactos_del_motor(
        integration_session,
        run,
        ejecucion,
        exit_code="0",
        referencia="replay:prueba",
    )
    await integration_session.refresh(run)

    assert run.provider_cost_usd is None
    assert run.provider_tokens is None


# --------------------------------------------------------------------------- #
# La divergencia se calcula y se ve
# --------------------------------------------------------------------------- #


def _fila(provider: Decimal | None, catalogo: Decimal | None, tokens: int | None = None):
    """Un `PentestRun` **sin guardar** con los tres números puestos.

    ## Por qué un objeto real y no un doble de tres atributos

    Porque `ScanCost.desde_run` recibe la fila y lee tres columnas; un doble con tres atributos
    comprobaría que la función lee «lo que hay», no que lee «las columnas que existen». Con un
    `PentestRun` de verdad, si alguien renombra una columna, esta prueba deja de compilar en vez de
    pasar en verde leyendo un atributo que ya no está.

    Y no se guarda en la base: no hay sesión, no hay `flush` y no hay fila. La aritmética de la
    comparación no depende de PostgreSQL —lo que sí depende, que las columnas se lean bien al
    venir de la base, lo comprueban las pruebas de arriba—.
    """

    return PentestRun(
        organization_id=uuid.uuid4(),
        target_type=TargetTypeEnum.DOMAIN,
        target_identifier="mindguard.site",
        scan_mode=ScanModeEnum.QUICK,
        status=ScanStatusEnum.COMPLETED,
        provider_cost_usd=provider,
        catalogue_cost_usd=catalogo,
        provider_tokens=tokens,
    )


def test_la_divergencia_del_run_real_se_detecta() -> None:
    """Con los números del run real, la divergencia es **de un factor cuatro**.

    Este es el caso que motivationó el módulo, y el que hay que mirar antes de decidir nada sobre
    precios.
    """

    coste = ScanCost.desde_run(_fila(Decimal("1.84830111"), Decimal("6.80189760"), 16_865_493))

    assert coste.provider_usd == Decimal("1.84830111")
    assert coste.catalogue_usd == Decimal("6.80189760")
    assert coste.delta_usd == Decimal("4.95359649")
    assert coste.diverges is True


def test_un_catalogo_que_coincide_no_divergencia() -> None:
    """El caso normal: los dos números dicen lo mismo y no hay nada que avisar.

    ## Por qué importa este caso y no solo el de la divergencia

    Porque una comprobación que solo sabe decir «divergencia» es inservible: acabaría marcando
    todos los escaneos y nadie la leería. El umbral tiene que ser lo bastante **estrecho** para no
    molestar con el redondeo de microdólares y lo bastante **ancho** para no tapar el factor cuatro.
    """

    coste = ScanCost.desde_run(_fila(Decimal("1.84830111"), Decimal("1.84830111")))

    assert coste.delta_usd == Decimal("0")
    assert coste.diverges is False


def test_el_redondeo_de_microdolar_no_cuenta_como_divergencia() -> None:
    """Una diferencia de fracciones de céntimo es redondeo, no discrepancia.

    `compute_charge` redondea a `0.000001`, así que dos cifras que son la misma pueden diferir en
    la última cifra. Con un margen de un céntimo fijo, un escaneo de 0,05 USD aparecería como
    divergente siempre.
    """

    coste = ScanCost.desde_run(_fila(Decimal("1.848301"), Decimal("1.84830111")))

    assert coste.diverges is False


def test_el_catalogo_por_debajo_tambien_divergencia() -> None:
    """La comprobación es de **divergencia**, no solo de sobreprecio.

    ## Por qué se afirma el signo contrario

    Porque un día el proveedor puede bajar un precio y el catálogo quedarse alto, o al revés. Una
    comprobación que solo mirara `delta > 0` dejaría pasar el otro caso, que es el que más daña:
    un catálogo que **subestima** significa que la plataforma vende por debajo de lo que le
    cuesta.
    """

    coste = ScanCost.desde_run(_fila(Decimal("6.80189760"), Decimal("1.84830111")))

    assert coste.delta_usd == Decimal("-4.95359649")
    assert coste.diverges is True


def test_sin_los_dos_numeros_no_hay_divergencia_que_afirmar() -> None:
    """Falta uno de los dos: `diverges` es `None`, no `False`.

    ## Por qué `None` y no `False`

    Porque `False` afirmaría «el catálogo coincide con el proveedor», y eso es una afirmación sobre
    el dinero hecha sin dato. `None` es «no lo sé», que es lo que corresponde, y es la misma
    distinción que usa `coverage === null` para no pintar una tarjeta de ceros.
    """

    assert ScanCost.desde_run(_fila(Decimal("1.85"), None)).diverges is None
    assert ScanCost.desde_run(_fila(None, Decimal("6.80"))).diverges is None
    assert ScanCost.desde_run(_fila(None, None)).diverges is None


# --------------------------------------------------------------------------- #
# El precio cobrado no se toca
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_medir_la_divergencia_no_mueve_el_saldo(
    integration_session: AsyncSession, tmp_path: Path
) -> None:
    """Persistir y mostrar los dos importes deja el ledger exactamente como estaba.

    ## Por qué esta prueba es la que protege el dinero

    Porque la tentación natural al ver una divergencia de factor cuatro es «ajustar el cobro». Y
    aquí se afirma que el módulo no lo hace: el asiento sellado no se toca (R4) y el saldo del
    tenant no se mueve. El ajuste de precios es una decisión del humano, con los dos números
    delante; no un efecto secundario de hacerlos visibles.
    """

    assert integration_session is not None
    organization = await crear_tenant(integration_session)
    run = await crear_run(integration_session, organization)

    await apply_credit_delta(
        session=integration_session,
        organization_id=organization.id,
        amount=Decimal("-10"),
        reason=LedgerReasonEnum.SCAN_CONSUMPTION,
        reference_id=str(run.id),
    )
    await integration_session.commit()
    saldo_antes = Decimal(organization.credit_balance)
    antes = (
        await integration_session.execute(
            select(CreditLedger.id, CreditLedger.amount_delta).where(
                CreditLedger.organization_id == organization.id
            )
        )
    ).all()

    ejecucion = ejecucion_real(tmp_path)
    await _persistir_artefactos_del_motor(
        integration_session,
        run,
        ejecucion,
        exit_code="0",
        referencia="replay:prueba",
    )

    despues = (
        await integration_session.execute(
            select(CreditLedger.id, CreditLedger.amount_delta).where(
                CreditLedger.organization_id == organization.id
            )
        )
    ).all()
    await integration_session.refresh(organization)

    assert despues == antes
    assert Decimal(organization.credit_balance) == saldo_antes
