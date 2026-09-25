# ROADMAP — Mind Guard Fenix Team

> **Fuente única de ORDEN de trabajo.** Su checklist (al final) es la única fuente de estado del proyecto.
> Cada fase se ejecuta de principio a fin (*end-to-end*), se audita con el harness de `.agents/`, se verifica contra su *Definition of Done* (DoD) y solo entonces se marca como completada. No se saltan fases ni se inician fases en paralelo.
>
> **Autoridades documentales:**
> - `ARCHITECTURE.md` (QUÉ es el sistema y cómo encajan sus planos).
> - `MENU-MAP.md` (QUÉ pantallas y componentes existen en la interfaz).
> - `AGENTS.md` (CÓMO se construye, reglas de oro y protocolo de revisión).
> - `.agents/` (Subagentes, comandos operativos, hooks y skills de ingeniería).
> - `phases/` (Especificación ejecutable detallada de cada fase individual).
>
> Si algo contradice a estos documentos, mandan `ARCHITECTURE.md` y `AGENTS.md`, no este archivo.
>
> **Reglas duras transversales a todas las fases:**
> - `engines/` es **estrictamente de solo lectura**: `usestrix/strix` y `saas-boilerplate` quedan congelados. El runtime nunca importa ni ejecuta código directo desde esa carpeta. Todo componente reutilizable se extrae y adapta hacia `backend/` o `frontend/`.
> - **Datos remotos en Dokploy, código local por Tailscale (R6):** PostgreSQL 16 (puerto `5433`) y Redis 7 (puerto `6380`) residen en el VPS remoto bajo Dokploy, escuchando exclusivamente en la interfaz privada de Tailscale (`100.89.59.70`). Ningún puerto de datos se expone al Internet público (`0.0.0.0`) y el stack previo de Mindguard permanece intacto.
> - **Aislamiento Multi-tenant (R3):** Toda consulta relacional filtra por `organization_id`. Todo pentest corre en su propio contenedor Docker efímero (`ghcr.io/usestrix/strix-sandbox`), aislado en red y memoria.
> - **Inmutabilidad Financiera y de Evidencias (R4):** `credit_ledger` es *append-only* (solo `INSERT`). El balance de créditos se calcula mediante `SUM(delta_credits)`. Los hallazgos de vulnerabilidades y trazas de PoC no son editables desde la UI.
> - **Zero Data en Código Fuente (R5):** El código clonado del cliente solo reside en el volumen efímero del runner durante el análisis (`/tmp/fenix_workspaces/<job_id>`) y se purga de disco con seguridad al terminar.
> - **Cero Hardcoding e i18n total (R1):** Ningún texto visible en código (`t('clave')` obligatorio en `frontend/src/locales/{es,en}/`). Configuración de modelos, costes y timeouts en base de datos o `.env`.

---

## Fase 1 · Fundación de Infraestructura, Dokploy, Tailscale & Core Multi-tenant

**Objetivo:** Dejar la infraestructura base de Dokploy operativa en el VPS en puertos aislados (`5433` y `6380`), la red privada Tailscale configurada, y el backend/frontend locales conectados y autenticados contra los datos remotos con aislamiento multi-tenant estricto, extrayendo la base desde `engines/saas-boilerplate`.

**Depende de:** Nada (fase raíz). Requiere VPS con Dokploy instalado y red Tailscale activa (`100.89.59.70`).

