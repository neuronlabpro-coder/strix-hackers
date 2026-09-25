# Evidencia TDD Â· Fase 4 Â· Bloque 4.1

## Alcance

Shell, dashboard principal (Â§1.0 y Â§1.2 de `MENU-MAP.md`), gestor de repositorios (Â§6.1) y el endpoint de solo lectura que los alimenta.

Archivos nuevos de backend: `backend/apps/dashboard/{__init__,score,schemas,service,router}.py` y `backend/tests/test_dashboard_api.py`.
Archivos nuevos de frontend: `frontend/src/charts/{EChart.tsx,palette.ts}`, `frontend/src/features/dashboard/useDashboardSummary.ts`, `frontend/src/features/repositories/{RepositoriesPage,ConnectRepositoryModal,RepositoryReviewToggle}.tsx` y los namespaces `dashboard.json` y `repositories.json` en `es/` y `en/`.

## DecisiÃ³n de alcance

El dashboard exigÃa datos que el backend no exponÃa (no existÃa listado de revisiones de PR ni de ejecuciones por repositorio). Se acordÃ³ con el responsable aÃ±adir un endpoint de solo lectura en lugar de mostrar placeholders o disparar N+1 consultas desde el panel.

## RED

```text
uv run --project backend pytest -c backend/pyproject.toml backend/tests/test_dashboard_api.py -q
ModuleNotFoundError: No module named 'backend.apps.dashboard'
```

La misma iteraciÃ³n en verde detectÃ³ un fallo de modelo de datos: la primera consulta de hallazgos por repositorio devolviÃ³ `4` en lugar de `2` porque dos revisiones compartÃan `run_id`. Se corrigiÃ³ con `count(distinct(Vulnerability.id))` y se dejÃ³ el caso cubierto por la prueba.

## GREEN

```text
uv run --project backend pytest -c backend/pyproject.toml backend/tests -q
183 passed, 2 skipped

uv run --project backend ruff check backend
All checks passed!

uv run --project backend pyright --project backend/pyproject.toml
0 errors, 0 warnings, 0 informations

npm run typecheck
(sin salida: 0 errores)

npm run lint
(sin salida: 0 advertencias)

npm run build
dist/assets/index-*.js       366 kB â”‚ gzip: 114 kB
dist/assets/EChart-*.js      453 kB â”‚ gzip: 153 kB
```

## Correcciones posteriores (layouts y endpoints de servicio)

1. **RegresiÃ³n de layout compartida.** Un selector mÃºltiple introducido en este bloque declaraba `display: flex` sobre `.metric-grid`, `.chart-grid`, `.metric-card`, `.content-card`, `.auth-card`, `.empty-card` y `.table-wrapper`, sobrescribiendo los `display: grid` previos y comprimiendo cada tarjeta en una sola fila. Se detectÃ³ con capturas headless de `/login` y `/register`; se eliminÃ³ el selector y cada bloque volviÃ³ a declarar su propio modo de layout. AuditorÃa posterior: ningÃºn contenedor de bloque o grid queda forzado a `flex`.
2. **Tarjeta de autenticaciÃ³n.** Rehecha como tarjeta Ãºnica centrada (`max-width: 28rem`), fondo `#2A2A2A`, borde `rgba(138, 143, 138, 0.35)`, inputs `width: 100%` con `padding: 11px 12px` y `min-height: 44px`, botÃ³n primario a ancho completo en `#17a163` sobre `#1C1C1C` y selector de idioma en la esquina superior derecha (`position: absolute`) para que no empuje el formulario. Se aÃ±ade `overflow-wrap: break-word` y `min-width: 0` para que ningÃºn texto se rompa por palabra.
3. **Endpoints de servicio.** `GET /` devuelve `{"name": "Mind Guard Fenix Team API", "status": "online", "docs": "/docs"}` y `GET /health` devuelve `{"status": "healthy"}`, ambos sin autenticaciÃ³n, con esquemas Pydantic y tres pruebas en `backend/tests/test_service_endpoints.py`.

## Auditoría de diseño (skill `impeccable`)

