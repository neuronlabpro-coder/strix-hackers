# FASE 4 — Panel Web Frontend, Gestión de Vulnerabilidades & Knowledge Base

> **Documento de especificación ejecutable.** Define el desarrollo visual completo del panel de usuario en React 19, TypeScript y Tailwind CSS bajo los tokens estrictos de `design-dark.md` (Dark Emerald), la integración de Apache ECharts, el gestor de vulnerabilidades en modo Lista/Kanban, el visor y reproductor de Pruebas de Concepto (PoC), el visualizador de parches *autofix*, la verificación de dominios y la base de conocimiento de la aplicación.
>
> **Estado:** `[ ]` En ejecución — Bloque 4.1 (shell, dashboard y repositorios) implementado
> **Dependencias previas:** Fase 1 (Shell UI base, Auth, Multi-tenancy), Fase 2 (Modelos y persistencia de PentestRuns y Vulnerabilities) y Fase 3 (Repositorios Git y automatización de PRs).  
> **Autoridades que rigen esta fase:** `ARCHITECTURE.md` (§8), `MENU-MAP.md` (§1, §2, §3, §5, §6.2, §7), `design-dark.md` y `AGENTS.md` (Reglas de Oro R1, R3 y R4).

---

## 1. Objetivo de la Fase

Implementar en el frontend (`frontend/src/`) todas las pantallas operativas principales del producto con estética técnica de alta densidad, eliminando cualquier texto hardcodeado mediante internacionalización estricta (`i18next`).

El usuario debe poder visualizar sus métricas de seguridad en el dashboard principal, lanzar y monitorizar pentests en vivo, gestionar incidencias en formato Lista o tablero Kanban, examinar los comandos y scripts de reproducción de las **Pruebas de Concepto (PoC)**, inspeccionar el diff unificado del **Autofix**, validar la propiedad de dominios antes de escanearlos, inyectar contexto de arquitectura en la base de conocimiento y operar la consola interactiva de chat con agentes.

---

## 2. Decisiones de Diseño y UI (`design-dark.md`)

1. **Tokens de Color Canónicos:**
   * **Base / Fondo:** `#1C1C1C` (Neutral). Nada de grises azulados o fondos blancos.
   * **Superficies / Cards / Modales:** `#2A2A2A` (Surface).
   * **Texto Primario / Títulos:** `#EDEDED` (Primary).
   * **Texto Secundario / Metadatos / Bordes:** `#8A8F8A` (Secondary).
   * **Acento Único (Tertiary):** `#17a163` (Emerald). Reservado estrictamente para la acción principal por pantalla (`+ New Pentest`, `Run Autofix`, `Verify Domain`).
   * **Prohibición de Degradados:** El sistema es plano por diseño (*flat design*).
2. **Tipografía Técnica:**
   * **Display / Headings / Interfaz:** `Inter` (autoalojada, sin CDN externo).
   * **Datos Técnicos y Monospace:** `JetBrains Mono` a `0.72rem` (hashes de commit, comandos PoC, rutas de archivos, CVEs, puntuaciones CVSS y badges de severidad).
3. **Badges de Severidad (Alto Contraste):**
   * `Critical`: Fondo `rgba(239, 68, 68, 0.15)` · Texto `#EF4444` · Borde `rgba(239, 68, 68, 0.4)`.
   * `High`: Fondo `rgba(249, 115, 22, 0.15)` · Texto `#F97316` · Borde `rgba(249, 115, 22, 0.4)`.
   * `Medium`: Fondo `rgba(234, 179, 8, 0.15)` · Texto `#EAB308` · Borde `rgba(234, 179, 8, 0.4)`.
   * `Low`: Fondo `rgba(59, 130, 246, 0.15)` · Texto `#3B82F6` · Borde `rgba(59, 130, 246, 0.4)`.
4. **Gráficas ECharts como Biblioteca Exclusiva:**
   * Todos los gráficos (postura, evolución temporal, distribución de incidencias) se implementan mediante `echarts-for-react` con opciones tematizadas oscuras, sin cargar bibliotecas redundantes (Chart.js, Recharts, etc.).