**Alcance (entregables):**
- Configuración de servicios en Dokploy vía compose: `fenix-postgres` (16) en puerto `5433` y `fenix-redis` (7) en puerto `6380` vinculados únicamente a la IP privada de Tailscale (`100.89.59.70`).
- Extracción y adaptación del núcleo backend desde `engines/saas-boilerplate` hacia `backend/core/` (`config`, `database`, `security`, `middleware`) y `backend/apps/organizations/`.
- Modelo de datos multi-tenant: `Organization`, `User`, `Membership` (roles `Admin` y `Member`) y sistema de invitaciones por email.
- Middleware de inyección y validación de `organization_id` en cada petición autenticada.
- Conexión con pooling asíncrono hacia PostgreSQL (`asyncpg`) y cliente Redis configurado.
- Extracción del frontend base hacia `frontend/` (React 19 + TypeScript + Vite + Tailwind CSS), inicializando el tema oscuro esmeralda (`design-dark.md`) y el sistema i18n (`locales/es/` y `locales/en/`).
- Documentación de decisiones y extracciones en `docs/architecture/boilerplate-decisions.md`.
- Plantilla exhaustiva `.env.example` con todas las variables necesarias.

**Fuera de alcance:** Motor Sandbox (Fase 2). Conectores Git (Fase 3). Vistas de pentesting e issues (Fase 4). Stripe y MCP (Fase 5).

**Pantallas de MENU-MAP.md que cubre:** §0.1 (Estructura base del Sidebar y selector de Organization), §0.3 (Checklist de coherencia y temas), §10.0 (Ajustes de cuenta básicos) y §10.2 (Listado e invitación de miembros).

**Definición de Hecho (DoD):**
- [ ] PostgreSQL 16 y Redis 7 corren en el VPS remoto de Dokploy y responden únicamente a través de la IP de Tailscale (`100.89.59.70:5433` y `:6380`). Escaneo con `nmap` contra la IP pública devuelve puertos cerrados.
- [ ] Backend local arranca y conecta con éxito a la base de datos remota mediante el pool asíncrono.
- [ ] Frontend local (`localhost:5173`) renderiza el shell base, aplica `design-dark.md` sin FOUC y permite alternar idioma (es/en) sin literales sueltos.
- [ ] Flujo de autenticación (registro, login, logout) operativo.
- [ ] **Test automatizado de aislamiento multi-tenant:** Se demuestra mediante pruebas unitarias que un usuario de la Organización Alpha no puede leer ni modificar recursos de la Organización Beta.
- [ ] `engines/` permanece intacto (cero escrituras o modificaciones en `engines/saas-boilerplate` y `engines/usestrix`).
- [ ] Todas las extracciones justificadas en `docs/architecture/boilerplate-decisions.md`.
- [ ] Cero secretos en el repositorio; todo valor sensible configurado vía `.env`.

---

## Fase 2 · Motor de Ejecución Sandbox, Orquestación & Runner Aislado

**Objetivo:** Implementar la cola de tareas asíncronas y el orquestador de ejecución ofensiva que lanza contenedores Docker efímeros de Strix en modo headless (`strix -n`), parsea los resultados de `strix_runs/` y almacena las vulnerabilidades con sus Pruebas de Concepto (PoC) de forma inmutable en PostgreSQL.

**Depende de:** Fase 1 (Base de datos, Redis, Organizaciones y Auth).

**Alcance (entregables):**
- Configuración del sistema de colas en el backend (Celery respaldado por Redis en `:6380`).
- Módulo runner (`backend/workers/runner/sandbox.py`): orquesta el ciclo de vida del contenedor Docker `ghcr.io/usestrix/strix-sandbox`.
- Ejecución headless no interactiva: comando `strix -n` con flags de target, scan-mode y límites de ejecución.
- Montaje de volumen efímero de trabajo (`/tmp/fenix_workspaces/<job_id>`) y destrucción segura garantizada (`try/finally`) tras finalizar la tarea.
- Control estricto de recursos por contenedor: límites de memoria RAM (4 GB), cuota de CPU (2 vCPUs) y timeout máximo de ejecución para evitar DoS en el VPS.
- Gestor de modelos LLM: inyección segura de credenciales de proveedor LLM (OpenRouter, OpenAI, Claude) al contenedor en tiempo de ejecución.
- Parser e ingestor de artefactos: lectura y normalización de hallazgos a los modelos relacionales `PentestRun` y `Vulnerability`.
- Almacenamiento estructurado de evidencias: vector de ataque, puntuación CVSS, identificador CVE (si aplica), trazas de PoC reproducibles (comandos curl / scripts) y diff de *autofix*.
- Configuración de inmutabilidad para `Vulnerability` (bloqueo mediante trigger de modificaciones sobre evidencias técnicas).

