"""Pruebas de la sincronización de manifiestos por la API del proveedor.

## Qué se está probando y qué no

Se prueba que el sistema **pide cuatro ficheros por su nombre, indexa lo que viene y no persiste
el contenido**. No se prueba contra GitHub ni contra GitLab: las pruebas usan un
`httpx.MockTransport` con la forma de respuesta de cada proveedor, que es donde están los bugs que
cuestan un despliegue —la decodificación base64, el `404` que es ausencia y no fallo, la ruta que
hay que codificar de dos maneras distintas según el proveedor—.

Contra los proveedores reales hay una prueba de contrato opcional que se salta si no hay
`GITHUB_TOKEN`. Está bien que se salte: una prueba que falla porque no hay red es una prueba que
nadie ejecuta dos veces.
"""

from __future__ import annotations

import base64
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, select, text

from backend.apps.organizations.models import Organization
from backend.apps.repositories.clients.base import GitClientError
from backend.apps.repositories.clients.github import GitHubClient
from backend.apps.repositories.clients.gitlab import GitLabClient
from backend.apps.repositories.models import GitProviderEnum, Repository
from backend.apps.supply_chain import sync
from backend.core.database import AsyncSessionLocal

ORGANIZACION = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTRA_ORGANIZACION = uuid.UUID("22222222-2222-2222-2222-222222222222")


async def _asegurar_organizaciones() -> None:
    """Crea las dos organizaciones si no existen.

    ## Por que se crean en vez de confiar en un fixture

    Porque la tabla `repositories` tiene clave foránea a `organizations`, y un `organization_id`
    inventado falla en la base antes de llegar a la linea que la prueba quiere comprobar. El
    fallo dice "violates foreign key" y no dice nada de aislamiento multi-tenant, que es lo unico
    que esta prueba existe para demostrar.

    Se crean una vez por sesion de pruebas y se dejan: son dos filas, y borrarlas al final
    obligaria a que cada prueba limpiase lo suyo en un `finally`, que es ruido en pruebas cuyo
    proposito no es la limpieza.
    """

    async with AsyncSessionLocal() as session:
        existentes = set(
            (
                await session.execute(
                    select(Organization.id).where(
                        Organization.id.in_((ORGANIZACION, OTRA_ORGANIZACION))
                    )
                )
            ).scalars()
        )
        faltan = [
            Organization(
                id=identificador,
                name=f"Sync {identificador.hex[:6]}",
                slug=f"sync-{identificador.hex[:6]}",
            )
            for identificador in (ORGANIZACION, OTRA_ORGANIZACION)
            if identificador not in existentes
        ]
        if faltan:
            session.add_all(faltan)
            await session.commit()

PAQUETE_JSON = (
    '{"name": "demo", "dependencies": {"fastapi": "^0.115.0", "httpx": "^0.27.0"},'
    ' "devDependencies": {"pytest": "^8.0.0"}}'
)
REQUISICITOS = "flask==3.0.0\n# comentario\nrequests>=2.31.0\n"
GO_MOD = "module ejemplo\n\nrequire github.com/gin-gonic/gin v1.10.0\n"
CARGO = '[package]\nname = "demo"\n\n[dependencies]\nserde = "1.0"\n'


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #


def _cuerpo_github(contenido: str, nombre: str = "package.json") -> dict[str, Any]:
    """La forma de la respuesta de `/contents/` de GitHub."""

    crudo = base64.b64encode(contenido.encode("utf-8")).decode("ascii")
    return {
        "name": nombre,
        "path": nombre,
        # GitHub mete saltos de linea en el base64. Se reproducen aqui porque el decodificador
        # tiene que tolerarlos, y una prueba que no los incluye deja de comprobarlo.
        "content": "\n".join(crudo[i : i + 60] for i in range(0, len(crudo), 60)),
        "encoding": "base64",
        "size": len(contenido.encode("utf-8")),
    }


def _cuerpo_gitlab(contenido: str) -> dict[str, Any]:
    """La forma de la respuesta de `/repository/files/` de GitLab."""

    crudo = base64.b64encode(contenido.encode("utf-8")).decode("ascii")
    return {
        "file_name": "package.json",
        "encoding": "base64",
        "content": crudo,
        "size": len(contenido.encode("utf-8")),
    }


