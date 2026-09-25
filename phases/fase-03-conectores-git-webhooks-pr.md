# FASE 3 — Conectores Git, Webhooks & Automatización de Pull Requests

> **Documento de especificación ejecutable.** Define las integraciones OAuth y Apps para GitHub, GitLab, Bitbucket y Gitea, la recepción y validación criptográfica de Webhooks, el motor de análisis superficial en CI/CD (`--scan-mode quick`), el bot de retroalimentación en Pull Requests y la apertura programática de ramas con parches *autofix*.
>
> **Estado:** `[ ]` Pendiente de cierre — Bloques 3.1, 3.2 y 3.3 implementados en código y pruebas
> **Dependencias previas:** Fase 1 (Organizaciones, RBAC y Auth) y Fase 2 (Motor de ejecución Strix en Docker Sandbox).  
> **Autoridades que rigen esta fase:** `ARCHITECTURE.md` (§2, §3, §4) y `AGENTS.md` (Reglas de Oro R1, R3, R4 y R5).

---

## 1. Objetivo de la Fase

Conectar la plataforma de forma bidireccional con los cuatro proveedores de código fuente principales (**GitHub, GitLab, Bitbucket y Gitea**). 

La plataforma debe sincronizar el inventario de repositorios de la organización, recibir y validar criptográficamente eventos de webhooks ante la apertura o actualización de Pull Requests / Merge Requests, clonar de forma superficial y efímera el diff modificado (`depth=1`), ejecutar el escáner rápido de Strix, reportar el estado de seguridad a los checks de CI/CD (bloqueando el merge ante fallos críticos), comentar en el hilo del PR con las Pruebas de Concepto (PoC) reproducibles y ofrecer la creación automatizada de ramas con parches correctivos (*autofix*).

---

## 2. Decisiones de Arquitectura e Infraestructura

1. **Aislamiento Criptográfico de Credenciales Git (R3 Estricta):**
   * Los tokens de acceso OAuth, tokens de instalación de GitHub Apps y Personal Access Tokens (PAT) se almacenan cifrados en PostgreSQL mediante **AES-256-GCM**.
   * La clave de cifrado maestro (`GIT_ENCRYPTION_KEY`) reside exclusivamente en variables de entorno (`.env`), nunca en la base de datos ni en el repositorio.
   * Toda consulta y mutación hacia la API de un proveedor Git se realiza en el contexto de la organización propietaria del repositorio.
2. **Defensa contra Inyecciones y Clonado Seguro:**
   * La URL de clonado nunca se interpola directamente en una shell. Se utiliza la librería `GitPython` o llamadas parametrizadas a `git clone` con sanitización estricta de nombres de ramas y SHAs de commit para prevenir inyecciones de comandos.
   * Las credenciales temporales de clonado se inyectan en memoria o mediante el encabezado `Authorization` en la URL HTTPS (`https://x-access-token:<token>@github.com/...`), destruyéndose inmediatamente tras el clon.
3. **Escaneo Incremental Rápido (Diff-Scoped):**
   * Los escaneos en PRs no ejecutan el modo exhaustivo para no demorar los pipelines del cliente ni consumir tokens LLM innecesarios.
   * Se invoca: `strix -n --scan-mode quick --target /workspace/repo`.
   * El runner clona únicamente el commit del PR con profundidad acotada (`git clone --depth=1 --branch=<pr_branch>`).
4. **Validación Estricta de Webhooks:**
   * Todo webhook entrante debe validar su firma antes de tocar cualquier lógica de negocio.
   * Prevención de ataques de temporización utilizando `hmac.compare_digest`.
   * Respuesta inmediata con `HTTP 202 Accepted` y despacho asíncrono a Celery para evitar exceder los timeouts de entrega del proveedor (típicamente 10 segundos).
5. **Zero Data en Código Fuente (R5):**
   * El código clonado del PR vive en `/tmp/pr_workspaces/<job_id>`.
   * Al emitir el veredicto y comentarios, el directorio se purga por completo del sistema de archivos del servidor.

---

## 3. Desglose de Tareas de Implementación

### Tarea 3.1 · Modelos de Datos en PostgreSQL

