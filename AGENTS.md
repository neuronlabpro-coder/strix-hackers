# AGENTS.md — Constitución de Ingeniería de Mind Guard Fenix Team

> **Fuente única de verdad de CÓMO se construye.**
> Cualquier agente de codificación o desarrollador humano DEBE leer este archivo completo antes de escribir, modificar o desplegar una sola línea de código.
>
> - **`ARCHITECTURE.md`** define QUÉ es Mind Guard Fenix Team y cómo encajan sus planos. Si algo de aquí lo contradice, manda `ARCHITECTURE.md`.
> - **`MENU-MAP.md`** define QUÉ pantallas existen en el panel. Si una funcionalidad no está ahí, se añade ahí primero, luego se construye.
> - **`ROADMAP.md`** define el ORDEN secuencial de trabajo. Su checklist es la única fuente de estado.
> - **`phases/`** contiene la especificación técnica y la *Definition of Done* detallada de cada fase.
> - **`.agents/`** contiene el catálogo de subagentes especializados, comandos de verificación, hooks y skills operativas.
>
> **IDIOMA:** **SIEMPRE español.** En toda conversación con el responsable, resúmenes de fase, commits y documentación interna. Única excepción: términos técnicos sin traducción natural (nombres de paquetes, comandos de terminal, nombres de variables o flags de CLI).

---

## 0. Identidad del Proyecto

| Campo | Valor |
| :--- | :--- |
| **Producto** | Mind Guard Fenix Team |
| **Descripción** | Plataforma SaaS de Pentesting Autónomo, DevSecOps y Evaluación Ofensiva Continua |
| **Base SaaS** | SaaS Boilerplate (`apptension/saas-boilerplate` en `engines/`, solo lectura) |
| **Motor Ofensivo** | Strix Sandbox Engine (`usestrix/strix` en `engines/`, solo lectura) |
| **Entorno de Datos** | VPS Dokploy Dedicado: PostgreSQL 16 (`100.89.59.70:5433`), Redis 7 (`100.89.59.70:6380`) |
| **Red Segura** | Tailscale Mesh Network (acceso restringido a interfaces `100.x.y.z`) |
| **Diseño Visual** | `design-dark.md` (Dark Emerald · Estética Supabase code-first) |
| **Harness de Agentes** | Subagentes, comandos y hooks gestionados en `.agents/` |

---

## 1. Las 6 Reglas de Oro (NO NEGOCIABLES)

Tienen prioridad absoluta sobre cualquier atajo o preferencia personal. Violar una regla invalida el trabajo aunque el código compile.

### 🥇 R1 — Cero Hardcoding e i18n Estricto
Nada configurable vive en el código fuente: modelos LLM, prompts de contexto, cuotas de escaneo, precios de planes, claves API o timeouts. Todo reside en la base de datos o en variables de entorno (`.env`).
* **Corolario i18n:** Ningún texto visible en pantalla puede ser un literal hardcodeado en componentes React; todo pasa por `t('clave')` mediante `i18next` con soporte para español e inglés (`frontend/src/locales/{es,en}/`).

### 🥈 R2 — `engines/` es ESTRICTAMENTE SOLO LECTURA
Los repositorios ubicados en `engines/` (`usestrix/strix` y `saas-boilerplate`) son de **solo lectura**.
* **Prohibido** modificar archivos dentro de `engines/`.
* **Prohibido** importar en runtime rutas relativas directas desde `engines/`.
* El código reutilizable (conectores, utilidades, esquemas de BD, modelos RBAC) se extrae y adapta hacia `backend/` o `frontend/`, documentando su origen en `docs/architecture/boilerplate-decisions.md`.

### 🥉 R3 — Aislamiento Multi-tenant Absoluto (Datos y Ejecución)
* **A nivel relacional:** Toda consulta a tablas privadas debe filtrar obligatoriamente por `organization_id`.
* **A nivel de ejecución ofensiva:** Cada análisis de Mind Guard Fenix Team se ejecuta en un **contenedor Docker efímero e independiente** (`ghcr.io/usestrix/strix-sandbox`). Ningún proceso de pentest comparte memoria, socket proxy ni espacio de trabajo en disco con el de otro cliente.

### R4 — Inmutabilidad Financiera y de Hallazgos
* La tabla `credit_ledger` es **append-only**: solo acepta operaciones `INSERT`. El balance disponible se calcula siempre mediante `SUM(delta_credits)` en tiempo real.
* Los registros de vulnerabilidades detectadas, los logs de ejecución y los artefactos de Pruebas de Concepto (PoC) son inmutables desde la interfaz de usuario para garantizar su validez jurídica y de auditoría (SOC 2, ISO 27001).

