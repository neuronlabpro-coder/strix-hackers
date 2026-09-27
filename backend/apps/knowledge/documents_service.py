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

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.knowledge.documents import (
    KnowledgeDocTypeEnum,
    WorkspaceKnowledgeDocument,
)


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
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[WorkspaceKnowledgeDocument], int]:
    """Los documentos del workspace, y cuántos hay en total.

    El filtro por organización es **obligatorio y no opcional**, y va en la consulta y no en un
    `where` que el llamador pueda olvidar. Es la misma razón por la que la cabecera
    `X-Organization-Id` es un selector y no una autoridad: el aislamiento se comprueba siempre,
    aunque hoy el único llamador pase bien el valor.
    """

    condiciones = [WorkspaceKnowledgeDocument.organization_id == organization_id]
    if doc_type is not None:
        condiciones.append(WorkspaceKnowledgeDocument.doc_type == doc_type)

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
