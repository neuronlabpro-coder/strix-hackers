"""Pruebas del catálogo oficial de OpenRouter, el enlace al runner y la telemetría."""

import uuid
from decimal import Decimal
from typing import cast

import pytest
from sqlalchemy import Numeric, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.models import CreditLedger, LedgerReasonEnum
from backend.apps.billing.service import apply_credit_delta, credit_balance_of
from backend.apps.llm_router.models import LLMModelConfig, LLMUsageEvent, LLMUseCaseEnum
from backend.apps.llm_router.pricing import compute_charge
from backend.apps.llm_router.routing import resolve_model_chain
from backend.apps.organizations.models import Organization
from backend.core.config import settings
from backend.workers.runner.sandbox import StrixSandboxManager
from backend.workers.runner.telemetry import (
    LlmUsageTelemetry,
    extract_token_usage,
    select_runtime_models,
)

pytestmark = pytest.mark.integration

# Catálogo oficial elegido por el Owner: (model_id, coste in, out, markup, prioridad, caso de uso)
#
# Catálogo activo, con su orden de fallback. El precio y el recargo van por unidad de
# `Decimal` para que la comparación sea exacta y no dependa de cómo el driver traiga el
# `NUMERIC` de la base.
#
# ## Por qué `stealth/space-bunny-alpha` NO está en esta tabla
#
# Porque el proveedor NO lo publica, así que no puede ser el primario de nada ni servir de
# alternativa. La fila sigue existiendo —`llm_usage_events.model_config_id` la referencia con
# `ON DELETE CASCADE`, y borrarla destruiría el historial de coste de los escaneos de prueba que
# se hicieron con él—, pero `is_active` es `false` y su prioridad es la última. Su hueco en esta
# tabla ES la aserción: `assert active == {...}` cae si vuelve a activarse, sin necesitar una
# prueba aparte para él.
#
# ## Por qué el primario es `z-ai/glm-5.3` y el resto es `AUTOFIX`
#
# Por la política del Owner: el pentesting ofensivo va a GLM y a nada más. `openai/gpt-6-sol`
# devuelve `provider_code: cyber_policy` y se niega a hacer análisis ofensivos, así que
# mientras estuvo declarado como `ALL` —transversal, en todas las cadenas— un escaneo podía
# acabar llegando a él después de gastar tokens y quedar sin resultado. `gpt-6-astra`,
# `claude-opus-5.5` y `gpt-6-sol` son modelos de revisión y reparación de CÓDIGO, que es un
# caso de uso distinto del que dispara un escaneo.
#
# ## Por qué esta tabla se lee y no se escribe
#
# Porque el primario **cambia**, y cambiandolo cambiaron antes las prioridades de todos los
# demas. Un test que fija el nombre del primario no se rompe cuando el Owner elige otro
# modelo: se rompe cuando el Owner elige otro modelo, que es exactamente cuando no deberia
#considered un fallo. Por eso las pruebas de este fichero comprueban la **regla** —el de
# prioridad 1 encabeza, y es transversal— y dejan el nombre en la tabla, que es la parte que
# si hay que revisar a mano cuando la cadena de modelos se reconfigura.
#
# El resto de las pruebas del fichero no nombran ningun primario, y eso es deliberado: si el
# primario vuelve a cambiar, estas pruebas siguen diciendo la verdad sobre el mecanismo.
EXPECTED_CATALOG = [
    # (model_id, coste entrada, coste salida, recargo, prioridad, caso de uso)
    ("z-ai/glm-5.3", "0.40", "1.60", "200.00", 1, "ALL"),
    ("openai/gpt-6-astra", "4.00", "18.00", "150.00", 2, "AUTOFIX"),
    ("anthropic/claude-opus-5.5", "5.00", "25.00", "150.00", 3, "AUTOFIX"),
    ("deepseek/deepseek-v4-pro-0813", "1.20", "4.80", "200.00", 4, "AUTOFIX"),
    ("anthropic/claude-fable-5.1", "2.00", "8.00", "150.00", 5, "AUTOFIX"),
    ("openai/gpt-6-sol", "1.50", "6.00", "200.00", 6, "AUTOFIX"),
    ("moonshotai/kimi-k3", "0.80", "3.20", "250.00", 7, "AUTOFIX"),
    ("deepseek/deepseek-v4.1-flash", "0.15", "0.60", "300.00", 8, "AUTOFIX"),
]

#: El modelo que el proveedor no publica. La fila no se borra, así que lo que se afirma no es su
#: ausencia sino sus dos propiedades: que no está activo y que no encabeza ninguna cadena.
MODELO_INALCANZABLE = "stealth/space-bunny-alpha"