Se auditó el panel con la skill `impeccable` tras crear `PRODUCT.md` y `DESIGN.md` (el proyecto no tenía contexto de diseño previo). Puntuación **15/20** con las correcciones ya aplicadas.

### Contraste medido sobre la paleta real

| Par | Ratio | Veredicto |
| --- | --- | --- |
| `#EDEDED` sobre `#1C1C1C` | 14.56:1 | AAA |
| `#EDEDED` sobre `#2A2A2A` | 12.26:1 | AAA |
| `#1C1C1C` sobre `#17A163` | 5.12:1 | AA |
| `#17A163` sobre `#1C1C1C` | 5.12:1 | AA |
| `#8A8F8A` sobre `#1C1C1C` | 5.17:1 | AA |
| `#8A8F8A` sobre `#2A2A2A` | **4.36:1** | **fallo AA** |
| `#EDEDED` al 70 % sobre `#2A2A2A` (`--color-caption`) | **6.77:1** | AA |

### Hallazgos corregidos

| Severidad | Hallazgo | Corrección |
| --- | --- | --- |
| P1 | El texto secundario pequeño sobre superficies (pies de KPI, cabeceras de tabla, rótulos de modal, badges apagados) quedaba en 4.36:1 | Token derivado `--color-caption` (`#B2B2B2`) aplicado solo donde el fondo es superficie |
| P1 | Bajo 760 px la navegación se ocultaba con `display: none`, dejando `/repositories`, `/issues`, `/knowledge` y `/settings` inalcanzables en móvil | Tira horizontal desplazable con las mismas secciones y el mismo estado activo |
| P1 | El modal de conexión no atrapaba el foco ni lo devolvía al cerrarse (WCAG 2.4.3) | Trampa de foco en `Tab` y `Shift+Tab`, `Escape` y restauración del elemento previo |
| P1 | `document.documentElement.lang` quedaba fijo en `es` aunque se cambiara a inglés | Se sincroniza en `languageChanged` y en la carga |
| P2 | El selector de idioma anunciaba "Cambiar a inglés" con texto visible "EN" (WCAG 2.5.3) | Nombre accesible `EN · Cambiar a inglés` |
| P2 | Los datos de los gráficos solo existían dentro del canvas | Listas `visually-hidden` con los conteos por severidad y la cifra del gauge |
| P2 | La paleta de ECharts duplicaba los hexadecimales en TypeScript, creando una segunda fuente de verdad | `readChartPalette()` lee las variables CSS en tiempo de ejecución |
| P2 | Franja de acento de 2 px en el enlace activo de navegación | Reducida a 1 px; el estado activo se marca con fondo y borde |
| P2 | La tarjeta de auth no tenía `min-width: 0` y el track del grid no era encogible | `minmax(0, 1fr)` con `justify-items: center` y `min-width: 0`, verificado a 430 px reales |
| P3 | Bordes y sombra con `rgba()` literales en vez de tokens | Registrados en `DESIGN.md`; pendiente convertirlos en tokens |

### Pendiente de decisión

- **Tipografías desde CDN de terceros.** `index.html` y `styles/index.css` cargan Inter y JetBrains Mono desde `fonts.googleapis.com`, en contra de la especificación de la Fase 4 ("autoalojada, sin CDN externo"), y filtran la IP del usuario a un tercero en cada carga. Recomiendo autoalojarlas con `fontsource`; queda a criterio del responsable porque cambia la política de activos del repositorio.
- **Verificación visual del shell autenticado.** Las pantallas de autenticación se verificaron con capturas reales (1280 px y 430 px); `/dashboard` y `/repositories` se auditaron por código y por contrato de datos, sin captura, por limitación del navegador headless del entorno.

## GarantÃas cubiertas