### R5 — Zero Data y Privacidad de Código Fuente
* El código fuente clonado para revisiones en repositorios vive únicamente dentro del volumen temporal montado en el contenedor de escaneo (`/tmp/fenix_workspaces/<job_id>`).
* Al finalizar el análisis, el directorio de trabajo se purga de forma segura.
* La base de datos central de la plataforma **nunca persiste el código fuente completo del cliente**, limitándose a almacenar: identificadores de archivo, números de línea afectados, pasos del PoC, score CVSS y el diff de parche (*autofix*).

### R6 — Datos Remotos en Dokploy, Código Local por Tailscale
* Los servicios centrales de datos (PostgreSQL en puerto `5433` y Redis en puerto `6380`) residen en el VPS gestionado por Dokploy.
* El frontend y el backend de desarrollo corren en la máquina local con hot-reload conectándose a los datos remotos exclusivamente a través de la IP de Tailscale (`100.89.59.70`).
* Ningún puerto de base de datos se expone al Internet público (`0.0.0.0`). Los contenedores del proyecto anterior de Mindguard permanecen intactos y aislados.

---

## 2. Stack Tecnológico Estándar

| Capa | Tecnología |
| :--- | :--- |
| **Backend API** | Python 3.12+ (FastAPI o Django REST) + Uvicorn |
| **Orquestador / Colas** | Redis 7 (`:6380`) + Celery / Celery Beat (ejecución asíncrona de jobs) |
| **Frontend** | React 19 + TypeScript + Vite + Tailwind CSS + shadcn/ui |
| **Visualización** | Apache ECharts (única biblioteca para métricas, dashboards y postura) |
| **Base de Datos** | PostgreSQL 16 (`:5433`, esquema relacional multi-tenant indexado) |
| **Motor de Pentesting** | Strix Headless CLI (`strix -n`) en contenedor Docker Sandbox efímero |
| **Localización** | i18next + react-i18next (namespaces por módulo) |
| **Facturación** | Stripe Billing (Suscripciones) + Stripe Checkout (Créditos) |
| **Gestión de Infraestructura** | Dokploy sobre VPS Linux + Tailscale Mesh Network |

---

## 3. Estructura Canónica de Directorios

```text
MindGuardFenixTeam/
├── .agents/                   # Harness de agentes, comandos, hooks y skills
│   ├── agents/                # Subagentes especializados (security-reviewer, etc.)
│   ├── commands/              # Comandos operativos (/code-review, /security-scan, etc.)
│   ├── hooks/                 # Hooks de verificación (check-gstack, etc.)
│   ├── rules/                 # Reglas específicas de contexto
│   └── skills/                # Definición de habilidades cargables por fase
│
├── backend/
│   ├── core/                  # Configuración, DB pool, seguridad, middleware y RBAC
│   ├── apps/
│   │   ├── organizations/     # Tenants, usuarios, roles e invitaciones
│   │   ├── repositories/      # Conexiones Git (GitHub, GitLab, Bitbucket, Gitea)
│   │   ├── pentests/          # Ciclo de vida de escaneos, dominios y targets
│   │   ├── vulnerabilities/   # Gestor de issues, CVSS, CVEs y PoCs reproducibles
│   │   ├── billing/           # Credit ledger (append-only), Stripe webhooks
│   │   └── knowledge/         # Reglas de negocio y contexto de aplicaciones
│   ├── workers/               # Tareas Celery (despacho de Docker, parseo de resultados)
│   ├── mcp/                   # Servidor MCP remoto (/mcp vía SSE/HTTP)
│   └── tests/                 # Pruebas unitarias, integración y aislamiento multi-tenant
│
├── frontend/
│   ├── src/
│   │   ├── app/               # Enrutador, providers y bootstrap de i18n
│   │   ├── components/        # Componentes UI (Button, Card, Modal, Table, Badge)
│   │   ├── charts/            # Componentes gráficos basados en Apache ECharts
│   │   ├── features/          # Vistas (Dashboard, Pentests, Issues, Repositories, Billing)
│   │   ├── locales/           # Archivos de traducción: es/ y en/
│   │   └── styles/            # Tokens CSS basados en design-dark.md
│   └── tests/
│
├── engines/                   # ⛔ SOLO LECTURA
│   ├── usestrix/              # Clon de referencia del motor Strix
│   └── saas-boilerplate/      # Clon de referencia del boilerplate SaaS
│
├── infra/
│   └── dokploy/               # docker-compose.yml para PostgreSQL (5433) y Redis (6380)
│
├── phases/                    # Especificaciones ejecutables fase por fase
│   ├── fase-01-fundacion-infra-dokploy.md
│   └── ...
│
├── docs/                      # Decisiones de arquitectura y diagramas
├── ARCHITECTURE.md            # Qué es el sistema (Autoridad técnica)
├── AGENTS.md                  # Este documento (Cómo se construye)
├── MENU-MAP.md                # Qué pantallas existen (Autoridad de UI)
├── ROADMAP.md                 # Orden de trabajo y estado de avance
├── design-dark.md             # Tokens de diseño visual
└── .env.example               # Catálogo exhaustivo de variables de entorno
```

