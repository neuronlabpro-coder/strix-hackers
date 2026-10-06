"""Se puede lanzar el análisis de un pull request desde el panel.

## Qué estaba ausente

Hasta el 4 de octubre de 2026 la única forma de que una revisión de PR se analizara era que un
webhook del proveedor la trajera. `/api/v1/pr-reviews/` devolvía la tabla, `/metrics` los KPIs y
no había ninguna operación de escritura: la función que el producto vende —«analiza el código de
un pull request»— no se podía pedir, solo esperar.

## Qué se comprueba aquí y por qué en estos casos

Cuatro cosas, y cada una es un modo de fallo distinto:

1. **La transición y el encolado**, que es el camino feliz.
2. **R3**: una revisión de otro tenant da `404` y **no** se encola. Es el fallo que más cuesta
   ver porque el endpoint nuevo no tenía forma de fallar por la vía equivocada hasta que se
   escribió. Un `403` además confirmaría que el `review_id` existe.
3. **Un doble clic**: la segunda llamada tiene que rechazarse, no encolar un segundo contenedor.
   Dos escaneos del mismo commit es la misma seguridad cobrada dos veces, y es el modo de fallo
   que un endpoint de relanzamiento introduce por definición.
4. **La cola caída**: si `dispatch` falla, la revisión **no** puede quedar en `QUEUED`, porque
   en `QUEUED` no la recoge nadie y el panel muestra «en cola» de algo que no está en ninguna
   cola. Es un fallo silencioso en el sentido literal de `silent-failure-hunter`, y por eso tiene
   prueba propia.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.organizations.models import Membership, Organization, RoleEnum, User
from backend.apps.repositories.models import (
    GitProviderEnum,
    PRReviewStatusEnum,
    PullRequestReview,
    Repository,
)
from backend.apps.repositories.router import get_dispatch_pr_review
from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration

ENDPOINT = "/api/v1/pr-reviews/{review_id}/analyze"


@contextmanager
def _encolar_que_anota(
    encoladas: list[str],
    *,
    falla: bool = False,
) -> Iterator[None]:
    """Sustituye la dependencia de encolado por una que anota, o por una que revienta.

    ## Por qué un `override` y no `patch.object(router, ...)`

    Porque FastAPI resuelve la dependencia **una vez**, al construir el dependant, y guarda la
    función que encontró. Un `patch` sobre el atributo del módulo llega después de esa resolución
    y no cambia lo que el router va a llamar: el test pasa, se drummer en verde y en realidad
    sigue yendo a Redis. Eso es un test que no mide lo que dice medir, que es el peor tipo.

    El `override` es la vía que el propio framework garantiza, y por eso la usa ya `conftest.py`
    para el resto de dependencias.

    ## Por qué restaura la anterior en el `finally`

    Porque el `conftest` pone un override por defecto que devuelve `"test-task-id"`, y si esta
    prueba lo deja pisado, la siguiente hereda el de esta. Un test que ensucia el estado global
    de la suite convierte un fallo local en un fallo en otro fichero, que es la forma más
    cara de perder tiempo.
    """

    def _dispatch(review_id: str) -> str:
        if falla:
            raise RuntimeError("redis no responde")
        encoladas.append(review_id)
        return f"task-{review_id[:8]}"

    # El `conftest` pone el override con la forma `lambda: <funcion>`, no como la función
    # directamente: FastAPI resuelve `get_dispatch_pr_review` —que no toma parámetros del
    # request— y **llama** a lo que devuelve el override. Sustituirlo por `_dispatch` a secas
    # haría que FastAPI la invocara sin argumentos y le devolviera una cadena, que es
    # exactamente el `TypeError: 'str' object is not callable` de la primera versión de esta
    # prueba. Se conserva la envoltura.
    anterior = app.dependency_overrides.get(get_dispatch_pr_review)
    app.dependency_overrides[get_dispatch_pr_review] = lambda: _dispatch
    try:
        yield
    finally:
        if anterior is not None:
            app.dependency_overrides[get_dispatch_pr_review] = anterior
        else:
            app.dependency_overrides.pop(get_dispatch_pr_review, None)


async def _crear_tenant(session: AsyncSession) -> tuple[Organization, User, dict[str, str]]:
    sufijo = uuid.uuid4().hex
    organization = Organization(
        name=f"Reviews {sufijo}",
        slug=f"reviews-{sufijo}",
        credit_balance=Decimal("1000"),
    )
    user = User(
        email=f"reviews-{sufijo}@example.com",
        hashed_password="not-used",
        full_name="Reviews User",
        email_verified=True,
    )
    session.add_all([organization, user])
    await session.flush()
    session.add(Membership(organization_id=organization.id, user_id=user.id, role=RoleEnum.ADMIN))
    await session.flush()
    token = create_access_token({"sub": str(user.id)})
    return organization, user, {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": str(organization.id),
    }


async def _crear_review(
    session: AsyncSession,
    organization: Organization,
    *,
    status: PRReviewStatusEnum = PRReviewStatusEnum.ERROR,
    pr_reviews_enabled: bool = True,
    is_active: bool = True,
) -> PullRequestReview:
    repository = Repository(
        organization_id=organization.id,
        provider=GitProviderEnum.GITHUB,
        remote_repo_id=f"repo-{uuid.uuid4().hex[:8]}",
        name="demo",
        full_name=f"demo/{uuid.uuid4().hex[:8]}",
        clone_url="https://github.com/demo/demo.git",
        default_branch="main",
        pr_reviews_enabled=pr_reviews_enabled,
        is_active=is_active,
    )
    session.add(repository)
    await session.flush()
    review = PullRequestReview(
        organization_id=organization.id,
        repository_id=repository.id,
        pr_number=4242,
        pr_title="Endpoint de lanzamiento",
        pr_author="alguien",
        source_branch="feature/lanzar",
        target_branch="main",
        commit_sha="a" * 40,
        base_sha="b" * 40,
        status=status,
    )
    session.add(review)
    await session.flush()
    return review


@pytest.mark.asyncio
async def test_lanzar_analisis_pone_la_revision_en_cola_y_la_encola(
    integration_session: AsyncSession,
) -> None:
    assert integration_session is not None
    organization, _, cabeceras = await _crear_tenant(integration_session)
    review = await _crear_review(integration_session, organization)

    encoladas: list[str] = []
    with _encolar_que_anota(encoladas):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            respuesta = await client.post(
                ENDPOINT.format(review_id=review.id),
                headers=cabeceras,
            )

    assert respuesta.status_code == 202, respuesta.text
    assert encoladas == [str(review.id)]
    cuerpo = respuesta.json()
    assert cuerpo["status"] == "QUEUED"
    assert cuerpo["id"] == str(review.id)
    # El `repository_name` viene del repositorio real, no de un valor por defecto: es lo que la
    # tabla muestra y un nombre vacío ahí es un dato falso, no un dato ausente.
    assert cuerpo["repository_name"].startswith("demo/")

    releida = (
        await integration_session.execute(
            select(PullRequestReview).where(PullRequestReview.id == review.id)
        )
    ).scalar_one()
    assert releida.status == PRReviewStatusEnum.QUEUED


@pytest.mark.asyncio
async def test_una_revision_de_otro_tenant_no_se_puede_lanzar(
    integration_session: AsyncSession,
) -> None:
    """R3: el `review_id` de otro cliente da `404` y no encola nada.

    Sin este filtro, el pipeline cobraría al cliente dueño del PR por un análisis que le pidió
    otro, usando su credencial y su repositorio. Y el `404` —no `403`— es parte del mismo
    requisito: un `403` confirma que ese identificador existe en algún sitio.
    """

    assert integration_session is not None
    organizacion_victima, _, _ = await _crear_tenant(integration_session)
    review_victima = await _crear_review(integration_session, organizacion_victima)
    _, _, cabeceras_atacante = await _crear_tenant(integration_session)

    encoladas: list[str] = []
    with _encolar_que_anota(encoladas):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            respuesta = await client.post(
                ENDPOINT.format(review_id=review_victima.id),
                headers=cabeceras_atacante,
            )

    assert respuesta.status_code == 404
    assert encoladas == [], "una revision ajena no puede encolar nada"

    intacta = (
        await integration_session.execute(
            select(PullRequestReview).where(PullRequestReview.id == review_victima.id)
        )
    ).scalar_one()
    assert intacta.status == PRReviewStatusEnum.ERROR


@pytest.mark.asyncio
async def test_relanzar_una_revision_en_curso_se_rechaza(
    integration_session: AsyncSession,
) -> None:
    """El segundo clic no encola un segundo contenedor.

    Es el modo de fallo que un endpoint de relanzamiento introduce por definición: si acepta
    `SCANNING`, dos clics conscientiousos —«no veo que avance, voy a darle otra vez»— producen
    dos escaneos del mismo commit, y eso es la misma seguridad cobrada dos veces.
    """

    assert integration_session is not None
    organization, _, cabeceras = await _crear_tenant(integration_session)
    review = await _crear_review(
        integration_session, organization, status=PRReviewStatusEnum.SCANNING
    )

    encoladas: list[str] = []
    with _encolar_que_anota(encoladas):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            respuesta = await client.post(
                ENDPOINT.format(review_id=review.id),
                headers=cabeceras,
            )

    assert respuesta.status_code == 404
    assert encoladas == []


@pytest.mark.asyncio
async def test_si_la_cola_falla_la_revision_no_queda_en_cola(
    integration_session: AsyncSession,
) -> None:
    """Un fallo de encolado deja la revisión en `ERROR`, nunca en `QUEUED`.

    ## Por qué esto no es un detalle

    Porque `QUEUED` sin nada que la recoja es el peor estado posible: el panel muestra «en cola»
    de un trabajo que no está en ninguna cola, el watchdog no la toca —solo marca *runs*—, y el
    usuario no tiene ninguna señal de que está perdido. Es un fallo silencioso en el sentido
    literal, y por eso tiene su propia prueba y no se esconde dentro de la del camino feliz.
    """

    assert integration_session is not None
    organization, _, cabeceras = await _crear_tenant(integration_session)
    review = await _crear_review(integration_session, organization)

    with _encolar_que_anota([], falla=True):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            respuesta = await client.post(
                ENDPOINT.format(review_id=review.id),
                headers=cabeceras,
            )

    assert respuesta.status_code == 503
    assert respuesta.headers.get("Retry-After") == "30", (
        "un 503 de infraestructura tiene que ser reintentable, y eso lo dice la cabecera"
    )

    releida = (
        await integration_session.execute(
            select(PullRequestReview).where(PullRequestReview.id == review.id)
        )
    ).scalar_one()
    assert releida.status == PRReviewStatusEnum.ERROR, (
        "en QUEUED no la recoge nadie y el panel miente"
    )


@pytest.mark.asyncio
async def test_un_repositorio_desconectado_no_se_puede_relanzar(
    integration_session: AsyncSession,
) -> None:
    """Con el repositorio desconectado, el fallo se dice ahora y no tres segundos después.

    Sin esta comprobación la revisión se encolaría, el worker fallaría al abrir el cliente Git y
    el panel mostraría un `ERROR` sin motivo de un fallo que era del despliegue. Aquí es un `404`
    inmediato, que además es el mismo código que un `review_id` inexistente y por la misma razón:
    no se le dice al llamador más de lo que puede saber.
    """

    assert integration_session is not None
    organization, _, cabeceras = await _crear_tenant(integration_session)
    review = await _crear_review(integration_session, organization, is_active=False)

    encoladas: list[str] = []
    with _encolar_que_anota(encoladas):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            respuesta = await client.post(
                ENDPOINT.format(review_id=review.id),
                headers=cabeceras,
            )

    assert respuesta.status_code == 404
    assert encoladas == []


@pytest.mark.asyncio
async def test_un_miembro_no_puede_lanzar_analisis(integration_session: AsyncSession) -> None:
    """Lanzar un escaneo es una acción de administración del riesgo, no de lectura.

    El pipeline abre un cliente Git con la credencial del workspace y consume tokens de la
    plataforma. Que lo pueda hacer cualquier miembro convertiría la pantalla en un botón de
    gasto.
    """

    assert integration_session is not None
    organization, user, cabeceras = await _crear_tenant(integration_session)
    review = await _crear_review(integration_session, organization)
    await integration_session.execute(
        update(Membership)
        .where(Membership.user_id == user.id)
        .values(role=RoleEnum.MEMBER)
    )
    await integration_session.commit()

    encoladas: list[str] = []
    with _encolar_que_anota(encoladas):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            respuesta = await client.post(
                ENDPOINT.format(review_id=review.id),
                headers=cabeceras,
            )

    assert respuesta.status_code == 403
    assert encoladas == []


@pytest.mark.asyncio
async def test_el_encolado_usa_la_tarea_de_pr_reviews(integration_session: AsyncSession) -> None:
    """La dependencia real encola `repositories.run_pr_security_pipeline`.

    No parece una prueba —la llamada va al broker de verdad si no se sustituye—, pero comprueba
    algo que ninguna otra comprueba: que la dependencia del router **apunta a la tarea
    correcta**. Es fácil registrar un endpoint nuevo encolando `execute_pentest_run`, que es lo
    que se hace con un pentest y no con un PR: el nombre del target lo distingue, pero el escaneo
    no clonaría el código del pull request y el panel mostraría un run de pentest colgado.
    """

    assert integration_session is not None
    organization, _, cabeceras = await _crear_tenant(integration_session)
    review = await _crear_review(integration_session, organization)
    # Aquí sí se quita el override: lo que se prueba es la dependencia **de verdad**, que
    # encola `run_pr_security_pipeline.delay`. El `conftest` pone un sustituto por defecto y
    # sin quitarlo la prueba no miraría nada.
    app.dependency_overrides.pop(get_dispatch_pr_review, None)

    with patch(
        "backend.apps.repositories.tasks.run_pr_security_pipeline.delay",
        return_value=SimpleNamespace(id="task-real-1"),
    ) as delay:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            respuesta = await client.post(
                ENDPOINT.format(review_id=review.id),
                headers=cabeceras,
            )

    assert respuesta.status_code == 202, respuesta.text
    delay.assert_called_once_with(str(review.id))