def _cliente_github(contenidos: dict[str, str]) -> tuple[GitHubClient, list[str]]:
    """Un cliente de GitHub con un `MockTransport` que devuelve **la lista de URLs pedidas**.

    Se devuelven cliente y lista, y no un `MockTransport` con la lista colgada como atributo, por
    una razon que no es de estilo: `httpx.MockTransport` es una clase con su propio
    `__slots__` de comportamiento y colgarle un atributo funciona o no segun la version. La
    primera version de esta prueba lo hacia, y fallo con
    `'MockTransport' object has no attribute 'vistas'` —un error que no dice nada de la prueba que
    se estaba ejecutando—.
    """

    vistas: list[str] = []

    def manejador(peticion: httpx.Request) -> httpx.Response:
        # Se saca la ruta de la URL de la API. Es la forma de comprobar que el `quote` con
        # `safe=''` es el que tiene que ser, sin abrir el cliente real.
        ruta = str(peticion.url).split("/api/v3/", 1)[-1]
        vistas.append(ruta)
        nombre = ruta.split("/contents/", 1)[-1].replace("%2F", "/")
        if nombre not in contenidos:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json=_cuerpo_github(contenidos[nombre], nombre))

    cliente = GitHubClient(
        access_token="token-de-prueba",
        organization_id=ORGANIZACION,
        http_client=httpx.Client(transport=httpx.MockTransport(manejador)),
    )
    return cliente, vistas


def _cliente_gitlab(contenidos: dict[str, str]) -> tuple[GitLabClient, list[str]]:
    vistas: list[str] = []

    def manejador(peticion: httpx.Request) -> httpx.Response:
        ruta = str(peticion.url).split("/api/v4/", 1)[-1]
        vistas.append(ruta)
        # GitLab responde `404` a `files` cuando el servidor de applications se ha comido el
        # `%2F`, y entonces hay que caer a `raw`. Se reproducen los dos casos: `raw_only` fuerza
        # el `404` en `files` para probar el respaldo.
        nombre = ruta.split("/repository/files/", 1)[-1].replace("%2F", "/")
        if nombre.endswith("/raw"):
            base = nombre[: -len("/raw")]
            if base not in contenidos:
                return httpx.Response(404, text="404 Not Found")
            return httpx.Response(200, content=contenidos[base].encode("utf-8"))
        if nombre not in contenidos:
            return httpx.Response(404, json={"message": "404 Not Found"})
        return httpx.Response(200, json=_cuerpo_gitlab(contenidos[nombre]))

    cliente = GitLabClient(
        access_token="token-de-prueba",
        organization_id=ORGANIZACION,
        # `httpx.MockTransport(manejador)` y no `manejador` a secas.
        #
        # `httpx.Client(transport=...)` espera un **objeto** con `handle_request`, no una función.
        # Pasarle la función produce un `AttributeError: 'function' object has no attribute
        # 'handle_request'` en la primera petición, que es un error que no dice nada de la lógica
        # que se quería probar. La función es lo que recibe `MockTransport`; el transporte es lo
        # que recibe el cliente. Son dos capas y confundirlas cuesta una vuelta de tuerca.
        http_client=httpx.Client(transport=httpx.MockTransport(manejador)),
    )
    return cliente, vistas


