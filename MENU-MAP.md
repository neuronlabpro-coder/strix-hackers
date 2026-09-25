# MAPA DE MENÚ Y SUBPÁGINAS — Mind Guard Fenix Team

> **Documento de control y autoridad de producto/UI.**
> Define cada vista, subpestaña, modal y componente interactivo del panel web basándose en las capturas del producto comercial y la arquitectura técnica oficial de **Mind Guard Fenix Team**.
> Sirve como especificación para el frontend y como lista de verificación cruzada entre fases.
>
> **Regla de producto:** Si una pantalla o funcionalidad visual no está listada en este documento, **no se construye** sin registrarla aquí previamente.
>
> Estado de cada elemento: `[ ]` pendiente de desarrollo · `[~]` en progreso · `[x]` completado y verificado.

---

## 0. Estructura Global y Shell de Navegación

- [ ] **0.1 Barra Lateral Izquierda (Sidebar)**
  - [ ] Selector de Organización / Workspace (Dropdown con switch entre organizaciones y botón `+ Create Workspace`)
  - [ ] Navegación Principal:
    - [ ] `Dashboard` (`/dashboard`)
    - [ ] `Pentests` (`/pentests`)
    - [ ] `Issues` (`/issues`)
    - [ ] `PR Reviews` (`/pr-reviews`)
    - [ ] `Supply Chain` (Bloqueado con candado · Tier Enterprise)
    - [ ] `Containers` (Bloqueado con candado · Tier Enterprise)
    - [ ] `Chat` (`/chat`)
  - [ ] Separador de Activos & Configuración:
    - [ ] `Repositories` (`/repositories`)
    - [ ] `Domains` (`/domains`)
    - [ ] `Asset Discovery` (Bloqueado con candado · Tier Enterprise)
    - [ ] `Networks` (Bloqueado con candado · Tier Enterprise)
    - [ ] `Knowledge` (`/knowledge`)
    - [ ] `Integrations` (`/integrations`)
    - [ ] `API Access` (`/api`)
    - [ ] `Settings` (`/settings`)
  - [ ] Footer del Sidebar: Perfil de usuario autenticado (Avatar, Nombre, Email, Menú de cierre de sesión)
- [ ] **0.2 Barra Superior (Header)**
  - [ ] Banner de Estado de Suscripción (Días restantes de prueba / Alerta de añadir tarjeta de pago)
  - [ ] Botón de acción global destacada `+ New Pentest` (Estilo Tertiary Emerald `#17a163`)
- [ ] **0.3 Checklist de Coherencia Transversal**
  - [ ] Tema oscuro exclusivo basado en `design-dark.md` (`#1C1C1C` base, `#2A2A2A` tarjetas, `#17a163` acción única)
  - [ ] Cero literales hardcodeados: todos los textos mediante `i18next` (`locales/es/` y `locales/en/`)
  - [ ] Todas las tablas con paginación server-side y filtros funcionales
  - [ ] Aislamiento multi-tenant estricto: ninguna llamada API permite listar datos de otra organización

---

## 1. Dashboard Principal (`/dashboard`)

- [x] **1.0 · KPIs de Postura de Seguridad**  _(Bloque 4.1)_
  - [x] Tarjeta `Security Score` (Cálculo porcentual de salud 0-100% mediante ECharts Gauge)
  - [x] Contador `Open Issues` (Vulnerabilidades activas sin mitigar)
  - [x] Contador `Issues Found` (Total histórico acumulado)
  - [x] Ratio `Fix Rate` (% de vulnerabilidades resueltas / autofixed)
  - [x] Contador `PRs Reviewed` (Revisiones en Pull Requests ejecutadas en el mes)
  - [x] Contador `Pentests` (Total de escaneos profundos ejecutados)
- [ ] **1.1 · Guía de Configuración Inicial (Get Set Up)**
  - [ ] Barra de progreso reactiva (X de 6 pasos completados)
  - [ ] Paso 1: Conectar repositorios (`Connect your repositories` ──▶ `/repositories`)
  - [ ] Paso 2: Lanzar primer pentest (`Run your first pentest` ──▶ Modal `+ New Pentest`)
  - [ ] Paso 3: Contextualizar aplicaciones (`Teach Fenix about your apps` ──▶ `/knowledge`)
  - [ ] Paso 4: Revisar primer Pull Request (`Get your first PR security review` ──▶ `/pr-reviews`)
  - [ ] Paso 5: Conectar integraciones (`Set up integrations` Slack/Jira ──▶ `/integrations`)
  - [ ] Paso 6: Invitar al equipo (`Invite your team` ──▶ `/settings/members`)