> **Resolución de contradicciones de diseño (Bloque 4.1, 2026-09-25).** `design-dark.md` es la autoridad visual y este mismo documento exige `AGENTS.md` R1 (i18n y cero literales), pero propone colores de severidad que no existen en la ficha de diseño. Como las reglas de construcción y la ficha de diseño rigen sobre las propuestas de este bloque, el Bloque 4.1 aplica las siguientes decisiones, todas reversibles si producto decide otra cosa:
> 1. **Severidad sin paleta paralela:** la distribución por severidad y los badges usan `#EDEDED` con escalones de opacidad (`CRITICAL` 100 %, `HIGH` 72 %, `MEDIUM` 52 %, `LOW` 34 %, `INFO` 18 %). Se respeta el acento único `#17a163` y la prohibición de degradados. Si se adoptan los colores del §2.3, deben incorporarse primero a `design-dark.md`.
>
>    **Actualización (Bloque 4.2, 2026-09-25):** producto autorizó expresamente la rampa cromática de severidad para visualización de datos, y se ha incorporado a `design-dark.md` como tokens `--color-critical` (`#EF4444`), `--color-high` (`#F97316`), `--color-medium` (`#F59E0B`), `--color-low` (`#3B82F6`) e `--color-info` (`#8A8F8A`). El acento único `#17a163` sigue reservado para la acción principal de cada pantalla; la rampa solo aparece en indicadores de severidad (swatches, anillos y distribución). El badge de *estado de remediación* permanece monocromo a propósito: el color ya comunica la severidad y volver a colorear el estado haría competir dos escalas en la misma celda.
> 2. **ECharts nativo en lugar de `echarts-for-react`:** se importa `echarts/core` con solo `GaugeChart`, `PieChart`, `LegendComponent`, `TooltipComponent` y `CanvasRenderer`, y se carga con `React.lazy` en su propio chunk (153 kB gzip) para que el shell y el login no lo descarguen. Sigue siendo Apache ECharts como biblioteca exclusiva.
> 3. **Iconos de marca:** `lucide-react` ya no incluye logotipos de GitHub/GitLab. La columna Proveedor usa un icono genérico más el identificador del proveedor en `JetBrains Mono`, coherente con la estética code-first.

---

## 3. Desglose de Tareas de Implementación

### Tarea 4.1 · Dashboard Principal (`frontend/src/features/dashboard/`)
1. **Componente de KPIs (`SecurityMetrics.tsx`):**
   * `Security Score`: Componente semicircular o gauge de ECharts con cálculo dinámico (100 - penalizaciones por fallos críticos/altos).
   * Contadores: `Open Issues`, `Issues Found`, `Fix Rate` (%), `PRs Reviewed` y `Pentests`.
2. **Guía de Configuración Inicial (`OnboardingGuide.tsx`):**
   * Barra de progreso reactiva: `X of 6 complete`.
   * Pasos con enlace directo:
     * 1. Conectar repositorios (`/repositories`).
     * 2. Lanzar primer pentest (Abre modal `+ New Pentest`).
     * 3. Añadir contexto de negocio (`/knowledge`).
     * 4. Revisar primer Pull Request (`/pr-reviews`).
     * 5. Configurar alertas e integraciones (`/integrations`).
     * 6. Invitar colaboradores (`/settings/members`).
3. **Widget de Repositorios Conectados (`ConnectedRepositoriesWidget.tsx`):**
   * Tabla compacta con iconos de GitHub/GitLab, nombre del repositorio, badge `Reviews on` / `Off` con toggle interactivo y enlace `View all (N)` hacia `/repositories`.

---

### Tarea 4.1-b · Bloque 4.1 · Shell, Dashboard y Gestión de Repositorios

> **Estado:** `[x]` Implementado y verificado (`frontend/` con `typecheck`, `lint` y `build` limpios; `backend/tests` con `180 passed, 2 skipped`).

