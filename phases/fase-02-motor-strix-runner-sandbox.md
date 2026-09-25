# FASE 2 — Motor de Ejecución Strix, Orquestación & Sandbox Runner

> **Documento de especificación ejecutable.** Define la arquitectura del runner ofensivo, orquestación de colas asíncronas con Celery/Redis, ejecución headless de Strix (`strix -n`) en contenedores Docker efímeros, control de recursos, ingesta estructurada de vulnerabilidades y almacenamiento inmutable de Pruebas de Concepto (PoC).
>
> **Estado:** `[ ]` Pendiente de cierre E2E — Bloques 2.1 y 2.2 implementados en código y pruebas
> **Dependencias previas:** Fase 1 completada y validada (Base de datos PostgreSQL 16, Redis 7, Organizaciones y Auth con aislamiento multi-tenant R3).  
> **Autoridades que rigen esta fase:** `ARCHITECTURE.md` (§1, §2.2, §2.3, §4), `AGENTS.md` (Reglas de Oro R2, R3, R4 y R5).

---

## 1. Objetivo de la Fase

Construir la infraestructura de ejecución de escaneos asíncronos que permita despachar, controlar y recolectar auditorías de seguridad dinámicas de Strix. 

Cada análisis se ejecutará dentro de un contenedor Docker efímero (`ghcr.io/usestrix/strix-sandbox`) gobernado por un worker de Celery, con aislamiento absoluto entre organizaciones, límites estrictos de CPU/RAM/tiempo para proteger el servidor VPS, purga total de código fuente en disco al terminar (Zero Data R5) e ingesta normalizada de los hallazgos técnicos (severidad CVSS, vector OWASP, comando de reproducción PoC y diff de autofix) con garantías de inmutabilidad relacional (R4).

---

## 2. Decisiones de Arquitectura e Infraestructura

1. **Aislamiento Multi-tenant Estricto en Ejecución (R3):**
   * Cada escaneo se ejecuta en una instancia de contenedor Docker independiente.
   * Prohibido reutilizar contenedores entre distintas organizaciones o entre ejecuciones consecutivas.
   * Cada contenedor opera en una red Docker bridge aislada por job (`strix_net_<job_id>`), sin visibilidad de otros contenedores en ejecución ni del host Dokploy.
2. **Control de Recursos y Mitigación de DoS:**
   * Limitación obligatoria de recursos mediante cgroups de Docker:
     * Memoria máxima: `4 GB` (`--memory="4g"` / `--memory-swap="4g"`).
     * Cuota de procesador: `2 vCPUs` (`--cpus="2.0"`).
     * Timeout duro de ejecución por escaneo: cancelable por Celery y forzado a nivel de Docker (`docker kill` si excede el tiempo máximo configurado, por defecto 30 minutos).
3. **Privacidad y Zero Data en Código Fuente (R5):**
   * El código del repositorio o los artefactos del target residen en un volumen montado temporal: `/tmp/fenix_workspaces/<job_id>/workspace`.
   * El ciclo de vida está encapsulado en un bloque `try ... finally` garantizado:
     ```python
     try:
         # 1. Montar volumen efímero
         # 2. Ejecutar Strix headless (-n)
         # 3. Parsear JSON de salida strix_runs/
     finally:
         # 4. Detener y remover contenedor Docker (force=True)
         # 5. Destrucción segura en disco del directorio temporal (/tmp/fenix_workspaces/<job_id>)
     ```
   * En PostgreSQL nunca se persiste el código fuente completo, únicamente referencias de archivos, números de línea, trazas de PoC y parches de corrección.
4. **Inmutabilidad de Evidencias (R4):**
   * Las vulnerabilidades detectadas y sus pruebas de concepto (PoC) quedan congeladas. Los campos técnicos (`poc_reproduction_raw`, `cvss_score`, `cve_id`) no son editables desde la interfaz de usuario una vez registrados.

---

## 3. Desglose de Tareas de Implementación

### Tarea 2.1 · Estructura del Módulo de Workers y Colas Celery
1. Organizar el directorio `backend/workers/`:
   ```text
   backend/workers/
   ├── __init__.py
   ├── celery_app.py           # Instancia Celery configurada contra Redis remoto
   ├── tasks.py                # Tarea Celery: execute_pentest_run
   ├── runner/
   │   ├── __init__.py
   │   ├── docker_client.py    # Wrapper seguro de Docker SDK
   │   ├── sandbox.py          # Ciclo de vida del contenedor ghcr.io/usestrix/strix-sandbox
   │   └── exceptions.py       # Excepciones: SandboxTimeoutError, ContainerExecutionError
   ├── parser/
   │   ├── __init__.py
   │   ├── strix_parser.py     # Parser del JSON generado en strix_runs/
   │   └── normalizer.py       # Mapeo a CVSS v3.1 y enums de severidad
   └── tests/
       ├── test_docker_sandbox.py
       ├── test_strix_parser.py
       └── test_resource_limits.py
   ```
