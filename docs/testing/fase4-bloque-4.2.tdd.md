# Evidencia TDD — Fase 4 — Bloque 4.2

## Alcance

Gestor de vulnerabilidades (§3 y §3.4), gestor de pentests con terminal en vivo (§2), secciones
Enterprise con candado en el Sidebar y base de la consola de SuperAdmin (`/admin`), con los contratos
de backend que la interfaz necesita: listado paginado de pentests, búsqueda de vulnerabilidades,
perfil autenticado y consola de administración.

## RED

Pruebas escritas antes de la implementación, todas fallando por ausencia de ruta:

| Prueba | Fallo inicial |
| --- | --- |
| `test_pentest_list_is_paginated_and_counts_findings` | `404` en `GET /api/v1/pentests/` |
| `test_pentest_list_filters_by_status_mode_and_search` | `404` en `GET /api/v1/pentests/` |
| `test_pentest_list_never_leaks_other_tenants` | `404` en `GET /api/v1/pentests/` |
| `test_vulnerability_search_filters_title_and_target` | el parámetro `search` se ignoraba, `total` no se filtraba |
| `test_superuser_lists_organizations_with_plan_and_credits` | `404` en `GET /api/v1/admin/organizations` |
| `test_admin_routes_reject_regular_users` | `404` en las dos rutas de administración |
| `test_admin_routes_require_authentication` | `404` en lugar de `401` |
| `test_infrastructure_health_reports_dependencies` | `404` en `GET /api/v1/admin/health` |
| `test_current_user_profile_exposes_superuser_flag` | `404` en `GET /api/v1/auth/me` |

Resultado: `8 failed`.

## GREEN

Implementación y resultado de la suite completa: `192 passed, 2 skipped`.

Añadidos en backend:

- `GET /api/v1/pentests/` (`backend/apps/pentests/router.py`) con filtros por estado, tipo, modo y
  búsqueda, paginación y `findings` agregado por run. El conteo excluye los hallazgos `IGNORED` y
  cuenta vulnerabilidades distintas, porque un `run_id` puede referenciarse desde varias revisiones.
- `search` en `GET /api/v1/vulnerabilities/` sobre título, target y CVE.
- `GET /api/v1/auth/me` reutilizando `UserResponse` con `is_superuser`. Sin migración: el campo ya
  existía en el modelo.
- `backend/apps/admin/` con `require_superuser`, `check_infrastructure`, los esquemas y los dos
  endpoints de administración. El sondeo ejecuta `SELECT 1` y `PING` y degrada a `degraded` sin
  filtrar credenciales.

Añadidos en frontend: `features/issues/`, `features/pentests/`, `features/admin/`,
`features/enterprise/`, cuatro namespaces nuevos en `es` y `en`, tokens de severidad, y los estilos de
tablero, visor de código, diff, terminal, health cards y candados.

## Correcciones durante la implementación

1. **`lower()` sobre columna enum:** la primera versión de la búsqueda de pentests filtraba también
   por `target_type`, y PostgreSQL lanzaba `UndefinedFunctionError: function
   lower(target_type_enum) does not exist`. Se eliminó esa rama en lugar de añadir un cast: la
   búsqueda por el identificador del target cubre el caso de uso y el cast penalizaría el índice.
2. **Semántica del estado agregado:** el sondeo devolvía `online` en el campo `status` raíz, lo que
   mezclaba el estado de una dependencia con el de la infraestructura. Se separaron
   `DependencyStatusEnum` (`healthy`/`degraded`) del `Literal` de dependencia (`online`/`offline`).
3. **Literales visibles propios:** el terminal imprimía `ok` y `...` y la ficha de vulnerabilidad
   etiquetaba el campo como `review_id`. Ambos violaban R1; se movieron a `pentests.terminal.done`,
   `pentests.terminal.active` y `issues.detail.autofix.reviewId`.
4. **Import dinámico inútil:** el modal de nuevo pentest hacía `await import('../../lib/api')`, que no
   separaba chunk porque el módulo ya se importa estáticamente en toda la app. Se eliminó; Vite dejó
   de emitir el aviso `INEFFECTIVE_DYNAMIC_IMPORT`.
5. **Clases de badge inexistentes:** los badges de estado usaban `badge-status-*` sin ninguna regla
   CSS que los definiera, de modo que se veían idénticos. Se añadieron reglas explícitas: los estados
   terminales en color de error, los de éxito con borde de acento y los descartados con borde
   discontinuo, todo monocromo.
6. **Literales de color en el diff:** los tintes de línea añadida y suprimida eran `rgba` literales.
   Se sustituyeron por `color-mix` sobre `--color-accent` y `--color-critical`, para que el tinte
   siga al token si la paleta cambia.

## Estado de carga derivado

Los hooks `useIssues`, `usePentests`, `AdminPage`, `PentestRunPage` y `VulnerabilityDetailPage`
escribían estado sincrónicamente dentro de los efectos, lo que disparaba cinco advertencias
`react(set-state-in-effect)`. Se resolvió derivando `isLoading` de una clave que identifica la
consulta resuelta: mientras la clave no coincida con la petición actual, la vista está cargando.