- [x] **1.2 · Widget de Repositorios Conectados**  _(Bloque 4.1)_
  - [x] Lista de repositorios vinculados con badge de proveedor (GitHub, GitLab, Bitbucket, Gitea)
  - [x] Toggle interactivo de estado de revisión continua (`Reviews on` / `Off`)
  - [x] Enlace rápido `View all` hacia `/repositories`

---

## 2. Pentests & Escaneos (`/pentests`)

- [ ] **2.0 · Pestaña Pentests (Listado de ejecuciones)**
  - [ ] Barra de herramientas: Búsqueda textual (`Search pentests...`), Filtro de Estado (`All statuses`: Queued, Running, Completed, Failed), Filtro de Tipo (`All types`: Repo Scan, Web App, API Contract), Filtro de Fecha (`Any date`)
  - [ ] Tabla de ejecuciones:
    - [ ] `Status`: Badge de estado con animación de pulso en `Running`
    - [ ] `Pentest`: Nombre del target o repositorio analizado
    - [ ] `Type`: Tipo de escaneo (Whitebox repo / Blackbox web / API spec)
    - [ ] `Issues`: Conteo de vulnerabilidades encontradas por severidad
    - [ ] `Started`: Timestamp de inicio relativo
  - [ ] Estado vacío interactivo (`No pentests yet` + Botón `+ New pentest`)
- [ ] **2.1 · Pestaña Schedules (Programación recurrente)**
  - [ ] Lista de auditorías programadas (semanales, mensuales o cron)
  - [ ] Botón para calendarizar nueva auditoría periódica
- [ ] **2.2 · Modal de Lanzamiento: `+ New Pentest`**
  - [ ] Selector de Objetivo: Repositorio conectado vs URL Externa (Dominio o API)
  - [ ] Campo para subir o seleccionar especificación OpenAPI / Swagger / Postman
  - [ ] Selector de modo de análisis: `Quick` (SAST superficial) vs `Standard` vs `Deep Penetration`
  - [ ] Selector de Branch / Tag (si el objetivo es un repositorio)
  - [ ] Estimación de consumo de créditos visible antes de confirmar
  - [ ] Botón de confirmación `Start Pentest` (`#17a163`)
- [ ] **2.3 · Vista Detallada de Ejecución (`/pentests/:id`)**
  - [ ] Visor de progreso en vivo con polling / SSE
  - [ ] Consola de logs estructurados del motor en `JetBrains Mono` con autoscroll y botón de descarga
  - [ ] Grafo de agentes activos: `Reconnaissance` ──▶ `Exploitation` ──▶ `Validation (PoC)` ──▶ `Autofix`
  - [ ] Botón de cancelación de emergencia (`Abort Run`)

---

## 3. Gestor de Vulnerabilidades (`/issues`)

- [ ] **3.0 · Contadores por Severidad**
  - [ ] `Critical` (Rojo) · `High` (Naranja) · `Medium` (Amarillo) · `Low` (Azul)
- [ ] **3.1 · Pestañas de Estado de Resolución**
  - [ ] `All` · `Open` · `In progress` · `Snoozed` · `Fixed` · `Ignored`
- [ ] **3.2 · Filtros y Conmutador de Vistas**
  - [ ] Barra de búsqueda de issues
  - [ ] Selector de severidad (`All severities`)
  - [ ] Selector de repositorio / dominio (`All repositories`)
  - [ ] Conmutador de visualización: Modo Lista (`List`) vs Modo Tablero (`Board` Kanban)
- [ ] **3.3 · Tabla de Vulnerabilidades**
  - [ ] Checkbox para acciones masivas (Snooze masivo, Mark as Fixed, Export CSV)
  - [ ] Columnas: `Severity`, `Issue` (Título y endpoint/archivo), `CVE`, `CVSS Score`, `Discovered`
  - [ ] Estado vacío (`No open issues`)
