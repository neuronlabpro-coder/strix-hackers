"""El orden de un listado no puede depender del planificador.

## Qué defecto cubre este fichero

Once listados ordenaban por una columna que **no** es única —casi siempre un `now()` de
servidor— y nada más. Con eso, dos consultas idénticas pueden devolver las páginas en distinto
orden: la página 2 repite filas de la página 1 y se come otras, y el usuario ve la misma
entrada dos veces y pierde una sin que nada lo avise. El `total` sale bien y los
identificadores son correctos, así que la pantalla parece correcta.

## Por qué la prueba intercala dos peticiones y no solo mira una

Porque el defecto **no** está en una consulta: está entre dos. Una única consulta con empate
siempre devuelve *algún* orden, y ese orden puede ser el correcto o no. Lo que hay que
comprobar es que dos peticiones del mismo listado, con una perturbación en medio que no toca
la columna del `ORDER BY`, devuelven **la misma** página 1.

Y por eso la perturbación es un `UPDATE` de una columna que no está en el `ORDER BY`: es lo
que pasa en producción de verdad. Un `last_seen_at` que se escribe, un `status` que avanza, un
`result` que llega. La fila cambia de posición física —PostgreSQL escribe la versión nueva al
final del heap— y su marca temporal no se mueve. Con desempate por clave primaria el orden no
cambia porque depende de los **valores** de las columnas; sin él, depende de dónde estén las
tuplas.

## Por qué además se comprueba el orden exacto

Porque hay listados donde la perturbación **no** llega a mover nada, y una sola comprobación
se quedaría corta. Ahí lo que afirma la prueba es el **contrato**: el orden es
`(columna DESC, id DESC)`. El `id` de cada fila se fuerza con `uuid.UUID(int=n)` y las filas se
insertan en orden **creciente**, de modo que "orden de inserción" y "`id` descendente" son
cosas distintas: sin el desempate la base devuelve las filas en orden de inserción y la
comprobación falla. Eso hace que la prueba sea determinista en lugar de depender de que el heap
se mueva por casualidad.

## Lo que se comprueba en cada listado paginado

1. La página 1 es la misma antes y después de la perturbación.
2. La página 2 no repite ninguna fila de la página 1.
3. Las dos páginas juntas son exactamente las filas que se sembraron.
4. El orden dentro de cada página es el del contrato `(columna DESC, id DESC)`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.agents.models import (
    AgentJob,
    AgentJobKindEnum,
    AgentJobStatusEnum,
    ScannerAgent,
)
from backend.apps.agents.service import huella_resultado
from backend.apps.api_access.models import ApiToken
from backend.apps.api_access.schemas import ApiTokenCreate
from backend.apps.api_access.service import create_api_token, list_api_tokens
from backend.apps.knowledge.documents import KnowledgeDocTypeEnum, WorkspaceKnowledgeDocument
from backend.apps.knowledge.retrieval import recuperar_documentos
from backend.apps.organizations.models import (
    Membership,
    Organization,
    PlanTierEnum,
    RoleEnum,
    User,
)
from backend.apps.pentests.models import PentestRun, ScanModeEnum, ScanStatusEnum, TargetTypeEnum
from backend.apps.repositories.models import (
    GitProviderEnum,
    PRReviewStatusEnum,
    PullRequestReview,
    Repository,
)
from backend.apps.vulnerabilities.models import SeverityEnum, Vulnerability
from backend.apps.webhooks.models import WebhookEndpoint
from backend.core.security import create_access_token, hash_password
from backend.main import app

pytestmark = pytest.mark.integration

#: Marca temporal fija. Si se sembrara con `server_default=func.now()`, todas las filas de una
#: prueba compartirían la marca de la transacción —que es justo lo que dispara el defecto—, y
#: además el valor dependería de cuándo se ejecutó. Fijarla hace la prueba repetible.
MARCA = datetime(2026, 1, 1, tzinfo=UTC)

#: Marca **futura** para las vistas de la consola de plataforma. Esas vistas no filtran por
#: organización (a propósito: son globales), así que en la base de demostración hay filas de
#: miles de organizaciones antiguas y las del tenant de esta prueba se irían al final de
#: cualquier página. Con una marca en el futuro quedan arriba, que es lo que hace falta para
#: que la página 1 sea de este test.
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
    session: AsyncSession, *, superuser: bool = False, prefix: str = "orden"
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


def _ids_crecientes(cantidad: int = FILAS) -> list[uuid.UUID]:
    """Identificadores forzados y crecientes.

    El propósito es que **orden de inserción** y **`id` descendente** sean cosas distintas: con
    los `id` aleatorios de `uuid4` no se puede afirmar nada sobre el orden que devuelve la base
    cuando el `ORDER BY` empata, y la prueba se volvería una ruleta.
    """

    return [uuid.UUID(int=n) for n in range(1, cantidad + 1)]


async def _perturbar(
    session: AsyncSession, modelo: type[object], identificador: uuid.UUID, **columnas: object
) -> None:
    """Reescribe columnas que **no** están en el `ORDER BY` de la fila dada.

    Es lo que hace un `last_seen_at` que se escribe o un `status` que avanza: la fila cambia de
    sitio en el heap y su marca temporal no se mueve.
    """

    await session.execute(
        update(modelo).where(modelo.id == identificador).values(**columnas)  # type: ignore[attr-defined]
    )
    # Las instancias ya cargadas por la sesión tienen los valores de antes. Sin esto, la
    # segunda petición leería el mapa de identidad y no la base, y la prueba no probaría nada.
    session.expire_all()


def _ids_de_pagina(response: Response) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


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


def _orden_esperado(primera: list[str], ids: list[uuid.UUID]) -> None:
    """El orden de la página 1 es `(columna DESC, id DESC)`.

    Los `id` se siembran en orden creciente, así que el orden de inserción es justo el
    contrario: si la base devolviera las filas como las insertó, la comparación falla.
    """

    esperados = [str(i) for i in reversed(ids)][: len(primera)]
    assert primera == esperados, (
        "el orden no es (columna DESC, id DESC): sin desempate la base devuelve las filas en "
        f"orden de inserción.\n  primera:  {primera}\n  esperado: {esperados}"
    )


def _agente(identificador: uuid.UUID, organization_id: uuid.UUID, marca: datetime) -> ScannerAgent:
    return ScannerAgent(
        id=identificador,
        organization_id=organization_id,
        name=f"agente-{identificador.int:04d}",
        # `token_hash` es único en toda la base, así que el nombre va dentro del hash.
        token_hash=f"hash-de-orden::{identificador}",
        token_prefix="fx_abcd1234",
        sistema_objetivo="linux",
        enrolled_at=marca,
    )


def _revision(
    identificador: uuid.UUID,
    organization_id: uuid.UUID,
    repositorio_id: uuid.UUID,
    marca: datetime,
) -> PullRequestReview:
    numero = identificador.int
    return PullRequestReview(
        id=identificador,
        organization_id=organization_id,
        repository_id=repositorio_id,
        pr_number=numero,
        pr_title=f"PR {numero}",
        pr_author="autor",
        source_branch="feature",
        target_branch="main",
        commit_sha=f"{numero:040x}",
        base_sha=f"{numero + 1000:040x}",
        created_at=marca,
        updated_at=marca,
    )


def _trabajo(identificador: uuid.UUID, organization_id: uuid.UUID, marca: datetime) -> AgentJob:
    return AgentJob(
        id=identificador,
        organization_id=organization_id,
        kind=AgentJobKindEnum.CONTAINER_SCAN,
        target=f"alpine:3.20:{identificador.int:04d}",
        # `QUEUED` y no un estado terminal: el trigger de evidencia de `agent_jobs` no deja
        # reescribir nada de un trabajo terminado, y la perturbación tiene que poder tocarlo.
        status=AgentJobStatusEnum.QUEUED,
        created_at=marca,
    )


async def _repositorio(session: AsyncSession, organization_id: uuid.UUID) -> Repository:
    repositorio = Repository(
        organization_id=organization_id,
        provider=GitProviderEnum.GITHUB,
        remote_repo_id=f"orden-{uuid.uuid4().hex[:8]}",
        name="repo",
        full_name="orden/repo",
        clone_url="https://ejemplo.invalid/orden.git",
        webhook_secret="secreto-de-prueba",
    )
    session.add(repositorio)
    await session.flush()
    return repositorio


# --------------------------------------------------------------------------- #
# Agentes del tenant: GET /api/v1/agents
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_los_agentes_no_solapan_paginas_al_entre_dos_peticiones(
    integration_session: AsyncSession,
) -> None:
    """`GET /api/v1/agents`, ordenado por `enrolled_at` sin desempate.

    `enrolled_at` es `now()` de servidor: dos agentes dados de alta en la misma transacción
    comparten marca, y el reparto de ese empate entre las páginas lo decide el planificador.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session)
    ids = _ids_crecientes()
    integration_session.add_all([_agente(i, tenant.organization_id, MARCA) for i in ids])
    await integration_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        primera = _ids_de_pagina(
            await client.get(
                "/api/v1/agents", params={"limit": LIMITE, "offset": 0}, headers=tenant.headers
            )
        )
        await _perturbar(integration_session, ScannerAgent, ids[0], last_seen_at=datetime.now(UTC))
        primera_de_nuevo = _ids_de_pagina(
            await client.get(
                "/api/v1/agents", params={"limit": LIMITE, "offset": 0}, headers=tenant.headers
            )
        )
        segunda = _ids_de_pagina(
            await client.get(
                "/api/v1/agents",
                params={"limit": LIMITE, "offset": LIMITE},
                headers=tenant.headers,
            )
        )

    assert primera_de_nuevo == primera, "la página 1 cambió al reescribir una fila"
    _sin_solapes(primera, segunda, [str(i) for i in ids])
    _orden_esperado(primera, ids)