1. **Contrato de datos (`GET /api/v1/dashboard/summary`):** endpoint de solo lectura en `backend/apps/dashboard/` que entrega `security_score`, `open_issues`, `total_issues`, `fix_rate`, `prs_reviewed` (30 días), `prs_reviewed_total`, `pentests_total`, `repositories_monitored`, `severity_distribution`, `repositories[]` (con `status`, `open_vulnerabilities` y `last_tested_at`) y `generated_at`. Todas las consultas filtran por `organization_id` (R3) y el cálculo del score vive en `score.py` con pesos documentados y pruebas unitarias.
2. **Negociación de contenido en `/authorize`:** el endpoint OAuth de GitHub/GitLab exige cabecera `Authorization`, inalcanzable en una navegación normal; con `Accept: application/json` responde `200` con `authorization_url` para el panel, y conserva el `302` para el flujo de navegador.
3. **Dashboard (`frontend/src/features/dashboard/`):** cuatro tarjetas KPI, gauge de postura y anillo de distribución por severidad con Apache ECharts (chunk diferido), widget de repositorios con toggle real contra `PATCH /api/v1/repositories/{id}` y enlace a `/repositories`. Estados de carga, error y reintento.
4. **Repositorios (`frontend/src/features/repositories/`):** tabla de MENU-MAP §6.1 (Proveedor, Repositorio, Estado, Vulnerabilidades abiertas, PR reviews, Supply chain, Last tested), buscador, estado vacío y modal de conexión con dos pasos: OAuth GitHub/GitLab e inventario remoto con importación directa desde `GET /api/v1/repositories/remote`.
5. **i18n:** namespaces `dashboard` y `repositories` en `es`/`en` con paridad verificada; el bloque antiguo `common:dashboard` se eliminó para evitar claves duplicadas.
6. **Pendiente de este bloque:** guía `Get Set Up` (§1.1, Tarea 4.1.2) y la columna Supply chain con datos reales de SBOM; ambas requieren endpoints que aún no existen.

---

### Tarea 4.2-b · Bloque 4.2 · Issues, Pentests, Secciones Enterprise y Base de SuperAdmin

> **Estado:** `[x]` Implementado y verificado (`frontend/` con `typecheck`, `lint` y `build` limpios; `backend/tests` con `192 passed, 2 skipped`, `ruff` y `pyright` sin hallazgos y `alembic check` sin drift).

1. **Contratos de backend añadidos:**
   * `GET /api/v1/pentests/` devuelve página de ejecuciones (`items`, `total`, `limit`, `offset`) con filtros por `status`, `target_type`, `scan_mode` y `search`, y agrega el conteo de hallazgos por run excluyendo los `IGNORED`. Es la primera lectura de histórico de escaneos de la plataforma, antes inexistente.
   * `GET /api/v1/vulnerabilities/` acepta `search` con coincidencia parcial sobre título, target y CVE.
   * `GET /api/v1/auth/me` expone el perfil autenticado con `is_superuser`, de modo que el cliente decide la visibilidad del enlace de SuperAdmin sin adivinar.
   * `GET /api/v1/admin/organizations` y `GET /api/v1/admin/health` bajo `require_superuser`. El sondeo ejecuta `SELECT 1` y `PING` y devuelve `healthy`/`degraded` con la latencia observada, sin exponer credenciales, hosts ni puertos (R3).
