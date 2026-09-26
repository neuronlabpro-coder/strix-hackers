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
| OAuth y onboarding de repositorios | `backend/apps/repositories/oauth.py`, `router_auth.py`, `router.py`, `inventory.py` y `validation.py` | El `state` OAuth se firma con HMAC-SHA256 sobre `SECRET_KEY`, se indexa en Redis por su SHA-256 y se consume con `GETDEL` (un solo uso, 10 minutos). El callback no confía en el state: revalida la membresía activa en PostgreSQL antes de cifrar el token con AES-256-GCM. El alta de repositorio revalida los metadatos contra la API del proveedor, genera el secreto HMAC local y registra el webhook de forma best-effort (`webhook_registered: false` si el proveedor lo rechaza). `build_client_for_repository` se generalizó en `get_client_for_credential` para construir clientes acotados al tenant sin repositorio previo. No se requieren migraciones: el estado del flujo vive en Redis. |
| Validación canónica de referencias Git | `backend/apps/repositories/validation.py` | `validate_git_branch`, `validate_git_commit_sha` y `validate_git_clone_url` concentran las reglas anti-inyección y anti-traversal que antes vivían duplicadas en `workspace.py` y `autofix.py`; el allowlist de hosts se aplica también al inventario remoto. |
| Resumen de dashboard | `backend/apps/dashboard/` | `GET /api/v1/dashboard/summary` agrega KPIs, distribución por severidad y estado de monitorización por repositorio en una sola llamada de solo lectura, siempre filtrada por `organization_id`. El `security_score` vive en `score.py` como función pura y auditable (`100 - media_ponderada` de hallazgos abiertos: CRITICAL 5, HIGH 2, MEDIUM 1, LOW 0.25, INFO 0) para que el cálculo sea verificable y no una heurística dispersa en el panel. Los hallazgos por repositorio se obtienen uniendo revisiones PR → runs → vulnerabilidades y contando vulnerabilidades distintas, porque un rerun puede referenciar la misma ejecución. |
| OAuth con cliente XHR | `backend/apps/repositories/router_auth.py` | `/authorize` exige cabecera `Authorization`, que una navegación de navegador no puede portar. Con `Accept: application/json` devuelve `200` + `authorization_url` para el panel y conserva el `302` del flujo de navegador, sin duplicar la lógica de state ni el rate limit. |
| Panel web Fase 4 · Bloque 4.1 | `frontend/src/features/dashboard/`, `frontend/src/features/repositories/`, `frontend/src/charts/` | El panel consume un único endpoint agregado en lugar de N+1 consultas por repositorio. Apache ECharts entra con `echarts/core` (solo Gauge y Pie) y `React.lazy`, en un chunk propio de 153 kB gzip que el shell no descarga. El estado de carga se deriva en lugar de escribirse dentro de efectos, y el toggle de revisiones aplica una actualización optimista compartida entre dashboard y repositorios para que ambas vistas nunca divergan. |
| Listado de pentests | `backend/apps/pentests/router.py` y `schemas.py` | `GET /api/v1/pentests/` entrega página con `items`, `total`, `limit` y `offset`, filtra por `status`, `target_type`, `scan_mode` y `search` (coincidencia parcial en `target_identifier`) y agrega `findings` por run. El conteo usa `count(distinct(Vulnerability.id))` excluyendo `IGNORED` porque un `run_id` puede referenciar la misma ejecución desde varias revisiones: contar filas duplicaría hallazgos en pantalla. La búsqueda se mantiene solo sobre el identificador del target, no sobre el enum `target_type`, porque `lower()` no existe para tipos enum en PostgreSQL y forzarlo exigiría un cast en el índice. |
| Búsqueda de vulnerabilidades | `backend/apps/vulnerabilities/router.py` | `search` aplica coincidencia parcial sobre título, target y CVE, que son las dimensiones por las que un usuario busca un hallazgo. Se añadió en lugar de filtrar en el cliente: filtrar la página cargada haría que los resultados parecieran completos cuando en realidad son solo 25 filas. |
| Perfil autenticado | `backend/apps/organizations/router.py` | `GET /api/v1/auth/me` reutiliza `UserResponse` con `is_superuser` para que el cliente decida si muestra el enlace de la consola de SuperAdmin. El campo ya existía en el modelo, por lo que no requirió migración. Antes el cliente sintetizaba el usuario con el correo como nombre y el Sidebar mostraba el email donde debía ir el nombre real. |
| Consola de SuperAdmin | `backend/apps/admin/` | `require_superuser` es una dependencia propia que falla cerrado con `403` sobre `get_current_user`; comprobar el privilegio dentro de cada handler lo dejaría como remember‑now/forget‑later. El sondeo ejecuta `SELECT 1` y `PING` y responde `healthy` o `degraded` con la latencia observada, sin host, puerto, DSN ni credenciales: un estado degradado no debe convertirse en una guía de reconocimiento. `GET /api/v1/admin/organizations` es la única lectura que ignora `organization_id` a propósito, y por eso vive detrás de ese guard en lugar de reutilizar el contexto de tenant. |
| Panel web Fase 4 · Bloque 4.2 | `frontend/src/features/issues/`, `frontend/src/features/pentests/`, `frontend/src/features/admin/`, `frontend/src/features/enterprise/` | Los hooks de datos derivan `isLoading` de una clave de petición que identifica la consulta resuelta, en lugar de escribir estado dentro del efecto: el sondeo de 5 s de la vista de ejecución no debe devolver la pantalla al estado de carga. `RequireSuperuser` falla cerrado en cliente y el backend vuelve a validar, porque ocultar un enlace no es un control de acceso. El diff de autofix tiñe con `color-mix` sobre los tokens de acento y severidad en vez de declarar `rgba` literales, para que el tinte siga al token si la paleta cambia. Los badges de estado de remediación quedan monocrimos: si el estado también fuera cromático, la celda comunicaría dos escalas a la vez. |
| Triaje de vulnerabilidades | `backend/apps/vulnerabilities/router.py` y `schemas.py` | `VulnerabilityTriageRequest` declara `extra="forbid"` y un único campo `status`. R4 queda protegida en dos capas independientes: el esquema rechaza con `422` cualquier mutación forense antes de tocar la base, y el trigger `protect_vulnerability_evidence` de la Fase 2 sigue bloqueando la escritura por SQL directo. Añadir un campo al esquema es un cambio de una línea; que PostgreSQL lo permita exige una migración *forward-only*. Esa asimetría es deliberada. El `404` ante identificadores ajenos, en lugar de `403`, evita confirmar la existencia de un recurso de otro tenant. |
| Rastro de auditoría | `backend/apps/audit/` y migración `f1a2b3c4d5e6` | `audit_log` es *append-only* con un único trigger `FOR EACH STATEMENT` sobre `UPDATE OR DELETE OR TRUNCATE`. Se eligió un disparador de sentencia y no de fila: el rastro se altera en bloque o no se altera, y la versión por fila multiplicaría el coste de una tabla que ya es pequeña sin ganar nada. No existe ruta HTTP de escritura ni de borrado. Cubre el historial inmutable de MENU-MAP §3.4. |
| Revisiones de PR por repositorio | `backend/apps/repositories/router.py` y `schemas.py` | `GET /api/v1/repositories/{id}/reviews` carga primero el repositorio acotado a `organization_id`, de modo que un identificador de otro tenant devuelve `404` sin revelar siquiera si existe. `PRReviewResponse` expone `short_sha` (7 caracteres) pero nunca `head_clone_url`: publicar la URL de clonación del pull request convertiría el listado de revisiones en una vía de acceso al código del cliente. Tampoco expone `comment_id`, que no aporta nada a la decisión de triaje. |
| Catálogo de conocimiento | `backend/apps/knowledge/` y migración `f1a2b3c4d5e6` | `knowledge_entries` es una tabla de referencia **compartida y de solo lectura**, sembrada con diez apuntes OWASP/CWE. R1 prohíbe hardcodear configuración en el runtime, pero un catálogo editorial no es configuración: es un activo versionable que el equipo de seguridad mantiene con migraciones y que así puede auditarse con `git blame`. No pertenece a ninguna organización, pero la ruta sí exige autenticación porque describe técnicas ofensivas. Deliberadamente **no** es la base de conocimiento de MENU-MAP §7.0 y §7.1, que es un módulo por organización para reglas de negocio escritas por el tenant. |
| Progreso de onboarding | `backend/apps/onboarding/` | `GET /api/v1/onboarding/status` deriva el checklist del estado persistido (`GitCredential`, `Repository`, `PentestRun`) en lugar de almacenar marcas. El paso «primer escaneo» excluye los runs `QUICK`, que dispara el webhook de un PR y no el usuario: contarlos completaría el checklist sin que nadie haya lanzado nada desde el panel. Solo hay tres pasos porque son los verificables; los pasos 4 a 6 de §1.1 dependen de pantallas que siguen siendo placeholder. |
| Panel web Fase 4 · Bloque 4.3 | `frontend/src/features/issues/`, `frontend/src/features/knowledge/`, `frontend/src/features/dashboard/`, `frontend/src/features/shared/` | El tablero ofrece arrastre **y** un selector de destino por tarjeta: no es redundante, porque sin el segundo camino la acción central de la vista quedaría fuera de alcance para quien navega con teclado o lector de pantalla. `ToastProvider` vive en el árbol de rutas porque una reversión optimista sin aviso dejaría el tablero mostrando un estado que el servidor no tiene. El mismo patrón de clave derivada que en el Bloque 4.2 se aplicó a los cuatro componentes nuevos. `ReviewPicker` resuelve el repositorio de origen comparando `affected_target` con `full_name`, porque no existe columna que enlace vulnerabilidad y repositorio; cuando no hay coincidencia lo declara en pantalla en vez de ofrecer un desplegable inútil. |

