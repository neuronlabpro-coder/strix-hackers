# FASE 1 — Fundación de Infraestructura, Dokploy, Tailscale & Core Multi-tenant

> **Documento de especificación ejecutable.** Define el alcance técnico detallado, los pasos de implementación, los contratos de datos, la integración con el harness de `.agents/` y la *Definition of Done* (DoD) de la Fase 1 para **Mind Guard Fenix Team**.
>
> **Estado:** `[ ]` Pendiente de ejecución  
> **Dependencias previas:** Ninguna (fase raíz). Requiere contenedores `fenix-postgres` y `fenix-redis` activos en Dokploy vía Tailscale (`100.89.59.70`).  
> **Autoridades que rigen esta fase:** `ARCHITECTURE.md` (§1, §4, §5), `AGENTS.md` (Reglas de Oro R1, R2, R3 y R6) y el catálogo de subagentes en `.agents/`.

---

## 1. Objetivo de la Fase

Validar la conectividad del entorno de desarrollo local (Windows / VS Code) con la infraestructura de datos dedicada desplegada en Dokploy sobre puertos aislados (`fenix-postgres` en `100.89.59.70:5433` y `fenix-redis` en `100.89.59.70:6380`), garantizando que no se interfiera con los servicios preexistentes de Mindguard.

Posteriormente, extraer y adaptar desde `engines/saas-boilerplate` el núcleo de backend y frontend para arrancar en local un entorno de desarrollo con recarga rápida (*hot-reload*), autenticación de usuarios, gestión de organizaciones, soporte de localización i18n (`locales/es/` y `locales/en/`), estética oscura esmeralda (`design-dark.md`) y **aislamiento multi-tenant estricto** garantizado por tests automatizados.

---

## 2. Decisiones de Arquitectura e Infraestructura

1. **R6 Estricta (Datos remotos en VPS, código local por Tailscale):**
   * PostgreSQL 16 y Redis 7 corren en el VPS dentro de contenedores independientes:
     * Base de datos: `100.89.59.70:5433` (DB: `fenix_team_db`, User: `fenix_admin`).
     * Caché y colas: `100.89.59.70:6380`.
   * El stack previo de Mindguard (`5432`, `6379`, `5050`, Gitea) permanece intacto y sin modificaciones.
   * El backend API (`localhost:8000`) y el frontend (`localhost:5173`) corren en la máquina local.
   * **Seguridad de red:** Los puertos `5433` y `6380` están enlazados exclusivamente a la interfaz privada de Tailscale (`100.89.59.70`). Escaneos públicos con `nmap` contra la IP pública del VPS confirmarán los puertos como `closed`/`filtered`.
2. **R2 Estricta (Engines solo lectura):**
   * El código de `engines/saas-boilerplate` y `engines/usestrix` solo se lee para extraer patrones de autenticación, middleware de organizaciones y clientes de conexión. Prohibido modificar o ejecutar directamente dentro de `engines/`.
   * Toda extracción debe registrarse en `docs/architecture/boilerplate-decisions.md`.
3. **R3 Estricta (Aislamiento relacional):**
   * El modelo `Organization` es la entidad raíz de multi-tenancy.
   * Todo modelo dependiente de la organización (`Membership`, `Invitation`, y en fases posteriores `Repository`, `PentestRun`, `Vulnerability`) tiene clave foránea `organization_id` indexada y no nula.

---

## 3. Desglose de Tareas de Implementación

### Tarea 1.1 · Verificación de Conectividad y Configuración Local
1. Comprobar desde la terminal local que los servicios de Dokploy responden por Tailscale:
   ```powershell
   Test-NetConnection -ComputerName 100.89.59.70 -Port 5433
   Test-NetConnection -ComputerName 100.89.59.70 -Port 6380
   ```