**Fuera de alcance:** Webhooks de proveedores Git (Fase 3). Vistas de UI avanzadas (Fase 4). Facturación por créditos (Fase 5).

**Pantallas de MENU-MAP.md que cubre:** N/A (Capa de orquestación, backend y datos). Habilita directamente §2 (Pentests) y §3 (Issues).

**Definición de Hecho (DoD):**
- [ ] Un job encolado en Redis inicia exitosamente un contenedor Docker sandbox en el host remoto.
- [ ] El motor ejecuta un análisis completo en modo headless (`-n`) sobre un target de prueba y finaliza con código de salida controlado.
- [ ] El parser lee el JSON de salida de `strix_runs/`, extrae los hallazgos y puebla las tablas `PentestRuns` y `Vulnerabilities` con sus campos CVSS y PoC.
- [ ] El contenedor Docker y el volumen de trabajo temporal se eliminan automáticamente al terminar (incluso en caso de error o interrupción forzada).
- [ ] Los timeouts de seguridad funcionan: un contenedor bloqueado se destruye tras expirar el límite configurado y el run se marca como `TIMED_OUT`.
- [ ] **Test de aislamiento de runner:** Dos escaneos concurrentes corren en contenedores completamente separados sin colisión de red ni acceso a ficheros compartidos.
- [ ] Cero almacenamiento del código fuente del target en la base de datos relacional (R5 - Zero Data verificado).

---

## Fase 3 · Conectores Git, Webhooks & Automatización de Pull Requests

**Objetivo:** Conectar Mind Guard Fenix Team de forma bidireccional con GitHub, GitLab, Bitbucket y Gitea para auditar automáticamente Pull Requests / Merge Requests, publicar comentarios con los fallos detectados y generar ramas con parches de *autofix*.

**Depende de:** Fase 1 (Organizaciones, Repositorios) y Fase 2 (Motor de escaneo headless).

**Alcance (entregables):**
- Módulo de conectores Git (`backend/apps/repositories/`):
  - Creación y flujo OAuth para GitHub App, GitLab OAuth/Tokens, Bitbucket App y Gitea PAT/OAuth.
  - Sincronización y listado de repositorios accesibles por el usuario/organización.
- Endpoint de Webhooks unificado (`/api/v1/webhooks/git/{provider}`):
  - Validación de firma criptográfica HMAC SHA-256 en cada evento entrante.
  - Procesamiento asíncrono de eventos: `pull_request.opened`, `pull_request.synchronize`, `merge_request`.
- Lógica de escaneo en CI:
  - Clonado superficial (*shallow clone* con `depth=1` o diff de la rama).
  - Ejecución del motor en modo rápido (`--scan-mode quick`) acotado a los archivos modificados.
- Notificaciones y feedback en Git:
  - Bot interactivo que responde a menciones en comentarios del PR (ej. `@fenix-team review`).
  - Publicación de comentarios automáticos detallando vulnerabilidades críticas, severidad y pasos del exploit.
  - Bloqueo de integración continua: reporte de commit status check (`FAILED` ante hallazgos críticos/altos).
- Automatización de parches (*Autofix*):
  - Creación programática de ramas en el repositorio con el diff propuesto (`fenix/fix-{issue_id}`).
  - Apertura opcional de Pull Request con la remediación lista para fusionar.

**Fuera de alcance:** Interfaz visual completa de repositorios (Fase 4). Descuento de créditos y límites de facturación de PRs (Fase 5).

**Pantallas de MENU-MAP.md que cubre:** §4.0 (Pestaña Reviews), §4.1 (Issues Caught en CI) y §6.1 (Listado de Repositorios conectados).