2. **Gestor de vulnerabilidades (`frontend/src/features/issues/`):** `IssuesPage` con contadores de severidad que filtran al pulsarse, conmutador Tabla/Tablero segmentado, buscador, filtros de severidad, estado y target, y paginación conectada a la API. El tablero agrupa por `OPEN`, `IN_PROGRESS`, `FIXED`, `SNOOZED` e `IGNORED`. `VulnerabilityDetailPage` presenta metadatos (CVE, CVSS, target, línea, fechas), el visor de PoC inmutable con copiado, el diff de autofix y el botón de creación de Pull Request contra `POST /api/v1/vulnerabilities/{id}/create-fix-pr`.
3. **Gestor de pentests (`frontend/src/features/pentests/`):** tabla histórica con ID corto, target, tipo, modo, estado (con pulso animado en `RUNNING`), hallazgos y fecha. `NewPentestModal` elige repositorio importado o target manual, valida el formato según el tipo y lanza el escaneo con confirmación esmeralda. `PentestRunPage` sondea cada 5 s mientras la ejecución está activa, muestra cronología, contenedor, código de salida, duración y una terminal monoespaciada, y permite abortar.
4. **Secciones Enterprise:** `EnterpriseGateModal` con trampa de foco, restauración del foco previo y cierre con `Escape`; el Sidebar expone `Networks`, `Containers` y `Supply Chain` con etiqueta de candado. Ninguna navega: la capacidad aún no existe y ofrecer un destino vacío sería mentir.
5. **Consola de SuperAdmin:** `RequireSuperuser` falla cerrado (sin sesión → `/login`; sin privilegio → `/dashboard`) y `AdminPage` lista organizaciones con plan y saldo, más el estado de PostgreSQL y Redis. El enlace del Sidebar solo aparece para superusuarios.
6. **i18n:** namespaces `issues`, `pentests`, `admin` y `enterprise` en `es`/`en`, con 312 claves usadas verificadas contra ambos idiomas y sin claves huérfanas.
7. **Deuda que deja el bloque:** el arrastre de tarjetas entre columnas del Kanban requiere `PATCH /api/v1/vulnerabilities/{id}`, que el backend aún no expone; la terminal muestra el ciclo de vida de la ejecución porque el stdout de Strix todavía no se sirve por la API. Ambas quedan anotadas como pendientes de backend, no como funcionalidad terminada.

---

### Tarea 4.2 · Gestor de Pentests (`frontend/src/features/pentests/`)
1. **Listado de Pentests (`PentestListPage.tsx`):**
   * Pestañas: `Pentests` y `Schedules` (programaciones periódicas).
   * Barra de herramientas: Input de búsqueda rápida, selector de estado (`All statuses`, `Queued`, `Running`, `Completed`, `Failed`), selector de tipo (`All types`) y filtro de fechas.
   * Tabla de ejecuciones con badge animado en `RUNNING`.
   * Estado vacío interactivo cuando no hay escaneos (`No pentests yet` + botón `+ New pentest`).
2. **Modal Interactivo `+ New Pentest` (`NewPentestModal.tsx`):**
   * Selector tipo radio: `Repository` vs `Domain / API Host`.
   * Si es repositorio: selector desplegable de repositorios conectados y selector de rama (`branch`).
   * Si es URL externa: input de dominio (con validación de que esté en estado verificado) y selector opcional de archivo OpenAPI/Swagger (`.json`, `.yaml`).
   * Selector de modo: `Quick` (análisis rápido), `Standard` (balanceado), `Deep` (auditoría profunda).
   * Advertencia de créditos estimada antes de confirmar.
   * Botón de confirmación con estilo Tertiary `#17a163`.
3. **Vista de Ejecución en Vivo (`PentestRunDetailPage.tsx`):**
   * Cabecera con target analizado, modo, estado y duración.
   * Terminal de logs en tiempo real (`ExecutionLogsViewer.tsx`): tipografía `JetBrains Mono`, fondo `#121212`, scroll automático y botón para descargar log completo.
   * Grafo o línea de estado de agentes activos: `Reconnaissance` ──▶ `Exploitation` ──▶ `Validation (PoC)` ──▶ `Autofix`.
   * Botón crítico `Abort Run` que lanza modal de confirmación y llama al endpoint de cancelación.

---

### Tarea 4.3 · Gestor de Vulnerabilidades (`frontend/src/features/issues/`)
1. **Cabecera y Contadores por Severidad (`IssueCountersBar.tsx`):**
   * 4 contadores de alto impacto visual: `Critical` (Rojo), `High` (Naranja), `Medium` (Amarillo), `Low` (Azul).
