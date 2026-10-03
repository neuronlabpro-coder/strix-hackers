"""Pruebas del listado global de revisiones de PR y sus KPIs.

El endpoint por repositorio ya existía (`/repositories/{id}/reviews`) pero no había
forma de ver la actividad de pull requests de toda la organización, que es lo que
pide la vista `/pr-reviews` de MENU-MAP §4.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import (
    Membership,
    Organization,
    RoleEnum,
    User,
)
from backend.apps.repositories.models import (
    GitProviderEnum,
    PRReviewStatusEnum,
    PullRequestReview,
    Repository,
)
from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration


async def _tenant(
    session: AsyncSession, *, plan_tier: str = "PRO"
) -> tuple[Organization, dict[str, str]]:
    suffix = uuid.uuid4().hex
    organization = Organization(
        name=f"PR Reviews {suffix}", slug=f"prr-{suffix}", plan_tier=plan_tier
    )
    user = User(
        email=f"prr-{suffix}@example.com",
        hashed_password="not-used",
        full_name="PR Reviewer",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(
        Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.ADMIN)
    )
    await session.commit()
    return organization, {
        "Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}",
        "X-Organization-Id": str(organization.id),
    }


async def _repository(session: AsyncSession, organization_id: uuid.UUID) -> Repository:
    suffix = uuid.uuid4().hex
    repository = Repository(
        organization_id=organization_id,
        provider=GitProviderEnum.GITHUB,
        remote_repo_id=f"9{suffix[:6]}",
        name=f"repo-{suffix[:4]}",
        full_name=f"acme/repo-{suffix[:4]}",
        clone_url=f"https://github.com/acme/repo-{suffix[:4]}.git",
        default_branch="main",
    )
    session.add(repository)
    await session.commit()
    return repository


def _review(
    repository: Repository,
    *,
    pr_number: int,
    status: PRReviewStatusEnum = PRReviewStatusEnum.PASSED,
    critical: int = 0,
    high: int = 0,
    merge_blocked: bool = False,
    days_ago: int = 1,
    title: str | None = None,
    author: str = "dev@example.com",
    branch: str | None = None,
    created_at: datetime | None = None,
) -> PullRequestReview:
    """Revisión de prueba.

    `created_at` se puede fijar a mano porque `TimestampMixin` solo declara
    `server_default`: la columna acepta el valor explícito, que es lo que permite medir el
    filtro de rango sin depender del reloj.
    """

    return PullRequestReview(
        organization_id=repository.organization_id,
        repository_id=repository.id,
        run_id=None,
        pr_number=pr_number,
        pr_title=title if title is not None else f"Pull request #{pr_number}",
        pr_author=author,
        source_branch=branch if branch is not None else f"feature/pr-{pr_number}",
        target_branch="main",
        commit_sha="a" * 40,
        base_sha="b" * 40,
        status=status,
        issues_caught_critical=critical,
        issues_caught_high=high,
        merge_blocked=merge_blocked,
        finished_at=datetime.now(UTC) - timedelta(days=days_ago),
        **({} if created_at is None else {"created_at": created_at}),
    )


# --------------------------------------------------------------------------- #
# Listado global
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pr_reviews_lists_across_every_repository_of_the_tenant(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization, headers = await _tenant(integration_session)
    first = await _repository(integration_session, organization.id)
    second = await _repository(integration_session, organization.id)
    integration_session.add_all(
        [
            _review(first, pr_number=1, days_ago=1),
            _review(second, pr_number=2, days_ago=2),
            _review(second, pr_number=3, days_ago=3),
        ]
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/pr-reviews/", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    # El repositorio viaja en cada fila: la tabla global no puede exigir un segundo
    # clic para saber de qué repositorio es cada PR.
    assert {item["repository_name"] for item in body["items"]} == {
        first.full_name,
        second.full_name,
    }
    # Ordenado por fecha descendente: lo más reciente primero.
    created = [item["created_at"] for item in body["items"]]
    assert created == sorted(created, reverse=True)


@pytest.mark.asyncio
async def test_pr_reviews_isolates_tenants(integration_session: AsyncSession) -> None:
    """R3: la organización A nunca ve revisiones de la B."""

    assert integration_session is not None
    alpha, alpha_headers = await _tenant(integration_session)
    beta, _beta_headers = await _tenant(integration_session)
    alpha_repository = await _repository(integration_session, alpha.id)
    beta_repository = await _repository(integration_session, beta.id)
    integration_session.add_all(
        [
            _review(alpha_repository, pr_number=10),
            _review(beta_repository, pr_number=20),
        ]
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/pr-reviews/", headers=alpha_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    # Se comprueba **todas** las filas y no solo la primera.
    #
    # Con la consulta que había, sin `JOIN`, el resultado era un producto cartesiano: cada
    # revisión se cruzaba con cada repositorio de la base, el contenido se filtraba por
    # organización pero el `repository_name` salía del primer repositorio que se encontrara.
    # Mirar solo `items[0]` hacía la prueba intermitente, porque dependía de qué fila
    # devolviera el planificador; el nombre de otro tenant podía aparecer en la segunda fila
    # sin que el test lo notara. Afirmar que **ninguna** fila nombra un repositorio ajeno es lo
    # que hace que el fallo sea un fallo y no una coincidencia.
    assert body["items"], "el listado del tenant emptiness no puede salir vacio"
    for item in body["items"]:
        assert item["repository_name"] == alpha_repository.full_name
    nombres = {item["repository_name"] for item in body["items"]}
    assert nombres == {alpha_repository.full_name}, (
        f"el listado nombra repositorios que no son del tenant: {nombres}"
    )


@pytest.mark.asyncio
async def test_pr_reviews_filters_by_status_and_repository(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization, headers = await _tenant(integration_session)
    first = await _repository(integration_session, organization.id)
    second = await _repository(integration_session, organization.id)
    integration_session.add_all(
        [
            _review(first, pr_number=1, status=PRReviewStatusEnum.PASSED),
            _review(first, pr_number=2, status=PRReviewStatusEnum.FAILED, merge_blocked=True),
            _review(second, pr_number=3, status=PRReviewStatusEnum.SCANNING),
        ]
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        by_status = await client.get(
            "/api/v1/pr-reviews/?status=FAILED", headers=headers
        )
        by_repo = await client.get(
            f"/api/v1/pr-reviews/?repository_id={second.id}", headers=headers
        )

    assert by_status.json()["total"] == 1
    assert by_status.json()["items"][0]["pr_number"] == 2
    assert by_repo.json()["total"] == 1
    assert by_repo.json()["items"][0]["pr_number"] == 3


@pytest.mark.asyncio
async def test_pr_reviews_rejects_a_repository_from_another_tenant(
    integration_session: AsyncSession,
) -> None:
    """Un filtro por repositorio ajeno devuelve la lista vacía, no un `403` filtrando existencia."""

    assert integration_session is not None
    _organization, headers = await _tenant(integration_session)
    other, _other_headers = await _tenant(integration_session)
    foreign_repository = await _repository(integration_session, other.id)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/pr-reviews/?repository_id={foreign_repository.id}", headers=headers
        )

    assert response.status_code == 200
    assert response.json()["total"] == 0


@pytest.mark.asyncio
async def test_pr_reviews_paginates(integration_session: AsyncSession) -> None:
    assert integration_session is not None
    organization, headers = await _tenant(integration_session)
    repository = await _repository(integration_session, organization.id)
    integration_session.add_all(
        [_review(repository, pr_number=number, days_ago=number) for number in range(1, 6)]
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/api/v1/pr-reviews/?limit=2&offset=0", headers=headers)
        second = await client.get("/api/v1/pr-reviews/?limit=2&offset=2", headers=headers)

    assert first.json()["total"] == 5
    assert len(first.json()["items"]) == 2
    assert second.json()["offset"] == 2
    first_ids = {item["id"] for item in first.json()["items"]}
    second_ids = {item["id"] for item in second.json()["items"]}
    assert first_ids.isdisjoint(second_ids)


# --------------------------------------------------------------------------- #
# Búsqueda por texto y rango de fechas
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pr_reviews_searches_title_author_branch_and_repository(
    integration_session: AsyncSession,
) -> None:
    """El buscador tiene que encontrar por las cuatro cosas con las que se recuerda una revisión.

    Si solo buscara en el título, escribir el nombre de quien abrió el pull request —que es
    como se busca media vez— devolvería página vacía y el buscador parecería roto.
    """

    assert integration_session is not None
    organization, headers = await _tenant(integration_session)
    repository = await _repository(integration_session, organization.id)
    integration_session.add_all(
        [
            _review(repository, pr_number=1, title="Anadir inicio de sesion unico"),
            _review(repository, pr_number=2, author="ana@example.com"),
            _review(repository, pr_number=3, branch="feature/pagos"),
        ]
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        por_titulo = await client.get("/api/v1/pr-reviews/?query=sesion", headers=headers)
        por_autor = await client.get("/api/v1/pr-reviews/?query=ana@", headers=headers)
        por_rama = await client.get("/api/v1/pr-reviews/?query=pagos", headers=headers)
        por_repositorio = await client.get("/api/v1/pr-reviews/?query=acme", headers=headers)
        sin_coincidencia = await client.get(
            "/api/v1/pr-reviews/?query=nada-de-esto", headers=headers
        )

    assert por_titulo.json()["total"] == 1
    assert por_titulo.json()["items"][0]["pr_number"] == 1
    assert por_autor.json()["total"] == 1
    assert por_autor.json()["items"][0]["pr_number"] == 2
    assert por_rama.json()["total"] == 1
    assert por_rama.json()["items"][0]["pr_number"] == 3
    # `full_name` de los repositorios del tenant empieza por `acme/`, así que las tres entran.
    assert por_repositorio.json()["total"] == 3
    assert sin_coincidencia.json()["total"] == 0


@pytest.mark.asyncio
async def test_pr_reviews_search_by_number_matches_only_that_pull_request(
    integration_session: AsyncSession,
) -> None:
    """El número se compara exacto: `42` es la revisión 42, no la 142 ni la 420.

    Con `LIKE '%42%'` las tres entrarían, y quien busca el pull request 42 leería como
    hallazgo suyo una revisión de otro pull request. Ni los títulos ni las ramas de esta
    prueba llevan dígitos a propósito, para que lo único que pueda hacer coincidir sea el
    número.
    """

    assert integration_session is not None
    organization, headers = await _tenant(integration_session)
    repository = await _repository(integration_session, organization.id)
    integration_session.add_all(
        [
            _review(repository, pr_number=42, title="Primer titulo", branch="feature/primera"),
            _review(repository, pr_number=142, title="Segundo titulo", branch="feature/segunda"),
            _review(repository, pr_number=420, title="Tercer titulo", branch="feature/tercera"),
        ]
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/pr-reviews/?query=42", headers=headers)

    assert response.json()["total"] == 1
    assert response.json()["items"][0]["pr_number"] == 42


@pytest.mark.asyncio
async def test_pr_reviews_search_treats_like_wildcards_as_literals(
    integration_session: AsyncSession,
) -> None:
    """`_` y `%` se buscan literales, no como comodines de `LIKE`.

    Los nombres de rama llevan `_` —`feature/web_app`— así que sin escapar, buscar `web_app`
    devolvería también `webXapp`: un resultado que el usuario no pidió y que no puede
    distinguir de un fallo del buscador.
    """

    assert integration_session is not None
    organization, headers = await _tenant(integration_session)
    repository = await _repository(integration_session, organization.id)
    integration_session.add_all(
        [
            _review(repository, pr_number=1, branch="feature/web_app"),
            _review(repository, pr_number=2, branch="feature/webXapp"),
            _review(repository, pr_number=3, branch="release/100%off"),
            _review(repository, pr_number=4, branch="release/1000off"),
        ]
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        con_guion_bajo = await client.get("/api/v1/pr-reviews/?query=web_app", headers=headers)
        con_porcentaje = await client.get("/api/v1/pr-reviews/?query=100%25off", headers=headers)

    assert con_guion_bajo.json()["total"] == 1
    assert con_guion_bajo.json()["items"][0]["pr_number"] == 1
    assert con_porcentaje.json()["total"] == 1
    assert con_porcentaje.json()["items"][0]["pr_number"] == 3


@pytest.mark.asyncio
async def test_pr_reviews_filters_by_creation_range_with_an_inclusive_last_day(
    integration_session: AsyncSession,
) -> None:
    """El rango recorta por fecha de alta y el día final entra **entero**.

    Se comprueban los cuatro bordes: una revisión de la medianoche previa se queda fuera, la
    de la medianoche inicial entra, la del último segundo del día final entra y la de la
    medianoche siguiente se queda fuera. Un `<=` sobre la medianoche del `created_to` dejaría
    pasar la última y solo la última.
    """

    assert integration_session is not None
    organization, headers = await _tenant(integration_session)
    repository = await _repository(integration_session, organization.id)
    integration_session.add_all(
        [
            _review(
                repository,
                pr_number=1,
                created_at=datetime(2026, 3, 9, 23, 59, tzinfo=UTC),
            ),
            _review(
                repository,
                pr_number=2,
                created_at=datetime(2026, 3, 10, 0, 0, tzinfo=UTC),
            ),
            _review(
                repository,
                pr_number=3,
                created_at=datetime(2026, 3, 12, 23, 59, 59, tzinfo=UTC),
            ),
            _review(
                repository,
                pr_number=4,
                created_at=datetime(2026, 3, 13, 0, 0, tzinfo=UTC),
            ),
        ]
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        rango = await client.get(
            "/api/v1/pr-reviews/?created_from=2026-03-10&created_to=2026-03-12", headers=headers
        )
        solo_desde = await client.get(
            "/api/v1/pr-reviews/?created_from=2026-03-12", headers=headers
        )
        invertido = await client.get(
            "/api/v1/pr-reviews/?created_from=2026-03-12&created_to=2026-03-10", headers=headers
        )

    assert rango.json()["total"] == 2
    assert {item["pr_number"] for item in rango.json()["items"]} == {2, 3}
    assert solo_desde.json()["total"] == 2
    # Un rango invertido no es un `422`: las dos condiciones son incompatibles por
    # construcción, así que la lista vacía ya es la respuesta que corresponde.
    assert invertido.status_code == 200
    assert invertido.json()["total"] == 0


@pytest.mark.asyncio
async def test_pr_reviews_filters_stay_inside_the_tenant(
    integration_session: AsyncSession,
) -> None:
    """R3 también con los filtros nuevos: la búsqueda no amplía la vista.

    Un buscador es la forma más fácil de Meter en la consulta una condición que el llamador
    elige, así que la prueba tiene que mirar lo que **no** devuelve tanto como lo que sí.
    """

    assert integration_session is not None
    alpha, alpha_headers = await _tenant(integration_session)
    beta, _beta_headers = await _tenant(integration_session)
    alpha_repository = await _repository(integration_session, alpha.id)
    beta_repository = await _repository(integration_session, beta.id)
    integration_session.add_all(
        [
            _review(alpha_repository, pr_number=1, branch="feature/alfa"),
            _review(beta_repository, pr_number=2, branch="feature/beta"),
        ]
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ajeno = await client.get("/api/v1/pr-reviews/?query=beta", headers=alpha_headers)
        propio = await client.get("/api/v1/pr-reviews/?query=alfa", headers=alpha_headers)

    assert ajeno.json()["total"] == 0
    assert ajeno.json()["items"] == []
    assert propio.json()["total"] == 1


@pytest.mark.asyncio
async def test_pr_reviews_search_counts_only_matches_before_paginating(
    integration_session: AsyncSession,
) -> None:
    """`total` cuenta lo que coincide con los filtros, no lo que hay en la organización.

    Es lo que hace que la barra de paginación sea honesta: si `total` saliera sin filtrar, el
    resumen «1-25 de 300» prometería páginas que al pulsarlas saldrían vacías.
    """

    assert integration_session is not None
    organization, headers = await _tenant(integration_session)
    repository = await _repository(integration_session, organization.id)
    integration_session.add_all(
        [_review(repository, pr_number=number, branch="feature/matches") for number in range(1, 4)]
        + [_review(repository, pr_number=9, branch="feature/other")]
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        primera = await client.get(
            "/api/v1/pr-reviews/?query=matches&limit=2&offset=0", headers=headers
        )
        segunda = await client.get(
            "/api/v1/pr-reviews/?query=matches&limit=2&offset=2", headers=headers
        )

    assert primera.json()["total"] == 3
    assert len(primera.json()["items"]) == 2
    assert primera.json()["limit"] == 2
    assert segunda.json()["offset"] == 2
    assert len(segunda.json()["items"]) == 1
    assert all(
        item["source_branch"] == "feature/matches"
        for item in primera.json()["items"] + segunda.json()["items"]
    )


# --------------------------------------------------------------------------- #
# KPIs
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pr_review_metrics_count_audited_clean_and_blocking(
    integration_session: AsyncSession,
) -> None:
    """Los tres KPIs de la cabecera deben sumar la misma categoría sin solaparse.

    Se cuenta por revisión, no por hallazgo: una revisión con doce hallazgos sigue
    siendo una revisión. Y una revisión con `merge_blocked` cuenta como bloqueante
    aunque su estado sea `PASSED`, porque lo que bloquea el merge es la bandera, no el
    estado del escaneo.
    """

    assert integration_session is not None
    organization, headers = await _tenant(integration_session)
    repository = await _repository(integration_session, organization.id)
    integration_session.add_all(
        [
            _review(repository, pr_number=1, status=PRReviewStatusEnum.PASSED),
            _review(repository, pr_number=2, status=PRReviewStatusEnum.PASSED),
            _review(
                repository,
                pr_number=3,
                status=PRReviewStatusEnum.FAILED,
                critical=2,
                high=5,
                merge_blocked=True,
            ),
            _review(
                repository,
                pr_number=4,
                status=PRReviewStatusEnum.PASSED,
                high=1,
                merge_blocked=True,
            ),
            _review(repository, pr_number=5, status=PRReviewStatusEnum.ERROR),
            _review(repository, pr_number=6, status=PRReviewStatusEnum.QUEUED),
        ]
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/pr-reviews/metrics", headers=headers)

    assert response.status_code == 200
    metrics = response.json()
    # Auditadas: todas las revisiones que el sistema llegó a(create y registró.
    assert metrics["total"] == 6
    # Limpias: pasaron sin bloquear el merge.
    assert metrics["clean"] == 2
    # Bloqueantes: con el merge bloqueado, sea cual sea el estado del escaneo.
    assert metrics["blocking"] == 2
    # El desglose de severidad cuenta hallazgos, no revisiones.
    assert metrics["issues_critical"] == 2
    assert metrics["issues_high"] == 6


@pytest.mark.asyncio
async def test_pr_review_metrics_are_tenant_scoped(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    alpha, alpha_headers = await _tenant(integration_session)
    beta, _beta_headers = await _tenant(integration_session)
    alpha_repository = await _repository(integration_session, alpha.id)
    beta_repository = await _repository(integration_session, beta.id)
    integration_session.add_all(
        [
            _review(alpha_repository, pr_number=1),
            _review(beta_repository, pr_number=2),
            _review(beta_repository, pr_number=3),
        ]
    )
    await integration_session.commit()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/pr-reviews/metrics", headers=alpha_headers)

    assert response.json()["total"] == 1


# --------------------------------------------------------------------------- #
# Autenticación
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pr_reviews_endpoints_require_authentication() -> None:
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        listing = await client.get("/api/v1/pr-reviews/")
        metrics = await client.get("/api/v1/pr-reviews/metrics")

    assert listing.status_code == 401
    assert metrics.status_code == 401