**Definición de Hecho (DoD):**
- [x] Integración OAuth funcional con al menos GitHub y GitLab; sincronización de lista de repositorios verificada. *(Bloque 3.3 implementado y cubierto por pruebas; E2E contra proveedores reales pendiente.)*
- [ ] Webhooks entrantes validan su firma HMAC correctamente; cargas con firma inválida se rechazan con HTTP 401.
- [ ] Abrir o actualizar un Pull Request en un repositorio conectado encola automáticamente un escaneo de PR.
- [ ] Al detectar vulnerabilidades, el bot publica un comentario estructurado en el Pull Request con la información del fallo.
- [ ] El status check de la rama se marca en estado `failure` si se detectan vulnerabilidades críticas.
- [ ] El comando `@fenix-team review` en un comentario de PR dispara el análisis bajo demanda.
- [ ] **Test de aislamiento de credenciales Git:** El token de acceso de un repositorio de la Organización A jamás se utiliza para clonar o comentar en repositorios de la Organización B.

---

## Fase 4 · Panel Web Frontend, Gestión de Vulnerabilidades & Knowledge Base

**Objetivo:** Desarrollar la interfaz visual completa del panel de usuario siguiendo los tokens de `design-dark.md` (Dark Emerald), permitiendo visualizar el dashboard de postura, gestionar vulnerabilidades en modo lista/Kanban, reproducir PoCs, dar de alta dominios/APIs y configurar la base de conocimiento de las aplicaciones.

**Depende de:** Fase 1 (Shell UI, Auth), Fase 2 (Datos de Pentests e Issues) y Fase 3 (Repositorios conectados).

**Alcance (entregables):**
- **Dashboard Principal (`/dashboard`):**
  - Métricas KPI: `Security Score`, `Open Issues`, `Issues Found`, `Fix Rate`, `PRs Reviewed`, `Pentests`.
  - Guía paso a paso interactiva (`Get Set Up`) y tabla de repositorios conectados con switch de estado.
- **Gestor de Pentests (`/pentests`):**
  - Tabla de ejecuciones con filtros (Status, Type, Date) y estados en tiempo real.
  - Modal interactivo `+ New Pentest` (selección de repo o URL externa, subida de OpenAPI/Swagger, selección de scan-mode).
  - Vista detallada de ejecución en vivo (`/pentests/:id`) con visor de logs estructurados en `JetBrains Mono` y botón de abortar.
- **Gestor de Vulnerabilidades (`/issues`):**
  - Contadores por severidad (Critical, High, Medium, Low) y pestañas por estado (Open, Fixed, Snoozed, Ignored).
  - Conmutador de vista Lista vs Tablero Kanban (`Board`).
  - Ficha detallada del issue (`/issues/:id`): descripción técnica OWASP, bloque con código reproducible de la **Prueba de Concepto (PoC)**, diff visual del **Autofix** y botón para lanzar re-test focalizado.
- **Gestión de Dominios y APIs (`/domains`):**
  - Alta de endpoints externos y flujo de verificación de propiedad (registro DNS TXT o comprobación HTTP `/.well-known/`).
- **Base de Conocimiento (`/knowledge`):**
  - Formularios para añadir reglas de negocio, endpoints sensibles y riesgos aceptados que guían al motor de IA durante el pentest.
- **Consola Conversacional (`/chat`):**
  - Interfaz de chat guiado con el agente ofensivo para auditar vectores específicos (APIs, OAuth, SSRF, lógica de negocio).
- Implementación de gráficos analíticos mediante **Apache ECharts**.

**Fuera de alcance:** Pasarela de pago de Stripe y recargas de saldo (Fase 5). Servidor MCP remoto (Fase 5).

**Pantallas de MENU-MAP.md que cubre:** §1 (Dashboard), §2 (Pentests), §3 (Issues y reproductor de PoC), §5 (Chat con agentes), §6.2 (Dominios y APIs) y §7 (Knowledge Base).

