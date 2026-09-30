"""Aplicación FastAPI de control de Mind Guard Fenix Team."""

from typing import Literal

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from backend.apps.admin.router import router as admin_router
from backend.apps.agents.router import router as agents_router
from backend.apps.api_access.mcp_router import router as mcp_router
from backend.apps.api_access.router import router as api_access_router
from backend.apps.assets.router import router as assets_router
from backend.apps.audit.router import router as audit_router
from backend.apps.billing.router import router as billing_router
from backend.apps.chat.router import router as chat_router
from backend.apps.cve_database.router import router as cve_router
from backend.apps.dashboard.router import router as dashboard_router
from backend.apps.knowledge.router import router as knowledge_router
from backend.apps.knowledge.router_documents import router as knowledge_documents_router
from backend.apps.onboarding.router import router as onboarding_router
from backend.apps.organizations.router import router as organizations_router
from backend.apps.pentests.router import router as pentests_router
from backend.apps.repositories.router import router as repositories_router
from backend.apps.repositories.router_auth import router as repositories_auth_router
from backend.apps.repositories.router_webhooks import router as repositories_webhooks_router
from backend.apps.supply_chain.router import router as supply_chain_router
from backend.apps.support.router import admin_router as support_admin_router
from backend.apps.support.router import router as support_router
from backend.apps.vulnerabilities.router import router as vulnerabilities_router
from backend.apps.webhooks.router import router as webhooks_router
from backend.core.config import settings

#: Si la documentación interactiva y el esquema se sirven en este despliegue.
#:
#: ## Por qué no en staging ni en producción
#:
#: Porque `/openapi.json` es el **mapa de la superficie de ataque**: cada ruta, cada nombre de
#: parámetro y cada esquema de error, enumerados y ordenados, gratis y sin autenticación. Y
#: `/docs` lo hace legible. No es una fuga de datos, es una entrega de inventario.
#:
#: Y no basta con protegerlos con autenticación, porque el esquema también se sirve desde
#: OpenAPI en el panel de Swagger: proteger uno y no el otro deja el mismo mapa accesible por la
#: otra puerta. Se desactivan los tres, o ninguno.
#:
#: ## Por qué tampoco se pueden dejar «abiertos pero inofensivos»
#:
#: Porque en este proyecto la autenticación se declara con `Depends`, no con `Security`. FastAPI
#: deduce el `securitySchemes` de la **firma** de la función, y una dependencia de `Depends` no
#: genera ninguna: el esquema sale sin un solo `securityScheme`, y Swagger ni siquiera puede
#: autenticar una petición. Es decir: la documentación no solo filtra la superficie, es que además
#: está incompleta. Pasarlo a `Security` en veinte rutas es lo correcto y es un arreglo aparte;
#: desactivar la documentación fuera de desarrollo es lo que cabe aquí.
#:
#: Y se decide por **entorno**, no por bandera, para que no exista el caso de alguien que
#: desactive la comprobación en producción creyendo que la tiene activa.
_EXPONE_DOCUMENTACION: bool = settings.environment == "development"

app = FastAPI(
    title="Mind Guard Fenix Team API",
    docs_url="/docs" if _EXPONE_DOCUMENTACION else None,
    redoc_url="/redoc" if _EXPONE_DOCUMENTACION else None,
    openapi_url="/openapi.json" if _EXPONE_DOCUMENTACION else None,
)

# --------------------------------------------------------------------------- #
# CORS
# --------------------------------------------------------------------------- #
#
# ## Por qué hace falta y por qué no se notaba que faltaba
#
# En desarrollo no hace falta: el proxy de Vite sirve `/api` desde el mismo origen que la
# SPA, así que el navegador nunca ve un cruce y se puede tener la aplicación entera
# funcionando sin declarar ni un origen.
#
# En producción sí, y sin esto el panel se queda vacío: la SPA se sirve en `panel.` y la API
# responde en `api.`, que son orígenes distintos por definición, y el navegador bloquea
# cada petición **antes** de que salga. El síntoma es un panel que carga sin datos y un error
# de red que no menciona CORS, que es la forma más cara de perder un día de diagnóstico.
#
# ## Por qué `allow_credentials=False`
#
# La API **no** usa cookies: el JWT va en `Authorization` y en `sessionStorage`. Activar
# credenciales obligaría a declarar un origen exacto en vez de un comodín, y se aplicaría a
# un flujo que no existe. Mantenerlo desactivado significa que la cabecera `*` sigue siendo
# válida y que añadir un origen después no obliga a revisar las otras dos opciones.
#
# `allow_methods` y `allow_headers` se declaran en vez de usar los comodines porque
# `Authorization` y `X-Organization-Id` son cabeceras **no simples**: sin declararlas, el
# navegador hace una petición previa y las preflights empiezan a fallar en desarrollo, con
# un error que sí menciona CORS pero no la cabecera concreta.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Accept",
        "X-Organization-Id",
        "Stripe-Signature",
    ],
    # El máximo del navegador para un valor de preflight. Declararlo deja claro que el
    # valor es deliberado y no un 600 que alguien escribió una vez.
    max_age=600,
)

