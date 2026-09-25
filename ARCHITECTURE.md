# ARQUITECTURA DEL SISTEMA — Mind Guard Fenix Team

> **Documento canónico de arquitectura técnica.** Fuente única de verdad sobre *qué es* Mind Guard Fenix Team, *cómo encajan sus planos* y las garantías de aislamiento, seguridad y ejecución.
> Todo desarrollo posterior, especificación de fases (`phases/`) y decisiones de código deben derivar estrictamente de este documento. Si algo contradice a este documento, manda este documento.
>
> **Versión:** 1.0 (Mind Guard Fenix Team · B2B SaaS Multi-tenant + Sandbox Engine + Dokploy/Tailscale)  
> **Base de referencia:** `usestrix/strix` (Motor en `engines/`) y `apptension/saas-boilerplate` (Plataforma en `engines/`).

---

## 0. Visión General del Producto

**Mind Guard Fenix Team** es una plataforma SaaS B2B de **seguridad ofensiva continua, pentesting autónomo y DevSecOps asistido por Inteligencia Artificial**.

A diferencia de los escáneres estáticos tradicionales (SAST) que saturan a los equipos de desarrollo con falsos positivos sintácticos, Mind Guard Fenix Team opera como un equipo ofensivo automatizado: analiza el código fuente, despliega vectores de ataque dinámicos (DAST + SAST híbrido) dentro de un entorno aislado (sandbox), **valida cada vulnerabilidad mediante Pruebas de Concepto (PoC) reproducibles** y genera parches de código remediadores (*autofix*) listos para su integración mediante Pull Requests / Merge Requests.

### Modos de Operación Soportados:
1. **Auditoría Continua de Código (CI/CD & Git Providers):** Conexión nativa con **GitHub, GitLab, Bitbucket y Gitea** para auditar Pull Requests entrantes de forma automática o mediante comandos en comentarios (`@fenix-team review`).
2. **Pentesting Externo de Aplicaciones y APIs:** Escaneo dinámico de caja negra y caja gris sobre dominios verificados y contratos de API (OpenAPI, Swagger, colecciones Postman).
3. **Superficie MCP (Model Context Protocol):** Interfaz remota `/mcp` para que asistentes de desarrollo en IDEs (Cursor, Claude Code) o ChatGPT consuman directamente las capacidades de auditoría, PoC y autofix.

---

## 1. Las 6 Reglas de Oro de Ingeniería

Tienen prioridad absoluta sobre cualquier atajo o conveniencia técnica. Su infracción invalida cualquier entrega:

* **🥇 R1 — Cero Hardcoding e i18n Estricto:** Precios de planes, cuotas de escaneo, costes de créditos, URLs de endpoints, modelos LLM y prompts base residen en base de datos o variables de entorno (`.env`). Cero literales de texto en el frontend; todo pasa por `t('clave')` mediante `i18next` con soporte para español e inglés (`frontend/src/locales/{es,en}/`).
* **🥈 R2 — `engines/` es Estrictamente Solo Lectura:** El repositorio `usestrix/strix` y el código base de `saas-boilerplate` se ubican en la carpeta `engines/`. Prohibido editar dentro de ellos o depender en tiempo de ejecución de sus rutas relativas. Todo código se extrae y adapta limpiamente hacia `backend/` y `frontend/`.
* **🥉 R3 — Aislamiento Multi-tenant Absoluto (Datos y Ejecución):** Toda consulta a datos privados en PostgreSQL incluye `organization_id`. A nivel de runner: **cada pentest corre en su propio contenedor Docker efímero**, sin compartir memoria, socket proxy ni almacenamiento con otros clientes.
* **R4 — Inmutabilidad Financiera y de Evidencias:** La tabla `credit_ledger` es estrictamente *append-only* (solo `INSERT`, el balance es siempre `SUM`). Los hallazgos de vulnerabilidades, trazas de ejecución de PoC y registros de auditoría no pueden ser alterados ni borrados para garantizar validez legal y técnica ante auditorías (SOC 2, ISO 27001).
* **R5 — Zero Data y Privacidad de Código Fuente:** El código descargado de los clientes solo existe temporalmente en el volumen efímero del runner durante el análisis. Al finalizar el job, el volumen se purga de disco con seguridad. La base de datos central únicamente persiste metadatos de vulnerabilidad (CVE/CVSS), pasos de reproducción del exploit y el diff del parche propuesto.
* **R6 — Datos Remotos en Dokploy, Desarrollo Local por Tailscale:** PostgreSQL (`5433`) y Redis (`6380`) residen en el VPS remoto bajo Dokploy, enlazados exclusivamente a la interfaz privada de Tailscale (`100.89.59.70`)[cite: 40]. Los puertos de base de datos nunca se exponen al Internet público (`0.0.0.0`) y el stack previo de Mindguard permanece intacto[cite: 40].