- [ ] **3.4 · Ficha de Detalle de Vulnerabilidad (`/issues/:id`)**
  - [ ] Descripción técnica de la vulnerabilidad y vector de ataque (OWASP Top 10)
  - [ ] **Sección Prueba de Concepto (PoC):** Comando curl reproducible o script Python generado por el agente
  - [ ] **Sección Autofix:** Diff del código con el parche propuesto listo para aplicar
  - [ ] Botón `Create Pull Request with Fix` (Abre PR con el parche en el proveedor Git)
  - [ ] Botón `Re-test Vulnerability` (Dispara un test focalizado para validar si sigue abierta)
  - [ ] Historial de auditoría inmutable de cambios de estado del issue

---

## 4. Revisiones en Pull Requests (`/pr-reviews`)

- [ ] **4.0 · Pestaña Reviews (Historial de PRs auditados)**
  - [ ] Banner informativo: `Tag @fenix-team on any pull request to run a security review`
  - [ ] Filtros por estado del PR: `All`, `Awaiting merge`, `Needs attention`, `Merged with open findings`, `Passed`
  - [ ] Botones de cabecera: `Settings`, `+ Connect repository`, `Review a pull request`
  - [ ] Tabla: `Status`, `Pull request` (Número, título y rama), `Repository`, `Issues` detectados, `Created`
- [ ] **4.1 · Pestaña Issues Caught (Vulnerabilidades frenadas en CI)**
  - [ ] Métricas de prevención: `PRs reviewed`, `Issues caught`, `Critical / High`, `Merges blocked`
  - [ ] Tabla de fallos detectados antes de llegar a producción con filtro de severidad y estado del PR

---

## 5. Consola Conversacional con Agentes (`/chat`)

- [ ] **5.0 · Interfaz de Chat con el Agente Ofensivo**
  - [ ] Cabecera con advertencia de créditos: *"A chat uses part of a credit for each agent step. Your balance is in Billing"*
  - [ ] Entrada de prompt multimodal con adjuntos:
    - [ ] Botón `Credentials` (Inyectar credenciales temporales de prueba)
    - [ ] Botón `Scope domains` (Delimitar dominios permitidos)
    - [ ] Botón `Add repositories` (Vincular repositorios como contexto)
- [ ] **5.1 · Atajos de Auditoría Guiada por Categoría**
  - [ ] Pestañas: `Web`, `Code`, `Cloud`, `Recon`, `Network`, `Threat Intel`, `Compliance`
  - [ ] Cards preconfiguradas:
    - [ ] `Test API authorization`: Comprobación de BOLA / IDOR y escalada de privilegios
    - [ ] `Analyze OAuth flows`: Validación de tokens, redirecciones y CSRF en OAuth
    - [ ] `Detect SSRF vectors`: Detección de peticiones del lado del servidor hacia servicios internos
    - [ ] `Audit business logic`: Condiciones de carrera, manipulación de precios y bypass de flujos

---

## 6. Gestión de Activos

### 6.1 Repositorios (`/repositories`)  _(Bloque 4.1)_
- [x] Barra de búsqueda y botón `+ Add repository`
- [x] Tabla de repositorios conectados:
  - [x] Icono del proveedor (GitHub, GitLab, Bitbucket, Gitea) + `Nombre del repo`
  - [x] `Status`: Not tested / Tested / Scanning
  - [x] `Issues`: Conteo de vulnerabilidades abiertas
  - [x] `PR reviews`: Toggle interactivo para activar/desactivar revisiones automáticas
  - [ ] `Supply chain`: Estado de SBOM de dependencias  _(pendiente: Strix aún no expone SBOM)_
  - [x] `Last tested`: Fecha y hora de la última auditoría

### 6.2 Dominios y APIs (`/domains`)
- [ ] Barra de búsqueda y botón `+ Add domain`
- [ ] Tabla de endpoints y hosts externos:
  - [ ] `Domain`: Hostname o URL base
  - [ ] `Verification`: Estado de validación de propiedad (DNS TXT record o archivo HTTP en `/.well-known/`)
  - [ ] `Login`: Configuración de credenciales de prueba para análisis autenticado
  - [ ] `Issues`: Conteo de fallos detectados
  - [ ] `Last tested`: Último análisis ejecutado
- [ ] Modal de Verificación de Dominio: Instrucciones obligatorias antes de permitir el escaneo

---