# --------------------------------------------------------------------------- #
# Cabeceras de cache
# --------------------------------------------------------------------------- #
#
# ## Qué cubre y qué no
#
# Cubre lo que un **caché intermedio** —CDN, proxy corporativo, el de un router de un banco—
# puede hacer con una respuesta: guardarla y servirla. Y CORS no mitiga eso, porque gobierna la
# lectura desde JavaScript, no el almacenamiento por el intermediario.
#
# ## Por qué `no-store` y no `private`
#
# Porque `private` permite al caché **del navegador** guardar la respuesta, y el token vive en
# `sessionStorage`: cerrar la pestaña y volver a abrirla en un equipo compartido dejaría la lista
# de vulnerabilidades del cliente a la vista del siguiente usuario de la máquina. `no-store` lo
# prohíbe en los dos sitios.
#
# ## Por qué `Vary` y no solo `no-store`
#:
#: Porque no todos los intermediarios respetan `no-store` —algunos lo tratan como una sugerencia
#: y cachean igual—, y el `Vary` es la defensa que sí sobrevive a un caché maleducado: le dice
#: que la respuesta **cambia** con `Authorization` y con `X-Organization-Id`, así que no puede
#: servir la de un cliente a otro. Es el mismo motivo por el que el aislamiento multi-tenant no se
#: apoya solo en el filtro del `WHERE`.
#:
#: ## Por qué no va un middleware
#:
#: Porque un middleware de respuesta tiene que reescribir la respuesta de cada ruta, y aquí
#: bastan tres cabeceras sobre las que ya se trabaja. La alternativa sería un `HTTPResponse`
#: completo, que es un middleware que además se puede equivocar.
#:
#: ## Por qué no se toca `/health` ni la raíz
#:
#: Porque no llevan datos de tenant. Ponerles `no-store` obligaría a cada sonda del orquestador
#: a Traversar el camino entero y a un `HEAD` de comprobación de vida a no poder cachear una
#: respuesta que existe para poder cachearse.


class _CabecerasDeCache(BaseHTTPMiddleware):
    """Añade `Cache-Control` y `Vary` a lo que no sea una ruta de vida."""

    #: Rutas que no llevan datos de tenant y sí se pueden cachear. La sonda del orquestador
    #: pregunta cada pocos segundos y no tiene ninguna razón para ir al origen.
    #: `/docs` no está porque solo existe en desarrollo, y ahí no hay caché que poisoning.
    RUTAS_DE_VIDA: frozenset[str] = frozenset({"/health", "/"})

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        respuesta = await call_next(request)
        if request.url.path not in self.RUTAS_DE_VIDA:
            respuesta.headers["Cache-Control"] = "no-store, max-age=0"
            respuesta.headers["Pragma"] = "no-cache"
            # El orden de los valores no importa, pero el conjunto sí: sin `Authorization` y
            # `X-Organization-Id` un caché puede servir la respuesta de un cliente a otro, y con
            # ambos puede decidir bien.
            respuesta.headers["Vary"] = "Authorization, X-Organization-Id, Accept-Encoding"
        return respuesta


app.add_middleware(_CabecerasDeCache)


class ServiceInfoResponse(BaseModel):
    """Identidad del servicio para quien consulta la raíz de la API."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "Mind Guard Fenix Team API",
                    "status": "online",
                    "docs": "/docs",
                }
            ]
        }
    )

    name: str
    status: Literal["online"] = "online"
    docs: str = "/docs"


class HealthResponse(BaseModel):
    """Resultado del sondeo de salud del proceso."""

    status: Literal["healthy"] = "healthy"


@app.get("/", response_model=ServiceInfoResponse, tags=["system"])
async def read_service_info() -> ServiceInfoResponse:
    """Evita el `404` en la raíz y descubre la documentación de la API."""

    return ServiceInfoResponse(name="Mind Guard Fenix Team API", status="online", docs="/docs")


@app.get("/health", response_model=HealthResponse, tags=["system"])
async def read_health() -> HealthResponse:
    """Sondeo de vida del proceso, pensado para orquestadores y balanceadores."""

    return HealthResponse(status="healthy")


app.include_router(organizations_router)
app.include_router(pentests_router)
app.include_router(vulnerabilities_router)
app.include_router(repositories_webhooks_router)
app.include_router(repositories_auth_router)
app.include_router(repositories_router)
app.include_router(dashboard_router)
app.include_router(api_access_router)
app.include_router(admin_router)
app.include_router(support_admin_router)
app.include_router(support_router)
app.include_router(audit_router)
# El catalogo tecnico declara `/{entry_id}` bajo `/api/v1/knowledge`, asi que tiene que
# declararse **despues** de los documentos: en caso contrario su comodin se queda con
# `/knowledge/documents` y la peticion muere con un `422` de UUID invalido. El orden de
# `include_router` es el orden de coincidencia, y decide que ruta gana.
app.include_router(knowledge_documents_router)
app.include_router(knowledge_router)
app.include_router(onboarding_router)
app.include_router(billing_router)
app.include_router(cve_router)
app.include_router(webhooks_router)
app.include_router(assets_router)
app.include_router(mcp_router)
# El chat no declara ningun comodin suelto, asi que su posicion no compite con otra
# ruta. Va al final, y no por un orden de prioridad que aqui no existe, sino para que
# anadir un router nuevo al final del bloque sea la regla y no la excepcion.
app.include_router(chat_router)
app.include_router(supply_chain_router)
app.include_router(agents_router)
