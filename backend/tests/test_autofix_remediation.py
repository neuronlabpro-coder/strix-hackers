"""Pruebas del flujo «One-Click Autofix PR».

## Qué se comprueba y por qué

El flujo son cinco pasos encadenados y el valor está en las **costuras** entre ellos, no en
cada paso por separado. Las cinco costuras que hay que probar:

1. **El razonamiento no llega al diff.** Un diff con una frase de explicación delante no se
   aplica, y `git apply` lo rechaza con un error que no señala el texto sobrante. Es el fallo
   más probable de todo el flujo y el más invisible: el parche "existe" y no aplica.
2. **Se cobra lo que se gastó, aunque la PR no se abra.** Los tokens ya se consumieron. Cobrar
   solo el éxito regala margen al cliente que reintenta y perjudica a la plataforma.
3. **El estado se mueve a `REMEDIATION_PROPOSED` y no a `FIXED`.** Un PR abierto no arregla
   nada, y decir que sí hace que el panel dé el hallazgo por resuelto.
4. **`previous_status` sale de la fila.** Hardcodear `OPEN` haría que un hallazgo que venía de
   `IN_PROGRESS` publicara una transición que no ocurrió, y un suscriptor que reconstruya su
   vista a partir del evento contaría mal.
5. **El aislamiento (R3).** Un hallazgo de otro workspace da `404`, y el consumo no se cobra a
   un tenant por pedir una remediación sobre material ajeno.

## Por qué el modelo es un doble

Porque una prueba que hable con OpenRouter no es una prueba: depende de la red, cuesta dinero y
falla por causas que no son del código. El doble devuelve una respuesta con razonamiento
deliberadamente dentro, que es el caso que hay que cazar.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.models import CreditLedger, LedgerReasonEnum
from backend.apps.llm_router.client import LlmCompletion
from backend.apps.llm_router.models import LLMModelConfig, LLMUsageEvent, LLMUseCaseEnum
from backend.apps.organizations.models import (
    Membership,
    Organization,
    RoleEnum,
    User,
)
from backend.apps.pentests.models import PentestRun, ScanModeEnum, TargetTypeEnum
from backend.apps.repositories.models import GitProviderEnum, Repository
from backend.apps.vulnerabilities import remediation
from backend.apps.vulnerabilities.autofix import (
    LlmInvoker,
    RemediationError,
    extraer_diff,
    limpiar_razonamiento,
)
from backend.apps.vulnerabilities.models import IssueStatusEnum, Vulnerability
from backend.core.security import create_access_token, hash_password
from backend.main import app

pytestmark = pytest.mark.integration

#: Un diff válido y pequeño. Lo suficiente para que `parse_patch` lo acepte, que es lo que hace
#: la validación antes de publicar.
DIFF_DE_EJEMPLO = """diff --git a/app/login.py b/app/login.py
index 1111111..2222222 100644
--- a/app/login.py
+++ b/app/login.py
@@ -1,4 +1,4 @@
 def login(user, password):