#: El único modelo al que se permite lanzar un escaneo.
UNICO_MODELO_DE_PENTEST = "z-ai/glm-5.3"

#: Los tres casos de uso que acaban en una cadena de pentest: los dos que disparan un escaneo y el
#: transversal, que entra en los dos. `ALL` va aquí porque un modelo `ALL` con prioridad menor se
#: cuela en la cadena de pentest igual que uno `DEEP_PENTEST`, y fue exactamente así como
#: `openai/gpt-6-sol` acabó ejecutando escaneos que no podía hacer.
CASOS_DE_ESCANEO = (
    LLMUseCaseEnum.ALL,
    LLMUseCaseEnum.DEEP_PENTEST,
    LLMUseCaseEnum.QUICK_SCAN,
)

# Modelos del catálogo anterior que el Owner retiró. Ninguno debe quedar activo: un
# fallback que enrute a un modelo que producto ya no ofrece es un coste sincobrar.
#
# Los cinco primeros ya **no están** en la tabla: la migración `d4e5f6a7b8c9` los borra, y
# solo puede hacerlo porque no tienen eventos de consumo asociados —la FK de
# `llm_usage_events` es `ON DELETE CASCADE`, así que borrarlos con historial borraría el
# registro de coste, y por eso la migración se niega a hacerlo en vez de dejárselo al
# `CASCADE`—. Los dos últimos no se borran porque nunca se sembraron en este despliegue y
# `openrouter/auto` sigue presente por decisión propia: enruta al proveedor.
#
# La lista se mantiene entera, sin filtrar, a proposito: la aserción de abajo tiene que
# seguir valiendo para un `model_id` que se reincorporase por error, y para eso hace falta
# que el nombre siga escrito aqui.
RETIRED_MODEL_IDS = (
    "anthropic/claude-3.7-sonnet",
    "deepseek/deepseek-r1",
    "openai/o3-mini",
    "openai/gpt-4o",
    "deepseek/deepseek-chat",
    "openrouter/auto",
    "anthropic/claude-3.5-sonnet",
)