2. Configuración en `backend/workers/celery_app.py`:
   * Broker y Result Backend apuntando a Redis en VPS por Tailscale: `redis://:${REDIS_PASSWORD}@${TAILSCALE_BIND_IP}:6379/1`.
   * Serializador estricto JSON: `task_serializer = "json"`, `accept_content = ["json"]`.
   * Prefetch limitado para evitar acumulación de jobs pesados en un solo worker: `worker_prefetch_multiplier = 1`.
   * Límites de tiempo: `task_time_limit = 2400` (40 minutos timeout duro), `task_soft_time_limit = 2100` (35 minutos; deja margen para que el runner aplique sus límites y limpie).

---

### Tarea 2.2 · Modelos de Datos en PostgreSQL

Crear las migraciones para `PentestRun` y `Vulnerability` en `backend/apps/pentests/models.py` y `backend/apps/vulnerabilities/models.py`:

```python
import uuid
from datetime import datetime
from enum import Enum
from sqlalchemy import (
    Column, String, Float, DateTime, ForeignKey, 
    Text, Enum as SQLEnum, Index
)
from sqlalchemy.dialects.postgresql import UUID
from backend.core.database import Base

class TargetTypeEnum(str, Enum):
    REPOSITORY = "REPOSITORY"
    DOMAIN = "DOMAIN"
    API_SPEC = "API_SPEC"

class ScanModeEnum(str, Enum):
    QUICK = "QUICK"
    STANDARD = "STANDARD"
    DEEP = "DEEP"

class ScanStatusEnum(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    ABORTED = "ABORTED"

class SeverityEnum(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"

class IssueStatusEnum(str, Enum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    FIXED = "FIXED"
    SNOOZED = "SNOOZED"
    IGNORED = "IGNORED"

class PentestRun(Base):
    __tablename__ = "pentest_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    target_type = Column(SQLEnum(TargetTypeEnum), nullable=False)
    target_identifier = Column(String(512), nullable=False)  # URL de repo, dominio o path
    scan_mode = Column(SQLEnum(ScanModeEnum), nullable=False, default=ScanModeEnum.STANDARD)
    status = Column(SQLEnum(ScanStatusEnum), nullable=False, default=ScanStatusEnum.QUEUED, index=True)
    
    # Metadatos del contenedor y ejecución
    container_id = Column(String(64), nullable=True)
    exit_code = Column(String(16), nullable=True)
    error_message = Column(Text, nullable=True)
    
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_pentest_runs_org_status", "organization_id", "status"),
    )

class Vulnerability(Base):
    __tablename__ = "vulnerabilities"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id = Column(UUID(as_uuid=True), ForeignKey("pentest_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)
    severity = Column(SQLEnum(SeverityEnum), nullable=False, index=True)
    cvss_score = Column(Float, nullable=False, index=True)
    cve_id = Column(String(64), nullable=True, index=True)
    
    # Ubicación del fallo
    affected_target = Column(String(512), nullable=False) # Endpoint o ruta de archivo relativo
    affected_line = Column(String(64), nullable=True)     # Línea o rango si es SAST
    
    # Evidencia técnica inmutable (R4)
    poc_reproduction_raw = Column(Text, nullable=False)   # Script Python o comando curl generado por Strix
    autofix_patch_diff = Column(Text, nullable=True)      # Diff Git unificado con la solución propuesta
    
    status = Column(SQLEnum(IssueStatusEnum), nullable=False, default=IssueStatusEnum.OPEN, index=True)
    discovered_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_vulnerabilities_org_severity", "organization_id", "severity"),
        Index("ix_vulnerabilities_org_status", "organization_id", "status"),
    )
```

Trigger SQL para forzar la inmutabilidad de evidencias en PostgreSQL:
```sql
CREATE OR REPLACE FUNCTION protect_vulnerability_evidence()
RETURNS TRIGGER AS $$
BEGIN
    IF (OLD.poc_reproduction_raw IS DISTINCT FROM NEW.poc_reproduction_raw) OR
       (OLD.cvss_score IS DISTINCT FROM NEW.cvss_score) OR
       (OLD.cve_id IS DISTINCT FROM NEW.cve_id) THEN
        RAISE EXCEPTION 'Las evidencias técnicas y puntuaciones CVSS de una vulnerabilidad son inmutables.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_protect_vulnerability_evidence
BEFORE UPDATE ON vulnerabilities
FOR EACH ROW EXECUTE FUNCTION protect_vulnerability_evidence();
```

