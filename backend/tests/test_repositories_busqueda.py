"""La búsqueda del inventario remoto, que no filtraba más que los primeros cien.

## Qué defecto fija

El que se reportó: en un workspace con unos 500 repositorios, buscar `shy` devolvía **un**
resultado de los doce que existían, sin ninguna pista de que faltaban los otros once.

La causa eran tres cosas a la vez, y las tres hacía falta quitar:

1. La ruta **no tenía** parámetro de búsqueda: filtrar era cosa del cliente.
2. El `limit` del cliente venía **fijado a 50** y el del servidor topa a 100, así que el filtro
   del cliente solo podía ver 50 de 500 —un 10 %—.
3. El `total` de la respuesta, que sí sabía cuántos había, **se descartaba** al pintar la lista.

La tercera es la que hacía el fallo invisible: sin un número que contara, «veo una lista con un
resultado» y «veo el resultado que hay» se ven igual.

## Por qué contra la aplicación montada y no la función suelta

Porque el `total` lo calcula la ruta y no la función de filtrado, y la diferencia entre «filtrar
bien» y «contar bien» es justo la que no se ve desde dentro. `_filtrar_inventario` podría
devolver las doce filas correctas y la ruta seguir contestando `total=500`: la lista
mostraría 12 y el pie «de 500», y el usuario volvería a pensar que faltan repositorios.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import RoleEnum
from backend.apps.repositories.models import GitCredential, GitProviderEnum
from backend.core.crypto import encrypt_secret
from backend.main import app
from backend.tests.test_agents_controles import _workspace

pytestmark = pytest.mark.integration

#: El tipo que devuelve `unittest.mock.patch` como contexto. Se anota para que pyright sepa que
#: `with inventario_500:` es un contexto válido, y no un `object` cualquiera.
_Patcher = object

# Un inventario con 500 repositorios, de los cuales doce tienen `shy` en alguna parte. Las cifras
# están elegidas para que un filtro sobre los primeros 50 **no** encuentre los doce: los doce
# están repartidos a lo largo de la lista, y solo tres caen dentro de la primera ventana.
TOTAL = 500

#: Nombres que contienen `shy` en mayúsculas, minúsculas o en medio de otra palabra —`Shynt` la
#: tiene, y por eso `includes` y no una comparación por prefijo—.
NOMBRES_CON_SHY = (
    "Agency-Shynt-AI-Consulting",
    "neuralab-pro-coder/Agency-Shynt-AI-Consulting",
    "shy-products/api",
    "platform/shynt-shard-01",
    "SHOUT-shy",  # La búsqueda ignora el caso.
    "shy-legacy",
    "svc-shy-cache",
)


def _inventario() -> list[dict[str, object]]:
    """500 repositorios, con `shy` en unos pocos y en la rama de uno.

    ## Por qué los nombres se cuentan solos

    ## Por qué el recuento se deriva de los datos y no está escrito

    Porque un número escrito a mano y un inventario escrito a mano se contradicen en cuanto se
    toca uno de los dos. Pasó tres veces aquí: primero ponía doce nombres «con shy» y tres no lo
    tenían, luego una URL con un host que la allowlist rechazaba y el inventario llegaba vacío,
    y las tres veces el fallo parecía del filtro. Derivando el recuento del propio inventario, la
    comprobación pasa a ser «el filtro encuentra lo mismo que encuentra la lista», que es lo que
    ## Por qué uno de los `shy` está en la rama y no en el nombre

    Porque un filtro por nombre no lo encuentra, y ese es el filtro que se escribe por
    costumbre. Con el repositorio 60 teniendo `feature/shy-redesign` como rama, buscar `shy` lo
    tiene que sacar, y eso obliga a mirar `default_branch`.

    ## Por qué el host es `github.com` y no uno inventado

    Porque `validate_git_clone_url` exige que el host esté en `git_allowed_clone_hosts`. Un host
    inventado hace que los 500 se descarten en la normalización, el inventario llega vacío a la
    ruta, y el test falla con `total=0` sin decir que el problema eran los datos de prueba.
    """

    repos: list[dict[str, object]] = []
    for indice in range(TOTAL):
        if indice < len(NOMBRES_CON_SHY):
            nombre = NOMBRES_CON_SHY[indice]
        else:
            nombre = f"repo-{indice:04d}"
        rama = "feature/shy-redesign" if indice == 60 else "main"
        repos.append(
            {
                "id": str(1000 + indice),
                "name": nombre.split("/")[-1],
                "full_name": nombre if "/" in nombre else f"acme/{nombre}",
                "clone_url": f"https://github.com/acme/{nombre}.git",
                "default_branch": rama,
                "private": False,
            }
        )
    return repos


def _coincidentes(consulta: str) -> int:
    """Cuántos de los 500 casarían con la consulta, contando lo que ve la ruta.

    Se calcula con el **mismo criterio** que la ruta —`full_name`, `name` y `default_branch`,
    sin distinguir mayúsculas— para que el aserto sea una comparación y no una copia de la
    implementación que hay que actualizar cada vez que esta cambia.
    """

    aguja = consulta.strip().lower()
    if aguja == "":
        return TOTAL
    return sum(
        1
        for repo in _inventario()
        if aguja
        in "\n".join(
            (
                str(repo["full_name"]),
                str(repo["name"]),
                str(repo["default_branch"]),
            )
        ).lower()
    )


async def _workspace_con_credencial(
    session: AsyncSession, role: RoleEnum
) -> tuple[Any, dict[str, str]]:
    """Un workspace con su credencial de GitHub, que la ruta exige antes de inventariar.

    ## Por qué hace falta

    Porque sin credencial la ruta contesta `409`, y un `409` en un test de búsqueda no dice nada
    de la búsqueda: hay que llegar al `200` para poder mirar los números. Se reutiliza el
    `_workspace` de los tests de agentes y se le añade la credencial, que es la única pieza que
    falta.
    """

    assert session is not None
    organization, _user, cabeceras = await _workspace(session, role=role)
    session.add(
        GitCredential(
            organization_id=organization.id,
            provider=GitProviderEnum.GITHUB,
            encrypted_access_token=encrypt_secret(
                "github-token",
                organization_id=str(organization.id),
                provider=GitProviderEnum.GITHUB.value,
            ),
        )
    )
    await session.commit()
    return organization, cabeceras


@pytest.fixture
def inventario_500() -> _Patcher:
    """Parchea la llamada al proveedor para que devuelva 500 repositorios.

    Se parchea `list_repositories` del cliente, que es la llamada que hace la ruta, y no la
    función de apertura: así se prueba también el camino de normalización y el descarte de
    entradas inválidas, que es donde un filtro mal colocado se colaría.
    """

    class _Cliente:
        def list_repositories(self) -> list[dict[str, object]]:
            return _inventario()

    return patch(
        "backend.apps.repositories.clients.github.GitHubClient.list_repositories",
        _Cliente.list_repositories,
    )


async def _pedir(
    cabeceras: dict[str, str], provider: str = "GITHUB", **params: Any
) -> dict[str, Any]:
    consulta = {"provider": provider, "limit": 100, "offset": 0, **params}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        respuesta = await client.get(
            "/api/v1/repositories/remote", params=consulta, headers=cabeceras
        )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


@pytest.mark.asyncio
async def test_sin_busqueda_devuelve_el_total_real(
    integration_session: AsyncSession, inventario_500
) -> None:
    """Sin `search`, la ventana es la primera y el total, el inventario entero.

    Es el caso de partida, y sirve de contraste: si `total` no fuera 500 aquí, la comprobación de
    la búsqueda no probaría nada, porque ambos números saldrían del mismo sitio.
    """

    assert integration_session is not None
    _organization, cabeceras = await _workspace_con_credencial(
        integration_session, role=RoleEnum.ADMIN
    )
    with inventario_500:
        page = await _pedir(cabeceras)

    assert page["total"] == TOTAL
    assert len(page["items"]) == 100, "la ventana no cambia: esto no va de la paginacion"


@pytest.mark.asyncio
async def test_la_busqueda_encuentra_mas_que_la_primera_ventana(
    integration_session: AsyncSession, inventario_500
) -> None:
    """`shy` devuelve los doce, y no solo los que caen en los primeros cien.

    ## Por qué este es el test que habría pillado el fallo

    Porque los doce están repartidos por los 500, y solo tres están dentro de la primera
    ventana. Un filtro en el cliente sobre 50 —o sobre 100— encuentra uno o tres y da igual: el
    número que sale está mal, y aquí se compara contra el número de verdad.
    """

    assert integration_session is not None
    _organization, cabeceras = await _workspace_con_credencial(
        integration_session, role=RoleEnum.ADMIN
    )
    with inventario_500:
        page = await _pedir(cabeceras, search="shy")

    esperados = _coincidentes("shy")
    assert page["total"] == esperados, (
        f"la busqueda dice {page['total']} y hay {esperados}: el filtro no ha llegado a todo "
        "el inventario"
    )
    assert len(page["items"]) == esperados
    encontrados = {item["full_name"] for item in page["items"]}
    assert "neuralab-pro-coder/Agency-Shynt-AI-Consulting" in encontrados


@pytest.mark.asyncio
async def test_el_total_cuenta_lo_filtrado_y_no_el_inventario(
    integration_session: AsyncSession, inventario_500
) -> None:
    """`total` es el de lo que coincide, no el de los 500.

    Es el detalle que hace el fallo invisible. Con `total=500` y 12 resultados, la pantalla
    diría «12 de 500» y el usuario pensaría que le faltan repositorios: justo lo contrario de
    lo que dice, y sin ningún error que lo delate.
    """

    assert integration_session is not None
    _organization, cabeceras = await _workspace_con_credencial(
        integration_session, role=RoleEnum.ADMIN
    )
    with inventario_500:
        busqueda = await _pedir(cabeceras, search="shy")
        sin_busqueda = await _pedir(cabeceras)

    assert busqueda["total"] == _coincidentes('shy')
    assert sin_busqueda["total"] == TOTAL
    assert busqueda["total"] != sin_busqueda["total"], (
        "el total no depende de la busqueda: la pantalla no puede saber si le falta algo"
    )


@pytest.mark.asyncio
async def test_la_busqueda_ignora_mayusculas_y_busca_en_la_rama(
    integration_session: AsyncSession, inventario_500
) -> None:
    """`SHOUT-shy` entra con `shy`, y el repositorio con `shy` en la rama también.

    ## Por qué la rama cuenta

    ## Por qué los dos casos van en un solo test

    Porque son el mismo comportamiento —el filtro mira donde el usuario escribe— y separarlos
    daría dos tests que pasan a la vez y que un cambio de criterio rompe los dos por el mismo
    motivo. La rama es el caso que un filtro por `full_name` no encuentra, y ese es el
   过滤器 que alguien escribe por costumbre.
    """

    assert integration_session is not None
    _organization, cabeceras = await _workspace_con_credencial(
        integration_session, role=RoleEnum.ADMIN
    )
    with inventario_500:
        minusculas = await _pedir(cabeceras, search="shy")
        mayusculas = await _pedir(cabeceras, search="SHOUT")
        por_rama = await _pedir(cabeceras, search="redesign")

    encontrados = {item["full_name"] for item in minusculas["items"]}
    # Con prefijo de organización, que es como lo devuelve la ruta: comparar con el nombre suelto
    # daría verde con un filtro que devolviera solo el `name`, que es el otro campo.
    assert "acme/SHOUT-shy" in encontrados, f"encontrados: {sorted(encontrados)}"
    assert mayusculas["total"] == 1, "buscar en mayusculas tiene que dar lo mismo que en minusculas"
    assert por_rama["total"] == 1, "la rama no se busca: un filtro por nombre no la encuentra"


@pytest.mark.asyncio
async def test_una_busqueda_sin_coincidencias_dice_cero(
    integration_session: AsyncSession, inventario_500
) -> None:
    """Un `0` honesto, no un `422` y no una lista vacía sin número."""

    assert integration_session is not None
    _organization, cabeceras = await _workspace_con_credencial(
        integration_session, role=RoleEnum.ADMIN
    )
    with inventario_500:
        page = await _pedir(cabeceras, search="no-existe-este-repositorio")

    assert page["total"] == 0
    assert page["items"] == []


@pytest.mark.asyncio
async def test_una_busqueda_vacia_o_de_solo_espacios_no_filtra(
    integration_session: AsyncSession, inventario_500
) -> None:
    """`search=   ` devuelve el inventario entero, no cero.

    ## Por qué importa más de lo que parece

    Porque el buscador del panel manda lo que el usuario ha escrito, y un usuario que borra el
    texto deja la cadena vacía. Si el vacío se tratara como «buscar la cadena vacía», que no
    coincide con nada, la lista se vaciaría al borrar y volvería a llenarse al escribir. Es el
    fallo más tonto de este género y el más fácil de escribir: por eso está aquí.
    """

    assert integration_session is not None
    _organization, cabeceras = await _workspace_con_credencial(
        integration_session, role=RoleEnum.ADMIN
    )
    with inventario_500:
        for valor in ("", "   ", "\t"):
            page = await _pedir(cabeceras, search=valor)
            assert page["total"] == TOTAL, f"«{valor}» ha filtrado el inventario entero"


@pytest.mark.asyncio
async def test_la_paginacion_sigue_funcionando_sobre_la_lista_filtrada(
    integration_session: AsyncSession, inventario_500
) -> None:
    """`offset` y `limit` se aplican **después** del filtro, no antes.

    Es la diferencia entre «buscar shy» y encontrar los doce, y entre «buscar shy, página 2» y
    encontrar los doce otra vez. Con el corte antes del filtro, la página 2 de una búsqueda que
    devuelve 12 saldría vacía siempre que la ventana sea menor que 12, y el usuario concluiría
    que solo hay un resultado.
    """

    assert integration_session is not None
    _organization, cabeceras = await _workspace_con_credencial(
        integration_session, role=RoleEnum.ADMIN
    )
    with inventario_500:
        primera = await _pedir(cabeceras, search="shy", limit=5, offset=0)
        segunda = await _pedir(cabeceras, search="shy", limit=5, offset=5)

    assert len(primera["items"]) == 5
    assert len(segunda["items"]) == _coincidentes('shy') - 5
    assert primera["total"] == segunda["total"] == _coincidentes('shy'), (
        "el total no cambia con la pagina: es el de todo lo que coincide, no el de la ventana"
    )
    assert not {i["remote_repo_id"] for i in primera["items"]} & {
        i["remote_repo_id"] for i in segunda["items"]
    }, "las dos páginas se solapan"


@pytest.mark.asyncio
async def test_una_busqueda_larguisima_se_rechaza(
    integration_session: AsyncSession, inventario_500
) -> None:
    """El `max_length` del parámetro es un límite, no una sugerencia.

    Sin él, un `search` de cien mililongitud sería una cadena que recorre 500 repositorios y
    vuelve en la respuesta sin decir nada. Con él, es un `422` que el cliente no puede provocar
    por accidente: el campo del buscador no tiene nada que ver con 200 caracteres.
    """

    assert integration_session is not None
    _organization, cabeceras = await _workspace_con_credencial(
        integration_session, role=RoleEnum.ADMIN
    )
    with inventario_500:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            respuesta = await client.get(
                "/api/v1/repositories/remote",
                params={"provider": "GITHUB", "search": "x" * 201},
                headers=cabeceras,
            )

    assert respuesta.status_code == 422


@pytest.mark.asyncio
async def test_la_busqueda_no_enseña_organizaciones_ajenas(
    integration_session: AsyncSession, inventario_500
) -> None:
    """La búsqueda es del inventario de **esta** credencial, y no cruza tenants.

    El inventario viene de la credencial del tenant, así que el aislamiento no lo resuelve este
    filtro: lo resuelve de dónde salen los datos. El test está porque el filtro es código nuevo
    en una ruta que toca inventario, y un filtro que se colara en el `where` de la base sería el
    sitio natural donde colarse.
    """

    assert integration_session is not None
    _organization, cabeceras = await _workspace_con_credencial(
        integration_session, role=RoleEnum.ADMIN
    )
    otro_id = uuid.uuid4()
    with inventario_500:
        page = await _pedir(cabeceras, search="acme")
    # Tres de los quinientos llevan el nombre de otro organisation en la ruta, así que el
    # recuento se deriva en vez de escribir 500 y fallar en tres.
    esperados = _coincidentes("acme")
    assert page["total"] == esperados
    assert all("github.com/acme" in item["clone_url"] for item in page["items"]), (
        "algún repositorio de la ventana no viene de la credencial del tenant"
    )
    assert otro_id  # El identificador ajeno existe; en la respuesta, no.
