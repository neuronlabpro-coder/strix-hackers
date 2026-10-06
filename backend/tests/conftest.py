"""Fixtures compartidas para aislar conexiones y datos de pruebas."""

from collections.abc import AsyncIterator, Iterator
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.pentests.router import get_dispatch_pentest_run
from backend.apps.repositories.router import get_dispatch_pr_review
from backend.core.config import settings
from backend.core.database import engine, get_db
from backend.core.rate_limit import (
    enforce_autofix_rate_limit,
    enforce_create_organization_rate_limit,
    enforce_email_resend_rate_limit,
    enforce_email_verification_rate_limit,
    enforce_git_webhook_rate_limit,
    enforce_invitation_accept_rate_limit,
    enforce_invitation_rate_limit,
    enforce_login_rate_limit,
    enforce_oauth_callback_rate_limit,
    enforce_pentest_rate_limit,
    enforce_register_rate_limit,
    enforce_repository_management_rate_limit,
    get_rate_limit_redis,
)
from backend.main import app


def pytest_configure(config: pytest.Config) -> None:
    """Impide que una suite de pruebas pueda ejecutarse contra producción."""

    if settings.environment not in {"development", "test"}:
        raise pytest.UsageError(
            "La suite de pruebas solo puede ejecutarse en ENVIRONMENT=development o test"
        )
    if settings.email_verification_delivery_mode != "development":
        raise pytest.UsageError(
            "Las pruebas no pueden ejecutar el delivery real de email; usa el modo development"
        )


@pytest.fixture(autouse=True)
async def dispose_database_engine() -> AsyncIterator[None]:
    """Cierra conexiones del pool antes de que termine el event loop de cada test."""

    yield
    await engine.dispose()


@pytest.fixture(autouse=True)
def override_auth_rate_limits() -> Iterator[None]:
    """Desactiva el rate limiter real solo dentro de la suite controlada."""

    app.dependency_overrides[enforce_login_rate_limit] = lambda: None
    app.dependency_overrides[enforce_oauth_callback_rate_limit] = lambda: None
    app.dependency_overrides[enforce_register_rate_limit] = lambda: None
    app.dependency_overrides[enforce_repository_management_rate_limit] = lambda: None
    app.dependency_overrides[enforce_create_organization_rate_limit] = lambda: None
    app.dependency_overrides[enforce_autofix_rate_limit] = lambda: None
    app.dependency_overrides[enforce_email_verification_rate_limit] = lambda: None
    app.dependency_overrides[enforce_email_resend_rate_limit] = lambda: None
    app.dependency_overrides[enforce_git_webhook_rate_limit] = lambda: None
    app.dependency_overrides[get_rate_limit_redis] = lambda: AsyncMock()
    app.dependency_overrides[enforce_invitation_accept_rate_limit] = lambda: None
    app.dependency_overrides[enforce_invitation_rate_limit] = lambda: None
    app.dependency_overrides[enforce_pentest_rate_limit] = lambda: None
    app.dependency_overrides[get_dispatch_pentest_run] = lambda: (lambda _run_id: "test-task-id")
    # La de revisiones de PR, por el mismo motivo: sin esto, un test que lanzara un análisis
    # llegaría a Redis de verdad. Los tests que necesitan ver **qué** se encoló sustituyen esta
    # por la suya con un contexto, y lo restauran a la de aquí.
    app.dependency_overrides[get_dispatch_pr_review] = lambda: (lambda _review_id: "test-task-id")
    yield
    app.dependency_overrides.pop(enforce_login_rate_limit, None)
    app.dependency_overrides.pop(enforce_oauth_callback_rate_limit, None)
    app.dependency_overrides.pop(enforce_register_rate_limit, None)
    app.dependency_overrides.pop(enforce_repository_management_rate_limit, None)
    app.dependency_overrides.pop(enforce_create_organization_rate_limit, None)
    app.dependency_overrides.pop(enforce_autofix_rate_limit, None)
    app.dependency_overrides.pop(enforce_email_verification_rate_limit, None)
    app.dependency_overrides.pop(enforce_email_resend_rate_limit, None)
    app.dependency_overrides.pop(enforce_git_webhook_rate_limit, None)
    app.dependency_overrides.pop(get_rate_limit_redis, None)
    app.dependency_overrides.pop(enforce_invitation_accept_rate_limit, None)
    app.dependency_overrides.pop(enforce_invitation_rate_limit, None)
    app.dependency_overrides.pop(enforce_pentest_rate_limit, None)
    app.dependency_overrides.pop(get_dispatch_pentest_run, None)
    app.dependency_overrides.pop(get_dispatch_pr_review, None)