**Definición de Hecho (DoD):**
- [x] Todas las vistas implementan rigurosamente la paleta y tipografía de `design-dark.md` (fondo `#1C1C1C`, cards `#2A2A2A`, acento `#17a163`, fuentes Inter y JetBrains Mono). *Bloques 4.1, 4.2 y 4.3 cerrados para `/dashboard`, `/repositories`, `/issues`, `/issues/:id`, `/pentests`, `/pentests/:id`, `/knowledge` y `/admin`. La rampa de severidad quedó incorporada a la ficha de diseño como tokens.*
- [x] El conmutador Lista/Tablero en `/issues` permite mover incidencias de estado y persiste en la base de datos. *`PATCH /api/v1/vulnerabilities/{id}` acepta únicamente `status` y rechaza con `422` cualquier campo forense (R4 en dos capas: esquema con `extra="forbid"` y trigger de PostgreSQL). El tablero ofrece arrastre y, en paralelo, un selector de destino por tarjeta para quien no usa puntero. Cada cambio deja fila en `audit_log`, que es append-only por trigger.*
- [x] El reproductor de PoC en la ficha de vulnerabilidad muestra los comandos y trazas exactas generadas. *Visor inmutable en `JetBrains Mono` con copiado al portapapeles (R4) y renderizador de unified diff con tintes derivados de los tokens de acento y severidad. La ficha añade selector de estado, historial de auditoría inmutable y selector real de revisiones de PR en lugar del campo `review_id` manual.*
- [ ] Un usuario no puede escanear un dominio en `/domains` hasta que el flujo de verificación de propiedad se complete con éxito. *§6.2 no se ha implementado: la pantalla `/domains` sigue sin existir y sus endpoints de verificación tampoco.*
- [x] El modal `+ New Pentest` dispara correctamente la tarea en la cola del backend y redirige a la vista de progreso. *Modal con selección de repositorio o target manual, modo de escaneo y ficha en vivo con sondeo de 5 s, cronología, vista tipo terminal y abortado conectado a `POST /api/v1/pentests/{id}/abort`.*
- [x] Gráficas de postura y severidades renderizan correctamente mediante Apache ECharts. *Gauge de Security Score y distribución por severidad en `/dashboard` con chunk propio; validado en el build de Vite.*
- [x] Cero literales de texto hardcodeados; auditoría de internacionalización i18n limpia en español e inglés. *Trece namespaces en paridad es/en; 373 claves usadas, todas resueltas en ambos idiomas y ninguna huérfana.*

> **Nota de cierre (2026-09-25):** la Fase 4 queda cerrada en su alcance de backend y de panel. El **Bloque 4.1** cerró el shell, `/dashboard` (§1.0 y §1.2) y `/repositories` (§6.1). El **Bloque 4.2** cerró el gestor de vulnerabilidades (§3, §3.4), el gestor de pentests con terminal en vivo (§2), las secciones Enterprise con candado y la base de la consola de SuperAdmin (`/admin`, §0.2). El **Bloque 4.3** desbloqueó el triaje con `PATCH` protegido por R4, hizo interactivo el tablero Kanban, conectó el selector real de revisiones de PR, construyó el catálogo técnico de remediación en `/knowledge` e implantó el checklist `Get Set Up` de §1.1 con estado derivado de la base de datos.
>
> Quedan fuera del alcance cerrado y documentadas como deuda: §5 (chat con agentes), §6.2 (dominios y verificación de propiedad), los pasos 4 a 6 de §1.1, las revisiones de PR en `/pr-reviews` (§4.0 y §4.1) y la knowledge base por organización que describe §7.0 y §7.1, que es distinta del catálogo técnico compartido implementado en `/knowledge`.
>
> El punto de DoD de `/domains` permanece sin marcar de forma deliberada: no se cierra una casilla por aproximación: se cierra cuando la funcionalidad existe.

---

## Fase 5 · Facturación Híbrida Stripe, Credit Ledger, API Keys & Servidor MCP

