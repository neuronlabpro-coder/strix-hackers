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
from datetime import UTC, datetime

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
    *,
    created_at: datetime | None = None,
) -> WorkspaceKnowledgeDocument:
    documento = WorkspaceKnowledgeDocument(
        organization_id=organization_id,
        title=titulo,
        doc_type=doc_type,
        content=contenido,
        # `created_at` se puede fijar a mano porque la columna solo declara `server_default`, y
        # es lo unico que permite medir un rango de fechas sin depender del reloj. El rango que
        # se prueba es el de **alta**, no el de `updated_at`, que por eso no se toca aqui.
        **({} if created_at is None else {"created_at": created_at}),
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
async def test_el_listado_devuelve_la_descripcion_del_frontmatter() -> None:
    """La tarjeta necesita la descripción, y el listado no trae el `content`.

    Antes de esto, la vista hacía `extraerParaFormulario(documento.content)` sobre un listado que
    no devuelve `content`, y la pantalla reventaba con «Cannot read properties of undefined» en
    cuanto el workspace tenía **un** documento. No se vio porque el workspace de demostración no
    tenía ninguno.

    La descripción se extrae en el servidor, que es donde está el documento entero, y es la misma
    línea que el motor inyecta cuando cita el documento sin su cuerpo.
    """

    async with _sesion() as session:
        organization, token = await _tenant(session, "docs")
        await _crear(
            session,
            organization.id,
            "Politica de retention",
            contenido=(
                "---\ntype: business_rule\n"
                'title: "Politica de retention"\n'
                'description: "Los datos se conservan siete anos."\n'
                "---\n\nEl cuerpo."
            ),
        )
        async with _cliente(token, organization.id) as c:
            respuesta = await c.get(BASE)
    assert respuesta.status_code == 200, respuesta.text
    documento = respuesta.json()["documents"][0]
    assert documento["description"] == "Los datos se conservan siete anos."
    # Y el cuerpo sigue sin venir: la descripción no es el cuerpo.
    assert "content" not in documento


@pytest.mark.asyncio
async def test_un_documento_con_frontmatter_invalido_no_rompe_el_listado() -> None:
    """Un documento que no parsea deja la descripción vacía, pero el listado responde.

    `parse_okf` lanza `OkfError` con el frontmatter inválido, y un listado no puede dejar de
    responder por un documento guardado antes de que el formato fuera estricto. En ese caso la
    tarjeta enseña el título, que es lo que hay.
    """

    async with _sesion() as session:
        organization, token = await _tenant(session, "docs")
        await _crear(
            session,
            organization.id,
            "Legacy sin frontmatter",
            contenido="Esto no tiene valla de frontmatter, solo texto.",
        )
        async with _cliente(token, organization.id) as c:
            respuesta = await c.get(BASE)
    assert respuesta.status_code == 200, respuesta.text
    documento = respuesta.json()["documents"][0]
    assert documento["title"] == "Legacy sin frontmatter"
    assert documento["description"] == ""


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


# --------------------------------------------------------------------------- #
# Filtros del listado: texto y rango de fechas
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_el_buscador_encuentra_por_titulo_y_por_contenido() -> None:
    """El texto se busca en las dos columnas, y no solo en el título.

    Un documento de arquitectura se titula `payments` y dentro tiene la palabra `webhook`
    muchas veces. Quien busca es quien recuerda una frase que leyó, y esa frase casi nunca está
    en el título: buscar solo por título haría que el buscador pareciera roto la mitad de las
    veces.
    """

    async with _sesion() as session:
        organization, token = await _tenant(session, "docs")
        await _crear(
            session,
            organization.id,
            "Arquitectura de pagos",
            contenido="El webhook de reintento espera treinta segundos.",
        )
        await _crear(session, organization.id, "Regla de limites", contenido="Nada que ver.")
        async with _cliente(token, organization.id) as c:
            por_titulo = await c.get(f"{BASE}?query=pagos")
            por_contenido = await c.get(f"{BASE}?query=webhook")

    assert por_titulo.json()["total"] == 1
    assert por_titulo.json()["documents"][0]["title"] == "Arquitectura de pagos"
    assert por_contenido.json()["total"] == 1
    assert por_contenido.json()["documents"][0]["title"] == "Arquitectura de pagos"


@pytest.mark.asyncio
async def test_el_buscador_no_trata_el_porcentaje_como_comodin() -> None:
    """`?query=%` busca un símbolo de porcentaje, no devuelve la tabla entera.

    Es el fallo más silencioso que puede tener un buscador: la pantalla responde, enseña filas y
    el operador da por bueno un filtro que no ha filtrado nada. Con el escape de `LIKE`, un
    término que no aparece en ningún título no devuelve nada.
    """

    async with _sesion() as session:
        organization, token = await _tenant(session, "docs")
        await _crear(session, organization.id, "Sin porcentajes")
        async with _cliente(token, organization.id) as c:
            respuesta = await c.get(f"{BASE}?query=%25")

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["total"] == 0
    assert cuerpo["documents"] == []


@pytest.mark.asyncio
async def test_el_buscador_no_trata_el_subrayado_como_comodin() -> None:
    """`web_app` no puede devolver `webXapp`.

    El `_` es un comodín de un solo carácter en `LIKE`, y los títulos de documento llevan `_` con
    más frecuencia de lo que parece: `web_app`, `api_admin`, `campo_legacy`. Sin escape, buscar
    el nombre exacto devuelve también los nombres parecidos.
    """

    async with _sesion() as session:
        organization, token = await _tenant(session, "docs")
        await _crear(session, organization.id, "Modulo web_app")
        await _crear(session, organization.id, "Modulo webXapp")
        async with _cliente(token, organization.id) as c:
            respuesta = await c.get(f"{BASE}?query=web_app")

    cuerpo = respuesta.json()
    assert cuerpo["total"] == 1
    assert cuerpo["documents"][0]["title"] == "Modulo web_app"


@pytest.mark.asyncio
async def test_el_rango_de_altas_es_inclusivo_en_su_limite_superior() -> None:
    """«Del 1 al 3» son tres días, y el tercero entra entero.

    El corte superior es la medianoche **del día siguiente**, no la del propio `created_to`. Con
    un `<=` contra la medianoche del día final, ese día solo aportaría los documentos de
    exactamente las 00:00, que además depende de la zona horaria de quien pregunta.
    """

    async with _sesion() as session:
        organization, token = await _tenant(session, "docs")
        await _crear(
            session,
            organization.id,
            "Dia uno",
            created_at=datetime(2026, 3, 1, 9, 0, tzinfo=UTC),
        )
        await _crear(
            session,
            organization.id,
            "Dia tres al final del dia",
            created_at=datetime(2026, 3, 3, 23, 59, tzinfo=UTC),
        )
        await _crear(
            session,
            organization.id,
            "Dia cuatro",
            created_at=datetime(2026, 3, 4, 0, 1, tzinfo=UTC),
        )
        async with _cliente(token, organization.id) as c:
            respuesta = await c.get(f"{BASE}?created_from=2026-03-01&created_to=2026-03-03")

    cuerpo = respuesta.json()
    assert cuerpo["total"] == 2
    titulos = {d["title"] for d in cuerpo["documents"]}
    assert titulos == {"Dia uno", "Dia tres al final del dia"}


@pytest.mark.asyncio
async def test_un_rango_invertido_devuelve_lista_vacia_y_no_un_422() -> None:
    """Del 5 al 1 son cero documentos, no un error de validación.

    Las dos condiciones son incompatibles por construcción, así que la lista vacía **es** la
    respuesta correcta. Un `422` obligaría al panel a manejar un estado de error que solo se da
    cuando alguien se equivoca escribiendo las fechas, y ese estado no aporta nada.
    """

    async with _sesion() as session:
        organization, token = await _tenant(session, "docs")
        await _crear(
            session,
            organization.id,
            "Dia tres",
            created_at=datetime(2026, 3, 3, 12, 0, tzinfo=UTC),
        )
        async with _cliente(token, organization.id) as c:
            respuesta = await c.get(f"{BASE}?created_from=2026-03-05&created_to=2026-03-01")

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["total"] == 0
    assert cuerpo["documents"] == []


@pytest.mark.asyncio
async def test_la_paginacion_devuelve_limit_y_offset_que_aplico_el_servidor() -> None:
    """La barra de paginación no puede inventar el tamaño de página.

    `limit` y `offset` viajan en la respuesta porque el componente que dibuja el resumen los
    necesita y no los puede adivinar: si los calculara el cliente, cada pantalla tendría su
    propio número y alguno no coincidiría con lo que el servidor realmente hizo.
    """

    async with _sesion() as session:
        organization, token = await _tenant(session, "docs")
        await _crear(session, organization.id, "Uno")
        await _crear(session, organization.id, "Dos")
        async with _cliente(token, organization.id) as c:
            respuesta = await c.get(f"{BASE}?limit=1&offset=1")

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["limit"] == 1
    assert cuerpo["offset"] == 1
    assert cuerpo["total"] == 2
    assert len(cuerpo["documents"]) == 1


@pytest.mark.asyncio
async def test_el_buscador_no_atraviesa_el_aislamiento_del_workspace() -> None:
    """El texto no es una puerta trasera al filtro de organización.

    El `organization_id` es la **primera** condición de la consulta, antes que el término. Al
    revés, un término que casara con el título de un documento ajeno lo devolvería, porque el
    `or_` del texto entraría sin el filtro del tenant detrás.
    """

    async with _sesion() as session:
        ajeno, _token_ajeno = await _tenant(session, "ajeno")
        propio, token_propio = await _tenant(session, "propio")
        await _crear(session, ajeno.id, "Saldo corporativo")
        async with _cliente(token_propio, propio.id) as c:
            respuesta = await c.get(f"{BASE}?query=Saldo")

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["total"] == 0
    assert cuerpo["documents"] == []


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
