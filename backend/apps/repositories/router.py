"""Endpoints multi-tenant de inventario, conexión y gestión de repositorios."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.repositories.clients.base import BaseGitClient, GitClientError
from backend.apps.repositories.clients.factory import UnsupportedGitProviderError
from backend.apps.repositories.inventory import (
    NormalizedRepository,
    RemoteRepositoryError,
    normalize_remote_repository,
    webhook_callback_url,
    webhook_subscription_events,
)
from backend.apps.repositories.models import (
    GitProviderEnum,
    PRReviewStatusEnum,
    PullRequestReview,
    Repository,
    generate_webhook_secret,
)
from backend.apps.repositories.schemas import (
    PRReviewMetrics,
    PRReviewPage,
    PRReviewResponse,
    RemoteRepositoryPage,
    RemoteRepositoryResponse,
    RepositoryConnectRequest,
    RepositoryConnectResponse,
    RepositoryPage,
    RepositoryResponse,
    RepositoryUpdateRequest,
)
from backend.apps.repositories.services import (
    GitCredentialNotFoundError,
    build_organization_client,
)
from backend.core.crypto import CryptoError
from backend.core.database import get_db
from backend.core.middleware import (
    AdminRequired,
    TenantContext,
    exigir_admin_del_tenant,
    get_current_tenant,
)
from backend.core.rate_limit import enforce_repository_management_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter()
SessionDependency = Annotated[AsyncSession, Depends(get_db)]
TenantDependency = Annotated[TenantContext, Depends(get_current_tenant)]
ManagementRateLimit = Depends(enforce_repository_management_rate_limit)

_IN_PROGRESS_REVIEW_STATUSES = (PRReviewStatusEnum.QUEUED, PRReviewStatusEnum.SCANNING)
_SUPPORTED_MANAGEMENT_PROVIDERS = frozenset({GitProviderEnum.GITHUB, GitProviderEnum.GITLAB})


async def _require_admin(tenant: TenantDependency) -> None:
    """Exige `ADMIN` del workspace, o superusuario.

    ## Por qué `async` y por qué `TenantDependency`, y no `TenantContext` a secas

    Porque esta función se usa de las dos formas: en el `dependencies=[...]` de un decorador
    **y** llamada en el cuerpo de otras rutas. Con la anotación suelta, la forma del decorador
    le dice a FastAPI que `TenantContext` es un modelo de respuesta, no un parámetro ya
    resuelto, y la aplicación no arranca:

        `FastAPIError: Invalid args for response field! Hint: check that <class
        'backend.core.middleware.TenantContext'> is a valid Pydantic field type.`

    El alias `Annotated[TenantContext, Depends(get_current_tenant)]` lleva la dependencia
    declarada y las dos formas funcionan.

    ## Por qué se delega en la regla de `core` en vez de repetirla

    Porque antes esta comparaba `tenant.role != RoleEnum.ADMIN` **sin** la excepción del
    superusuario, y las otras cuatro copias del módulo sí la tenían. Cuatro reglas de
    autorización que se distinguen en un `and not` son cuatro reglas, y un superusuario que
    podía administrar un recurso por un router y no por otro es exactamente el tipo de
    diferencia que se descubre cuando un cliente necesita arreglado algo.

    Y por qué el mensaje sigue siendo el de este módulo: es el que lee quien llama, y un
    `403` que dice «repositorios» localiza el problema más rápido que uno que dice «esta
    operación».
    """

    try:
        await exigir_admin_del_tenant(tenant)
    except HTTPException as error:
        raise HTTPException(
            status_code=error.status_code,
            detail="Se requiere permiso de administrador",
        ) from error


def _translate_client_error(error: GitClientError) -> HTTPException:
    if error.status_code in {401, 403}:
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="La credencial del proveedor fue rechazada o carece de permisos",
        )
    if error.status_code == 404:
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="El recurso no existe en el proveedor Git",
        )
    if error.status_code == 429:
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="El proveedor Git agotó su cuota temporal",
            headers={"Retry-After": str(error.retry_after or 30)},
        )
    if error.status_code is not None and error.status_code >= 500:
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="El proveedor Git no está disponible",
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="No se pudo completar la operación con el proveedor Git",
    )


@asynccontextmanager
async def _open_client(
    session: AsyncSession,
    organization_id: UUID,
    provider: GitProviderEnum,
) -> AsyncIterator[BaseGitClient]:
    if provider not in _SUPPORTED_MANAGEMENT_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="El proveedor todavía no tiene conector de gestión",
        )
    client = await _try_open_client(session, organization_id, provider)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Conecta primero una credencial de {provider.value} para esta organización",
        )
    try:
        yield client
    finally:
        client.close()


async def _try_open_client(
    session: AsyncSession,
    organization_id: UUID,
    provider: GitProviderEnum,
) -> BaseGitClient | None:
    """Construye el cliente del tenant o devuelve `None` si no hay credencial utilizable."""

    try:
        return await build_organization_client(session, organization_id, provider)
    except (GitCredentialNotFoundError, UnsupportedGitProviderError, CryptoError):
        return None


def _normalized_inventory(
    provider: GitProviderEnum,
    raw_repositories: list[dict[str, object]],
) -> list[NormalizedRepository]:
    """Descarta entradas que no podemos materializar de forma segura."""

    normalized: list[NormalizedRepository] = []
    for payload in raw_repositories:
        try:
            normalized.append(normalize_remote_repository(provider, payload))
        except RemoteRepositoryError as error:
            logger.warning(
                "Repositorio remoto descartado del inventario: provider=%s reason=%s",
                provider.value,
                str(error),
            )
    return normalized


def _filtrar_inventario(
    inventory: list[NormalizedRepository], search: str | None
) -> list[NormalizedRepository]:
    """Filtra el inventario por un texto, o lo devuelve entero si no hay texto.

    ## Por qué compara `full_name`, `name` y `default_branch`

    Porque los tres son cosas que el usuario escribe. `acme/api-gateway` se busca por `gateway`,
    y una rama `feature/shy-redesign` se busca por `shy`. Con solo el `full_name`, buscar `shy`
    no encuentra un repositorio cuya rama lo tenga, y el usuario concluye que el repositorio no
    existe.

    ## Por qué ignora mayúsculas pero no acentos

    Porque el nombre de un repositorio en GitHub no distingue mayúsculas de minúsculas, así que
    `API-Gateway` y `api-gateway` son la misma búsqueda y tratarlas distinto sería una sorpresa.
    Con los acentos pasa lo contrario: `diseño` y `diseno` **son** cadenas distintas en un
    nombre de GitHub, y un desplegar un `unicode`-folding introduciría colisiones que no existen
    en el proveedor. Se compara lo que el proveedor considera la misma cadena.
    """
    ## Por qué no hay un atajo para la búsqueda vacía
    #
    # ## Por qué no hay vuelta atrás para la búsqueda vacía
    #
    # Porque no hace falta, y se comprobó. La aguja vacía es subcadena de cualquier cadena, así
    # que `"" in texto` es `True` para todo repositorio y la lista comprehensión devuelve el
    # inventario entero igual que lo haría un `if consulta == "": return inventory`.
    #
    # Se dejó puesto un atajo, y al reintroducir los defectos uno a uno salió el único que ningún
    # test detectaba: quitarlo no cambiaba nada. Eso lo convierte en código muerto, y el código
    # muerto en una función de tres líneas es peor que la línea que ahorra: parece que importa.
    #
    # Y el `strip()` de arriba es lo que de verdad resuelve el caso de los espacios: una búsqueda
    # de «   » llega aquí como aguja vacía, y sin el `strip` sería una búsqueda literal de tres
    # espacios que no encuentra nada. `test_una_busqueda_vacia_o_de_solo_espacios_no_filtra` es
    # el que vigila esa parte.
    consulta = (search or "").strip().lower()
    return [
        repository
        for repository in inventory
        if consulta
        in "\n".join(
            (repository.full_name, repository.name, repository.default_branch)
        ).lower()
    ]


@router.get(
    "/api/v1/repositories/remote",
    response_model=RemoteRepositoryPage,
    dependencies=[ManagementRateLimit, AdminRequired],
)
async def list_remote_repositories(
    tenant: TenantDependency,
    session: SessionDependency,
    provider: Annotated[GitProviderEnum, Query()],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    search: Annotated[str | None, Query(max_length=200)] = None,
) -> RemoteRepositoryPage:
    """Lista el inventario accesible con la credencial conectada del tenant.

    ## Por qué hay un `search` aquí y no solo en el cliente

    Porque el inventario **no** se descarga entero: `limit` está topado a 100, así que un
    workspace con 500 repositorios solo deja ver 50 si el buscador filtra en el navegador. El
    síntoma era buscar `shy` y ver un resultado de los doce que hay, sin ninguna pista de que
    faltaran los otros once.

    ## Por qué aquí no cuesta nada y en el cliente sí

    Porque el cliente del proveedor se llama una vez y devuelve **el inventario entero** —eso
    ya lo hacía esta función para el `total`—. El corte por `limit`/`offset` es posterior, así
    que filtrar por `search` es recorrer una lista que ya está en memoria. No hay una segunda
    ida al proveedor y no hay una consulta a la base: el filtro va donde ya estaban los datos.
    """

    connected_result = await session.execute(
        select(Repository.remote_repo_id).where(
            Repository.organization_id == tenant.organization.id,
            Repository.provider == provider,
        )
    )
    connected_ids = {row for row in connected_result.scalars()}
    async with _open_client(session, tenant.organization.id, provider) as client:
        try:
            raw_repositories = await asyncio.to_thread(client.list_repositories)
        except GitClientError as error:
            raise _translate_client_error(error) from error
        normalized = _normalized_inventory(provider, raw_repositories)
    filtrados = _filtrar_inventario(normalized, search)
    window = filtrados[offset : offset + limit]
    return RemoteRepositoryPage(
        items=[
            RemoteRepositoryResponse.from_normalized(
                repository,
                already_connected=repository.remote_repo_id in connected_ids,
            )
            for repository in window
        ],
        # El total es el de la lista **filtrada**, no el del inventario entero. Con lo
        # contrario, buscando `shy` la interfaz respondería «12 de 500» cuando lo que hay son 12
        # de 12, y el número que sirve para saber si falta algo —`total`— mentiría justo cuando
        # el usuario está intentando saber si le falta algo.
        total=len(filtrados),
        limit=limit,
        offset=offset,
        provider=provider,
    )


@router.post(
    "/api/v1/repositories/connect",
    response_model=RepositoryConnectResponse,
    dependencies=[ManagementRateLimit],
)
async def connect_repository(
    response: Response,
    tenant: TenantDependency,
    session: SessionDependency,
    payload: RepositoryConnectRequest,
) -> RepositoryConnectResponse:
    """Da de alta el repositorio verificando sus metadatos contra el proveedor."""

    await _require_admin(tenant)
    # La búsqueda va **con** `organization_id` y no contra una tabla entera.
    #
    # ## Por qué esto ya no distingue «existe en otra organización»
    #
    # Antes se buscaba por `(provider, remote_repo_id)` a secas y, si la fila era de otro tenant,
    # se devolvía un `409` con el texto de que ya estaba vinculada a otra organización. Eso era un
    # oráculo: `remote_repo_id` es un identificador público de GitHub, y recorrerlos con un
    # `ADMIN` de la propia organización permitía enumerar **qué repositorios tienen conectados
    # otros clientes de la plataforma** y aprender su `provider`.
    #
    # Y contradecía frontalmente el criterio del proyecto, que usa `404` en vez de `403` para no
    # confirmar la existencia de un recurso ajeno —`_load_tenant_repository`, línea 605, y
    # `list_pr_reviews`, línea 440, lo hacen en este mismo fichero.
    #
    # ## Por qué el `409` tampoco era necesario
    #
    # Porque la unicidad de `repositories` pasó a ser **por organización**
    # (`uq_repositories_org_provider_remote`). Dos clientes conectando el mismo repositorio no es
    # un problema —cada uno tiene su credencial, su espacio de revisión y su cargo— y con la
    # restricción global la organización A reclamaba un repositorio popular y la B no podía
    # conectarlo nunca. Sin esa restricción, no hay conflicto que señalar.
    #
    # ## Por qué se sigue mirando la fila del propio tenant
    #
    # Para que conectar dos veces el mismo repositorio en la misma organización sea un
    # **actualizar**, que es lo que hace el resto de la función, y no un error de unicidad que el
    # cliente no puede distinguir de un fallo suyo.
    existing_result = await session.execute(
        select(Repository).where(
            Repository.organization_id == tenant.organization.id,
            Repository.provider == payload.provider,
            Repository.remote_repo_id == payload.remote_repo_id,
        )
    )
    existing = existing_result.scalar_one_or_none()

    async with _open_client(session, tenant.organization.id, payload.provider) as client:
        try:
            raw_repository = await asyncio.to_thread(
                client.get_repository,
                payload.remote_repo_id,
            )
        except GitClientError as error:
            raise _translate_client_error(error) from error
        try:
            normalized = normalize_remote_repository(payload.provider, raw_repository)
        except RemoteRepositoryError as error:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="El proveedor devolvió metadatos de repositorio no utilizables",
            ) from error

        repository = existing
        created = repository is None
        if repository is None:
            repository = Repository(
                organization_id=tenant.organization.id,
                provider=payload.provider,
                remote_repo_id=normalized.remote_repo_id,
                name=normalized.name,
                full_name=normalized.full_name,
                clone_url=normalized.clone_url,
                default_branch=normalized.default_branch,
                pr_reviews_enabled=payload.pr_reviews_enabled,
                webhook_secret=generate_webhook_secret(),
            )
            session.add(repository)
        else:
            repository.name = normalized.name
            repository.full_name = normalized.full_name
            repository.clone_url = normalized.clone_url
            repository.default_branch = normalized.default_branch
            repository.pr_reviews_enabled = payload.pr_reviews_enabled
            repository.is_active = True
        try:
            await session.flush()
        except IntegrityError as error:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="El repositorio ya está conectado",
            ) from error

        webhook_registered = False
        if repository.webhook_id is None:
            try:
                repository.webhook_id = await asyncio.to_thread(
                    client.create_webhook,
                    normalized.full_name,
                    webhook_callback_url(payload.provider),
                    repository.webhook_secret,
                    webhook_subscription_events(),
                )
                webhook_registered = True
            except (GitClientError, RemoteRepositoryError) as error:
                logger.warning(
                    "No se pudo registrar el webhook: provider=%s repository=%s reason=%s",
                    payload.provider.value,
                    normalized.full_name,
                    str(error),
                )
        else:
            webhook_registered = True
        await session.commit()
        await session.refresh(repository)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return RepositoryConnectResponse(
        repository=RepositoryResponse.from_repository(repository),
        webhook_registered=webhook_registered,
        created=created,
    )


@router.get("/api/v1/repositories/", response_model=RepositoryPage)
async def list_repositories(
    tenant: TenantDependency,
    session: SessionDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    provider: Annotated[GitProviderEnum | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
) -> RepositoryPage:
    """Lista paginada de repositorios conectados de la organización activa."""

    filters = [Repository.organization_id == tenant.organization.id]
    if provider is not None:
        filters.append(Repository.provider == provider)
    if is_active is not None:
        filters.append(Repository.is_active.is_(is_active))
    total_result = await session.execute(
        select(func.count()).select_from(Repository).where(*filters)
    )
    total = int(total_result.scalar_one())
    result = await session.execute(
        select(Repository)
        .where(*filters)
        .order_by(Repository.created_at.desc(), Repository.id)
        .limit(limit)
        .offset(offset)
    )
    repositories = result.scalars().all()
    return RepositoryPage(
        items=[RepositoryResponse.from_repository(repository) for repository in repositories],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/api/v1/repositories/{repository_id}/reviews",
    response_model=PRReviewPage,
)
async def list_repository_reviews(
    tenant: TenantDependency,
    session: SessionDependency,
    repository_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    review_status: Annotated[PRReviewStatusEnum | None, Query(alias="status")] = None,
    source_branch: Annotated[str | None, Query(max_length=255)] = None,
) -> PRReviewPage:
    """Historial paginado de revisiones de seguridad de un repositorio del tenant.

    El repositorio se carga primero acotado a `organization_id`, de modo que un
    identificador ajeno devuelve `404` sin revelar siquiera si existe.
    """

    repository = await _load_tenant_repository(session, tenant.organization.id, repository_id)
    filters = [
        PullRequestReview.repository_id == repository.id,
        PullRequestReview.organization_id == tenant.organization.id,
    ]
    if review_status is not None:
        filters.append(PullRequestReview.status == review_status)
    if source_branch:
        pattern = f"%{source_branch.strip().lower()}%"
        filters.append(func.lower(PullRequestReview.source_branch).like(pattern))

    total_result = await session.execute(
        select(func.count()).select_from(PullRequestReview).where(*filters)
    )
    total = int(total_result.scalar_one())
    result = await session.execute(
        select(PullRequestReview)
        .where(*filters)
        .order_by(PullRequestReview.created_at.desc(), PullRequestReview.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return PRReviewPage(
        items=[
            PRReviewResponse.from_review(review, repository_name=repository.full_name)
            for review in result.scalars().all()
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/api/v1/pr-reviews/", response_model=PRReviewPage)
async def list_pr_reviews(
    tenant: TenantDependency,
    session: SessionDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    review_status: Annotated[PRReviewStatusEnum | None, Query(alias="status")] = None,
    repository_id: UUID | None = None,
) -> PRReviewPage:
    """Historial paginado de revisiones de pull request de toda la organización.

    ## Por qué el `JOIN` es **incondicional**

    Porque `Repository.full_name` se selecciona en todas las filas, y sin el `JOIN` la consulta
    es un **producto cartesiano**: PostgreSQL devuelve cada revisión cruzada con **cada**
    repositorio de la base, y el nombre que sale en la respuesta es el del primero que se
    encuentra, no el de la revisión.

    SQLAlchemy lo avisa (`SAWarning: SELECT statement has a cartesian product between FROM
    element(s) "pull_request_reviews" and "repositories"`), y el aviso es correcto: esto era
    una **fuga entre tenants**. El contenido de la revisión se filtra bien por
    `organization_id`, pero el nombre del repositorio que la acompaña salía de cualquier otro
    tenant de la base. En una herramienta de pentesting, "tu revisión está en
    `competidor/privado`" es exactamente el tipo de dato que no debe aparecer.

    Solo pasaba desapercibido por suerte: el producto cartesiano devuelve las filas en el orden
    que el planificador elija, y cuando el repositorio del propio tenant salía primero el
    resultado era el correcto. Por eso la prueba de aislamiento era **intermitente** —fallaba
    en cuanto la base compartida tenia otro repositorio por delante— y no porque el filtro
    estuviera mal, sino porque el nombre venía de otro sitio.

    ## Por qué el filtro por `organization_id` sigue siendo obligatorio

    R3 no es negociable y esta es una vista global, que es donde más fácil sería colarse una
    fuga si se leyera solo por repositorio.

    El filtro sobre `PullRequestReview.organization_id` basta porque la base **ya** garantiza
    la coincidencia: `fk_pr_reviews_repository_organization` es una clave foránea compuesta
    sobre `(repository_id, organization_id)`, así que PostgreSQL no permite que una revisión
    apunte a un repositorio de otra organización. El filtro sobre `Repository.organization_id`
    que había aquí era redundante, y se quita: mantenía la desconfianza en un sitio donde la
    base ya la aplica, y hacía creer que las dos columnas podían separarse.
    """

    filters = [PullRequestReview.organization_id == tenant.organization.id]
    if review_status is not None:
        filters.append(PullRequestReview.status == review_status)
    if repository_id is not None:
        # Un repositorio de otro tenant no devuelve `403`: no se le dice al llamador si
        # existe o no. La lista simplemente sale vacía, igual que si el filtro fuese suyo
        # y no tuviera revisiones.
        filters.append(PullRequestReview.repository_id == repository_id)

    join_condition = Repository.id == PullRequestReview.repository_id
    count_query = (
        select(func.count())
        .select_from(PullRequestReview)
        .join(Repository, join_condition)
        .where(*filters)
    )
    rows_query = select(PullRequestReview, Repository.full_name).join(Repository, join_condition)

    total = int((await session.execute(count_query)).scalar_one())
    result = await session.execute(
        rows_query.where(*filters)
        .order_by(PullRequestReview.created_at.desc(), PullRequestReview.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return PRReviewPage(
        items=[
            PRReviewResponse.from_review(review, repository_name=repository_name)
            for review, repository_name in result.all()
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/api/v1/pr-reviews/metrics", response_model=PRReviewMetrics)
async def read_pr_review_metrics(
    tenant: TenantDependency,
    session: SessionDependency,
) -> PRReviewMetrics:
    """Indicadores de cabecera de la vista global.

    Se resuelven en una sola consulta agregada en lugar de tres `COUNT` separados: la
    cabecera se pinta en cada carga de la vista y tres viajes a la base por tres
    números que salen de la misma fila es trabajo desperdiciado.
    """

    result = await session.execute(
        select(
            func.count(PullRequestReview.id),
            func.count(PullRequestReview.id).filter(
                PullRequestReview.status == PRReviewStatusEnum.PASSED,
                PullRequestReview.merge_blocked.is_(False),
            ),
            func.count(PullRequestReview.id).filter(PullRequestReview.merge_blocked.is_(True)),
            func.coalesce(func.sum(PullRequestReview.issues_caught_critical), 0),
            func.coalesce(func.sum(PullRequestReview.issues_caught_high), 0),
        ).where(PullRequestReview.organization_id == tenant.organization.id)
    )
    total, clean, blocking, critical, high = result.one()
    return PRReviewMetrics(
        total=int(total),
        clean=int(clean),
        blocking=int(blocking),
        issues_critical=int(critical),
        issues_high=int(high),
    )


@router.get("/api/v1/repositories/{repository_id}", response_model=RepositoryResponse)
async def get_repository(
    tenant: TenantDependency,
    session: SessionDependency,
    repository_id: UUID,
) -> RepositoryResponse:
    """Devuelve un repositorio únicamente si pertenece a la organización activa."""

    repository = await _load_tenant_repository(session, tenant.organization.id, repository_id)
    return RepositoryResponse.from_repository(repository)


@router.patch(
    "/api/v1/repositories/{repository_id}",
    response_model=RepositoryResponse,
    dependencies=[ManagementRateLimit],
)
async def update_repository(
    tenant: TenantDependency,
    session: SessionDependency,
    repository_id: UUID,
    payload: RepositoryUpdateRequest,
) -> RepositoryResponse:
    """Actualiza la política de revisiones y la rama por defecto del repositorio."""

    await _require_admin(tenant)
    repository = await _load_tenant_repository(session, tenant.organization.id, repository_id)
    if payload.pr_reviews_enabled is not None:
        repository.pr_reviews_enabled = payload.pr_reviews_enabled
    if payload.default_branch is not None:
        repository.default_branch = payload.default_branch
    if payload.is_active is not None:
        repository.is_active = payload.is_active
    await session.commit()
    await session.refresh(repository)
    return RepositoryResponse.from_repository(repository)


@router.delete(
    "/api/v1/repositories/{repository_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[ManagementRateLimit],
)
async def delete_repository(
    tenant: TenantDependency,
    session: SessionDependency,
    repository_id: UUID,
) -> Response:
    """Desvincula el repositorio y elimina el webhook en el proveedor si es posible."""

    await _require_admin(tenant)
    repository = await _load_tenant_repository(session, tenant.organization.id, repository_id)
    in_progress_result = await session.execute(
        select(func.count())
        .select_from(PullRequestReview)
        .where(
            PullRequestReview.repository_id == repository.id,
            PullRequestReview.organization_id == tenant.organization.id,
            PullRequestReview.status.in_(_IN_PROGRESS_REVIEW_STATUSES),
        )
    )
    if int(in_progress_result.scalar_one()) > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No se puede desvincular un repositorio con revisiones en curso",
        )
    if repository.webhook_id is not None:
        client = await _try_open_client(
            session,
            tenant.organization.id,
            repository.provider,
        )
        if client is None:
            logger.warning(
                "Sin credencial utilizable para eliminar el webhook: provider=%s repository=%s",
                repository.provider.value,
                repository.full_name,
            )
        else:
            try:
                await asyncio.to_thread(
                    client.delete_webhook,
                    repository.full_name,
                    repository.webhook_id,
                )
            except GitClientError as error:
                logger.warning(
                    "No se pudo eliminar el webhook remoto: provider=%s repository=%s reason=%s",
                    repository.provider.value,
                    repository.full_name,
                    str(error),
                )
            finally:
                client.close()
    await session.delete(repository)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _load_tenant_repository(
    session: AsyncSession,
    organization_id: UUID,
    repository_id: UUID,
) -> Repository:
    """Carga un repositorio acotado al tenant; 404 si pertenece a otra organización."""

    result = await session.execute(
        select(Repository).where(
            Repository.id == repository_id,
            Repository.organization_id == organization_id,
        )
    )
    repository = result.scalar_one_or_none()
    if repository is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repositorio no encontrado",
        )
    return repository