2. **Barra de Pestañas y Filtros:**
   * Pestañas de estado: `All`, `Open`, `In progress`, `Snoozed`, `Fixed`, `Ignored`.
   * Selector `Mode`: Botones para conmutar entre vista **Lista** (`List`) y tablero **Kanban** (`Board`).
   * Búsqueda por texto y filtros por repositorio y severidad.
3. **Tablero Kanban (`IssueKanbanBoard.tsx`):**
   * 4 columnas de estado: `Open`, `In Progress`, `Fixed`, `Snoozed`.
   * Tarjetas drag-and-drop o con menú contextual para mover de columna, actualizando el estado de la incidencia en el backend.
4. **Ficha de Detalle de Vulnerabilidad (`IssueDetailPage.tsx`):**
   * Encabezado con título, severidad, CVSS Score, CVE y vector OWASP.
   * Descripción técnica detallada del impacto.
   * **Sección Prueba de Concepto (`PoCViewer.tsx`):**
     * Bloque de código con comando `curl` o script Python generado por Strix.
     * Botón de copiado en un clic con feedback visual (`Copied!`).
   * **Sección Autofix (`AutofixDiffViewer.tsx`):**
     * Visualizador de diff unificado estilo Git (fondo `#181818`, líneas añadidas en verde tenue, eliminadas en rojo tenue).
     * Botón `Create Pull Request with Fix`: dispara la creación de la rama y apertura del PR en el proveedor Git correspondiente.
   * Botón `Re-test Vulnerability`: encola un escaneo focalizado contra el endpoint vulnerable.

---

### Tarea 4.4 · Dominios y Verificación de Propiedad (`frontend/src/features/domains/`)
1. **Listado `/domains`:**
   * Tabla con `Domain`, `Verification` (Badge verde `Verified` o amarillo `Pending`), `Login` (Credenciales configuradas), `Issues` y `Last tested`.
2. **Modal de Alta y Verificación (`AddDomainModal.tsx`):**
   * Formulario de URL base o hostname.
   * Pantalla de desafío criptográfico para verificar titularidad:
     * **Método 1 (DNS):** Crear registro TXT con nombre `_strix-challenge.tudominio.com` y valor generado `strix-verify=<token>`.
     * **Método 2 (HTTP):** Alojar archivo accesible en `https://tudominio.com/.well-known/strix-challenge.txt`.
   * Botón `Verify DNS / HTTP`: consulta el backend y valida la existencia del desafío. Si falla, muestra instrucciones claras y botón de reintento.

---

### Tarea 4.5 · Base de Conocimiento (`frontend/src/features/knowledge/`)
1. **Pestañas:** `Custom` (reglas manuales), `Internal` (aprendidas por la plataforma) y `Connected sources` (vía MCP).
2. **Formulario de Alta (`AddKnowledgeModal.tsx`):**
   * Input de título.
   * Selector de ámbito (`Scope`): `Global` o repositorio específico.
   * Selector de tipo: `Business Logic Flaw`, `Critical Asset`, `Testing Rule`, `Accepted Risk`.
   * Editor Markdown para documentar flujos especiales de autenticación o arquitectura.
3. **Tabla de Reglas:** Listado con buscador y filtros por ámbito.

---

### Tarea 4.6 · Consola de Chat con Agentes (`frontend/src/features/chat/`)
1. **Shell de Conversación (`AgentChatPage.tsx`):**
   * Banner superior: *"A chat uses part of a credit for each agent step. Your balance is in Billing"*.
   * Historial de mensajes con formato Markdown, renderizado de bloques de código y trazas de herramientas invocadas por el agente.
2. **Barra de Entrada de Prompts:**
   * Textarea expandible con atajo `Enter` para enviar.
   * Botones de inyección de contexto:
     * `Credentials`: Modal para adjuntar usuario/contraseña o Bearer tokens temporales.
     * `Scope domains`: Selector de dominios autorizados para la prueba.
     * `Add repositories`: Selector de código de referencia.