Crear las migraciones para `Repository`, `GitCredential` y `PullRequestReview` en `backend/apps/repositories/models.py`:

```python
import uuid
from datetime import datetime
from enum import Enum
from sqlalchemy import (
    Column, String, Boolean, DateTime, ForeignKey, 
    Text, Integer, Enum as SQLEnum, Index
)
from sqlalchemy.dialects.postgresql import UUID
from backend.core.database import Base

class GitProviderEnum(str, Enum):
    GITHUB = "GITHUB"
    GITLAB = "GITLAB"
    BITBUCKET = "BITBUCKET"
    GITEA = "GITEA"

class PRReviewStatusEnum(str, Enum):
    QUEUED = "QUEUED"
    SCANNING = "SCANNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    ERROR = "ERROR"

class Repository(Base):
    __tablename__ = "repositories"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = Column(SQLEnum(GitProviderEnum), nullable=False)
    
    # Identificadores remotos
    remote_repo_id = Column(String(128), nullable=False)    # ID o path único en el proveedor
    name = Column(String(255), nullable=False)             # ej: "mi-app"
    full_name = Column(String(512), nullable=False)        # ej: "organizacion/mi-app"
    clone_url = Column(String(1024), nullable=False)
    default_branch = Column(String(128), nullable=False, default="main")
    
    # Configuración de automatización
    pr_reviews_enabled = Column(Boolean, nullable=False, default=True)
    webhook_secret = Column(String(128), nullable=False)   # Secreto aleatorio HMAC
    webhook_id = Column(String(128), nullable=True)        # ID del webhook registrado en el proveedor
    
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_repositories_org_provider", "organization_id", "provider"),
        Index("ix_repositories_remote_id", "provider", "remote_repo_id", unique=True),
    )

class GitCredential(Base):
    __tablename__ = "git_credentials"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = Column(SQLEnum(GitProviderEnum), nullable=False)
    
    # Tokens cifrados con AES-256-GCM
    encrypted_access_token = Column(Text, nullable=False)
    encrypted_refresh_token = Column(Text, nullable=True)
    installation_id = Column(String(128), nullable=True)   # Específico para GitHub Apps
    token_expires_at = Column(DateTime, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

class PullRequestReview(Base):
    __tablename__ = "pull_request_reviews"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    repository_id = Column(UUID(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id = Column(UUID(as_uuid=True), ForeignKey("pentest_runs.id", ondelete="SET NULL"), nullable=True)
    
    pr_number = Column(Integer, nullable=False)
    pr_title = Column(String(512), nullable=False)
    pr_author = Column(String(255), nullable=False)
    source_branch = Column(String(255), nullable=False)
    target_branch = Column(String(255), nullable=False)
    commit_sha = Column(String(64), nullable=False)
    
    status = Column(SQLEnum(PRReviewStatusEnum), nullable=False, default=PRReviewStatusEnum.QUEUED, index=True)
    issues_caught_critical = Column(Integer, nullable=False, default=0)
    issues_caught_high = Column(Integer, nullable=False, default=0)
    merge_blocked = Column(Boolean, nullable=False, default=False)
    
    comment_id = Column(String(128), nullable=True)        # ID del comentario emitido en el PR
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_pr_reviews_repo_number", "repository_id", "pr_number"),
    )
```

---

### Tarea 3.2 · Clientes de API Git y Cifrado (`backend/apps/repositories/clients/`)

1. **Utilidad de Cifrado (`backend/core/crypto.py`):**
   * Implementar `encrypt_secret(plaintext: str) -> str` y `decrypt_secret(ciphertext: str) -> str` usando `cryptography.hazmat.primitives.ciphers.aead.AESGCM`.
2. **Cliente Base Abstracto (`backend/apps/repositories/clients/base.py`):**
   ```python
   from abc import ABC, abstractmethod

   class BaseGitClient(ABC):
       @abstractmethod
       def list_repositories(self) -> list[dict]: ...

       @abstractmethod
       def set_commit_status(self, repo_full_name: str, sha: str, state: str, description: str, target_url: str): ...

       @abstractmethod
       def post_pr_comment(self, repo_full_name: str, pr_number: int, body: str) -> str: ...

       @abstractmethod
       def update_pr_comment(self, repo_full_name: str, comment_id: str, body: str): ...

       @abstractmethod
       def create_autofix_branch_and_pr(self, repo_full_name: str, base_branch: str, branch_name: str, patch_diff: str, title: str) -> str: ...
   ```