## Frontend

| Referencia | Destino | Adaptación |
| --- | --- | --- |
| Shell de navegación SaaS | `frontend/src/components/Sidebar.tsx` | Sidebar React 19 con selector de organización, navegación principal, sección de activos, perfil y cierre de sesión. |
| Rutas y vistas base | `frontend/src/app/App.tsx` y `frontend/src/features/` | Se añadieron rutas de Dashboard, Pentests, Issues, PR Reviews, Repositories, Knowledge, Settings y Admin sin introducir pantallas fuera de `MENU-MAP.md`. `/dashboard`, `/repositories`, `/issues`, `/issues/:id`, `/pentests`, `/pentests/:id` y `/admin` tienen vista real; `/pr-reviews`, `/knowledge` y `/settings` siguen como placeholder de navegación porque sus endpoints todavía no existen. |
| Autenticación | `frontend/src/features/auth/` | Login y registro consumen FastAPI mediante `fetch`. El JWT se mantiene en `sessionStorage` bajo una clave de sesión y se elimina al cerrar sesión. |
| Tokens visuales de `design-dark.md` | `frontend/src/styles/index.css` | Se definieron `#1C1C1C`, `#2A2A2A`, `#EDEDED`, `#8A8F8A`, `#17a163` y `#1C1C1C` para `on-primary`, más `--color-caption` (`#B2B2B2`, derivado de `#EDEDED` al 70 % sobre la superficie para superar 4.5:1) y la rampa de severidad `--color-critical/high/medium/low/info` incorporada a `design-dark.md` por el Bloque 4.2. No se usan degradados ni acentos alternativos. El Bloque 4.1 añade tablas, badges, toggle, modal y tarjetas de gráfico; el 4.2 añade tablero, visor de código, diff, terminal, health cards y candados Enterprise; el 4.3 añade tarjetas de conocimiento, historial de auditoría, notificaciones y checklist de onboarding, **solo** con esos tokens y sus opacidades. |
| Localización | `frontend/src/i18n.ts` y `frontend/src/locales/{es,en}/` | Namespaces `common`, `auth`, `navigation`, `errors`, `dashboard`, `repositories`, `issues`, `pentests`, `admin`, `enterprise`, `triage`, `knowledge`, `onboarding` y `llm`. Todos los textos visibles de React se resuelven mediante `t()`; la auditoría comprueba que las 426 claves usadas existen en ambos idiomas, que no hay claves divergentes entre `es` y `en` y que no quedan literales visibles en JSX. |
| Cliente HTTP y tenant | `frontend/src/lib/api.ts` | Toda llamada con datos de tenant envía `Authorization` **y** `X-Organization-Id`; el servidor vuelve a validar la membresía, de modo que el encabezado del cliente nunca amplía permisos. Las respuestas `204` se tratan sin cuerpo. |
| Gráficas | `frontend/src/charts/EChart.tsx` y `palette.ts` | Envoltura mínima sobre `echarts/core` con `role="img"` y `aria-label` para que el canvas sea accesible, redimensionado en `resize` y paleta importada desde un único módulo. |