# --------------------------------------------------------------------------- #
# Consola de agentes: GET /api/v1/admin/agents
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_la_consola_de_agentes_no_solapa_paginas(
    integration_session: AsyncSession,
) -> None:
    """`GET /api/v1/admin/agents`: el mismo listado, pero en `JOIN` con `organizations`.

    El `JOIN` es por la clave primaria de la organización, o sea que es de uno a uno y no
    multiplica filas. Aun así, el desempate tiene que ir sobre `scanner_agents.id`: la tabla
    que se pagina es esa, y `ScannerAgent.id` sigue identificando cada fila de la salida.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session, superuser=True, prefix="orden-consola")
    ids = _ids_crecientes()
    integration_session.add_all([_agente(i, tenant.organization_id, MARCA_CONSOLA) for i in ids])
    await integration_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        primera = _ids_de_pagina(
            await client.get(
                "/api/v1/admin/agents",
                params={"limit": LIMITE, "offset": 0},
                headers=tenant.headers,
            )
        )
        await _perturbar(integration_session, ScannerAgent, ids[0], last_seen_at=datetime.now(UTC))
        primera_de_nuevo = _ids_de_pagina(
            await client.get(
                "/api/v1/admin/agents",
                params={"limit": LIMITE, "offset": 0},
                headers=tenant.headers,
            )
        )
        segunda = _ids_de_pagina(
            await client.get(
                "/api/v1/admin/agents",
                params={"limit": LIMITE, "offset": LIMITE},
                headers=tenant.headers,
            )
        )

    assert primera_de_nuevo == primera, "la página 1 cambió al reescribir una fila"
    _sin_solapes(primera, segunda, [str(i) for i in ids])
    _orden_esperado(primera, ids)


# --------------------------------------------------------------------------- #
# Revisiones de la consola: GET /api/v1/admin/operations/reviews
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_las_revisiones_de_la_consola_no_solapan_paginas(
    integration_session: AsyncSession,
) -> None:
    """`GET /api/v1/admin/operations/reviews`, ordenado por `created_at` sin desempate."""

    assert integration_session is not None
    tenant = await _tenant(integration_session, superuser=True, prefix="orden-revisiones")
    repositorio = await _repositorio(integration_session, tenant.organization_id)
    ids = _ids_crecientes()
    integration_session.add_all(
        [_revision(i, tenant.organization_id, repositorio.id, MARCA_CONSOLA) for i in ids]
    )
    await integration_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        primera = _ids_de_pagina(
            await client.get(
                "/api/v1/admin/operations/reviews",
                params={"limite": LIMITE, "desplazamiento": 0},
                headers=tenant.headers,
            )
        )
        await _perturbar(
            integration_session,
            PullRequestReview,
            ids[0],
            status=PRReviewStatusEnum.PASSED,
        )
        primera_de_nuevo = _ids_de_pagina(
            await client.get(
                "/api/v1/admin/operations/reviews",
                params={"limite": LIMITE, "desplazamiento": 0},
                headers=tenant.headers,
            )
        )
        segunda = _ids_de_pagina(
            await client.get(
                "/api/v1/admin/operations/reviews",
                params={"limite": LIMITE, "desplazamiento": LIMITE},
                headers=tenant.headers,
            )
        )

    assert primera_de_nuevo == primera, "la página 1 cambió al reescribir una fila"
    _sin_solapes(primera, segunda, [str(i) for i in ids])
    _orden_esperado(primera, ids)


# --------------------------------------------------------------------------- #
# Trabajos de la consola: GET /api/v1/admin/operations/jobs
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_los_trabajos_de_la_consola_no_solapan_paginas(
    integration_session: AsyncSession,
) -> None:
    """`GET /api/v1/admin/operations/jobs`, ordenado por `created_at` sin desempate.

    Aquí el caso es peor que en las revisiones porque los trabajos **nacen a ráfaga**: un
    webhook con cinco eventos encola cinco trabajos en la misma transacción y los cinco
    comparten `created_at`.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session, superuser=True, prefix="orden-trabajos")
    ids = _ids_crecientes()
    integration_session.add_all([_trabajo(i, tenant.organization_id, MARCA_CONSOLA) for i in ids])
    await integration_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        primera = _ids_de_pagina(
            await client.get(
                "/api/v1/admin/operations/jobs",
                params={"limite": LIMITE, "desplazamiento": 0},
                headers=tenant.headers,
            )
        )
        await _perturbar(integration_session, AgentJob, ids[0], updated_at=datetime.now(UTC))
        primera_de_nuevo = _ids_de_pagina(
            await client.get(
                "/api/v1/admin/operations/jobs",
                params={"limite": LIMITE, "desplazamiento": 0},
                headers=tenant.headers,
            )
        )
        segunda = _ids_de_pagina(
            await client.get(
                "/api/v1/admin/operations/jobs",
                params={"limite": LIMITE, "desplazamiento": LIMITE},
                headers=tenant.headers,
            )
        )

    assert primera_de_nuevo == primera, "la página 1 cambió al reescribir una fila"
    _sin_solapes(primera, segunda, [str(i) for i in ids])
    _orden_esperado(primera, ids)


