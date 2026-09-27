"""Endpoints de la base de conocimiento corporativa.

## Por qué no hay `PUT` ni `PATCH`

Porque el contenido se **sustituye** entero, no se parchea. Un documento es una unidad de
contexto: medio documento de reglas de negocio no significa nada para el motor, y permitir
parchearlo invita a dejar la tabla a medias sin que nada avise. Quien quiere cambiarlo, lo
reescribe; quien se equivoca, lo borra y lo vuelve a subir.

Por eso el `POST` acepta el contenido completo y no hay ruta de edicion parcial. Es una
decision de la forma del dato, no una funcionalidad pendiente.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from backend.apps.knowledge import documents_service
from backend.apps.knowledge.documents import KnowledgeDocTypeEnum
from backend.apps.knowledge.schemas import (
    KnowledgeDocumentCreate,
    KnowledgeDocumentDetail,
    KnowledgeDocumentItem,
    KnowledgeDocumentPage,
)
from backend.core.middleware import (
    SessionDependency,
    TenantContext,
    get_current_tenant,
)

#: El tenant se resuelve por la cabecera `X-Organization-Id` y **se comprueba** contra la
#: membresia del usuario. No es una confianza en lo que dice la cabecera: es un selector
#: que el backend valida, y por eso enviar el identificador de otro workspace no da acceso
#: a nada.
TenantDependency = Annotated[TenantContext, Depends(get_current_tenant)]

router = APIRouter(prefix="/api/v1/knowledge/documents", tags=["knowledge"])


def _no_encontrado() -> HTTPException:
    """El `404` de estos endpoints.

    `404` y no `403` a proposito: un `403` confirmaria que el documento existe, y con eso basta
    para enumerar identificadores y saber que tiene el tenant vecino. El `404` no distingue
    "no existe" de "no es tuyo", que es lo unico que no filtra informacion.
    """

    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="Documento no encontrado"
    )


@router.get("", response_model=KnowledgeDocumentPage)
async def listar_documentos(
    tenant: TenantDependency,
    session: SessionDependency,
    doc_type: Annotated[KnowledgeDocTypeEnum | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> KnowledgeDocumentPage:
    """Los documentos de contexto del workspace activo.

    Devuelve solo la cabecera de cada uno; el contenido se pide documento a documento. Un
    listado con veinte especificaciones de API son varios megabytes que la tabla de la pantalla
    no usa.
    """

    documentos, total = await documents_service.list_documents(
        session,
        tenant.organization.id,
        doc_type=doc_type,
        limit=limit,
        offset=offset,
    )
    return KnowledgeDocumentPage(
        documents=[KnowledgeDocumentItem.model_validate(d) for d in documentos],
        total=total,
    )


@router.post("", response_model=KnowledgeDocumentDetail, status_code=status.HTTP_201_CREATED)
async def crear_documento(
    tenant: TenantDependency,
    session: SessionDependency,
    payload: KnowledgeDocumentCreate,
) -> KnowledgeDocumentDetail:
    """Guarda un documento de contexto.

    Devuelve el documento con su contenido, y no solo el `201`: el cliente acaba de escribir
    varios miles de lineas y quiere comprobar que se han guardado tal cual, no recibir un
    identificador y tener que releerlo para saber si algo se perdio por el camino.
    """

    documento = await documents_service.create_document(
        session,
        tenant.organization.id,
        title=payload.title,
        doc_type=payload.doc_type,
        content=payload.content,
    )
    await session.commit()
    return KnowledgeDocumentDetail.model_validate(documento)


@router.get("/{document_id}", response_model=KnowledgeDocumentDetail)
async def leer_documento(
    tenant: TenantDependency,
    session: SessionDependency,
    document_id: uuid.UUID,
) -> KnowledgeDocumentDetail:
    """Un documento con su contenido completo.

    El `DocumentNotFoundError` se traduce a `404` en la ruta y no en el servicio, que es la
    separacion que sigue el resto del proyecto: el servicio decide, la ruta traduce. Sin esa
    traduccion la excepcion sube hasta FastAPI y sale un `500`, que dice "fallo del servidor"
    cuando el documento no existe. Un cliente que reintenta ante un `500` se queda golpeando
    una peticion que jamas va a salir bien.
    """

    try:
        documento = await documents_service.get_document(
            session, tenant.organization.id, document_id
        )
    except documents_service.DocumentNotFoundError as error:
        raise _no_encontrado() from error
    return KnowledgeDocumentDetail.model_validate(documento)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def borrar_documento(
    tenant: TenantDependency,
    session: SessionDependency,
    document_id: uuid.UUID,
) -> Response:
    """Borra un documento del workspace.

    `204` y no un cuerpo con un mensaje: no hay nada que devolver, y un `200` con `{"ok": true}`
    obliga al cliente a inventarse un tipo para un valor constante.
    """

    try:
        await documents_service.delete_document(session, tenant.organization.id, document_id)
    except documents_service.DocumentNotFoundError as error:
        raise _no_encontrado() from error
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
