"""Pruebas de la recuperacion de documentos de contexto.

## Qué se comprueba y por qué

La recuperacion decide **qué reglas del cliente** entran en el prompt. Un fallo aqui no es un
fallo de busqueda: son reglas de negocio equivocadas en una respuesta, y el usuario no tiene
forma de saber que no eran las suyas.

Por eso la prueba de aislamiento es la mas importante del fichero, y la de ordenacion la
segunda: un `ILIKE` que devuelve todo lo que menciona la palabra es peor que uno que no
devuelve nada, porque el modelo lo toma como contexto fiable.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.knowledge.documents import (
    KnowledgeDocTypeEnum,
    WorkspaceKnowledgeDocument,
)
from backend.apps.knowledge.okf import parse_okf
from backend.apps.knowledge.retrieval import (
    extraer_terminos,
    recuperar_documentos,
)
from backend.core.database import AsyncSessionLocal
from backend.core.security import hash_password

pytestmark = pytest.mark.asyncio


@asynccontextmanager
async def _sesion() -> AsyncIterator[AsyncSession]:
    sesion = AsyncSessionLocal()
    try:
        yield sesion
    finally:
        await sesion.close()


async def _tenant(session: AsyncSession, prefijo: str) -> uuid.UUID:
    from backend.apps.organizations.models import (
        Membership,
        Organization,
        RoleEnum,
        User,
    )

    suffix = uuid.uuid4().hex
    organization = Organization(
        name=f"{prefijo} {suffix}", slug=f"{prefijo}-{suffix}"
    )
    user = User(
        email=f"{prefijo}-{suffix}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Cliente",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(
        Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.ADMIN)
    )
    await session.commit()
    return organization.id


def _okf(*, tipo: str, titulo: str, descripcion: str, cuerpo: str) -> str:
    return f'---\ntype: {tipo}\ntitle: "{titulo}"\ndescription: "{descripcion}"\n---\n\n{cuerpo}\n'


async def _guardar(
    session: AsyncSession, organization_id: uuid.UUID, contenido: str, titulo: str
) -> WorkspaceKnowledgeDocument:
    documento = WorkspaceKnowledgeDocument(
        organization_id=organization_id,
        title=titulo,
        doc_type=KnowledgeDocTypeEnum.DOCUMENTATION,
        content=contenido,
    )
    session.add(documento)
    await session.commit()
    return documento


# --------------------------------------------------------------------------- #
# Los terminos
# --------------------------------------------------------------------------- #


async def test_descarta_los_terminos_cortos_y_las_palabras_vacias() -> None:
    """"de" y "la" no buscan nada. "id" tampoco: sale en la mitad de los documentos.

    Sin este filtro, una consulta como "Revisa los id de la tabla" devolveria todo el
    knowledge base, y los tres documentos recuperados no tendrian nada que ver con la pregunta.
    """

    terminos = extraer_terminos("Revisa los id de la tabla de autenticacion")
    assert "autenticacion" in terminos
    assert "tabla" in terminos
    assert "los" not in terminos
    assert "la" not in terminos
    assert "id" not in terminos


async def test_parte_un_termino_por_guion() -> None:
    """`api-gateway` tambien sirve como `api` y `gateway`.

    Es lo que hace que una consulta en español encuentre un documento que escribe
    `authorization` y uno que escribe `api_spec`: los terminos se parten y se comparan por
    partes, no enteros.
    """

    terminos = extraer_terminos("revisa el api-gateway")
    assert "api-gateway" in terminos or "apigateway" in terminos
    assert "gateway" in terminos


# --------------------------------------------------------------------------- #
# La recuperacion
# --------------------------------------------------------------------------- #


async def test_recupera_el_documento_que_trata_el_tema() -> None:
    async with _sesion() as session:
        organization_id = await _tenant(session, "rag")
        await _guardar(
            session,
            organization_id,
            _okf(
                tipo="business_rule",
                titulo="Politica de autenticacion",
                descripcion="Como se validan las sesiones de usuario.",
                cuerpo="Las sesiones duran 30 minutos y se renuevan con un refresh token.",
            ),
            "Politica de autenticacion",
        )
        await _guardar(
            session,
            organization_id,
            _okf(
                tipo="architecture",
                titulo="Diagrama de servicios",
                descripcion="Como se reparten los servicios.",
                cuerpo="Cada servicio tiene su propia base de datos y su cola.",
            ),
            "Diagrama de servicios",
        )
        recuperados = await recuperar_documentos(
            session, organization_id, "como se validan las sesiones"
        )
    assert len(recuperados) == 1
    assert recuperados[0].title == "Politica de autenticacion"


async def test_el_titulo_manda_sobre_una_mencion_de_paso() -> None:
    """Un documento **titulado** sobre el tema gana a uno que lo menciona una vez.

    Este es el motivo de que la puntuacion pese por zona. Con un `ILIKE` plano, el documento
    largo que menciona "sesion" tres veces ganaria a uno de ocho lineas que trata el tema
    entero, y el modelo receberia el contexto equivocado como si fuera el bueno.
    """

    async with _sesion() as session:
        organization_id = await _tenant(session, "rag")
        # Mencion de paso: mucho texto y una sola aparicion del termino.
        await _guardar(
            session,
            organization_id,
            _okf(
                tipo="architecture",
                titulo="Catalogo de servicios",
                descripcion="Lista de servicios del monorepo.",
                cuerpo=("Sellado y empaquetado. " * 20)
                + "La sesion se abre en el borde.\n"
                + ("Sello de version. " * 20),
            ),
            "Catalogo de servicios",
        )
        await _guardar(
            session,
            organization_id,
            _okf(
                tipo="business_rule",
                titulo="Ciclo de sesion",
                descripcion="Duracion y renovacion de la sesion.",
                cuerpo="La sesion dura 30 minutos.",
            ),
            "Ciclo de sesion",
        )
        recuperados = await recuperar_documentos(
            session, organization_id, "duracion de la sesion"
        )
    assert recuperados[0].title == "Ciclo de sesion"


async def test_aislamiento_entre_tenants() -> None:
    """Las reglas de un tenant **no** llegan al prompt de otro.

    Es la prueba mas importante del fichero. Un fallo aqui no se ve como un identificador raro
    en una lista: el modelo responderia usando las reglas de negocio de otro cliente, y ni el
    usuario ni el operador tendrian forma de notarlo.
    """

    async with _sesion() as session:
        ajeno = await _tenant(session, "ajeno")
        propio = await _tenant(session, "propio")
        await _guardar(
            session,
            ajeno,
            _okf(
                tipo="business_rule",
                titulo="Politica de precios interna",
                descripcion="Los precios de lista del competidor son estos.",
                cuerpo="El competidor cobra 12 al mes y no incluye soporte.",
            ),
            "Politica de precios interna",
        )
        recuperados = await recuperar_documentos(
            session, propio, "cual es la politica de precios"
        )
    assert recuperados == []


async def test_una_consulta_sin_terminos_devuelve_nada() -> None:
    """Una pregunta de dos palabras sin contenido no recupera documentos.

    Recuperar "los documentos" porque aparecen en todos ellos no aporta nada y desplaza el
    contexto real.
    """

    async with _sesion() as session:
        organization_id = await _tenant(session, "rag")
        await _guardar(
            session,
            organization_id,
            _okf(
                tipo="concept",
                titulo="Titulo",
                descripcion="Descripcion.",
                cuerpo="Cuerpo con palabras.",
            ),
            "Algún título",
        )
        recuperados = await recuperar_documentos(session, organization_id, "de la")
    assert recuperados == []


async def test_limita_a_top_k() -> None:
    """Se devuelven los que pide, ni uno más.

    El limite no es decorativo: cada documento recuperado ocupa contexto, y un prompt con veinte
    documentos de reglas desplaza a la pregunta, que es lo que el usuario ha escrito.
    """

    async with _sesion() as session:
        organization_id = await _tenant(session, "rag")
        for indice in range(5):
            await _guardar(
                session,
                organization_id,
                _okf(
                    tipo="business_rule",
                    titulo=f"Regla de despliegue {indice}",
                    descripcion="Regla sobre el despliegue.",
                    cuerpo=f"Regla {indice} del despliegue en produccion.",
                ),
                f"Regla de despliegue {indice}",
            )
        recuperados = await recuperar_documentos(
            session, organization_id, "reglas de despliegue", top_k=2
        )
    assert len(recuperados) == 2


async def test_un_documento_sin_okf_no_rompe_la_consulta() -> None:
    """Un documento mal formado no hace fallar la recuperacion.

    Puede haber documentos guardados antes de que existiera el formato, o pegados sin
    frontmatter. Un `422` al recuperar el contexto dejaria el chat entero sin responder por un
    documento, que es la peor relacion entre un problema pequeno y una consecuencia grande.
    """

    async with _sesion() as session:
        organization_id = await _tenant(session, "rag")
        await _guardar(
            session,
            organization_id,
            "Sin frontmatter, pero menciona la sesion de usuario en una linea.",
            "Texto suelto",
        )
        await _guardar(
            session,
            organization_id,
            _okf(
                tipo="business_rule",
                titulo="Sesion de usuario",
                descripcion="Como funciona.",
                cuerpo="La sesion dura 30 minutos.",
            ),
            "Sesion de usuario",
        )
        recuperados = await recuperar_documentos(session, organization_id, "sesion de usuario")
    # Los dos se recuperan, y el bien formado va primero. El mal formado participar es
    # correcto: menciona la sesion de paso, asi que debe aparecer, pero puntuado por
    # debajo del que trata el tema.
    assert recuperados[0].title == "Sesion de usuario"
    assert len(recuperados) == 2


async def test_el_bloque_de_contexto_cita_el_documento() -> None:
    """El bloque inyectado en el prompt dice de qué documento viene.

    Un contexto sin fuente es contexto en el que el usuario no puede dudar, y un usuario que
    no puede dudar de la respuesta la acaba aceptando aunque sea falsa. La cita es lo que
    permite decir "esto lo dice tu documento de autenticacion".
    """

    documento = parse_okf(
        _okf(
            tipo="business_rule",
            titulo="Politica de autenticacion",
            descripcion="Como se validan las sesiones.",
            cuerpo="Las sesiones duran 30 minutos.",
        )
    )
    assert documento.title == "Politica de autenticacion"

    from backend.apps.knowledge.retrieval import RetrievedDocument

    bloque = RetrievedDocument(
        document_id=uuid.uuid4(),
        title=documento.title,
        doc_type=documento.doc_type.value,
        description=documento.description,
        body=documento.body,
        score=1.0,
        matched_terms=("sesion",),
    ).as_context_block()
    assert "Politica de autenticacion" in bloque
    assert "Las sesiones duran 30 minutos" in bloque
    # El frontmatter no se cuela en el contexto.
    assert "type: business_rule" not in bloque
