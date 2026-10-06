"""Pruebas de la ingesta y la consulta del inventario de dependencias.

## Qué se comprueba y por qué

Tres cosas, y las tres importan más que el CRUD:

- **El aislamiento.** Un inventario de dependencias dice qué stack y qué versiones usa cada
  cliente. Es de los datos más reveladores que hay en la plataforma, y un fallo aquí no se ve
  como un identificador raro en una lista: se ve como la tabla de dependencias de tu vecino.
- **La idempotencia de la indexación.** Indexar dos veces el mismo manifiesto no puede crear
  filas duplicadas ni fallar con un error de restricción.
- **Que `has_vulnerabilities` sigue en `NULL`.** Es la propiedad que impide que el panel diga
  "limpio" de algo que nadie ha comprobado, y se rompe en silencio: basta con que un `INSERT`
  Incluya `False` por defecto para que todos los paquetes aparezcan verificados.

## Por qué cada prueba usa su propia sesión

Porque estos endpoints hacen `commit`, y la sesión del fixture se comparte con la aplicación en
modo `savepoint`. Un `commit` de la ruta dentro de esa sesión se propaga a la base de pruebas y
deja filas que ensucian el resto de la suite.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import Membership, Organization, RoleEnum, User
from backend.apps.repositories.models import GitProviderEnum, Repository
from backend.apps.supply_chain import service
from backend.apps.supply_chain.models import EcosystemEnum, SupplyChainPackage
from backend.core.database import AsyncSessionLocal
from backend.core.security import create_access_token, hash_password
from backend.main import app

pytestmark = pytest.mark.asyncio


@asynccontextmanager
async def _sesion() -> AsyncIterator[AsyncSession]:
    sesion = AsyncSessionLocal()
    try:
        yield sesion
    finally:
        await sesion.close()


class Tenant:
    def __init__(self, organization: Organization, user: User) -> None:
        self.organization_id = organization.id
        self.headers = {
            "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
            "X-Organization-Id": str(organization.id),
        }


async def _tenant(sesion: AsyncSession, prefijo: str) -> Tenant:
    sufijo = uuid.uuid4().hex
    organization = Organization(name=f"{prefijo} {sufijo}", slug=f"{prefijo}-{sufijo}")
    user = User(
        email=f"{prefijo}-{sufijo}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name="Cliente",
        email_verified=True,
    )
    sesion.add_all([organization, user])
    await sesion.flush()
    sesion.add(Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.ADMIN))
    await sesion.commit()
    return Tenant(organization, user)


async def _repositorio(sesion: AsyncSession, organization_id: uuid.UUID, nombre: str) -> Repository:
    repositorio = Repository(
        organization_id=organization_id,
        provider=GitProviderEnum.GITHUB,
        remote_repo_id=str(uuid.uuid4()),
        name=nombre,
        full_name=nombre,
        clone_url=f"https://github.com/{nombre}.git",
        default_branch="main",
        webhook_secret="x" * 48,
    )
    sesion.add(repositorio)
    await sesion.commit()
    return repositorio


MANIFIESTO = json.dumps(
    {
        "license": "MIT",
        "dependencies": {"express": "^4.17.21", "axios": "~1.6.0"},
        "devDependencies": {"typescript": "5.4.0"},
    }
)


# --------------------------------------------------------------------------- #
# La ingesta
# --------------------------------------------------------------------------- #


async def test_indexar_un_manifiesto_persiste_sus_dependencias() -> None:
    async with _sesion() as sesion:
        tenant = await _tenant(sesion, "sc")
        repositorio = await _repositorio(sesion, tenant.organization_id, "cliente/api")

        resultado = await service.indexar_manifiesto(
            sesion,
            organization_id=tenant.organization_id,
            repository_id=repositorio.id,
            manifest_path="package.json",
            contenido=MANIFIESTO,
        )
        await sesion.commit()

        # `populate_existing` porque la ingesta escribe con SQL crudo y no pasa por el mapa de
        # identidad del ORM. Ver el contrato en `service.listar_paquetes`.
        filas = (
            (
                await sesion.execute(
                    select(SupplyChainPackage)
                    .where(SupplyChainPackage.repository_id == repositorio.id)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )

    assert resultado.inserted == 3
    assert resultado.updated == 0
    assert {f.name for f in filas} == {"express", "axios", "typescript"}
    por_nombre = {f.name: f for f in filas}
    assert por_nombre["express"].version == "^4.17.21"
    assert por_nombre["express"].license == "MIT"
    assert por_nombre["typescript"].is_dev_dependency is True
    assert por_nombre["express"].is_dev_dependency is False


async def test_indexar_no_puede_comprobar_vulnerabilidades() -> None:
    """La propiedad que impide que el panel diga "limpio" de lo que nadie ha mirado.

    La ingesta **no** tiene fuente de vulnerabilidades que consultar, asi que escribe `NULL` y
    no un `False` por defecto. Con `False`, todos los paquetes recien indexados aparecerian
    verificados y el usuario dejaria de comprobar por su cuenta, que es el daño mas caro de
    los tres estados posibles.
    """

    async with _sesion() as sesion:
        tenant = await _tenant(sesion, "sc")
        repositorio = await _repositorio(sesion, tenant.organization_id, "cliente/api")
        await service.indexar_manifiesto(
            sesion,
            organization_id=tenant.organization_id,
            repository_id=repositorio.id,
            manifest_path="package.json",
            contenido=MANIFIESTO,
        )
        await sesion.commit()

        # `populate_existing` porque la ingesta escribe con SQL crudo y no pasa por el mapa de
        # identidad del ORM. Ver el contrato en `service.listar_paquetes`.
        filas = (
            (
                await sesion.execute(
                    select(SupplyChainPackage)
                    .where(SupplyChainPackage.repository_id == repositorio.id)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )

    assert filas
    assert all(f.has_vulnerabilities is None for f in filas)
    assert all(f.cve_ids == [] for f in filas)


async def test_indexar_dos_veces_no_duplica_ni_falla() -> None:
    """La indexación es idempotente.

    Con un `SELECT` y luego un `UPDATE` haberia una ventana entre las dos en la que dos
    peticiones simultaneas insertarian la misma fila, y la segunda moriria con un
    `IntegrityError`: el usuario veria un error por algo que no esta mal.
    """

    async with _sesion() as sesion:
        tenant = await _tenant(sesion, "sc")
        repositorio = await _repositorio(sesion, tenant.organization_id, "cliente/api")

        primero = await service.indexar_manifiesto(
            sesion,
            organization_id=tenant.organization_id,
            repository_id=repositorio.id,
            manifest_path="package.json",
            contenido=MANIFIESTO,
        )
        await sesion.commit()
        segundo = await service.indexar_manifiesto(
            sesion,
            organization_id=tenant.organization_id,
            repository_id=repositorio.id,
            manifest_path="package.json",
            contenido=MANIFIESTO,
        )
        await sesion.commit()

        total = (
            await sesion.execute(
                select(func.count())
                .select_from(SupplyChainPackage)
                .where(SupplyChainPackage.repository_id == repositorio.id)
            )
        ).scalar_one()

    assert primero.inserted == 3
    assert segundo.inserted == 0
    assert segundo.updated == 3
    assert total == 3


async def test_reindexar_actualiza_la_version_sin_tocar_el_estado_de_vulnerabilidad() -> None:
    """Cambiar de versión actualiza la fila y **no** borra lo que otro comprobador dijo.

    Este servicio no comprueba vulnerabilidades, asi que escribir `has_vulnerabilities = NULL`
    en cada reindexacion borraria el resultado de un comprobador que se conectara en el
    futuro. La regla es: lo que este servicio no sabe, no lo toca.
    """

    async with _sesion() as sesion:
        tenant = await _tenant(sesion, "sc")
        repositorio = await _repositorio(sesion, tenant.organization_id, "cliente/api")
        await service.indexar_manifiesto(
            sesion,
            organization_id=tenant.organization_id,
            repository_id=repositorio.id,
            manifest_path="package.json",
            contenido=MANIFIESTO,
        )
        await sesion.commit()

        # Se simula que un comprobador dejo un resultado.
        fila = (
            await sesion.execute(
                select(SupplyChainPackage).where(
                    SupplyChainPackage.repository_id == repositorio.id,
                    SupplyChainPackage.name == "express",
                )
            )
        ).scalar_one()
        fila.has_vulnerabilities = True
        fila.cve_ids = ["CVE-2024-0001"]
        await sesion.commit()

        await service.indexar_manifiesto(
            sesion,
            organization_id=tenant.organization_id,
            repository_id=repositorio.id,
            manifest_path="package.json",
            contenido=json.dumps({"dependencies": {"express": "^5.0.0"}}),
        )
        await sesion.commit()

        tras = (
            await sesion.execute(
                select(SupplyChainPackage)
                .where(
                    SupplyChainPackage.repository_id == repositorio.id,
                    SupplyChainPackage.name == "express",
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one()

    assert tras.version == "^5.0.0"
    assert tras.has_vulnerabilities is True
    assert tras.cve_ids == ["CVE-2024-0001"]


async def test_indexar_un_repositorio_ajeno_da_error() -> None:
    """La comprobacion de pertenencia esta en el **servicio**, no solo en la ruta.

    La ingesta la llamara el contenedor de escaneo o un worker, y ninguno pasa por una
    dependencia de FastAPI que compruebe el tenant. Un filtro que solo existe en la ruta es un
    filtro que la proxima ruta nueva no tendra.
    """

    async with _sesion() as sesion:
        victima = await _tenant(sesion, "sc-victima")
        atacante = await _tenant(sesion, "sc-atacante")
        repositorio = await _repositorio(sesion, victima.organization_id, "cliente/api")

        with pytest.raises(service.RepositoryNotIndexedError):
            await service.indexar_manifiesto(
                sesion,
                organization_id=atacante.organization_id,
                repository_id=repositorio.id,
                manifest_path="package.json",
                contenido=MANIFIESTO,
            )
        await sesion.rollback()


async def test_un_token_con_credenciales_no_llega_a_la_base() -> None:
    """La proteccion, de extremo a extremo, contra la tabla.

    Un manifiesto con un token en una URL de registro es una cosa que pasa; que el token acabe
    en un `Text` de la base es un incidente que no se ve hasta que alguien filtra la base.
    """

    manifiesto = "\n".join(
        [
            "requests==2.31.0",
            "https://deploy:ghp_16CaracteresDeEjemplo0@registry.internal/pkg.whl",
        ]
    )
    async with _sesion() as sesion:
        tenant = await _tenant(sesion, "sc")
        repositorio = await _repositorio(sesion, tenant.organization_id, "cliente/api")
        resultado = await service.indexar_manifiesto(
            sesion,
            organization_id=tenant.organization_id,
            repository_id=repositorio.id,
            manifest_path="requirements.txt",
            contenido=manifiesto,
        )
        await sesion.commit()

        # `populate_existing` porque la ingesta escribe con SQL crudo y no pasa por el mapa de
        # identidad del ORM. Ver el contrato en `service.listar_paquetes`.
        filas = (
            (
                await sesion.execute(
                    select(SupplyChainPackage)
                    .where(SupplyChainPackage.repository_id == repositorio.id)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )

    # La URL se descarta como dependencia, y con ella el token.
    assert [f.name for f in filas] == ["requests"]
    assert resultado.discarded == 1
    volcado = json.dumps(
        [{"n": f.name, "v": f.version, "l": f.license} for f in filas]
    )
    assert "ghp_" not in volcado
    assert "deploy" not in volcado


# --------------------------------------------------------------------------- #
# El aislamiento
# --------------------------------------------------------------------------- #


async def test_no_se_ven_las_dependencias_de_otro_tenant() -> None:
    """El inventario de un tenant no aparece en el listado del otro.

    Es la prueba mas importante del fichero. Las dependencias de un cliente dicen que
    tecnologias usa y en que version, que es de lo mas revelador que hay en la plataforma.
    """

    async with _sesion() as sesion:
        victima = await _tenant(sesion, "sc-victima")
        atacante = await _tenant(sesion, "sc-atacante")
        repositorio = await _repositorio(
            sesion, victima.organization_id, "competidor/stack-interno"
        )
        await service.indexar_manifiesto(
            sesion,
            organization_id=victima.organization_id,
            repository_id=repositorio.id,
            manifest_path="package.json",
            contenido=MANIFIESTO,
        )
        await sesion.commit()

        filas, total = await service.listar_paquetes(sesion, atacante.organization_id)

    assert filas == []
    assert total == 0


async def test_el_resumen_no_cuenta_lo_de_otro_tenant() -> None:
    """El resumen tambien filtra, y no solo el listado.

    Un resumen que contenga los numeros de otro tenant los delata aunque el listado no muestre
    una sola fila: seiscientos paquetes en la cabecera ya son un dato.
    """

    async with _sesion() as sesion:
        victima = await _tenant(sesion, "sc-victima")
        atacante = await _tenant(sesion, "sc-atacante")
        repositorio = await _repositorio(sesion, victima.organization_id, "competidor/api")
        await service.indexar_manifiesto(
            sesion,
            organization_id=victima.organization_id,
            repository_id=repositorio.id,
            manifest_path="package.json",
            contenido=MANIFIESTO,
        )
        await sesion.commit()

        resumen = await service.resumen_inventario(sesion, atacante.organization_id)

    assert resumen.total_dependencies == 0
    assert resumen.unchecked == 0
    assert resumen.repositories_indexed == 0


async def test_indexar_en_un_repositorio_ajeno_no_crea_filas() -> None:
    """Un intento fallido no deja rastro en la base.

    El error se lanza antes de insertar nada, y no hay un `INSERT` parcial que despues alguien
    encuentre datos de un repositorio al que no tenia acceso.
    """

    async with _sesion() as sesion:
        victima = await _tenant(sesion, "sc-victima")
        atacante = await _tenant(sesion, "sc-atacante")
        repositorio = await _repositorio(sesion, victima.organization_id, "competidor/api")

        with pytest.raises(service.RepositoryNotIndexedError):
            await service.indexar_manifiesto(
                sesion,
                organization_id=atacante.organization_id,
                repository_id=repositorio.id,
                manifest_path="package.json",
                contenido=MANIFIESTO,
            )
        await sesion.rollback()

        # El recuento va filtrado por la organización de la víctima, no global. La base es
        # compartida entre pruebas y contar filas globales mide el orden de ejecución de pytest,
        # no el servicio: cualquier otra prueba que haya indexado algo antes hace fallar esta
        # sin que haya ningún fallo.
        total = (
            await sesion.execute(
                select(func.count())
                .select_from(SupplyChainPackage)
                .where(SupplyChainPackage.organization_id == victima.organization_id)
            )
        ).scalar_one()

    assert total == 0


# --------------------------------------------------------------------------- #
# Los filtros y el resumen
# --------------------------------------------------------------------------- #


async def test_filtrar_por_ecosistema_y_por_repositorio() -> None:
    async with _sesion() as sesion:
        tenant = await _tenant(sesion, "sc")
        node = await _repositorio(sesion, tenant.organization_id, "cliente/node")
        python = await _repositorio(sesion, tenant.organization_id, "cliente/servicio")
        await service.indexar_manifiesto(
            sesion,
            organization_id=tenant.organization_id,
            repository_id=node.id,
            manifest_path="package.json",
            contenido=MANIFIESTO,
        )
        await service.indexar_manifiesto(
            sesion,
            organization_id=tenant.organization_id,
            repository_id=python.id,
            manifest_path="requirements.txt",
            contenido="flask==3.0.0\n",
        )
        await sesion.commit()

        por_ecosistema, total_eco = await service.listar_paquetes(
            sesion, tenant.organization_id, ecosystem=EcosystemEnum.PYPI
        )
        por_repo, total_repo = await service.listar_paquetes(
            sesion, tenant.organization_id, repository_id=node.id
        )
        _, total_todo = await service.listar_paquetes(sesion, tenant.organization_id)

    assert [f.name for f, _n, _c in por_ecosistema] == ["flask"]
    assert total_eco == 1
    assert total_repo == 3
    assert len(por_repo) == 3
    assert total_todo == 4


async def test_el_buscador_trata_los_comodines_como_literales() -> None:
    r"""`%` y `_` se buscan literales en `listar_paquetes(busqueda=...)`.

    Sin escapar, `busqueda="%"` devuelve **el inventario entero**: el comodín va también en los
    dos extremos del patrón, así que `%\%` casa con cualquier nombre. Y `_` casa con cualquier
    carácter, de modo que `web_app` también traería `webXapp` —y los nombres de paquete de npm
    llevan `_` con frecuencia, `@types/node`, `lodash.merge` no pero `snake_case` sí—.

    Se comprueba sobre el **servicio** y no sobre el endpoint porque el filtro se construye aquí,
    en el servicio: un test del endpoint que pasara probaría el enrutado, no el `ILIKE`.

    ## Por qué el patrón no viene ya escapado de otra capa

    Porque no hay otra capa: `busqueda` llega del endpoint al servicio sin tocar nada, y aquí se
    montaba el patrón. Que hoy el texto se escriba en minúsculas no lo salva: `ILIKE` ya es
    insensible a mayúsculas y el escape del comodín es otra cosa.
    """

    async with _sesion() as sesion:
        tenant = await _tenant(sesion, "sc")
        repositorio = await _repositorio(sesion, tenant.organization_id, "cliente/web")
        await service.indexar_manifiesto(
            sesion,
            organization_id=tenant.organization_id,
            repository_id=repositorio.id,
            manifest_path="package.json",
            contenido=MANIFIESTO,
        )
        await sesion.commit()

        # Dos paquetes que solo se diferencian en el carácter que se va a buscar: `_` contra `X`
        # y `%` contra `0`. La licencia lleva el comodín en el segundo par, para comprobar que
        # también escapa la segunda columna del `or_`.
        sufijo = uuid.uuid4().hex[:8]
        sesion.add_all(
            [
                SupplyChainPackage(
                    organization_id=tenant.organization_id,
                    repository_id=repositorio.id,
                    name=f"web_app-{sufijo}",
                    version="1.0.0",
                    ecosystem=EcosystemEnum.NPM,
                    license="MIT",
                ),
                SupplyChainPackage(
                    organization_id=tenant.organization_id,
                    repository_id=repositorio.id,
                    name=f"webXapp-{sufijo}",
                    version="1.0.0",
                    ecosystem=EcosystemEnum.NPM,
                    license="MIT",
                ),
                SupplyChainPackage(
                    organization_id=tenant.organization_id,
                    repository_id=repositorio.id,
                    name=f"descuento-{sufijo}",
                    version="1.0.0",
                    ecosystem=EcosystemEnum.NPM,
                    license="Lic-100%off",
                ),
                SupplyChainPackage(
                    organization_id=tenant.organization_id,
                    repository_id=repositorio.id,
                    name=f"otro-{sufijo}",
                    version="1.0.0",
                    ecosystem=EcosystemEnum.NPM,
                    license="Lic-1000off",
                ),
            ]
        )
        await sesion.commit()

        por_subrayado, total_subrayado = await service.listar_paquetes(
            sesion, tenant.organization_id, busqueda=f"web_app-{sufijo}"
        )
        por_porcentaje, total_porcentaje = await service.listar_paquetes(
            sesion, tenant.organization_id, busqueda="Lic-100%off"
        )
        solo_porcentaje, total_solo_porcentaje = await service.listar_paquetes(
            sesion, tenant.organization_id, busqueda="%"
        )

    # `_` no es comodín de un carácter: solo el paquete que lo lleva literalmente.
    assert total_subrayado == 1
    assert [f.name for f, _n, _c in por_subrayado] == [f"web_app-{sufijo}"]
    # El `%` en medio se busca literal, sin arrastrar al `1000off` ni a las licencias del
    # manifiesto sembrado antes.
    assert total_porcentaje == 1
    assert [f.license for f, _n, _c in por_porcentaje] == ["Lic-100%off"]
    # Y `%` a secas devuelve **una** fila —la que lleva el símbolo— y no el inventario entero,
    # que a estas alturas ya tiene siete paquetes entre el manifiesto y los cuatro añadidos.
    assert total_solo_porcentaje == 1
    assert [f.license for f, _n, _c in solo_porcentaje] == ["Lic-100%off"]


async def test_filtrar_solo_por_las_no_comprobadas() -> None:
    """`has_vulnerabilities=None` como filtro es "no filtrar", no "solo las no comprobadas".

    Son dos preguntas distintas y con un flag booleano no caben las dos. Esta es la que hace el
    usuario cuando quiere saber cuanto de lo que ve no sabe nada, y es la que distingue este
    modulo de uno que solo sepa decir "limpio" y "vulnerable".
    """

    async with _sesion() as sesion:
        tenant = await _tenant(sesion, "sc")
        repositorio = await _repositorio(sesion, tenant.organization_id, "cliente/api")
        await service.indexar_manifiesto(
            sesion,
            organization_id=tenant.organization_id,
            repository_id=repositorio.id,
            manifest_path="package.json",
            contenido=MANIFIESTO,
        )
        await sesion.commit()

        # Se marca una como comprobada y limpia, como haria un comprobador futuro.
        fila = (
            await sesion.execute(
                select(SupplyChainPackage).where(
                    SupplyChainPackage.repository_id == repositorio.id,
                    SupplyChainPackage.name == "axios",
                )
            )
        ).scalar_one()
        fila.has_vulnerabilities = False
        await sesion.commit()

        # `None` en el parametro **no** filtra: salen las tres.
        todas, total_todas = await service.listar_paquetes(
            sesion, tenant.organization_id, has_vulnerabilities=None
        )
        _, total_sin_filtro = await service.listar_paquetes(sesion, tenant.organization_id)
        # Y `False` es una comprobacion distinta: solo las limpias.
        limpias, total_limpias = await service.listar_paquetes(
            sesion, tenant.organization_id, has_vulnerabilities=False
        )

    assert total_sin_filtro == 3
    assert total_todas == 3
    assert len(todas) == 3
    # `IS false` no incluye los `NULL`: eso es justo lo que separa "comprobada y limpia" de "no
    # comprobada". Si las incluyera, el filtro no serviria para nada.
    assert total_limpias == 1
    assert [f.name for f, _n, _c in [_t for _t in limpias]] == ["axios"]


async def test_el_resumen_separa_los_tres_estados() -> None:
    """`vulnerable`, `clean` y `unchecked` se cuentan por separado y **suman el total**.

    Es la propiedad que impide que el panel diga "todo limpio" de un inventario entero sin
    comprobar. Si los tres no suman el total, hay un estado que se está perdiendo por el
    camino, y ese es el que engaña.
    """

    async with _sesion() as sesion:
        tenant = await _tenant(sesion, "sc")
        repositorio = await _repositorio(sesion, tenant.organization_id, "cliente/api")
        await service.indexar_manifiesto(
            sesion,
            organization_id=tenant.organization_id,
            repository_id=repositorio.id,
            manifest_path="package.json",
            contenido=MANIFIESTO,
        )
        await sesion.commit()

        # Uno vulnerable, uno limpio, y el tercero sin comprobar.
        estados = {
            "express": True,
            "axios": False,
        }
        for nombre, valor in estados.items():
            fila = (
                await sesion.execute(
                    select(SupplyChainPackage).where(
                        SupplyChainPackage.repository_id == repositorio.id,
                        SupplyChainPackage.name == nombre,
                    )
                )
            ).scalar_one()
            fila.has_vulnerabilities = valor
        await sesion.commit()

        resumen = await service.resumen_inventario(sesion, tenant.organization_id)

    assert resumen.total_dependencies == 3
    assert resumen.vulnerable == 1
    assert resumen.clean == 1
    assert resumen.unchecked == 1
    # La suma es la propiedad que hay que fijar: si dejara de cuadrar, habria un estado que se
    # pierde y el panel estaria mintiendo en la cabecera sin que se note.
    assert resumen.vulnerable + resumen.clean + resumen.unchecked == resumen.total_dependencies
    assert resumen.by_ecosystem["NPM"] == 3
    assert resumen.repositories_indexed == 1


async def test_lo_no_comprobado_aparece_primero_en_el_listado() -> None:
    """El orden pone delante lo que **no** se sabe.

    Un `ORDER BY` por nombre deja las dependencias sin verificar en el mismo plano que las
    comprobadas, y quien mira la tabla asume que todo lo que ve se sabe. Mandando delante lo
    desconocido, la primera pantalla dice la verdad sobre cuanto de lo que hay no se sabe.
    """

    async with _sesion() as sesion:
        tenant = await _tenant(sesion, "sc")
        repositorio = await _repositorio(sesion, tenant.organization_id, "cliente/api")
        await service.indexar_manifiesto(
            sesion,
            organization_id=tenant.organization_id,
            repository_id=repositorio.id,
            manifest_path="package.json",
            contenido=MANIFIESTO,
        )
        await sesion.commit()

        # `axios` se comprueba y queda limpio; `express` y `typescript` no.
        fila = (
            await sesion.execute(
                select(SupplyChainPackage).where(
                    SupplyChainPackage.repository_id == repositorio.id,
                    SupplyChainPackage.name == "axios",
                )
            )
        ).scalar_one()
        fila.has_vulnerabilities = False
        await sesion.commit()

        filas, _total = await service.listar_paquetes(
            sesion, tenant.organization_id, limite=10
        )

    # Se afirma la **propiedad** y no un nombre en una posicion: lo que importa es que ninguna
    # fila comprobada aparezca antes que una sin comprobar. Fijar un nombre en una posicion
    # haria que el test dependiera del alfabeto de los nombres, que no es lo que se comprueba.
    estados = [f.has_vulnerabilities for f, _n, _c in filas]
    assert estados[0] is None
    assert estados[1] is None
    assert estados[-1] is False


# --------------------------------------------------------------------------- #
# Los endpoints
# --------------------------------------------------------------------------- #


async def test_el_endpoint_de_listado_trae_el_nombre_del_repositorio() -> None:
    """El nombre del repositorio viene con la fila, y no en una consulta por fila.

    Con treinta filas y un `N+1` serian treinta consultas en cada recarga de la tabla: la
    respuesta no se nota, pero la base recibe treinta veces el trabajo.
    """

    async with _sesion() as sesion:
        tenant = await _tenant(sesion, "sc")
        repositorio = await _repositorio(sesion, tenant.organization_id, "cliente/api")
        await service.indexar_manifiesto(
            sesion,
            organization_id=tenant.organization_id,
            repository_id=repositorio.id,
            manifest_path="package.json",
            contenido=MANIFIESTO,
        )
        await sesion.commit()

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as cliente:
            respuesta = await cliente.get("/api/v1/supply-chain/packages", headers=tenant.headers)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["total"] == 3
    assert all(item["repository_name"] == "cliente/api" for item in cuerpo["items"])
    # Y el estado de "no comprobado" viaja como `null`, no como `false`.
    assert all(item["has_vulnerabilities"] is None for item in cuerpo["items"])


async def test_el_endpoint_devuelve_404_al_indexar_un_repositorio_ajeno() -> None:
    """`404` y no `403`: un `403` confirmaria que el repositorio existe."""

    async with _sesion() as sesion:
        victima = await _tenant(sesion, "sc-victima")
        atacante = await _tenant(sesion, "sc-atacante")
        repositorio = await _repositorio(sesion, victima.organization_id, "competidor/api")

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as cliente:
            respuesta = await cliente.post(
                "/api/v1/supply-chain/packages/index",
                params={"repository_id": str(repositorio.id)},
                json={"manifest_path": "package.json", "content": MANIFIESTO},
                headers=atacante.headers,
            )

    assert respuesta.status_code == 404
    assert respuesta.json()["detail"] == "No encontrado"


async def test_el_endpoint_de_indexar_devuelve_los_cuentos() -> None:
    async with _sesion() as sesion:
        tenant = await _tenant(sesion, "sc")
        repositorio = await _repositorio(sesion, tenant.organization_id, "cliente/api")

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as cliente:
            respuesta = await cliente.post(
                "/api/v1/supply-chain/packages/index",
                params={"repository_id": str(repositorio.id)},
                json={"manifest_path": "package.json", "content": MANIFIESTO},
                headers=tenant.headers,
            )

    assert respuesta.status_code == 201
    cuerpo: dict[str, Any] = respuesta.json()
    assert cuerpo["inserted"] == 3
    assert cuerpo["total"] == 3


async def test_el_resumen_por_endpoint() -> None:
    async with _sesion() as sesion:
        tenant = await _tenant(sesion, "sc")
        repositorio = await _repositorio(sesion, tenant.organization_id, "cliente/api")
        await service.indexar_manifiesto(
            sesion,
            organization_id=tenant.organization_id,
            repository_id=repositorio.id,
            manifest_path="package.json",
            contenido=MANIFIESTO,
        )
        await sesion.commit()

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as cliente:
            respuesta = await cliente.get("/api/v1/supply-chain/summary", headers=tenant.headers)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["total_dependencies"] == 3
    assert cuerpo["unchecked"] == 3
    assert cuerpo["vulnerable"] == 0
    assert cuerpo["clean"] == 0