## 7. Base de Conocimiento de la Aplicación (`/knowledge`)

- [ ] **7.0 · Pestañas de Contexto**
  - [ ] `Custom`: Reglas de negocio añadidas manualmente por el usuario
  - [ ] `Internal`: Documentación de arquitectura y contexto deducido automáticamente por el agente
  - [ ] `Connected sources`: Fuentes conectadas mediante servidores MCP
- [ ] **7.1 · Gestión de Conocimiento**
  - [ ] Barra de búsqueda y filtro por ámbito (`All scopes`)
  - [ ] Botón `+ Add knowledge`
  - [ ] Formulario: Título, Ámbito (Global / Repositorio específico), Tipo (Business Logic, Critical Asset, Testing Rule, Accepted Risk) y Contenido en Markdown
  - [ ] Tabla: `Knowledge`, `Scope`, `Type`, `Updated`

---

## 8. Integraciones (`/integrations`)

- [ ] **8.0 · Proveedores de Código (Code Providers)**
  - [ ] `GitHub`: Estado (`Connected` con nombre de usuario/org), Botón `Configure`, Menú de desconexión
  - [ ] `GitLab`: Botón `Connect` (Flujo OAuth2 / Personal Access Token)
  - [ ] `Bitbucket`: Botón `Connect` (Flujo OAuth2)
  - [ ] `Gitea`: Botón `Connect` (Instancia autoalojada con URL y Token)
- [ ] **8.1 · Canales de Notificación**
  - [ ] `Slack`: Botón `Connect` (Alertas a canales de seguridad)
  - [ ] `Microsoft Teams`: Badge `Coming soon`
- [ ] **8.2 · Seguimiento de Incidencias (Issue Tracking - Sincronización Bidireccional)**
  - [ ] `Jira`: Botón `Connect` (Apertura automática de tickets con PoCs)
  - [ ] `Linear`: Botón `Connect`
- [ ] **8.3 · Infraestructura sobre MCP (Lectura de Arquitectura)**
  - [ ] Cards de conexión: `AWS`, `Vercel`, `Supabase`, `Cloudflare`, `Google Cloud`
- [ ] **8.4 · Fuentes de Conocimiento MCP (Organization Knowledge)**
  - [ ] Botón `+ Add an MCP server`
  - [ ] Modal con catálogo de conectores: Notion, Confluence, Trello, Airtable, PostHog, Neon, Stripe, ClickUp, Railway, Sentry, Amplitude, Attio, incident.io, Glean
  - [ ] Opción manual `Enter a URL` para conectar cualquier servidor MCP externo

---

## 9. Acceso API y Servidor MCP (`/api`)

- [ ] **9.0 · Pestaña Tokens (API Keys del Tenant)**
  - [ ] Tabla: `Name`, `Type` (Personal token vs Service key), `Scopes`, `Status`, `Last used`, Acciones (Revocar)
  - [ ] Botón `+ New token`
  - [ ] **Modal de Creación de Token:**
    - [ ] Campo `Name` y Selector `Type`
    - [ ] Matriz de selección granular de permisos (46 Scopes con presets: Defaults, All, None):
      - [ ] `Scans` (Read / Write / Message)
      - [ ] `Vulnerabilities` (Read / Write)
      - [ ] `Dependencies` (Read) · `Supply chain` (Read / Write)
      - [ ] `Containers` (Read / Write) · `Schedules` (Read / Write)
      - [ ] `Assets` (Read / Write) · `Organizations` (Read / Write)
      - [ ] `Members` (Read / Write) · `Invitations` (Read / Write)
      - [ ] `Webhooks` (Read / Write) · `Tokens` (Write)
      - [ ] `Audit` (Read) · `PR reviews` (Read / Write)
      - [ ] `Connectors` (Read / Write) · `Knowledge` (Read / Write)
      - [ ] `Uploads` (Write) · `Integrations` (Read / Write)
      - [ ] `Chat` (Read / Write) · `Analytics` (Read)
      - [ ] `LLM` (Read / Write) · `Test users` (Read / Write)
      - [ ] `Discovery` (Read / Write) · `License` (Read)
      - [ ] `Logs` (Read) · `Billing` (Read / Write)
    - [ ] Selector de Expiración (30 días, 90 días, 1 año, Sin expiración)
    - [ ] Pantalla de clave generada única con prefijo `mgf_live_...` (Advertencia: solo se muestra una vez)