2. Crear el archivo `.env` en la raíz del proyecto basándose en `.env.example`:
   ```env
   ENVIRONMENT=development
   DEBUG=True
   SECRET_KEY=fenix_dev_secret_key_change_in_production_8947291847192

   # PostgreSQL Remoto (Dokploy VPS vía Tailscale)
   DB_HOST=100.89.59.70
   DB_PORT=5433
   DB_NAME=fenix_team_db
   DB_USER=fenix_admin
   DB_PASSWORD=<configurar_en_dotenv>
   DATABASE_URL=postgresql+asyncpg://<DB_USER>:<DB_PASSWORD>@100.89.59.70:5433/<DB_NAME>

   # Redis Remoto (Dokploy VPS vía Tailscale)
   REDIS_HOST=100.89.59.70
   REDIS_PORT=6380
   REDIS_PASSWORD=<configurar_en_dotenv>
   REDIS_URL=redis://:<REDIS_PASSWORD>@100.89.59.70:6380/0

   # Criptografía interna (R3)
   GIT_ENCRYPTION_KEY=clave_aes256_32_bytes_de_ejemplo_para_dev!
   ```

---

### Tarea 1.2 · Estructura del Backend y Gestión de Dependencias
1. Estructurar el directorio `backend/`:
   ```text
   backend/
   ├── core/
   │   ├── config.py           # Configuración Pydantic validada desde .env
   │   ├── database.py         # Engine SQLAlchemy asíncrono (asyncpg) con pooling
   │   ├── redis.py            # Cliente Redis asíncrono y healthcheck
   │   ├── security.py         # Hash de contraseñas (bcrypt/argon2) y tokens JWT
   │   └── middleware.py       # Inyección de OrganizationContext en el request
   ├── apps/
   │   └── organizations/
   │       ├── models.py       # Organization, User, Membership, Invitation
   │       ├── schemas.py      # Esquemas Pydantic estrictos
   │       ├── services.py     # Lógica de creación de orgs e invitaciones
   │       └── router.py       # Endpoints REST de autenticación y organizaciones
   ├── migrations/             # Migraciones versionadas con Alembic
   │   ├── env.py
   │   └── versions/
   ├── tests/
   │   ├── conftest.py
   │   └── test_multitenant_isolation.py
   ├── pyproject.toml          # Dependencias gestionadas por uv o poetry
   └── main.py                 # Punto de entrada FastAPI / Uvicorn
   ```
2. Instalar dependencias en el entorno virtual (`.venv`):
   ```text
   fastapi>=0.115.0
   uvicorn[standard]>=0.30.0
   pydantic-settings>=2.4.0
   sqlalchemy[asyncio]>=2.0.30
   asyncpg>=0.29.0
   alembic>=1.13.0
   redis>=5.0.0
   passlib[bcrypt]>=1.7.4
   python-jose[cryptography]>=3.3.0
   pytest>=8.3.0
   pytest-asyncio>=0.24.0
   httpx>=0.27.0
   ```

---

### Tarea 1.3 · Núcleo de Base de Datos y Seguridad

1. **`backend/core/config.py`:**
   * Implementar clase `Settings` con `pydantic-settings` para validar todas las variables de `.env` al arrancar. Cero valores sensibles hardcodeados.
2. **`backend/core/database.py`:**
   * Motor SQLAlchemy con `create_async_engine(settings.DATABASE_URL)`.
   * Opciones de pool para absorber latencias de Tailscale:
     * `pool_size=10`
     * `max_overflow=20`
     * `pool_pre_ping=True` (comprobación de salud antes de despachar conexión)
     * `pool_recycle=3600`
   * Generador de sesiones: `get_db()`.
3. **`backend/core/security.py`:**
   * `hash_password(password: str) -> str`
   * `verify_password(plain_password: str, hashed_password: str) -> bool`
   * `create_access_token(data: dict, expires_delta: timedelta | None = None) -> str`

---

### Tarea 1.4 · Modelos de Datos Multi-tenant

Implementar el esquema relacional en `backend/apps/organizations/models.py`:

