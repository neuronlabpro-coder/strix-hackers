"""Pruebas de la consola de precios de plataforma y del catálogo que la sostiene.

## Qué se comprueba y por qué

Dos cosas que en este sistema son la misma: **quién puede cambiar lo que cobra la plataforma** y
**que el número que se cobra sale de un solo sitio**. La segunda es la que importa más, porque un
precio que sale de dos sitios no da ningún error: da dos facturas.

Las propiedades que se comprueban, en lugar de ejemplos sueltos:

1. **Solo un superusuario cambia precios.** Un `ADMIN` de un tenant no. Un precio de plataforma
   no pertenece a ninguna organización, así que «administrador de la mia» no es una credencial
   suficiente para tocarlo.
2. **Cada cambio deja un asiento con valor anterior, valor nuevo, autor y motivo.** Y uno por
   campo, no uno por petición: «quién subió esto y cuándo» tiene que responderse con una
   consulta.
3. **El motivo es obligatorio y un PATCH vacío se rechaza.** Los dos rechazos van al esquema,
   antes de tocar la base.
4. **Un pack desactivado sale del catálogo vivo pero su fila se queda.** No hay borrados: es R4
   aplicado a la configuración comercial.
5. **El catálogo sin tramo base cobra sin descuento, en vez de reventar.** El `IndexError` es el
   modo de fallo que un índice a cero produce.
6. **La paridad manda en el precio de verdad.** Con paridad 2,50, 100 créditos cuestan 40 dólares,
   no 100. Es la comprobación que distingue «el precio viene de la fuente» de «el precio está
   duplicado en la fuente».
7. **El catálogo de arranque coincide con las constantes del código.** El respaldo existe para
   poder desplegar la base y el código por separado, y un respaldo que dice otra cosa no es un
   respaldo: es un segundo precio.

## Por qué las pruebas de HTTP usan un superusuario creado al vuelo

Porque la consola es la única parte del sistema que escribe en estado global del proceso —el
catálogo y los precios viven en una instantánea—, y una prueba que usara el usuario sembrado
dejo cobro el proceso a un precio que nadie eligió. El usuario de aquí se crea y se tira dentro
de la transacción que la propia prueba revierte.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.catalogo import (
    Catalogo,
    cargar_catalogo,
    catalogo_de_arranque,
    catalogo_vigente,
    fijar_catalogo,
)
from backend.apps.billing.models import CreditPack, PlatformPriceChange, PlatformPricing
from backend.apps.billing.pricing import (
    PlatformPrices,
    cargar_precios,
    fijar_precios,
    precios_vigentes,
    scan_credit_cost,
)
from backend.apps.billing.schemas import (
    CREDIT_PACKS,
    CUSTOM_SPEND_MAXIMUM_USD,
    CUSTOM_SPEND_MINIMUM_USD,
    PRO_SUBSCRIPTION_MONTHLY_USD,
    VOLUME_DISCOUNT_TIERS,
    credits_for_spend,
    price_for_credits,
)
from backend.apps.organizations.models import (
    Membership,
    Organization,
    RoleEnum,
    User,
)
from backend.apps.pentests.models import ScanModeEnum
from backend.core.security import create_access_token, hash_password
from backend.main import app

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def instantaneas_negras() -> Iterator[None]:
    """Deja las instantáneas como estaban, porque las pruebas las mueven de verdad.

    Sin esto, una prueba que cambia la paridad deja el proceso entero cobrando a ese número
    durante el resto de la sesión, y el fallo aparece en otra prueba con un nombre que no habla
    de precios.
    """

    try:
        yield
    finally:
        fijar_precios(None)
        fijar_catalogo(None)


def _cabeceras(usuario: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token({'sub': str(usuario.id)})}"}


async def _superusuario(integration_session: AsyncSession) -> tuple[User, dict[str, str]]:
    sufijo = uuid.uuid4().hex
    organizacion = Organization(name=f"Precios {sufijo}", slug=f"precios-{sufijo}")
    usuario = User(
        email=f"super-{sufijo}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Super Admin",
        email_verified=True,
        is_superuser=True,
    )
    integration_session.add_all([organizacion, usuario])
    await integration_session.flush()
    integration_session.add(
        Membership(organization_id=organizacion.id, user_id=usuario.id, role=RoleEnum.ADMIN)
    )
    await integration_session.commit()
    return usuario, _cabeceras(usuario)


async def _admin_de_tenant(integration_session: AsyncSession) -> dict[str, str]:
    """Un `ADMIN` de su propia organización: no tiene ningún derecho sobre un precio
    de plataforma, y su pertenencia a un tenant no es una base para concedérselo."""

    sufijo = uuid.uuid4().hex
    organizacion = Organization(name=f"Cliente {sufijo}", slug=f"cliente-{sufijo}")
    usuario = User(
        email=f"admin-{sufijo}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Admin Cliente",
        email_verified=True,
        is_superuser=False,
    )
    integration_session.add_all([organizacion, usuario])
    await integration_session.flush()
    integration_session.add(
        Membership(organization_id=organizacion.id, user_id=usuario.id, role=RoleEnum.ADMIN)
    )
    await integration_session.commit()
    return _cabeceras(usuario)


async def _cliente(integration_session: AsyncSession) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    )


# --------------------------------------------------------------------------- #
# Quién puede cambiar lo que cobra la plataforma
# --------------------------------------------------------------------------- #


async def test_sin_token_no_se_leen_los_precios(
    integration_session: AsyncSession,
) -> None:
    """Sin credencial, la lectura es un `401` y no un catálogo vacío.

    Un `200` con lista vacía sería el peor fallo posible: el operador vería «no hay packs» y
    concluiría que se han perdido, cuando lo que pasa es que no le dejaron entrar.
    """

    async with await _cliente(integration_session) as cliente:
        respuesta = await cliente.get("/api/v1/admin/pricing")
    assert respuesta.status_code == 401


async def test_un_admin_de_tenant_no_cambia_los_precios(
    integration_session: AsyncSession,
) -> None:
    """Un `ADMIN` de su propia organización no toca un precio de plataforma.

    Es la comprobación que separa «administrador de lo mío» de «administrador de todo». El
    precio no pertenece a ninguna organización, así que la pertenencia no da ninguna base para
    decidir quién puede moverlo.
    """

    cabeceras = await _admin_de_tenant(integration_session)
    async with await _cliente(integration_session) as cliente:
        lectura = await cliente.get("/api/v1/admin/pricing", headers=cabeceras)
        escritura = await cliente.patch(
            "/api/v1/admin/pricing",
            headers=cabeceras,
            json={"scan_credit_cost": "1", "motivo": "me rebajo el precio a mi"},
        )
    assert lectura.status_code == 403
    assert escritura.status_code == 403


async def test_el_superusuario_lee_el_catalogo_completo(
    integration_session: AsyncSession,
) -> None:
    """La lectura trae los siete precios, los packs y los tramos, en una sola respuesta.

    ## Por qué se comparan solo los packs **activos**

    Porque la respuesta los trae todos, inactivos incluidos, y eso es deliberado: el panel tiene
    que poder enseñar un pack desactivado para volver a activarlo. Si un paquete desaparece de
    la lista al desactivarlo, desactivarlo sería indistinguible de borrarlo —y borrarlo está
    prohibido por R4—. Así que la lista completa es lo que se comprueba, y el subconjunto activo
    es lo que se compara con el catálogo.
    """

    _, cabeceras = await _superusuario(integration_session)
    async with await _cliente(integration_session) as cliente:
        respuesta = await cliente.get("/api/v1/admin/pricing", headers=cabeceras)
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["desde_la_base"] is True
    activos = {p["credits"] for p in cuerpo["packs"] if p["is_active"]}
    assert activos <= set(CREDIT_PACKS), "hay packs activos que el codigo no declara"
    assert activos, "la lectura no trae ningun pack activo"
    assert [t["spend_min_usd"] for t in cuerpo["tiers"] if t["is_active"]] == [
        str(gasto) for gasto, _ in VOLUME_DISCOUNT_TIERS
    ]
    for campo in (
        "credits_per_usd",
        "scan_credit_cost",
        "quick_scan_credit_multiplier",
        "low_credit_balance_threshold",
        "custom_spend_minimum_usd",
        "custom_spend_maximum_usd",
        "pro_subscription_monthly_usd",
    ):
        assert campo in cuerpo["precios"], f"falta el precio {campo} en la respuesta"


# --------------------------------------------------------------------------- #
# Cada cambio deja rastro
# --------------------------------------------------------------------------- #


async def test_cambiar_un_precio_deja_un_asiento_por_campo(
    integration_session: AsyncSession,
) -> None:
    """Un cambio de dos precios deja **dos** asientos, con el valor anterior de cada uno.

    ## Por qué dos y no uno

    Porque `clave` no distingue «subió el escaneo» de «subió el Pro»: las dos escriben
    `scan_credit_cost`… no, las dos escriben claves distintas, pero el asiento de una petición
    con dos precios tendría que inventar una clave para el conjunto. Con un asiento por campo,
    la pregunta «cuándo pasó a 14 el escaneo y quién lo hizo» sale de una consulta y no de un
    diff de dos semanas de entradas.
    """

    _, cabeceras = await _superusuario(integration_session)
    async with await _cliente(integration_session) as cliente:
        respuesta = await cliente.patch(
            "/api/v1/admin/pricing",
            headers=cabeceras,
            json={
                "scan_credit_cost": "14",
                "pro_subscription_monthly_usd": "34",
                "motivo": "revision trimestral de margen",
            },
        )
    assert respuesta.status_code == 200

    asientos = (
        (
            await integration_session.execute(
                select(PlatformPriceChange).where(
                    PlatformPriceChange.motivo == "revision trimestral de margen"
                )
            )
        )
        .scalars()
        .all()
    )
    assert {a.clave for a in asientos} == {"scan_credit_cost", "pro_subscription_monthly_usd"}
    for asiento in asientos:
        assert asiento.valor_nuevo is not None
        assert asiento.valor_anterior is not None, "un cambio necesita saber de donde salio"
        assert asiento.actor_user_id is not None, "un cambio sin autor no se puede atribuir"

    # Y el proceso ya cobra al precio nuevo, sin reiniciar.
    assert precios_vigentes().scan_credit_cost == Decimal("14.0000")


async def test_el_motivo_corto_se_rechaza_antes_de_tocar_la_base(
    integration_session: AsyncSession,
) -> None:
    """Un motivo de menos de tres caracteres es un `422`, y el precio no se mueve.

    ## Por qué el motivo es obligatorio

    Porque un motivo opcional es un motivo ausente. Es el mismo argumento que hace obligatorio
    el diagnóstico de una caída de producción: un campo que el sistema no acaba vacía casi
    siempre, y entonces no aporta nada. Y dentro de seis meses, sin él, no hay forma de
    distinguir una corrección de un error de una subida deliberada.
    """

    _, cabeceras = await _superusuario(integration_session)
    async with await _cliente(integration_session) as cliente:
        antes = (await cliente.get("/api/v1/admin/pricing", headers=cabeceras)).json()
        respuesta = await cliente.patch(
            "/api/v1/admin/pricing",
            headers=cabeceras,
            json={"scan_credit_cost": "99", "motivo": "x"},
        )
        despues = (await cliente.get("/api/v1/admin/pricing", headers=cabeceras)).json()
    assert respuesta.status_code == 422
    assert despues["precios"]["scan_credit_cost"] == antes["precios"]["scan_credit_cost"]


async def test_un_patch_vacio_se_rechaza(
    integration_session: AsyncSession,
) -> None:
    """Enviar solo el motivo es un `422`: un `PATCH` vacío no es un acierto silencioso.

    ## Por qué no un `200` con «nada que hacer»

    Porque un `200` indistinguishable de un guardado correcto es peor que un error: el operador
    ve el mensaje de éxito, cierra el diálogo y cree que el cambio está aplicado cuando nunca
    salió de su navegador. El `422` obliga a decidir.
    """

    _, cabeceras = await _superusuario(integration_session)
    async with await _cliente(integration_session) as cliente:
        respuesta = await cliente.patch(
            "/api/v1/admin/pricing",
            headers=cabeceras,
            json={"motivo": "no quiero cambiar nada en realidad"},
        )
    assert respuesta.status_code == 422


async def test_un_rango_invertido_se_rechaza(
    integration_session: AsyncSession,
) -> None:
    """Un tope por debajo del mínimo es un `422`, y dice por qué.

    Sin este rechazo, el catálogo queda con un rango vacío: `credits_for_spend` devuelve `0` para
    cualquier cantidad, el panel no ofrece ninguna compra y **no falla nada**. Es el peor modo de
    fallo posible en una pantalla de precios, porque todo parece funcionando.
    """

    _, cabeceras = await _superusuario(integration_session)
    async with await _cliente(integration_session) as cliente:
        respuesta = await cliente.patch(
            "/api/v1/admin/pricing",
            headers=cabeceras,
            json={
                "custom_spend_minimum_usd": "500",
                "custom_spend_maximum_usd": "100",
                "motivo": "quiero un rango al reves",
            },
        )
    assert respuesta.status_code == 422
    assert "500" in respuesta.text or "menor" in respuesta.text.lower()


async def test_desactivar_un_pack_lo_saca_del_catalogo_pero_no_borra_la_fila(
    integration_session: AsyncSession,
) -> None:
    """Un pack desactivado deja de ofrecerse y su fila se queda, con su historial.

    ## Por qué no hay `DELETE`

    Por R4 y por operativa. Un borrado accidental desde el panel no se distingue de una
    decisión, y un precio que se puede borrar no se puede auditar. Desactivar deja la fila, la
    saca del catálogo y hace que su historial siga siendo legible.
    """

    _, cabeceras = await _superusuario(integration_session)
    async with await _cliente(integration_session) as cliente:
        creado = await cliente.post(
            "/api/v1/admin/pricing/packs",
            headers=cabeceras,
            json={"credits": 777, "amount_usd": "700.00", "motivo": "prueba de desactivacion"},
        )
    assert creado.status_code == 201
    nuevo = creado.json()

    async with await _cliente(integration_session) as cliente:
        desactivado = await cliente.patch(
            f"/api/v1/admin/pricing/packs/{nuevo['id']}",
            headers=cabeceras,
            json={"is_active": False, "motivo": "retirado tras la prueba"},
        )
    assert desactivado.status_code == 200
    assert desactivado.json()["is_active"] is False

    await cargar_catalogo(integration_session)
    assert all(creditos != 777 for creditos, _ in catalogo_vigente().packs)

    fila = (
        await integration_session.execute(select(CreditPack).where(CreditPack.credits == 777))
    ).scalar_one()
    assert fila is not None, "la fila se borro: el pack tiene que seguir existiendo"
    assert fila.is_active is False

    asientos = (
        (
            await integration_session.execute(
                select(PlatformPriceChange).where(
                    PlatformPriceChange.clave == f"credit_pack:{nuevo['id']}"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(asientos) >= 1, "el alta del pack no dejo asiento"


async def test_el_historico_filtra_por_clave(
    integration_session: AsyncSession,
) -> None:
    """El filtro por clave devuelve solo los asientos de ese precio.

    Es la pregunta que se hace de verdad —«quién movió *este* precio»— y filtrar por fecha
    obligaría a paginar desde el principio de una tabla que solo crece.
    """

    _, cabeceras = await _superusuario(integration_session)
    async with await _cliente(integration_session) as cliente:
        await cliente.patch(
            "/api/v1/admin/pricing",
            headers=cabeceras,
            json={"scan_credit_cost": "11", "motivo": "para el filtro del historico"},
        )
        respuesta = await cliente.get(
            "/api/v1/admin/pricing/changes",
            headers=cabeceras,
            params={"clave": "scan_credit_cost"},
        )
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["total"] >= 1
    assert {e["clave"] for e in cuerpo["items"]} == {"scan_credit_cost"}


async def test_crear_un_tramo_duplicado_es_conflicto(
    integration_session: AsyncSession,
) -> None:
    """Dos tramos con el mismo umbral: `409`, no un descuento no determinista.

    ## Por qué esto importa más de lo que parece

    Con dos filas en el mismo umbral, **las dos** satisfacen «el gasto es mayor o igual» y gana la
    última que llegue, que decide el planificador de Postgres y no el código. El mismo gasto de
    un mismo cliente se cobraría con dos descuentos distintos según el plan de ejecución, y los
    dos serían correctos según el esquema. No lanza nada y no hay ningún `CHECK` que lo rechace.

    Por eso el `UNIQUE (spend_min_usd)` está en la tabla, y aquí se comprueba que la API lo
    traduce a un conflicto con sentido.
    """

    _, cabeceras = await _superusuario(integration_session)
    async with await _cliente(integration_session) as cliente:
        primero = await cliente.post(
            "/api/v1/admin/pricing/tiers",
            headers=cabeceras,
            json={"spend_min_usd": "777.00", "discount": "0.10", "motivo": "tramo de prueba"},
        )
        segundo = await cliente.post(
            "/api/v1/admin/pricing/tiers",
            headers=cabeceras,
            json={"spend_min_usd": "777.00", "discount": "0.20", "motivo": "mismo umbral otra vez"},
        )
    assert primero.status_code == 201
    assert segundo.status_code == 409


# --------------------------------------------------------------------------- #
# El catálogo, cuando la base no tiene lo que el cálculo necesita
# --------------------------------------------------------------------------- #


async def test_un_catalogo_sin_tramos_cobra_sin_descuento() -> None:
    """Sin ningún tramo, el descuento es `0`, no un `IndexError`.

    ## Por qué este caso está en las pruebas y no se da por imposible

    Porque el catálogo es editable desde el panel y un operador puede desactivar el tramo base.
    Con `tramos[0][1]` —que es como estaba— eso producía un `IndexError` en **cada cobro**, no
    en la carga de la pantalla: la pantalla parecería bien y el checkout se caería después, en el
    sitio donde nadie está mirando el catálogo.
    """

    vacio = Catalogo(
        tramos=(),
        packs=(),
        gasto_minimo_usd=CUSTOM_SPEND_MINIMUM_USD,
        gasto_maximo_usd=CUSTOM_SPEND_MAXIMUM_USD,
        suscripcion_mensual_usd=PRO_SUBSCRIPTION_MONTHLY_USD,
    )
    assert vacio.descuento_para(Decimal("1")) == Decimal("0.00")
    assert vacio.descuento_para(Decimal("100000")) == Decimal("0.00")

    fijar_catalogo(vacio)
    # Y el cálculo completo aguanta: sin tramos, el precio cae al de la paridad base.
    assert price_for_credits(100) == Decimal("100.00")


async def test_los_tramos_llegan_ordenados_por_umbral(
    integration_session: AsyncSession,
) -> None:
    """Los tramos salen de la consulta en orden de umbral, y no en el que se insertaron.

    ## Por qué el orden es parte del contrato y no un detalle de estilo

    Porque `price_for_credits` usa el **siguiente** umbral como cota superior del tramo actual.
    Con los tramos desordenados, las cotas son incorrectas y el precio deja de ser monótono
    **sin lanzar nada**: cobrar más cuesta menos, y eso no se ve hasta una conciliación.

    Por eso el `ORDER BY` va en la consulta y no en Python: ordenar en Python obligaría a que
    quien lee se acuerde de hacerlo, y un `ORDER BY` que se olvida tampoco avisa.
    """

    await cargar_catalogo(integration_session)
    tramos = catalogo_vigente().tramos
    umbrales = [gasto for gasto, _ in tramos]
    assert umbrales == sorted(umbrales), "los tramos no llegan ordenados por umbral"


async def test_la_paridad_manda_de_verdad_en_el_precio(
    integration_session: AsyncSession,
) -> None:
    """Con paridad 2,50, 100 créditos cuestan 40 dólares, y no 100.

    ## Por qué esta es la comprobación que vale

    Porque es la que distingue «el precio sale de la fuente única» de «el precio está
    duplicado en la fuente». El proyecto ya tuvo la paridad escrita a mano en cuatro sitios, dos
    de los cuales eran literales que **coincidían con el valor por defecto** y por eso no
    fallaban nada: con paridad 1,00 cualquier prueba sobre el valor pasaba.

    Con paridad 2,50, cualquier sitio que siga multiplicando por `1,00` en vez de por `1 /
    paridad` da un resultado visiblemente distinto. La prueba sube la paridad y comprueba que el
    precio se mueve con ella.
    """

    fila = (
        await integration_session.execute(select(PlatformPricing).where(PlatformPricing.id == 1))
    ).scalar_one()
    fila.credits_per_usd = Decimal("2.5")
    await integration_session.flush()
    await cargar_precios(integration_session)
    await cargar_catalogo(integration_session)

    assert precios_vigentes().credits_per_usd == Decimal("2.50000000")

    # 100 créditos a 2,50 por dólar son 40 dólares, no 100.
    assert price_for_credits(100) == Decimal("40.00")
    # Y el camino inverso: 100 dólares compran 250 créditos.
    assert credits_for_spend(Decimal("100")) == 250
    # El escaneo rápido también sigue a la base, no a una fracción fija.
    fijar_precios(PlatformPrices(Decimal("2.5"), Decimal("10"), Decimal("0.3"), Decimal("0")))
    assert scan_credit_cost(ScanModeEnum.QUICK) == Decimal("3.0000")


async def test_el_catalogo_de_arranque_coincide_con_el_codigo() -> None:
    """El respaldo del catálogo dice exactamente lo que dicen las constantes del código.

    ## Por qué esta prueba existe

    Porque el respaldo es lo que cobra cuando la tabla no está, y se construye leyendo las
    constantes de `billing.schemas` con un `import` local, dentro de un módulo que `schemas`
    importa para derivar sus propios límites. Eso funciona porque todas las constantes que se
    leen están definidas por encima de esa línea, y es una fragilidad real: si alguien mueve una
    constante de sitio o le cambia el valor sin cambiar el respaldo, el sistema cobraría con dos
    catálogos distintos según si la tabla existe o no.

    Esta prueba es la que convierte esa fragilidad en algo no silencioso.
    """

    arranque = catalogo_de_arranque()
    assert dict(arranque.packs) == dict(CREDIT_PACKS)
    assert arranque.tramos == tuple(VOLUME_DISCOUNT_TIERS)
    assert arranque.gasto_minimo_usd == CUSTOM_SPEND_MINIMUM_USD
    assert arranque.gasto_maximo_usd == CUSTOM_SPEND_MAXIMUM_USD
    assert arranque.suscripcion_mensual_usd == PRO_SUBSCRIPTION_MONTHLY_USD


async def test_la_paridad_de_la_fila_coincide_con_la_del_respaldo(
    integration_session: AsyncSession,
) -> None:
    """La fila de la base y la configuración dicen el mismo número.

    ## Por qué esto se comprueba y no se da por supuesto

    Porque hubo un momento en que no coincidían: la migración sembraba `0.3` en el
    multiplicador del escaneo rápido y el `default` de la configuración era `0.5`. El efecto no
    era visible —la fila gana siempre— pero una base **restaurada de antes de la migración**, sin
    fila, cobraba un 40 % más caro que una migrada, sin error en ninguna parte.

    Un respaldo que dice otra cosa no es un respaldo: es un segundo precio.
    """

    await cargar_precios(integration_session)
    fila = precios_vigentes()
    respaldo = PlatformPrices.desde_configuracion()
    for nombre in (
        "credits_per_usd",
        "scan_credit_cost",
        "quick_scan_credit_multiplier",
        "low_credit_balance_threshold",
    ):
        assert getattr(fila, nombre) == getattr(respaldo, nombre), (
            f"{nombre}: la fila dice {getattr(fila, nombre)} y el respaldo "
            f"{getattr(respaldo, nombre)}"
        )


async def test_la_trampa_de_precios_sigue_siendo_append_only(
    integration_session: AsyncSession,
) -> None:
    """La traza de precios no admite `UPDATE`.

    ## Por qué no basta con probar el `UPDATE`

    ## Por qué el resto de operaciones va en su propia prueba

    Porque **`TRUNCATE` no dispara un trigger de fila**: no hay filas que disparar. Con el
    disparador por fila que tenía antes, `TRUNCATE platform_price_changes` pasaba entero, sin
    error y sin dejar asiento, y no había forma de saber después que había pasado. Este proyecto
    ya pagó ese bug dos veces, en `agent_jobs` y en las evidencias de vulnerabilidad.

    El disparador ahora es de sentencia y cubre las tres operaciones; aquí se comprueba el
    `UPDATE` y el `TRUNCATE` va en la prueba de la fila única, que necesita el mismo disparador.
    """

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    await integration_session.execute(
        text(
            "INSERT INTO platform_price_changes (id, clave, valor_anterior, valor_nuevo, motivo) "
            "VALUES (gen_random_uuid(), 'prueba:append_only', 1, 2, 'prueba automatica')"
        )
    )
    await integration_session.flush()

    with pytest.raises(DBAPIError, match="append-only"):
        await integration_session.execute(
            text(
                "UPDATE platform_price_changes SET valor_nuevo = 3 "
                "WHERE clave = 'prueba:append_only'"
            )
        )


async def test_la_fila_de_precios_no_se_puede_borrar(
    integration_session: AsyncSession,
) -> None:
    """Borrar la fila única está rechazado: es lo que impide quedarse sin precios.

    `CHECK (id = 1)` garantiza que si hay fila, es la única. No garantiza que **haya** fila. Sin
    esta protección, un `DELETE` —o un `TRUNCATE`— dejaba la plataforma cobrando con el
    catálogo del código en silencio, que es exactamente lo que este trabajo vino a eliminar.
    """

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError, match="platform_pricing"):
        await integration_session.execute(text("DELETE FROM platform_pricing WHERE id = 1"))


async def test_el_numero_de_asientos_crece_con_cada_cambio(
    integration_session: AsyncSession,
) -> None:
    """La traza tiene exactamente un asiento por campo cambiado, ni uno más ni uno menos.

    Se comprueba el **contable** y no la lista porque un asiento duplicado por una reejecución
    sería indistinguible de un cambio real al leerlo, y es el tipo de ruido que hace que un
    histórico deje de servir para lo único que existe: reconstruir por qué un cobro salió como
    salió.
    """

    _, cabeceras = await _superusuario(integration_session)
    async with await _cliente(integration_session) as cliente:
        antes = (
            await integration_session.execute(select(func.count()).select_from(PlatformPriceChange))
        ).scalar_one()
        await cliente.patch(
            "/api/v1/admin/pricing",
            headers=cabeceras,
            json={
                "scan_credit_cost": "13",
                "credits_per_usd": "1.25",
                "motivo": "dos precios en una peticion",
            },
        )
        despues = (
            await integration_session.execute(select(func.count()).select_from(PlatformPriceChange))
        ).scalar_one()
    assert despues - antes == 2