---

## 2. Los Tres Planos de la Arquitectura

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                              CLIENTES & CANALES DE ENTRADA                             │
│     Panel Web (Dark Emerald)   │   Git PR Reviews   │   IDEs / MCP (Cursor, Claude)    │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │ HTTPS / WSS / SSE
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        PLANO DE CONTROL (Backend API Gateway)                          │
│  - Multi-tenancy (Workspaces / Orgs)            - RBAC (46 Permisos granulares)        │
│  - Conectores Git (OAuth & Webhooks)            - Facturación Stripe & Credit Ledger   │
│  - Gestor de Vulnerabilidades (CVSS/PoC)        - Servidor MCP Remoto (/mcp)           │
└───────────────────────┬────────────────────────────────────────┬───────────────────────┘
                        │ Encola Job (Task Payload)               │ Consulta Estado / Logs
                        ▼                                        ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                     PLANO DE ORQUESTACIÓN (Colas Redis + Workers)                      │
│  - Despachador de escaneos           - Control de cuotas, timeouts y tokens LLM        │
│  - Monitoreo de salud de runners     - Ingesta de artefactos strix_runs/               │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │ Lanza contenedor efímero
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                  PLANO DE EJECUCIÓN AISLADA (Docker Sandbox Engine)                    │
│                                                                                        │
│  Contenedor Efímero: [ ghcr.io/usestrix/strix-sandbox ]                                │
│  ┌──────────────────────────────────────────────────────────────────────────────────┐  │
│  │  Engine Headless (-n)                                                            │  │
│  │  ├── Graph of Agents (Reconocimiento, Explotación, Validación PoC, Autofix)      │  │
│  │  ├── Herramientas integradas: Caido Proxy, Playwright Headless, Shell / Python   │  │
│  │  └── Conexión a Proveedores LLM (OpenAI, Anthropic, OpenRouter)                  │  │
│  └──────────────────────────────────────────────────────────────────────────────────┘  │
│                                            │                                           │
│                       Volumen Temporal de Trabajo (/workspace)                         │
│                       (Clonado efímero ──▶ Escaneo ──▶ Purga segura R5)                │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 Plano de Control (Control Plane)
* **Gestión de Cuentas y Workspaces:** Autenticación de usuarios, creación de organizaciones, invitaciones por correo y asignación de roles (`Admin`, `Member`).
* **API REST y Rutas Operativas:** Expone los endpoints administrativos, el visor de escaneos, gestión de incidencias y estado de suscripciones.
* **Control de Acceso Basado en Roles (RBAC):** Catálogo granular de 46 permisos que protege cada acción (`scans:read/write`, `vulnerabilities:read/write`, `billing:read/write`, `webhooks`, etc.).
* **Facturación Híbrida (Stripe):** Suscripción recurrente por desarrollador activo + saldo prepago de créditos para escaneos profundos + facturación por uso para exceso de PRs.

### 2.2 Plano de Orquestación (Orchestration Plane)
* **Broker de Tareas (Redis 7 en puerto `6380`):** Cola central donde se registran las peticiones de escaneo (manuales, cron o disparadas por webhooks de Git)[cite: 40].
* **Workers de Despacho (Celery):** Gestionan el ciclo de vida del contenedor de escaneo, limitan la concurrencia por cliente y aplican timeouts estrictos para evitar costes desmedidos de LLM o bloqueo de recursos en el VPS.
* **Ingestor de Resultados:** Parser que analiza el archivo de resultados generado en el sandbox, extrae los hallazgos estructurados, clasifica las severidades CVSS, verifica la traza de los exploits PoC y persiste los datos en PostgreSQL (`5433`)[cite: 40].

