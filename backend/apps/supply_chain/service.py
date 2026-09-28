"""Ingesta y consulta del inventario de dependencias.

## Por qué la ingesta es **una función que recibe el texto**, no unEndpoint que lo descarga

Porque **quién** trae el manifiesto es una decisión que este proyecto todavía no ha tomado, y
que no le corresponde tomar a un servicio de inventario. Hay tres caminos posibles y los tres
terminan aquí:

- La API del proveedor Git —`GET /repos/{o}/{r}/contents/package.json`— con la credencial que ya
  hay. Funciona hoy y no requiere clonar, pero mete el código del cliente en el proceso del
  backend, y eso es una decisión sobre R5 que corresponde al responsable del proyecto.
- El contenedor de escaneo, que es donde el código está legítimamente. Es la vía correcta para R5
  y la que no exige nada nuevo, pero **Strix no publica el inventario**: no hay por dónde
  sacarlo hasta que lo exponga.
- Que el cliente suba el manifiesto a mano.

Lo que **no** cambia con ninguna de las tres es lo que se guarda: solo identificadores, y nunca
el texto. Esa parte está hecha y probada en `manifests.py`.

Por eso la firma es `indexar_manifiesto(session, ..., contenido: str)` y no
`sincronizar_repositorio(session, repository_id)`. Quien quiera cablear cualquiera de los tres
llama a esta función; el servicio no sabe de donde salio el texto y no podria afirmarlo ni
aunque quisiera.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.supply_chain.manifests import ResultadoDelParseo, parsear_manifiesto
from backend.apps.supply_chain.models import EcosystemEnum, SupplyChainPackage


class RepositoryNotIndexedError(LookupError):
    """El repositorio no existe, o no es de esta organización.

    Un solo error para los dos casos, a proposito: ver la nota del router.
    """


@dataclass(frozen=True, slots=True)
class ResultadoDeIndexado:
    """Lo que una indexación ha producido.

    Los tres numeros se devuelven para que la interfaz pueda decir **qué ha pasado** en vez de
    limitarse a un "sincronizado". Un inventario que se reindexa y que tras la operación tiene
    menos filas que antes necesita que se note, y eso solo se puede ver con estos numeros a la
    vista.
    """

    inserted: int
    updated: int
    discarded: int

    @property
    def total(self) -> int:
        return self.inserted + self.updated


async def indexar_manifiesto(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    repository_id: uuid.UUID,
    manifest_path: str,
    contenido: str,
) -> ResultadoDeIndexado:
    """Parsea un manifiesto y persiste sus dependencias directas.

    ## Por qué usa `INSERT ... ON CONFLICT DO UPDATE` y no un `SELECT` y luego un `UPDATE`

    Porque entre el `SELECT` y el `UPDATE` cabe otra petición —dos indexaciones del mismo
    repositorio a la vez, o el mismo manifiesto indexado desde dos procesos— y las dos
    insertarian la misma fila. Una acabaria con `IntegrityError` y el usuario veria un error
    por algo que no esta mal.

    El `ON CONFLICT` hace que la segunda operacion **actualice** en lugar de fallar, y que el
    resultado sea el mismo. Es idempotente: indexar dos veces el mismo manifiesto deja lo mismo.

    ## Por qué actualiza **solo** la fecha y el manifiesto, no el estado de vulnerabilidad

    Porque este servicio no comprueba vulnerabilidades —no hay fuente que lo haga— y escribir
    `has_vulnerabilities = NULL` en cada reindexacion borraria el resultado de un comprobador
    que se conectara en el futuro. La regla es: lo que este servicio no sabe, no lo toca.
    """

    await _existe_repositorio(session, organization_id, repository_id)
    resultado: ResultadoDelParseo = parsear_manifiesto(manifest_path, contenido)
    if not resultado.dependencies:
        return ResultadoDeIndexado(inserted=0, updated=0, discarded=resultado.discarded)

    valores = [
        {
            "organization_id": organization_id,
            "repository_id": repository_id,
            "name": dependencia.name[:255],
            "version": dependencia.version[:100],
            "ecosystem": dependencia.ecosystem,
            "license": dependencia.license,
            "has_vulnerabilities": None,
            "cve_ids": [],
            "manifest_path": manifest_path[:255],
            "is_dev_dependency": dependencia.is_dev,
        }
        for dependencia in resultado.dependencies
    ]

    # Se cuentan las altas y las actualizaciones por separado, y se hace con el `RETURNING` de
    # PostgreSQL en lugar de con un `SELECT` de comprobacion antes y despues. Preguntar dos
    # veces cuesta el doble y, entre las dos preguntas, el estado puede haber cambiado.
    antes = await _nombres_existentes(session, repository_id, resultado.dependencies)
    insertadas = await _subir(session, valores)
    # Lo que no existia antes es nuevo. Es la unica forma de decirlo sin una segunda vuelta a
    # la base, y el numero no es critico: lo que el panel necesita es el total, no el desglose.
    nuevos = insertadas - antes
    actualizadas = insertadas - nuevos

    return ResultadoDeIndexado(
        inserted=max(nuevos, 0),
        updated=max(actualizadas, 0),
        discarded=resultado.discarded,
    )


async def _subir(session: AsyncSession, valores: Sequence[dict[str, object]]) -> int:
    """Inserta o actualiza en una sola sentencia, y devuelve cuantas filas hay ahora."""

    if not valores:
        return 0
    sentencia = pg_insert(SupplyChainPackage).values(list(valores))
    sentencia = sentencia.on_conflict_do_update(
        index_elements=[
            SupplyChainPackage.repository_id,
            SupplyChainPackage.name,
            SupplyChainPackage.ecosystem,
        ],
        set_={
            "version": sentencia.excluded.version,
            "license": sentencia.excluded.license,
            "manifest_path": sentencia.excluded.manifest_path,
            "is_dev_dependency": sentencia.excluded.is_dev_dependency,
            "last_seen_at": func.now(),
        },
    ).returning(SupplyChainPackage.id)
    resultado = await session.execute(sentencia)
    return len(resultado.fetchall())


async def _nombres_existentes(
    session: AsyncSession,
    repository_id: uuid.UUID,
    dependencias: Sequence[object],
) -> int:
    """Cuantas de esas dependencias ya estaban en el repositorio.

    El filtro es por **nombre**, y el `repository_id` ya acota. Preguntar por `name` y
    `ecosystem` como un par no cambiaria el numero, porque un mismo nombre no puede vivir dos
    veces con dos ecosistemas distintos bajo la restriccion unica de la tabla.
    """

    nombres = {d.name for d in dependencias}  # type: ignore[attr-defined]
    if not nombres:
        return 0
    total = (
        await session.execute(
            select(func.count())
            .select_from(SupplyChainPackage)
            .where(
                SupplyChainPackage.repository_id == repository_id,
                SupplyChainPackage.name.in_(nombres),
            )
        )
    ).scalar_one()
    return int(total)


async def _existe_repositorio(
    session: AsyncSession, organization_id: uuid.UUID, repository_id: uuid.UUID
) -> None:
    """El repositorio tiene que existir **y** ser de esta organización.

    Es R3 en la propia función de ingesta, y no solo en el endpoint: la ingesta puede llamarla
    el contenedor de escaneo o un worker, y ninguno de los dos pasa por una dependencia de
    FastAPI que compruebe el tenant. Un filtro que solo existe en la ruta es un filtro que la
    proxima ruta nueva no tendra.
    """

    from backend.apps.repositories.models import Repository

    existe = (
        await session.execute(
            select(Repository.id).where(
                Repository.id == repository_id,
                Repository.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if existe is None:
        raise RepositoryNotIndexedError(str(repository_id))


# --------------------------------------------------------------------------- #
# Consulta
# --------------------------------------------------------------------------- #


async def listar_paquetes(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    repository_id: uuid.UUID | None = None,
    ecosystem: EcosystemEnum | None = None,
    has_vulnerabilities: bool | None = None,
    solo_desarrollo: bool | None = None,
    busqueda: str | None = None,
    limite: int = 50,
    offset: int = 0,
) -> tuple[list[tuple[SupplyChainPackage, str, str]], int]:
    """Las dependencias del workspace, y cuántas hay en total.

    Devuelve ademas el **nombre completo del repositorio** de cada fila, porque el panel lo
    muestra en una columna y traerlo con un join es mejor que un `N+1`: con cincuenta filas, un
    `N+1` son cincuenta consultas en cada recarga de la tabla.

    ## Por qué `has_vulnerabilities` admite `None` como valor de filtro

    Porque `None` en el filtro significa "no filtrar por esto", que es distinto de
    `has_vulnerabilities IS NULL`, que es "solo las que no se han comprobado". La segunda es una
    pregunta real y útil —¿cuánto del inventario está sin verificar?— y con un flag booleano no
    caben las dos. Por eso el parametro es `bool | None` y no `bool`.

    El filtro por `organization_id` es obligatorio y va en la misma sentencia que el `COUNT`.
    R3 no es una recomendacion.
    """

    condiciones = [SupplyChainPackage.organization_id == organization_id]
    if repository_id is not None:
        condiciones.append(SupplyChainPackage.repository_id == repository_id)
    if ecosystem is not None:
        condiciones.append(SupplyChainPackage.ecosystem == ecosystem)
    if has_vulnerabilities is not None:
        condiciones.append(
            SupplyChainPackage.has_vulnerabilities.is_(has_vulnerabilities)
        )
    if solo_desarrollo is not None:
        condiciones.append(SupplyChainPackage.is_dev_dependency.is_(solo_desarrollo))
    if busqueda:
        patron = f"%{busqueda}%"
        condiciones.append(
            SupplyChainPackage.name.ilike(patron) | SupplyChainPackage.license.ilike(patron)
        )

    from backend.apps.repositories.models import Repository

    total = (
        await session.execute(
            select(func.count())
            .select_from(SupplyChainPackage)
            .where(*condiciones)
        )
    ).scalar_one()

    # `populate_existing` **no es opcional** y no es una precaution de estilo.
    #
    # La ingesta escribe con `INSERT ... ON CONFLICT`, que es SQL crudo y **pasa por debajo del
    # mapa de identidad** del ORM: las filas que la sesion ya tiene cargadas siguen con los
    # valores de antes. La lectura siguiente las devolveria tal cual, y el sintoma no es un error
    # sino un dato viejo: se reindexa un manifiesto con una version nueva, la fila se actualiza
    # de verdad en la base, y al releer sale la version anterior.
    #
    # `expire_all()` pareceria la solucion y **empeora el problema**: invalida objetos que el
    # `commit` siguiente tiene que refrescar, y ese refresco es una ida a la base que en async
    # ocurre fuera de un contexto de greenlet y levanta `MissingGreenlet`. Es decir: arreglar la
    # lectura obsoleta asi rompe el guardado.
    #
    # Con `populate_existing` cada lectura vuelve a pedir las filas a la base y sobrescribe lo
    # que hubiera en memoria, sin invalidar nada y sin tocar el `commit`.
    filas = (
        await session.execute(
            select(SupplyChainPackage, Repository.full_name, Repository.name)
            .join(Repository, Repository.id == SupplyChainPackage.repository_id)
            .where(*condiciones)
            .execution_options(populate_existing=True)
            .order_by(
                # `is_not(None)` y **no** `is_(None)`, y el sentido es lo que importa:
                # en PostgreSQL `ORDER BY` ascendente pone `False` antes que `True`, asi que
                # `is_(None)` —que es `True` en las no comprobadas— las pondria **ultimo**, que
                # es justo lo contrario de lo que se busca. `is_not(None)` da `False` en las
                # no comprobadas y las deja delante.
                SupplyChainPackage.has_vulnerabilities.is_not(None),
                SupplyChainPackage.name.asc(),
            )
            .limit(limite)
            .offset(offset)
        )
    ).all()

    return list(filas), int(total)


@dataclass(frozen=True, slots=True)
class ResumenDeInventario:
    """Los numeros de cabecera, ya contados.

    Es un `dataclass` y no un `dict` porque los numeros se suman entre si —los tres estados
    tienen que cuadrar con el total— y con un `dict[str, object]` esa suma no compila y un
    nombre mal escrito no lo detecta el type checker. Aqui `resumen.vulnerable + resumen.clean`
    es un `int` o un error, y `resumen.vulnearable` es un `AttributeError` al escribir el
    test, no un `KeyError` en produccion.
    """

    total_dependencies: int
    vulnerable: int
    clean: int
    unchecked: int
    by_ecosystem: dict[str, int]
    repositories_indexed: int


async def resumen_inventario(
    session: AsyncSession,
    organization_id: uuid.UUID,
) -> ResumenDeInventario:
    """Los numeros de cabecera, contados con SQL y no sobre una pagina.

    ## Por qué no se cuenta "lo que vino" y ya

    Porque una pagina de doscientos no es el inventario. Con tres mil dependencias, el resumen
    de una pagina diria que no hay ninguna sin comprobar cuando hay dos mil, y el usuario
    leeria "todo comprobado" en la cabecera de una tabla donde no se sabe nada. Un numero
    aproximado en la cabecera de un panel de seguridad es peor que no dar el numero: es un
    numero **falso** con apariencia de exacto.

    Se cuentan los tres estados con una sola consulta agregada y un `COUNT ... FILTER` por
    estado, que es lo que hace que sea exacto y no una estimacion. Los tres se cuentan en la
    misma fila porque separarlos en tres consultas daría tres momentos distintos, y entre
    ellos un ingreso podria hacer que no sumaran el total.
    """

    fila = (
        await session.execute(
            select(
                func.count(SupplyChainPackage.id),
                func.count(SupplyChainPackage.id).filter(
                    SupplyChainPackage.has_vulnerabilities.is_(True)
                ),
                func.count(SupplyChainPackage.id).filter(
                    SupplyChainPackage.has_vulnerabilities.is_(False)
                ),
                func.count(SupplyChainPackage.id).filter(
                    SupplyChainPackage.has_vulnerabilities.is_(None)
                ),
                func.count(func.distinct(SupplyChainPackage.repository_id)),
            )
            .select_from(SupplyChainPackage)
            .where(SupplyChainPackage.organization_id == organization_id)
            # `populate_existing` por el mismo motivo que en `listar_paquetes`: el `upsert` de
            # la ingesta no pasa por el mapa de identidad del ORM.
            .execution_options(populate_existing=True)
        )
    ).one()

    # La clave es el **valor** del enum, no el miembro: el esquema lo declara como `dict[str, int]`
    # porque es lo que viaja al panel, y un `EcosystemEnum` serializado sale como el nombre del
    # miembro, que aqui coincide con el valor pero no por contrato.
    por_ecosistema = {
        ecosistema.value: int(cuenta)
        for ecosistema, cuenta in (
            await session.execute(
                select(SupplyChainPackage.ecosystem, func.count())
                .where(SupplyChainPackage.organization_id == organization_id)
                .group_by(SupplyChainPackage.ecosystem)
            )
        ).all()
    }

    total, vulnerable, limpio, sin_comprobar, repositorios = (int(v) for v in fila)
    return ResumenDeInventario(
        total_dependencies=total,
        vulnerable=vulnerable,
        clean=limpio,
        unchecked=sin_comprobar,
        by_ecosystem=por_ecosistema,
        repositories_indexed=repositorios,
    )


__all__ = [
    "RepositoryNotIndexedError",
    "ResultadoDeIndexado",
    "ResumenDeInventario",
    "indexar_manifiesto",
    "listar_paquetes",
    "resumen_inventario",
]