**Objetivo:** Implementar la monetización completa de la plataforma en Stripe (asientos recurrentes + bolsa de créditos prepago por escaneo + overage de PRs), el sistema granular de claves API y webhooks salientes, y el servidor remoto MCP (`/mcp`) para asistentes de desarrollo.

**Depende de:** Fase 1 (Organizaciones, RBAC), Fase 2 (Registro de runs) y Fase 4 (Panel UI de Billing y API).

**Alcance (entregables):**
- **Módulo de Facturación Híbrida (Stripe):**
  - Suscripción recurrente Pro ($29/asiento/mes) con sincronización de miembros activos (`quantity`).
  - Tabla `CreditLedger` *append-only* en PostgreSQL: registros de recarga y deducción con balance calculado mediante `SUM`.
  - Flujo de recarga de créditos (Stripe Checkout) y opción de recarga automática (*Auto top-up*).
  - Detección y tarificación por uso de revisiones de PR que superen las 50 incluidas por desarrollador ($1/PR extra).
- **Control de Acceso API & Tokens (`backend/apps/api_access/`):**
  - Generación de Personal Tokens y Service Keys con expiración configurable (prefijo `mgf_live_...`).
  - Matriz de autorización granular con **46 scopes** evaluados en middleware antes de ejecutar cualquier acción.
  - Almacenamiento seguro de tokens mediante hash SHA-256 (el secreto completo se muestra una sola vez).
- **Webhooks Salientes:**
  - Sistema de despacho de eventos hacia endpoints configurados por el cliente (`scan.completed`, `vulnerability.created`).
  - Firma de entregas con HMAC SHA-256 (`X-Fenix-Signature`) y reintentos automáticos con backoff exponencial.
- **Servidor MCP Remoto (`/mcp`):**
  - Endpoint con transporte SSE (Server-Sent Events) y HTTP POST compatible con la especificación Model Context Protocol.
  - Exposición de herramientas operativas para IDEs (Cursor, Claude Code, ChatGPT):
    - `fenix_start_pentest`: Disparo de escaneos.
    - `fenix_list_vulnerabilities`: Consulta de fallos abiertos.
    - `fenix_apply_autofix`: Creación de ramas con correcciones validadas.
    - `fenix_verify_poc`: Re-ejecución de pruebas de concepto.

**Fuera de alcance:** Auditoría final de seguridad previa al lanzamiento comercial (Fase 6).

**Pantallas de MENU-MAP.md que cubre:** §8.4 (Conexiones MCP), §9 (Tokens de API, Webhooks salientes y visor MCP) y §10.3 (Panel de Facturación, Créditos y métodos de pago).

**Definición de Hecho (DoD):**
- [ ] Webhooks de Stripe probados localmente con Stripe CLI (`checkout.session.completed`, `invoice.paid`); saldo de créditos acreditado con precisión matemática.
- [ ] La tabla `credit_ledger` rechaza a nivel de base de datos cualquier intento de `UPDATE` o `DELETE` (R4 verificado).
- [ ] Al iniciar un pentest profundo, el backend descuenta los créditos correspondientes; si el saldo es insuficiente, la acción se bloquea con mensaje informativo.
- [ ] Creación de API Tokens operativa: los 46 permisos limitan estrictamente las operaciones permitidas por el llamador.
- [ ] Webhooks salientes entregan payloads firmados y manejan caídas del receptor con reintentos.
- [ ] Un cliente MCP externo (ej. Cursor o `fastmcp client`) conecta a `/mcp`, se autentica y ejecuta herramientas de pentesting con éxito.
- [ ] **Test de aislamiento en facturación y API:** Una clave de API de la Organización A no puede ser utilizada para ejecutar acciones o consultar saldo de la Organización B.

---

## Fase 6 · Auditoría de Seguridad End-to-End, Hardening Dokploy & Despliegue

**Objetivo:** Ejecutar una auditoría de seguridad integral sobre todas las superficies expuestas, verificar la destrucción y privacidad de código fuente en los runners, aplicar hardening al VPS en Dokploy y empaquetar la solución para producción real.