### 2.3 Plano de Ejecución Ofensiva (Execution Plane)
* **Aislamiento Multi-Tenant Estricto (R3):** Cada análisis se ejecuta en una instancia dedicada de la imagen Docker `ghcr.io/usestrix/strix-sandbox`.
* **Ejecución Headless (`-n`):** El motor opera en modo no interactivo controlado por el worker:
  ```bash
  strix -n \
    --target /workspace/repo \
    --scan-mode standard \
    --instruction-file /workspace/knowledge.md \
    --output /workspace/output.json
  ```
* **Destrucción Efímera (R5):** Tras parsear y almacenar el reporte, el contenedor se detiene y se elimina junto con el volumen temporal que contenía el código clonado.

---

## 3. Integración con Proveedores Git y Flujo de CI/CD

Mind Guard Fenix Team se conecta con **GitHub, GitLab, Bitbucket y Gitea** mediante Apps OAuth y Webhooks dedicados:

```
[ Desarrollador abre o actualiza PR / MR ] ──▶ [ Webhook del Proveedor Git ]
                                                         │
                                                         ▼
                                             [ Endpoint /api/webhooks/git ]
                                                         │
                                                         ├── 1. Valida firma HMAC SHA-256
                                                         ├── 2. Identifica Organización y Repositorio
                                                         ├── 3. Verifica saldo de créditos / cuota de PR
                                                         └── 4. Encola tarea: `run_pr_security_scan`
                                                                 │
                                                                 ▼
                                                     [ Worker de Escaneo ]
                                                                 │
                                                ┌────────────────┴────────────────┐
                                                │ Clona rama (shallow / depth=1)  │
                                                │ Ejecuta Quick Scan              │
                                                │ Genera reporte de hallazgos     │
                                                └────────────────┬────────────────┘
                                                                 │
                                                         [ Ingestor Fenix ]
                                                                 │
                                                         ▼
                                       [ Notificación y Feedback en el PR ]
                                       ├── Comentario con vulnerabilidades y PoCs
                                       ├── Bloqueo de Merge (Status Check: Failed)
                                       └── Generación de Pull Request con Autofix
```

### Modos de Análisis en Repositorios:
1. **Revisión de PR Automática:** Escaneo incremental acotado estrictamente a los archivos modificados en el diff del Pull Request (`--scan-mode quick`).
2. **Revisión Bajo Demanda (ChatOps):** Activación manual comentando en el hilo del PR (ej. `@fenix-team review`).
3. **Pentest Completo Programado:** Auditoría exhaustiva semanal o mensual sobre la rama principal (`main`/`master`) evaluando rutas complejas y dependencias.

---

## 4. Modelo de Datos Central (PostgreSQL :5433)[cite: 40]