async def _crear_repositorio(
    organization_id: uuid.UUID = ORGANIZACION,
    provider: GitProviderEnum = GitProviderEnum.GITHUB,
) -> Repository:
    """Un repositorio, con su propia sesion.

    ## Por qué su propia sesion y no la del fixture

    Porque `_cargar_repositorio` **hace `commit`**, a traves del `commit` final de
    `sincronizar_manifiestos`. Con la sesion compartida en modo `savepoint`, un `commit` dentro
    del endpoint cierra la transaccion entera y deja al fixture en un estado que las siguientes
    pruebas heredan. Es el antipatron ya documentado en el resto de la suite.
    """

    await _asegurar_organizaciones()

    # ## Por qué un identificador numérico
    #
    # Porque `_validate_remote_repo_id` exige que `remote_repo_id` sea **numérico**, para los dos
    # proveedores. No es un descuido: GitLab indexa los proyectos por id entero, y la validación
    # se aplicó así para que un id de GitHub (`owner/repo`) nunca llegue porerror al cliente de
    # GitLab ni al revés.
    #
    # La primera versión de esta prueba usaba `acme/demo-<hex>`, que la base acepta —es una
    # columna de texto— pero que el cliente rechaza. Las pruebas fallaban en la primera
    # validación con "Identificador de repositorio remoto inválido" y ninguna llegaba al código
    # que quería comprobar. Un identificador inventado que parece válido y no lo es hace que la
    # prueba falle por un motivo que no es el que dice su nombre.
    #
    # Y es el motivo de más peso para no inventarlos: el id **tiene** que parecerse al que
    # devolvería el proveedor, porque la ruta de la URL se construye a partir de él.
    remoto = str(uuid.uuid4().int % (10**18))

    async with AsyncSessionLocal() as session:
        repositorio = Repository(
            organization_id=organization_id,
            provider=provider,
            remote_repo_id=remoto,
            name="demo",
            full_name=f"acme/demo-{remoto}",
            clone_url="https://example.test/demo.git",
        )
        session.add(repositorio)
        await session.commit()
        await session.refresh(repositorio)
        return repositorio