---

### Tarea 2.3 · Orquestador Sandbox Docker (`backend/workers/runner/sandbox.py`)

Implementar el ciclo de vida del contenedor de Strix utilizando Docker SDK:

```python
import os
import shutil
import tempfile
import docker
from docker.errors import ContainerError, APIError
from backend.core.config import settings

class StrixSandboxManager:
    SANDBOX_IMAGE = "ghcr.io/usestrix/strix-sandbox:latest"

    def __init__(self, run_id: str, target_path_or_url: str, scan_mode: str = "standard"):
        self.run_id = str(run_id)
        self.target = target_path_or_url
        self.scan_mode = scan_mode
        self.client = docker.from_env()
        self.temp_dir = None
        self.container = None

    def setup_workspace(self) -> str:
        self.temp_dir = tempfile.mkdtemp(prefix=f"strix_job_{self.run_id}_")
        os.makedirs(os.path.join(self.temp_dir, "workspace"), exist_ok=True)
        os.makedirs(os.path.join(self.temp_dir, "output"), exist_ok=True)
        return self.temp_dir

    def run(self, timeout_seconds: int = 1800) -> dict:
        try:
            workspace_dir = self.setup_workspace()
            output_file = "/workspace/output/results.json"

            # Inyección de variables de entorno seguras para el LLM
            container_env = {
                "STRIX_LLM": settings.DEFAULT_STRIX_LLM,          # ej: "openrouter/z-ai/glm-5.3"
                "LLM_API_KEY": settings.LLM_API_KEY,
                "LLM_API_BASE": settings.LLM_API_BASE or "",
                "STRIX_NON_INTERACTIVE": "1",
            }

            volumes = {
                os.path.join(workspace_dir, "workspace"): {"bind": "/workspace/target", "mode": "ro"},
                os.path.join(workspace_dir, "output"): {"bind": "/workspace/output", "mode": "rw"}
            }

            command = [
                "strix", "-n", "--target", self.target_argument,
                "--scan-mode", self.scan_mode, "--run-name", self.run_name,
                "--output", output_file,
            ]

            self.container = self.client.containers.run(
                image=self.SANDBOX_IMAGE,
                command=command,
                environment=container_env,
                volumes=volumes,
                mem_limit="4g",
                nano_cpus=int(2.0 * 1e9),
                pids_limit=256,
                network=self.network_name,
                detach=True,
                remove=False
            )

            # Espera activa controlada con timeout
            result = self.container.wait(timeout=timeout_seconds)
            exit_code = result.get("StatusCode", -1)

            local_output_path = os.path.join(workspace_dir, "output", "results.json")
            if not os.path.exists(local_output_path):
                raise FileNotFoundError(f"Strix no generó el archivo de resultados. Código de salida: {exit_code}")

            return {
                "exit_code": exit_code,
                "output_path": local_output_path
            }

        finally:
            self.cleanup()

    def cleanup(self):
        # 1. Asegurar parada y eliminación del contenedor Docker
        if self.container:
            try:
                self.container.remove(force=True)
            except Exception:
                pass
            self.container = None

        # 2. Purgar con seguridad el directorio temporal del host (R5 Zero Data)
        if self.temp_dir and os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            self.temp_dir = None
```

---

### Tarea 2.4 · Parser e Ingestor de Artefactos (`backend/workers/parser/strix_parser.py`)

1. Leer el archivo JSON de salida generado por Strix (`results.json`).
2. Estructura esperada de hallazgos emitida por Strix:
   ```json
   {
     "scan_id": "...",
     "status": "completed",
     "findings": [
       {
         "id": "strix-vuln-001",
         "title": "SQL Injection in User Authentication",
         "description": "Unsanitized user input in username parameter allows authentication bypass.",
         "severity": "CRITICAL",
         "cvss_score": 9.8,
         "cve": "CVE-2024-XXXX",
         "target": "/api/v1/auth/login",
         "line": "42",
         "poc": "curl -X POST https://target/api/v1/auth/login -d '{\"user\": \"admin' OR '1'='1\"}'",
         "autofix": "--- auth.py\n+++ auth.py\n@@ -42 +42 @@\n- query = f\"SELECT * FROM users WHERE u = '{user}'\"\n+ query = \"SELECT * FROM users WHERE u = %s\""
       }
     ]
   }
   ```