# --------------------------------------------------------------------------- #
# Trabajos del tenant: GET /api/v1/agents/jobs
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_los_trabajos_del_tenant_no_solapan_paginas(
    integration_session: AsyncSession,
) -> None:
    """`GET /api/v1/agents/jobs`, que es `listar_trabajos` en `agents/service.py`."""

    assert integration_session is not None
    tenant = await _tenant(integration_session, prefix="orden-jobs")
    ids = _ids_crecientes()
    integration_session.add_all([_trabajo(i, tenant.organization_id, MARCA) for i in ids])
    await integration_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        primera = _ids_de_pagina(
            await client.get(
                "/api/v1/agents/jobs", params={"limit": LIMITE, "offset": 0}, headers=tenant.headers
            )
        )
        await _perturbar(integration_session, AgentJob, ids[0], updated_at=datetime.now(UTC))
        primera_de_nuevo = _ids_de_pagina(
            await client.get(
                "/api/v1/agents/jobs", params={"limit": LIMITE, "offset": 0}, headers=tenant.headers
            )
        )
        segunda = _ids_de_pagina(
            await client.get(
                "/api/v1/agents/jobs",
                params={"limit": LIMITE, "offset": LIMITE},
                headers=tenant.headers,
            )
        )

    assert primera_de_nuevo == primera, "la página 1 cambió al reescribir una fila"
    _sin_solapes(primera, segunda, [str(i) for i in ids])
    _orden_esperado(primera, ids)