@pytest.mark.asyncio
async def test_official_catalog_is_seeded_in_fallback_order(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    models = (await integration_session.execute(select(LLMModelConfig))).scalars().all()
    by_id = {model.model_id: model for model in models}

    for model_id, cost_in, cost_out, markup, priority, use_case in EXPECTED_CATALOG:
        assert model_id in by_id, f"falta {model_id} en el catálogo"
        model = by_id[model_id]
        assert model.base_cost_input_m == Decimal(cost_in), model_id
        assert model.base_cost_output_m == Decimal(cost_out), model_id
        assert model.markup_pct == Decimal(markup), model_id
        assert model.priority_order == priority, model_id
        assert model.use_case == LLMUseCaseEnum(use_case), model_id
        assert model.is_active is True, model_id
        # El nombre visible no puede quedar vacío: la tabla de `/admin/llm` lo pinta.
        assert model.display_name.strip(), model_id

    # El catálogo activo es exactamente el del Owner, sin sobras ni filas deactivated
    # que la consola seguiría mostrando como si estuvieran disponibles.
    active = {model.model_id for model in models if model.is_active}
    assert active == {model_id for model_id, *_ in EXPECTED_CATALOG}


@pytest.mark.asyncio
async def test_el_modelo_que_no_publica_el_proveedor_no_es_resoluble(
    integration_session: AsyncSession,
) -> None:
    """`stealth/space-bunny-alpha` no puede salir en ninguna cadena, por ninguna puerta.

    ## Por qué se afirma sobre la fila y no sobre su ausencia

    Porque la fila **no se borra**: `llm_usage_events.model_config_id` la referencia con
    `ON DELETE CASCADE` y borrarla destruiría el registro de lo que costó cada escaneo de prueba
    que se hizo con ella. Lo que se afirma son sus dos propiedades, y las dos importan por
    motivos distintos: `is_active` es la que lo saca de la resolución, y la prioridad es la que
    evita que un despiste al reactivarlo lo vuelva a poner el primero.

    ## Por qué se comprueba **por las tres cadenas** y no solo por la de pentest

    Porque el fallo no fue de una cadena: `use_case = ALL` lo metía en todas. Comprobar solo
    `DEEP_PENTEST` habría pasado con el modelo como `QUICK_SCAN` o como `CHAT`, que es
    precisamente la clase de comprobación parcial que deja el defecto puesto.
    """

    assert integration_session is not None
    modelos = (await integration_session.execute(select(LLMModelConfig))).scalars().all()
    fila = next((modelo for modelo in modelos if modelo.model_id == MODELO_INALCANZABLE), None)

    if fila is not None:
        assert fila.is_active is False, (
            "un modelo que el proveedor no publica no puede estar activo"
        )
        prioridades_de_la_cadena = [
            modelo.priority_order for modelo in modelos if modelo.is_active
        ]
        assert fila.priority_order > max(prioridades_de_la_cadena, default=0), (
            "no puede encabezar nada: si alguien lo reactiva por un despiste tiene que salir al "
            "final de la cadena y no el primero"
        )

    for caso in CASOS_DE_ESCANEO:
        cadena = await resolve_model_chain(integration_session, caso)
        assert MODELO_INALCANZABLE not in {modelo.model_id for modelo in cadena}, caso.value


@pytest.mark.asyncio
@pytest.mark.parametrize("caso", CASOS_DE_ESCANEO)
async def test_el_pentest_resuelve_solo_a_glm(
    integration_session: AsyncSession, caso: LLMUseCaseEnum
) -> None:
    """Ninguna cadena de escaneo contiene un modelo que no pueda hacer pentesting.

    ## Por qué se afirma el **conjunto**, no el primero

    Porque el fallo original no era que GLM no encabezara: lo encabezaba. El fallo era que detrás
    había un segundo modelo que devolvía `provider_code: cyber_policy`, y un escaneo que llegaba
    hasta ahí había gastado tokens para nada. Afirmar solo `chain[0]` habría dado verde al
    defecto entero.

    ## Por qué `gpt-6-sol` tiene su propia línea

    Porque es el caso conocido y el que justificó la política: se niega explícitamente. La regla
    general de arriba ya lo cubre, y esta línea dice **por qué** existe la regla, para que nadie
    la borre creyéndola una formalidad.
    """

    assert integration_session is not None
    cadena = await resolve_model_chain(integration_session, caso)
    ids = {modelo.model_id for modelo in cadena}

    assert UNICO_MODELO_DE_PENTEST in ids, (
        f"la cadena de {caso.value} tiene que poder usar el modelo de pentest del Owner"
    )
    assert "openai/gpt-6-sol" not in ids, (
        "gpt-6-sol devuelve provider_code cyber_policy y se niega a hacer analisis ofensivos: "
        "dejarlo en una cadena de escaneo gasta tokens para obtener un rechazo"
    )


@pytest.mark.asyncio
async def test_default_explicito_gana_en_su_caso_sin_cambiar_pentest(
    integration_session: AsyncSession,
) -> None:
    opus = await integration_session.scalar(
        select(LLMModelConfig).where(LLMModelConfig.model_id == "anthropic/claude-opus-5.5")
    )
    assert opus is not None
    opus.is_default = True
    await integration_session.flush()
    autofix = await resolve_model_chain(integration_session, LLMUseCaseEnum.AUTOFIX)
    pentest = await resolve_model_chain(integration_session, LLMUseCaseEnum.DEEP_PENTEST)
    assert autofix[0].id == opus.id
    assert [model.model_id for model in pentest] == [UNICO_MODELO_DE_PENTEST]


@pytest.mark.asyncio
async def test_retired_models_are_not_active(
    integration_session: AsyncSession,
) -> None:
    """Un modelo retirado sale de la cadena, y de la tabla si no tiene historial que perder.

    Hay dos finales posibles y los dos son correctos, asi que la asercion los acepta a los
    dos:

    - **No esta en la tabla.** Es lo que ocurre con los cinco que la migracion
      `d4e5f6a7b8c9` borro, porque no tenian eventos de consumo.
    - **Esta pero con `is_active` falso.** Es lo que corresponde a un retirado con historial
      attached, o a `openrouter/auto`, que sigue en la tabla por decision propia. Lo que no
      puede es estar activo: eso es justo lo que `resolve_model_chain` no filtra, y lo que
      haria que el motor lo usara.

    Lo que esta asercion **no** puede hacer es declarar que un retirado no debe existir: si
    mañana se decide conservar uno por su historial, esta prueba tiene que seguir pasando. Por
    eso la condicion es "si esta, que no este activo", y no "que no este".
    """

    assert integration_session is not None
    models = (await integration_session.execute(select(LLMModelConfig))).scalars().all()
    by_id = {model.model_id: model for model in models}
    for retired in RETIRED_MODEL_IDS:
        if retired in by_id:
            assert by_id[retired].is_active is False, retired


@pytest.mark.asyncio
async def test_markup_column_replaced_profit_margin(
    integration_session: AsyncSession,
) -> None:
    """El campo de margen se llama `markup_pct`: es un recargo, no un margen."""

    assert integration_session is not None
    columns = {column.name for column in LLMModelConfig.__table__.columns}
    assert "markup_pct" in columns
    assert "profit_margin_pct" not in columns


@pytest.mark.asyncio
async def test_credit_balance_uses_twelve_four_numeric(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    del integration_session
    assert settings.credits_per_usd == Decimal("1.00")

    # El tipo se compara contra la clase `Numeric` y no contra el tipo concreto de la
    # columna: comprobar `precision` sobre un `TypeEngine` genérico no la convierte en
    # una `Numeric`, y el `cast` documenta que la aserción depende de esa suposición.
    column_type = cast(Numeric, Organization.__table__.columns["credit_balance"].type)
    assert isinstance(column_type, Numeric)
    assert column_type.precision == 12
    assert column_type.scale == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "use_case",
    CASOS_DE_ESCANEO,
)
async def test_el_de_prioridad_uno_encabeza_cada_cadena(
    integration_session: AsyncSession,
    use_case: LLMUseCaseEnum,
) -> None:
    """Lo que se inyecta al contenedor es, exactamente, el modelo de prioridad 1.

    Se comprueba la **regla** y no un nombre. La regla es la que el Owner quiere en cada
    reconfiguracion —el primario encabeza todas las cadenas, incluido `DEEP_PENTEST`—, y el
    nombre lo decide el catalogo. Comprobar el nombre aqui haria que cambiar de primario
    pareciera un fallo, cuando es una decision legitima; comprobar la regla hace que se
    detecte el fallo de verdad, que es que el primario deje de encabezar.
    """

    assert integration_session is not None

    # El primario se lee del catalogo, no se escribe: asi la prueba sigue diciendo algo
    # verdadero sea cual sea el modelo que este activo.
    activos = (
        (
            await integration_session.execute(
                select(LLMModelConfig).where(LLMModelConfig.is_active.is_(True))
            )
        )
        .scalars()
        .all()
    )
    primarios = [model for model in activos if model.priority_order == 1]
    assert len(primarios) == 1, "tiene que haber exactamente un modelo activo de prioridad 1"
    primario = primarios[0]
    # Transversal, para que incluya en cada cadena. Sin esto, encabezaria solo la suya.
    assert primario.use_case == LLMUseCaseEnum.ALL, (
        "el primario tiene que ser ALL: una fila solo admite un caso de uso y model_id es "
        "unico, asi que 'primario de todas' solo se expresa con ALL, no duplicando la fila"
    )

    chain = await resolve_model_chain(integration_session, use_case)
    assert chain, f"la cadena de {use_case.value} no debe estar vacía"
    assert chain[0].model_id == primario.model_id, use_case.value
    # La cadena sale ordenada por prioridad y solo con modelos activos.
    priorities = [model.priority_order for model in chain]
    assert priorities == sorted(priorities), use_case.value
    assert all(model.is_active for model in chain), use_case.value


@pytest.mark.asyncio
async def test_la_cadena_de_pentest_no_sale_del_unico_modelo_autorizado(
    integration_session: AsyncSession,
) -> None:
    """Las dos cadenas de escaneo se quedan con GLM, y el resto vive en la de código.

    ## Por qué esto cambió y no es una degradación

    Porque `gpt-6-sol` se niega a hacer análisis ofensivos con `provider_code: cyber_policy`, y
    `gpt-6-astra`, `claude-opus-5.5` y `deepseek-v4-pro-0813` estaban declarados como
    `DEEP_PENTEST`, así que la cadena de pentest tenía cuatro eslabones y **ninguno** de los tres
    últimos podía hacer el trabajo. Un fallback que no puede hacer el trabajo no es un fallback.

    La contrapartida es real y hay que decirla: si GLM falla, el escaneo falla. Es la consecuencia
    de la política del Owner, no un olvido, y la alternativa —dejar un modelo que se niega en la
    cadena— es peor: cobra tokens y devuelve un rechazo.
    """

    assert integration_session is not None
    for caso in (LLMUseCaseEnum.DEEP_PENTEST, LLMUseCaseEnum.QUICK_SCAN):
        chain = await resolve_model_chain(integration_session, caso)
        assert [model.model_id for model in chain] == [UNICO_MODELO_DE_PENTEST], caso.value


@pytest.mark.asyncio
async def test_los_modelos_de_codigo_no_se_colan_en_el_pentest(
    integration_session: AsyncSession,
) -> None:
    """Cada modelo de revisión de código está en la cadena de código y **fuera** de la de pentest.

    ## Por qué se afirma el «fuera» y no solo el «dentro»

    Porque el defecto era de `use_case`, no de lista: con `ALL` el modelo estaba en las dos. Una
    prueba que solo afirmara «está en la cadena de código» habría pasado con `ALL` puesto, que es
    exactamente el defecto.
    """

    assert integration_session is not None
    de_codigo = await resolve_model_chain(integration_session, LLMUseCaseEnum.AUTOFIX)
    ids_codigo = {model.model_id for model in de_codigo}

    for modelo in (
        "openai/gpt-6-astra",
        "anthropic/claude-opus-5.5",
        "deepseek/deepseek-v4-pro-0813",
        "anthropic/claude-fable-5.1",
        "openai/gpt-6-sol",
    ):
        assert modelo in ids_codigo, f"{modelo} deberia estar en la cadena de revision de codigo"

    for caso in (LLMUseCaseEnum.DEEP_PENTEST, LLMUseCaseEnum.QUICK_SCAN):
        ids_pentest = {
            model.model_id
            for model in await resolve_model_chain(integration_session, caso)
        }
        # GLM está en las dos cadenas a propósito: es `ALL`, y por eso encabeza el escaneo. La
        # intersección que no puede existir es la de **los** modelos de código con la de pentest.
        assert not (ids_codigo - {UNICO_MODELO_DE_PENTEST}) & ids_pentest, (
            f"un modelo de codigo se ha colado en la cadena de {caso.value}"
        )


# --------------------------------------------------------------------------- #
# Enlace al runner
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_primary_model_reaches_the_container_as_strix_llm(
    integration_session: AsyncSession,
) -> None:
    """El camino completo: cadena resuelta → sandbox → variable de entorno del motor.

    El motor Strix lee `STRIX_LLM` (`strix/llm/config.py` y `strix/interface/main.py`
    lo consultan con `os.getenv`, y `main.py` aborta el arranque si falta). Inyectar
    con otro nombre no daría error: el contenedor caería al `openai/gpt-5` que el motor
    trae por defecto, cada escaneo correría fuera del catálogo y `llm_usage_events`
    nunca se escribiría. Esta prueba ata la resolución con el nombre exacto que lee el
    motor, que es el detalle que hace que todo el bloque funcione.
    """

    assert integration_session is not None
    chain = await resolve_model_chain(integration_session, LLMUseCaseEnum.DEEP_PENTEST)
    manager = StrixSandboxManager(
        "6f1b1f5c-2f4a-4a2f-9a1c-9c2f1a7d5e10",
        "app.example.com",
        "DEEP",
        client=object(),  # type: ignore[arg-type]
        llm_model=chain[0].model_id,
    )
    environment = manager.container_environment()

    # Lo que llega al contenedor es el primario **de la cadena resuelta**, no un nombre
    # escrito aqui. Escribirlo fijaria en el test una decision que es del Owner, y el fallo
    # que hay que cazar es que el contenedor reciba otra cosa, no que el primario cambie.
    assert environment["STRIX_LLM"] == chain[0].model_id
    # `STRIX_LLM_MODEL` no existe para el motor. Si alguien lo añade aquí creyendo que
    # es un alias, el contenedor lo ignorará en silencio y esta aserción no lo detectaría;
    # por eso se comprueba explícitamente que la variable antigua no está.
    assert "STRIX_LLM_MODEL" not in environment


def test_sandbox_injects_the_resolved_model_instead_of_the_default() -> None:
    """El contenedor recibe el modelo resuelto, no el `DEFAULT_STRIX_LLM` fijo."""

    # Un identificador cualquiera, y no el primario del catálogo: esta prueba comprueba que
    # lo resuelto gana a lo configurado, y para eso el valor no importa mientras sea
    # distinto del respaldo.
    resuelto = "proveedor/un-modelo-resuelto-explicitamente"
    manager = StrixSandboxManager(
        "6f1b1f5c-2f4a-4a2f-9a1c-9c2f1a7d5e10",
        "app.example.com",
        "DEEP",
        client=object(),  # type: ignore[arg-type]
        llm_model=resuelto,
    )
    environment = manager.container_environment()

    assert environment["STRIX_LLM"] == resuelto
    assert environment["STRIX_LLM"] != settings.default_strix_llm
    assert environment["STRIX_NON_INTERACTIVE"] == "1"


def test_sandbox_keeps_the_default_when_no_model_is_resolved() -> None:
    manager = StrixSandboxManager(
        "6f1b1f5c-2f4a-4a2f-9a1c-9c2f1a7d5e10",
        "app.example.com",
        "STANDARD",
        client=object(),  # type: ignore[arg-type]
    )
    environment = manager.container_environment()
    assert environment["STRIX_LLM"] == settings.default_strix_llm


def test_sandbox_rejects_an_empty_model_slug() -> None:
    with pytest.raises(ValueError):
        StrixSandboxManager(
            "6f1b1f5c-2f4a-4a2f-9a1c-9c2f1a7d5e10",
            "app.example.com",
            "STANDARD",
            client=object(),  # type: ignore[arg-type]
            llm_model="   ",
        )


# --------------------------------------------------------------------------- #
# Selección de modelos y fallback
# --------------------------------------------------------------------------- #


def test_select_runtime_models_returns_the_ordered_chain() -> None:
    primary = LLMModelConfig(
        model_id="a/model",
        display_name="A",
        base_cost_input_m=Decimal("1"),
        base_cost_output_m=Decimal("2"),
        markup_pct=Decimal("100"),
        priority_order=1,
        is_active=True,
        use_case=LLMUseCaseEnum.ALL,
    )
    fallback = LLMModelConfig(
        model_id="b/model",
        display_name="B",
        base_cost_input_m=Decimal("1"),
        base_cost_output_m=Decimal("2"),
        markup_pct=Decimal("100"),
        priority_order=2,
        is_active=True,
        use_case=LLMUseCaseEnum.ALL,
    )
    chain = select_runtime_models([primary, fallback])
    assert chain == ["a/model", "b/model"]


def test_select_runtime_models_ignores_inactive_and_dedupes() -> None:
    models = [
        LLMModelConfig(
            model_id="a/model",
            display_name="A",
            base_cost_input_m=Decimal("1"),
            base_cost_output_m=Decimal("2"),
            markup_pct=Decimal("100"),
            priority_order=1,
            is_active=False,
            use_case=LLMUseCaseEnum.ALL,
        ),
        LLMModelConfig(
            model_id="b/model",
            display_name="B",
            base_cost_input_m=Decimal("1"),
            base_cost_output_m=Decimal("2"),
            markup_pct=Decimal("100"),
            priority_order=2,
            is_active=True,
            use_case=LLMUseCaseEnum.ALL,
        ),
        LLMModelConfig(
            model_id="b/model",
            display_name="B duplicado",
            base_cost_input_m=Decimal("1"),
            base_cost_output_m=Decimal("2"),
            markup_pct=Decimal("100"),
            priority_order=3,
            is_active=True,
            use_case=LLMUseCaseEnum.ALL,
        ),
    ]
    assert select_runtime_models(models) == ["b/model"]


# --------------------------------------------------------------------------- #
# Telemetría de tokens
# --------------------------------------------------------------------------- #


def test_extract_token_usage_reads_a_usage_block() -> None:
    usage = extract_token_usage(
        """
        {"status": "completed", "scan_id": "s-1", "findings": [],
         "usage": {"prompt_tokens": 120000, "completion_tokens": 34000}}
        """
    )
    assert usage is not None
    assert usage.prompt_tokens == 120000
    assert usage.completion_tokens == 34000


def test_extract_token_usage_accepts_alternative_key_names() -> None:
    usage = extract_token_usage(
        '{"status":"completed","scan_id":"s-1","findings":[],'
        '"token_usage":{"input_tokens":50,"output_tokens":10}}'
    )
    assert usage is not None
    assert usage.prompt_tokens == 50
    assert usage.completion_tokens == 10


def test_extract_token_usage_returns_none_when_absent_or_invalid() -> None:
    assert extract_token_usage('{"status":"completed","scan_id":"s","findings":[]}') is None
    assert extract_token_usage("no es json") is None
    assert (
        extract_token_usage(
            '{"status":"completed","scan_id":"s","findings":[],'
            '"usage":{"prompt_tokens":-5,"completion_tokens":10}}'
        )
        is None
    )


def test_extract_token_usage_sums_a_list_of_usage_blocks() -> None:
    usage = extract_token_usage(
        '{"status":"completed","scan_id":"s","findings":[],'
        '"usage":[{"prompt_tokens":10,"completion_tokens":1},'
        '{"prompt_tokens":20,"completion_tokens":2}]}'
    )
    assert usage is not None
    assert usage.prompt_tokens == 30
    assert usage.completion_tokens == 3


# --------------------------------------------------------------------------- #
# Tarificación y deducción tras el run
# --------------------------------------------------------------------------- #


async def _catalog_model(session: AsyncSession, model_id: str) -> LLMModelConfig:
    """Carga un modelo del catálogo sembrado por la migración.

    Las pruebas de telemetría no crean modelos nuevos: el runner solo puede tarificar
    contra modelos que existen en el catálogo, así que usar filas propias no probaría
    el camino real y además rompería el `model_id` único.
    """

    model = (
        await session.execute(select(LLMModelConfig).where(LLMModelConfig.model_id == model_id))
    ).scalar_one()
    return model


async def _funded_tenant(session: AsyncSession, balance: str) -> Organization:
    """Crea una organización cuyo saldo viene del ledger, nunca de la columna.

    Escribir `credit_balance` a mano dejaría el saldo denormalizado sin asiento que lo
    respalde, y `credit_balance_of` —que suma el ledger— no coincidiría con la columna.
    En producción el saldo de partida es siempre un bono de alta.
    """

    suffix = uuid.uuid4().hex
    organization = Organization(name=f"Telemetría {suffix}", slug=f"tel-{suffix}")
    session.add(organization)
    await session.flush()
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
    return organization


@pytest.mark.asyncio
async def test_telemetry_charges_the_ledger_and_records_usage(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization = await _funded_tenant(integration_session, "1000")
    model = await _catalog_model(integration_session, "deepseek/deepseek-v4-pro-0813")
    before = await credit_balance_of(integration_session, organization.id)

    telemetry = LlmUsageTelemetry(
        model=model,
        use_case=LLMUseCaseEnum.DEEP_PENTEST,
        prompt_tokens=1_000_000,
        completion_tokens=0,
    )
    charged = await telemetry.charge(
        session=integration_session,
        organization_id=organization.id,
        run_id=None,
        credits_per_usd=settings.credits_per_usd,
    )

    # 1,20 USD de coste base con 200 % de recargo = 3,60 USD al cliente, que a
    # `credits_per_usd = 1` son 3,60 créditos. El margen son los 2,40 de diferencia.
    assert charged is not None
    assert charged.client_cost_credits == Decimal("3.6000")
    assert await credit_balance_of(integration_session, organization.id) == before - Decimal("3.6")

    entry = (
        await integration_session.execute(
            select(CreditLedger)
            .where(CreditLedger.organization_id == organization.id)
            .where(CreditLedger.reason == LedgerReasonEnum.ADMIN_ADJUSTMENT)
        )
    ).scalars().all()
    assert len(entry) == 1
    assert entry[0].amount_delta == -Decimal("3.6000")

    usage_events = (
        await integration_session.execute(
            select(LLMUsageEvent).where(LLMUsageEvent.model_config_id == model.id)
        )
    ).scalars().all()
    assert len(usage_events) == 1
    assert usage_events[0].prompt_tokens == 1_000_000
    assert usage_events[0].base_cost_usd == Decimal("1.20")
    assert usage_events[0].net_profit_usd == Decimal("2.40")


@pytest.mark.asyncio
async def test_telemetry_charges_nothing_without_token_usage(
    integration_session: AsyncSession,
) -> None:
    """Sin datos de tokens no hay cargo: inventar un coste sería tarificar al aire."""

    assert integration_session is not None
    organization = await _funded_tenant(integration_session, "100")
    model = await _catalog_model(integration_session, "deepseek/deepseek-v4.1-flash")

    telemetry = LlmUsageTelemetry(
        model=model,
        use_case=LLMUseCaseEnum.QUICK_SCAN,
        prompt_tokens=None,
        completion_tokens=None,
    )
    charged = await telemetry.charge(
        session=integration_session,
        organization_id=organization.id,
        run_id=None,
        credits_per_usd=settings.credits_per_usd,
    )

    assert charged is None
    assert await credit_balance_of(integration_session, organization.id) == Decimal("100")
    usage_events = (
        await integration_session.execute(
            select(LLMUsageEvent).where(LLMUsageEvent.model_config_id == model.id)
        )
    ).scalars().all()
    assert usage_events == []


@pytest.mark.asyncio
async def test_telemetry_refunds_the_overcharged_reserve(
    integration_session: AsyncSession,
) -> None:
    """El escaneo se cobra por adelantado; la telemetría compensa la diferencia.

    El tenant debe acabar pagando exactamente el consumo real: ni el doble por
    cobrar consumo y reserva, ni de menos por no ajustar lo que se cobró de más.
    """

    assert integration_session is not None
    organization = await _funded_tenant(integration_session, "100")
    model = await _catalog_model(integration_session, "deepseek/deepseek-v4-pro-0813")
    reserved = Decimal("10")
    starting = await credit_balance_of(integration_session, organization.id)

    # El router descuenta la reserva antes de encolar el escaneo.
    await apply_credit_delta(
        session=integration_session,
        organization_id=organization.id,
        amount=-reserved,
        reason=LedgerReasonEnum.SCAN_CONSUMPTION,
        reference_id="run-referencia",
    )
    await integration_session.commit()

    charged = await LlmUsageTelemetry(
        model=model,
        use_case=LLMUseCaseEnum.DEEP_PENTEST,
        prompt_tokens=1_000_000,
        completion_tokens=0,
    ).charge(
        session=integration_session,
        organization_id=organization.id,
        run_id=None,
        credits_per_usd=settings.credits_per_usd,
        reserved_credits=reserved,
    )

    # 1,20 USD de coste con 200 % de recargo = 3,60 USD = 3,60 créditos. La reserva
    # de 10 sobraba, así que se devuelven 6,40 y el saldo final es 100 - 3,60.
    assert charged is not None
    assert charged.client_cost_credits == Decimal("3.6000")
    assert await credit_balance_of(integration_session, organization.id) == (
        starting - Decimal("3.60")
    )


@pytest.mark.asyncio
async def test_telemetry_collects_consumption_above_the_reserve(
    integration_session: AsyncSession,
) -> None:
    """Si el motor gastó más de lo reservado, la diferencia se cobra.

    El caso inverso al reembolso: sin esto, un escaneo que consuma más de lo
    reservado sería un unchecked gratis para la plataforma.
    """

    assert integration_session is not None
    organization = await _funded_tenant(integration_session, "100")
    model = await _catalog_model(integration_session, "deepseek/deepseek-v4-pro-0813")
    reserved = Decimal("1")
    starting = await credit_balance_of(integration_session, organization.id)

    await apply_credit_delta(
        session=integration_session,
        organization_id=organization.id,
        amount=-reserved,
        reason=LedgerReasonEnum.SCAN_CONSUMPTION,
        reference_id="run-sobreconsumo",
    )
    await integration_session.commit()

    charged = await LlmUsageTelemetry(
        model=model,
        use_case=LLMUseCaseEnum.DEEP_PENTEST,
        prompt_tokens=1_000_000,
        completion_tokens=0,
    ).charge(
        session=integration_session,
        organization_id=organization.id,
        run_id=None,
        credits_per_usd=settings.credits_per_usd,
        reserved_credits=reserved,
    )

    assert charged is not None
    assert charged.client_cost_credits == Decimal("3.6000")
    # Se cobran 3,60 en total: 1 de reserva más 2,60 de exceso.
    assert await credit_balance_of(integration_session, organization.id) == (
        starting - Decimal("3.60")
    )


@pytest.mark.asyncio
async def test_telemetry_writes_no_ledger_entry_when_the_charge_is_exact(
    integration_session: AsyncSession,
) -> None:
    """Si la reserva coincide con el consumo, el ledger no ensucia con un asiento de cero."""

    assert integration_session is not None
    organization = await _funded_tenant(integration_session, "100")
    model = await _catalog_model(integration_session, "deepseek/deepseek-v4-pro-0813")
    exact = Decimal("3.60")

    charged = await LlmUsageTelemetry(
        model=model,
        use_case=LLMUseCaseEnum.DEEP_PENTEST,
        prompt_tokens=1_000_000,
        completion_tokens=0,
    ).charge(
        session=integration_session,
        organization_id=organization.id,
        run_id=None,
        credits_per_usd=settings.credits_per_usd,
        reserved_credits=exact,
    )

    assert charged is not None
    entries = (
        await integration_session.execute(
            select(CreditLedger).where(
                CreditLedger.organization_id == organization.id
            )
        )
    ).scalars().all()
    # Solo el bono de alta: ni el asiento del consumo ni el del ajuste.
    assert [entry.reason for entry in entries] == [LedgerReasonEnum.SIGNUP_BONUS]
    # El evento de uso sí se registra aunque no haya asiento: el margen es auditable
    # aunque el movimiento de créditos sea cero.
    events = (
        await integration_session.execute(
            select(LLMUsageEvent).where(LLMUsageEvent.model_config_id == model.id)
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].net_profit_usd == Decimal("2.40")


def test_compute_charge_matches_the_documented_markup_for_the_seed() -> None:
    """El catálogo sembrado y la calculadora cuentan la misma historia."""

    for model_id, cost_in, _cost_out, markup, _priority, _use_case in EXPECTED_CATALOG:
        charge = compute_charge(
            base_cost_input_m=Decimal(cost_in),
            base_cost_output_m=Decimal("0"),
            markup_pct=Decimal(markup),
            prompt_tokens=1_000_000,
            completion_tokens=0,
            credits_per_usd=Decimal("1.00"),
        )
        assert charge.markup_multiplier == Decimal(1) + Decimal(markup) / Decimal(100), model_id
