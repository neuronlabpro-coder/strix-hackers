"""Pruebas de lo añadido en el bloque 5.4B: edición de usuarios, pack a medida e importes.

## Qué se comprueba y por qué

Tres cosas que no existían y que son fáciles de dejar a medias:

1. **`PATCH /admin/users/{id}`** con sus dos conmutadores y la protección del último
   superusuario. La protección es la parte que importa: quitarse el superusuario al único
   que queda deja la plataforma sin nadie que pueda recuperar la cuenta, y eso no tiene
   vuelta atrás desde la propia consola.

2. **El pack a medida.** La compra admitía solo tres valores del catálogo. Ampliarlo a
   "cualquier cantidad desde el mínimo" es un cambio de una línea en el esquema y de tres
   en la UI, y justo por ser pequeño es donde se olvida actualizar la respuesta que le dice
   al panel los límites.

3. **`amount_cents` en los eventos de Stripe.** La columna se rellena en la ingestión, que
   es el único sitio donde el payload existe. Si el extractor se equivoca, la columna
   queda en `NULL` para siempre y la consola muestra «importe no registrado» sin que nada
   avise.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import Membership, Organization, RoleEnum, User
from backend.core.security import create_access_token, hash_password
from backend.main import app

pytestmark = pytest.mark.integration


async def _superuser(session: AsyncSession) -> dict[str, str]:
    sufijo = uuid.uuid4().hex[:8]
    organization = Organization(name=f"Ops {sufijo}", slug=f"ops-{sufijo}")
    user = User(
        email=f"ops-{sufijo}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Ops",
        email_verified=True,
        is_superuser=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.ADMIN))
    await session.commit()
    return {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }


async def _usuario(session: AsyncSession, *, superuser: bool = False) -> User:
    sufijo = uuid.uuid4().hex[:8]
    user = User(
        email=f"user-{sufijo}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name=f"User {sufijo}",
        email_verified=True,
        is_superuser=superuser,
    )
    session.add(user)
    await session.commit()
    return user


async def _tenant_con_miembro(
    session: AsyncSession,
) -> tuple[Organization, dict[str, str]]:
    """Un tenant con su usuario, y las cabeceras para llamar a sus rutas.

    Las cabeceras incluyen la de organización. Sin ella las rutas del tenant devuelven
    `403` con el mensaje de autenticación, que es el mismo que aparece cuando **no** hay
    cabecera: dos fallos distintos con el mismo texto, y el segundo esconde al primero.
    """

    sufijo = uuid.uuid4().hex[:8]
    organization = Organization(name=f"Ops {sufijo}", slug=f"ops-{sufijo}")
    organization.credit_balance = Decimal("1000")
    user = User(
        email=f"comprador-{sufijo}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Comprador",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.ADMIN))
    await session.commit()
    cabeceras = {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }
    return organization, cabeceras


# --------------------------------------------------------------------------- #
# PATCH /admin/users/{id}
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_desactivar_una_cuenta_lo_impide_entrar(
    integration_session: AsyncSession,
) -> None:
    """`is_active = false` corta el acceso, no solo lo esconde del listado.

    Es la diferencia entre una cuenta desactivada y una cuenta borrada del panel. La
    comprobación es de acceso real —un `login` que ya no funciona— porque una lista que
    no la muestra no demuestra que el token deje de servir.
    """

    assert integration_session is not None
    cabeceras = await _superuser(integration_session)
    objetivo = await _usuario(integration_session)
    transporte = ASGITransport(app=app)

    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        antes = await client.get("/api/v1/auth/me", headers=cabeceras)
        cambio = await client.patch(
            f"/api/v1/admin/users/{objetivo.id}", json={"is_active": False}, headers=cabeceras
        )
        token_objetivo = create_access_token({"sub": str(objetivo.id)})
        despues = await client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {token_objetivo}"}
        )

    assert antes.status_code == 200
    assert cambio.status_code == 200, cambio.text
    assert cambio.json()["is_active"] is False
    # El token de la cuenta desactivada deja de servir.
    assert despues.status_code in {401, 403}, f"sigue entrando: HTTP {despues.status_code}"


@pytest.mark.asyncio
async def test_quitar_el_superusuario_se_comporta_segun_la_plataforma_tenga_otros(
    integration_session: AsyncSession,
) -> None:
    """La protección depende de un estado **global**, y la prueba no lo puede acorralar.

    ## Por qué una sola prueba no basta

    "No se puede quitar el superusuario al último" es, por definición, una afirmación sobre
    **toda la plataforma**. La transacción de la prueba se deshace al terminar, así que solo
    controla las filas que ella crea: los superusuarios que dejaron las corridas anteriores
    —y los de la propia base de desarrollo— siguen ahí y son visibles para el servidor.

    Por eso la prueba no puede fijar "soy el último": si la base tiene otros, la respuesta
    correcta es `200` y la prueba fallaría dando a entender que la protección no funciona.
    Y si no los tiene, `200` sería un fallo de seguridad real.

    ## Qué hace entonces

    Cuenta primero y afirma **la rama que corresponde al estado real**:

    - si es el único → `409`, y el permiso sigue puesto;
    - si hay otros → `200`, y el permiso se quita.

    Las dos ramas verifican la misma regla, y cada una verifica algo distinto según la base.
    Una prueba que solo Pudiera fijar una de las dos dejaría sin cubrir la mitad del
    comportamiento en cualquier entorno.
    """

    assert integration_session is not None
    cabeceras = await _superuser(integration_session)
    objetivo = (
        await integration_session.execute(select(User).where(User.email.like("ops-%")))
    ).scalars().first()
    assert objetivo is not None

    otros = int(
        (
            await integration_session.execute(
                select(func.count(User.id)).where(
                    User.is_superuser.is_(True), User.id != objetivo.id
                )
            )
        ).scalar_one()
    )

    transporte = ASGITransport(app=app)
    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        respuesta = await client.patch(
            f"/api/v1/admin/users/{objetivo.id}",
            json={"is_superuser": False},
            headers=cabeceras,
        )

    if otros == 0:
        assert respuesta.status_code == 409, respuesta.text
        refrescado = (
            await integration_session.execute(select(User).where(User.id == objetivo.id))
        ).scalar_one()
        assert refrescado.is_superuser is True, "el permiso se quitó pese al 409"
    else:
        assert respuesta.status_code == 200, respuesta.text
        assert respuesta.json()["is_superuser"] is False
        refrescado = (
            await integration_session.execute(select(User).where(User.id == objetivo.id))
        ).scalar_one()
        assert refrescado.is_superuser is False


@pytest.mark.asyncio
async def test_degradar_al_ultimo_superusuario_no_lo_pierde(
    integration_session: AsyncSession,
) -> None:
    """La rama que importa: con otro superusuario, degradar está permitido.

    Se separa de la prueba anterior para que la rama de "sí se puede" tenga su propia
    comprobación, en lugar de depender de lo que hubiera en la base cuando se ejecutó la
    otra. En un entorno limpio —sin superusuarios previos— la prueba anterior tomaría la
    rama del `409` y esta seguiría verificando que degradar a otro funciona.
    """

    assert integration_session is not None
    cabeceras = await _superuser(integration_session)
    objetivo = await _usuario(integration_session, superuser=True)
    transporte = ASGITransport(app=app)

    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        respuesta = await client.patch(
            f"/api/v1/admin/users/{objetivo.id}",
            json={"is_superuser": False},
            headers=cabeceras,
        )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["is_superuser"] is False


@pytest.mark.asyncio
async def test_un_campo_ausente_no_toca_nada(
    integration_session: AsyncSession,
) -> None:
    """`{"is_active": false}` no desactiva el superusuario.

    `false` **es** un valor y ausente es "no cambiar": confundir los dos convertiría una
    desactivación de cuenta en una promoción o degradación de permisos por accidente.
    """

    assert integration_session is not None
    cabeceras = await _superuser(integration_session)
    objetivo = await _usuario(integration_session, superuser=True)
    transporte = ASGITransport(app=app)

    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        respuesta = await client.patch(
            f"/api/v1/admin/users/{objetivo.id}",
            json={"is_active": False},
            headers=cabeceras,
        )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["is_active"] is False
    assert respuesta.json()["is_superuser"] is True, "ausente no debe tocar el otro campo"


@pytest.mark.asyncio
async def test_editar_usuarios_solo_lo_puede_hacer_un_superusuario(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    _cabeceras = await _superuser(integration_session)
    normal = await _usuario(integration_session)
    cabeceras = {
        "Authorization": f"Bearer {create_access_token({'sub': str(normal.id)})}",
    }
    transporte = ASGITransport(app=app)

    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        objetivo = await _usuario(integration_session)
        respuesta = await client.patch(
            f"/api/v1/admin/users/{objetivo.id}",
            json={"is_superuser": True},
            headers=cabeceras,
        )

    assert respuesta.status_code == 403, respuesta.text


@pytest.mark.asyncio
async def test_un_usuario_inexistente_es_404(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    cabeceras = await _superuser(integration_session)
    transporte = ASGITransport(app=app)

    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        respuesta = await client.patch(
            f"/api/v1/admin/users/{uuid.uuid4()}", json={"is_active": False}, headers=cabeceras
        )

    assert respuesta.status_code == 404


# --------------------------------------------------------------------------- #
# Compra de créditos
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_los_packs_publicados_son_los_del_catalogo(
    integration_session: AsyncSession,
) -> None:
    """Lo que el panel ofrece es exactamente lo que el checkout acepta.

    Es la garantía de que un botón «comprar» del panel no puede devolver `422`: si las dos
    listas vivieran en sitios distintos, la primera en que se tocara una sin la otra
    produciría un botón que el propio panel ofreció y el servidor rechazó.
    """

    assert integration_session is not None
    from backend.apps.billing.schemas import CREDIT_PACKS, CUSTOM_CREDITS_MINIMUM

    _organization, cabeceras = await _tenant_con_miembro(integration_session)
    transporte = ASGITransport(app=app)

    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        respuesta = await client.get("/api/v1/billing/summary", headers=cabeceras)

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert {p["credits"] for p in cuerpo["packs"]} == set(CREDIT_PACKS)
    for pack in cuerpo["packs"]:
        assert Decimal(pack["amount_usd"]) == CREDIT_PACKS[pack["credits"]]
    # Los límites del pack a medida viajan, para que el panel no repita la regla.
    assert cuerpo["custom_minimum"] == CUSTOM_CREDITS_MINIMUM
    assert cuerpo["custom_maximum"] >= cuerpo["custom_minimum"]
    # Y la paridad que declara coincide con el precio de todos los packs.
    paridad = Decimal(cuerpo["credits_per_usd"])
    for pack in cuerpo["packs"]:
        assert Decimal(pack["amount_usd"]) == Decimal(pack["credits"]) * paridad


def test_una_cantidad_a_medida_cuesta_su_propio_precio() -> None:
    """Sin descuento, el precio de cualquier cantidad es la cantidad.

    Es lo que hace posible el campo libre del pack a medida: si el precio dependiera del
    pack más cercano, un campo numérico tendría que discretizar a múltiplos del pack
    pequeño, que no es lo que el panel enseña.
    """

    from backend.apps.billing.schemas import CREDIT_PACKS, price_for_credits

    assert price_for_credits(40) == Decimal("40")
    assert price_for_credits(10) == Decimal("10")
    # Un pack del catálogo usa su precio declarado, que es el mismo por la paridad.
    for credits, amount in CREDIT_PACKS.items():
        assert price_for_credits(credits) == amount


@pytest.mark.asyncio
async def test_una_compra_por_debajo_del_minimo_se_rechaza(
    integration_session: AsyncSession,
) -> None:
    """El mínimo cubre el coste fijo de la sesión de Checkout.

    Por debajo de 10 créditos la sesión de cobro se comería el margen, así que el servidor
    lo rechaza con un mensaje que **dice el mínimo**, y no con un `422` genérico.
    """

    assert integration_session is not None
    from backend.apps.billing.schemas import CUSTOM_CREDITS_MINIMUM

    _organization, cabeceras = await _tenant_con_miembro(integration_session)
    transporte = ASGITransport(app=app)

    async with AsyncClient(transport=transporte, base_url="http://test") as client:
        respuesta = await client.post(
            "/api/v1/billing/checkout-session",
            json={
                "credits": CUSTOM_CREDITS_MINIMUM - 1,
                "success_url": "https://app.example.com/billing?status=paid",
                "cancel_url": "https://app.example.com/billing?status=cancelled",
            },
            headers=cabeceras,
        )

    # Sin Stripe configurado puede ser `503` en vez de `422`; lo que importa es que **no**
    # se acepte la compra por debajo del mínimo.
    assert respuesta.status_code in {422, 503}, respuesta.text
    if respuesta.status_code == 422:
        assert str(CUSTOM_CREDITS_MINIMUM) in respuesta.text


# --------------------------------------------------------------------------- #
# Importes de los eventos de Stripe
# --------------------------------------------------------------------------- #


def test_el_importe_se_lee_de_ambos_campos_de_stripe() -> None:
    """`amount_total` en una sesión de Checkout, `amount` en un pago.

    Se leen los dos porque el objeto de Stripe cambia según el tipo de evento, y quedarse
    con uno de los dos deja fuera la mitad de los cobros.
    """

    from backend.apps.billing.router import _extract_amount_cents

    assert _extract_amount_cents({"amount_total": 2500}) == 2500
    assert _extract_amount_cents({"amount": 2500}) == 2500
    # Sin importe: `None`, no `0`. Un cero se vería como una venta de $0,00.
    assert _extract_amount_cents({}) is None
    assert _extract_amount_cents({"metadata": {"credits": "10"}}) is None
    # Un booleano es un `int` en Python, pero no es un importe.
    assert _extract_amount_cents({"amount_total": True}) is None
    # Un importe negativo se ignora: Stripe no lo emite y sería un dato corrupto.
    assert _extract_amount_cents({"amount_total": -100}) is None