- [ ] **9.1 · Pestaña Webhooks (Eventos Salientes)**
  - [ ] Tabla de endpoints registrados y su estado de entrega
  - [ ] Botón `+ New webhook`
  - [ ] **Modal de Creación de Webhook:**
    - [ ] Campo `Endpoint URL`
    - [ ] Selector de Eventos suscritos:
      - [ ] `scan.created` · `scan.completed` · `scan.failed` · `scan.cancelled`
      - [ ] `vulnerability.created` · `vulnerability.status_changed` · `vulnerability.severity_changed`
      - [ ] `All events *` (Suscripción global)
    - [ ] Generación de secreto HMAC SHA-256 para validación de firma (`X-Fenix-Signature`)
- [ ] **9.2 · Pestaña Servidor MCP (`/api?tab=mcp`)**
  - [ ] Visualización de URLs del servidor:
    - [ ] `Server URL`: Endpoint SSE principal (`https://app.tu-dominio.com/mcp`)
    - [ ] `Core profile URL`: Perfil ligero para clientes con límite de herramientas
  - [ ] Guías de configuración con botón de copiado rápido:
    - [ ] Instrucciones para `Claude Desktop`
    - [ ] Instrucciones para `ChatGPT` (OAuth Connector)
    - [ ] Snippet JSON para `.cursor/mcp.json`
    - [ ] Comando de terminal para `Claude Code` (`claude mcp add ...`)

---

## 10. Configuración del Workspace (`/settings`)

- [ ] **10.0 · Sección General (`/settings/general`)**
  - [ ] Bloque `Account`: Avatar, Nombre, Email, Botón `Sign out`, Plan activo (`Pro`) con enlace a `Manage billing`
  - [ ] Bloque `Security`: Switch para activar/desactivar `Two-factor authentication` (TOTP / Authenticator App)
  - [ ] Bloque `Organization`: Input de Nombre de Organización, `Organization ID` con botón de copiado, `Your role` (Admin)
  - [ ] Bloque `Danger zone`: Botón crítico `Delete organization` (con modal de confirmación destructiva)
- [ ] **10.1 · Sección Audit Logs (`/settings/audit-logs`)**
  - [ ] Bloqueo visual con banner informativo `Upgrade to Enterprise`
  - [ ] Modal descriptivo: Logs de auditoría inmutables, SSO/SCIM, roles a medida, exportación a SIEM (Splunk, Snowflake)
- [ ] **10.2 · Sección Miembros (`/settings/members`)**
  - [ ] Contador total de miembros del workspace
  - [ ] Tabla de Miembros: Avatar/Email, `Role` (Admin / Member), Fecha de incorporación (`Joined`)
  - [ ] Lista de invitaciones pendientes (`Pending invitations`) con fecha de expiración
  - [ ] Botón `+ Invite member`
  - [ ] Modal de Invitación: Campo de correo electrónico y selector de rol (`Admin` vs `Member`)
- [ ] **10.3 · Sección Facturación & Créditos (`/settings/billing`)**
  - [ ] Bloque `Credits`:
    - [ ] Saldo en vivo (`0.00 credits`)
    - [ ] Botón `+ Top up credits` (Modal de recarga rápida con Stripe Checkout)
  - [ ] Bloque `Active developers`:
    - [ ] Conteo de desarrolladores activos en el mes
    - [ ] Texto informativo: *Pro includes 50 PR reviews per active developer per month. Additional PR reviews cost $1 each.*
    - [ ] Botón `View breakdown`
  - [ ] Bloque `Payment method`:
    - [ ] Estado de tarjeta guardada o badge de alerta `Missing`
    - [ ] Botón `Add card`
    - [ ] Switch interactivo `Auto top-up` (Recarga automática cuando el saldo sea bajo)
  - [ ] Bloque `Enterprise`: Enlace comercial `Talk to a human` y `View plans`
  - [ ] Bloque `Startup program`: Banner con descuento del 50% durante 6 meses con botón `Apply`
- [ ] **10.4 · Soporte Integrado (Help & Support Widget)**
  - [ ] Widget flotante en esquina inferior derecha
  - [ ] Opciones: `Report an issue` (Envío de bugs con captura), `Request a feature` y Chat con soporte