# --------------------------------------------------------------------------- #
# El resumen de la cabecera: GET /api/v1/agents/summary
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_el_resumen_de_la_cabecera_no_cambia_entre_dos_peticiones(
    integration_session: AsyncSession,
) -> None:
    """El `limit(200)` de `resumen_agente` sin desempate.

    Aquí **no** hay paginación: hay un tope. El síntoma tampoco es una página repetida, sino
    que el KPI de la cabecera se mueve solo entre dos pantallas sin que haya cambiado ningún
    escaneo, que es peor: un número que se corrige solo es un número que miente.

    Se siembran 205 trabajos —cinco más que el tope— todos con la misma `created_at` y con
    inventarios distintos, y se afirma el contrato dos veces: las dos llamadas dan lo mismo, y
    la cuenta es exactamente la de los 200 con `id` mayor. La segunda comprobación es la que
    no depende de que el heap se mueva, así que es determinista en lugar de afortunada.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session, prefix="orden-resumen")
    tope = 200
    total = tope + 5
    ids = _ids_crecientes(total)
    # Cada trabajo lleva un número de paquetes distinto, y el resumen los cuenta uno a uno
    # desde el `JSONB`. Los cinco con menos paquetes son los de `id` más bajo, así que la
    # suma esperada solo puede ser la de los 200 con `id` mayor.
    paquetes_por_fila: list[int] = []
    filas = []
    for indice, identificador in enumerate(ids):
        cuantos = indice % 7 + 1
        paquetes_por_fila.append(cuantos)
        resultado = {
            "referencia": "alpine:3.20",
            "total_capas": 1,
            "paquetes": [
                {"name": f"paquete-{numero}", "version": "1.0", "ecosystem": "apk"}
                for numero in range(cuantos)
            ],
        }
        trabajo = _trabajo(identificador, tenant.organization_id, MARCA)
        trabajo.result = resultado
        trabajo.result_digest = huella_resultado(resultado)
        filas.append(trabajo)
    integration_session.add_all(filas)
    await integration_session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        primera = (await client.get("/api/v1/agents/summary", headers=tenant.headers)).json()
        await _perturbar(integration_session, AgentJob, ids[0], updated_at=datetime.now(UTC))
        segunda = (await client.get("/api/v1/agents/summary", headers=tenant.headers)).json()

    assert segunda["total_imagenes"] == primera["total_imagenes"], (
        "el número de imágenes se movió solo entre dos peticiones"
    )
    assert segunda["total_paquetes"] == primera["total_paquetes"], (
        "el número de paquetes se movió solo entre dos peticiones"
    )
    # El tope es 200 de 205, así que quedan fuera cinco. Con `ORDER BY created_at DESC, id
    # DESC` quedan fuera los de `id` **menor** —los cinco últimos del bucle—, que son
    # justamente los que tienen menos paquetes de cada ronda.
    assert primera["total_imagenes"] == tope
    assert primera["total_paquetes"] == sum(paquetes_por_fila[-tope:]), (
        "la cuenta no es la de los 200 trabajos con id mayor"
    )


# --------------------------------------------------------------------------- #
# Endpoints de webhook: GET /api/v1/webhooks
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_los_endpoints_de_webhook_no_cambian_de_orden_entre_dos_peticiones(
    integration_session: AsyncSession,
) -> None:
    """`GET /api/v1/webhooks`, ordenado por `created_at` sin desempate.

    Esta lista **no** está paginada —`total` es el número de filas que se devuelven y no hay
    `limit` ni `offset`—, así que no puede solapar páginas ni perder filas. Lo que se
    comprueba aquí es el otro efecto del mismo defecto: que dos peticiones con el mismo filtro
    devuelven las mismas filas **en el mismo orden**. Sin desempate, dos endpoints con la
    misma marca se intercambian de sitio y la pantalla parece haber cambiado sola.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session, prefix="orden-hooks")
    ids = _ids_crecientes()
    integration_session.add_all(
        [
            WebhookEndpoint(
                id=identificador,
                organization_id=tenant.organization_id,
                url=f"https://ejemplo.invalid/orden/{identificador.int}",
                encrypted_secret=f"secreto-{identificador.int:08d}",
                event_types=["pentest.completed"],
                created_at=MARCA,
            )
            for identificador in ids
        ]
    )
    await integration_session.commit()

    _token, raw = await create_api_token(
        integration_session,
        tenant.organization_id,
        ApiTokenCreate(name="prueba", scopes=["webhooks:read"], expires_in_days=30),
    )
    cabeceras = {"Authorization": f"Bearer {raw}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        primera = _ids_de_pagina(await client.get("/api/v1/webhooks", headers=cabeceras))
        await _perturbar(integration_session, WebhookEndpoint, ids[0], consecutive_failures=3)
        segunda = _ids_de_pagina(await client.get("/api/v1/webhooks", headers=cabeceras))

    assert sorted(primera) == sorted(str(i) for i in ids)
    assert segunda == primera, "la lista cambió de orden entre dos peticiones idénticas"
    _orden_esperado(primera, ids)


# --------------------------------------------------------------------------- #
# Tokens de API: `list_api_tokens`
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_los_tokens_de_api_no_cambian_de_orden_entre_dos_peticiones(
    integration_session: AsyncSession,
) -> None:
    """`list_api_tokens`, en `api_access/service.py`, ordenado por `created_at` sin desempate.

    Tampoco está paginada, así que lo que se comprueba es la estabilidad del orden. Y aquí la
    perturbación habitual —reescribir `last_used_at`, que es lo que se escribe en cada
    petición autenticada— **no** llega a mover nada: la consulta se resuelve con
    `ix_api_tokens_org_created`, y un `UPDATE` de una columna que no está en el índice es HOT,
    así que la fila no se mueve dentro de él. Medido: 0 de 20 veces.

    La perturbación que sí lo mueve es la que hace la segunda lectura con **otro plan**, que es
    lo que pasa en producción cada vez que la tabla crece y el planificador decide otra cosa.
    Con desempate los dos planes tienen que dar la misma lista, porque el orden depende de los
    valores de las columnas y no de dónde estén las tuplas. Sin desempate, cada plan devuelve
    las filas empatadas a su manera.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session, prefix="orden-tokens")
    ids = _ids_crecientes()
    integration_session.add_all(
        [
            ApiToken(
                id=identificador,
                organization_id=tenant.organization_id,
                name=f"token-{identificador.int:04d}",
                token_hash=f"{identificador.int:064x}",
                token_prefix="fx_abcd1234",
                scopes=["tokens:read"],
                created_at=MARCA,
            )
            for identificador in ids
        ]
    )
    await integration_session.commit()

    async def listado(otro_plan: bool) -> list[str]:
        if otro_plan:
            await integration_session.execute(text("set local enable_indexscan = off"))
        tokens = await list_api_tokens(integration_session, tenant.organization_id)
        return [str(token.id) for token in tokens]

    primera = await listado(False)
    await _perturbar(integration_session, ApiToken, ids[0], last_used_at=datetime.now(UTC))
    segunda = await listado(True)

    assert sorted(primera) == sorted(str(i) for i in ids)
    assert segunda == primera, (
        "la lista cambió de orden entre dos peticiones idénticas con distinto plan"
    )
    _orden_esperado(primera, ids)


# --------------------------------------------------------------------------- #
# Recuperación de conocimiento: `recuperar_documentos`
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_la_recuperacion_devuelve_el_mismo_contexto_entre_dos_peticiones(
    integration_session: AsyncSession,
) -> None:
    """`recuperar_documentos`, en `knowledge/retrieval.py`.

    Este listado tiene **dos** órdenes y por eso necesita **dos** desempates:

    - La consulta de la base ordena por `updated_at desc` y corta con `limit(top_k * 3)`. Sin
      desempate, el corte deja fuera un documento distinto en cada llamada, y el síntoma es
      que **la misma pregunta recibe un contexto distinto**.
    - El orden que ve el usuario lo pone un `sort` de Python por `(-puntuacion, title)`. Ahí
      `title` **no es único** —nada impide dos documentos con el mismo nombre— y `id` detrás
      es lo que cierra el orden y decide el `[:top_k]` final.

    Los documentos se siembran con el mismo `updated_at`, el mismo título y el mismo cuerpo, de
    modo que puntúan igual: la única cosa que puede ordenarlos es `id`. Y se insertan en
    orden **descendente**, para que el orden de inserción y el `id` ascendente sean distintos.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session, prefix="orden-texto")
    ids = list(reversed(_ids_crecientes()))
    integration_session.add_all(
        [
            WorkspaceKnowledgeDocument(
                id=identificador,
                organization_id=tenant.organization_id,
                title="Regla de descuentos",
                doc_type=KnowledgeDocTypeEnum.BUSINESS_RULE,
                content="descuento del 50% en el segundo producto",
                created_at=MARCA,
                updated_at=MARCA,
            )
            for identificador in ids
        ]
    )
    await integration_session.commit()

    async def recuperar() -> list[str]:
        documentos = await recuperar_documentos(
            integration_session, tenant.organization_id, "descuento", top_k=len(ids)
        )
        return [str(documento.document_id) for documento in documentos]

    primera = await recuperar()
    await _perturbar(integration_session, WorkspaceKnowledgeDocument, ids[0], content="otro cuerpo")
    segunda = await recuperar()

    assert sorted(primera) == sorted(str(i) for i in ids)
    assert segunda == primera, "el contexto cambió entre dos preguntas idénticas"
    assert primera == [str(i) for i in reversed(ids)], (
        "el orden no es (-puntuacion, title, id): sin `id` al final el empate queda en manos "
        "del planificador"
    )