---

## 4. Subagentes y Comandos del Harness (`.agents/`)

El entorno cuenta con herramientas y roles modulares en `.agents/` para auditar y asistir en la construcción:

### 4.1 Subagentes Especializados (`.agents/agents/`)
* **`security-reviewer`**: Auditoría de autenticación, RBAC, prevención de fugas cross-tenant, secretos y criptografía AES-256-GCM.
* **`silent-failure-hunter`**: Análisis de flujos asíncronos en Celery, manejo de excepciones en contenedores y control de timeouts.
* **`database-reviewer`**: Revisión de índices relacionales, claves foráneas y triggers de inmutabilidad en PostgreSQL.
* **`fastapi-reviewer` / `python-reviewer`**: Verificación de tipado estricto con Pydantic y estándares en el backend.
* **`react-reviewer` / `typescript-reviewer`**: Validación de hooks, ciclo de vida en React 19, TS estricto y cero `any`.
* **`performance-optimizer`**: Optimización de consultas SQL, gestión de conexiones y cuotas de memoria en Docker.
* **`code-architect` / `code-reviewer`**: Guardián de la arquitectura y filtro final de calidad antes de cerrar cada entrega.

### 4.2 Comandos Operativos (`.agents/commands/`)
* `/security-scan`: Ejecuta auditoría de seguridad sobre los cambios de la fase.
* `/code-review`: Filtro general de calidad y detección de regresiones.
* `/test-coverage`: Ejecución de suite de tests y reporte de cobertura.
* `/checkpoint`: Registro de estado de avance e integridad del repositorio.
* `/quality-gate`: Validación global contra la *Definition of Done*.

---

## 5. Reglas de Salida de Código

1. **Código para Producción, no Prototipos:** Prohibidos comentarios `TODO`, `FIXME` o simulaciones temporales en lógica de seguridad, aislamiento o facturación.
2. **Consultas Seguras Anti-Inyección:** Cero concatenación de cadenas en SQL. Todas las queries deben usar ORM o consultas parametrizadas.
3. **Tipado Estricto:** Pydantic en Python con validación estricta de esquemas; TypeScript en modo estricto en el frontend sin uso de `any`.
4. **Manejo Explícito de Excepciones:** Los workers que lanzan contenedores Docker deben implementar captura de señales, timeouts con eliminación garantizada del contenedor (`try/finally`) y logging estructurado.
5. **Cero Secretos en el Árbol de Git:** Ningún archivo `.env`, clave de Stripe, token de GitHub o clave de LLM se comitea. Toda credencial se documenta en `.env.example` con valores ficticios.

---

## 6. Protocolo de Revisión Especializada (Paso 8 - Previo al Cierre de Fase)

Antes de marcar cualquier fase como completada en `ROADMAP.md`, se debe invocar a los subagentes pertinentes de `.agents/agents/`:

1. **Seguridad y Aislamiento:** Invocación de `security-reviewer`. Certificar que el tenant A no puede ver recursos del tenant B.
2. **Resiliencia de Procesos:** Invocación de `silent-failure-hunter`. Certificar que no hay promesas o tareas Celery que queden colgadas en `RUNNING`.
3. **Salud de Base de Datos:** Invocación de `database-reviewer`. Verificar índices compuestos y restricciones de inmutabilidad.
4. **Coherencia Visual:** Invocación de `react-reviewer`. Certificar cero literales de texto y adhesión a `design-dark.md`.

Cualquier hallazgo clasificado como **CRITICAL** o **HIGH** debe corregirse obligatoriamente antes de dar la fase por concluida.

---

## 7. Flujo de Trabajo del Agente

1. Leer `AGENTS.md` (este archivo) y `ARCHITECTURE.md`.
2. Consultar `ROADMAP.md` e identificar la **única fase activa**.
3. Abrir el archivo correspondiente en `phases/` (ej. `phases/fase-01-fundacion-infra-dokploy.md`) y revisar su alcance.
4. Cargar las skills requeridas desde `.agents/skills/` y ejecutar las tareas respetando las 6 Reglas de Oro.
5. Ejecutar los tests automatizados y verificar los puntos de la *Definition of Done*.
6. Ejecutar el protocolo de revisión especializada con los subagentes de `.agents/agents/`.
7. Marcar la fase como completada en `ROADMAP.md` y esperar la autorización para comenzar la siguiente.