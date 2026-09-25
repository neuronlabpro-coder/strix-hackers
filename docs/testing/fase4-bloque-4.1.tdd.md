# Evidencia TDD · Fase 4 · Bloque 4.1

## Alcance

Shell, dashboard principal (§1.0 y §1.2 de `MENU-MAP.md`), gestor de repositorios (§6.1) y el endpoint de solo lectura que los alimenta.

Archivos nuevos de backend: `backend/apps/dashboard/{__init__,score,schemas,service,router}.py` y `backend/tests/test_dashboard_api.py`.
Archivos nuevos de frontend: `frontend/src/charts/{EChart.tsx,palette.ts}`, `frontend/src/features/dashboard/useDashboardSummary.ts`, `frontend/src/features/repositories/{RepositoriesPage,ConnectRepositoryModal,RepositoryReviewToggle}.tsx` y los namespaces `dashboard.json` y `repositories.json` en `es/` y `en/`.

## Decisión de alcance

El dashboard exigía datos que el backend no exponía (no existía listado de revisiones de PR ni de ejecuciones por repositorio). Se acordó con el responsable añadir un endpoint de solo lectura en lugar de mostrar placeholders o disparar N+1 consultas desde el panel.

## RED

```text
uv run --project backend pytest -c backend/pyproject.toml backend/tests/test_dashboard_api.py -q
ModuleNotFoundError: No module named 'backend.apps.dashboard'
```

La misma iteración en verde detectó un fallo de modelo de datos: la primera consulta de hallazgos por repositorio devolvió `4` en lugar de `2` porque dos revisiones compartían `run_id`. Se corrigió con `count(distinct(Vulnerability.id))` y se dejó el caso cubierto por la prueba.

## GREEN

```text
uv run --project backend pytest -c backend/pyproject.toml backend/tests -q
180 passed, 2 skipped

uv run --project backend ruff check backend
All checks passed!

uv run --project backend pyright --project backend/pyproject.toml
0 errors, 0 warnings, 0 informations

npm run typecheck
(sin salida: 0 errores)

npm run lint
(sin salida: 0 advertencias)

npm run build
dist/assets/index-*.js       366 kB │ gzip: 114 kB
dist/assets/EChart-*.js      453 kB │ gzip: 153 kB
```

## Garantías cubiertas

| Garantía | Evidencia |
| --- | --- |
| `security_score` determinista, monótono y acotado a 0-100 | `backend/tests/test_dashboard_api.py::test_security_score_is_deterministic_and_clamped` (8 casos, incluido el tope en 0) |
| Tenant vacío devuelve ceros, score 100 y distribución con las cinco severidades | `::test_dashboard_summary_of_empty_tenant` |
| KPIs reales: issues abiertos, total, fix rate, revisiones del mes y totales, pentests, repositorios monitorizados | `::test_dashboard_summary_aggregates_findings_reviews_and_repositories` |
| Hallazgos por repositorio sin duplicar reruns y `last_tested_at` correcto | mismo test (dos revisiones comparten `run_id`) |
| Estado derivado por repositorio: `NOT_TESTED`, `TESTED`, `SCANNING` | mismo test |
| Aislamiento multi-tenant del resumen | `::test_dashboard_summary_never_leaks_other_tenants` |
| `/authorize` responde `200` + `authorization_url` a clientes XHR y conserva el `302` de navegador | `backend/tests/test_git_oauth.py::test_authorize_returns_json_for_xhr_clients` y el test previo del `302` |
| Cero literales visibles en React | Auditoría de JSX sin nodos de texto ni atributos `aria-label`/`title`/`placeholder` literales |
| Toda clave `t(...)` existe en `es` y `en` y no hay claves huérfanas | Script de auditoría sobre `frontend/src` y `frontend/src/locales` |
| Paridad de namespaces es/en | `common` 26, `auth` 32, `navigation` 10, `errors` 7, `dashboard` 25, `repositories` 55 |
| Flujo real a través del proxy de Vite (registro → verificación → login → dashboard → repositorios → 409 sin credencial) | Smoke manual con `Invoke-RestMethod` contra `http://127.0.0.1:5173/api/...` |

## Adhesión a `design-dark.md`

- Todos los estilos nuevos (tabla, badges, toggle, modal, tarjetas de gráfico) usan exclusivamente `#1C1C1C`, `#2A2A2A`, `#EDEDED`, `#8A8F8A` y `#17a163`, con `Inter` para interfaz y `JetBrains Mono` para hashes, ramas y puntuaciones. No hay degradados.
- El gauge usa `#17a163` como progreso y `#2A2A2A` como pista. La distribución por severidad usa `#EDEDED` con escalones de opacidad, para no introducir una paleta paralela de colores.
- Un solo botón con acento por pantalla: en `/repositories` es `+ Añadir repositorio`; los toggles y el resto de acciones son neutros con borde.

## Límites y riesgos

- **Sin validación visual en navegador:** no hay navegador de escritorio conectado en el entorno, por lo que no se pudieron capturar pantallas ni verificar el render del canvas de ECharts. La verificación se limita a `typecheck`, `lint`, `build` y al smoke de API. La revisión visual en navegador queda como paso manual obligatorio antes de publicar.
- **`Supply chain` sin dato real:** la columna existe con estado "Pendiente de SBOM" porque Strix todavía no expone SBOM; no se inventa el valor.
- **Guía `Get Set Up` (§1.1) no implementada:** 3 de sus 6 pasos dependen de endpoints inexistentes (knowledge, integraciones, miembros). Se deja para el bloque siguiente.
- **Sin acciones destructivas:** `DELETE /api/v1/repositories/{id}` existe en el backend pero el panel no expone todavía la desconexión con confirmación.
- **Rendimiento del resumen:** una sola llamada con seis `SELECT` agregados; con volúmenes altos de vulnerabilidades conviene un índice adicional o materialización.
- **Fórmula del score:** los pesos (CRITICAL 5, HIGH 2, MEDIUM 1, LOW 0.25, INFO 0) están en código y cubiertos por pruebas, pero deberían pasar a configuración de negocio cuando exista la tabla de planes de la Fase 5.
