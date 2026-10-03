"""Alta, listado y borrado de documentos de contexto del workspace.

## Por qué el borrado está permitido y el de hallazgos no

Porque no son lo mismo. Un hallazgo de seguridad es **evidencia forense** y R4 lo declara
inmutable desde la interfaz: un hallazgo que se puede borrar no sirve para auditoría, y el
cliente que paga por SOC 2 necesita poder demostrar qué se encontró y cuándo. Un documento de
contexto es **entrada del cliente**: si escribe mal una regla de negocio, la corrige y la
sustituye, igual que corrige una contraseña.

La asimetría es deliberada y es la razón de que esta tabla no comparta modelo con
`knowledge_entries`, que sí es de solo lectura.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.knowledge.documents import (
    KnowledgeDocTypeEnum,
    WorkspaceKnowledgeDocument,
)
from backend.core.filtros_texto import coincide, rango_creado


class DocumentNotFoundError(Exception):
    """El documento no existe en este workspace.

    De dominio y no `HTTPException`, por la misma razón que el resto del proyecto: la decisión
    la toma el servicio y la ruta la traduce.
    """


async def create_document(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    title: str,
    doc_type: KnowledgeDocTypeEnum,
    content: str,
) -> WorkspaceKnowledgeDocument:
    """Guarda un documento y lo devuelve con su identificador ya asignado."""

    documento = WorkspaceKnowledgeDocument(
        organization_id=organization_id,
        title=title,
        doc_type=doc_type,
        content=content,
    )
    session.add(documento)
    await session.flush()
    return documento


async def list_documents(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    doc_type: KnowledgeDocTypeEnum | None = None,
    query: str | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[WorkspaceKnowledgeDocument], int]:
    """Los documentos del workspace, y cuántos hay en total.

    El filtro por organización es **obligatorio y no opcional**, y va en la consulta y no en un
    `where` que el llamador pueda olvidar. Es la misma razón por la que la cabecera
    `X-Organization-Id` es un selector y no una autoridad: el aislamiento se comprueba siempre,
    aunque hoy el único llamador pase bien el valor.

    ## Por qué el texto busca en el título y en el contenido

    Porque el título es lo que se escribe al guardar y el contenido es lo que se va a leer. Un
    documento de arquitectura se titula `payments` y dentro tiene la palabra `webhook` mil
    veces; quien busca es quien recuerda una frase que leyó, y esa frase casi nunca está en el
    título. Con el título solo, el buscador parecería roto la mitad de las veces.

    Y con las dos columnas, `LIKE` va sin escapar por el motivo que explica
    `core.filtros_texto.escape_like`: los títulos de documentos llevan `_` y `%` con más
    frecuencia de lo que parece, y sin escape `?query=%` devolvería la tabla entera.

    ## Por qué el rango va sobre `created_at` y no sobre `updated_at`

    Porque el listado se ordena por `updated_at`, pero `created_at` es la columna que nunca es
    `NULL` y la que responde a «qué documentos subí en esta semana». Filtrar por `updated_at`
    mezclaría dos preguntas distintas —cuándo se subió y cuándo se tocó— y un documento
    reescrito la semana pasada aparecería en el rango de creación de hace seis meses, que es
    justo lo que el filtro está diciendo que no quiere.

    Se dice en el `title` de la etiqueta, en la pantalla, que el rango es de alta.
    """

    condiciones = [WorkspaceKnowledgeDocument.organization_id == organization_id]
    if doc_type is not None:
        condiciones.append(WorkspaceKnowledgeDocument.doc_type == doc_type)
    # `coincide` devuelve **una sola** condición, con su propio `or_` de columnas dentro. Se
    # añade a la lista y no se mezcla con el filtro de organización: la lista se une con `AND`,
    # así que el texto acota la lista que el tenant ya ha recortado. Envolver los dos en un
    # `or_` sería devolver el workspace entero en cuanto el término casara con un título.
    if termino := (query or "").strip():
        condiciones.append(
            coincide(
                [WorkspaceKnowledgeDocument.title, WorkspaceKnowledgeDocument.content],
                termino,
            )
        )
    condiciones.extend(
        rango_creado(WorkspaceKnowledgeDocument.created_at, created_from, created_to)
    )

    total = int(
        (
            await session.execute(
                select(func.count(WorkspaceKnowledgeDocument.id)).where(*condiciones)
            )
        ).scalar_one()
    )
    filas = (
        (
            await session.execute(
                select(WorkspaceKnowledgeDocument)
                .where(*condiciones)
                # `updated_at` descendente con el `id` como desempate. Sin el desempate, dos
                # documentos subidos en la misma transacción vienen en orden arbitrario y la
                # tabla da un salto al recargar, lo que parece que el guardado se ha duplicado.
                .order_by(
                    WorkspaceKnowledgeDocument.updated_at.desc(),
                    WorkspaceKnowledgeDocument.id,
                )
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return list(filas), total


async def get_document(
    session: AsyncSession, organization_id: uuid.UUID, document_id: uuid.UUID
) -> WorkspaceKnowledgeDocument:
    """Un documento por identificador, dentro del workspace.

    El filtro es por **organization_id y id a la vez**, no por id. Un documento de otro tenant
    devuelve «no encontrado» y no «sin permiso»: confirmar que existe ya es filtrarlo.
    """

    documento = (
        await session.execute(
            select(WorkspaceKnowledgeDocument).where(
                WorkspaceKnowledgeDocument.id == document_id,
                WorkspaceKnowledgeDocument.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if documento is None:
        raise DocumentNotFoundError("Documento no encontrado")
    return documento


async def delete_document(
    session: AsyncSession, organization_id: uuid.UUID, document_id: uuid.UUID
) -> None:
    """Borra un documento del workspace.

    Se localiza primero con el filtro de organización y se borra la fila ya traída, en vez de
    issuing un `DELETE` con la condición. La diferencia es que un `DELETE ... WHERE id = :id`
    sin el filtro de organización borraría el documento de otro tenant si el identificador
    coincidiera, y con él un `404` que no distingue "no existe" de "no es tuyo" ocultaría el
    fallo en vez de delatarlo.
    """

    documento = await get_document(session, organization_id, document_id)
    await session.delete(documento)
    await session.flush()