@pytest.mark.asyncio
async def test_el_corte_de_candidatos_de_la_recuperacion_es_estable(
    integration_session: AsyncSession,
) -> None:
    """El `limit(top_k * 3)` de `recuperar_documentos` decide qué documentos puntúan.

    Es un `LIMIT` **sin** `OFFSET`, y aquí importa por una razón distinta de la paginación: de
    los candidatos que salen de la consulta solo se puntúan los primeros, así que si el corte
    no estádecided, el documento que entra en el prompt es otro.

    ## Lo que este test **no** afirma, y por qué

    Que el corte pueda dejar fuera un documento que puntúa. No puede: `condicion_de_terminos`
    y `_puntuar` usan los mismos términos, de modo que toda fila que pasa el `ILIKE` puntúa
    más de cero. Se comprobó con un test que lo daba por hecho y que **era verde con el
    defecto puesto**: un test verde con el defecto es un test vacío, así que no se ha
    kepto.

    Lo que sí se afirma es el contrato del conjunto y del orden: con ocho documentos
    idénticos, `top_k=1` corta a tres, y el que sale es el de `id` 6 —el más bajo de los tres
    que el desempate deja—. Sin desempate el corte son las tres primeras filas en orden de
    inserción y sale el de `id` 1.
    """

    assert integration_session is not None
    tenant = await _tenant(integration_session, prefix="orden-corte")
    ids = _ids_crecientes()
    integration_session.add_all(
        [
            WorkspaceKnowledgeDocument(
                id=identificador,
                organization_id=tenant.organization_id,
                title="Regla de descuentos",
                doc_type=KnowledgeDocTypeEnum.BUSINESS_RULE,
                content="descuento aplicable a la segunda compra",
                created_at=MARCA,
                updated_at=MARCA,
            )
            for identificador in ids
        ]
    )
    await integration_session.commit()

    documentos = await recuperar_documentos(
        integration_session, tenant.organization_id, "descuento", top_k=1
    )
    assert len(documentos) == 1, "la recuperación no ha devuelto un solo documento"
    # El corte son los tres de `id` mayor (8, 7 y 6) y el orden final es `id` ascendente, así
    # que el primero de esos tres es el 6.
    assert str(documentos[0].document_id) == str(ids[5]), (
        "el corte y el orden final no se han resuelto por (updated_at DESC, id DESC)"
    )