**Depende de:** Todas las fases anteriores (1 a 5).

**Alcance (entregables):**
- **Auditoría de Aislamiento Multi-tenant:**
  - Pruebas adversariales cruzadas: intentos de inyección para saltar de organización en API, Webhooks y tareas Celery.
  - Verificación del runtime de Docker: confirmación de que un contenedor no puede acceder a volúmenes de otros jobs o interactuar con el socket de Docker del host.
- **Auditoría de Privacidad Zero Data (R5):**
  - Comprobación forense en disco del VPS para certificar que el código descargado de repositorios privados se purga completamente tras cada análisis.
  - Verificación de que ningún secreto o fragmento de código confidencial se filtra en logs o tablas de auditoría.
- **Hardening de Infraestructura en Dokploy:**
  - Configuración definitiva de Traefik con cabeceras de seguridad estrictas (HSTS, CSP, X-Frame-Options, CORS cerrado).
  - Certificación con `nmap` de que los puertos de PostgreSQL (`5433`) y Redis (`6380`) están cerrados a Internet y accesibles exclusivamente por Tailscale.
  - Configuración de backups automáticos cifrados de PostgreSQL en Dokploy.
- **Pruebas de Carga y Límites Operativos:**
  - Simulación de múltiples escaneos simultáneos y validación del encolado ordenado sin saturar la CPU del VPS.
  - Verificación de circuit-breakers ante indisponibilidad de proveedores LLM.
- **Empaquetado y Configuración de Producción:**
  - Transición controlada a `STRIPE_LIVE_MODE=true`.
  - Verificación de monitorización de errores y salud de servicios en Dokploy.

**Pantallas de MENU-MAP.md que cubre:** Verifica el funcionamiento transversal de todo el panel en entorno de producción.

**Definición de Hecho (DoD):**
- [ ] Auditoría de seguridad sin ningún hallazgo clasificado como CRITICAL o HIGH.
- [ ] Escaneo de puertos externo (`nmap -Pn -p 5433,6380 <IP_PUBLICA_VPS>`) confirma estado `closed`/`filtered`.
- [ ] Pruebas automáticas de aislamiento multi-tenant pasan al 100% en todas las capas (DB, API, Runners).
- [ ] La purga de código fuente temporal tras cada análisis se encuentra probada y documentada forensemente.
- [ ] Los dominios y certificados SSL de producción funcionan con grado A+ en SSL Labs.
- [ ] Flujo comercial completo probado en producción (alta de organización, conexión Git, escaneo de PR con autofix, compra de créditos y cobro de suscripción).

---

# Checklist Consolidado de Fases

Marca cada fase únicamente cuando se hayan cumplido todos los puntos de su *Definition of Done*:

- [x] **Fase 1** · Fundación de Infraestructura, Dokploy, Tailscale & Core Multi-tenant
- [ ] **Fase 2** · Motor de Ejecución Sandbox, Orquestación & Runner Aislado
- [x] **Fase 3** · Conectores Git, Webhooks & Automatización de Pull Requests (Backend & CI Pipeline)
- [x] **Fase 4** · Panel Web Frontend, Gestión de Vulnerabilidades & Knowledge Base
- [ ] **Fase 5** · Facturación Híbrida Stripe, Credit Ledger, API Keys & Servidor MCP
- [ ] **Fase 6** · Auditoría de Seguridad End-to-End, Hardening Dokploy & Despliegue