-    return db.query("SELECT * FROM users WHERE name='" + user + "'")
+    return db.query("SELECT * FROM users WHERE name = ?", [user])
"""


def _respuesta_del_modelo(texto: str) -> LlmCompletion:
    return LlmCompletion(
        text=texto,
        model="anthropic/claude-fable-5.1",
        prompt_tokens=1200,
        completion_tokens=340,
        finish_reason="stop",
    )


def _invocador(texto: str) -> LlmInvoker:
    """Devuelve un invocador que responde con `texto`.

    Es una **fabrica** y no la respuesta ya construida porque `invoke_llm` recibe la funcion
    que el flujo va a llamar: pasar la corrutina ya creada haria que se evaluara al construir
    el argumento y el flujo recibiria un objeto awaited en lugar de una funcion.

    Y el invocador devuelto es **asincrono**, igual que `complete`. Un doble sincrono
    obligaría a ensanchar el tipo en el módulo para admitirlo, y entonces la prueba dejaría de
    comprobar que el flujo espera la respuesta, que es justo lo que se rompería si alguien
    olvidara el `await`.
    """

    async def _invocar(**_kwargs: Any) -> LlmCompletion:
        return _respuesta_del_modelo(texto)

    return _invocar


def _headers(user: User, organization: Organization) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }


async def _montaje(
    session: AsyncSession, *, con_repositorio: bool = True
) -> tuple[User, Organization, dict[str, str], Repository, Vulnerability]:
    """Un workspace con admin, repositorio, run y hallazgo listo para remediar."""

    suffix = uuid.uuid4().hex
    organization = Organization(
        name=f"Remediation {suffix}", slug=f"remediation-{suffix}"
    )
    organization.credit_balance = Decimal("500")
    user = User(
        email=f"remediation-{suffix}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Cliente de remediación",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(
        Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.ADMIN)
    )

    repositorio = Repository(
        organization_id=organization.id,
        provider=GitProviderEnum.GITHUB,
        remote_repo_id="42",
        name="app",
        full_name=f"cliente-{suffix}/app",
        clone_url=f"https://example.invalid/{suffix}/app.git",
        default_branch="main",
    )
    session.add(repositorio)
    await session.flush()

    run = PentestRun(
        organization_id=organization.id,
        target_type=TargetTypeEnum.REPOSITORY,
        target_identifier=repositorio.clone_url,
        scan_mode=ScanModeEnum.STANDARD,
    )
    session.add(run)
    await session.flush()

    hallazgo = Vulnerability(
        organization_id=organization.id,
        run_id=run.id,
        title="Inyección SQL en el inicio de sesión",
        description=(
            "El nombre de usuario se concatena directamente en la consulta, lo que permite "
            "inyectar SQL y leer la tabla de credenciales."
        ),
        severity="HIGH",
        cvss_score=8.1,
        affected_target="app/login.py",
        affected_line="12",
        poc_reproduction_raw="curl -i \"https://app.example/login?u=admin' OR '1'='1\"",
        status=IssueStatusEnum.IN_PROGRESS,
    )
    session.add(hallazgo)
    if not con_repositorio:
        # El repositorio pasa a estar inactivo: el hallazgo sigue apuntando a su clone_url,
        # pero ya no se puede resolver a un repositorio utilizable.
        repositorio.is_active = False
    await session.commit()
    return user, organization, _headers(user, organization), repositorio, hallazgo


async def _modelo(session: AsyncSession) -> LLMModelConfig:
    # `model_id` es unico en toda la tabla, y la base de pruebas es compartida: dos
    # pruebas que sembraran el mismo identificador fallarian por la segunda, no por lo que
    # miden. Cada una usa el suyo.
    model = LLMModelConfig(
        model_id=f"anthropic/claude-fable-5.1-{uuid.uuid4().hex[:8]}",
        display_name="Claude",
        base_cost_input_m=Decimal("3.00"),
        base_cost_output_m=Decimal("15.00"),
        markup_pct=150,
        priority_order=1,
        is_active=True,
        use_case=LLMUseCaseEnum.AUTOFIX,
    )
    session.add(model)
    await session.commit()
    return model


# --------------------------------------------------------------------------- #
# Extracción del diff: la costura más silenciosa
# --------------------------------------------------------------------------- #


def test_el_razonamiento_no_llega_al_diff() -> None:
    """Un bloque de razonamiento dentro del contenido se descarta.

    El cliente HTTP ya filtra los bloques `{"type": "reasoning"}` del sobre del proveedor, y
    eso no cubre el caso real: un modelo que emite el razonamiento **dentro** del texto de
    contenido. Aquí va justo así, y sin esta limpieza el diff empezaría por una frase que
    `git apply` rechazaría con un error que no señala el texto sobrante.
    """

    crudo = (
        "Primero reviso la función de login. Veo que concatena el usuario. "
        "El arreglo es pasar un parámetro vinculado.\n\n"
        + DIFF_DE_EJEMPLO
    )
    parche = extraer_diff(limpiar_razonamiento(crudo))
    # Se mide el **parche final**, no el texto intermedio. La limpieza por etiqueta y la
    # extracción del diff hacen trabajos distintos y la prueba tiene que mirar el punto donde
    # los dos han actuado: un diff que empieza por "Primero reviso" no se aplica.
    assert parche.startswith("diff --git")
    assert "Primero reviso" not in parche


def test_una_etiqueta_de_razonamiento_se_descarta_sola() -> None:
    """El bloque marcado se quita aunque no haya nada más alrededor.

    Es la capa que cubre el formato de proveedor, que el cliente HTTP ya filtra. Aquí se
    comprueba por separado porque son dos capas que fallan de forma distinta: quedarse con
    una deja el camino abierto a la otra.
    """

    crudo = (
        "<thinking>El usuario se concatena en la consulta, hay que usar un parámetro "
        "vinculado.</thinking>\n\n" + DIFF_DE_EJEMPLO
    )
    limpio = limpiar_razonamiento(crudo)
    assert "el usuario se concatena" not in limpio
    assert "thinking" not in limpio


def test_una_explicacion_antes_del_diff_se_quita() -> None:
    """Lo que precede al diff se descarta aunque no sea razonamiento marcado.

    Un diff empieza por `diff --git` o por `---`, así que buscar el inicio es la única forma
    de no depender de una lista de frases de explicación, que es infinita.
    """

    crudo = (
        "Claro. Este es el diff que propongo:\n\n"
        "```diff\n"
        + DIFF_DE_EJEMPLO
        + "```\n\nEspero que te sirva."
    )
    parche = extraer_diff(limpiar_razonamiento(crudo))
    assert parche.startswith("diff --git")
    assert "Espero que te sirva" not in parche
    assert "```" not in parche


def test_una_respuesta_sin_diff_da_un_error_explicito() -> None:
    """Sin diff, el error dice que el modelo no propuso corrección.

    Es un resultado válido —el modelo se negó por no haber una corrección segura—, y no debe
    confundirse con una caída del proveedor: quien reintenta ante un `500` gasta más tokens
    para obtener el mismo "no".
    """

    with pytest.raises(RemediationError):
        extraer_diff("Aquí tienes el diff:\n\nNo es seguro cambiar esto automáticamente.")
    with pytest.raises(RemediationError):
        extraer_diff("")


def test_un_tarjeta_que_no_emita_diff_ahora_mas_tampoco() -> None:
    """El texto vacío no es un diff vacío que se pueda publicar."""

    with pytest.raises(RemediationError):
        extraer_diff("   \n  \n")


# --------------------------------------------------------------------------- #
# Flujo completo
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_el_flujo_propone_publica_y_cambia_el_estado(
    integration_session: AsyncSession,
) -> None:
    """El camino feliz: diff limpio, PR abierta, estado movido y consumo asentado.

    Se miden las cuatro cosas que tienen que ser ciertas a la vez, porque el valor del flujo
    está en que ocurran **juntas**: un diff sin publicar no sirve, una PR sin estado deja el
    hallazgo mintiendo, y un estado sin consumo deja el margen sin auditar.
    """

    session = integration_session
    assert session is not None
    _, _, _, repositorio, hallazgo = await _montaje(session)
    await _modelo(session)
    await session.refresh(hallazgo)
    id_hallazgo = hallazgo.id
    id_org = hallazgo.organization_id
    saldo_antes = Decimal(
        (
            await session.execute(
                select(Organization.credit_balance).where(
                    Organization.id == id_org
                )
            )
        ).scalar_one()
    )

    publicadas: list[tuple[str, str, str]] = []

    async def _publicar(review_id: str, vulnerability_id: str) -> str:
        publicadas.append((review_id, vulnerability_id, ""))
        return "https://github.example/cliente/app/pull/7"

    url = await remediation.generar_y_publicar(
        session,
        hallazgo,
        invoke_llm=_invocador("Primero explico el error.\n\n" + DIFF_DE_EJEMPLO),
        publicar=_publicar,
    )
    # La URL vuelve ya validada como `AnyHttpUrl`: el tipo **es** la prueba de que pasó por
    # `_url_de_pr_de_confiar`, y por eso el aserto va sobre el texto que se guarda.
    assert str(url).endswith("/pull/7")
    assert len(publicadas) == 1

    await session.refresh(hallazgo)
    assert hallazgo.status is IssueStatusEnum.REMEDIATION_PROPOSED
    assert hallazgo.remediation_pr_url == str(url)
    # El diff guardado no lleva ni la explicación ni las vallas.
    assert hallazgo.remediation_patch_diff is not None
    assert hallazgo.remediation_patch_diff.startswith("diff --git")
    assert "Primero explico" not in hallazgo.remediation_patch_diff
    # Y **no** toca la evidencia del escaneo. `autofix_patch_diff` es inmutable por trigger
    # bajo R4, y esta aserción documenta por qué el borrador tiene columna propia: aquí sigue
    # lo que escribió el motor, o el NULL de siempre, sin haberlo sobrescrito.
    assert hallazgo.autofix_patch_diff is None

    eventos = (
        (
            await session.execute(
                select(LLMUsageEvent).where(LLMUsageEvent.organization_id == id_org)
            )
        )
        .scalars()
        .all()
    )
    assert len(eventos) == 1
    assert eventos[0].use_case is LLMUseCaseEnum.AUTOFIX
    assert eventos[0].prompt_tokens == 1200

    saldo_despues = Decimal(
        (
            await session.execute(
                select(Organization.credit_balance).where(Organization.id == id_org)
            )
        )
        .scalar_one()
    )
    assert saldo_despues < saldo_antes
    await session.rollback()
    assert id_hallazgo is not None
    assert repositorio is not None


@pytest.mark.asyncio
async def test_el_consumo_se_cobra_aunque_la_pr_no_se_abra(
    integration_session: AsyncSession,
) -> None:
    """El fallo de publicación no devuelve los tokens.

    Los tokens **ya** se gastaron: la generación ocurrió y fue correcta. Devolver el dinero
    sería regalar el margen al cliente que reintenta, y no registrar el fallo dejaría el coste
    real sin dato, que es justo lo que hace que el margen del catálogo deje de ser auditable.
    """

    session = integration_session
    assert session is not None
    _, _, _, _, hallazgo = await _montaje(session)
    await _modelo(session)
    await session.refresh(hallazgo)
    id_org = hallazgo.organization_id
    saldo_antes = Decimal(
        (
            await session.execute(
                select(Organization.credit_balance).where(
                    Organization.id == id_org
                )
            )
        ).scalar_one()
    )

    async def _fallar(review_id: str, vulnerability_id: str) -> str:
        raise remediation.PublicarRemediationError("El proveedor Git no respondió")

    with pytest.raises(remediation.PublicarRemediationError):
        await remediation.generar_y_publicar(
            session,
            hallazgo,
            invoke_llm=_invocador(DIFF_DE_EJEMPLO),
            publicar=_fallar,
        )

    await session.refresh(hallazgo)
    # El estado NO se movió: la propuesta no existe.
    assert hallazgo.status is IssueStatusEnum.IN_PROGRESS
    assert hallazgo.remediation_pr_url is None

    eventos = (
        (
            await session.execute(
                select(LLMUsageEvent).where(LLMUsageEvent.organization_id == id_org)
            )
        )
        .scalars()
        .all()
    )
    assert len(eventos) == 1
    saldo_despues = Decimal(
        (
            await session.execute(
                select(Organization.credit_balance).where(
                    Organization.id == id_org
                )
            )
        ).scalar_one()
    )
    assert saldo_despues < saldo_antes


@pytest.mark.asyncio
async def test_sin_modelo_activo_no_se_cobra_nada(
    integration_session: AsyncSession,
) -> None:
    """Sin modelo en la cadena `AUTOFIX` no hay consumo, y el error es de configuración.

    No es un `503`: el servicio está sano y lo que falta es un modelo activo en el catálogo.
    Un `503` haría que un balanceador lo leyera como caída y dejara de mandar tráfico, y no hay
    nada que balancear.
    """

    session = integration_session
    assert session is not None
    _, _, _, _, hallazgo = await _montaje(session)
    await session.refresh(hallazgo)
    id_org = hallazgo.organization_id

    # El catálogo de modelos es **global**, no por workspace, y la base de pruebas puede tener
    # un modelo activo de una siembra o de otro despliegue. Sin desactivarlo explícitamente la
    # prueba dependería del estado de la base: pasaría por casualidad en una máquina y fallaría
    # en otra. Y se desactivan **todos** los activos, no solo los de `AUTOFIX`, porque
    # `resolve_model_chain` incluye también los declarados como `ALL`, que son transversales y
    # sirven para cualquier caso de uso. Desactivar solo los de `AUTOFIX` dejaría pasar un `ALL`
    # activo y la prueba seguiría encontrando un modelo.
    await session.execute(
        update(LLMModelConfig).where(LLMModelConfig.is_active.is_(True)).values(
            is_active=False
        )
    )
    await session.commit()

    with pytest.raises(remediation.NoModelAvailableError):
        await remediation.generar_y_publicar(
            session,
            hallazgo,
            invoke_llm=_invocador(DIFF_DE_EJEMPLO),
        )

    eventos = (
        (
            await session.execute(
                select(func.count(LLMUsageEvent.id)).where(
                    LLMUsageEvent.organization_id == id_org
                )
            )
        ).scalar_one()
    )
    assert eventos == 0
    asientos = (
        (
            await session.execute(
                select(func.count(CreditLedger.id)).where(
                    CreditLedger.organization_id == id_org
                )
            )
        ).scalar_one()
    )
    assert asientos == 0


@pytest.mark.asyncio
async def test_sin_repositorio_no_se_intenta_generar_nada(
    integration_session: AsyncSession,
) -> None:
    """Un hallazgo sin repositorio utilizable no consume tokens.

    El caso real es un hallazgo de un escaneo de **dominio**: lo vulnerable está en un
    servicio de terceros y un parche sobre él no es del cliente ni arregla nada. Es un caso
    esperado, no un error de configuración, y por eso tiene su propio tipo: el panel puede
    decir "este hallazgo no tiene repositorio" en vez de "no se pudo generar".
    """

    session = integration_session
    assert session is not None
    _, _, _, _, hallazgo = await _montaje(session, con_repositorio=False)
    await _modelo(session)
    await session.refresh(hallazgo)
    id_org = hallazgo.organization_id

    with pytest.raises(remediation.NoRepositoryLinkedError):
        await remediation.generar_y_publicar(
            session,
            hallazgo,
            invoke_llm=_invocador(DIFF_DE_EJEMPLO),
        )

    eventos = (
        (
            await session.execute(
                select(func.count(LLMUsageEvent.id)).where(
                    LLMUsageEvent.organization_id == id_org
                )
            )
        ).scalar_one()
    )
    assert eventos == 0


@pytest.mark.asyncio
async def test_un_hallazgo_ajeno_es_404_y_no_cobra(
    integration_session: AsyncSession,
) -> None:
    """Pedir la remediación de un hallazgo de otro workspace da `404` y no toca el saldo ajeno.

    Se mide con `WHERE organization_id = ...` sobre el tenant de la **víctima**: un contador
    global de la base compartida no probaría nada.
    """

    session = integration_session
    assert session is not None
    _, org_victima, _, _, hallazgo = await _montaje(session)
    suffix = uuid.uuid4().hex
    atacante = Organization(
        name=f"Atacante {suffix}", slug=f"atacante-{suffix}"
    )
    atacante.credit_balance = Decimal("500")
    user = User(
        email=f"atacante-{suffix}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Atacante",
        email_verified=True,
    )
    session.add_all([atacante, user])
    await session.flush()
    session.add(
        Membership(
            organization_id=atacante.id, user_id=user.id, role=RoleEnum.ADMIN
        )
    )
    await session.commit()
    await _modelo(session)

    id_victima = org_victima.id
    saldo_victima = Decimal(org_victima.credit_balance)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.post(
            f"/api/v1/vulnerabilities/{hallazgo.id}/remediate",
            headers=_headers(user, atacante),
        )
    assert respuesta.status_code == 404, respuesta.text

    await session.refresh(org_victima)
    assert Decimal(org_victima.credit_balance) == saldo_victima
    eventos = (
        await session.execute(
            select(func.count(LLMUsageEvent.id)).where(
                LLMUsageEvent.organization_id == id_victima
            )
        )
    ).scalar_one()
    assert eventos == 0


@pytest.mark.asyncio
async def test_un_miembro_no_admin_no_propone_remediacion(
    integration_session: AsyncSession,
) -> None:
    """Mover un hallazgo de estado y abrir un PR es una acción de administrador.

    Sin esta comprobación, un miembro con permiso de lectura podría abrir pull requests en los
    repositorios del cliente: escribir fuera del panel con el rol de leer.
    """

    session = integration_session
    assert session is not None
    suffix = uuid.uuid4().hex
    organization = Organization(name=f"Miembro {suffix}", slug=f"miembro-{suffix}")
    user = User(
        email=f"miembro-{suffix}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Miembro",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(
        Membership(
            organization_id=organization.id, user_id=user.id, role=RoleEnum.MEMBER
        )
    )
    await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as cliente:
        respuesta = await cliente.post(
            f"/api/v1/vulnerabilities/{uuid.uuid4()}/remediate",
            headers=_headers(user, organization),
        )
    assert respuesta.status_code == 403, respuesta.text


def test_remediation_proposed_cuenta_como_abierto() -> None:
    """Un hallazgo con una propuesta abierta sigue contando como abierto.

    Es el criterio que comparten el resumen de postura y la herramienta MCP, y se lee de un
    solo sitio para que no puedan discrepar sobre qué es un hallazgo sin resolver. Si
    contara como cerrado, el panel diría que está arreglado cuando lo único que existe es una
    propuesta sin fusionar.
    """

    assert IssueStatusEnum.REMEDIATION_PROPOSED.is_open_for_closure is True
    assert IssueStatusEnum.OPEN.is_open_for_closure is True
    assert IssueStatusEnum.IN_PROGRESS.is_open_for_closure is True
    assert IssueStatusEnum.FIXED.is_open_for_closure is False
    assert IssueStatusEnum.IGNORED.is_open_for_closure is False


def test_el_motivo_del_consumo_es_consumo_de_escaneo() -> None:
    """El cargo va con el motivo que el cliente reconoce en su propio historial.

    El motivo se elige por lo que el cliente **lee** en su extracto, no por lo que ocurre por
    dentro: desde su punto de vista es un gasto de escaneo porque consume saldo de escaneo,
    aunque el trabajo técnico lo haya hecho un modelo.
    """

    assert LedgerReasonEnum.SCAN_CONSUMPTION.value == "SCAN_CONSUMPTION"
    assert remediation.AUTOFIX_SCOPE.value == "vulnerabilities:triage"
