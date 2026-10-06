"""Los escaneos en cola no se tapan entre páginas, y el historial de entregas tampoco.

## Por qué este fichero existe aparte de `test_orden_paginado.py`

Porque el defecto que cubre el resto de ese fichero es un **empate** entre filas que comparten
una marca temporal. Aquí hay otro, que no es un empate sino una columna entera de nulos, y que
necesita su propia demostración.

`started_at` de `pentest_runs` es **anulable**, y un run `QUEUED` lo tiene a `NULL` por
definición: todavía no ha empezado. La consola de operaciones ordena por
`started_at ASC NULLS FIRST`, así que **todos** los runs en cola comparten valor de orden. No es
que empaten por casualidad: empatan por definición, y en una plataforma con diez escaneos
esperando son la mayoría de la lista.

Con `limit`/`offset` encima, de cada fila solo se lee una vez, así que una fila tapada no es una
fila invisible: sale en la página 2. Lo que el empate produce es peor de medir y más difícil de
ver: **el reparto entre páginas no está decidido**, de modo que dos peticiones idénticas pueden
traer páginas distintas y el operador ve la misma cola con huecos y duplicados.

Por eso la prueba de este caso no puede ser la genérica. La genérica comprueba que la página 1
no cambia al reescribir una fila, y aquí eso **pasa igual con el defecto puesto**: dos lecturas
seguidas de la misma consulta dan el mismo orden aunque ese orden sea arbitrario, porque el
planificador no cambia de plan entre una y otra. Lo que hay que comprobar es el **contrato**:
que el orden sea `(started_at ASC NULLS FIRST, id ASC)`. Eso no puede fallar por casualidad, y
por eso los `id` se fuerzan a `uuid.UUID(int=n)` y las filas se insertan en orden creciente:
con el defecto, la base devuelve las filas en orden de inserción y la comprobación se rompe.

## Las tres consultas que se comprueban

1. `GET /api/v1/admin/operations/scans`: `started_at ASC NULLS FIRST` sin desempate.
2. `GET /api/v1/admin/operations/containers`: el mismo `ORDER BY` en la vista de contenedores,
   donde **toda** la lista es de runs en curso y el grupo de nulos es la página entera.
3. `GET /api/v1/webhooks/{id}/deliveries`: `created_at DESC, attempt DESC` sin tercer criterio,
   donde `attempt` **no es único** y dos eventos distintos pueden llevar los dos `attempt = 1`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.api_access.schemas import ApiTokenCreate
from backend.apps.api_access.service import create_api_token
from backend.apps.organizations.models import (
    Membership,
    Organization,
    PlanTierEnum,
    RoleEnum,
    User,
)
from backend.apps.pentests.models import PentestRun, ScanModeEnum, ScanStatusEnum, TargetTypeEnum
from backend.apps.webhooks.models import WebhookDelivery, WebhookEndpoint
from backend.core.security import create_access_token, hash_password
from backend.main import app

pytestmark = pytest.mark.integration

#: Marca temporal fija de los runs que **sí** han empezado. Los que están en cola llevan
#: `started_at = None`, que es el caso que interesa.
MARCA = datetime(2026, 1, 1, tzinfo=UTC)

#: Marca **futura** para las vistas de la consola de plataforma. Esas vistas no filtran por
#: organización —a propósito: son globales—, así que en la base de demostración hay runs de
#: organizaciones antiguas y los de esta prueba se irían al final de cualquier página. Con una
#: marca en el futuro los runs **en curso** de esta prueba salen delante, y los en cola —con
#: `NULLS FIRST`— salen por delante de esos. Las dos cosas a la vez son las que hacen falta.
MARCA_CONSOLA = datetime.now(UTC) + timedelta(days=365)

FILAS = 8
LIMITE = 4


class Tenant:
    def __init__(self, organization: Organization, user: User) -> None:
        self.organization_id = organization.id
        self.headers = {
            "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
            "X-Organization-Id": str(organization.id),
        }


async def _tenant(
    session: AsyncSession, *, superuser: bool = False, prefix: str = "nulos"
) -> Tenant:
    suffix = uuid.uuid4().hex
    organization = Organization(
        name=f"{prefix} {suffix}",
        slug=f"{prefix}-{suffix}",
        plan_tier=PlanTierEnum.PRO,
        credit_balance=0,
    )
    user = User(
        email=f"{prefix}-{suffix}@example.com",
        hashed_password=hash_password("NoSeUsa"),
        full_name=f"{prefix} User",
        email_verified=True,
        is_superuser=superuser,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.ADMIN))
    await session.commit()
    return Tenant(organization, user)


#: Desplazamiento de los `id` forzados. Hace falta porque `uuid.UUID(int=n)` produce
#: identificadores tan bajos que se parecen a los de otra prueba del árbol: `test_orden_paginado`
#: fuerza `uuid.UUID(int=1..8)` en `scanner_agents`, `agent_jobs`, `api_tokens`,
#: `pull_request_reviews` y `webhook_endpoints`, y **`pentest_runs` comparte base con todos
#: ellos**. Como las pruebas de integración comparten la base de datos real —cada una en su
#: transacción, pero las transacciones no se pisan entre archivos— sembrar `00000000-...-0001`
#: en `pentest_runs` choca con lo que otra prueba haya sembrado mientras esta corría.
#:
#: El desplazamiento conserva lo que hace falta: que los `id` sean **consecutivos y en orden
#: conocido**, que es lo único que hace determinista la prueba. Y `uuid.UUID(int=...)` no es un
#: `uuid4`: su bit de versión no es 4, así que no colisiona con ningún `uuid4` generado por la
#: aplicación, que es de donde salen los `id` reales.
DESPLAZAMIENTO_DE_IDS = 1_000_000


def _bloque_de_ids(desplazamiento: int, cantidad: int = FILAS) -> list[uuid.UUID]:
    """`cantidad` identificadores consecutivos a partir de `desplazamiento`.

    El propósito es que **orden de inserción** y **`id` ascendente** sean cosas distintas: con
    `uuid4` aleatorios no se puede afirmar nada sobre el orden que devuelve la base cuando el
    `ORDER BY` empata, y la prueba se volvería una ruleta.

    El `desplazamiento` es lo que permite tener **dos** bloques disjuntos en una misma prueba,
    que es lo que hace falta para afirmar el orden relativo entre dos grupos.
    """

    return [uuid.UUID(int=n) for n in range(desplazamiento + 1, desplazamiento + 1 + cantidad)]


def _ids_crecientes(cantidad: int = FILAS) -> list[uuid.UUID]:
    """El bloque de identificadores de este fichero, con su desplazamiento ya aplicado."""

    return _bloque_de_ids(DESPLAZAMIENTO_DE_IDS, cantidad)


async def _perturbar(
    session: AsyncSession, modelo: type[object], identificador: uuid.UUID, **columnas: object
) -> None:
    """Reescribe columnas que **no** están en el `ORDER BY` de la fila dada.

    Es lo que hace un contenedor que se crea o un run que arranca: la fila cambia de sitio en el
    heap y su marca temporal no se mueve. Sin desempate, el orden depende de dónde estén las
    tuplas; con desempate, depende de los valores de las columnas.
    """

    await session.execute(
        update(modelo).where(modelo.id == identificador).values(**columnas)  # type: ignore[attr-defined]
    )
    # Las instancias ya cargadas por la sesión tienen los valores de antes. Sin esto, la
    # segunda petición leería el mapa de identidad y no la base.
    session.expire_all()


def _ids_de_pagina(response: Response) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


def _run_ids_de_pagina(response: Response) -> list[str]:
    """Los identificadores de la página de **contenedores**.

    La clave del ítem es `run_id` y no `id` porque un contenedor no tiene entidad propia: se
    deriva del run que lo creó. Es el mismo nombre en el esquema que en la lista de escaneos, y
    por eso hace falta un ayudante aparte en vez de reutilizar el de arriba.
    """

    assert response.status_code == 200, response.text
    return [item["run_id"] for item in response.json()["items"]]


def _sin_solapes(primera: list[str], segunda: list[str], esperadas: list[str]) -> None:
    """Ni repeticiones entre páginas ni filas perdidas, con mensajes que dicen qué pasó."""

    assert set(primera) & set(segunda) == set(), (
        f"la página 2 repite filas de la 1: {sorted(set(primera) & set(segunda))}"
    )
    assert sorted(primera + segunda) == sorted(esperadas), (
        "las dos páginas juntas no son las filas sembradas: "
        f"faltan {sorted(set(esperadas) - set(primera + segunda))}, "
        f"sobran {sorted(set(primera + segunda) - set(esperadas))}"
    )


def _en_cola(identificador: uuid.UUID, organization_id: uuid.UUID) -> PentestRun:
    """Un run **en cola**: `status = QUEUED` y `started_at = NULL`, que no es opcional.

    Es la fila que hace que esta prueba no se parezca a las demás: no comparte marca con nadie
    porque no tiene marca, y `NULLS FIRST` la pone delante de todas las demás.
    """

    return PentestRun(
        id=identificador,
        organization_id=organization_id,
        target_type=TargetTypeEnum.REPOSITORY,
        target_identifier=f"en-cola/{identificador.int:04d}",
        scan_mode=ScanModeEnum.STANDARD,
        status=ScanStatusEnum.QUEUED,
        # `started_at` se deja a propósito: es el `None` que ordena esta lista.
        started_at=None,
        created_at=MARCA_CONSOLA,
    )


def _en_curso(identificador: uuid.UUID, organization_id: uuid.UUID) -> PentestRun:
    """Un run ya empezado, con `started_at` en el futuro para que salga detrás de los en cola."""

    return PentestRun(
        id=identificador,
        organization_id=organization_id,
        target_type=TargetTypeEnum.REPOSITORY,
        target_identifier=f"en-curso/{identificador.int:04d}",
        scan_mode=ScanModeEnum.STANDARD,
        status=ScanStatusEnum.RUNNING,
        started_at=MARCA_CONSOLA,
        created_at=MARCA_CONSOLA,
    )


# --------------------------------------------------------------------------- #
# Escaneos de la consola: GET /api/v1/admin/operations/scans
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_los_escaneos_en_cola_no_se_tapan_entre_paginas(
    integration_session: AsyncSession,
) -> None:
    """`GET /api/v1/admin/operations/scans` con `started_at ASC NULLS FIRST` sin desempate.

    ## Qué demuestra y por qué hace falta una prueba propia

    Que **`id` sí basta** para un `NULLS FIRST` sobre una columna entera de nulos, y que no hace
    falta tocar el `NULLS FIRST`. El motivo es que `NULLS FIRST` no es un empate: es una regla
    que coloca **todos** los nulos delante de todos los no nulos, y dentro de ese grupo sigue
    haciendo falta un criterio. `id` cierra ese grupo. No hay ningún otro problema que un `id`
    no resuelva aquí, y eso es lo que la prueba demuestra.

    Lo que la prueba **no** hace —y por eso no se ha reutilizado la genérica— es esperar que dos
    peticiones difieran. Con el defecto puesto, dos peticiones idénticas seguidos dan el mismo
    orden, porque el planificador no cambia de plan entre medias: el empate se reparte de una
    manera, pero de forma *consistente*. La prueba genérica de «la página 1 no cambia» pasaría
    en verde con el defecto, así que aquí se afirma el **contrato** en vez de la estabilidad.

    Se siembran ocho runs en cola con `id` creciente, y se afirma que las dos páginas juntas son
    las ocho filas en orden de `id` **ascendente**. Sin desempate, la base las devuelve en orden
    de inserción —que aquí es el mismo orden, y por eso los `id` se siembran en orden
    **decreciente** para que las dos cosas sean distintas— y la comprobación falla.

    ## Por qué la perturbación es la que importa

    Reescribir `container_id` de un run lo mueve de sitio en el heap sin tocar `started_at`. Con
    desempate el orden no se mueve porque depende de los valores de las columnas; sin él, depende
    de dónde estén las tuplas. Y `container_id` es justo lo que escribe el sistema cuando el
    contenedor arranca, así que no es una perturbación artificial: es producción.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session, superuser=True, prefix="nulos-scans")
    # Orden de inserción **decreciente**: si la base devolviera las filas como se insertaron,
    # saldrían al revés del contrato y la comparación falla.
    ids = list(reversed(_ids_crecientes()))
    integration_session.add_all([_en_cola(i, tenant.organization_id) for i in ids])
    await integration_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        primera = _ids_de_pagina(
            await client.get(
                "/api/v1/admin/operations/scans",
                params={"limite": LIMITE, "desplazamiento": 0},
                headers=tenant.headers,
            )
        )
        await _perturbar(
            integration_session, PentestRun, ids[0], container_id="fenix-strix-medicion"
        )
        primera_de_nuevo = _ids_de_pagina(
            await client.get(
                "/api/v1/admin/operations/scans",
                params={"limite": LIMITE, "desplazamiento": 0},
                headers=tenant.headers,
            )
        )
        segunda = _ids_de_pagina(
            await client.get(
                "/api/v1/admin/operations/scans",
                params={"limite": LIMITE, "desplazamiento": LIMITE},
                headers=tenant.headers,
            )
        )

    assert primera_de_nuevo == primera, "la página 1 cambió al reescribir una fila"
    _sin_solapes(primera, segunda, [str(i) for i in ids])
    esperados = [str(i) for i in sorted(ids)]
    assert primera + segunda == esperados, (
        "el orden no es (started_at ASC NULLS FIRST, id ASC): los runs en cola comparten valor "
        "de orden y, sin desempate, la base los devuelve en orden de inserción\n"
        f"  obtenido:  {primera + segunda}\n  esperado:  {esperados}"
    )


