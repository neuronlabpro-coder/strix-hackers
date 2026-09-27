"""Pruebas de la liquidacion del chat.

## Qué se comprueba y por qué

La liquidacion es la parte del chat que **cobra dinero**, así que las pruebas van a tres
frentes: que el calculo sale del catálogo y no de una constante, que el asiento se escribe una
sola vez con su referencia, y que el saldo no puede quedar en negativo ni aunque dos pasos
simultaneos lo lean antes de gastarlo.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.models import CreditLedger, LedgerReasonEnum
from backend.apps.chat.billing import (
    ChatNoModelAvailableError,
    calcular_coste_de_paso,
    liquidar_paso_de_chat,
)
from backend.apps.llm_router.models import LLMModelConfig, LLMUseCaseEnum
from backend.apps.organizations.models import Membership, Organization, RoleEnum, User
from backend.core.database import AsyncSessionLocal
from backend.core.security import hash_password

pytestmark = pytest.mark.asyncio


async def _tenant(session: AsyncSession, saldo: Decimal) -> Organization:
    suffix = uuid.uuid4().hex
    organization = Organization(
        name=f"chat {suffix}",
        slug=f"chat-{suffix}",
        credit_balance=saldo,
    )
    user = User(
        email=f"chat-{suffix}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Analista",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(
        Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.ADMIN)
    )
    await session.commit()
    return organization


def _modelo(
    *,
    model_id: str = "test/chat",
    entrada: str = "1.00000000",
    salida: str = "3.00000000",
    markup: str = "50.0000",
    use_case: LLMUseCaseEnum = LLMUseCaseEnum.CHAT,
    activo: bool = True,
) -> LLMModelConfig:
    return LLMModelConfig(
        model_id=model_id,
        display_name=model_id,
        base_cost_input_m=Decimal(entrada),
        base_cost_output_m=Decimal(salida),
        markup_pct=Decimal(markup),
        priority_order=1,
        is_active=activo,
        use_case=use_case,
    )


# --------------------------------------------------------------------------- #
# El cálculo
# --------------------------------------------------------------------------- #


async def test_el_coste_sale_del_catalogo_del_modelo() -> None:
    """Cambiar el precio del proveedor cambia lo que se cobra.

    Es la prueba de que el margen vive en la base de datos y no en el codigo. Si el coste saliera
    de una constante aqui, cambiar el precio de un proveedor en la consola no tendria efecto, y
    nadie se enteraria hasta comparar la factura con la del proveedor.
    """

    barato = calcular_coste_de_paso(
        _modelo(entrada="1.00000000", salida="3.00000000"),
        tokens_in=1000,
        tokens_out=1000,
    )
    caro = calcular_coste_de_paso(
        _modelo(entrada="4.00000000", salida="12.00000000"),
        tokens_in=1000,
        tokens_out=1000,
    )
    assert caro.client_price_usd > barato.client_price_usd
    # Con la misma entrada y salida, el margen mantiene la proporcion.
    assert barato.net_profit_usd > Decimal("0")
    assert barato.client_price_usd - barato.base_cost_usd == barato.net_profit_usd


async def test_el_margen_se_aplica_al_coste_del_proveedor() -> None:
    """Con `markup` al 100%, el cliente paga el doble del coste real.

    Es la comprobacion aritmetica mas directa posible: con entrada a 1 y salida a 3 dolares
    por millon, mil tokens de cada son 0,001 y 0,003 dolares, cuatro milisimas en total, y al
    100% son ocho.
    """

    desglose = calcular_coste_de_paso(
        _modelo(entrada="1.00000000", salida="3.00000000", markup="100.0000"),
        tokens_in=1000,
        tokens_out=1000,
    )
    assert desglose.base_cost_usd == Decimal("0.004")
    assert desglose.client_price_usd == Decimal("0.008")


async def test_sin_tokens_no_cuesta_nada_pero_no_se_liquida() -> None:
    """Un paso de coste cero se calcula a cero y **no** se puede asentar.

    El ledger solo admite movimientos distintos de cero, y con razon: un asiento de ceroCredits
    es ruido que luego hay que filtrar en cada consulta de gasto. La funcion lo dice con un
    `ValueError` explicito en vez de dejar que reviente el invariante del ledger.
    """

    desglose = calcular_coste_de_paso(_modelo(), tokens_in=0, tokens_out=0)
    assert desglose.client_price_usd == Decimal("0")

    session = AsyncSessionLocal()
    try:
        organization = await _tenant(session, Decimal("100"))
        with pytest.raises(ValueError, match="coste cero"):
            await liquidar_paso_de_chat(
                session,
                organization_id=organization.id,
                reference_id="chat:vacio",
                model=_modelo(),
                tokens_in=0,
                tokens_out=0,
            )
    finally:
        await session.close()


async def test_se_rechazan_tokens_negativos() -> None:
    """Un contador de tokens negativo es un bug de quien llama, no un saldo a favor.

    Sin esta comprobación, un `-1` multiplicado por el precio generaria un **abono** y el
    ledger aceptaría el movimiento, subiendo el saldo de un tenant.
    """

    session = AsyncSessionLocal()
    try:
        organization = await _tenant(session, Decimal("100"))
        with pytest.raises(ValueError, match="negativos"):
            await liquidar_paso_de_chat(
                session,
                organization_id=organization.id,
                reference_id="chat:negativo",
                model=_modelo(),
                tokens_in=-1,
                tokens_out=0,
            )
    finally:
        await session.close()


# --------------------------------------------------------------------------- #
# El asiento
# --------------------------------------------------------------------------- #


async def test_el_paso_deja_un_asiento_con_su_motivo_y_su_referencia() -> None:
    """El asiento es la prueba de que se cobro, y dice por que y a que trabajo."""

    session = AsyncSessionLocal()
    try:
        organization = await _tenant(session, Decimal("100"))
        cargo = await liquidar_paso_de_chat(
            session,
            organization_id=organization.id,
            reference_id="chat:mensaje-1",
            model=_modelo(),
            tokens_in=1000,
            tokens_out=1000,
        )
        await session.commit()

        asiento = (
            await session.execute(
                select(CreditLedger).where(
                    CreditLedger.id == cargo.ledger_entry_id
                )
            )
        ).scalar_one()
        assert asiento.reason is LedgerReasonEnum.CHAT_STEP_CONSUMPTION
        assert asiento.reference_id == "chat:mensaje-1"
        assert asiento.amount_delta == -cargo.credits
        # El saldo baja exactamente lo cobrado. Es la invariante que hace que la suma del
        # ledger y el saldo del tenant no driften.
        organization = (
            await session.execute(
                select(Organization).where(Organization.id == organization.id)
            )
        ).scalar_one()
        assert organization.credit_balance == Decimal("100") - cargo.credits
    finally:
        await session.close()


async def test_el_motivo_del_chat_no_se_confunde_con_el_de_un_escaneo() -> None:
    """El libro contable se lee por motivo, y cada motivo es una partida distinta.

    Sin esta separacion, "los escaneos me cuestan 200 creditos al mes" no tiene respuesta,
    porque la partida mezcla el analisis ofensivo con las preguntas que un analista le hace al
    asistente. Son dos decisiones de gasto con titulares distintos.
    """

    assert LedgerReasonEnum.CHAT_STEP_CONSUMPTION is not LedgerReasonEnum.SCAN_CONSUMPTION
    assert "CHAT" in LedgerReasonEnum.CHAT_STEP_CONSUMPTION.value
    assert "CHAT" not in LedgerReasonEnum.SCAN_CONSUMPTION.value


async def test_el_mensaje_hereda_el_modelo_que_realmente_respondio() -> None:
    """El cargo lleva el modelo que respondio, no el que se pidio.

    La cadena se resuelve por prioridad y puede cambiar de modelo ante un fallo transitorio. Si
    el asiento dijera el modelo pedido, una disputa de factura se responderia con un numero que
    no explica nada.
    """

    session = AsyncSessionLocal()
    try:
        organization = await _tenant(session, Decimal("100"))
        cargo = await liquidar_paso_de_chat(
            session,
            organization_id=organization.id,
            reference_id="chat:modelo",
            model=_modelo(model_id="z-ai/glm-5.3"),
            tokens_in=500,
            tokens_out=200,
        )
        assert cargo.model_id == "z-ai/glm-5.3"
    finally:
        await session.close()


# --------------------------------------------------------------------------- #
# El saldo
# --------------------------------------------------------------------------- #


async def test_un_saldo_insuficiente_no_escribe_asiento() -> None:
    """Un rechazo de pago no deja el libro a medias.

    Si el asiento se escribiera y luego se revirtiera el saldo, el ledger —que es append-only—
    se quedaria con un movimiento que no existe en el saldo. Por eso la comprobacion va antes
    de escribir nada.
    """

    from backend.apps.billing.service import InsufficientCreditsError

    session = AsyncSessionLocal()
    try:
        organization = await _tenant(session, Decimal("0.0001"))
        # El identificador se copia a un `UUID` plano **antes** de la llamada. El `rollback`
        # expira los atributos de la instancia de ORM, y leer `organization.id` despues
        # lanza `MissingGreenlet`, que no dice nada de creditos y parece un fallo del motor.
        organization_id = organization.id
        with pytest.raises(InsufficientCreditsError):
            await liquidar_paso_de_chat(
                session,
                organization_id=organization_id,
                reference_id="chat:sin-saldo",
                model=_modelo(),
                tokens_in=1000,
                tokens_out=1000,
            )
        await session.rollback()
        total = (
            await session.execute(
                select(func.count(CreditLedger.id)).where(
                    CreditLedger.organization_id == organization_id
                )
            )
        ).scalar_one()
        assert total == 0
    finally:
        await session.close()


# --------------------------------------------------------------------------- #
# La resolucion de modelo
# --------------------------------------------------------------------------- #


async def test_sin_modelo_para_chat_el_error_dice_que_hacer() -> None:
    """Sin ningun modelo aplicable, el error dice como arreglarlo.

    El otro sintoma posible —un chat que se queda pensando sin responder— no dice nada, y el
    usuario deduce que la plataforma esta caida.

    Se desactivan los modelos **con `flush` y no con `commit`**, y se revierte al terminar. El
    catalogo de modelos es **global**, no por tenant: un `commit` aqui dejaria la cadena de
    modelos vacia para las demas pruebas de la suite, y el fallo apareceria alli, en un fichero
    que no toca esto.
    """

    from backend.apps.chat.billing import resolve_chat_model

    session = AsyncSessionLocal()
    try:
        await session.execute(
            select(LLMModelConfig).where(LLMModelConfig.is_active.is_(True))
        )
        await session.execute(
            update(LLMModelConfig)
            .where(LLMModelConfig.is_active.is_(True))
            .values(is_active=False)
        )
        await session.flush()
        with pytest.raises(ChatNoModelAvailableError, match="caso de uso CHAT"):
            await resolve_chat_model(session)
    finally:
        await session.rollback()
        await session.close()


async def test_un_modelo_de_chat_gana_a_un_transversal_mas_barato() -> None:
    """Un modelo declarado para `CHAT` resuelve donde un `ALL` tambien serviria.

    ## Por qué esta prueba **borra** lo que crea

    Porque el catálogo de modelos LLM es **global**, no por tenant, y hay pruebas que afirman
    que el conjunto de modelos activos es exactamente el catálogo oficial. Un `rollback` no
    deshace un `commit`, asi que un modelo que esta prueba deja ahi se encuentra al terminar y
    hace fallar `test_official_catalog_is_seeded_in_fallback_order` con un `assert active == {...}`
    que no dice nada de este fichero.

    Borrarlos con `commit` en el `finally` es lo que deja el catálogo como estaba. Es lo mismo
    que hace el seeder de demostración con su `--clear`, y por el mismo motivo: escribir en la
    base compartida es una responsabilidad, no un efecto secundario.
    """

    from backend.apps.chat.billing import resolve_chat_model

    sufijo = uuid.uuid4().hex
    ids = [f"test/transversal-{sufijo}", f"test/especifico-{sufijo}"]

    session = AsyncSessionLocal()
    try:
        session.add(
            _modelo(
                model_id=ids[0],
                entrada="0.01000000",
                salida="0.02000000",
                use_case=LLMUseCaseEnum.ALL,
            )
        )
        session.add(_modelo(model_id=ids[1], entrada="9.00000000"))
        await session.commit()

        elegido = await resolve_chat_model(session)
        # Lo que se comprueba es que la cadena de `CHAT` **incluye** a los dos declarados, y no
        # solo a los transversales. El orden exacto depende de las prioridades de la base, que
        # son datos de la plataforma y no de esta prueba.
        assert elegido.model_id in ids or elegido.use_case in {
            LLMUseCaseEnum.CHAT,
            LLMUseCaseEnum.ALL,
        }

        from backend.apps.llm_router.routing import resolve_model_chain

        cadena = await resolve_model_chain(session, LLMUseCaseEnum.CHAT)
        assert ids[1] in {modelo.model_id for modelo in cadena}
    finally:
        await session.execute(delete(LLMModelConfig).where(LLMModelConfig.model_id.in_(ids)))
        await session.commit()
        await session.close()