3. Implementar adaptadores específicos:
   * `GitHubClient`: Utiliza `PyGithub` o llamadas HTTP directas con token de GitHub App.
   * `GitLabClient`: Utiliza `python-gitlab` (soporte tanto para GitLab SaaS como instancias autoalojadas).
   * `BitbucketClient`: Utiliza llamadas REST a la API 2.0 de Bitbucket Cloud.
   * `GiteaClient`: Utiliza la API REST de Gitea (compatible con instancias locales y remotas).

---

### Tarea 3.3 · Endpoint Unificado de Webhooks (`backend/apps/repositories/router_webhooks.py`)

1. Definir endpoint: `POST /api/v1/webhooks/git/{provider}`.
2. Mecanismo de validación criptográfica según proveedor:
   * **GitHub:** Encabezado `X-Hub-Signature-256`. Computar HMAC-SHA256 del cuerpo crudo y comparar con `hmac.compare_digest`.
   * **GitLab:** Encabezado `X-Gitlab-Token`. Comparar token secreto configurado.
   * **Bitbucket:** Encabezado `X-Hub-Signature`. Comparar HMAC SHA-256.
   * **Gitea:** Encabezado `X-Gitea-Signature`. Comparar HMAC SHA-256.
3. Extracción de eventos normalizados:
   * Eventos de PR/MR: `opened`, `synchronize`, `reopened`.
   * Eventos de Comentarios: `issue_comment.created` (para activar ChatOps ante `@strix review`).
4. Flujo de respuesta:
   * Si la firma falla: responder de inmediato con `HTTP 401 Unauthorized` (`{"error": "invalid_signature"}`).
   * Si el repositorio no está registrado: responder con `HTTP 404 Not Found`.
   * Si es válido: encolar tarea en Celery `process_git_webhook_event.delay(provider, event_type, payload)` y devolver `HTTP 202 Accepted` en menos de 500 ms.

---

### Tarea 3.4 · Tarea Orquestadora de CI/CD (`backend/workers/pr_scanner.py`)

Implementar el flujo completo de revisión en Pull Requests:

```python
import os
import shutil
import tempfile
import git
from backend.workers.celery_app import celery_app
from backend.apps.repositories.models import Repository, PullRequestReview, PRReviewStatusEnum
from backend.apps.pentests.models import PentestRun, TargetTypeEnum, ScanModeEnum, ScanStatusEnum
from backend.workers.runner.sandbox import StrixSandboxManager
from backend.workers.parser.strix_parser import parse_strix_output
from backend.apps.repositories.clients.factory import get_git_client

@celery_app.task(name="tasks.run_pr_security_scan", bind=True, max_retries=2)
def run_pr_security_scan(self, repo_id: str, pr_number: int, commit_sha: str, source_branch: str, target_branch: str, pr_title: str, pr_author: str):
    db = get_db_session()
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    git_client = get_git_client(repo.organization_id, repo.provider)
    
    # 1. Crear registro de revisión en BD
    review = PullRequestReview(
        organization_id=repo.organization_id,
        repository_id=repo.id,
        pr_number=pr_number,
        pr_title=pr_title,
        pr_author=pr_author,
        source_branch=source_branch,
        target_branch=target_branch,
        commit_sha=commit_sha,
        status=PRReviewStatusEnum.SCANNING
    )
    db.add(review)
    db.commit()

    # 2. Notificar commit status: PENDING
    git_client.set_commit_status(
        repo.full_name, commit_sha, 
        state="pending", 
        description="Strix security review in progress...", 
        target_url=f"[https://app.tu-dominio.com/pr-reviews/](https://app.tu-dominio.com/pr-reviews/){review.id}"
    )

    temp_clone_dir = tempfile.mkdtemp(prefix=f"strix_pr_{review.id}_")
    try:
        # 3. Clonado superficial acotado al commit (depth=1)
        token = git_client.get_clone_token()
        authenticated_url = repo.clone_url.replace("https://", f"https://x-access-token:{token}@")
        git.Repo.clone_from(authenticated_url, temp_clone_dir, branch=source_branch, depth=1)

        # 4. Crear PentestRun asociado
        run = PentestRun(
            organization_id=repo.organization_id,
            target_type=TargetTypeEnum.REPOSITORY,
            target_identifier=f"{repo.full_name}#PR-{pr_number}",
            scan_mode=ScanModeEnum.QUICK,
            status=ScanStatusEnum.RUNNING
        )
        db.add(run)
        db.commit()
        review.run_id = run.id

        # 5. Ejecutar Strix en modo rápido
        sandbox = StrixSandboxManager(run_id=str(run.id), target_path_or_url=temp_clone_dir, scan_mode="quick")
        result = sandbox.run(timeout_seconds=900) # 15 min max en PRs

        # 6. Parsear e ingerir vulnerabilidades
        findings = parse_strix_output(result["output_path"], repo.organization_id, run.id, db)
        
        crit_count = sum(1 for f in findings if f.severity == "CRITICAL")
        high_count = sum(1 for f in findings if f.severity == "HIGH")
        
        review.issues_caught_critical = crit_count
        review.issues_caught_high = high_count

        # 7. Evaluar veredicto y emitir feedback en Git
        if crit_count > 0 or high_count > 0:
            review.status = PRReviewStatusEnum.FAILED
            review.merge_blocked = True
            
            # Bloquear merge
            git_client.set_commit_status(
                repo.full_name, commit_sha, 
                state="failure", 
                description=f"Security Review Failed: {crit_count} Critical, {high_count} High issues found.",
                target_url=f"[https://app.tu-dominio.com/pr-reviews/](https://app.tu-dominio.com/pr-reviews/){review.id}"
            )
            # Publicar comentario estructurado con PoCs y parches
            comment_body = build_pr_comment_markdown(findings, review.id)
            comment_id = git_client.post_pr_comment(repo.full_name, pr_number, comment_body)
            review.comment_id = comment_id
        else:
            review.status = PRReviewStatusEnum.PASSED
            git_client.set_commit_status(
                repo.full_name, commit_sha, 
                state="success", 
                description="Strix Security Review: No critical or high vulnerabilities detected.",
                target_url=f"[https://app.tu-dominio.com/pr-reviews/](https://app.tu-dominio.com/pr-reviews/){review.id}"
            )

        db.commit()

    finally:
        # 8. Purgar con seguridad el código clonado de disco (R5 Zero Data)
        if os.path.exists(temp_clone_dir):
            shutil.rmtree(temp_clone_dir, ignore_errors=True)
```

---

### Tarea 3.5 · Formato de Comentarios Markdown y Generación de Autofix

1. **Estructura del Comentario emitido en el PR (`build_pr_comment_markdown`):**
   ```markdown
   ## 🦉 Strix Security Review: Vulnerabilities Detected
   
   Strix has analyzed this Pull Request and found **{critical} Critical** and **{high} High** severity issues that block merging.
   
   ---
   
   ### 🔴 [{severity}] {title}
   * **Location:** `{affected_target}:{affected_line}`
   * **CVSS Score:** `{cvss_score}` | **OWASP Category:** `{owasp_category}`
   
   #### 💥 Proof of Concept (Exploit Reproduction)
   ```bash
   {poc_reproduction_raw}
   ```
   
   #### 🛠️ Suggested Remediation (Autofix)
   ```diff
   {autofix_patch_diff}
   ```
   
   ---
   👉 [View full analysis and logs in Dashboard](https://app.tu-dominio.com/issues/{issue_id})
   💬 *You can tag `@strix review` to re-run the assessment after committing changes.*
   ```