```python
import uuid
from datetime import datetime
from enum import Enum
from sqlalchemy import Column, String, Boolean, Float, DateTime, ForeignKey, Enum as SQLEnum, Index
from sqlalchemy.dialects.postgresql import UUID
from backend.core.database import Base

class RoleEnum(str, Enum):
    ADMIN = "admin"
    MEMBER = "member"

class PlanTierEnum(str, Enum):
    FREE = "FREE"
    PRO = "PRO"
    ENTERPRISE = "ENTERPRISE"

class Organization(Base):
    __tablename__ = "organizations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(128), nullable=False)
    slug = Column(String(128), nullable=False, unique=True, index=True)
    plan_tier = Column(SQLEnum(PlanTierEnum), nullable=False, default=PlanTierEnum.PRO)
    credit_balance = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), nullable=False, unique=True, index=True)
    hashed_password = Column(String(255), nullable=False)
    full_name = Column(String(255), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    is_superuser = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

class Membership(Base):
    __tablename__ = "memberships"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(SQLEnum(RoleEnum), nullable=False, default=RoleEnum.MEMBER)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_membership_org_user", "organization_id", "user_id", unique=True),
    )

class Invitation(Base):
    __tablename__ = "invitations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    email = Column(String(255), nullable=False)
    role = Column(SQLEnum(RoleEnum), nullable=False, default=RoleEnum.MEMBER)
    token = Column(String(128), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime, nullable=False)
    accepted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
```

---

### Tarea 1.5 · Migraciones Asíncronas con Alembic
1. Inicializar Alembic en modo async: `alembic init -t async migrations`.
2. Configurar `migrations/env.py` para enlazar `target_metadata = Base.metadata` y leer la URL desde `settings.DATABASE_URL`.
3. Generar la primera migración:
   ```bash
   alembic revision --autogenerate -m "fase1_initial_multitenant_schema"
   ```
4. Aplicar la migración contra el PostgreSQL remoto (`100.89.59.70:5433`):
   ```bash
   alembic upgrade head
   ```

---

### Tarea 1.6 · Middleware de Tenant y Rutas Base (Aislamiento R3)
1. **`backend/core/middleware.py`:**
   * Dependencia `get_current_tenant`:
     * Extrae token del encabezado `Authorization: Bearer <jwt>`.
     * Resuelve `user_id` autenticado.
     * Lee `X-Organization-Id` del encabezado HTTP.
     * Consulta si existe una fila en `Membership` que vincule dicho usuario con la organización.
     * Si no existe membresía activa: retorna `HTTP 403 Forbidden` (`{"detail": "Acceso a la organización denegado"}`).
     * Si es válida: inyecta la organización y el rol del usuario en el contexto del request.
2. **Endpoints en `backend/apps/organizations/router.py`:**
   * `POST /api/v1/auth/register`: Registro de usuario y creación de organización inicial por defecto.
   * `POST /api/v1/auth/login`: Login con emisión de JWT.
   * `GET /api/v1/organizations/me`: Lista las organizaciones del usuario autenticado.
   * `POST /api/v1/organizations/`: Creación de un nuevo workspace.
   * `POST /api/v1/organizations/{id}/invite`: Envío de invitación a un nuevo miembro con rol especificado.

---

### Tarea 1.7 · Inicialización del Frontend Base (`frontend/`)
1. Inicializar proyecto con Vite en `frontend/`:
   ```bash
   npm create vite@latest frontend -- --template react-ts
   cd frontend
   npm install
   ```
2. Instalar dependencias visuales y de localización:
   ```bash
   npm install tailwindcss @tailwindcss/vite lucide-react i18next react-i18next
   ```
3. Configurar Tailwind incorporando los tokens de **`design-dark.md`**:
   * Background Neutral: `#1C1C1C`
   * Surface (Cards/Modales): `#2A2A2A`
   * Primary Text: `#EDEDED`
   * Secondary Text/Borders: `#8A8F8A`
   * Tertiary Accent: `#17a163` (Emerald)
   * Fuentes: `Inter` para titulares e interfaz, `JetBrains Mono` para datos técnicos.
4. Configurar `i18next`:
   * Crear `src/locales/es/common.json` y `src/locales/en/common.json`.
   * Registrar namespaces para autenticación, navegación y errores.
   * Prohibir cadenas de texto planas; todo componente React utiliza `t()`.
5. Construir el Shell de navegación (`Sidebar.tsx` según §0.1 de `MENU-MAP.md`) con el selector de organizaciones y vistas de Login/Registro.

