# Decisiones de adaptación de la base SaaS

## Contexto

Mind Guard Fenix Team toma como referencia de lectura el boilerplate SaaS ubicado en `saas-boilerplate/` y el motor Strix ubicado en `strix/`. Ambos directorios permanecen sin modificaciones y no se importan desde el runtime, conforme a la regla R2.

Las adaptaciones siguientes viven exclusivamente en `backend/` y `frontend/`. Se conservaron los conceptos de organizaciones, usuarios, autenticación y shell, pero se rediseñaron para SQLAlchemy 2, FastAPI asíncrono, React 19 y el sistema de diseño Dark Emerald.

## Backend

| Origen conceptual | Destino | Adaptación |
| --- | --- | --- |
| Modelo de organización y workspace | `backend/apps/organizations/models.py` | `Organization` usa UUID, `slug` único, `PlanTierEnum` y timestamps. Se añadió `Organization` como raíz de aislamiento. |
| Usuario y pertenencia a workspace | `backend/apps/organizations/models.py` | `User`, `Membership` e `Invitation` usan UUID, FKs con `CASCADE`, índices de FK y el índice único compuesto `ix_membership_org_user`. |
| Flujo de sesión | `backend/core/security.py` | Se implementaron hash y verificación con bcrypt nativo. Se aplica SHA-256 de tamaño fijo antes de bcrypt para soportar el límite de contraseña configurado y conservar un hash bcrypt con salt. |
| Email y verificación | `backend/apps/organizations/models.py`, `backend/apps/organizations/services.py` y `backend/apps/organizations/schemas.py` | `EmailStr` valida la entrada, los emails se normalizan con `strip().lower()` antes de consultar o guardar y `User.email_verified` comienza en `false` para el futuro flujo de confirmación. |
| Rate limiting de autenticación | `backend/core/rate_limit.py` y `backend/apps/organizations/router.py` | Login y registro usan contadores Redis por IP con ventana fija, límites configurables, Lua atómico, `429` y `Retry-After`; Redis no disponible falla cerrado con `503`. |
| Emisión de tokens | `backend/core/security.py` | JWT con `python-jose`, algoritmo y expiración controlados por variables de entorno. |
| Dependencias de autenticación | `backend/core/middleware.py` | `get_current_user` valida el JWT y `get_current_tenant` exige `X-Organization-Id` y una membresía activa, fallando cerrado con HTTP 403. |
| Servicios de cuentas y workspaces | `backend/apps/organizations/services.py` | Registro, login, creación de organizaciones e invitaciones se ejecutan con SQLAlchemy asíncrono y transacciones explícitas. |
| API de cuentas y organizaciones | `backend/apps/organizations/router.py` | Se expone REST bajo `/api/v1` con Pydantic estricto y respuestas sin secretos de contraseña ni tokens de invitación. |

### Migraciones

Alembic utiliza `Base.metadata` y `settings.database_url` desde `backend/migrations/env.py`. La primera revisión es `bb33e283b0d5_fase1_initial_multitenant_schema` y se ejecuta con `asyncpg` en modo asíncrono.

## Frontend

| Referencia | Destino | Adaptación |
| --- | --- | --- |
| Shell de navegación SaaS | `frontend/src/components/Sidebar.tsx` | Sidebar React 19 con selector de organización, navegación principal, sección de activos, perfil y cierre de sesión. |
| Rutas y vistas base | `frontend/src/app/App.tsx` y `frontend/src/features/` | Se añadieron rutas de Dashboard, Pentests, Issues, PR Reviews, Repositories, Knowledge y Settings sin introducir pantallas fuera de `MENU-MAP.md`. |
| Autenticación | `frontend/src/features/auth/` | Login y registro consumen FastAPI mediante `fetch`. El JWT se mantiene en `sessionStorage` bajo una clave de sesión y se elimina al cerrar sesión. |
| Tokens visuales de `design-dark.md` | `frontend/src/styles/index.css` | Se definieron exactamente `#1C1C1C`, `#2A2A2A`, `#EDEDED`, `#8A8F8A`, `#17a163` y `#1C1C1C` para `on-primary`, además de Inter y JetBrains Mono. No se usan degradados ni acentos alternativos. |
| Localización | `frontend/src/i18n.ts` y `frontend/src/locales/{es,en}/` | Se configuran namespaces `common`, `auth`, `navigation` y `errors`. Todos los textos visibles de React se resuelven mediante `t()`. |

## Decisiones de seguridad

- El frontend solo usa `VITE_API_URL` como configuración pública; nunca incorpora secretos.
- El token se almacena en `sessionStorage` para limitar su persistencia en el cliente. La evolución recomendada es migrar a cookie `HttpOnly`, `Secure` y `SameSite=Strict` cuando el backend exponga ese contrato.
- Las consultas multi-tenant se filtran por `organization_id` a través de `Membership`; el middleware falla cerrado si falta el tenant o la membresía.
- `backend/tests/conftest.py` bloquea la suite cuando `ENVIRONMENT=production`, desactiva el rate limiter real solo dentro de pytest y elimina automáticamente los registros canary `alpha-*`, `beta-*` y `api-*` al finalizar cada prueba de integración.
- `engines/`, `saas-boilerplate/` y `strix/` permanecen como referencias de solo lectura.

## Verificación realizada

- `npm run build` y `npm run typecheck` en `frontend/`.
- `ruff check`, `pyright` y `pytest` en `backend/`.
- `alembic current` y `alembic check` contra la base remota configurada.
- Test de aislamiento multi-tenant con Alpha y Beta.
