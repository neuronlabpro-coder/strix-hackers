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
| Email y verificación | `backend/apps/organizations/models.py`, `backend/apps/organizations/services.py`, `backend/apps/organizations/router.py` y `backend/core/email.py` | `EmailStr` valida la entrada, los emails se normalizan con `strip().lower()` antes de consultar o guardar, `User.email_verified` comienza en `false`, el token se almacena solo como hash y el login exige verificación. El entorno local expone el token; producción exige SMTP, HTTPS y STARTTLS. El reenvío rota el token sin revelar la existencia de una cuenta. |
| Invitaciones | `backend/apps/organizations/services.py`, `backend/apps/organizations/router.py` y `frontend/src/features/auth/AcceptInvitationPage.tsx` | Las invitaciones se envían por SMTP, se validan contra el email del usuario autenticado, expiran y solo pueden aceptarse una vez. El enlace frontend permite completar el flujo sin exponer el token en respuestas productivas. |
| Rate limiting de autenticación | `backend/core/rate_limit.py` y `backend/apps/organizations/router.py` | Login usa contadores Redis atómicos por IP y por cuenta normalizada; registro, verificación, invitaciones y creación de pentests tienen límites configurables. Redis no disponible falla cerrado con `503`. |
| Emisión de tokens | `backend/core/security.py` | JWT con `python-jose`, algoritmo y expiración controlados por variables de entorno. |
| Dependencias de autenticación | `backend/core/middleware.py` | `get_current_user` valida el JWT y `get_current_tenant` exige `X-Organization-Id` y una membresía activa, fallando cerrado con HTTP 403. |
| Servicios de cuentas y workspaces | `backend/apps/organizations/services.py` | Registro, login, creación de organizaciones e invitaciones se ejecutan con SQLAlchemy asíncrono y transacciones explícitas. |
| API de cuentas y organizaciones | `backend/apps/organizations/router.py` | Se expone REST bajo `/api/v1` con Pydantic estricto; los tokens de invitación y verificación solo se incluyen en respuestas del modo local `development`. |

### Migraciones

Alembic utiliza `Base.metadata` y `settings.database_url` desde `backend/migrations/env.py`. La primera revisión es `bb33e283b0d5_fase1_initial_multitenant_schema`; las revisiones Fase 2 `7dd8e09a263d`, `c3f8a1d9e2b4`, `d4e6f8a1b2c3`, `e5f7a9b1c3d4`, `f6a8b0c2d4e6`, `a7b9c1d3e5f7`, `b8c0d2e4f6a8`, `c9d1e3f5a7b9` y `d0e2f4a6b8c9` añaden runs, vulnerabilidades, provenance/idempotencia, la FK compuesta de aislamiento, los triggers R3/R4 endurecidos, el ID de tarea Celery y el marcador de reintento de cleanup. Las revisiones Fase 3 `e1f3a5c7e9b0`, `e2f4b6d8a0c1`, `e3a5c7c9d1e2`, `e4b6c8d0f2a3`, `e5c7d9e1f3a5`, `e6d8f0a2b4c6`, `e7f9a1b3c5d7` y `e8a0b2c4d6e8` añaden las tablas, índices, restricciones cifradas, FK/trigger de tenant, idempotencia y provenance de materialización de revisiones PR; las últimas seis son forward-only para no debilitar R3. Las migraciones se ejecutan con `asyncpg` en modo asíncrono.

## Fase 2 · Datos y cola de ejecución