## Decisiones de seguridad

- El frontend solo usa `VITE_API_URL` como configuración pública; nunca incorpora secretos.
- El token se almacena en `sessionStorage` para limitar su persistencia en el cliente. La evolución recomendada es migrar a cookie `HttpOnly`, `Secure` y `SameSite=Strict` cuando el backend exponga ese contrato.
- Las consultas multi-tenant se filtran por `organization_id` a través de `Membership`; el middleware falla cerrado si falta el tenant o la membresía.
- `backend/tests/conftest.py` bloquea la suite fuera de `ENVIRONMENT=development/test` y fuera del modo de email `development`, desactiva los rate limiters reales solo dentro de pytest y ejecuta cada prueba de integración en una sesión SQLAlchemy con savepoints y rollback exterior obligatorio.
- `engines/`, `saas-boilerplate/` y `strix/` permanecen como referencias de solo lectura.
- El panel nunca renderiza el secreto HMAC del webhook ni tokens: el backend no los incluye en ninguna respuesta y `RepositoryResponse` solo expone `webhook_registered` como booleano.
- El estado de las revisiones de PR se cambia solo con `PATCH /api/v1/repositories/{id}`, restringido a administradores; la actualización optimista del cliente se revierte si la API responde con error.
- La consola de SuperAdmin falla cerrada en ambos lados: `RequireSuperuser` devuelve al login sin sesión y al dashboard sin privilegio, y el backend responde `403` con `require_superuser` y `401` sin token. El enlace del Sidebar solo se renderiza para superusuarios, pero eso es visibilidad, no autorización.
- `GET /api/v1/admin/organizations` es la única consulta que no filtra por `organization_id`; por diseño debe seguir siendo la única, y su protección es el guard de superusuario más la ausencia de cualquier dato sensible más allá de nombre, slug, plan y saldo.
- La consola de modelos de LLM comparte ese mismo guard: el catálogo de modelos y sus márgenes son política comercial de la plataforma, no un recurso de tenant. `llm_usage_events` sí guarda `organization_id` y `run_id`, para que el consumo se pueda atribuir a un cliente sin exponer el de otros.
- El ledger de créditos es el único origen del saldo. `organizations.credit_balance` es una caché denormalizada que `apply_credit_delta` mantiene bajo bloqueo; escribirla directamente dejaría el saldo sin asiento que lo respalde. Las tres pruebas que crean organizaciones con saldo lo hacen ahora a través de un bono de alta por esa razón.
- `audit_log` no admite `UPDATE`, `DELETE` ni `TRUNCATE` ni siquiera desde una sesión con permisos de escritura en la base. Es la misma exigencia que R4 impone a las evidencias de los hallazgos, aplicada al rastro que las documenta: un historial que se puede reescribir no vale como evidencia de auditoría.

