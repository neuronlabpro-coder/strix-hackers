"""Generación del parche de remediación con un modelo de lenguaje.

## Por qué este módulo existe y no lo hace el motor

Strix **trae** el parche desde dentro del contenedor: el escaneo analiza el código y el motor
redacta un diff que llega ya hecho en `autofix_patch_diff`. Eso es un parche de quien ya tenía
el repositorio desplegado y el modelo dentro de la sesión del escaneo.

Lo que se añade aquí es el otro camino: una vulnerabilidad ya registrada en la plataforma,
de la que solo hay **evidencia** —descripción, PoC, archivo y línea afectados—, y la que se
quiere es un parche sin volver a escanear. La diferencia es dónde se ejecuta y con qué
credenciales, no la forma del resultado.

## Por qué el parche se genera sin el repositorio desplegado

Porque desplegar el repositorio de un cliente para redactar un diff es todo lo que R5
prohíbe: el código tiene que estar en un contenedor efímero y purgarse después. Un parche
generado contra evidencia —"en la línea 42 de este archivo hay esto, y esto es lo que lo
vulnerable"— es **menos preciso** que uno generado con el archivo delante, y por eso el
resultado es una *propuesta* en estado `REMEDIATION_PROPOSED` y no un cambio aplicado. La
diferencia entre las dos cosas es lo que separa una ayuda de una garantía.

## Por qué el bloque de razonamiento se descarta dos veces

Una vez en el cliente HTTP, que ya lo hace para todas las peticiones, y otra aquí al extraer
el diff. No es redundancia gratuita: son dos capas que fallan de forma distinta. El cliente
descarta los bloques `{"type": "reasoning"}` de la respuesta del proveedor, y aquí se descarta
cualquier cosa que el modelo haya metido **dentro** del texto —"Primero veamos…", "El diff es:"—
que es donde se cuela casi siempre. Un diff con una frase de explicación delante no se aplica,
y `git apply` lo rechaza con un error que no señala el texto sobrante.

## Por qué la validación del diff es la del motor y no una nueva

Porque `validate_autofix_patch` ya comprueba tamaño, número de archivos y que el texto se
parsea como un diff. Escribir una segunda validación produciría dos criterios de aceptación
que divergen en cuanto uno de los dos se toca, y el que no se toca es el que se queda
permitiendo un diff inválido.

## Por qué el consumo se cobra y se registra aunque la PR falle

Porque el token ya se gastó. Cobrar solo el éxito regala margen al cliente que reintenta y
perjudica a la plataforma; no registrar el fallo deja el coste real sin dato, y el margen
declarado en el catálogo deja de ser auditable. Son dos asientos distintos y los dos van.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.billing.models import LedgerReasonEnum
from backend.apps.billing.service import apply_credit_delta
from backend.apps.llm_router.models import LLMUsageEvent, LLMUseCaseEnum
from backend.apps.llm_router.routing import (
    LLMAllModelsInactiveError,
    resolve_model_chain,
)
from backend.apps.pentests.models import PentestRun, TargetTypeEnum
from backend.apps.repositories.models import Repository
from backend.apps.vulnerabilities.models import IssueStatusEnum, Vulnerability
from backend.core.config import settings

logger = logging.getLogger(__name__)


class RemediationError(RuntimeError):
    """Fallo controlado al generar o publicar una propuesta de remediación.

    De dominio, no `HTTPException`: la decisión la toma este módulo y cada transporte compone
    su mensaje. Es la misma frontera que usa `pentests.service`.
    """


class NoRepositoryLinkedError(RemediationError):
    """El hallazgo no viene de un repositorio del workspace, así que no hay a qué aplicar.

    Un hallazgo de un escaneo de **dominio** no tiene código propio del cliente: lo vulnerable
    está en un servicio de terceros, y un parche sobre ese servicio no es del cliente ni
    arregla nada. Es un caso esperado, no un error de configuración, y por eso tiene tipo
    propio: el panel puede decir "este hallazgo no tiene repositorio asociado" en vez de
    "no se pudo generar el parche".
    """


class NoModelAvailableError(RemediationError):
    """No hay ningún modelo activo para el caso de uso `AUTOFIX`."""


#: Instruction del sistema. Va separada del prompt de usuario porque el cliente la manda en un
#: campo distinto, y mezclarlas obligaría al modelo a distinguir por formato en lugar de por
#: rol.
SYSTEM_PROMPT: Final[str] = (
    "Eres un ingeniero de seguridad que redacta parches de corrección. "
    "Respondes **únicamente** con un diff unificado en formato `git diff`, sin texto antes ni "
    "después, sin bloques de razonamiento y sin vallas de código. "
    "El diff tiene que aplicarse con `git apply` sobre el repositorio tal como está. "
    "Si no puedes corregir el problema con seguridad, responde con un diff vacío."
)

#: Instruction del usuario: qué hay que corregir y con qué evidencia.
PROMPT_TEMPLATE: Final[str] = """Corrige esta vulnerabilidad.