@pytest.mark.asyncio
async def test_los_escaneos_en_cola_no_tapan_a_los_que_si_han_empezado(
    integration_session: AsyncSession,
) -> None:
    """El `NULLS FIRST` se queda, y los en cola salen **delante** de los que sí han empezado.

    Esta prueba existe para que nadie «arregle» el defecto quitando el `NULLS FIRST`. Es un
    cambio de comportamiento, no de orden: si los runs en cola se fueran al final, la pantalla
    de «qué está colgado» empezaría por los runs que llevan cuarenta minutos colgados y
    acabaría por los que están esperando su turno, que es justo al revés de como se usa.

    Así que el contrato tiene **dos** mitades y aquí se comprueban las dos: los en cola
    (`started_at IS NULL`) delante, y dentro de cada grupo ordenado por `id`.

    ## Por qué el filtro por organización no es opcional aquí

    Porque la consola de operaciones es **deliberadamente global** —no filtra por tenant, que es
    justo lo que la hace útil—, así que sin `organization_id` la respuesta trae también los runs
    de las organizaciones antiguas que quedan en la base de demostración. Se filtra por
    organización, que es un parámetro que el endpoint acepta y que el operador usa a diario, y así
    la aserción es sobre los ocho runs de esta prueba y no sobre lo que hay alrededor.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session, superuser=True, prefix="nulos-mezcla")
    en_cola = list(reversed(_ids_crecientes(4)))
    # Los cuatro ya empezados van con `id` más bajo, para que un orden por `id` sin más —
    # o un `NULLS LAST`— los traicionaría. Es un bloque aparte porque los dos grupos
    # comparten tabla y los `id` tienen que ser distintos.
    en_curso = _bloque_de_ids(DESPLAZAMIENTO_DE_IDS // 2, 4)
    integration_session.add_all([_en_cola(i, tenant.organization_id) for i in en_cola])
    integration_session.add_all([_en_curso(i, tenant.organization_id) for i in en_curso])
    await integration_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        todos = _ids_de_pagina(
            await client.get(
                "/api/v1/admin/operations/scans",
                params={
                    "limite": FILAS,
                    "desplazamiento": 0,
                    "organization_id": str(tenant.organization_id),
                },
                headers=tenant.headers,
            )
        )

    esperados = [str(i) for i in sorted(en_cola)] + [str(i) for i in sorted(en_curso)]
    assert todos == esperados, (
        "el contrato es (started_at ASC NULLS FIRST, id ASC): primero los en cola, que no han "
        "empezado, y dentro de cada grupo ordenados por id\n"
        f"  obtenido:  {todos}\n  esperado:  {esperados}"
    )


# --------------------------------------------------------------------------- #
# Contenedores de la consola: GET /api/v1/admin/operations/containers
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_los_contenedores_no_solapan_paginas_con_runs_en_cola(
    integration_session: AsyncSession,
) -> None:
    """`GET /api/v1/admin/operations/containers`, que es el peor caso del mismo defecto.

    Aquí la lista **solo** contiene runs `QUEUED` y `RUNNING`, así que el grupo de nulos no es
    una coincidencia: es la lista entera. Un run `QUEUED` no ha empezado, luego `started_at` es
    `NULL`; y como `NULLS FIRST` los pone delante, la primera página es una página de nulos
    **siempre**, no solo cuando hay muchos escaneos esperando.

    La prueba afirma las tres cosas del mismo contrato: la página 1 no cambia al reescribir una
    fila, las dos páginas no se solapan, y el orden dentro de cada página es el de `id`
    ascendente. Los `id` se siembran en orden **decreciente** para que el orden de inserción no
    coincida con el del contrato.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session, superuser=True, prefix="nulos-contenedores")
    ids = list(reversed(_ids_crecientes()))
    integration_session.add_all([_en_cola(i, tenant.organization_id) for i in ids])
    await integration_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        pagina = await client.get(
            "/api/v1/admin/operations/containers",
            params={"limite": LIMITE, "desplazamiento": 0},
            headers=tenant.headers,
        )
        assert pagina.status_code == 200, pagina.text
        assert all(
            item["started_at"] is None for item in pagina.json()["items"]
        ), "esta prueba solo vale si la primera página es de runs sin empezar"
        primera = [item["run_id"] for item in pagina.json()["items"]]
        await _perturbar(
            integration_session, PentestRun, ids[0], container_id="fenix-strix-medicion"
        )
        primera_de_nuevo = _run_ids_de_pagina(
            await client.get(
                "/api/v1/admin/operations/containers",
                params={"limite": LIMITE, "desplazamiento": 0},
                headers=tenant.headers,
            )
        )
        segunda = _run_ids_de_pagina(
            await client.get(
                "/api/v1/admin/operations/containers",
                params={"limite": LIMITE, "desplazamiento": LIMITE},
                headers=tenant.headers,
            )
        )

    assert primera_de_nuevo == primera, "la página 1 cambió al reescribir una fila"
    _sin_solapes(primera, segunda, [str(i) for i in ids])
    esperados = [str(i) for i in sorted(ids)]
    assert primera + segunda == esperados, (
        "el orden no es (started_at ASC NULLS FIRST, id ASC)\n"
        f"  obtenido:  {primera + segunda}\n  esperado:  {esperados}"
    )