## Fase 5 · Cobro, créditos y orquestación de modelos

- `organizations.credit_balance` pasó de `float` a `Numeric(18, 4)`. Un saldo con coma flotante deriva de centavo y un ledger de créditos que no cuadra es un ledger que nadie puede explicar. La migración `a2b3c4d5e6f7` hace el cambio ahora, con la tabla vacía, y no al final de la fase, cuando ya habrá miles de asientos que reparar.
- `credit_ledger` es *append-only* con un trigger `FOR EACH STATEMENT` sobre `UPDATE OR DELETE OR TRUNCATE`. Se eligió disparador de sentencia y no de fila porque el rastro se altera en bloque o no se altera, y la versión por fila multiplicaría el coste de una tabla que ya es pequeña sin ganar nada. `balance_after` es una fotografía del saldo en el asiento, no un saldo derivado: cada fila es verificable de forma independiente.
- `apply_credit_delta` serializa las deducciones con `SELECT ... FOR UPDATE` sobre la fila de la organización. Sin ese bloqueo, dos pentests concurrentes que leen 10 créditos y gastan 8 cada uno dejarían el saldo en −6. El movimiento se rechaza entero si dejaría el saldo negativo: no hay saldo parcial ni cobro parcial.
- Los créditos se descuentan **antes** de encolar el escaneo. Con el orden inverso, un fallo de la cola de Celery dejaría un escaneo registrado que nadie pagó y que nadie ejecutará. Si el encolado falla, `_refund_failed_dispatch` escribe un asiento compensatorio con referencia `{run_id}:refund`.
- Tras un `rollback` de SQLAlchemy, las instancias de ORM quedan expiradas y acceder a sus atributos dispara una recarga que necesita contexto verde. En la ruta de error del encolado eso significaba un `MissingGreenlet` *dentro del manejador*. Los identificadores se copian a `UUID` planos antes de cualquier rollback y el run se vuelve a leer con una consulta explícita.
- `profit_margin_pct` es un **recargo sobre coste base**, no un margen sobre precio: `precio = coste × (1 + margen/100)`. Con la definición habitual de margen, un 150 % sería imposible porque el precio tendría que ser negativo. `ChargeBreakdown` expone `markup_multiplier` y su docstring declara la semántica para que nadie la lea al revés.
- `compute_charge` es una función pura sin base de datos: el precio que paga el cliente y el beneficio neto deben poder auditarse sin levantar el sistema. Los importes se redondean a microdólar *antes* de convertirlos a créditos, y el beneficio se calcula sobre valores ya redondeados, de modo que `precio - coste == beneficio` siempre. Hay una prueba que lo verifica en bucle sobre varios márgenes y tamaños.
- El catálogo de LLMs y el registro de consumo son tablas separadas a propósito. El margen del catálogo es una intención comercial; el beneficio efectivo depende de los tokens que el motor queme de verdad. Sin `llm_usage_events` el margen sería una promesa sin respaldo, así que la consola muestra ambos.
- `resolve_model_chain` falla cerrada con `LLMAllModelsInactiveError` en lugar de recurrir a un modelo por defecto en código: un modelo implícito sería coste ni tarifado ni auditable. Solo enruta a modelos activos, de modo que apagar uno en caliente lo retira de la cadena sin borrar su historial.
- `is_transient_status` distingue los fallos que mejoran con reintento (408, 429, 5xx, timeouts) de los que no (400, 401, 403, 404, 422). Reintentar un 401 contra el siguiente modelo solo gastaría tiempo y tokens sin cambiar el resultado.
- `CREDIT_PACKS` vive en el backend, no en el frontend. R1 prohíbe precios en el código, y un importe de recarga que el panel pudiera alterar sería un importe que el backend no conoce.
- La organización viaja al webhook en la `metadata` firmada por Stripe, nunca en la URL: la URL de Checkout es pública y se puede compartir o interceptar. `success_url` y `cancel_url` se validan contra `frontend_base_url` para impedir open redirect.
- La idempotencia del webhook descansa en la restricción única de `stripe_events.event_id`, no en una comprobación previa. La comprobación cubre la reentrega secuencial; la restricción cubre también dos entregas concurrentes del mismo evento, que es el caso que un `SELECT` previo no resuelve.
- `Settings` es `frozen=True` a propósito. Cuando una prueba necesita cambiar un valor, la solución no fue relajar la inmutabilidad sino pasar el secreto al constructor de `StripeSDKClient`, que además evita que el secreto se relea del estado global en cada llamada.
- La API de webhooks del SDK cambió en `stripe` 15.x: `StripeClient` ya no expone `webhooks` y `WebhookSignature.verify_header` solo verifica (devuelve un booleano) en lugar de devolver el evento. La implementación verifica y luego parsea el mismo `bytes` con `json`, lo que garantiza que el objeto verificado y el procesado son el mismo.
- La verificación de firma se prueba con el SDK real y HMAC calculado a mano, no con un doble. Un doble que devuelve `True` demuestra que el router llama al verificador, no que el verificador rechaza un payload manipulado.