| GarantÃa | Evidencia |
| --- | --- |
| `security_score` determinista, monÃ³tono y acotado a 0-100 | `backend/tests/test_dashboard_api.py::test_security_score_is_deterministic_and_clamped` (8 casos, incluido el tope en 0) |
| Tenant vacÃo devuelve ceros, score 100 y distribuciÃ³n con las cinco severidades | `::test_dashboard_summary_of_empty_tenant` |
| KPIs reales: issues abiertos, total, fix rate, revisiones del mes y totales, pentests, repositorios monitorizados | `::test_dashboard_summary_aggregates_findings_reviews_and_repositories` |
| Hallazgos por repositorio sin duplicar reruns y `last_tested_at` correcto | mismo test (dos revisiones comparten `run_id`) |
| Estado derivado por repositorio: `NOT_TESTED`, `TESTED`, `SCANNING` | mismo test |
| Aislamiento multi-tenant del resumen | `::test_dashboard_summary_never_leaks_other_tenants` |
| `/authorize` responde `200` + `authorization_url` a clientes XHR y conserva el `302` de navegador | `backend/tests/test_git_oauth.py::test_authorize_returns_json_for_xhr_clients` y el test previo del `302` |
| Cero literales visibles en React | AuditorÃa de JSX sin nodos de texto ni atributos `aria-label`/`title`/`placeholder` literales |
| Toda clave `t(...)` existe en `es` y `en` y no hay claves huÃ©rfanas | Script de auditorÃa sobre `frontend/src` y `frontend/src/locales` |
| Paridad de namespaces es/en | `common` 26, `auth` 32, `navigation` 10, `errors` 7, `dashboard` 25, `repositories` 55 |
| Flujo real a travÃ©s del proxy de Vite (registro â†’ verificaciÃ³n â†’ login â†’ dashboard â†’ repositorios â†’ 409 sin credencial) | Smoke manual con `Invoke-RestMethod` contra `http://127.0.0.1:5173/api/...` |
| Layout de auth correcto y sin regresiÃ³n en tarjetas/ rejillas | Capturas headless de `/login` y `/register` + auditorÃa CSS de `display` por bloque |
| RaÃz y salud del servicio responden sin autenticaciÃ³n | `backend/tests/test_service_endpoints.py` |

## AdhesiÃ³n a `design-dark.md`

- Todos los estilos nuevos (tabla, badges, toggle, modal, tarjetas de grÃ¡fico) usan exclusivamente `#1C1C1C`, `#2A2A2A`, `#EDEDED`, `#8A8F8A` y `#17a163`, con `Inter` para interfaz y `JetBrains Mono` para hashes, ramas y puntuaciones. No hay degradados.
- El gauge usa `#17a163` como progreso y `#2A2A2A` como pista. La distribuciÃ³n por severidad usa `#EDEDED` con escalones de opacidad, para no introducir una paleta paralela de colores.
- Un solo botÃ³n con acento por pantalla: en `/repositories` es `+ AÃ±adir repositorio`; los toggles y el resto de acciones son neutros con borde.

## LÃmites y riesgos

- **Sin validaciÃ³n visual en navegador:** no hay navegador de escritorio conectado en el entorno, por lo que no se pudieron capturar pantallas ni verificar el render del canvas de ECharts. La verificaciÃ³n se limita a `typecheck`, `lint`, `build` y al smoke de API. La revisiÃ³n visual en navegador queda como paso manual obligatorio antes de publicar.
- **`Supply chain` sin dato real:** la columna existe con estado "Pendiente de SBOM" porque Strix todavÃa no expone SBOM; no se inventa el valor.
- **GuÃa `Get Set Up` (Â§1.1) no implementada:** 3 de sus 6 pasos dependen de endpoints inexistentes (knowledge, integraciones, miembros). Se deja para el bloque siguiente.
- **Sin acciones destructivas:** `DELETE /api/v1/repositories/{id}` existe en el backend pero el panel no expone todavÃa la desconexiÃ³n con confirmaciÃ³n.
- **Rendimiento del resumen:** una sola llamada con seis `SELECT` agregados; con volÃºmenes altos de vulnerabilidades conviene un Ãndice adicional o materializaciÃ³n.
- **FÃ³rmula del score:** los pesos (CRITICAL 5, HIGH 2, MEDIUM 1, LOW 0.25, INFO 0) estÃ¡n en cÃ³digo y cubiertos por pruebas, pero deberÃan pasar a configuraciÃ³n de negocio cuando exista la tabla de planes de la Fase 5.
