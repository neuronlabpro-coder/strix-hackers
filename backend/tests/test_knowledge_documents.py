"""Pruebas de la base de conocimiento corporativa.

## Qué se comprueba y por qué

Cada prueba ataca un fallo concreto que esta tabla cometera si se escribiera sin pensar:

- El listado y la lectura filtran por organización **siempre**, no cuando el llamador se
  acuerda.
- Un documento de otro workspace da `404` y no `403`, porque un `403` confirma que existe.
- El borrado va contra la fila ya filtrada, y no con un `DELETE` por identificador.
- El listado devuelve cabecera y no cuerpo, para que una tabla con veinte especificaciones de
  API no pese megabytes.
- Un `doc_type` desconocido es un `422` y no un documento guardado con otro nombre.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.knowledge.documents import (
    KnowledgeDocTypeEnum,
    WorkspaceKnowledgeDocument,
)
from backend.apps.organizations.models import Membership, Organization, RoleEnum, User
from backend.core.database import AsyncSessionLocal
from backend.core.security import create_access_token, hash_password
from backend.main import app

BASE = "/api/v1/knowledge/documents"


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def _sesion() -> AsyncIterator[AsyncSession]:
    """Sesión propia contra la base de integración, cerrada al terminar.

    Se abre propia en vez de usar el fixture `integration_session` porque estas pruebas hacen
    `commit`, y el fixture comparte la sesión con la aplicación en modo `savepoint`: leer un
    atributo de una instancia de ORM después de un `commit` ajeno lanza `MissingGreenlet`.

    El `close()` del `finally` no es opcional. Sin él, la conexión vuelve al pool cuando el
    ciclo de eventos ya está cerrado y cada prueba deja un `SAWarning` que ensucia la salida de
    toda la suite. Un aviso que aparece veinte veces es un aviso que nadie lee, y por eso acaba
    tapando los que sí importan.
    """

    sesion = AsyncSessionLocal()
    try:
        yield sesion
    finally:
        await sesion.close()


async def _tenant(session: AsyncSession, prefijo: str) -> tuple[Organization, str]:
    """Un workspace con su admin, y el **JWT** de ese admin.

    Devuelve un token de acceso y no un token de API a propósito. Los documentos de contexto
    son lo más sensible que un cliente puede escribir en la plataforma, y esta ruta va por
    `get_current_tenant`, que solo acepta el JWT de la sesión del panel. Que un token de API no
    llegue aquí es una decisión, no una limitación: las reglas de negocio del cliente no se
    exponen a integraciones.

    La organización sale del propio token y del `X-Organization-Id` que el panel manda, que se
    valida contra la membresía. Por eso las pruebas de aislamiento crean **dos** workspaces en
    lugar de mandar dos cabeceras con la misma credencial.
    """

    suffix = uuid.uuid4().hex
    organization = Organization(
        name=f"{prefijo} {suffix}", slug=f"{prefijo}-{suffix}"
    )
    user = User(
        email=f"{prefijo}-{suffix}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name=f"Cliente de {prefijo}",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(
        Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.ADMIN)
    )
    await session.commit()
    return organization, create_access_token({"sub": str(user.id)})


def _cabeceras(token: str, organization_id: uuid.UUID) -> dict[str, str]:
    """Las dos cabeceras que manda el panel.

    El JWT dice **quién** es; el `X-Organization-Id` dice **en qué workspace** opera, y el
    backend lo comprueba contra la membresía antes de dejar pasar nada. Un identificador de
    otro tenant no da acceso a nada: produce el mismo `403` que no mandar nada.
    """

    return {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": str(organization_id),
    }


def _cliente(token: str, organization_id: uuid.UUID) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=_cabeceras(token, organization_id),
    )


async def _crear(
    session: AsyncSession,
    organization_id: uuid.UUID,
    titulo: str = "Regla",
    doc_type: KnowledgeDocTypeEnum = KnowledgeDocTypeEnum.BUSINESS_RULE,
    contenido: str = "El endpoint de solo lectura no es un fallo.",
) -> WorkspaceKnowledgeDocument:
    documento = WorkspaceKnowledgeDocument(
        organization_id=organization_id,
        title=titulo,
        doc_type=doc_type,
        content=contenido,
    )
    session.add(documento)
    # `commit`, no `flush`. La petición la resuelve **otra** sesión —la de la aplicación— y lo
    # que esta sesión no confirma no existe para ella. Un `flush` aquí daría un `404` en las
    # pruebas que parece un fallo de aislamiento y no lo es.
    await session.commit()
    return documento


# --------------------------------------------------------------------------- #
# Alta
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_crea_un_documento_y_lo_devuelve_con_su_contenido() -> None:
    async with _sesion() as session:
        organization, token = await _tenant(session, "docs")
        async with _cliente(token, organization.id) as c:
            respuesta = await c.post(
                BASE,
                json={
                    "title": "Reglas de negocio",
                    "doc_type": "BUSINESS_RULE",
                    "content": "El campo `legacy_id` es un marcador fijo, no un identificador.",
                },
            )
    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    # El contenido vuelve en la respuesta a propósito: quien acaba de escribir varios miles de
    # líneas quiere comprobar que se han guardado tal cual, no recibir un id y releerlo.
    assert cuerpo["content"].startswith("El campo")
    assert cuerpo["doc_type"] == "BUSINESS_RULE"
    assert "organization_id" not in cuerpo


@pytest.mark.asyncio
async def test_rechaza_un_tipo_de_documento_inexistente() -> None:
    """Un `doc_type` desconocido es un `422`, no un documento guardado con otro nombre.

    Guardarlo con el texto tal cual parecería funcionar y volvería imposible filtrar por tipo
    después: el usuario vería sus documentos y el filtro no encontraría ninguno.
    """

    async with _sesion() as session:
        organization, token = await _tenant(session, "docs")
        async with _cliente(token, organization.id) as c:
            respuesta = await c.post(
                BASE,
                json={"title": "X", "doc_type": "POLITICA", "content": "algo"},
            )
    assert respuesta.status_code == 422


@pytest.mark.asyncio
async def test_rechaza_un_contenido_vacio() -> None:
    """Un documento sin contenido no es un documento: no hay nada que indexar."""

    async with _sesion() as session:
        organization, token = await _tenant(session, "docs")
        async with _cliente(token, organization.id) as c:
            respuesta = await c.post(
                BASE,
                json={"title": "Vacio", "doc_type": "DOCUMENTATION", "content": "   "},
            )
    assert respuesta.status_code == 422


# --------------------------------------------------------------------------- #
# Listado
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_el_listado_no_devuelve_el_contenido() -> None:
    """La cabecera y el cuerpo son cosas distintas, y la tabla no necesita el cuerpo.

    Una especificación de OpenAPI son miles de líneas. Si el listado las trae, veinte
    documentos son varios megabytes para pintar una tabla de tres columnas.
    """

    async with _sesion() as session:
        organization, token = await _tenant(session, "docs")
        documento = await _crear(session, organization.id, "Politica de retention")
        async with _cliente(token, organization.id) as c:
            respuesta = await c.get(BASE)
    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["total"] == 1
    assert cuerpo["documents"][0]["id"] == str(documento.id)
    assert "content" not in cuerpo["documents"][0]


@pytest.mark.asyncio
async def test_el_listado_filtra_por_tipo() -> None:
    async with _sesion() as session:
        organization, token = await _tenant(session, "docs")
        await _crear(session, organization.id, "Regla")
        await _crear(
            session,
            organization.id,
            "OpenAPI",
            KnowledgeDocTypeEnum.API_SPEC,
            "openapi: 3.1.0",
        )
        async with _cliente(token, organization.id) as c:
            respuesta = await c.get(f"{BASE}?doc_type=API_SPEC")
    cuerpo = respuesta.json()
    assert cuerpo["total"] == 1
    assert cuerpo["documents"][0]["title"] == "OpenAPI"


@pytest.mark.asyncio
async def test_un_workspace_no_ve_los_documentos_de_otro() -> None:
    """El aislamiento se comprueba siempre, no cuando el llamador se acuerda.

    Es la razón de que el filtro esté dentro del servicio y no sea un `where` opcional: un
    listado sin filtro de organización es el que filtra datos de un tenant el día que alguien
    añade un parámetro y olvida la condición.
    """

    async with _sesion() as session:
        ajeno, _token_ajeno = await _tenant(session, "ajeno")
        propio, token_propio = await _tenant(session, "propio")
        await _crear(session, ajeno.id, "Secreto del otro")
        async with _cliente(token_propio, propio.id) as c:
            respuesta = await c.get(BASE)
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["total"] == 0
    assert cuerpo["documents"] == []


# --------------------------------------------------------------------------- #
# Lectura
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_lee_el_contenido_completo() -> None:
    async with _sesion() as session:
        organization, token = await _tenant(session, "docs")
        documento = await _crear(session, organization.id)
        async with _cliente(token, organization.id) as c:
            respuesta = await c.get(f"{BASE}/{documento.id}")
    assert respuesta.status_code == 200
    assert respuesta.json()["content"].startswith("El endpoint")


@pytest.mark.asyncio
async def test_leer_un_documento_ajeno_da_404_y_no_403() -> None:
    """`404`, no `403`.

    Un `403` confirma que el documento existe: basta enumerar identificadores y recibir una
    lista de los que existen para saber qué tiene el tenant vecino. El `404` no distingue "no
    existe" de "no es tuyo", que es lo único que no filtra.
    """

    async with _sesion() as session:
        ajeno, _token_ajeno = await _tenant(session, "ajeno")
        propio, token_propio = await _tenant(session, "propio")
        documento = await _crear(session, ajeno.id, "Privado")
        async with _cliente(token_propio, propio.id) as c:
            respuesta = await c.get(f"{BASE}/{documento.id}")
    assert respuesta.status_code == 404


# --------------------------------------------------------------------------- #
# Borrado
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_borra_solo_lo_suyo() -> None:
    async with _sesion() as session:
        organization, token = await _tenant(session, "docs")
        documento = await _crear(session, organization.id)
        async with _cliente(token, organization.id) as c:
            respuesta = await c.delete(f"{BASE}/{documento.id}")
    assert respuesta.status_code == 204
    restantes = (
        await session.execute(
            select(func.count(WorkspaceKnowledgeDocument.id)).where(
                WorkspaceKnowledgeDocument.id == documento.id
            )
        )
    ).scalar_one()
    assert restantes == 0


@pytest.mark.asyncio
async def test_borrar_un_documento_ajeno_da_404_y_no_lo_borra() -> None:
    """El borrado también filtra, y con la fila ya traída.

    Un `DELETE ... WHERE id = :id` sin filtro de organización borraría el documento de otro
    tenant si el identificador coincidiera. Por eso el servicio localiza primero con
    organización **e** id, y borra esa fila.
    """

    async with _sesion() as session:
        ajeno, _token_ajeno = await _tenant(session, "ajeno")
        propio, token_propio = await _tenant(session, "propio")
        documento = await _crear(session, ajeno.id, "No se toca")
        async with _cliente(token_propio, propio.id) as c:
            respuesta = await c.delete(f"{BASE}/{documento.id}")
    assert respuesta.status_code == 404
    sobrevivientes = (
        await session.execute(
            select(func.count(WorkspaceKnowledgeDocument.id)).where(
                WorkspaceKnowledgeDocument.id == documento.id
            )
        )
    ).scalar_one()
    assert sobrevivientes == 1