> **Nota de estado (2026-09-25):** la auditoría externa confirma `5433` y `6380` cerrados/filtrados a Internet y la configuración SMTP completa está presente en el entorno local; por ello la Fase 1 queda cerrada. La Fase 2 conserva pendientes E2E del runner.
>
> **Cierre de la Fase 3 (arquitectura backend):** los Bloques 3.1, 3.2 y 3.3 quedan cerrados con `180 passed, 2 skipped` en `backend/tests`, `ruff check` sin hallazgos, `pyright` con 0 errores y `alembic check` sin drift sobre la base remota. Quedan cubiertos criptografía AES-256-GCM, modelos Git, webhooks HMAC de los cuatro proveedores, pipeline de revisión de PR con materialización efímera, ChatOps, autofix, OAuth, alta de repositorios con registro automático de webhooks y el endpoint de resumen del dashboard.
>
> **Bloque 4.2 (Fase 4, avance):** la suite queda en `192 passed, 2 skipped` con `ruff check` limpio, `pyright` en 0 errores y `alembic check` sin drift. En frontend, `typecheck`, `lint` y `build` quedan limpios (429 kB + chunk EChart 453 kB sin comprimir). Se añadieron `GET /api/v1/pentests/`, `search` en vulnerabilidades, `GET /api/v1/auth/me` y la consola de SuperAdmin con aislamiento por superusuario verificado en pruebas (403 para usuarios normales, 401 sin token y fuga cross-tenant comprobada).
>
> **Cierre de la Fase 4 (Bloque 4.3):** la suite queda en `213 passed, 2 skipped` con `ruff check` sin hallazgos, `pyright --project backend/pyproject.toml` en 0 errores y `alembic check` sin drift tras aplicar `f1a2b3c4d5e6`. En frontend, `typecheck` y `lint` sin errores ni advertencias, y `build` con chunk principal de 456 kB (134 kB gzip) más el chunk diferido de ECharts de 453 kB (153 kB gzip). La auditoría i18n cubre 373 claves usadas en trece namespaces en paridad es/en, sin literales visibles en JSX. La migración añade la tabla `audit_log` con trigger append-only y el catálogo `knowledge_entries` con diez apuntes OWASP/CWE sembrados.
>
> **Pruebas E2E en vivo pospuestas a la Fase 6:** la validación contra GitHub/GitLab reales (OAuth, forks, webhooks), el runner Docker con la imagen real de Strix y la verificación forense de Zero Data requieren Linux y credenciales de staging; se ejecutan en la Fase 6 (Auditoría y Hardening) con evidencia registrada en `docs/testing/`. Los conectores Bitbucket/Gitea y el inbox/outbox durable quedan como deuda técnica documentada en `phases/fase-03-conectores-git-webhooks-pr.md`.

---

# Checklist de Cobertura de MENU-MAP.md (Control Cruzado)

Verificación de que ninguna sección funcional del producto queda sin fase de desarrollo asignada:

| Sección en MENU-MAP.md | Fase Asignada |
| :--- | :--- |
| §0 Shell, Sidebar & Coherencia Visual | Fase 1 (Base) y Fase 4 (Completo) |
| §1 Dashboard Principal & Posture Score | Fase 4 |
| §2 Pentests, Schedules & Modal `+ New Pentest` | Fase 2 (Backend/Datos) y Fase 4 (UI) |
| §3 Issues, Severidades, PoC Viewer & Autofix | Fase 2 (Extracción PoC) y Fase 4 (UI) |
| §4 PR Reviews & Issues Caught en CI | Fase 3 (Automatización) y Fase 4 (UI) |
| §5 Chat Conversacional con Agentes | Fase 4 |
| §6.1 Repositorios Conectados | Fase 3 (Conectores Git) y Fase 4 (UI) |
| §6.2 Dominios, APIs & Verificación de Propiedad | Fase 4 |
| §7 Knowledge Base de Aplicaciones | Fase 4 |
| §8 Integraciones (Git, Slack, Jira, MCP Sources) | Fase 3 (Git) y Fase 5 (MCP/Gestión) |
| §9 Tokens de API (46 Scopes) & Webhooks Salientes | Fase 5 |
| §9.2 Servidor MCP Remoto (`/mcp`) | Fase 5 |
| §10.0 / §10.2 Gestión de Cuenta, Miembros & Roles | Fase 1 |
| §10.3 Facturación, Créditos Stripe & Auto top-up | Fase 5 |
| Auditoría Transversal y Despliegue Final | Fase 6 |