# MEMORY — Mind Guard Fenix Team

> Estado al cerrar la sesión del **3 de octubre de 2026**. Lee esto primero en una sesión nueva.

## Cómo trabajar aquí

- **Idioma: todo en español.** Comentarios y docstrings con tildes. **Los valores de los JSON de
  traducción van SIN tildes** ("Cargando precios..."), por decisión previa del proyecto.
- **Nada commiteado ni push.** El humano hace los commits.
- **Nada se borra sin permiso del humano.** Si algo sobra, se deja con un comentario explicando
  por qué.
- **Reglas del proyecto en `AGENTS.md`**: R1 nada hardcodeado + i18n estricto, R2 `engines/` solo
  lectura, R3 aislamiento multi-tenant, R4 inmutabilidad, R5 cero datos, R6 datos remotos solo por
  Tailscale.
- **Agentes y reglas del proyecto en `.agents/`** (react-reviewer, typescript-reviewer,
  fastapi-reviewer, database-reviewer, silent-failure-hunter, y `.agents/rules/{react,typescript,
  python,web}/*`). Los subagentes no los invocan por nombre: hay que decirles que los **lean**.
- Skills en `.opencode/skills/` (espejo de `.agents/skills/`).

## Verificación obligatoria

```powershell
$env:DB_CONNECT_TIMEOUT_SECONDS = "60"
uv run --project backend python scripts/ci_check.py     # 10 gates, ~22 min. Debe dar 10/10.
```

Comprobadores rápidos del frontend (desde `frontend/`):

```
python _css_brace_check.py        # llaves CSS cuadradas
python _css_comment_check.py      # comentarios CSS sin abrir
python _css_class_check.py        # toda clase usada existe en el CSS
python _i18n_check.py             # claves literales (solo las 9 pantallas de su lista)
python _que_claves_faltan.py <rutas.tsx>   # claves de cualquier fichero, por namespace real
python _det_bloque.py             # cabeceras de comentario "## Por qué" sin párrafo -> debe dar 0
```

Scripts de medición en navegador real (requieren `uv run --with playwright`):

```
scripts/_capturas_console.py      # 40 capturas; recorre pestañas. MÍRALAS, no basta con que corra.
scripts/_auditar_pegados.py       # 30 pantallas + 9 pestañas. Debe dar cero pegados.
frontend/_alinear_filtros.py <ruta> ".filter-bar"    # compara CENTROS, no bordes superiores
frontend/_medir_ancho.py <ruta> <selector> "<texto>" # texto partido o recortado
scripts/_informe_tabla.py <ruta> <selector tabla>    # celdas, desbordes, fondos
scripts/_capturar_escaneo.py      # entra por la lista a un detalle y mide la cronología
```

**Login demo**: `demo.admin@acmesecurity.io` / `DemoFenix2026!Empa`. Org
`8aef9544-3fe6-5fd9-890a-4c7e948a25e3`.

## Entorno

- Docker 29.8.0 **funciona** por el contexto `default` (`npipe:////./pipe/docker_engine`).
  Antes fallaba por `desktop-linux`; ya no.
- Para levantar: backend `$env:DB_CONNECT_TIMEOUT_SECONDS="60"; uv run --project backend python
  -m uvicorn backend.main:app --host 127.0.0.1 --port 8000`, y `npm run dev` en `frontend/`.
  **Los procesos en background mueren al cerrar el turno**: hay que relanzarlos y comprobar.
- El uvicorn **no tiene `--reload`**: tras tocar backend hay que reiniciarlo o el panel sirve
  código viejo.

## Lo que está HECHO y verificado (10/10 gates)

**Filtros completos** (patrón cerrado en CVE):
- `frontend/src/features/shared/Pagination.tsx` — componente compartido. Props `{total, limit,
  offset, onOffsetChange, namespace?}`. **No** reutilizar `features/admin/PaginationBar`: recibe un
  tipo de la consola e importarlo desde el panel rompe el aislamiento de navegación.
- **CVE**: buscador, severidad, año, KEV, paginación. `useCveCatalog.ts` con `offset` como estado
  **propio** (no un campo del filtro).
- **PR Reviews**: `query`, `created_from`, `created_to` en `backend/apps/repositories/router.py`
  (`list_pr_reviews`), 6 tests demostrados no vacíos.
- **Support Tickets**: `query` + rango. **Domains**: ahora **paginado** (`limit`/`offset` en la
  respuesta, que antes no existían).
- **Knowledge Documents, Asset Discovery, Admin Sales**: buscador + rango + limpiar.
- **Admin Users**: botón de limpiar (el buscador ya existía).

**Defectos de UI corregidos**, todos diagnosticados midiendo en el navegador:
- Etiqueta pegada al texto: clase nueva `.cell-inline` (flex, `gap: 8px`, `nowrap`).
- **Cronología del escaneo**: etiqueta y hora en columnas. Era un `<span>` sin estilo.
- **Icono de KEV** que caía a la línea siguiente conservando su `margin-left`.
- **Casilla «Solo KEV»** desalineada: `.filter-field-check` con `padding-top: 28px` (la altura real
  de la fila de etiqueta) y `min-height: 64px` para centrar en la fila de control.
- **Buscador de CVE** que cortaba el placeholder a media palabra: `min-width: 330px` **medido**
  (con 300 el input solo recibía 254 px y el placeholder necesita 274).
- **Pestaña Contenedores** de Operaciones: era un muñón que pintaba un vacío fijo con un contador
  que no le correspondía. Cableada a `GET /admin/operations/containers`.
- **Carrera de `useAsyncResource`**: la rama de carga debe ser `if (isLoading)` **a secas**, nunca
  `if (isLoading && data === null)`. Con la segunda forma se pintan las filas de la sección
  anterior. Bug introducido por mí,سبب de un aviso de React atribuido a `TablaContenedores`.