## Fase 5 · Bloque 5.2 — Catálogo oficial, enlace al runner y base de datos CVE

- `profit_margin_pct` pasa a llamarse `markup_pct` (migración `b3c4d5e6f7a8`). El nombre anterior describía una magnitud distinta de la que representa: con la definición habitual de margen —porcentaje sobre el precio de venta— un 150 % sería imposible, porque el precio tendría que ser negativo. Renombrar el campo cuesta una migración y ahorra una discusión recurrente con el equipo de finances.
- `organizations.credit_balance` se estrecha de `Numeric(18, 4)` a `Numeric(12, 4)`. Con 1 crédito = 1 USD, 99.999.999,99 créditos cubren cualquier saldo de una plataforma de por vida, y un saldo no negativo solo necesita un dígito entero. La precisión extra era *airport*.
 - El catálogo oficial se versiona dentro de la migración, no en la aplicación. Los modelos con sus costes publicados y su recargo comercial son una decisión de negocio fechada: en el historial de migraciones se lee con `git blame` quién cambió qué y cuándo, algo que una constante en Python no documenta.
 - Los modelos retirados se **desactivan** en vez de borrarse, porque `llm_usage_events` los referencia. La invariante de unicidad de `priority_order` se comprueba por eso sobre los modelos **activos**: los inactivos no entran en `resolve_model_chain` y su prioridad es irrelevante. La migración desactiva antes de insertar para que ningún activo comparta prioridad con un retirado; si lo hiciera, el desempate de `resolve_model_chain` pasaría a depender del alfabeto del slug.