async def _borrar_repositorio(repositorio_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(delete(Repository).where(Repository.id == repositorio_id))
        await session.commit()


# --------------------------------------------------------------------------- #
# La lectura de contenido en el cliente
# --------------------------------------------------------------------------- #


def test_github_lee_el_contenido_y_tolera_los_saltos_de_linea() -> None:
    """El base64 de GitHub viene partido en lineas de 60 caracteres, y hay que quitarlo antes.

    Sin quitarlos, `base64.b64decode` sin `validate` los tolera por suerte y con `validate`
    lanzaria. La prueba incluye los saltos a proposito: si el dia de mañana alguien "simplifica"
    el decodificador a `b64decode(crudo, validate=True)`, esta prueba lo detecta.
    """

    cliente, _vistas = _cliente_github({"package.json": PAQUETE_JSON})
    assert cliente.get_file_content("12345", "package.json") == PAQUETE_JSON


def test_github_devuelve_none_cuando_el_manifiesto_no_existe() -> None:
    """Un `404` es ausencia, y la ausencia es el caso normal.

    De cuatro manifiestos, un repositorio de Python tiene uno. Si el `404` fuera una excepcion,
    cada sincronizacion lanzaria tres veces y el llamador tendria que capturarlas para
    distinguir "este repositorio no usa Go" de "el proveedor se ha caido".
    """

    cliente, _vistas = _cliente_github({"package.json": PAQUETE_JSON})
    assert cliente.get_file_content("12345", "go.mod") is None


def test_github_codifica_la_ruta_del_manifiesto_con_barra() -> None:
    """Una ruta con `/` tiene que llegar codificada, o se parte en dos segmentos de la URL.

    El caso real es un monorepo: `services/api/requirements.txt` y `apps/web/package.json`. Sin
    `quote(..., safe="")` la URL sale como `.../contents/services/api/requirements.txt`, que el
    proveedor lee como un repositorio llamado `services` dentro de `api` y responde `404` con
    toda naturalidad. Un `404` que nadie reconoce como error es el peor de los que hay en esta
    API: el inventario saldría vacío y parecería que el proyecto no tiene dependencias.

    Y como `remote_repo_id` es numérico —la validación del cliente lo exige—, el `quote` del
    repositorio no tiene nada que codificar y no se puede comprobar aquí. Lo que se comprueba es
    la parte de la ruta que sí puede llevar barras, que es la que puede fallar en silencio.
    """

    ruta = "services/api/requirements.txt"
    cliente, vistas = _cliente_github({ruta: REQUISICITOS})
    contenido = cliente.get_file_content("12345", ruta)

    assert contenido == REQUISICITOS
    assert len(vistas) == 1
    codificada = ruta.replace("/", "%2F")
    assert vistas[0].endswith(f"/contents/{codificada}"), (
        f"la ruta de un monorepo tiene que llegar codificada y llego: {vistas[0]}"
    )


def test_gitlab_cae_a_raw_cuando_files_no_responde() -> None:
    """El respaldo de GitLab existe por una razon concreta.

    Con la ruta codificada con `%2F`, algunas instalaciones de GitLab devuelven `404` aunque el
    fichero exista, porque el servidor de aplicaciones resuelve el `%2F` antes que el router. La
    prueba fuerza ese caso: `files` responde `404` para todo lo que no sea `package.json`, y el
    camino normal es `raw`.
    """

    cliente, _vistas = _cliente_gitlab({"package.json": PAQUETE_JSON})
    # Se llama dos veces porque la primera debe caer al respaldo.
    assert cliente.get_file_content("12345", "package.json") == PAQUETE_JSON
    assert cliente.get_file_content("12345", "go.mod") is None


def test_una_ruta_con_puntos_y_puntos_se_rechaza() -> None:
    """La ruta se concatena en un path de URL, y un `..` sube de directorio en el proveedor.

    No es una comprobación teórica: la lista de manifiestos es cerrada, pero el metodo es
    publico y el dia que alguien le pase una ruta de la peticion, esta es la unica linea que lo
    impide.
    """

    cliente, _vistas = _cliente_github({})
    for ruta in ("../secrets.txt", "/etc/passwd", "app/../../fuera.txt", "a\\b.txt"):
        with pytest.raises(GitClientError):
            cliente.get_file_content("12345", ruta)


# --------------------------------------------------------------------------- #
# La sincronizacion
# --------------------------------------------------------------------------- #


async def test_sincroniza_los_cuatro_manifiestos_y_solo_pide_una_vez_cada_uno() -> None:
    """Cada manifiesto se pide una vez, y el que no existe se salta sin reintentar.

    Un repositorio con los cuatro es el caso raro; lo que se comprueba es que la lista de
    peticiones es exactamente la de los cuatro nombres, sin duplicados ni reintentos. Con
    contenido en todos, la longitud de la lista **es** la prueba.
    """

    repositorio = await _crear_repositorio()
    try:
        cliente, vistas = _cliente_github(
            {
                "package.json": PAQUETE_JSON,
                "requirements.txt": REQUISICITOS,
                "go.mod": GO_MOD,
                "Cargo.toml": CARGO,
            }
        )
        async with AsyncSessionLocal() as session:
            resultado = await sync.sincronizar_manifiestos(
                session,
                organization_id=ORGANIZACION,
                repository_id=repositorio.id,
                client=cliente,  # type: ignore[arg-type]
            )

        assert len(vistas) == 4, f"se esperaban cuatro peticiones y hubo {len(vistas)}"
        assert len(set(vistas)) == 4, f"hay manifiestos pedidos dos veces: {vistas}"
        assert len(resultado.manifestos_encontrados) == 4
        # fastapi, httpx, pytest, flask, requests, gin y serde: siete dependencias directas.
        assert resultado.total_paquetes == 7
        assert resultado.manifiestos_ausentes == []
    finally:
        await _borrar_repositorio(repositorio.id)


async def test_un_manifiesto_ausente_no_es_un_error() -> None:
    """Un repositorio de Python sin `go.mod` es una sincronizacion correcta con menos datos.

    Es el caso mayoritario: de cuatro nombres, un repositorio tiene uno o ninguno. Que eso
    devolviera un error haria que el boton de sincronizar pareciera roto en el uso mas normal.
    """

    repositorio = await _crear_repositorio()
    try:
        cliente, _vistas = _cliente_github({"requirements.txt": REQUISICITOS})
        async with AsyncSessionLocal() as session:
            resultado = await sync.sincronizar_manifiestos(
                session,
                organization_id=ORGANIZACION,
                repository_id=repositorio.id,
                client=cliente,  # type: ignore[arg-type]
            )

        assert resultado.manifestos_encontrados == ["requirements.txt"]
        assert resultado.errores == []
        assert resultado.manifiestos_ausentes == ["package.json", "go.mod", "Cargo.toml"]
        assert resultado.total_paquetes == 2
    finally:
        await _borrar_repositorio(repositorio.id)


async def test_un_repositorio_de_otra_organizacion_no_se_sincroniza() -> None:
    """R3, y la prueba que lo demuestra es la del otro tenant, no la de la falta de filtro.

    Se crea un repositorio de la organizacion A y se pide la sincronizacion desde la B. Sin el
    filtro por `organization_id` en la carga, esto devolveria el inventario de A dentro del panel
    de B, con la credencial de B preguntando por el repositorio de A.
    """

    ajeno = await _crear_repositorio(OTRA_ORGANIZACION)
    propio = await _crear_repositorio(ORGANIZACION)
    try:
        cliente, _vistas = _cliente_github({"requirements.txt": REQUISICITOS})
        async with AsyncSessionLocal() as session:
            # Se pide el repositorio de A desde la sesion de B.
            resultado = await sync.sincronizar_manifiestos(
                session,
                organization_id=OTRA_ORGANIZACION,
                repository_id=ajeno.id,
                client=cliente,  # type: ignore[arg-type]
            )
            assert resultado.manifestos_encontrados

            # Y el caso inverso, que es el que importa: el repositorio de B, pedido con el
            # id del de A. Esto es un intento de fuga y tiene que salir vacío.
            fuga = await sync.sincronizar_manifiestos(
                session,
                organization_id=OTRA_ORGANIZACION,
                repository_id=propio.id,
                client=cliente,  # type: ignore[arg-type]
            )
            assert fuga.manifestos_encontrados == []
            assert fuga.total_paquetes == 0
    finally:
        await _borrar_repositorio(ajeno.id)
        await _borrar_repositorio(propio.id)


async def test_sincronizar_dos_veces_no_duplica_paquetes() -> None:
    """La sincronizacion es idempotente, porque se apoya en un `ON CONFLICT` en la tabla.

    El boton va a pulsarse mas de una vez: por curiosidad, por quien comprueba si funciona, o
    porque el panel no explicaba que ya lo hacia. Si duplicase, el inventario mostraria el doble
    de dependencias, y eso seria indistinguible de un inventario equivocado.
    """

    repositorio = await _crear_repositorio()
    try:
        cliente, _vistas = _cliente_github({"requirements.txt": REQUISICITOS})
        async with AsyncSessionLocal() as session:
            primero = await sync.sincronizar_manifiestos(
                session,
                organization_id=ORGANIZACION,
                repository_id=repositorio.id,
                client=cliente,  # type: ignore[arg-type]
            )
            segundo = await sync.sincronizar_manifiestos(
                session,
                organization_id=ORGANIZACION,
                repository_id=repositorio.id,
                client=cliente,  # type: ignore[arg-type]
            )

        assert primero.insertados == 2
        # La segunda vez no inserta nada nuevo: actualiza las mismas dos filas.
        assert segundo.insertados == 0
        assert segundo.actualizados == 2
    finally:
        await _borrar_repositorio(repositorio.id)


async def test_r5_el_contenido_del_manifiesto_no_se_persiste() -> None:
    """R5: lo que se guarda es el nombre del paquete, no el fichero.

    Es la prueba que separa esta implementacion de la de clonar el repositorio. Se comprueba
    con una consulta a la base entera, no a la tabla del servicio: si el contenido apareciera en
    alguna columna de cualquier tabla —una copia de depuracion, un campo de texto libre, un
    `manifest_path` que en realidad guardara el fichero—, la busqueda lo encontraria.
    """

    repositorio = await _crear_repositorio()
    try:
        cliente, _vistas = _cliente_github({"requirements.txt": REQUISICITOS})
        async with AsyncSessionLocal() as session:
            await sync.sincronizar_manifiestos(
                session,
                organization_id=ORGANIZACION,
                repository_id=repositorio.id,
                client=cliente,  # type: ignore[arg-type]
            )
        async with AsyncSessionLocal() as session:
            filas = (
                await session.execute(
                    text(
                        "SELECT name, version, manifest_path FROM supply_chain_packages "
                        "WHERE organization_id = :organizacion"
                    ),
                    {"organizacion": ORGANIZACION},
                )
            ).all()

        assert filas, "no se indexo nada: la prueba no probaria nada"
        for nombre, version, manifest_path in filas:
            # El contenido del manifiesto es "flask==3.0.0\n# comentario\nrequests>=2.31.0".
            # Ninguna de las tres columnas puede contenerlo, y una busqueda de texto sobre la
            # tabla entera tampoco.
            for valor in (nombre, version, manifest_path):
                assert "comentario" not in (valor or "")
                assert "\n" not in (valor or "")
    finally:
        await _borrar_repositorio(repositorio.id)
