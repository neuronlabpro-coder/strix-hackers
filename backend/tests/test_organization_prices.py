"""Pruebas de los precios pactados con cada organización.

## Qué se comprueba y por qué

Que un precio negociado se aplique **encima** del de plataforma, y sobre todo que se pueda
**cambiar** sin romper las tres cosas que el modelo promete: nada se borra, nada se reescribe, y lo
que se cobra hoy es lo que el comercial cree que se cobra.

Las propiedades, en lugar de ejemplos sueltos:

1. **Sin pactado, el precio es el de plataforma.** Es el caso del cliente nuevo, y es el que
   garantiza que la resolución no está inventándose un precio.
2. **Con pactado, el pactado manda solo en su operación.** Un precio de pack no toca el de escaneo,
   porque son dos filas distintas con dos alcances distintos.
3. **Un pactado nuevo sustituye a otro sin escribir sobre el anterior.** Aquí está el corazón: la
   tabla es append-only, así que sustituir no es cerrar. Las dos filas se quedan y la segunda gana.
4. **Un pactado con fecha futura no aplica hasta su fecha**, y mientras no llega, el anterior sigue
   mandando. Esta es la prueba que importa más, porque el fallo que cubre es silencioso: el
   cliente empezaba a pagar un precio que nadie le había cobrado todavía, y sin que ninguna fila se
   hubiera borrado.
5. **Un pactado caducado deja de aplicar, y el anterior suyo vuelve a mandar.** La cadena se recorre
   hacia abajo, no se queda sin nada.
6. **`_ids_vigentes` marca los tres estados sin ambigüedad.** Vigente, futuro y caducado tienen que
   ser distinguibles en la lista del panel, y con un solo `bool` de «caducado» no lo son.
7. **La consola no borra ni reescribe.** Tras dos pactados hay dos filas, y la traza dice qué valor
   salió de juego.
8. **`organization_id=None` devuelve el precio de plataforma.** Los llamadores sin sesión —el worker
   de diagnóstico, un cálculo suelto— tienen que poder preguntar.

## Por qué hay una prueba con una cadena de cuatro pactados

Porque los casos se pueden tapar entre sí. Con un solo pactado, «el más reciente gana» y «el
primero gana» dan la misma respuesta, y un error en la resolución no se ve. La cadena de cuatro
—pasado, vigente, futuro, y otro pasado— es la mínima que separa las cuatro reglas, y cada una
tiene su propia prueba.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.models import (
    OrganizationPriceOverride,
    PlatformPriceChange,
    PriceOperationEnum,
)
from backend.apps.billing.organization_prices import (
    cargar_overrides,
    fijar_overrides,
    overrides_de,
    precios_de,
    un_pactado,
)
from backend.apps.billing.organization_prices_router import (
    PactadoResponse,
    _ids_vigentes,
)
from backend.apps.billing.pricing import PlatformPrices, fijar_precios, precios_vigentes
from backend.apps.organizations.models import (
    Membership,
    Organization,
    RoleEnum,
    User,
)
from backend.core.security import create_access_token, hash_password
from backend.main import app

pytestmark = pytest.mark.integration

AHORA = datetime.now(UTC)


def _dias(n: float) -> datetime:
    return AHORA + timedelta(days=n)


def _pactado(valor: str, desde: datetime, hasta: datetime | None = None):
    """Un pactado como lo guarda la caché, sin tocar la base.

    ## Por qué se construye sin sesión

    Porque `overrides_de` y `precios_de` leen la instantánea del proceso, no la base: probarlas
    contra la base probaría además el SQL, y el SQL ya lo cubren las pruebas de la consola. Lo
    que se quiere fijar aquí es la regla, y la regla es una función de la fecha.

    ## Por qué se usa la fábrica `un_pactado` y no el dataclass privado

    Porque la forma de la caché va a cambiar —ya cambió una vez, de un valor a una cadena— y
    una prueba atada al dataclass habría que reescribirse entera en ese momento, en lugar de
    fallar y decir qué cambió.
    """

    return un_pactado(Decimal(valor), desde, hasta)


def _fijar(organizacion: uuid.UUID, *pactados) -> None:
    """Fija la caché de una organización con los pactados dados, en el orden dado.

    ## Por qué el orden va explícito y no se deduce

    Porque el orden de la caché **es** parte de lo que hay que probar: si la resolución se
    descolgara de él, cobraría el precio equivocado. Dejarlo a la vista en la llamada es justo
    lo que un test debe hacer.
    """

    fijar_overrides(
        {organizacion: {PriceOperationEnum.SCAN_CREDIT_COST.value: pactados}}
    )


@pytest.fixture(autouse=True)
def instantaneas_negras() -> Iterator[None]:
    """Deja las instantáneas como estaban.

    Sin esto, una prueba que fije un precio deja el proceso entero cobrando a ese número durante el
    resto de la sesión, y el fallo aparece en otra prueba con un nombre que no habla de precios.
    """

    try:
        yield
    finally:
        fijar_precios(None)
        fijar_overrides(None)


@pytest.fixture
def precios_de_prueba() -> PlatformPrices:
    """Un precio de plataforma reconocible, con los tres escalares de cobro en cifras distintas.

    ## Por qué cifras distintas y no todas a 10

    Porque si las tres fueran iguales, un error que aplicase el override al campo equivocado
    pasaría el test. Con 10, 3 y 0,5 se sabe de un vistazo qué campo se ha tocado.
    """

    precios = PlatformPrices(
        credits_per_usd=Decimal("100"),
        scan_credit_cost=Decimal("10"),
        quick_scan_credit_multiplier=Decimal("0.5"),
        low_credit_balance_threshold=Decimal("20"),
    )
    fijar_precios(precios)
    return precios


# --------------------------------------------------------------------------- #
# La base: sin pactado, el precio es el de plataforma
# --------------------------------------------------------------------------- #


async def test_sin_pactado_se_cobra_el_precio_de_plataforma(
    precios_de_prueba: PlatformPrices,
) -> None:
    fijar_overrides(None)

    assert precios_de(None).scan_credit_cost == Decimal("10")
    assert precios_de(uuid.uuid4()).scan_credit_cost == Decimal("10"), (
        "una organización cualquiera no tiene por qué tener un precio distinto"
    )
    assert precios_vigentes().scan_credit_cost == Decimal("10"), (
        "fijar precios de prueba no puede alterar la fila de plataforma"
    )


async def test_una_organizacion_sin_pactados_no_tiene_nada_en_la_cache(
    integration_session: AsyncSession,
    precios_de_prueba: PlatformPrices,
) -> None:
    await cargar_overrides(integration_session)

    assert overrides_de(uuid.uuid4()) == {}


# --------------------------------------------------------------------------- #
# El pactado manda, pero solo en su operación
# --------------------------------------------------------------------------- #


async def test_el_pactado_sustituye_al_precio_de_plataforma(
    precios_de_prueba: PlatformPrices,
) -> None:
    organizacion = uuid.uuid4()
    _fijar(organizacion, _pactado("3", _dias(-1)))

    precios = precios_de(organizacion)

    assert precios.scan_credit_cost == Decimal("3")
    assert precios.credits_per_usd == Decimal("100"), "no pactado: el de plataforma"
    assert precios.quick_scan_credit_multiplier == Decimal("0.5"), "no pactado: el de plataforma"


async def test_un_pactado_de_pack_no_toca_el_precio_de_escaneo(
    precios_de_prueba: PlatformPrices,
) -> None:
    """`CREDIT_PACK_AMOUNT` y `SCAN_CREDIT_COST` son filas distintas, no dos precios de lo mismo.

    Es el caso que hace necesaria la clave con `alcance`: sin ella, el precio del pack pisaría el
    del escaneo en el mismo diccionario.
    """

    organizacion = uuid.uuid4()
    fijar_overrides(
        {
            organizacion: {
                f"{PriceOperationEnum.CREDIT_PACK_AMOUNT.value}:250": (
                    un_pactado(Decimal("199"), _dias(-1)),
                )
            }
        }
    )

    assert precios_de(organizacion).scan_credit_cost == Decimal("10"), (
        "el pack no puede cambiar el escaneo"
    )
    assert len(overrides_de(organizacion)) == 1


# --------------------------------------------------------------------------- #
# El corazón: sustituir sin escribir sobre el anterior
# --------------------------------------------------------------------------- #


async def test_un_pactado_nuevo_sustituye_al_anterior_sin_tocar_el_anterior(
    precios_de_prueba: PlatformPrices,
) -> None:
    organizacion = uuid.uuid4()
    primero = _pactado("3", _dias(-10))
    segundo = _pactado("4", _dias(-1))
    _fijar(organizacion, segundo, primero)

    assert precios_de(organizacion).scan_credit_cost == Decimal("4"), "gana el más reciente"
    assert len(overrides_de(organizacion)) == 1, "solo uno está vigente a la vez"
    assert primero.valor == Decimal("3"), "el anterior no se ha reescrito"


async def test_un_pactado_antiguo_no_pisa_a_uno_mas_reciente(
    precios_de_prueba: PlatformPrices,
) -> None:
    """El orden de la caché no es un detalle: se entrega del más reciente al más antiguo.

    Si la resolución se descolgara del orden —porque el que se inserta al final gana, que es
    como reventaba antes— este test falla, y es el fallo que cobraba el precio equivocado sin que
    nada más lo delatara.
    """

    organizacion = uuid.uuid4()
    _fijar(organizacion, _pactado("9", _dias(-1)), _pactado("1", _dias(-30)))

    assert precios_de(organizacion).scan_credit_cost == Decimal("9"), (
        "el más reciente tiene que ganar aunque el antiguo se escriba después"
    )


# --------------------------------------------------------------------------- #
# Un pactado con fecha futura no aplica hasta su fecha
# --------------------------------------------------------------------------- #


async def test_un_pactado_futuro_no_aplica_y_no_tapa_al_vigente(
    precios_de_prueba: PlatformPrices,
) -> None:
    """El caso que se rompió en el servidor y que no daba ningún error.

    Con la regla de «gana el más reciente al cargar», el pactado de mañana ganaba la
    selección, no pasaba el filtro de fecha al cobrar, y no quedaba ninguno: el cliente volvía al
    precio de plataforma sin haberlo pedido. Aquí el de hoy tiene que seguir mandando.
    """

    organizacion = uuid.uuid4()
    _fijar(organizacion, _pactado("8", _dias(30)), _pactado("5", _dias(-1)))

    assert precios_de(organizacion).scan_credit_cost == Decimal("5"), (
        "el precio de mañana no puede ser el de hoy"
    )


async def test_cuando_llega_la_fecha_manda_el_futuro(
    precios_de_prueba: PlatformPrices,
) -> None:
    """El mismo caso, un mes después.

    Sin esta mitad, un arreglo que hiciera «ignorar los pactados futuros para siempre» pasaría el
    test anterior y dejaría al cliente pagando el viejo para siempre, que es un fallo silencioso
    más caro que el que arregla.
    """

    organizacion = uuid.uuid4()
    _fijar(organizacion, _pactado("8", _dias(-1)), _pactado("5", _dias(-31)))

    assert precios_de(organizacion).scan_credit_cost == Decimal("8")


async def test_un_pactado_caducado_deja_de_aplicar(
    precios_de_prueba: PlatformPrices,
) -> None:
    organizacion = uuid.uuid4()
    _fijar(organizacion, _pactado("2", _dias(-30), _dias(-1)))

    assert precios_de(organizacion).scan_credit_cost == Decimal("10"), (
        "un acuerdo caducado devuelve al cliente al precio de plataforma"
    )


async def test_caducado_el_mas_reciente_vuelve_a_mandar_el_anterior(
    precios_de_prueba: PlatformPrices,
) -> None:
    """La cadena se recorre hacia abajo, y no se queda sin precio.

    Es lo que distingue «caduca el override» de «caduca el precio pactado»: si la resolución
    se quedara solo con el más reciente y lo descartara, el cliente caeria a plataforma en vez de
    volver al acuerdo anterior.
    """

    organizacion = uuid.uuid4()
    _fijar(organizacion, _pactado("6", _dias(-10), _dias(-1)), _pactado("4", _dias(-30)))

    assert precios_de(organizacion).scan_credit_cost == Decimal("4")


async def test_si_todos_estan_caducados_vuelve_a_plataforma(
    precios_de_prueba: PlatformPrices,
) -> None:
    organizacion = uuid.uuid4()
    _fijar(
        organizacion,
        _pactado("6", _dias(-19), _dias(-1)),
        _pactado("4", _dias(-30), _dias(-20)),
    )

    assert precios_de(organizacion).scan_credit_cost == Decimal("10")


async def test_el_mismo_pactado_no_cobra_dos_veces(
    precios_de_prueba: PlatformPrices,
) -> None:
    """Dos filas con el mismo valor no son dos precios: el cobro no puede multiplicar.

    Es el caso que aparece al reintentar un pactado fallido, y es donde un `+=` en vez de un
    `replace` se notaría.
    """

    organizacion = uuid.uuid4()
    _fijar(organizacion, _pactado("5", _dias(-1)), _pactado("5", _dias(-2)))

    assert precios_de(organizacion).scan_credit_cost == Decimal("5")


# --------------------------------------------------------------------------- #
# El cargador, no solo la resolucion
# --------------------------------------------------------------------------- #


async def _fila_en_base(
    session: AsyncSession,
    organizacion: uuid.UUID,
    operacion: PriceOperationEnum,
    valor: str,
    desde: datetime,
    alcance: str | None = None,
    hasta: datetime | None = None,
) -> OrganizationPriceOverride:
    """Una fila de verdad, escrita por la vía que usa la aplicación.

    ## Por qué `valido_hasta` va en el `INSERT` y no se asigna después

    Porque `organization_price_overrides` es append-only: asignar `valido_hasta` sobre una fila
    guardada es un `UPDATE` que el disparador de la base rechaza. Se comprobó intentando y el
    error fue la respuesta correcta. Un pactado con fecha de fin tiene que **nacerse** con ella, que
    es exactamente lo que hace la consola.
    """

    fila = OrganizationPriceOverride(
        organization_id=organizacion,
        operacion=operacion,
        alcance=alcance,
        valor=Decimal(valor),
        motivo="prueba de carga",
        valido_desde=desde,
        valido_hasta=hasta,
        created_by=None,
    )
    session.add(fila)
    await session.commit()
    return fila


async def test_la_carga_no_tapa_un_pactado_vigente_con_uno_futuro(
    integration_session: AsyncSession,
    precios_de_prueba: PlatformPrices,
) -> None:
    """El cargador tiene que guardar la **cadena**, no solo el ganador.

    Es la prueba que faltaba, y la que habría parado el defecto que se llegó al servidor: si
    `cargar_overrides` se queda con la fila más reciente de cada operación, un pactado con
    fecha de mañana ocupa el sitio del de hoy, no pasa el filtro al cobrar, y el cliente se queda
    sin pactado y paga el de plataforma. Ninguna otra prueba lo ve, porque las demás Falsean la
    caché a mano y se saltan el cargador entero.
    """

    organizacion = (await _organizacion(integration_session)).id
    await _fila_en_base(
        integration_session, organizacion, PriceOperationEnum.SCAN_CREDIT_COST, "5", _dias(-1)
    )
    await _fila_en_base(
        integration_session, organizacion, PriceOperationEnum.SCAN_CREDIT_COST, "8", _dias(30)
    )

    await cargar_overrides(integration_session)

    assert precios_de(organizacion).scan_credit_cost == Decimal("5"), (
        "el cargador no puede decidir el vigente: esa decisión es del reloj, y el reloj avanza"
    )


async def test_la_carga_conserva_los_pactos_de_organizaciones_distintas(
    integration_session: AsyncSession,
    precios_de_prueba: PlatformPrices,
) -> None:
    """Un cliente con precio pactado no puede arrastrar al vecino.

    Es R3 aplicado a la caché: el diccionario está en el proceso, no en la base, así que nada
    impide por accidente que la entrada de un tenant se mezcle con la de otro.
    """

    uno = (await _organizacion(integration_session)).id
    otro = (await _organizacion(integration_session)).id
    await _fila_en_base(
        integration_session, uno, PriceOperationEnum.SCAN_CREDIT_COST, "3", _dias(-1)
    )
    await _fila_en_base(
        integration_session, otro, PriceOperationEnum.SCAN_CREDIT_COST, "7", _dias(-1)
    )

    await cargar_overrides(integration_session)

    assert precios_de(uno).scan_credit_cost == Decimal("3")
    assert precios_de(otro).scan_credit_cost == Decimal("7")


async def test_la_carga_conserva_el_precio_de_cada_pack(
    integration_session: AsyncSession,
    precios_de_prueba: PlatformPrices,
) -> None:
    """Dos packs negociados a distinto precio tienen que seguir siendo dos precios.

    ## Por qué esta prueba y no la del escaneo

    Porque `CREDIT_PACK_AMOUNT` no tiene campo en `PlatformPrices`, así que un precio de pack no
    se ve en `scan_credit_cost` ni por accidente: la resolución lo ignora. Lo que la clave con
    `alcance` protege de verdad es que los dos packs no se pisen **en la caché**, que es donde se
    decide luego cuál se compra. Comprobarlo sobre el escaneo daría verde con la clave rota,
    porque el campo que se comprueba no es el que se habría roto.
    """

    organizacion = (await _organizacion(integration_session)).id
    await _fila_en_base(
        integration_session,
        organizacion,
        PriceOperationEnum.CREDIT_PACK_AMOUNT,
        "199",
        _dias(-1),
        alcance="250",
    )
    await _fila_en_base(
        integration_session,
        organizacion,
        PriceOperationEnum.CREDIT_PACK_AMOUNT,
        "449",
        _dias(-1),
        alcance="1000",
    )

    await cargar_overrides(integration_session)

    pactados = overrides_de(organizacion)
    assert len(pactados) == 2, "los dos packs tienen su precio"
    assert pactados[f"{PriceOperationEnum.CREDIT_PACK_AMOUNT.value}:250"].valor == Decimal("199")
    assert pactados[f"{PriceOperationEnum.CREDIT_PACK_AMOUNT.value}:1000"].valor == Decimal("449")


async def test_la_carga_ignora_un_pacto_caducado(
    integration_session: AsyncSession,
    precios_de_prueba: PlatformPrices,
) -> None:
    """Lo caducado no entra en la caché, para que no ocupe sitio ni confunda el diagnóstico."""

    organizacion = (await _organizacion(integration_session)).id
    await _fila_en_base(
        integration_session,
        organizacion,
        PriceOperationEnum.SCAN_CREDIT_COST,
        "2",
        _dias(-30),
        hasta=_dias(-1),
    )

    await cargar_overrides(integration_session)

    assert overrides_de(organizacion) == {}
    assert precios_de(organizacion).scan_credit_cost == Decimal("10")


# --------------------------------------------------------------------------- #
# Lo que ve el panel: los tres estados tienen que ser distinguibles
# --------------------------------------------------------------------------- #


def _fila(
    operacion: PriceOperationEnum,
    valor: str,
    desde: datetime,
    hasta: datetime | None = None,
):
    """Una fila de la base, para lo que consume el modelo y no la caché.

    ## Por qué estas pruebas usan filas y las de resolución usan `un_pactado`

    Porque son dos consumidores distintos. `_ids_vigentes` recibe lo que devuelve la consulta, que
    son filas; `overrides_de` lee la caché, que son `_PrecioPactado`. Probarlos a cada uno con
    la forma que realmente recibe es lo que hace que un fallo sea atribuible al sitio correcto.
    """

    return OrganizationPriceOverride(
        id=uuid.uuid4(),
        organization_id=uuid.UUID(int=0),
        operacion=operacion,
        alcance=None,
        valor=Decimal(valor),
        motivo="prueba",
        valido_desde=desde,
        valido_hasta=hasta,
        created_by=None,
        created_at=desde,
    )


async def test_los_tres_estados_se_marcan_sin_ambiguedad() -> None:
    vigente = _fila(PriceOperationEnum.SCAN_CREDIT_COST, "5", _dias(-1))
    futuro = _fila(PriceOperationEnum.SCAN_CREDIT_COST, "8", _dias(30))
    caducado = _fila(PriceOperationEnum.SCAN_CREDIT_COST, "3", _dias(-30), _dias(-20))

    marcados = _ids_vigentes([futuro, vigente, caducado])

    assert marcados == {vigente.id}
    assert futuro.id not in marcados, "el futuro no es vigente ni caducado: aún no ha empezado"
    assert caducado.id not in marcados, "el caducado se distingue del que nunca existió"


async def test_dos_organizaciones_se_marcan_por_separado() -> None:
    """La marca es por fila, no un `bool` global: dos clientes pueden estar en estados distintos."""

    a = _fila(PriceOperationEnum.SCAN_CREDIT_COST, "5", _dias(-1))
    b = _fila(PriceOperationEnum.SCAN_CREDIT_COST, "5", _dias(30))
    a.organization_id = uuid.UUID(int=1)
    b.organization_id = uuid.UUID(int=2)

    assert _ids_vigentes([a, b]) == {a.id}


async def test_dos_operaciones_distintas_pueden_ser_vigentes_a_la_vez() -> None:
    escaneo = _fila(PriceOperationEnum.SCAN_CREDIT_COST, "5", _dias(-1))
    pack = _fila(PriceOperationEnum.CREDIT_PACK_AMOUNT, "199", _dias(-2))
    pack.alcance = "250"

    assert _ids_vigentes([escaneo, pack]) == {escaneo.id, pack.id}, (
        "son operaciones distintas: no compiten"
    )


async def test_una_organizacion_sin_ningun_pactado_no_tiene_vigentes() -> None:
    assert _ids_vigentes([]) == set()


# --------------------------------------------------------------------------- #
# La consola de pactar
# --------------------------------------------------------------------------- #


async def _superusuario(
    integration_session: AsyncSession, organizacion: Organization
) -> dict[str, str]:
    sufijo = uuid.uuid4().hex
    usuario = User(
        email=f"super-{sufijo}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Super Admin",
        email_verified=True,
        is_superuser=True,
    )
    integration_session.add(usuario)
    await integration_session.flush()
    integration_session.add(
        Membership(organization_id=organizacion.id, user_id=usuario.id, role=RoleEnum.ADMIN)
    )
    await integration_session.commit()
    return {"Authorization": f"Bearer {create_access_token({'sub': str(usuario.id)})}"}


async def _organizacion(integration_session: AsyncSession) -> Organization:
    sufijo = uuid.uuid4().hex
    organizacion = Organization(name=f"Pactada {sufijo}", slug=f"pactada-{sufijo}")
    integration_session.add(organizacion)
    await integration_session.commit()
    return organizacion


def _cliente() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


async def test_pactar_dos_veces_no_borra_ni_reescribe(
    integration_session: AsyncSession,
) -> None:
    """Dos pactados dejan dos filas, y la traza dice qué valor salió de juego.

    Es la propiedad que hace que «cambiar un precio» sea posible sin romper la inmutabilidad: si
    esto fallara, o bien se estaría borrando el acuerdo anterior, o bien el precio nuevo no podría
    aplicarse.
    """

    organizacion = await _organizacion(integration_session)
    cabeceras = await _superusuario(integration_session, organizacion)

    async with _cliente() as cliente:
        primero = await cliente.post(
            f"/api/v1/admin/organizations/{organizacion.id}/price-overrides",
            headers=cabeceras,
            json={
                "operacion": "SCAN_CREDIT_COST",
                "valor": "3",
                "motivo": "primera cotizacion",
            },
        )
        assert primero.status_code == 201, primero.text
        assert primero.json()["sustituye_id"] is None, "el primero no sustituye a nadie"
        assert primero.json()["vigente"] is True

        segundo = await cliente.post(
            f"/api/v1/admin/organizations/{organizacion.id}/price-overrides",
            headers=cabeceras,
            json={
                "operacion": "SCAN_CREDIT_COST",
                "valor": "4",
                "motivo": "revision de la cotizacion",
            },
        )
        assert segundo.status_code == 201, segundo.text
        cuerpo = segundo.json()
        assert cuerpo["valor_sustituido"] == "3.00000000", "tiene que decir a qué sustituye"
        assert cuerpo["sustituye_id"] == primero.json()["id"]
        assert cuerpo["motivo_sustituido"] == "primera cotizacion"

    filas = (
        (
            await integration_session.execute(
                select(OrganizationPriceOverride).where(
                    OrganizationPriceOverride.organization_id == organizacion.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(filas) == 2, "las dos filas se quedan"
    assert {f.valor for f in filas} == {Decimal("3"), Decimal("4")}
    assert all(f.valido_hasta is None for f in filas), (
        "ninguno se cierra: la tabla es append-only y nadie puede escribir en ella"
    )

    asientos = (
        await integration_session.execute(
            select(PlatformPriceChange).where(
                PlatformPriceChange.clave.like(f"override:{organizacion.id}:%")
            )
        )
    ).scalars().all()
    assert len(asientos) == 2, "un asiento por pactado"
    nuevo = next(a for a in asientos if a.valor_nuevo == Decimal("4"))
    assert nuevo.valor_anterior == Decimal("3"), "la traza conserva el valor que sale de juego"


async def test_la_consola_marca_el_vigente_y_distingue_el_futuro(
    integration_session: AsyncSession,
) -> None:
    organizacion = await _organizacion(integration_session)
    cabeceras = await _superusuario(integration_session, organizacion)

    async with _cliente() as cliente:
        for valor in ("3", "5"):
            respuesta = await cliente.post(
                f"/api/v1/admin/organizations/{organizacion.id}/price-overrides",
                headers=cabeceras,
                json={
                    "operacion": "SCAN_CREDIT_COST",
                    "valor": valor,
                    "motivo": f"pactado de {valor}",
                },
            )
            assert respuesta.status_code == 201, respuesta.text

        futuro = await cliente.post(
            f"/api/v1/admin/organizations/{organizacion.id}/price-overrides",
            headers=cabeceras,
            json={
                "operacion": "SCAN_CREDIT_COST",
                "valor": "8",
                "motivo": "subida dentro de un mes",
                "valido_desde": _dias(30).isoformat(),
            },
        )
        assert futuro.status_code == 201, futuro.text
        assert futuro.json()["vigente"] is False, "acaba de pactarse y aún no se cobra"

        lectura = await cliente.get(
            f"/api/v1/admin/organizations/{organizacion.id}/price-overrides",
            headers=cabeceras,
        )
        assert lectura.status_code == 200, lectura.text
        cuerpo = lectura.json()

    assert cuerpo["precios"]["scan_credit_cost"] == "5.00000000", (
        "hoy se cobra el de 5, no el de 8"
    )
    assert cuerpo["precios_de_plataforma"]["scan_credit_cost"] != cuerpo["precios"][
        "scan_credit_cost"
    ], "el panel tiene que poder comparar el pactado con el de plataforma"

    vigentes = [o for o in cuerpo["overrides"] if o["vigente"]]
    assert len(vigentes) == 1, "exactamente uno vigente"
    assert vigentes[0]["valor"] == "5.00000000"


async def test_una_organizacion_inexistente_no_admite_precio(
    integration_session: AsyncSession,
) -> None:
    organizacion = await _organizacion(integration_session)
    cabeceras = await _superusuario(integration_session, organizacion)

    async with _cliente() as cliente:
        respuesta = await cliente.post(
            f"/api/v1/admin/organizations/{uuid.uuid4()}/price-overrides",
            headers=cabeceras,
            json={
                "operacion": "SCAN_CREDIT_COST",
                "valor": "3",
                "motivo": "para una organizacion que no existe",
            },
        )
    assert respuesta.status_code == 404, respuesta.text


async def test_un_pack_inexistente_no_admite_precio(
    integration_session: AsyncSession,
) -> None:
    """Un `alcance` que no corresponde a ningún pack pactaría un precio imposible de aplicar."""

    organizacion = await _organizacion(integration_session)
    cabeceras = await _superusuario(integration_session, organizacion)

    async with _cliente() as cliente:
        respuesta = await cliente.post(
            f"/api/v1/admin/organizations/{organizacion.id}/price-overrides",
            headers=cabeceras,
            json={
                "operacion": "CREDIT_PACK_AMOUNT",
                "alcance": "999999",
                "valor": "99",
                "motivo": "un pack que no existe",
            },
        )
    assert respuesta.status_code == 422, respuesta.text


async def test_la_lectura_exige_superusuario(
    integration_session: AsyncSession,
) -> None:
    """La ficha de precios de un cliente es información comercial: no es del propio cliente."""

    organizacion = await _organizacion(integration_session)
    sufijo = uuid.uuid4().hex
    usuario = User(
        email=f"cliente-{sufijo}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Admin Cliente",
        email_verified=True,
        is_superuser=False,
    )
    integration_session.add(usuario)
    await integration_session.flush()
    integration_session.add(
        Membership(organization_id=organizacion.id, user_id=usuario.id, role=RoleEnum.ADMIN)
    )
    await integration_session.commit()
    cabeceras = {
        "Authorization": f"Bearer {create_access_token({'sub': str(usuario.id)})}"
    }

    async with _cliente() as cliente:
        sin_token = await cliente.get(
            f"/api/v1/admin/organizations/{organizacion.id}/price-overrides"
        )
        con_admin = await cliente.get(
            f"/api/v1/admin/organizations/{organizacion.id}/price-overrides",
            headers=cabeceras,
        )

    assert sin_token.status_code == 401
    assert con_admin.status_code == 403, (
        "ser el dueño de la organización no da acceso a la consola de precios"
    )


async def test_el_pacto_deja_la_cache_al_dia_en_este_proceso(
    integration_session: AsyncSession,
) -> None:
    """Pactar tiene que dejar este proceso cobrando lo pactado, sin esperar a nada.

    Es la mitad del requisito de «lo que se pinta es lo que se cobra»: el proceso que escribe es
    el que no puede tener una ventana. La otra mitad —que la lectura también refresque, para el
    caso de que el precio lo haya escrito otro proceso— está en su propia prueba, porque si las dos
    se mezclan en una, quitar el refresco de la lectura no se nota.
    """

    organizacion = await _organizacion(integration_session)
    cabeceras = await _superusuario(integration_session, organizacion)
    fijar_overrides(None)

    async with _cliente() as cliente:
        await cliente.post(
            f"/api/v1/admin/organizations/{organizacion.id}/price-overrides",
            headers=cabeceras,
            json={
                "operacion": "SCAN_CREDIT_COST",
                "valor": "3",
                "motivo": "pactado reci\u00e9n escrito",
            },
        )
        lectura = await cliente.get(
            f"/api/v1/admin/organizations/{organizacion.id}/price-overrides",
            headers=cabeceras,
        )
    assert lectura.status_code == 200, lectura.text
    assert lectura.json()["precios"]["scan_credit_cost"] == "3.00000000"
    assert precios_de(organizacion.id).scan_credit_cost == Decimal("3"), (
        "la caché de este proceso no se ha enterado del pactado"
    )


async def test_el_cuerpo_del_pactado_se_rechaza_antes_de_tocar_la_base(
    integration_session: AsyncSession,
) -> None:
    organizacion = await _organizacion(integration_session)
    cabeceras = await _superusuario(integration_session, organizacion)
    antes = await integration_session.execute(
        select(func.count()).select_from(OrganizationPriceOverride)
    )

    casos = [
        {"operacion": "SCAN_CREDIT_COST", "valor": "0", "motivo": "precio cero"},
        {"operacion": "SCAN_CREDIT_COST", "valor": "-5", "motivo": "precio negativo"},
        {"operacion": "CREDIT_PACK_AMOUNT", "valor": "99", "motivo": "sin alcance"},
        {
            "operacion": "SCAN_CREDIT_COST",
            "valor": "5",
            "motivo": "ab",
        },
        {
            "operacion": "SCAN_CREDIT_COST",
            "valor": "5",
            "motivo": "rango invertido",
            "valido_desde": _dias(10).isoformat(),
            "valido_hasta": _dias(1).isoformat(),
        },
    ]

    async with _cliente() as cliente:
        for cuerpo in casos:
            respuesta = await cliente.post(
                f"/api/v1/admin/organizations/{organizacion.id}/price-overrides",
                headers=cabeceras,
                json=cuerpo,
            )
            assert respuesta.status_code == 422, f"{cuerpo} debería rechazarse: {respuesta.text}"

    despues = await integration_session.execute(
        select(func.count()).select_from(OrganizationPriceOverride)
    )
    assert antes.scalar_one() == despues.scalar_one(), "un rechazo no escribe nada"


def test_el_esquema_de_respuesta_del_pactado_cubre_los_datos_del_sustituido() -> None:
    """Guarda contra que el POST deje de decir a quién sustituye.

    ## Por qué una prueba sobre el esquema y no sobre la respuesta HTTP

    Porque el POST ya se comprueba arriba. Esta es la red de seguridad para cuando alguien quita un
    campo para «simplificar»: el esquema tiene que seguir exigendo los tres, o el panel se queda
    sin poder pintar la cadena.
    """

    campos = PactadoResponse.model_fields

    assert {"sustituye_id", "valor_sustituido", "motivo_sustituido", "vigente"} <= set(campos)
    for nombre in ("sustituye_id", "valor_sustituido", "motivo_sustituido"):
        assert campos[nombre].is_required() is False, (
            f"{nombre} puede ser None cuando es el primer pactado, pero no puede desaparecer"
        )


async def test_la_lectura_trae_el_precio_que_escribio_otro_proceso(
    integration_session: AsyncSession,
) -> None:
    """La lectura refresca, y no solo la escritura.

    ## Por qué esta prueba existe aparte de la del POST

    Porque las dos rutas de refresco se pueden quitar por separado, y si solo se probara la del
    POST, quitar la de la lectura pasaría el test entero. En el caso real es justo la que hace
    falta: un comercial pacta desde el panel de otra máquina, y este proceso —un worker de cobro,
    un webhook— se entera cuando alguien mira la ficha del cliente.

    Y es también la que evita el fallo más caro de la caché: pintar un precio y cobrar otro. Que el
    panel diga 3 y la factura diga 10 no lo detecta nadie hasta que el cliente protesta.
    """

    organizacion = await _organizacion(integration_session)
    cabeceras = await _superusuario(integration_session, organizacion)
    fijar_overrides(None)

    # Se escribe **por la sesión**, no por la API: así no hay ninguna escritura que refresque, y
    # el precio existe solo en la base. Es exactamente la situación de otro proceso.
    await _fila_en_base(
        integration_session,
        organizacion.id,
        PriceOperationEnum.SCAN_CREDIT_COST,
        "3",
        _dias(-1),
    )

    async with _cliente() as cliente:
        lectura = await cliente.get(
            f"/api/v1/admin/organizations/{organizacion.id}/price-overrides",
            headers=cabeceras,
        )
    assert lectura.status_code == 200, lectura.text
    assert lectura.json()["precios"]["scan_credit_cost"] == "3.00000000"
    assert precios_de(organizacion.id).scan_credit_cost == Decimal("3"), (
        "la lectura ha pintado un precio que este proceso no cobraría"
    )


async def test_pactar_despues_de_una_caducidad_no_dice_que_sustituye_a_lo_caducado(
    integration_session: AsyncSession,
) -> None:
    """La respuesta tiene que decir el precio que se cobraba, no el último que se escribió.

    ## Por qué hacen falta **dos** pactados anteriores

    Porque con uno solo no se distingue nada: si el único anterior estaba caducado, tanto la regla
    correcta como la que se lo salta eligen esa misma fila, y el test pasa con las dos
    implementaciones. La separación aparece con dos filas de fechas distintas:

    - una de hace dos meses **sin fecha de fin**, que es la que se sigue cobrando;
    - otra de hace un mes **con fecha de fin ya pasada**, que se escribió después y por tanto
      es la más reciente.

    La correcta dice «sustituye a 4», que es lo que estaba aplicando. La que se salta el filtro de
    caducidad dice «sustituye a 3», que es un número que ese cliente no estaba pagando. En la
    traza, dentro de un año, eso es una afirmación falsa sobre dinero.
    """

    organizacion = await _organizacion(integration_session)
    cabeceras = await _superusuario(integration_session, organizacion)

    # El vivo: sin fecha de fin, de hace dos meses, y por tanto es el que se cobra.
    await _fila_en_base(
        integration_session,
        organizacion.id,
        PriceOperationEnum.SCAN_CREDIT_COST,
        "4",
        _dias(-60),
    )
    # El más reciente, pero caducado hace tres días: ya no se está cobrando.
    await _fila_en_base(
        integration_session,
        organizacion.id,
        PriceOperationEnum.SCAN_CREDIT_COST,
        "3",
        _dias(-30),
        hasta=_dias(-3),
    )

    async with _cliente() as cliente:
        respuesta = await cliente.post(
            f"/api/v1/admin/organizations/{organizacion.id}/price-overrides",
            headers=cabeceras,
            json={
                "operacion": "SCAN_CREDIT_COST",
                "valor": "7",
                "motivo": "nuevo acuerdo tras la caducidad",
            },
        )
    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()

    assert Decimal(cuerpo["valor"]) == Decimal("7")
    assert cuerpo["vigente"] is True
    # Se compara como `Decimal` y no como texto: el formato de un importe serializado depende de la
    # escala que traiga de la base, y un test que falla al añadir un decimal al esquema no está
    # comprobando el negocio.
    assert Decimal(cuerpo["valor_sustituido"]) == Decimal("4"), (
        "lo que dejaba de cobrarse es el pactado vivo, no el caducado aunque sea más reciente"
    )