- El sandbox recibe el modelo resuelto por el orquestador, no `DEFAULT_STRIX_LLM`. `container_environment()` pasó de privado a público porque su contenido *es* el contrato del runner y las pruebas necesitan poder comprobarlo sin levantar un contenedor. Si el catálogo está vacío, se recurre al valor de configuración: es preferible un modelo de tarificación desconocida a no ejecutar el escaneo, y el worker deja constancia del slug usado.
- El fallback ante `429`/`5xx` **no** se implementa dentro del contenedor: Strix habla con OpenRouter por su cuenta y el worker no ve los códigos HTTP del proveedor. Lo que sí puede observar es que la ejecución falló, así que reencola con el siguiente modelo de la cadena. Solo se insiste ante errores que otro modelo podría mejorar; un contenedor que ni arrancó no mejora cambiando de proveedor.
- `extract_token_usage` devuelve `None`, no cero, cuando el reporte no publica consumo. Cero significa «el motor consumió tokens gratis» y `None` significa «no lo sabemos», y cobrar en función de la segunda lectura sería tarificar al aire. Un bloque con algún token declarado pero inutilizable se descarta entero: aceptar la mitad buena de un dato corrupto cobra la mitad del consumo real y regala la otra mitad.
- La tarificación posterior al run cobra la **diferencia** contra la reserva, no el consumo completo. El escaneo ya se cobró por adelantado al encolarlo; cargar el consumo otra vez dejaría pagando dos veces al cliente. El signo del movimiento se invierte dentro de `charge()` para que ningún llamador tenga que acordarse de él: una tarificación invertida no rompe nada, solo regala margen en silencio.
- `scan_credit_cost` vive en `backend/apps/billing/pricing.py` y no en el router de pentests. El worker lo necesita para ajustar la reserva y el router importa al worker, así que dejarlo en el router cerraba un ciclo de importación.
- `cve_records` es catálogo **público de referencia**: no tiene `organization_id` y no le aplica R3. La ruta sí exige autenticación porque describe técnicas de explotación en detalle, y lleva rate limit **por usuario** y no por organización, porque dos personas de la misma empresa consultando a la vez son uso legítimo mientras que un bucle de scraping salta siempre a uno solo.
- La búsqueda de CVE escapa `\`, `%` y `_` antes de interpolarlos en el `ILIKE`. Sin eso, buscar `CVE-2026-900001` devolvía ocho registros porque `_` es un comodín de un carácter. No es inyección —la consulta está parametrizada— pero es semántica incorrecta: el usuario recibe coincidencias que no pidió.
- El filtro por año usa trigram (`gin_trgm_ops`) y no el índice btree, porque `cve_id LIKE 'CVE-2026-%'` no usa un btree. La extensión `pg_trgm` debe existir **antes** del `CREATE INDEX`: PostgreSQL valida la clase de operadores en ese momento, no después.
- `available_years` ordena en Python y no en SQL. PostgreSQL exige que la expresión de `ORDER BY` aparezca en la lista de `SELECT` cuando hay `DISTINCT`, y duplicar el `substring` en la consulta sería peor que ordenar veinte números en memoria.
- `alembic check` no sabe comparar índices funcionales, así que `include_object` en `backend/migrations/env.py` excluye el índice GIN sobre `to_tsvector`. Excluir no es silenciar: la definición real sigue en la migración, que es donde se revisa, y sin la exclusión la puerta de calidad sería inútil.
- La expresión del índice tiene que coincidir **carácter a carácter** entre el modelo y la migración. El modelo construye `to_tsvector('spanish'::regconfig, ...)` porque `REGCONFIG` no tiene renderizador de literales en SQLAlchemy; la migración creaba `to_tsvector('spanish', ...)`. Misma expresión, texto distinto, y Alembic los trata como índices diferentes.

## Verificación realizada

- `npm run typecheck`, `npm run lint` y `npm run build` en `frontend/`.
- `ruff check`, `pyright` y `pytest` en `backend/`.
- `alembic current` y `alembic check` contra la base remota configurada.
- Test de aislamiento multi-tenant con Alpha y Beta.
- Bloque 3.3: `165 passed, 2 skipped`; `ruff check` sin hallazgos; `pyright --project backend/pyproject.toml` con 0 errores; `alembic current` = `e8a0b2c4d6e8 (head)` y `alembic check` sin drift.
- Bloque 4.1: `180 passed, 2 skipped`; `ruff check` y `pyright` limpios; `npm run typecheck` y `npm run lint` sin advertencias; `npm run build` genera el chunk principal (114 kB gzip) y el chunk diferido de ECharts (153 kB gzip); auditoría de strings sin literales visibles y con paridad de claves es/en.
- Bloque 4.2: `192 passed, 2 skipped`; `ruff check` y `pyright --project backend/pyproject.toml` con 0 errores; `alembic check` sin drift. En frontend, `typecheck`, `lint` y `build` limpios, con el chunk principal en 429 kB (127 kB gzip) y ECharts en 453 kB (153 kB gzip). Auditoría i18n: 312 claves usadas resueltas en `es` y `en`, sin divergencias entre idiomas y sin literales visibles en JSX. Verificado en el backend en ejecución: las nueve rutas nuevas están registradas en `/openapi.json` y responden `401` sin token.
- Bloque 4.3: `213 passed, 2 skipped`; `ruff check` sin hallazgos; `pyright --project backend/pyproject.toml` con 0 errores; `alembic upgrade head` aplicó `f1a2b3c4d5e6` y `alembic check` no detecta drift. En frontend, `typecheck` y `lint` sin errores ni advertencias; `build` con chunk principal de 456 kB (134 kB gzip) y ECharts de 453 kB (153 kB gzip). Auditoría i18n: 373 claves usadas en trece namespaces en paridad es/en, sin literales visibles en JSX. Verificado en el backend en ejecución: las cinco rutas nuevas registradas, `PATCH` declarado junto a `GET` en la ruta de vulnerabilidad y las cinco respondiendo `401` sin token.
- Bloque 5.1: `257 passed, 2 skipped`; `ruff check` sin hallazgos; `pyright --project backend/pyproject.toml` con 0 errores; `alembic upgrade head` aplicó `a2b3c4d5e6f7` y `alembic check` no detecta drift. En frontend, `typecheck` y `lint` sin errores ni advertencias; `build` con chunk principal de 473 kB (138 kB gzip) y ECharts de 453 kB (153 kB gzip). Auditoría i18n: 426 claves usadas en catorce namespaces en paridad es/en, sin literales visibles en JSX. Verificado en el backend en ejecución: las cinco rutas nuevas registradas, `POST /api/v1/billing/checkout-session` responde `401` sin token, `POST /api/v1/billing/webhooks` responde `400` a un payload sin firma y `GET /api/v1/admin/llm/` responde `401` sin token.
- Bloque 5.2: `295 passed, 2 skipped`; `ruff check` sin hallazgos; `pyright --project backend/pyproject.toml` con 0 errores; `alembic upgrade head` aplicó `b3c4d5e6f7a8`, `c4d5e6f7a8b9` y `d5e6f7a8b9c0`, `alembic current` = `d5e6f7a8b9c0 (head)` y `alembic check` no detecta drift. En frontend, `typecheck` y `lint` sin errores ni advertencias; `build` con chunk principal de 488 kB (141 kB gzip), CSS de 38 kB y ECharts de 453 kB (153 kB gzip). Auditoría i18n: 551 claves usadas en quince namespaces en paridad es/en, sin literales visibles en JSX. Auditoría de `.env.example`: paridad con los 116 campos de `Settings`. Verificado contra la base remota: los ocho modelos del Owner activos con sus precios y prioridades, los seis retirados inactivos, y `z-ai/glm-5.3` inyectado como `STRIX_LLM` en las cuatro cadenas. Verificado en el backend en ejecución: las cuatro rutas de `/api/v1/cve` registradas en `/openapi.json` y respondiendo `401` sin token.

## Fase 5 · Bloque 5.2 · Sustitución del catálogo por el listado del Owner

- **Un cambio de catálogo es una migración nueva, no una edición.** La migración `b3c4d5e6f7a8` ya estaba aplicada en la base remota por Tailscale cuando el Owner sustituyó el catálogo por su listado definitivo (`d5e6f7a8b9c0`). Reescribir el seed de una migración aplicada deja el repositorio y la base discrepuyendo, y `alembic check` no lo detecta porque compara el esquema, no las filas. Los datos que no se pueden reescribir se corrigen hacia adelante, que es la única dirección que la historia de la base permite deshacer.
- La variable que inyecta el modelo al contenedor es **`STRIX_LLM`**, no `STRIX_LLM_MODEL`. El motor la lee con `os.getenv` en `strix/llm/config.py` y `strix/interface/main.py`, y además aborta el arranque si falta. Inyectar con otro nombre no daría ningún error: el contenedor caería al `openai/gpt-5` que el motor trae por defecto, cada escaneo correría fuera del catálogo y `llm_usage_events` nunca se escribiría. Es la conmutación silenciosa más cara posible en este proyecto, y por eso hay una prueba que ata la resolución con el nombre exacto de la variable.
- `z-ai/glm-5.3` se declara `use_case = ALL` aunque el Owner lo pidió como primario de `ALL / DEEP_PENTEST`. `model_id` es único y una fila solo admite un caso de uso, así que duplicar la fila es imposible. `ALL` es la única forma de expresar «primario de todas sin duplicar», porque `resolve_model_chain` incluye los modelos `ALL` en cada cadena. El efecto pedido se cumple exactamente y la restricción de unicidad se mantiene intacta.
- Los costes del catálogo son los que el Owner ha fijado, no los que publica OpenRouter. El sistema no los verifica contra ningún feed: `markup_pct` es la política comercial y los precios base son un dato de entrada. Si un precio se equivoca, el margen se audita contra el número equivocado, así que conviene contrastarlos con la carta de precios antes de abrir el producto comercialmente.


## Fase 5 · Bloque 5.3 — Ejecución real contra la API de Stripe

- **`create_checkout_session` estaba roto y la suite no lo podía ver.** Pasaba
  `mode=`, `line_items=`, `metadata=` como argumentos con nombre, pero en `stripe` 15.x
  la firma real es `create_async(params: SessionCreateParams, options)`: **un único
  diccionario posicional**. La llamada reventaba con `TypeError` en la primera compra
  de un cliente. Los dobles de la suite aceptaban cualquier forma de llamada, así que
  257 pruebas en verde coexistían con una función incapaz de crear una sola sesión.
  Corregido, y con dos pruebas nuevas: una reproduce la firma real del SDK instalado y
  falla si vuelve a cambiar, y la otra ata que la llamada sea un dict posicional sin
  kwargs.
- **`StripeClient.checkout` está deprecado en favor de `StripeClient.v1.checkout`.** Se
  usa `v1` con recurso al camino antiguo si el SDK fuera anterior, para no depender de
  una versión exacta. Mantener el deprecado obligaría a tocar esto otra vez dentro de
  dos versiones.
- **Un `stripe trigger` no puede acreditar créditos, y no debe.** El fixture de
  `checkout.session.completed` no lleva `metadata.organization_id`, así que el webhook
  devuelve `422` y no escribe nada. Es el comportamiento correcto: un evento sin tenant
  identificable no puede mover el saldo de nadie. La prueba del camino completo exige
  una sesión creada por la propia aplicación.
- **El trigger append-only de R4 impide borrar una organización con historial


## Fase 5 · Bloque 5.4 — Borrado lógico de organizaciones

- **Un tenant nunca se borra físicamente.** R4 lo hace imposible: los triggers
  `BEFORE DELETE` sobre `credit_ledger` y `audit_log` bloquean la cascada. La baja es
  `deleted_at` + `is_active = false`, y la fila sobrevive porque el ledger y el rastro
  forense apuntan a ella.
- **`credit_ledger` y `audit_log` pasan a `ON DELETE RESTRICT`.** Con `CASCADE` el
  borrado de un tenant dispara un `DELETE` que el propio trigger bloquea, y el error
  resultante habla de un trigger de auditoría en lugar de de la política de borrado.
  `RESTRICT` declara la intención en el esquema y falla antes de tocar nada. Se elige
  `RESTRICT` y no `NO ACTION` porque el segundo pospondría la comprobación al final de
  la transacción, admitting un borrado seguido de una inserción que termine en un estado
  imposible.
- **La baja revoca el acceso al tenant, no la cuenta.** Se desactivan las
  `Membership` y nada más. Desactivar `User.is_active` era la primera versión y era un
  error: el usuario que pierde un workspace se quedaba sin poder iniciar sesión, ver
  sus otros workspaces o crear uno nuevo. Peor, un consultor que trabajaba para dos
  clientes perdía el acceso a los dos. `get_current_tenant` ya exige
  `Membership.is_active`, así que la revocación es real sin necesidad de una lista de
  tokens revocados: el JWT sigue siendo criptográficamente válido, pero deja de
  resolver contexto para ese tenant.
- **El asiento de baja se escribe antes de revocar y antes de llamar a Stripe.** Es lo
  único que sobrevive a un fallo posterior, y es la prueba de por qué se cortó el
  acceso. El orden completo es: asentar → revocar → cancelar en Stripe. La cancelación
  va última porque es la única que depende de una red externa; si fallara, el tenant ya
  está dado de baja y solo queda reconciliar un cobro, que es un problema de
  facturación. Al revés quedaría un tenant vivo con la baja asentada, que es un estado
  en el que nadie sabe qué hacer. El fallo de Stripe no propaga `500` por el mismo
  motivo: deja el tenant dado de baja y registra el aviso, tanto en la respuesta como
  en el rastro.
- **La segunda baja se rechaza con `403`, no es idempotente.** Al revocarse las
  membresías, la segunda llamada ya no resuelve contexto de tenant. Es mejor que un
  `200` idempotente: significa que la revocación se aplicó de verdad y no solo se
  escribió una fecha.
- **`organizations.stripe_customer_id` es nuevo y necesario.** Sin él la plataforma no
  puede localizar al cliente en Stripe: cada sesión de Checkout creaba un cliente
  distinto y cancelar una suscripción era imposible. Con el campo, la baja ya puede
  enumerar y cancelar las suscripciones vivas del cliente.
- **El cobro se verifica de extremo a extremo con la firma, no con la página de Stripe.**
  `payment_status` no es un parámetro actualizable de una Checkout Session, así que la
  única vía real para un pago es la página de Checkout alojada, que necesita un
  navegador. La entrega de Stripe ya se comprobó aparte con `stripe trigger`: el evento
  llegó, la firma se verificó y el endpoint respondió `422` por no identificar
  organización. La prueba permanente firma un evento realista con el secreto real y lo
  entrega al endpoint real, lo que cubre firma, idempotencia, ledger y saldo en CI sin
  credenciales.
  financiero.** `credit_ledger` y `audit_log` son `BEFORE DELETE`, y sus FK hacia
  `organizations` son `ON DELETE CASCADE`, así que borrar el tenant dispara un DELETE
  sobre el rastro que el propio trigger bloquea. Consecuencia de producto: **no existe
  borrado de organización**, y eso choca de frente con el «Danger Zone → Delete
  organization» de MENU-MAP §10. Requiere una decisión explícita: borrado lógico con
  anonimización, una vía de borrado controlada, o asumir que un tenant con historial
  financiero es permanente.
