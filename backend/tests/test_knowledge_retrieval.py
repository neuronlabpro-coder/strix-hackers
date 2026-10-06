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
    condicion_de_terminos,
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


async def test_los_comodines_de_la_consulta_se_buscan_literales() -> None:
    r"""Un `%` en la pregunta no devuelve el workspace entero.

    Es el mismo defecto de los listados, aqui con una consecuencia extra: la recuperacion decide
    que documentos entran en el prompt del chat. Un `ILIKE` sin escapar hace que un `%` escrito
    en la pregunta devuelva documentos que no tienen nada que ver, y el modelo los toma como
    contexto fiable. Es el peor sitio donde se cuela un filtro sin escape, porque lo que se cuela
    no es una fila de mas en una tabla: es una regla de negocio en la respuesta de otro tenant.

    ## Por que aquí se comprueba el `%` y no el `_`

    Porque el `_` **queda enmascarado por el troceado de términos**, y conviene decirlo en voz
    alta para que nadie busque un fallo donde no lo hay. `extraer_terminos` parte `web_app` en
    `web_app`, `web` y `app`, y las dos partes —`web` y `app`— también son subcadena de
    `webXapp`. Así que el documento `webXapp` entra de todos modos por las partes, con el `_`
    escapado o sin él: el resultado observable es el mismo y el caso no discrimina.

    El `%` sí discrimina, y por eso es el que se comprueba: `100%%` no se trocea —el separador
    es el guion y el espacio— y llega entero al `ILIKE`. Sin escape, el comodín del término se
    come el resto del patrón y el resultado pasa a ser «cualquier título y cualquier cuerpo».

    Y por eso el término de la prueba es **`100%%` y no `100%`**: el troceado separa por espacio,
    así que de una pregunta `100%` sale un único término `100%` —y un comodín en medio del patrón
    no amplía la búsqueda por sí solo, porque los comodines que hacen eso son los de los extremos,
    que aquí los pone la función—, mientras que de `100%%` sale un término con dos comodines que
    sí se come el resto del patrón.

    ## Por qué la pregunta es solo `100%%` y no `descuento 100%%`

    Porque `extraer_terminos` devuelve **todos** los términos y la condición es un `or_`: si la
    pregunta incluye `descuento`, ese término casa con «Regla de descuentos» y el documento entra
    por la puerta de al lado, con o sin escape. Para que el fallo sea atribuible al comodín hace
    falta que la pregunta **no tenga ningún otro término que case**, y ese es el motivo de que la
    pregunta de la prueba sea el símbolo solo.

    ## Lo que esta prueba NO puede detectar, y por qué se documenta

    **Esta prueba pasa con el escape y sin él.** Se comprobó revirtiendo el `ILIKE` de
    `retrieval.py` a `f"%{termino}%"`: sigue en verde. Y hay que decirlo, porque si no este
    fichero daría a pensar que el escape está cubierto cuando no lo está.

    La razón es que `recuperar_documentos` **filtra dos veces**. La consulta trae los candidatos
    con el `ILIKE`, y luego `_puntuar` los repasa con un `in` de Python —subcadena literal— sobre
    título, descripción y cuerpo, y descarta todo lo que puntúe cero. Un término `100%%` no
    aparece literalmente en ningún documento, así que `_puntuar` lo tira igual, con o sin escape:
    el comodín amplía la consulta y el segundo filtro deshace la ampliación.

    ## Qué es lo que sí protege el escape aquí

    Una defensa que hoy no se ve y que depende de un detalle de otro sitio: que `_puntuar` siga
    usando `in` literal. Si alguien lo cambiara por un `re` para admitir búsqueda por palabras,
    el `ILIKE` sin escape empezaría a devolver documentos que `_puntuar` aceptaría, y el defecto
    que hoy es latente pasaría a ser real. El escape no sobra: es lo que deja de depender de ese
    otro filtro. Y su prueba es `test_el_escape_del_patron_de_la_recuperacion_lleva_escape`, que
    sí mira el SQL emitido.
    """

    async with _sesion() as session:
        organization_id = await _tenant(session, "comodin")
        await _guardar(
            session,
            organization_id,
            _okf(
                tipo="policy",
                titulo="Regla de descuentos",
                descripcion="Regla sobre descuentos",
                cuerpo="Los descuentos requieren aprobacion.",
            ),
            titulo="Regla de descuentos",
        )
        # Este documento **no lleva el símbolo**, pero sí lleva la cifra `100`. Es lo que hace
        # que la prueba tenga dientes: sin escape, el comodín del término se come el resto del
        # patrón y este documento entra igual, porque lo único que el patrón ya escapado exige
        # —los caracteres `100%` completos— aquí no aparece.
        await _guardar(
            session,
            organization_id,
            _okf(
                tipo="policy",
                titulo="Regla de rendimiento",
                descripcion="Regla sobre rendimiento",
                cuerpo="El limite es de 100 peticiones por segundo.",
            ),
            titulo="Regla de rendimiento",
        )
        await session.commit()

        por_porcentaje = await recuperar_documentos(session, organization_id, "100%%")
        por_texto = await recuperar_documentos(session, organization_id, "revisa descuentos")

    # El símbolo no aparece en ningún documento, así que la respuesta es **cero**. Ojo: esta
    # aserción también se cumple sin el escape del `ILIKE`, porque `_puntuar` vuelve a filtrar por
    # subcadena literal. Ver el docstring.
    assert por_porcentaje == [], [d.title for d in por_porcentaje]
    # Y la búsqueda normal sigue encontrando lo que tiene que encontrar: el escape no ha
    # roto el camino bueno.
    assert {d.title for d in por_texto} == {"Regla de descuentos"}