```
┌─────────────────────────┐       ┌─────────────────────────┐       ┌─────────────────────────┐
│      Organizations      │1     N│       Repositories      │1     N│       PentestRuns       │
│─────────────────────────┼───────┼─────────────────────────┼───────┼─────────────────────────│
│ id (UUID)               │       │ id (UUID)               │       │ id (UUID)               │
│ name (VARCHAR)          │       │ organization_id (FK)    │       │ repository_id (FK, Null)│
│ plan_tier (VARCHAR)     │       │ provider (github/gitlab)│       │ target_url (VARCHAR)    │
│ credit_balance (DECIMAL)│       │ remote_repo_id (VARCHAR)│       │ scan_mode (quick/full)  │
│ created_at (TIMESTAMP)  │       │ default_branch (VARCHAR)│       │ status (QUEUED/RUNNING) │
└────────────┬────────────┘       │ pr_reviews_enabled(BOOL)│       │ started_at (TIMESTAMP) │
             │                    └─────────────────────────┘       │ finished_at (TIMESTAMP)│
             │                                                      └────────────┬────────────┘
             │1                                                                  │1
             │                                                                   │
             │N                                                                  │N
┌────────────┴────────────┐                                         ┌────────────┴────────────┐
│      CreditLedger       │                                         │     Vulnerabilities     │
│       (Append-Only)     │                                         │─────────────────────────│
│─────────────────────────│                                         │ id (UUID)               │
│ id (UUID)               │                                         │ run_id (FK)             │
│ organization_id (FK)    │                                         │ title (VARCHAR)         │
│ delta_credits (DECIMAL) │                                         │ severity (CRITICAL/HIGH)│
│ balance_after (DECIMAL) │                                         │ cvss_score (FLOAT)      │
│ event_type (VARCHAR)    │                                         │ cve_id (VARCHAR, Null)  │
│ reference_id (VARCHAR)  │                                         │ poc_reproduction (TEXT) │
│ created_at (TIMESTAMP)  │                                         │ autofix_patch (TEXT)    │
└─────────────────────────┘                                         │ status (OPEN/FIXED)     │
                                                                    └─────────────────────────┘
```

---

## 5. Infraestructura y Red (Dokploy + Tailscale)

El despliegue sigue la regla **R6 (Servicios y datos remotos, desarrollo local por túnel seguro)**:

```
[ ENTORNO DE DESARROLLO LOCAL (Windows / VS Code) ]
├── Frontend Vite / React (localhost:5173 con hot-reload)
├── Backend API / Servidor Local (localhost:8000)
└── Tailscale Client (IP Interna: 100.x.y.z)
         │
         │ (Túnel cifrado WireGuard punto a punto)
         ▼
[ VPS REMOTO CONTABO (Nodo Dokploy: 100.89.59.70) ][cite: 40]
├── Stack Existente Mindguard (INTACTO en 5432, 6379, 5050, Gitea)[cite: 40]
│
└── Stack Mind Guard Fenix Team (AISLADO):
      ├── PostgreSQL 16 (fenix-postgres en 100.89.59.70:5433)[cite: 40]
      ├── Redis 7 (fenix-redis en 100.89.59.70:6380)[cite: 40]
      └── Docker Daemon Host (para lanzar ghcr.io/usestrix/strix-sandbox efímeros)
```

* **Seguridad de Puertos:** Los puertos `5433` y `6380` están enlazados exclusivamente a la interfaz privada de Tailscale[cite: 40]. Escaneos públicos con `nmap` contra la IP pública del VPS confirmarán dichos puertos cerrados o filtrados.
* **Seguridad de Secretos:** Ninguna clave API de modelos LLM, credencial de base de datos o secreto de webhook Git se guarda en el repositorio. Residen en `.env` inyectadas en tiempo de ejecución.

---

## 6. Arquitectura MCP (Model Context Protocol)

Mind Guard Fenix Team opera bidireccionalmente con MCP:

1. **Plataforma como Servidor MCP (`app.tu-dominio.com/mcp`):**
   * Transporte SSE (Server-Sent Events) y HTTP POST autenticado con tokens de servicio.
   * Permite que asistentes de desarrollo en IDEs (Cursor, Claude Code) o ChatGPT ejecuten herramientas directamente:
     * `fenix_start_pentest`: Inicia un escaneo sobre un repositorio o dominio.
     * `fenix_list_issues`: Devuelve las vulnerabilidades filtradas por severidad.
     * `fenix_apply_autofix`: Crea una rama Git con el parche de seguridad validado.
     * `fenix_verify_poc`: Re-ejecuta la prueba de concepto para confirmar la corrección.
2. **Plataforma como Cliente MCP:**
   * Permite a los agentes del runner conectarse a la infraestructura del cliente (AWS, Supabase, Cloudflare, Linear, Jira) vía MCP para extraer contexto de arquitectura antes de iniciar el pentest.

---

## 7. Modelo Financiero y Facturación Técnica

