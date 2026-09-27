"""Orquestación del flujo «One-Click Autofix PR».

## Por qué el flujo entero vive en un módulo y no repartido en el router

Porque son cinco pasos que **dependen unos de otros** y cuyo fallo hay que distinguir: generar
el diff, cobrar el consumo, publicar la rama, guardar la URL y mover el estado. Repartidos, el
escenario interesante —"se publicó la rama pero falló al guardar la URL"— no se puede
expresar, y ese escenario es el que deja una PR abierta en GitHub de la que nadie sabe nada.

## Por qué el estado se mueve **después** de publicar y nunca antes

Porque el estado afirma que hay una propuesta. Si se moviera antes y la publicación fallara,
el panel diría "remediación propuesta" sin que exista nada, y el hallazgo aparecería
atendido. Después de publicar, el peor caso es el contrario y es recuperable: la PR existe y
el hallazgo sigue abierto, lo que se ve como una propuesta que aún no se ha registrado.

## Por qué se guarda la URL aunque la publicación la devuelve

Porque la publicación ocurre en un cliente Git externo y la escritura en nuestra base puede
fallar después. Sin la URL guardada, esa PR existe en GitHub y para el panel no existe nada.
Se guarda primero y se propaga el estado después, que es el orden que deja el menor número de
estados intermedios sin registrar.

## Por qué se devuelve la URL y no el diff

Por R4. El diff es evidencia y se lee desde la vulnerabilidad; devolverlo en la respuesta de
la acción lo expondría en cualquier log de intermediary, y no aporta nada que el panel no
pueda leer del sitio al que lo leyó.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.api_access.scopes import Scope
from backend.apps.llm_router.client import LlmUpstreamError, complete
from backend.apps.repositories.autofix import (
    AutofixError,
    create_autofix_branch_and_pr,
    validate_autofix_patch,
)
from backend.apps.repositories.models import PullRequestReview
from backend.apps.vulnerabilities import autofix as generador
from backend.apps.vulnerabilities.autofix import (
    NoModelAvailableError,
    NoRepositoryLinkedError,
    RemediationError,
    extraer_diff,
    resolver_modelo,
)
from backend.apps.vulnerabilities.models import IssueStatusEnum, Vulnerability
from backend.apps.webhooks.emission import (
    EventType,
    publish_event,
    vulnerability_status_payload,
)
from backend.core.config import settings

logger = logging.getLogger(__name__)


class PublicarRemediationError(RemediationError):
    """El diff se generó pero la rama o el PR no se pudieron publicar.

    El consumo ya se cobró cuando se lanza. El mensaje lo dice para que la interfaz no ofrezca
    "reintentar" como si no costara nada: reintentar cuesta otro intento de modelo.
    """


async def _publicar_en_hilo(review_id: str, vulnerability_id: str) -> str:
    """Abre la rama y el PR llamando al servicio de publicación del repositorio.

    Va en un hilo a propósito. `create_autofix_branch_and_pr` es un envoltorio síncrono que
    se apoya en `asyncio.run()`, pensado para un worker; desde dentro de un endpoint hay ya un
    bucle corriendo y `asyncio.run()` no se puede anidar. `to_thread` da el contexto limpio
    que el envoltorio necesita sin reescribirlo, y reescribirlo duplicaría su lógica de
    publicación en dos versiones que divergirían.

    El hilo no es un coste evitable por otra vía: la publicación habla con la API de GitHub o
    GitLab, así que es E/S de red de todos modos y el hilo no añade latencia.
    """


    return await asyncio.to_thread(create_autofix_branch_and_pr, review_id, vulnerability_id)


async def generar_y_publicar(
    session: AsyncSession,
    vulnerability: Vulnerability,
    *,
    invoke_llm: Callable[..., Awaitable[Any]] | None = None,
    publicar: Callable[[str, str], Awaitable[str]] = _publicar_en_hilo,
) -> str:
    """Genera el parche, lo publica y actualiza el hallazgo. Devuelve la URL de la PR.

    ## Por qué `invoke_llm` y `publicar` son parámetros

    Para que la prueba no tenga que hablar con un proveedor ni con GitHub. Es la misma
    decisión que en `attribution_environment`: la dependencia se declara en la firma en vez de
    importarse, y así el módulo se puede probar entero —incluida la liquidación— sin red.

    ## Por qué el publicador por defecto es **asíncrono**

    Porque la versión síncrona de la publicación —`create_autofix_branch_and_pr`— es un
    envoltorio con `asyncio.run()` para poder usarse desde un worker de Celery. Llamarla desde
    un endpoint es un `RuntimeError` garantizado: el bucle de eventos ya está corriendo, y
    `asyncio.run()` no admite anidarse. No es un caso raro ni una configuración exótica: es
    **la primera llamada** de la ruta, siempre.

    Y por eso el fallo es tan silencioso: la función existe, el import es válido, la firma
    encaja y el error solo aparece al ejecutarla. Lo encontró la prueba que dejó el publicador
    por defecto; las que lo sustituyen por un doble pasaban sin tocarlo.
    """

    if vulnerability.status is IssueStatusEnum.FIXED:
        raise RemediationError(
            "El hallazgo ya está cerrado: no tiene sentido proponer una corrección"
        )

    repository = await generador.resolver_repositorio_del_hallazgo(
        session, vulnerability
    )
    if repository is None:
        raise NoRepositoryLinkedError(
            "Este hallazgo no viene de un repositorio del espacio de trabajo"
        )

    model = await resolver_modelo(session)
    prompt = generador.construir_prompt(vulnerability)
    invocar = invoke_llm or _invocar_por_defecto

    try:
        respuesta = await invocar(
            model=model,
            prompt=prompt,
            system=generador.SYSTEM_PROMPT,
        )
    except LlmUpstreamError as error:
        # El proveedor falló, así que **no** hay consumo que registrar: no se sabe qué
        # gastó. Cobrar a ciegas por un fallo ajeno es la forma más rápida de perder la
        # confianza de un cliente que ve su saldo bajar sin un parche a cambio.
        logger.warning("El proveedor LLM falló al generar la remediación: %s", error)
        raise RemediationError(
            "El servicio de modelos no pudo generar la corrección"
        ) from error

    await generador.registrar_consumo(
        session,
        organization_id=vulnerability.organization_id,
        model=model,
        prompt_tokens=respuesta.prompt_tokens,
        completion_tokens=respuesta.completion_tokens,
        reference=f"{vulnerability.id}:autofix",
    )

    try:
        diff = extraer_diff(generador.limpiar_razonamiento(respuesta.text))
    except RemediationError:
        # El consumo **ya** se cobró y se registró, que es lo correcto: los tokens se
        # gastaron. Se anota con el motivo para que el margen siga siendo auditable.
        logger.info(
            "La generación de la remediación %s no produjo un diff aplicable",
            vulnerability.id,
        )
        raise

    # La validación es la del motor, no una nueva. Dos criterios de aceptación divergen en
    # cuanto uno de los dos se toca, y el que no se toca es el que deja pasar diffs inválidos.
    try:
        parche = validate_autofix_patch(diff)
    except AutofixError as error:
        raise RemediationError(
            "El parche generado no es un diff aplicable"
        ) from error

    # El diff va en `remediation_patch_diff` y **nunca** en `autofix_patch_diff`. Ese campo es
    # evidencia forense y el trigger de R4 lo hace inmutable junto con el PoC y el CVSS, con
    # razón: lo que escribió el motor durante el escaneo no puede reescribirse. Lo que se
    # acaba de generar es un borrador, y un borrador que no se puede rehacer no es un
    # borrador. Ver la nota del modelo.
    vulnerability.remediation_patch_diff = parche
    await session.commit()

    review_id = await _review_id_del_hallazgo(session, vulnerability)
    try:
        url = await publicar(review_id, str(vulnerability.id))
    except (AutofixError, ValueError) as error:
        raise PublicarRemediationError(
            "No se pudo abrir la pull request con la corrección"
        ) from error

    # El estado previo se lee de la fila y no de un literal: un hallazgo que venía de
    # `IN_PROGRESS` debe publicar ese, no `OPEN`. Hardcodearlo haría que un consumidor que
    # rebuilda su vista a partir del evento viera una transición que no ocurrió.
    previous_status = vulnerability.status
    vulnerability.remediation_pr_url = url
    vulnerability.status = IssueStatusEnum.REMEDIATION_PROPOSED
    await session.commit()

    await _emitir_cambio_de_estado(session, vulnerability, previous_status, url)
    logger.info(
        "Remediación propuesta para %s en %s", vulnerability.id, repository.full_name
    )
    return url


async def _review_id_del_hallazgo(
    session: AsyncSession, vulnerability: Vulnerability
) -> str:
    """La revisión de PR asociada al hallazgo, o un identificador nulo.

    ## Por qué puede no existir y por qué eso no es un error

    Porque hay dos orígenes de hallazgo. Uno es una revisión de pull request, que tiene
    revisión. El otro es un escaneo de pentest sobre el repositorio, que **no la tiene**: no
    hubo PR que revisar.

    La publicación de la rama necesita la revisión para saber la rama base, y sin ella no
    puede directionar el PR. Se pasa un identificador nulo y la publicación usa la rama por
    defecto del repositorio, que es lo correcto para un hallazgo de escaneo: se propone el
    arreglo contra `main`, no contra una rama que ya no existe.
    """


    revision = (
        await session.execute(
            select(PullRequestReview.id)
            .where(
                PullRequestReview.organization_id == vulnerability.organization_id,
                PullRequestReview.run_id == vulnerability.run_id,
            )
            .order_by(PullRequestReview.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return "" if revision is None else str(revision)


async def _emitir_cambio_de_estado(
    session: AsyncSession,
    vulnerability: Vulnerability,
    previous_status: IssueStatusEnum,
    url: str,
) -> None:
    """Publica `vulnerability.status_changed` con la PR incluida.

    Se emite **después** de confirmar la base, para que un suscriptor que venga a leer el
    hallazgo encuentre el estado nuevo. Emitir antes significaría que alguien recibe un evento
    de cambio de estado y, al ir a leer, ve el estado anterior.

    El `previous_status` se toma de la fila **antes** de moverla, no de un literal. Hardcodear
    `OPEN` haría que un hallazgo que venía de `IN_PROGRESS` publicara un `previous_status`
    falso, y un consumidor que rebuilda su vista a partir del evento —que es lo que hace
    cualquiera que mantenga un contador— contaría una transición que no ocurrió.
    """

    await publish_event(
        session,
        EventType.VULNERABILITY_STATUS_CHANGED,
        vulnerability.organization_id,
        {
            **vulnerability_status_payload(
                vulnerability_id=vulnerability.id,
                previous_status=previous_status.value,
                new_status=IssueStatusEnum.REMEDIATION_PROPOSED.value,
                severity=vulnerability.severity.value,
                title=vulnerability.title,
            ),
            # La URL viaja en el evento porque es lo que hace que un suscriptor pueda abrir
            # la propuesta sin una segunda llamada a la API.
            "remediation_pr_url": url,
        },
    )


async def _invocar_por_defecto(
    *, model: Any, prompt: str, system: str
) -> Any:
    """La llamada real al proveedor.

    El `api_base` y la `api_key` se leen de la configuración **en la llamada** y no de un
    import de módulo. `Settings` es inmutable y el cliente los recibe como parámetros a
    propósito: leerlos del global obligaría a `object.__setattr__` en producción y debilitaría
    la garantía de que la configuración no cambia a mitad de una petición.
    """

    return await complete(
        model=model.model_id,
        prompt=prompt,
        api_base=settings.llm_api_base,
        api_key=settings.llm_api_key,
        system=system,
        max_tokens=settings.strix_max_autofix_chars // 4,
    )


#: El scope que autoriza a pedir una remediación.
#:
#: Es `vulnerabilities:triage` y no uno nuevo porque la acción **es** triage: mover el
#: hallazgo a un estado distinto y abrir un PR es exactamente lo que ese permiso significa.
#: Crear `vulnerabilities:remediate` sería una cuarta forma de decir lo mismo, y los scopes
#: que duplican otros no enseñan nada y solo hacen el catálogo más difícil de leer.
AUTOFIX_SCOPE: Scope = Scope.VULNERABILITIES_TRIAGE


__all__ = [
    "AUTOFIX_SCOPE",
    "NoModelAvailableError",
    "NoRepositoryLinkedError",
    "PublicarRemediationError",
    "RemediationError",
    "generar_y_publicar",
]