2. **Apertura de Rama de Autofix (`create_autofix_branch_and_pr`):**
   * Endpoint: `POST /api/v1/vulnerabilities/{id}/create-fix-pr`.
   * Toma el diff almacenado en `vulnerabilities.autofix_patch_diff`.
   * Crea una rama remota en el repositorio: `strix/fix-{issue_id_short}`.
   * Aplica el parche con autor: `Strix Bot <bot@tu-dominio.com>`.
   * Abre un Pull Request dirigido a la rama de origen con título: `[Strix Security Fix] {vulnerability_title}`.

---

### Tarea 3.6 · OAuth, Sincronización e Importación de Repositorios (Bloque 3.3)

> **Estado:** `[x]` Implementado en código y pruebas (`backend/tests/test_git_oauth.py`, `backend/tests/test_repositories_api.py`, `backend/tests/test_repository_inventory.py`, `backend/tests/test_git_clients.py`). La validación E2E contra GitHub/GitLab reales sigue pendiente.

1. **Primitivas OAuth (`backend/apps/repositories/oauth.py`):**
   * `state` firmado con **HMAC-SHA256** sobre `SECRET_KEY`, con `provider`, `organization_id`, `user_id`, `nonce` de 32 bytes y expiración (`OAUTH_STATE_TTL_SECONDS`, 10 minutos por defecto).
   * La clave Redis del `state` es el **SHA-256 del state completo**, de modo que ni el token ni el nonce aparecen en claves de infraestructura.
   * Canje del código contra `GITHUB_OAUTH_TOKEN_URL` / `GITLAB_OAUTH_TOKEN_URL` con `client_id`, `client_secret` y `redirect_uri` verificados; los errores del proveedor se traducen a `HTTP 502` sin filtrar cuerpos ni secretos.
2. **Rutas (`backend/apps/repositories/router_auth.py`):**
   * `GET /api/v1/repositories/oauth/{provider}/authorize` — exige rol `ADMIN`, aplica rate limit por tenant (`repository-management`), falla cerrado con `HTTP 503` si el proveedor no está configurado y responde `302` a la pantalla de consentimiento.
   * `GET /api/v1/repositories/oauth/{provider}/callback` — **consume el state con `GETDEL`** (un solo uso), revalida en PostgreSQL que la membresía, el usuario y la organización siguen activos, cifra el token con `AES-256-GCM` y AAD por organización/proveedor/campo, y redirige con `303` a `/repositories?connected={PROVIDER}`. Un replay del mismo `state` responde `400`.
3. **Gestión de repositorios (`backend/apps/repositories/router.py`):**
   * `GET /api/v1/repositories/remote?provider=…` — inventario paginado del proveedor usando el token descifrado **solo en memoria**; normaliza la respuesta heterogénea (`inventory.py`), descarta entradas cuyo host de clonado no esté en la allowlist y marca `already_connected`.
   * `POST /api/v1/repositories/connect` — exige rol `ADMIN`, **revalida los metadatos contra el proveedor** (`GET /repositories/{id}` en GitHub, `GET /projects/{id}` en GitLab), genera un `webhook_secret` aleatorio de 32 bytes, inserta el repositorio y registra el webhook en `API_PUBLIC_BASE_URL` con los eventos de `GIT_WEBHOOK_SUBSCRIPTION_EVENTS`. Si el proveedor rechaza el hook, el alta se conserva con `webhook_registered: false` y traza sanitizada.
   * `GET /api/v1/repositories/` — listado paginado y filtrable (`provider`, `is_active`) acotado a `organization_id`.
   * `GET|PATCH|DELETE /api/v1/repositories/{id}` — lectura, actualización de `pr_reviews_enabled` / `default_branch` / `is_active` y desvinculación con borrado del webhook remoto. `PATCH` y `DELETE` exigen `ADMIN`; `DELETE` responde `409` si existen revisiones `QUEUED`/`SCANNING` en curso.
   * Toda lectura o mutación sobre un repositorio ajeno devuelve `404` genérico: un recurso de otra organización es indistinguible de uno inexistente.