| Concepto | Mecanismo en Stripe | Implementación en Base de Datos |
| :--- | :--- | :--- |
| **Suscripción Pro ($29 / asiento / mes)** | Suscripción recurrente con cantidad dinámica de asientos (`quantity`). | `Organization.plan_tier = 'PRO'`. Incluye 50 PR reviews al mes por cada desarrollador activo. |
| **Bolsa de Créditos Prepago** | Stripe Checkout (pago puntual de saldo) o recarga automática (*Auto top-up*). | Inserciones en `CreditLedger` (`delta_credits > 0`). Cada pentest profundo deduce entre $60 y $300 según el alcance. |
| **Exceso de PRs ($1 / revisión extra)** | Stripe Metered Billing mediante reporte de eventos de uso. | Se contabilizan revisiones que excedan el cupo mensual del plan y se reportan al ciclo de facturación. |
| **Tier Enterprise** | Facturación personalizada, contratos anuales, despliegue en VPC dedicada y soporte de BYOK (Bring Your Own Key). | Flags `enterprise_enabled` en la organización para desbloquear audit logs avanzados y conectores de red privada. |

---

## 8. Diseño Visual y Frontend (`design-dark.md`)

El frontend implementa una interfaz técnica de alta densidad basada en el tema oscuro esmeralda:

* **Paleta de Colores:**
  * **Neutral (`#1C1C1C`):** Base de la aplicación y fondos principales.
  * **Surface (`#2A2A2A`):** Tarjetas de métricas, contenedores de issues y modales.
  * **Primary (`#EDEDED`):** Textos principales, cabeceras y títulos.
  * **Secondary (`#8A8F8A`):** Bordes, metadatos, timestamps y textos secundarios.
  * **Tertiary (`#17a163` - Esmeralda):** Color de acento único reservado para la acción interactiva primaria por pantalla (`+ New Pentest`, `Connect repository`, `Run Autofix`).
* **Tipografía:** `Inter` para elementos de interfaz y titulares; `JetBrains Mono` para datos técnicos (hashes de commits, endpoints, CVEs, puntuaciones CVSS y trazas de PoC).
* **Gráficas:** **Apache ECharts** como biblioteca gráfica exclusiva para visualizar la postura de seguridad, tiempos de resolución y distribución de vulnerabilidades por severidad.

---

## 9. Hoja de Ruta de Desarrollo (`phases/`)

El desarrollo se ejecuta de manera modular y acumulativa; cada fase debe cumplir su *Definition of Done* antes de habilitar la siguiente:

* **Fase 1 (`phases/fase-01-fundacion-infra-dokploy.md`):** Configuración de Dokploy en VPS, conexión segura por Tailscale (`:5433` y `:6380`), extracción del núcleo multi-tenant de `saas-boilerplate`, configuración de base de datos PostgreSQL/Redis y autenticación[cite: 40].
* **Fase 2 (`phases/fase-02-motor-strix-runner-sandbox.md`):** Configuración de la cola de tareas (Celery), empaquetado del runner Docker Sandbox efímero, ejecución headless (`-n`), parser de resultados y almacenamiento inmutable de PoCs.
* **Fase 3 (`phases/fase-03-conectores-git-webhooks-pr.md`):** Apps OAuth y Webhooks para GitHub, GitLab, Bitbucket y Gitea. Feedback automático en Pull Requests y creación de ramas con parches *autofix*.
* **Fase 4 (`phases/fase-04-panel-ui-vulnerabilidades-poc.md`):** Construcción del panel web bajo `design-dark.md`: Dashboard ejecutivo, tablero de vulnerabilidades, visualizador de PoC, Knowledge Base y chat guiado con agentes.
* **Fase 5 (`phases/fase-05-facturacion-creditos-stripe-mcp.md`):** Integración de Stripe (asientos + créditos), sistema de API keys (46 scopes), webhooks salientes y servidor MCP (`/mcp`).
* **Fase 6 (`phases/fase-06-auditoria-end-to-end-produccion.md`):** Pruebas de estrés, auditoría de aislamiento multi-tenant, verificación Zero-Data de código fuente y despliegue final en Dokploy.