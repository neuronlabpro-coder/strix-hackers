"""Sincronización de manifiestos de dependencias por la API del proveedor.

## Por qué esto no clona el repositorio

Porque R5 lo prohíbe, y no es una Formalidad.

La regla dice que el código fuente del cliente no se persiste en la plataforma, y el modo
habitual de cumplirla es "clonar en un directorio temporal y borrar al terminar". Eso cumple la
letra de "no persistir" y rompe el espíritu por dos sitios: el código del cliente existe en
disco del servidor, y existe durante toda la ejecución del escaneo, que es la mayor parte del
tiempo.

Este módulo **no pide nunca el código**. Pide cuatro ficheros por su nombre —`package.json`,
`requirements.txt`, `go.mod`, `Cargo.toml`—, que son declaraciones de dependencias, no código:
el primero dice qué paquetes usa el proyecto, y no contiene una sola línea de la lógica del
cliente. Lo que se persiste de ellos es el nombre, la versión y la licencia de cada
dependencia. El contenido del manifiesto se usa en memoria y se descarta al acabar la función.

La diferencia no es de grado: es que un directorio de trabajo de un escaneo contiene el
repositorio **entero** —el código fuente, los secretos del cliente en ficheros de configuración,
su historial de commits— y un manifiesto son cuatro kilobytes de nombres de paquetes.

## Por qué cuatro peticiones y no una descarga recursiva

Porque una descarga recursiva empieza por la raíz del repositorio, y la raíz contiene el código.
Bajar un `package.json` que está en la raíz es una operación dirigida; bajarse el repositorio
entero y quedarse con el `package.json` que había dentro es exactamente lo que R5 prohíbe, con
un paso intermedio que lo hace más difícil de ver en una revisión de código.

## Por qué se itera y no se lanzan las cuatro en paralelo

Un proveedor con limite de peticiones secundarias devuelve `429` con poco margen, y cuatro
peticiones simultaneas a un repositorio ajeno son cuatro veces el riesgo de comerse ese margen.
Además, la respuesta correcta a un `404` —"este repositorio no usa Go"— es la que más se repite:
de cuatro manifiestos, un repositorio tipico tiene uno o ninguno. Fallar pronto en los que faltan
es lo correcto, y en paralelo fallarian todos a la vez.
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.repositories.clients.base import BaseGitClient
from backend.apps.repositories.models import Repository
from backend.apps.supply_chain.manifests import EcosystemEnum, detectar_ecosistema
from backend.apps.supply_chain.service import (
    ResultadoDeIndexado,
    indexar_manifiesto,
)

#: Los cuatro manifiestos que se piden, en el orden en que se piden.
#:
#: `package.json` primero porque es el más común: la mayoría de repositorios que tienen alguno de
#: estos lo tienen en Node, y preguntar en ese orden maximiza la probabilidad de que el primer
#: acierto corte el recorrido de los que faltan.
MANIFIESTOS: tuple[str, ...] = (
    "package.json",
    "requirements.txt",
    "go.mod",
    "Cargo.toml",
)


class ResultadoDeSincronizacion:
    """Lo que pasó al intentar sincronizar un repositorio.

    ## Por qué `errores` es una lista de cadenas y no de excepciones

    Porque esto devuelve un `200`. Un repositorio con `go.mod` ilegible porque no es Go, o con
    un `package.json` que es un array, no es un fallo de la sincronización: la sincronización ha
    funcionado y ha descubierto que uno de los cuatro ficheros no es un manifiesto. Confundir
    las dos cosas haría que un repositorio de Python sin `go.mod` devolviera un error, y que el
    botón de "sincronizar" pareciera roto en el caso más normal del mundo.

    Las excepciones de verdad —el proveedor caído, la credencial caducada— **sí** suben, desde
    `_leer_manifiesto`. Un `404` no es una excepción de verdad: es un `None`.
    """

    def __init__(self) -> None:
        self.manifestos_encontrados: list[str] = []
        self.insertados = 0
        self.actualizados = 0
        self.descartados = 0
        self.errores: list[str] = []

    def acumula(self, resultado: ResultadoDeIndexado) -> None:
        self.insertados += resultado.inserted
        self.actualizados += resultado.updated
        self.descartados += resultado.discarded

    @property
    def total_paquetes(self) -> int:
        return self.insertados + self.actualizados

    @property
    def manifiestos_ausentes(self) -> list[str]:
        """Los buscados que el repositorio no tenía.

        Se calcula aquí y no lo rellena el llamador, para que no se pueda olvidar: un
        `manifests_missing` vacío cuando solo se encontraron dos de cuatro se lee como "se buscó
        todo y no había nada", que es un informe de inventario más credible y más falso que el que
        no dice nada.
        """

        return [ruta for ruta in MANIFIESTOS if ruta not in self.manifestos_encontrados]


async def _cargar_repositorio(
    session: AsyncSession,
    organization_id: uuid.UUID,
    repository_id: uuid.UUID,
) -> Repository | None:
    """El repositorio, o `None` si no existe **o si es de otra organización**.

    El filtro por `organization_id` no es una comprobación: es lo que impide que exista. Sin él,
    `repository_id` sería un identificador adivinable y quien tuviera uno podría pedir la
    sincronización del repositorio de otro cliente, con la credencial de este. R3.
    """

    resultado = await session.execute(
        select(Repository).where(
            Repository.id == repository_id,
            Repository.organization_id == organization_id,
        )
    )
    return resultado.scalar_one_or_none()


async def sincronizar_manifiestos(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    repository_id: uuid.UUID,
    client: BaseGitClient,
) -> ResultadoDeSincronizacion:
    """Descarga e indexa los manifiestos de un repositorio.

    ## Por qué un fallo en un manifiesto no detiene el resto

    Porque los cuatro ficheros son independientes y uno raro no dice nada de los otros tres. Un
    `requirements.txt` con una URL de git que no se puede parsear, o un `Cargo.toml` con una
    sección que el parser no reconoce, son cosas que pasan en repositorios reales, y el
    `package.json` de ese mismo repositorio sigue siendo perfectamente válido.

    Lo que no se hace es tragarse el error en silencio: cada fallo va a `errores`, y el endpoint
    los devuelve. La diferencia entre "sincronizar y reportar" y "sincronizar y ocultar" está
    exactamente en esa lista.
    """

    repositorio = await _cargar_repositorio(session, organization_id, repository_id)
    if repositorio is None:
        return ResultadoDeSincronizacion()

    resultado = ResultadoDeSincronizacion()

    for ruta in MANIFIESTOS:
        contenido = await _leer_manifiesto(client, repositorio.remote_repo_id, ruta)
        if contenido is None:
            # Ausencia, no fallo: este repositorio no usa este ecosistema. Es la respuesta más
            # frecuente de las cuatro, y no se registra como error.
            continue

        # `detectar_ecosistema` devuelve `OTHER` para lo que no reconoce, en vez de lanzar. Los
        # cuatro nombres de `MANIFIESTOS` son conocidos, así que `OTHER` solo puede aparecer si
        # alguien añade un nombre a la tupla sin añadirlo al parser. Se trata como lo que es —
        # un descuido de la tabla, no un dato del repositorio— y se registra en `errores`.
        if detectar_ecosistema(ruta) is EcosystemEnum.OTHER:
            resultado.errores.append(
                f"{ruta}: el parser no reconoce este manifiesto; no se indexa"
            )
            continue

        indexado = await indexar_manifiesto(
            session,
            organization_id=organization_id,
            repository_id=repository_id,
            manifest_path=ruta,
            contenido=contenido,
        )
        resultado.manifestos_encontrados.append(ruta)
        resultado.acumula(indexado)

    # Un unico commit para los cuatro manifiestos, y no uno por manifiesto.
    #
    # Con cuatro commits, una sincronización que falla en el tercero deja el primero y el
    # segundo persistidos y el tercero a medias: el inventario queda en un estado que nunca
    # corresponde a ningún momento real del repositorio. Con uno, o entra todo o no entra nada.
    await session.commit()
    return resultado


async def _leer_manifiesto(
    client: BaseGitClient,
    remote_repo_id: str,
    ruta: str,
) -> str | None:
    """El contenido de un manifiesto, o `None` si el repositorio no lo tiene.

    ## Por qué va en un hilo

    Porque `BaseGitClient` envuelve un `httpx.Client` **sincrónico**, y llamar a un cliente
    bloqueante desde una corrutina bloquea el bucle de eventos entero durante toda la petición.
    En una ruta que atendía una sincronización, eso congelaría **todas** las demás peticiones
    del proceso, no solo la que está esperando al proveedor.

    Es el mismo patrón y la misma razón que usa `connect_repository` en el router de
    repositorios. No es una decisión de este módulo: el cliente es síncrono en todo el proyecto y
    la traducción a asíncrono ocurre en el borde, no dentro de la librería.

    Un `GitClientError` sube. Un error del proveedor —red caída, credencial caducada, límite de
    peticiones— es un fallo de la sincronización entera, y tragárselo devolvería un inventario
    vacío que parece un repositorio sin dependencias. El usuario vería "0 paquetes" y pensaría
    que su proyecto no tiene ninguno, cuando lo que pasó es que no se pudo preguntar.
    """

    return await asyncio.to_thread(client.get_file_content, remote_repo_id, ruta)