4. **Clientes (`clients/github.py`, `clients/gitlab.py`):** `get_repository`, `create_webhook` y `delete_webhook` con validación de URL HTTPS sin credenciales y secreto de al menos 32 caracteres; un `404` al borrar el hook se considera éxito.
5. **Sin migraciones:** `repositories` y `git_credentials` ya contienen `webhook_id`, `webhook_secret`, `default_branch`, `pr_reviews_enabled`, `is_active` y los tokens cifrados; el estado del flujo OAuth vive en Redis, por lo que `e8a0b2c4d6e8` sigue siendo *head* y `alembic check` no detecta drift.
6. **Pendiente del bloque:** conectores Bitbucket/Gitea (incluido `autofix`) y la pantalla de onboarding del frontend, que corresponde a la Fase 4.
7. **Higiene de logs (endurecimiento pendiente):** el `code` y el `state` viajan en la query del callback, por lo que el proxy de entrada y el *access log* de Uvicorn deben excluir la query de `/api/v1/repositories/oauth/*/callback` en staging y producción. El backend nunca los registra.

---

## 4. Definition of Done (DoD) — Criterios de Aceptación

Para dar por concluida la Fase 3, se deben validar y marcar todas las casillas siguientes:

- [ ] **Validación HMAC Funcional:** El endpoint de webhooks rechaza peticiones con firmas manipuladas (`HTTP 401`) y procesa firmas válidas de GitHub, GitLab, Bitbucket y Gitea (`HTTP 202`).
- [x] **Sincronización de Repositorios:** La conexión OAuth permite listar los repositorios de la cuenta conectada y habilitar el toggle `pr_reviews_enabled`. *Certificado por pruebas automatizadas con las APIs de GitHub y GitLab simuladas; la comprobación contra proveedores reales queda en el checklist de cierre de fase.*
- [ ] **Escaneo Automático ante PRs:** Al abrir o actualizar un PR en un repositorio de prueba conectado, la plataforma encola el escaneo rápido y reporta el status `pending` en los checks del commit.
- [ ] **Bloqueo de Merge ante Hallazgos Críticos:** Si el código modificado contiene un fallo crítico intencionado (ej. SQLi o Command Injection):
  * El Commit Status check pasa a `failure`.
  * El bot publica automáticamente el comentario estructurado con el comando PoC y el diff de solución.
- [ ] **Aprobación Limpia de PRs Seguros:** Si el PR no contiene fallos críticos ni altos, el check pasa a `success` sin emitir comentarios intrusivos.
- [ ] **ChatOps Operativo:** Escribir un comentario con `@strix review` en el PR dispara una nueva auditoría y actualiza el comentario previo en el hilo.
- [ ] **Creación de Pull Request con Autofix:** Pulsar "Create PR with Fix" genera la rama remota y abre el PR de corrección en el proveedor Git correspondiente.
- [ ] **Aislamiento Multi-tenant Estricto (R3):** Se demuestra mediante tests que los tokens Git de la Organización A jamás pueden ser utilizados para clonar o publicar en repositorios de la Organización B.
- [ ] **Zero Data en Disco (R5):** Tras finalizar el análisis del PR, el directorio temporal `/tmp/pr_workspaces/<job_id>` se elimina al 100%.

---

## 5. Protocolo de Revisión Especializada (Paso 8 de AGENTS.md)

| Especialista | Verificación Obligatoria |
| :--- | :--- |
| **Security Reviewer** | Comprobar que los tokens OAuth se descifran exclusivamente en memoria durante el tiempo de vida de la tarea y nunca se imprimen en logs de Celery ni en mensajes de excepción. Verificar que los webhooks previenen ataques de denegación de servicio por reenvío de eventos (*replay attacks*) mediante marcas temporales de entrega. |
| **Database Reviewer** | Verificar índices en `repositories(organization_id, provider)` y `pull_request_reviews(repository_id, pr_number)` para asegurar consultas inmediatas en las vistas de CI/CD. |
| **Silent Failure Hunter** | Asegurar que si la API del proveedor Git responde con `HTTP 429 Too Many Requests` (Rate Limiting), la tarea Celery reintenta con backoff exponencial sin marcar el escaneo falsamente como `ERROR`. |
| **Performance Optimizer** | Comprobar que el clonado de repositorios utiliza flags de clonado mínimo (`--depth=1 --no-tags --single-branch`) para minimizar la transferencia de red y reducir la duración del job a menos de 90 segundos. |