# --------------------------------------------------------------------------- #
# La revisión anterior que hereda el `comment_id`
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_la_revision_anterior_es_la_de_id_mayor(
    integration_session: AsyncSession,
) -> None:
    """`_upsert_review`, en `repositories/webhook_events.py`: el `comment_id` heredado.

    No es un listado paginado: es un `limit(1)` para heredar el `comment_id` de la revisión
    anterior del mismo PR. Aquí el fallo no es una página repetida, es que **la revisión que
    sale depende del planificador** y con ella el hilo en el que se publica el comentario.

    Se siembran dos revisiones previas con la misma `created_at` y `comment_id` distintos: la
    que debe heredarse es la de `id` mayor, y como los `id` se insertan en orden creciente el
    orden de inserción es el contrario, así que la comprobación es determinista.
    """

    assert integration_session is not None
    from backend.apps.repositories.chatops import NormalizedPullRequestEvent
    from backend.apps.repositories.webhook_events import _upsert_review

    tenant = await _tenant(integration_session, prefix="orden-anterior")
    repositorio = await _repositorio(integration_session, tenant.organization_id)

    previas = []
    for numero, identificador in enumerate(_ids_crecientes(2), start=1):
        revision = _revision(identificador, tenant.organization_id, repositorio.id, MARCA)
        revision.pr_number = 900
        revision.comment_id = f"comentario-{2000 + numero}"
        integration_session.add(revision)
        previas.append(revision)
    await integration_session.commit()

    evento = NormalizedPullRequestEvent(
        provider=GitProviderEnum.GITHUB,
        pr_number=900,
        title="PR 900",
        author="autor",
        source_branch="feature",
        target_branch="main",
        commit_sha="c" * 40,
        is_comment=False,
        base_sha="d" * 40,
        action="opened",
    )

    revision_nueva, creada = await _upsert_review(integration_session, repositorio, evento)

    assert creada, "la revisión nueva no se ha creado y la prueba no está midiendo nada"
    # `previsas[-1]` es la de `id` mayor, y se insertó la última: sin desempate la base
    # devuelve las filas en orden de inserción y heredaría el `comment_id` de la otra.
    assert revision_nueva.comment_id == previas[-1].comment_id, (
        "la revisión anterior elegida no es la de `id` mayor: el `comment_id` heredado depende "
        "del planificador"
    )


