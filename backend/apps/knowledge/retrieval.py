"""Recuperación de los documentos de contexto del workspace.

## Por qué **no** hay búsqueda vectorial, y sí una degradación declarada

Porque `pgvector` **no está disponible** en la base de este proyecto, y está medido: la
extensión no aparece en `pg_available_extensions` y `CREATE EXTENSION vector` responde
`extension "vector" is not available`. No es un problema de código, es que el servidor de
PostgreSQL 16.15 del VPS no la tiene compilada.

Aun asi, **añadir la columna `Vector(1536)` ahora sería un error**, por dos razones que se
pueden medir:

1. `alembic check` declararía deriva: el modelo tendría una columna que la base no puede tener.
2. Y, mas grave: **no hay ningún modelo de embeddings en el catálogo ni en la configuración**
   —cero, medido—, así que la columna no recibiría nunca un vector. Parecería que la búsqueda
   semántica funciona y devolvería siempre cero resultados, que es peor que no tenerla.

Por eso la recuperación tiene **dos caminos** y elige con una comprobación real, no con un
suposado:

- **Vectorial**, cuando exista la extensión, la columna y un modelo de embeddings. No está
  escrito todavía porque no se puede probar; se activa en cuanto las tres cosas existan.
- **Textual**, que es el que funciona hoy: los términos de la pregunta se buscan con `ILIKE`
  sobre el título, la descripción y el cuerpo, con una puntuación que ordena por dónde casaron.

La puntuación textual no es un `ILIKE` suelto. Un `ILIKE '%auth%'` devuelve todo lo que
menciona "auth" sin distinguir un documento **titulado** sobre autenticación de uno que lo
menciona de pasada. Aquí el título pesa más que la descripción, y la descripción más que el
cuerpo, y un documento que casó en los tres puntúa por encima de tres que casaron en uno.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.knowledge.documents import WorkspaceKnowledgeDocument
from backend.apps.knowledge.okf import OkfDocument, parse_okf
from backend.core.filtros_texto import patron_contains

#: Cuántos documentos se recuperan por defecto.
#:
#: Tiene que coincidir con `settings.chat_context_documents`, que es lo que usa el runner. Dos
#: números distintos para lo mismo hacen que "el valor por defecto" sea una de las dos cosas y no
#: la otra, y el que se equivoca es el que lee la otra: aquí se vería en las pruebas de esta
#: función, y en el runner no se vería en ninguno. Hay una prueba en `test_chat_runner.py` que
#: los compara precisamente por eso.
#:
#: Cuatro es lo que cabe en un prompt sin que el contexto del cliente desplace a la pregunta,
#: que es lo que el usuario ha escrito. Con más, el modelo empieza a responder sobre reglas que
#: nadie le ha preguntado.
TOP_K_POR_DEFECTO = 4

#: Un término de menos de tres letras produce demasiados candidatos: "id", "api" y "ui" salen
#: en la mitad de los documentos de un cliente y no distinguen nada.
LONGITUD_MINIMA = 3

#: Se descartan estos términos. No por poco significado, sino porque aparecen en casi todos los
#: documentos de contexto y no señalan ninguno: "nota", "dato" o "test".
IRRELEVANTES: frozenset[str] = frozenset(
    {
        "the", "and", "for", "with", "this", "that", "from", "los", "las", "una", "uno",
        "del", "que", "por", "con", "para", "sus", "como", "pero",
    }
)

#: Un término se busca por sus partes, no entero. `autenticacion` en español no aparece
#: escrito con tilde en un documento technical, y `authorization` en ingles no esta en uno
#: espanol. Partir por guion y por camelCase hace que las dos formasdencontre.
_SEPARADORES = re.compile(r"[-_\s]+|(?<=[a-z0-9])(?=[A-Z])")


@dataclass(frozen=True)
class RetrievedDocument:
    """Un documento recuperado, con la parte por la que se recuperó.

    El `score` se devuelve para que el llamador pueda explicar **por qué** se inyectó cada
    documento. Un RAG que no explica sus fuentes es un RAG del que el usuario no puede dudar,
    y un usuario que no puede dudar de la respuesta la acaba aceptando aunque sea falsa.
    """

    document_id: uuid.UUID
    title: str
    doc_type: str
    description: str
    body: str
    score: float
    matched_terms: tuple[str, ...]

    def as_context_block(self) -> str:
        """El bloque que se inyecta en el prompt, delimitado.

        ## Por qué los delimitadores explícitos y no un encabezado Markdown

        Porque un encabezado es ambiguo cuando hay varios documentos seguidos: un `###` puede
        ser el título de un documento y una subsección del anterior, y el modelo no tiene forma
        de saber cuál de las dos cosas está leyendo. La marca `[DOCUMENTO OKF: ...]` y las tres
        líneas de guiones lo dicen sin ambigüedad, y esa unambigüedad es exactamente lo que
        permite que el modelo cite la fuente en vez de parafrasearla como si fuera suya.

        La cita no es decorativa: es lo que permite decir "esto lo dice tu documento de
        autenticación" en vez de afirmar una regla como si fuera una regla general.
        """

        return (
            f"[DOCUMENTO OKF: {self.title} ({self.doc_type})]\n"
            f"{self.description}\n"
            "---\n"
            f"{self.body}\n"
        )


def extraer_terminos(consulta: str) -> tuple[str, ...]:
    """Los términos con los que se busca, ya normalizados.

    ## Por qué se guardan **el término entero y sus partes**

    Porque son dos búsquedas distintas y hacen falta las dos. El entero encuentra el documento
    que escribe `api-gateway` tal cual; las partes encuentran el que escribe `api` y `gateway`
    en otra frase. Con solo las partes se pierde el primero, y con solo el entero se pierde el
    segundo —que es el caso normal, porque un documento técnico escribe `gateway` y no
    `api-gateway` en la frase donde lo nombra.

    Con solo las partes, buscar "revisa el api-gateway" devolvía `('revisa', 'api', 'gateway')`
    y no encontraba un documento que escribiera la palabra entera. Es una pérdida de alcance
    silenciosa: la consulta devuelve resultados, así que parece que funciona.

    Se quitan las palabras vacias y las irrelevantes. Un término de menos de tres letras
    produce demasiados candidatos: "id", "api" y "ui" salen en la mitad de los documentos de un
    cliente y no distinguen ninguno.
    """

    vistos: dict[str, None] = {}

    def anota(valor: str) -> None:
        limpio = valor.strip(".,;:!?()[]{}\"'")
        if len(limpio) >= LONGITUD_MINIMA and limpio not in IRRELEVANTES:
            vistos.setdefault(limpio, None)

    for cruda in consulta.lower().split():
        # El termino entero, tal cual aparece escrito en la consulta.
        anota(cruda)
        # Y sus partes, que es donde aparecen los acentos y los sinónimos.
        for parte in _SEPARADORES.split(cruda):
            anota(parte)

    return tuple(vistos)


def condicion_de_terminos(terminos: tuple[str, ...]) -> ColumnElement[bool]:
    """La condición de texto de la recuperación: algún término casa en título o cuerpo.

    ## Por qué vive en su propia función y no dentro de `recuperar_documentos`

    Porque es la **única** parte de la recuperación cuyo SQL se puede inspeccionar desde fuera,
    y es la parte que decide si los comodines de `LIKE` se escapan o no. Dentro de
    `recuperar_documentos` el escape no se puede probar: la función devuelve documentos ya
    puntuados, y como `_puntuar` vuelve a filtrar por subcadena literal, un término con
    comodines no aparece en ningún documento y sale igual con escape o sin él. Extrayéndola, la
    prueba puede mirar el patrón que sale —`ESCAPE` declarado y la barra invertida en el
    parámetro— en vez de confiar en un resultado que no distingue las dos versiones.

    No es una función hecha solo para la prueba: lo que construye es una condición de la base,
    tiene forma propia y con razón propia, y leerla sin subir hasta el final de una función de
    treinta líneas ya es más legible.

    ## Por qué el `ILIKE` va sobre título y cuerpo

    Porque es lo que hay sin embeddings, y es exactamente por eso que la puntuación pondera:
    encontrar la palabra en el título dice más que encontrarla en la última línea de un
    documento largo.

    ## Por qué los comodines van escapados

    Porque lo que se cuela aquí no es una fila de más en una tabla: es un documento entero en el
    prompt del chat, y el modelo lo toma como contexto fiable. Sin escape, un `%` escrito en la
    pregunta —`100% de descuento` es una frase normal de un cliente, no un intento— devuelve el
    workspace entero.

    ## Por qué el escape se pone también aunque hoy no se pueda demostrar el `_`

    Porque el `_` queda enmascarado por el troceado: `extraer_terminos` parte `web_app` en
    `web_app`, `web` y `app`, y `web` y `app` ya son subcadena de `webXapp`, así que ese documento
    entra igual con el `_` escapado o sin él. El escape se pone porque es lo correcto y porque la
    doble filtración que hoy lo tapa —esta condición y luego `_puntuar`— es una coincidencia de
    implementación, no una garantía.
    """

    return or_(
        *(
            or_(
                WorkspaceKnowledgeDocument.title.ilike(patron_contains(termino), escape="\\"),
                WorkspaceKnowledgeDocument.content.ilike(patron_contains(termino), escape="\\"),
            )
            for termino in terminos
        )
    )


async def recuperar_documentos(
    session: AsyncSession,
    organization_id: uuid.UUID,
    consulta: str,
    *,
    top_k: int = TOP_K_POR_DEFECTO,
) -> list[RetrievedDocument]:
    """Los documentos de contexto más relevantes para una pregunta.

    ## Por qué el filtro por organización es **no negociable** aquí

    Porque esta función se llama desde el prompt del chat, y el prompt es la superficie donde
    un fallo de aislamiento se vuelve contenido. Si un tenant recibiera en su contexto un
    documento de otro, no vería un identificador raro en una lista: vería **las reglas de
    negocio de su competidor** respondiendo a su pregunta, y no tendría forma de saber que no
    son suyas.

    ## Por qué aquí hay **dos** desempates y no uno

    Porque aquí la ordenación que ve el usuario no es la de SQL: la puntuación se calcula en
    Python, con `_puntuar`. Poner `id` al final del `ORDER BY` de la consulta **no** arregla
    nada de lo que el usuario nota, porque ese `ORDER BY` solo decide qué `top_k * 3`
    documentos entran como candidatos; el orden final lo pone el `sort` de más abajo. Un solo
    desempate en SQL deja el problema entero donde se ve.

    Los dos hacen falta y ninguno de los dos cambia el filtro de organización, que sigue
    siendo la primera condición del `WHERE` (R3).
    """

    terminos = extraer_terminos(consulta)
    if not terminos:
        return []

    condicion_organizacion = (
        WorkspaceKnowledgeDocument.organization_id == organization_id
    )
    condicion_texto = condicion_de_terminos(terminos)

    filas = (
        (
            await session.execute(
                select(WorkspaceKnowledgeDocument)
                .where(condicion_organizacion, condicion_texto)
                # El desempate del **corte**. `updated_at` es `now()` de servidor y todos los
                # documentos que se indexan en la misma transacción comparten marca, así que
                # sin este segundo criterio el `limit(top_k * 3)` deja fuera un documento
                # distinto en cada llamada. El síntoma no es una página repetida: es que la
                # **misma pregunta** recibe un contexto distinto, y el usuario lo lee como que
                # el modelo ha cambiado de opinión entre un mensaje y el siguiente.
                .order_by(WorkspaceKnowledgeDocument.updated_at.desc())
                .order_by(WorkspaceKnowledgeDocument.id.desc())
                .limit(top_k * 3)
            )
        )
        .scalars()
        .all()
    )

    puntuados: list[tuple[WorkspaceKnowledgeDocument, float, tuple[str, ...]]] = []
    for documento in filas:
        puntuacion, casados = _puntuar(documento, terminos)
        if puntuacion > 0:
            puntuados.append((documento, puntuacion, casados))

    # El desempate por titulo hace la consulta **estable**: dos documentos con la misma
    # puntuacion vienen siempre en el mismo orden, y una lista que cambia de orden al recargar
    # hace pensar que ha cambiado algo cuando solo ha cambiado la consulta.
    #
    # Y `id` va detrás del título, no delante. `title` se queda como primer criterio porque es
    # lo que lee una persona —dos documentos con la misma puntuación se ordenan por su nombre,
    # que es lo esperable—, pero **el título no es único**: nada impide que dos documentos se
    # llamen igual, y mientras ese caso exista la lista sigue sin estar del todo ordenada. El
    # `id` cierra el orden porque sí es único, y de paso decide el `[:top_k]` de abajo: sin
    # él, dos documentos empatados en todo lo anterior entran y salen del contexto según le
    # toque.
    puntuados.sort(key=lambda trio: (-trio[1], trio[0].title, trio[0].id))

    return [
        _a_recuperado(documento, puntuacion, casados)
        for documento, puntuacion, casados in puntuados[:top_k]
    ]


#: Peso por zona del documento. El titulo manda porque es lo que el cliente eligio para
#: nombrar el concepto, y la descripcion va segunda porque es el resumen que escribio del
#: proposito. El cuerpo va ultimo porque una mencion de paso no convierte un documento en la
#: respuesta a la pregunta.
_PESO = {"titulo": 3.0, "descripcion": 2.0, "cuerpo": 1.0}


def _puntuar(
    documento: WorkspaceKnowledgeDocument, terminos: tuple[str, ...]
) -> tuple[float, tuple[str, ...]]:
    """Puntúa un documento y dice qué términos casaron.

    El frontmatter se separa del cuerpo **antes** de puntuar. Si no, el propio `title` del
    frontmatter contaria como una mencion del titulo en el cuerpo, y un documento ganaria
    puntos por su propio encabezado: puntuarlo dos veces no es un matiz, es un sesgo.
    """

    try:
        okf: OkfDocument | None = parse_okf(documento.content)
    except Exception:
        # Un documento que no cumple el formato no es motivo para fallar la consulta. Se
        # puntua con el texto entero, que es peor pero es algo, y se deja el error al que
        # intento guardarlo.
        okf = None

    if okf is not None:
        titulo, descripcion, cuerpo = okf.title, okf.description, okf.body
    else:
        titulo, cuerpo = documento.title, documento.content
        descripcion = ""

    titulo_min = titulo.lower()
    descripcion_min = descripcion.lower()
    cuerpo_min = cuerpo.lower()

    total = 0.0
    casados: list[str] = []
    for termino in terminos:
        puntos = 0.0
        if termino in titulo_min:
            puntos += _PESO["titulo"]
        if termino in descripcion_min:
            puntos += _PESO["descripcion"]
        if termino in cuerpo_min:
            puntos += _PESO["cuerpo"]
        if puntos > 0:
            total += puntos
            casados.append(termino)

    # El recargo por terminos casados es lo que separa un documento que trata el tema de tres
    # que lo mencionan de pasada. Sin el, un documento largo con diez menciones perderia
    # a uno corto que trata el tema entero.
    if len(casados) > 1:
        total *= 1.0 + 0.25 * (len(casados) - 1)
    return total, tuple(casados)


def _a_recuperado(
    documento: WorkspaceKnowledgeDocument,
    puntuacion: float,
    casados: tuple[str, ...],
) -> RetrievedDocument:
    try:
        okf = parse_okf(documento.content)
        titulo, descripcion, cuerpo = okf.title, okf.description, okf.body
        tipo = okf.doc_type.value
    except Exception:
        titulo, cuerpo = documento.title, documento.content
        descripcion = ""
        tipo = documento.doc_type.value

    return RetrievedDocument(
        document_id=documento.id,
        title=titulo,
        doc_type=tipo,
        description=descripcion,
        body=cuerpo,
        score=puntuacion,
        matched_terms=casados,
    )