| Entregable | Ubicación | Decisión |
| --- | --- | --- |
| Persistencia de runs | `backend/apps/pentests/models.py` y migraciones Fase 2 | `PentestRun` pertenece a una organización, persiste `source_scan_id` y `celery_task_id`, y se indexa por `(organization_id, status)`; `trg_protect_pentest_run_tenant` impide mover su identidad o provenance entre tenants. |
| Persistencia de hallazgos | `backend/apps/vulnerabilities/models.py` | `Vulnerability` exige que `run_id` y `organization_id` coincidan mediante FK compuesta, valida CVSS entre 0 y 10 y expone los índices compuestos por severidad y estado. |
| Inmutabilidad R4 | Migraciones `7dd8e09a263d`, `c3f8a1d9e2b4`, `d4e6f8a1b2c3`, `e5f7a9b1c3d4` y `f6a8b0c2d4e6` | El trigger PostgreSQL `trg_protect_vulnerability_evidence` bloquea cambios de identidad, provenance, PoC, CVSS, CVE y autofix mediante `UPDATE`, `DELETE` o `TRUNCATE`; solo el estado de remediación y `updated_at` pueden evolucionar. `source_finding_id` y la restricción única por run hacen idempotente la reentrega. La migración de esquema es irreversible en producción para evitar borrar evidencia. |
| Parser Strix | `backend/workers/parser/strix_parser.py` y `normalizer.py` | El parser es puro, exige `status=completed`, `scan_id` e IDs de finding, lanza excepciones tipadas y devuelve entidades sin abrir una transacción. La tarea de ingesta exige el `expected_scan_id` y realiza la persistencia atómica. |
| Cola Celery | `backend/workers/celery_app.py` y `backend/workers/tasks.py` | Redis usa una base lógica separada (`CELERY_REDIS_DB`), serialización JSON estricta, prefetch 1, límites de tiempo y beat periódico. La ingesta registra fallos del run sin ocultar el error al operador. |
| Runner y watchdog | `backend/workers/runner/sandbox.py`, `docker_client.py` y `tasks.py` | Cada run usa bridge Docker dedicado, workspace efímero con permisos privados, cgroups 4 GB/2 vCPU, límite de PIDs, secretos LLM solo por entorno, `run-name` determinista para provenance, lectura `O_NOFOLLOW` del artefacto y cleanup en `finally`. `worker_ready` y el watchdog periódico reconcilian runs `RUNNING` obsoletos como `FAILED`, purgan sus recursos y reintentan cleanup pendiente. |

## Fase 3 · Integraciones Git

| Entregable | Ubicación | Decisión |
| --- | --- | --- |
| Integraciones Git | `backend/apps/repositories/` y `backend/core/crypto.py` | Tokens Git se cifran con AES-256-GCM y permanecen cifrados en PostgreSQL; la descifración ocurre solo al construir un cliente ligado a `organization_id`. GitHub y GitLab usan clientes REST autenticados, y el webhook valida firmas HMAC antes de encolar el evento. |
| Pipeline PR y materialización | `backend/apps/repositories/workspace.py`, `pipeline.py`, `tasks.py` y `feedback.py` | El webhook crea revisiones idempotentes, el pipeline ejecuta un scan QUICK con archivos incrementales, publica checks/comentarios y purga el workspace en `finally`; el código fuente nunca se persiste en PostgreSQL. |
| ChatOps y Autofix | `backend/apps/repositories/chatops.py`, `autofix.py` y `patches.py` | Los comandos ChatOps verifican permisos de escritura; los parches R4 se validan y se aplican mediante clientes Git sin almacenar el código fuente del cliente. |

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
- `backend/tests/conftest.py` bloquea la suite fuera de `ENVIRONMENT=development/test` y fuera del modo de email `development`, desactiva los rate limiters reales solo dentro de pytest y ejecuta cada prueba de integración en una sesión SQLAlchemy con savepoints y rollback exterior obligatorio.
- `engines/`, `saas-boilerplate/` y `strix/` permanecen como referencias de solo lectura.

## Verificación realizada

- `npm run build` y `npm run typecheck` en `frontend/`.
- `ruff check`, `pyright` y `pytest` en `backend/`.
- `alembic current` y `alembic check` contra la base remota configurada.
- Test de aislamiento multi-tenant con Alpha y Beta.