Esto no es cosmético en `PentestRunPage`: el sondeo cada 5 s de una ejecución en curso habría devuelto
la pantalla completa al estado de carga cinco segundos después de mostrarse, interrumpiendo la
lectura de la cronología. Con la clave, el refresco silencioso no toca `isLoading`.

## Garantías cubiertas

| Garantía | Cómo se cubre |
| --- | --- |
| Aislamiento multi-tenant del listado de pentests | `test_pentest_list_never_leaks_other_tenants`: el tenant A obtiene `total = 0` y el texto de la respuesta no contiene el target del tenant B |
| La consola de administración no es accesible para usuarios normales | `test_admin_routes_reject_regular_users` espera `403` en las dos rutas |
| La consola exige autenticación | `test_admin_routes_require_authentication` espera `401` sin cabecera |
| El perfil expone el privilegio sin filtrar nada más | `test_current_user_profile_exposes_superuser_flag` compara el objeto completo, incluida la marca de superusuario, y espera `401` en anónimo |
| El sondeo de infraestructura informa sin revelar configuración | `test_infrastructure_health_reports_dependencies` valida estados y que la respuesta no expone host, puerto ni DSN |
| Los filtros de pentests no se contradicen entre sí | `test_pentest_list_filters_by_status_mode_and_search` aísla estado, modo y búsqueda por separado |
| La búsqueda de vulnerabilidades cubre las tres dimensiones | `test_vulnerability_search_filters_title_and_target` prueba título, target y un término inexistente |
| Todo literal visible pasa por i18n | Auditoría sobre `frontend/src` y `frontend/src/locales` |

## Auditoría de internacionalización

- 312 claves `t(...)` usadas en el código, todas existentes en `es` y en `en`.
- Cero claves divergentes entre los diez namespaces y cero claves huérfanas.
- Cero literales de texto visible en JSX, incluidos `title`, `placeholder` y `aria-label`.
- Valores no textuales sí llevan texto técnico literal y son deliberados: los ejemplos de target del
  modal (`app.example.com`, `https://api.example.com/openapi.json`) son *placeholders* de formato que
  la validación del backend rechaza si no coinciden con el tipo de target.

## Adhesión a `design-dark.md`

- Los tintes del diff se derivan con `color-mix` de `--color-accent` y `--color-critical`. No hay
  ningún `rgba` de color nuevo en los estilos del bloque.
- La rampa de severidad (`#EF4444`, `#F97316`, `#F59E0B`, `#3B82F6`, `#8A8F8A`) seincorporó a
  `design-dark.md` como tokens y solo aparece en indicadores de severidad: swatches, contadores y
  anillos. Nunca como color de acción.
- Los badges de estado de remediación y de escaneo permanecen monocromos. Es una decisión, no una
  omisión: si el estado también fuera cromático, la misma celda comunicaría dos escalas a la vez.
- Un solo acento por pantalla: en `/pentests` es `+ Nuevo pentest` y el abortado usa el mismo acento
  porque es la única acción disponible mientras la ejecución está activa.

## Verificación

- Backend: `192 passed, 2 skipped`; `ruff check` sin hallazgos; `pyright --project
  backend/pyproject.toml` con 0 errores; `alembic check` sin drift sobre la base remota.
- Frontend: `typecheck` y `lint` sin errores ni advertencias; `build` con chunk principal de 429 kB
  (127 kB gzip) y chunk diferido de ECharts de 453 kB (153 kB gzip).
- En el backend en ejecución: las nueve rutas nuevas aparecen en `/openapi.json` y tanto
  `/api/v1/admin/organizations` como `/api/v1/pentests/` responden `401` sin token.
- `engines/`, `saas-boilerplate/` y `strix/` sin modificaciones.

## Límites y riesgos

- **Sin validación visual del shell autenticado:** el login automatizado en headless no es fiable en
  este entorno, de modo que `/issues`, `/pentests` y `/admin` no se han verificado con capturas. La
  revisión visual en navegador sigue siendo obligatoria antes de publicar.
- **Kanban sin arrastre:** las columnas se renderizan y filtran correctamente, pero mover una tarjeta
  entre ellas requiere `PATCH /api/v1/vulnerabilities/{id}`, que el backend no expone. La interfaz no
  simula la mutación: un arrastre que no se persiste sería peor que no tenerlo.
- **Terminal sin stdout:** la vista de ejecución muestra el ciclo de vida, el contenedor, el código de
  salida y el error, pero no las líneas de Strix porque la API no expone logs. La propia vista lo
  declara en pantalla en lugar de fingir un log.
- **`review_id` escrito a mano:** el endpoint de autofix exige el identificador de la revisión de PR
  y el panel lo pide en un campo de texto. La selección real exige el listado de revisiones de PR
  (§4.0), que todavía es placeholder.
- **Contacto comercial ficticio:** el modal Enterprise apunta a un `mailto:` de ejemplo. Antes de
  publicar hay que sustituirlo por el canal real de ventas.
- **Verificación de superusuario en cliente:** `StoredUser.is_superuser` viaja en `sessionStorage`. No
  es una frontera de seguridad, solo visibilidad de interfaz; el backend valida en cada petición.