# --------------------------------------------------------------------------- #
# Historial de entregas: GET /api/v1/webhooks/{id}/deliveries
# --------------------------------------------------------------------------- #


def _entrega(
    identificador: uuid.UUID,
    endpoint_id: uuid.UUID,
    organization_id: uuid.UUID,
    marca: datetime,
    intento: int = 1,
) -> WebhookDelivery:
    return WebhookDelivery(
        id=identificador,
        endpoint_id=endpoint_id,
        organization_id=organization_id,
        event_type="pentest.completed",
        payload={"evento": "prueba de orden"},
        status_code=200,
        attempt=intento,
        created_at=marca,
    )


@pytest.mark.asyncio
async def test_las_entradas_con_el_mismo_intento_no_se_tapan_entre_paginas(
    integration_session: AsyncSession,
) -> None:
    """`GET /api/v1/webhooks/{id}/deliveries`: `created_at DESC, attempt DESC` sin tercer criterio.

    ## Por qué este caso necesita un desempate aunque `attempt` esté

    Porque **`attempt` no es único**, y no por accidente: cuenta los intentos **de un mismo
    evento**, así que dos eventos distintos pueden llevar los dos `attempt = 1`. Y como
    `created_at` es `now()` de servidor, dos entregas de eventos distintos que salen en la
    misma transacción comparten marca. Los dos criterios empatan **a la vez**, que es el caso
    normal y no el raro: un webhook que recibe cinco eventos en un lote produce cinco filas con
    `attempt = 1` y la misma marca.

    Con solo esos dos, el reparto entre las páginas lo decide el planificador: la página 2
    repite filas de la 1 y se come otras. Con `id` detrás, el orden es total.

    ## Por qué la perturbación es `status_code`

    Porque es lo que escribe el dispatcher en cada intento —la respuesta del destino—, y no está
    en el `ORDER BY`. Es el mismo patrón de las demás pruebas del fichero: una columna que se
    reescribe en producción y cuya reescritura mueve la tupla de sitio en el heap sin tocar la
    marca temporal.

    Los `id` se siembran en orden **decreciente** para que el orden de inserción no coincida con
    el del contrato, que es `id` **descendente**: si la base devolviera las filas como se
    insertaron, saldrían al revés.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session, prefix="nulos-entregas")
    endpoint = WebhookEndpoint(
        organization_id=tenant.organization_id,
        url="https://ejemplo.invalid/entregas",
        encrypted_secret="secreto-de-prueba-entregas",
        event_types=["pentest.completed"],
        created_at=MARCA,
    )
    integration_session.add(endpoint)
    await integration_session.flush()
    # Se copia a una variable local porque `_perturbar` llama a `expire_all()`, y leer
    # `endpoint.id` después dispararía una carga perezosa **fuera** del bucle de eventos, que
    # es un `MissingGreenlet`. El identificador no va a cambiar: es la clave primaria y hay un
    # trigger que impide además moverlo.
    endpoint_id = endpoint.id

    # Cinco eventos distintos, los cinco con `attempt = 1` y todos con la misma `created_at`.
    # Es exactamente lo que produce un webhook que recibe un lote.
    ids = list(reversed(_ids_crecientes()))
    integration_session.add_all(
        [_entrega(i, endpoint_id, tenant.organization_id, MARCA, intento=1) for i in ids]
    )
    await integration_session.commit()

    _token, raw = await create_api_token(
        integration_session,
        tenant.organization_id,
        ApiTokenCreate(name="prueba", scopes=["webhooks:read"], expires_in_days=30),
    )
    cabeceras = {"Authorization": f"Bearer {raw}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        primera = _ids_de_pagina(
            await client.get(
                f"/api/v1/webhooks/{endpoint_id}/deliveries",
                params={"limit": LIMITE, "offset": 0},
                headers=cabeceras,
            )
        )
        await _perturbar(integration_session, WebhookDelivery, ids[0], status_code=500)
        primera_de_nuevo = _ids_de_pagina(
            await client.get(
                f"/api/v1/webhooks/{endpoint_id}/deliveries",
                params={"limit": LIMITE, "offset": 0},
                headers=cabeceras,
            )
        )
        segunda = _ids_de_pagina(
            await client.get(
                f"/api/v1/webhooks/{endpoint_id}/deliveries",
                params={"limit": LIMITE, "offset": LIMITE},
                headers=cabeceras,
            )
        )

    assert primera_de_nuevo == primera, "la página 1 cambió al reescribir una fila"
    _sin_solapes(primera, segunda, [str(i) for i in ids])
    esperados = [str(i) for i in sorted(ids, reverse=True)]
    assert primera + segunda == esperados, (
        "el orden no es (created_at DESC, attempt DESC, id DESC): cinco eventos distintos "
        "comparten marca y los cinco llevan attempt = 1, así que sin `id` el reparto lo decide "
        "el planificador\n"
        f"  obtenido:  {primera + segunda}\n  esperado:  {esperados}"
    )