async def test_el_escape_del_patron_de_la_recuperacion_lleva_escape() -> None:
    """El SQL que sale de `recuperar_documentos` escapa el término y declara su `ESCAPE`.

    Esta es la prueba que **sí** cubre el escape, a diferencia de la anterior.

    ## Por qué hay que mirar el SQL y no el resultado

    Porque el resultado no lo delata. `recuperar_documentos` trae candidatos con el `ILIKE` y
    después `_puntuar` los repasa con un `in` de Python, que es subcadena literal: un término con
    comodines no aparece literalmente en ningún documento, así que acaba puntuando cero y
    descartándose. El `ILIKE` sin escape amplía la consulta y el segundo filtro deshace la
    ampliación —hoy. Se comprobó revirtiendo el `ILIKE` a `f"%{termino}%"`: la prueba de
    comportamiento sigue verde.

    ## Por qué el escape sigue siendo necesario aquí

    Porque esa doble filtración es una coincidencia de implementación, no una garantía. Si
    `_puntuar` pasara a buscar por palabras con expresiones regulares —que es lo que haría falta
    para suportar `web_app` y `camelCase` sin trocear— el `ILIKE` sin escape empezaría a devolver
    documentos que `_puntuar` aceptaría, y un `%` en la pregunta volvería a meter el workspace
    entero en el prompt del chat. El escape es lo que deja de depender de ese otro filtro.

    ## Por qué se comprueba el SQL compilado y no la consulta ejecutada

    Porque la consulta ejecutada devuelve la respuesta correcta con y sin escape, así que no
    distingue. El SQL compilado sí: el `ESCAPE` explícito y la barra invertida en el parámetro son
    la diferencia entre las dos versiones, y se leen en la cadena sin tener que confiar en el
    contenido de ninguna tabla.
    """

    async with _sesion() as session:
        organization_id = await _tenant(session, "escape-sql")
        await _guardar(
            session,
            organization_id,
            _okf(
                tipo="policy",
                titulo="Regla de descuentos",
                descripcion="Regla sobre descuentos",
                cuerpo="Los descuentos requieren aprobacion.",
            ),
            titulo="Regla de descuentos",
        )
        await session.commit()

        # Se llama a la condición **real** del servicio, no a una copia escrita aquí: una copia
        # en la prueba seguiría en verde con el servicio roto, que es justo el fallo que hay que
        # evitar.
        sql = str(
            condicion_de_terminos(extraer_terminos("100%%")).compile(
                compile_kwargs={"literal_binds": True}
            )
        )

    # El `ESCAPE` declarado, que es lo que le da sentido a la barra invertida del parámetro.
    assert "ESCAPE '\\'" in sql, sql
    # El parámetro con los dos comodines escapados. Sin esto, el patrón sería `%100%%`, que
    # casa con cualquier valor.
    assert "%100\\%\\%%" in sql, sql
    # Y no puede quedar ningún comodín sin escapar dentro del patrón.
    assert "'%100%%'" not in sql, sql


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


async def test_el_bloque_de_contexto_delimita_el_documento() -> None:
    """El bloque lleva marcas de inicio y fin inequívocas.

    Un encabezado Markdown no sirve como delimitador cuando hay varios documentos seguidos: un
    `###` puede ser el título de un documento o una subsección del anterior, y el modelo no
    tiene forma de saber cuál de las dos está leyendo. Con la marca explícita puede citar la
    fuente, y con las tres líneas de guiones sabe dónde acaba el cuerpo.

    Y el frontmatter no se cuela: es metadato del cliente, no contenido para el modelo.
    """

    documento = parse_okf(
        _okf(
            tipo="business_rule",
            titulo="Politica de autenticacion",
            descripcion="Como se validan las sesiones.",
            cuerpo="Las sesiones duran 30 minutos.",
        )
    )
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

    assert bloque.startswith("[DOCUMENTO OKF: Politica de autenticacion (BUSINESS_RULE)]")
    assert "Las sesiones duran 30 minutos." in bloque
    # La descripcion va entre la marca y el separador: es lo que se lee cuando el documento se
    # cita sin su cuerpo entero.
    assert bloque.index("Como se validan las sesiones.") < bloque.index("\n---\n")
    # El frontmatter no aparece en ninguna parte del bloque.
    assert "type: business_rule" not in bloque