---

### Tarea 1.8 · Documentación de Decisiones de Extracción
Crear `docs/architecture/boilerplate-decisions.md` registrando cada archivo o módulo adaptado desde `engines/saas-boilerplate`, indicando origen, destino y modificaciones realizadas.

---

## 4. Definition of Done (DoD) — Criterios de Aceptación

Para dar por concluida la Fase 1, se deben validar y marcar todas las casillas siguientes:

- [ ] **Seguridad de Red VPS (R6):** `fenix-postgres` (`5433`) y `fenix-redis` (`6380`) corren en Dokploy y están vinculados exclusivamente a la IP de Tailscale (`100.89.59.70`). Escaneo con `nmap` contra la IP pública devuelve puertos cerrados.
- [ ] **Stack Previo Intacto:** Los contenedores existentes de Mindguard (`5432`, `6379`, `5050`, Gitea) siguen funcionando con normalidad.
- [ ] **Conexión Local-Remoto:** El backend ejecutado en la máquina local conecta correctamente a PostgreSQL (`5433`) y Redis (`6380`) a través del túnel de Tailscale.
- [ ] **Migraciones Aplicadas:** Alembic genera y aplica la migración `fase1_initial_multitenant_schema` creando las tablas `organizations`, `users`, `memberships` e `invitations` en PostgreSQL.
- [ ] **Test de Aislamiento Multi-tenant (Automatizado):**
  * El archivo `backend/tests/test_multitenant_isolation.py` ejecuta una prueba con dos usuarios en dos organizaciones distintas.
  * Demuestra que un usuario de la Organización Alpha recibe `HTTP 403` al intentar consultar o modificar recursos indicando el `organization_id` de la Organización Beta.
- [ ] **Autenticación End-to-End:** Flujo completo de registro, login (emisión de JWT), lectura del perfil actual y cambio de organización activo.
- [ ] **Frontend Conforme a `design-dark.md`:** La aplicación carga en `localhost:5173` con fondo `#1C1C1C`, cards `#2A2A2A` y botón principal `#17a163` sin parpadeo de estilos (sin FOUC).
- [ ] **Auditoría de i18n:** El frontend conmuta entre español e inglés sin ningún texto literal suelto.
- [ ] **Regla R2 Verificada:** El directorio `engines/` se encuentra 100% limpio y sin modificaciones (`git status` no reporta cambios dentro de `engines/`).
- [ ] **Documentación:** El archivo `docs/architecture/boilerplate-decisions.md` existe y documenta las extracciones realizadas.
- [ ] **Secretos:** El archivo `.env.example` contiene todas las variables requeridas con valores de ejemplo y no existen credenciales reales en el árbol Git.

---

## 5. Protocolo de Revisión Especializada (Paso 8 con `.agents/`)

Antes de marcar la Fase 1 en `ROADMAP.md`, invocar a los subagentes especializados de `.agents/agents/`:

| Subagente | Verificación Obligatoria |
| :--- | :--- |
| **`security-reviewer`** | Comprobar que la dependencia de tenant en FastAPI no se puede eludir omitiendo el encabezado `X-Organization-Id` (debe fallar cerrado / *fail-closed*). Verificar que las contraseñas se hashean con algoritmo seguro y salting adecuado. |
| **`database-reviewer`** | Comprobar que existen índices compuestos en `memberships(organization_id, user_id)` y en claves foráneas para evitar escaneos secuenciales en consultas con aislamiento. |
| **`silent-failure-hunter`** | Comprobar que una caída temporal del túnel Tailscale activa reconexión automática en el pool de base de datos (`pool_pre_ping=True`) sin congelar el proceso del backend. |
| **`react-reviewer`** | Comprobar que ningún componente visual importe colores estándar de Tailwind que rompan la paleta esmeralda (`#1C1C1C`, `#2A2A2A`, `#EDEDED`, `#8A8F8A`, `#17a163`) y que todas las etiquetas usen `t()`. |

Si aparece cualquier hallazgo clasificado como **CRITICAL** o **HIGH**, debe resolverse de inmediato antes de proceder con la Fase 2.