@pytest.fixture(autouse=True)
async def bind_health_probe_redis() -> AsyncIterator[None]:
    """Da al sondeo de salud un cliente Redis atado al bucle de eventos de cada test.

    ## Por qué hace falta

    `backend.core.redis.redis_client` es un cliente creado **a la hora de importar el
    módulo**, y su pool de conexiones vive en el bucle de eventos que estuviera abierto
    cuando se usó por primera vez. En producción hay un solo bucle y no hay problema, pero
    aquí cada test corre en el suyo: el pool conserva una conexión de un bucle ya cerrado
    y el siguiente `PING` revienta con `Event loop is closed` o con
    `'NoneType' object has no attribute 'send'` de redis-py.

    El fallo es **dependiente del orden**: pasa si el test que usa Redis es el primero que
    lo usa, y falla si hay otro antes. Por eso la suite entera parecía verde y una prueba
    concreta fallaba en un archivo y no en otro.

    ## Por qué un cliente nuevo en vez de un mock

    Un `AsyncMock` haría que el sondeo devolviera `online` siempre, y la prueba de salud
    dejaría de comprobar nada: pasaría con Redis caído. Se crea un cliente real —la misma
    fábrica que usa producción— y se cierra al terminar, de modo que la prueba sigue
    midiendo algo y el único coste es una conexión.
    """

    from backend.apps.admin import service as admin_service
    from backend.core.redis import create_redis_client

    cliente = create_redis_client(settings)
    anterior = admin_service.redis_client
    admin_service.redis_client = cliente
    try:
        yield
    finally:
        admin_service.redis_client = anterior
        await cliente.aclose()


@pytest.fixture(autouse=True)
def controles_del_runner_desactivados(monkeypatch: pytest.MonkeyPatch) -> None:
    """Desactiva los dos controles de seguridad del runner para toda la bateria.

    ## Los dos controles que se desactivan

    1. **El cerco de salida.** `exigir_cerco_de_salida` falla cerrado: sin una regla de
       `iptables` que cubra la red del trabajo, se niega a lanzarlo. En la bateria no hay demonio
       de Docker, no hay `DOCKER-USER`, y en Windows ni hay `iptables`.
    2. **El reconocimiento de exposicion de la clave.** Igual de cerrado: sin la frase exacta en
       `STRIX_LLM_KEY_EXPOSURE_ACK`, no se entrega la clave del proveedor y no hay escaneo.

    Los dos fallan cerrados **a proposito**: en un despliegue en el que nadie ha decidido nada,
    lo que debe pasar es que no escaneen. Que la bateria tenga que desactivarlos a mano es el
    precio de esa garantia, y se paga en un unico sitio y a la vista. Ninguno se apaga por
    defecto en el codigo, porque un interruptor de seguridad apagado por omision no es un
    interruptor de seguridad.

    ## Por que se sustituye la funcion y no se cambia el ajuste

    Porque `Settings` es **congelado** —la garantia de la que ya se sirve `llm_router.client`—
    y mutarlo exigiria `object.__setattr__`, que es justo el truco que la inmutabilidad existe
    para evitar.

    Cada control tiene su propia prueba, que **si** lo exercise de verdad:
    `test_runner_egress_fence.py` y `test_runner_llm_key_exposure.py`.
    """

    monkeypatch.setattr("backend.workers.runner.sandbox.exigir_cerco_de_salida", _sin_cerco)
    monkeypatch.setattr(
        "backend.workers.runner.sandbox.exigir_reconocimiento_de_exposicion",
        _sin_reconocimiento,
    )


def _sin_cerco(_subred: str | None) -> None:
    """Sustituto de `exigir_cerco_de_salida` para la bateria de pruebas."""

    return None


def _sin_reconocimiento() -> str:
    """Sustituto de `exigir_reconocimiento_de_exposicion`: devuelve la clave sin comprobar.

    Devuelve la clave de verdad y no un marcador, porque hay pruebas que comparan el entorno
    que construye el contenedor, y un marcador haria que se estuviera probando un caso que
    nunca ocurre.
    """

    return settings.llm_api_key.get_secret_value()


@pytest.fixture
async def integration_session(request: pytest.FixtureRequest) -> AsyncIterator[AsyncSession | None]:
    """Proporciona una sesión de integración aislada por transacción y savepoints."""

    if request.node.get_closest_marker("integration") is None:
        yield None
        return

    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        app.dependency_overrides[get_db] = lambda: session
        try:
            yield session
        finally:
            app.dependency_overrides.pop(get_db, None)
            await session.close()
            await transaction.rollback()
            await engine.dispose()


@pytest.fixture(autouse=True)
async def isolate_integration_database(
    integration_session: AsyncSession | None,
) -> AsyncIterator[None]:
    """Garantiza que toda prueba marcada como integración use la sesión rollbackeable."""

    del integration_session
    yield
