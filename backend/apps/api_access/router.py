"""Router de gestión de tokens de la API pública.

Cada endpoint pide su propio scope con `require_scope`, y ese es el punto: el alcance de
lo que un token puede hacer no se decide en el cuerpo de la función, sino en la línea
que declara la dependencia. Una función de negocio que se pueda llamar desde un contexto
sin scope es una vía por la que se salta la autorización.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.apps.api_access.auth import (
    Session,
    TenantPrincipal,
    require_scope,
)
from backend.apps.api_access.schemas import (
    ApiScopeCatalogResponse,
    ApiTokenCreate,
    ApiTokenCreatedResponse,
    ApiTokenPage,
    ApiTokenResponse,
    scope_catalog_response,
)
from backend.apps.api_access.scopes import Scope
from backend.apps.api_access.service import (
    ApiTokenNotFoundError,
    create_api_token,
    list_api_tokens,
    revoke_api_token,
)
from backend.core.rate_limit import enforce_repository_management_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter()

@router.post(
    "/api/v1/auth/tokens",
    response_model=ApiTokenCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(enforce_repository_management_rate_limit)],
)
async def create_token(
    payload: ApiTokenCreate,
    principal: Annotated[TenantPrincipal, Depends(require_scope(Scope.TOKENS_CREATE))],
    session: Session,
) -> ApiTokenCreatedResponse:
    """Emite un token de API y devuelve el secreto **una única vez**.

    El secreto no se guarda en ningún sitio en claro, así que esta respuesta es la única
    oportunidad de leerlo. Si el cliente la pierde, la única salida es revocar el token.

    ## Por qué el 404 no aparece en el alta

    Un token nuevo siempre es del tenant que lo pide, así que no hay nada que no exista.
    El 404 entra en juego al revocar, y allí sí es deliberado.
    """

    organizacion = principal.organization
    token, raw_token = await create_api_token(session, organizacion.id, payload)
    # El secreto se registra en el log solo como presencia, nunca como valor. La línea
    # siguiente es la que un revisor tiene que poder leer sin tener la credencial delante.
    logger.info("Secreto emitido para el token %s (valor no registrado)", token.id)
    respuesta = ApiTokenResponse.from_model(token)
    return ApiTokenCreatedResponse(**respuesta.model_dump(), raw_token=raw_token)


@router.get(
    "/api/v1/auth/scopes",
    response_model=ApiScopeCatalogResponse,
)
async def list_scopes(
    principal: Annotated[TenantPrincipal, Depends(require_scope(Scope.TOKENS_READ))],
) -> ApiScopeCatalogResponse:
    """Expone el catálogo de permisos para que el panel no lo repita.

    Pide `tokens:read` y no un permiso propio por una razón práctica: quien puede ver
    los tokens de su organización es exactamente quien puede crearlos, así que exigir
    `tokens:create` solo impediría que un token de solo lectura pudiera abrir el
    asistente de alta para ver qué opciones tiene.

    No filtra por organización: el catálogo es el mismo para todos y no contiene nada
    del tenant. El sujeto se resuelve igual para dejar constancia de que la ruta exige
    autenticación, porque un catálogo público no es lo que se pidió.
    """

    del principal
    return scope_catalog_response()


@router.get(
    "/api/v1/auth/tokens",
    response_model=ApiTokenPage,
)
async def list_tokens(
    principal: Annotated[TenantPrincipal, Depends(require_scope(Scope.TOKENS_READ))],
    session: Session,
    include_revoked: Annotated[
        bool,
        Query(
            description=(
                "Incluye los revocados. Por defecto se omiten porque un token revocado no "
                "sirve para nada y solo añade ruido a la lista."
            )
        ),
    ] = False,
) -> ApiTokenPage:
    """Lista los tokens del tenant activo.

    El filtro por organización está en la consulta del servicio, no aquí: ponerlo en el
    router dejaría el aislamiento R3 en una capa que se puede olvidar, y una lista de
    credenciales de otro tenant es exactamente el tipo de fuga que ninguna prueba de
    este endpoint detectaría.
    """

    organizacion = principal.organization
    tokens = await list_api_tokens(
        session, organizacion.id, include_revoked=include_revoked
    )
    return ApiTokenPage(
        items=[ApiTokenResponse.from_model(token) for token in tokens],
        total=len(tokens),
        include_revoked=include_revoked,
    )


@router.delete(
    "/api/v1/auth/tokens/{token_id}",
    response_model=ApiTokenResponse,
)
async def revoke_token(
    token_id: UUID,
    principal: Annotated[TenantPrincipal, Depends(require_scope(Scope.TOKENS_REVOKE))],
    session: Session,
) -> ApiTokenResponse:
    """Revoca un token del tenant activo.

    ## Por qué un token de otro tenant responde 404 y no 403

    Un `403` confirmaría que ese identificador existe. Con `404`, un token de otro tenant
    es indistinguible de uno que nunca se creó, y probar identificadores no revela nada.
    Es el mismo criterio que usa el resto de la API para no filtrar existencia, y aquí
    importa más porque lo que se prueba son credenciales.
    """

    organizacion = principal.organization
    try:
        token = await revoke_api_token(session, organizacion.id, token_id)
    except ApiTokenNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Token no encontrado",
        ) from error
    return ApiTokenResponse.from_model(token)