3. **Tarjetas de Acceso Rápido (Quick Prompts):**
   * Pestañas: `Web`, `Code`, `Cloud`, `Recon`, `Network`, `Threat Intel`, `Compliance`.
   * Cards clickeables que inyectan prompts preconfigurados (ej. *Test API authorization*, *Analyze OAuth flows*, *Detect SSRF vectors*).

---

### Tarea 4.7 · Envoltorio Temático de Apache ECharts (`frontend/src/charts/`)
1. Crear el wrapper reutilizable `EChartsDarkBase.tsx`:
   * Fondo transparente (`backgroundColor: 'transparent'`).
   * Paleta temática: colores esmeralda, neutros y acentos de severidad.
   * Tooltip oscuro (`#2A2A2A` con borde `#8A8F8A` y texto `#EDEDED`).
   * Responsive: Hook que invoca `chart.resize()` ante eventos de redimensionamiento de ventana.

---

## 4. Definition of Done (DoD) — Criterios de Aceptación

Para dar por concluida la Fase 4, se deben validar y marcar todas las casillas siguientes:

- [ ] **Cumplimiento Estricto de `design-dark.md`:** 100% de las vistas renderizan con fondo `#1C1C1C`, cards `#2A2A2A`, texto `#EDEDED` y acento esmeralda `#17a163`. Cero elementos con degradados o colores no autorizados.
- [ ] **Tablero Kanban Interactivo:** En `/issues`, el conmutador cambia entre modo Lista y Tablero; arrastrar una tarjeta o cambiar su estado actualiza la base de datos y recalcula los contadores de severidad.
- [ ] **Visor de PoC Funcional:** La ficha de detalle de vulnerabilidad renderiza el script o comando curl generado por Strix y el botón de copiado traslada el texto al portapapeles.
- [ ] **Visor de Diff de Autofix:** El diff del parche se renderiza con formato unificado y el botón "Create Pull Request with Fix" dispara la creación de la rama en el repositorio conectado.
- [ ] **Gate Antifraude de Dominios:** La plataforma bloquea cualquier intento de pentest sobre dominios no verificados; el flujo de verificación por DNS TXT o HTTP `.well-known` valida la titularidad antes de habilitar el escaneo.
- [ ] **Terminal de Logs en Vivo:** En `/pentests/:id`, los logs de ejecución de Strix se muestran en tiempo real con tipografía monospace `JetBrains Mono` y autoscroll.
- [ ] **Auditoría de i18n Completa:** Cero cadenas de texto hardcodeadas en componentes (`i18n-audit` limpio en `es` y `en`). Conmutar idioma actualiza toda la interfaz instantáneamente.
- [ ] **Aislamiento Multi-tenant en UI (R3):** Cambiar de organización en el selector del sidebar recarga la totalidad de las vistas mostrando únicamente los recursos de la organización seleccionada.

---

## 5. Protocolo de Revisión Especializada (Paso 8 de AGENTS.md)

| Especialista | Verificación Obligatoria |
| :--- | :--- |
| **Frontend / UI Reviewer** | Comprobar que todos los componentes provienen del sistema de diseño oscuro, no existen estilos en línea arbitrarios y los estados de carga (*skeletons*) y vacíos (*empty states*) están implementados en todas las tablas. |
| **Security Reviewer** | Comprobar que los scripts de PoC y los diffs de autofix renderizados en el navegador escapan rigurosamente cualquier entrada HTML para prevenir vulnerabilidades de Cross-Site Scripting (Stored XSS). |
| **Silent Failure Hunter** | Asegurar que la terminal de logs en `/pentests/:id` maneja desconexiones transitorias de SSE o WebSockets con reconexión automática sin bloquear la interfaz de usuario. |
| **Performance Optimizer** | Verificar que la tabla de vulnerabilidades implementa paginación o virtualización para renderizar sin retrasos cuando una organización acumule más de 1.000 incidencias. |