- **10 iconos de estado vacío** unificados a `.empty-card-mark`.
- **14 textos rotos** del catálogo técnico (`catalog.*` no existía en ningún idioma).
- **2 etiquetas `aria`** de coste que `LlmModelsPage` pedía en el namespace `llm` y solo estaban
  en `admin`.
- **`LineChart` registrada** en `EChart.tsx` (no estaba importada; por eso no había línea apilada).
- **273 cabeceras «## Por qué» vacías** en `index.css` reescritas con explicación verificada.

**Strix**: 53 tests de superficie de seguridad en verde
(`test_runner_docker_surface.py`, `test_runner_egress_fence.py`, `test_docker_sandbox.py`,
`test_runner_llm_key_exposure.py`). Los HIGH antiguos (`CAIDO_PORT`, `ipam_config`) **ya no
aplican**: el sandbox se crea sin `ports` publicados, con red dedicada.

## Defectos REALES encontrados y arreglado por los subagentes

1. **LIKE sin escapar** en varios endpoints: `?search=%` devolvía la tabla entera y
   `?search=web_app` traía `webXapp`. Corregido con `escape_like`.
2. **Tickets a partir del 26 invisibles**: el cliente no mandaba `limit`, el backend topeaba a 25, y
   no había ni barra ni aviso.
3. **`getAdminAuditLog` ignoraba `limit` y `offset`**: el botón «Siguiente» pintaba **las mismas
   50 filas** con el resumen «51–100 de 210». Invisible porque `total` venía bien.
4. **`total` inflado por producto cartesiano** en `list_assets` (faltaba `JOIN` en el recuento):
   13 en vez de 8.
5. **La vista de documentos reventaba con un solo documento**: leía `content` del listado, que no
   lo devuelve, y hacía `.replace()` sobre `undefined`. Pantalla en blanco. Nadie lo había visto
   porque el workspace de demostración no tenía ninguno.

## PENDIENTE

**Gráficas: sin empezar, es el encargo entero.** `LineChart` ya está registrada. Falta decidir
qué series aportan información y **exponer datos agregados por periodo**, que la API no da.
Alternativa barata: series por estado/severidad sin series temporales.

**16 rótulos en español que redacté YO** tras un `git checkout --` que me llevó trabajo sin
commitear de sesiones anteriores. Están en `AdminTenantPricingPage` (9) y en el histórico de
precios. **El tono es mío, no del humano: hay que revisarlos antes de vender.**

**Consolidar `escape_like`**: hay **cuatro copias** en el árbol.
`backend/core/filtros_texto.py` es el módulo compartido nuevo (lo escribió un subagente y otro lo
amplió). Los de `repositories/router.py` y `cve_database/service.py` se quedan donde están.

**`scripts/ci_check.py` se rompe al imprimir un gate rojo**: `UnicodeEncodeError` cp1252 contra el
`✓` de vitest. Preexistente. **Es grave**: cuando algo falla no ves cuál, solo que algo falló.
Esquivar con `PYTHONIOENCODING=utf-8`.

**`GET /api/v1/assets/domains` rompe a un cliente viejo** que use la respuesta sin `limit`/`offset`
(sale `NaN–NaN`). No se puso `Number.isFinite` en `Pagination` a propósito: convertiría un bug real
del backend en «no se pinta nada». Decisión pendiente del humano.

**Nomenclatura inconsistente**: el filtro se llama `query` en endpoints de cliente y `search` en los
de consola. No se unificó porque renombrar un parámetro de consulta rompe al llamador sin avisar.
Documentado en ambos ficheros.

**Contradicciones de CSS documentadas y sin arreglar** (decisiones de diseño, no técnicas):
- `.filter-field label` tiene `margin-bottom: 0` anulado por otra regla más abajo con la misma
  especificidad: el hueco real es 12 px, no 6.
- `.priority-badge` nunca se ve como círculo: `.margin-field input` le gana en 7 propiedades. Su
  comentario describe un círculo de 24 px que no se renderiza.
- `.margin-field select` y `.margin-field input` son la misma regla escrita dos veces.
- Casilla «Solo superusuarios» de Admin Users desalineada −29 px (preexistente, lo comprobó con
  `git stash`).

**`_i18n_dinamico_check.py` sale en rojo** con 5 prefijos ausentes en es y en (`create.types`,
`infrastructure.destinations`, `issues.destinations`, `notifications.destinations`,
`scopes.actions`). Preexistente, confirmado con stash. No está en `ci_check.py`.

**404 del favicon**: única respuesta ≥400 en el barrido de 40 capturas, sin localizar en qué
pantalla.

## Advertencias para la sesión siguiente

- Los subagentes trabajan en el **mismo árbol**: sepáralos **por ficheros**, no por puertos, y
  **diles que no arranquen servidores**. Un conflicto de puertos produce errores de red que parecen
  fallos de código.
- **Los 10/10 de cada subagente son del árbol en ese momento**, no del estado final combinado.
  Relanza `ci_check.py` al final.
- Todo test nuevo **se demuestra no vacío** (se muta el defecto y se ve caer). Un test verde que
  nunca ha fallado no demuestra nada. El subagente B borró un test de ordenación que seguía verde
  sin comprobar lo que decía, en vez de dejarlo: buen ejemplo.
- Las capturas son **la única prueba** de que una pantalla está bien. Los gates pasan con el texto
  cortado.
- No ejecutar `checkout`, `restore` ni `reset` sobre ficheros modificados sin preguntar.
- Respaldo del trabajo en `%TEMP%\fenix_respaldo_20261002_135715`.