# --------------------------------------------------------------------------- #
# La revisión base del autofix
# --------------------------------------------------------------------------- #


async def _hallazgo(
    session: AsyncSession, tenant: Tenant, repositorio: Repository
) -> Vulnerability:
    """Un hallazgo mínimo, con el escaneo al que pertenece."""

    run = PentestRun(
        organization_id=tenant.organization_id,
        target_type=TargetTypeEnum.REPOSITORY,
        target_identifier=repositorio.clone_url,
        scan_mode=ScanModeEnum.STANDARD,
        status=ScanStatusEnum.COMPLETED,
    )
    session.add(run)
    await session.flush()
    hallazgo = Vulnerability(
        organization_id=tenant.organization_id,
        run_id=run.id,
        title="Hallazgo para la revision base",
        description="Solo existe para decidir contra que rama se publica el arreglo.",
        severity=SeverityEnum.HIGH,
        cvss_score=8.1,
        affected_target="app.py",
        poc_reproduction_raw="sin PoC",
    )
    session.add(hallazgo)
    await session.flush()
    return hallazgo


@pytest.mark.asyncio
async def test_la_revision_base_del_autofix_se_elige_sin_ambiguedad(
    integration_session: AsyncSession,
) -> None:
    """`_review_id_del_hallazgo`, en `vulnerabilities/remediation.py`.

    Tampoco es un listado: es el `limit(1)` que decide **contra qué rama se publica el
    arreglo**. Dos revisiones de un mismo escaneo comparten `created_at`, así que sin desempate
    la rama base puede cambiar entre dos llamadas idénticas.
    """

    assert integration_session is not None
    from backend.apps.vulnerabilities.remediation import _review_id_del_hallazgo

    tenant = await _tenant(integration_session, prefix="orden-autofix")
    repositorio = await _repositorio(integration_session, tenant.organization_id)
    hallazgo = await _hallazgo(integration_session, tenant, repositorio)
    ids = _ids_crecientes(2)

    for numero, identificador in enumerate(ids, start=1):
        revision = _revision(identificador, tenant.organization_id, repositorio.id, MARCA)
        revision.pr_number = 800 + numero
        revision.run_id = hallazgo.run_id
        integration_session.add(revision)
    await integration_session.commit()

    # Sin `expire_all()`: el hallazgo se acaba de insertar y sus columnas están en memoria, y
    # expirarlo convertiría el acceso perezoso a `organization_id` y `run_id` en una consulta
    # perezosa que no se puede lanzar fuera del bucle de eventos.
    elegida = await _review_id_del_hallazgo(integration_session, hallazgo)

    assert elegida == str(ids[1]), (
        "la revisión elegida no es la de `id` mayor: la rama base del arreglo depende del "
        "planificador"
    )