3. Lógica de persistencia en `backend/workers/tasks.py`:
   * Abrir sesión en PostgreSQL.
   * Por cada hallazgo en `findings`:
     * Normalizar severidad al enum `SeverityEnum`.
     * Validar que el campo `poc_reproduction_raw` no esté vacío.
     * Insertar el registro en la tabla `vulnerabilities` asociado a `organization_id` y `run_id`.
   * Actualizar el registro `PentestRun`: `status = COMPLETED`, `finished_at = datetime.utcnow()`.
   * Si ocurre una excepción no controlada: registrar `PentestRun.status = FAILED`, `PentestRun.error_message = str(e)`.

---

### Tarea 2.5 · API de Control y Cancelación de Pentests

Crear endpoints en `backend/apps/pentests/router.py`:
* `POST /api/v1/pentests/`: Inicia un nuevo escaneo manual (valida saldo en organizaciones y encola en Celery).
* `GET /api/v1/pentests/{id}`: Devuelve estado en vivo (`QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`).
* `POST /api/v1/pentests/{id}/abort`: Permite al usuario detener un escaneo en curso.
  * Localiza el `container_id` en PostgreSQL.
  * Invoca `docker.kill(container_id)`.
  * Marca `status = ABORTED`.
* `GET /api/v1/vulnerabilities/`: Listado paginado de vulnerabilidades con filtros (`severity`, `status`, `target`).
* `GET /api/v1/vulnerabilities/{id}`: Detalle completo de vulnerabilidad incluyendo la traza del PoC y el parche autofix.

---

## 4. Definition of Done (DoD) — Criterios de Aceptación

Para dar por concluida la Fase 2, se deben validar y marcar todas las casillas siguientes:

- [ ] **Imagen Docker Verificada:** La imagen `ghcr.io/usestrix/strix-sandbox` está disponible en el daemon de Docker del VPS y arranca sin errores de permisos.
- [ ] **Ejecución Headless End-to-End:** Se despacha una tarea Celery contra un target de prueba; Strix ejecuta el escaneo en modo `-n` y genera el archivo `results.json`.
- [ ] **Ingesta Atómica de Hallazgos:** El parser procesa `results.json` e inserta correctamente los registros en `PentestRun` y `Vulnerabilities` con sus campos CVSS y PoC.
- [ ] **Destrucción y Zero Data Verificada (R5):** Comprobado mediante inspección en disco tras la ejecución:
  * El contenedor Docker es detenido y eliminado (`docker ps -a` no lo lista).
  * El directorio temporal `/tmp/fenix_workspaces/<job_id>` es purgado al 100%.
  * La base de datos no contiene copias del código analizado.
- [ ] **Resiliencia ante Timeouts y Abortos:** Si un job excede el límite de tiempo o se invoca `/abort`, el contenedor es eliminado de forma forzada y el estado pasa a `TIMED_OUT` o `ABORTED`.
- [ ] **Inmutabilidad Relacional de Evidencias (R4):** Un intento de ejecutar un script SQL que modifique `poc_reproduction_raw` o `cvss_score` en un registro existente es rechazado por el trigger de PostgreSQL.
- [ ] **Aislamiento Multi-tenant Concurrente (R3):** Dos escaneos de dos organizaciones distintas corren en paralelo sin interferencia de red ni acceso cruzado a datos.

---

## 5. Protocolo de Revisión Especializada (Paso 8 de AGENTS.md)

| Especialista | Verificación Obligatoria |
| :--- | :--- |
| **Security Reviewer** | Comprobar que los contenedores sandbox corren sin privilegios (`--privileged=false`), sin montar el socket de Docker (`docker.sock`) y con memoria acotada para evitar fugas al host o ataques de denegación de servicio. |
| **Database Reviewer** | Verificar índices compuestos en `vulnerabilities(organization_id, severity)` y `vulnerabilities(organization_id, status)` para garantizar consultas rápidas en el dashboard y vistas de issues. |
| **Silent Failure Hunter** | Asegurar que bajo ningún escenario de error (código de salida inesperado, JSON corrupto, fallo del LLM) el escaneo permanezca indefinidamente en estado `RUNNING`. Debe capturar la excepción y transicionar a `FAILED`. |
| **Performance Optimizer** | Comprobar que la concurrencia máxima de workers en Celery está ajustada a la capacidad de CPU/RAM del VPS para evitar caídas por Out-Of-Memory (OOM). |