Título: {titulo}

Severidad: {severidad} (CVSS {cvss})

Descripción:
{descripcion}

Archivo afectado: {archivo}
Línea afectada: {linea}

Objetivo afectado: {objetivo}

Procedimiento de reproducción:
{poc}

Escribe el diff que corrige el problema. No cambies nada que no sea necesario para la
corrección, y no incluyas secretos ni credenciales.
"""

#: Marcas que un modelo deja alrededor del diff. Se buscan **después** de quitar el bloque de
#: razonamiento, porque es la forma en que se cuelan las frases de explicación.
VALLAS: Final[tuple[str, ...]] = ("```diff", "```patch", "```", "~~~")

#: Prefijos de explicación que preceden al diff en texto plano. Un diff empieza por `diff --git`
#: o por `---`, así que todo lo anterior es ruido.
_PREFIJOS_EXPLICATIVOS: Final[tuple[str, ...]] = (
    "aquí tienes",
    "aqui tienes",
    "el diff es",
    "el parche es",
    "primero",
    "claro,",
    "a continuación",
    "a continuacion",
    "here is",
    "here's",
    "the diff is",
    "the patch is",
    "first,",
    "sure,",
)


def _recortar_prompt(texto: str, limite: int) -> str:
    """Acota un campo de evidencia y avisa de que se recortó.

    La evidencia de un hallazgo puede ser un PoC de un millón de caracteres, y mandarlo
    entero no es «más contexto»: es contexto que desplaza la descripción del problema
    fuera de la ventana. Se recorta y se dice `[…]` al final, porque un PoC truncado que parece
    completo produce un parche que el modelo creerá completo.
    """

    if len(texto) <= limite:
        return texto
    return texto[:limite] + "\n[…recortado]"


def extraer_diff(texto: str) -> str:
    """Saca el diff del texto del modelo.

    ## Por qué se busca el inicio del diff y no se recorta por position

    Porque un diff empieza siempre por `diff --git` o por una línea `---`, y todo lo anterior
    —salvedades, explicaciones, "primero revisemos"— es texto que `git apply` rechazará con
    un error que no señala el texto sobrante. Buscar el inicio es la única forma de no
    depender de la lista de prefijos de explicación, que es infinita.
    """

    if not texto.strip():
        raise RemediationError("El modelo no devolvió ningún parche")

    lineas = texto.replace("\r\n", "\n").split("\n")
    inicio = next(
        (
            indice
            for indice, linea in enumerate(lineas)
            if linea.startswith("diff --git") or linea.startswith("--- ")
        ),
        None,
    )
    if inicio is None:
        # Un `MISMATCH` en el que el modelo se negó a redactar un diff **no** es un fallo del
        # servicio: es una respuesta válida. Se distingue de un error vacío, que sí lo es.
        if any(
            linea.strip().lower().startswith(_PREFIJOS_EXPLICATIVOS)
            for linea in lineas[:5]
        ):
            raise RemediationError(
                "El modelo no pudo proponer una corrección segura para este hallazgo"
            )
        raise RemediationError("La respuesta del modelo no contiene un diff")

    parche: list[str] = []
    dentro_de_valla = False
    for linea in lineas[inicio:]:
        if any(linea.strip().startswith(valla) for valla in VALLAS):
            dentro_de_valla = not dentro_de_valla
            continue
        if dentro_de_valla:
            continue
        parche.append(linea)

    resultado = "\n".join(parche).strip()
    if not resultado:
        raise RemediationError("La respuesta del modelo no contiene un diff")
    return resultado


async def resolver_modelo(session: AsyncSession) -> Any:
    """El primer modelo activo de la cadena `AUTOFIX`.

    ## Por qué cae a `ALL` y por qué eso no es opcional

    `resolve_model_chain` incluye ya los modelos declarados como `ALL`, que son
    transversales. Es lo que permite desplegar la plataforma sin declarar un modelo
    específico para cada caso de uso: un despliegue nuevo funciona en cuanto hay un modelo
    activo, y afinar el reparto es una decisión posterior.

    ## Por qué se toma el **primero** y no el que mejor encaje

    Porque la cadena viene ordenada por `priority_order` y ese orden **es** la decisión de
    quien configuró el catálogo. Intentar mejorarla aquí —eligir por longitud de contexto o
    por coste— duplicaría esa política en un sitio que no la posee, y el día que cambiara la
    regla del panel seguiría aplicando la de aquí sin que nadie lo notara.

    ## Por qué se traduce el error del enrutador en vez de mirar la lista vacía

    Porque `resolve_model_chain` **lanza** cuando no hay modelos, no devuelve una lista
    vacía. Una comprobación `if not cadena` no se ejecutaría nunca, y la falta de modelo
    llegaría al cliente como un error del enrutador cuyo texto habla de «modelos de lenguaje
    activos» sin decir que lo que falta es la configuración de remediaciones. La comprobación
    estaba escrita y la ruta la daba por buena: solo fallaba, con un error ajeno.
    """

    try:
        cadena = await resolve_model_chain(session, LLMUseCaseEnum.AUTOFIX)
    except LLMAllModelsInactiveError as error:
        raise NoModelAvailableError(
            "No hay ningún modelo de lenguaje activo para generar remediaciones"
        ) from error
    if not cadena:  # pragma: no cover - el enrutador lanza antes de llegar aquí
        raise NoModelAvailableError(
            "No hay ningún modelo de lenguaje activo para generar remediaciones"
        )
    return cadena[0]


async def resolver_repositorio_del_hallazgo(
    session: AsyncSession, vulnerability: Vulnerability
) -> Repository | None:
    """El repositorio del workspace al que pertenece el hallazgo, o `None`.

    ## Por qué el camino es indirecto

    `Vulnerability` guarda `run_id`, y el run guarda `target_identifier` —una URL o un nombre
    completo—, no el `repository_id`. La relación existe pero pasa por dos saltos y por una
    comparación de texto.

    Y por eso se exige que el `target_identifier` **coincida** con un repositorio de esta
    organización. Sin esa exigencia, un hallazgo cuyo run apunta a una URL cualquiera se
    atribuiría al repositorio activo más cercano, que puede no tener nada que ver: el parche
    se aplicaría al sitio equivocado.
    """

    run = (
        await session.execute(
            select(PentestRun).where(
                PentestRun.id == vulnerability.run_id,
                PentestRun.organization_id == vulnerability.organization_id,
            )
        )
    ).scalar_one_or_none()
    if run is None or run.target_type is not TargetTypeEnum.REPOSITORY:
        return None

    objetivo = run.target_identifier.strip()
    return (
        await session.execute(
            select(Repository).where(
                Repository.organization_id == vulnerability.organization_id,
                Repository.is_active.is_(True),
                (Repository.clone_url == objetivo) | (Repository.full_name == objetivo),
            )
        )
    ).scalar_one_or_none()


def construir_prompt(vulnerability: Vulnerability) -> str:
    """Monta el prompt con la evidencia del hallazgo.

    Los recortes son **por campo** y no sobre el prompt entero, porque el reparto importa: el
    título y la descripción son cortos y si no se tocaran; el PoC es el campo largo, y recortarlo
    es lo único que evita que desplace lo demás. Un recorte global dejaría al modelo sin
    título para saber qué está corrigiendo.
    """

    return PROMPT_TEMPLATE.format(
        titulo=_recortar_prompt(vulnerability.title, 255),
        severidad=str(vulnerability.severity.value),
        cvss=vulnerability.cvss_score,
        descripcion=_recortar_prompt(vulnerability.description, 8_000),
        archivo=_recortar_prompt(vulnerability.affected_target, 512),
        linea=vulnerability.affected_line or "—",
        objetivo=_recortar_prompt(vulnerability.affected_target, 512),
        poc=_recortar_prompt(vulnerability.poc_reproduction_raw, 12_000),
    )


#: Los precios del catálogo están **por millón de tokens**, que es como los publica el
#: proveedor. La conversión vive aquí y no en cada aserto porque es el único sitio donde cabe
#: equivocarse: si el factor fuera un billón en vez de un millón, el cargo sale un millón
#: de veces más caro, y con él desaparece la probabilidad de que alguien se queje.
TOKENS_POR_MILLON: Final[Decimal] = Decimal(1_000_000)


def calcular_coste_base(model: Any, prompt_tokens: int, completion_tokens: int) -> Decimal:
    """Lo que cuesta a la plataforma la llamada, según **su** catálogo.

    ## Por qué no se usa un `cost_usd` de la respuesta del proveedor

    Porque eso sería fiarse de la aritmética de un tercero para fijar lo que se le cobra al
    cliente. La plataforma tiene el catálogo de precios, y el proveedor no sabe nada de ese
    catálogo: si su contabilidad difiere en un redondeo, el margen deja de ser auditable sin
    que nada falle. Aquí el coste sale de los datos que la plataforma posee.

    ## Por qué el mínimo es cero en vez deumnull

    Un modelo que no publica `usage` devuelve cero tokens, y cero es un dato **válido** —dice
    "no lo sé" en el log, no un fallo—. Negarlo cobraría al cliente por un trabajo cuyo coste
    nadie conoce, y la alternativa —no registrar nada— perdería la fila de auditoría.
    """

    entrada = Decimal(prompt_tokens) / TOKENS_POR_MILLON
    salida = Decimal(completion_tokens) / TOKENS_POR_MILLON
    coste = entrada * Decimal(model.base_cost_input_m) + salida * Decimal(
        model.base_cost_output_m
    )
    return max(coste, Decimal(0)).quantize(Decimal("0.000001"))


async def registrar_consumo(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    model: Any,
    prompt_tokens: int,
    completion_tokens: int,
    reference: str,
) -> None:
    """Asienta el consumo real y lo descuenta del saldo del cliente.

    El movimiento es la **diferencia** entre lo que cuesta al cliente y lo que le costaba a
    la plataforma: `apply_credit_delta` resta, así que un cargo entra con signo negativo.
    Invertir el signo aquí evita que cada llamador tenga que acordarse, y acordarse es
    exactamente el tipo de detalle que acaba regalando margen sin que nadie lo note.
    """

    base = calcular_coste_base(model, prompt_tokens, completion_tokens)
    markup = Decimal(str(model.markup_pct)) / Decimal(100)
    precio = (base * (Decimal(1) + markup)).quantize(Decimal("0.0001"))
    await apply_credit_delta(
        session=session,
        organization_id=organization_id,
        amount=-precio,
        reason=LedgerReasonEnum.SCAN_CONSUMPTION,
        reference_id=reference,
    )
    session.add(
        LLMUsageEvent(
            model_config_id=model.id,
            organization_id=organization_id,
            use_case=LLMUseCaseEnum.AUTOFIX,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            base_cost_usd=base,
            net_profit_usd=(precio - base).quantize(Decimal("0.0001")),
        )
    )
    await session.commit()


def limpiar_razonamiento(texto: str) -> str:
    """Quita los bloques de razonamiento y las vallas que alrededor del diff.

    ## Por qué **no** confiar solo en el cliente HTTP

    Porque el cliente HTTP filtra los bloques `{"type": "reasoning"}` del sobre que devuelve
    el proveedor, y eso no cubre dos casos: un modelo que emitsu texto de razonamiento dentro
    del contenido —que es lo que hacen varios—, y un `system` de un intermediario que lo
    reescribe. Son dos capas que fallan de forma distinta, y quedarse con una deja el
    camino abierto al otro.
    """

    limpio = re.sub(
        r"<think(?:ing)?>.*?</think(?:ing)?>", "", texto, flags=re.DOTALL | re.IGNORECASE
    )
    limpio = re.sub(
        r"<(?:reasoning|scratchpad)>.*?</(?:reasoning|scratchpad)>",
        "",
        limpio,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return limpio.strip()


#: Tope de la respuesta. Es el mismo valor que el motor usa para el parche que él genera, y se
#: comparte a propósito: si el motor no puede producir un diff mayor, esta plataforma tampoco
#: debería aceptarlo.
MAX_RESPONSE_CHARS: Final[int] = settings.strix_max_autofix_chars


#: Tipo del invocador del modelo. Se declara como alias para que las pruebas puedan pasar un
#: doble sin que el módulo dependa del cliente HTTP.
LlmInvoker = Callable[..., Any]

__all__ = [
    "MAX_RESPONSE_CHARS",
    "SYSTEM_PROMPT",
    "IssueStatusEnum",
    "NoModelAvailableError",
    "NoRepositoryLinkedError",
    "RemediationError",
    "construir_prompt",
    "extraer_diff",
    "limpiar_razonamiento",
    "registrar_consumo",
    "resolver_modelo",
    "resolver_repositorio_del_hallazgo",
]
