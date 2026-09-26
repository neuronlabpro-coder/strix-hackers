# FASE 5 — Facturación Híbrida Stripe, Credit Ledger, API Keys & Servidor MCP

> **Documento de especificación ejecutable.** Define el modelo financiero híbrido en Stripe (asientos recurrentes + créditos prepago + tarificación por uso de PRs), el balance inmutable en `credit_ledger` (R4), la gestión de tokens de API con matriz de 46 scopes granulares, los webhooks salientes firmados criptográficamente y el servidor remoto MCP (`/mcp`) para asistentes de desarrollo (Cursor, Claude Code, ChatGPT).
>
> **Estado:** `[~]` En ejecución (Bloques 5.1 y 5.2 entregados)  
> **Dependencias previas:** Fase 1 (Organizaciones, RBAC y Auth), Fase 2 (Runs de escaneo) y Fase 4 (Vistas de UI de Facturación, Tokens y Ajustes).  
> **Autoridades que rigen esta fase:** `ARCHITECTURE.md` (§1 R4, §6, §7), `MENU-MAP.md` (§8.4, §9, §10.3, §7.b) y `AGENTS.md` (Reglas de Oro R1, R3 y R4).
>
> **Bloques entregados**
> * **5.1** — Orquestador dinámico de LLMs con margen auditable, ledger de créditos inmutable con saldo atómico, scaffolding de Stripe y consola SuperAdmin en `/admin/llm`.
> * **5.2** — Catálogo oficial de OpenRouter con `markup_pct`, enlace del orquestador al runner de Strix con fallback encadenado y telemetría de consumo, y base de datos CVE de referencia con `/cve` y sincronización periódica de los feeds oficiales.
>
> **Bloque 5.2 · decisiones que se apartan del enunciado y por qué**
>
> * La tabla es `llm_model_configs` y no `llm_models_config`. Renombrarla por coherencia de singular sería una migración sin ningún beneficio funcional, así que se documenta la diferencia en lugar de pagar el coste.
> * Las tareas de Strix viven en `backend/workers/tasks.py`, no en `backend/apps/pentests/tasks.py`. `apps/` contiene código de aplicación síncrono y `workers/` el proceso que lanza contenedores; el runner es lo segundo por definición.
> * El fallback no reacciona a un `429` en el momento, porque el contenedor de Strix habla con OpenRouter por su cuenta y el worker no ve los códigos HTTP del proveedor. Lo que sí puede observar es que la ejecución falló, así que reencola con el siguiente modelo de la cadena. Es menos granular que un reintento dentro del contenedor, pero es lo que la arquitectura permite sin exponer la credencial al proceso supervisor.
> * La tarificación solo cobra cuando el reporte de Strix publica consumo de tokens. `results.json` no incluye ese bloque hoy, así que `extract_token_usage` devuelve `None` —no cero— y la reserva del tenant se mantiene intacta. Un cero significaría «el motor consumió tokens gratis» y `None` significa «no lo sabemos»; cobrar en función de la segunda lectura sería tarificar al aire.
> * `organizations.credit_balance` se estrecha a `numeric(12,4)`. Con 1 crédito = 1 USD, 99.999.999,99 créditos cubren cualquier saldo de una plataforma de por vida y un saldo no negativo solo necesita un dígito entero.
>
> **Bloque 5.2 · Sustitución del catálogo por el listado del Owner (migración `d5e6f7a8b9c0`).** Ocho modelos con `z-ai/glm-5.3` como primario de prioridad 1, inyectado al contenedor como `STRIX_LLM` en las cuatro cadenas. Los seis modelos del catálogo anterior quedan desactivados sin borrarse, para no perder su historial de consumo.
> * El cambio es una migración **nueva**, no una edición de `b3c4d5e6f7a8`: esa ya estaba aplicada en la base remota, y reescribir su seed dejaría el repositorio y la base discrepando sin que `alembic check` lo detecte.
> * `z-ai/glm-5.3` se declara `use_case = ALL`. El Owner lo pidió como primario de `ALL / DEEP_PENTEST`, pero `model_id` es único y una fila solo admite un caso de uso. `ALL` cumple el efecto pedido porque `resolve_model_chain` incluye los modelos `ALL` en cada cadena.
> * La variable inyectada es `STRIX_LLM`, que es la que lee el motor. `STRIX_LLM_MODEL` no existiría para él y el contenedor caería **en silencio** a su modelo por defecto, con cada escaneo fuera del catálogo y sin telemetría.
> * Los precios base son los fijados por el Owner, sin contrastar con la carta de OpenRouter. El margen se audita contra los números que se declararon.
> * **Pendiente de decisión:** con el primario en prioridad 1 y transversal, `deepseek/deepseek-v4.1-flash` ($0,15) queda al final de la cadena y nunca es primario, así que los escaneos rápidos no usan el modelo barato.

> **Pendiente:** Tareas 3.4 a 3.6 (46 scopes, webhooks salientes y servidor MCP) y el resto del DoD.

---

## 1. Objetivo de la Fase

Implementar la monetización y la capa programática del producto. 

Por un lado, desplegar el sistema de facturación híbrido en Stripe: suscripción mensual por desarrollador activo ($29/asiento/mes con 50 PR reviews incluidas por asiento), gestión inmutable del saldo de créditos prepago mediante un libro mayor (*ledger*) de solo inserción en PostgreSQL (para escaneos profundos de $60 a $300 y pasos de chat), recargas manuales o automáticas (*auto top-up*) y tarificación por uso de revisiones adicionales de PR ($1/PR extra).

Por otro lado, exponer la plataforma de forma programática: creación de claves API protegidas por hash SHA-256 con una matriz de 46 permisos granulares, despacho de webhooks salientes con firma HMAC SHA-256 y un servidor remoto compatible con el protocolo MCP (Model Context Protocol) sobre transporte SSE en `/mcp` para operar el pentester desde IDEs externos.

---

## 2. Decisiones de Arquitectura e Infraestructura

1. **Inmutabilidad Financiera Absoluta (R4 Estricta):**
   * La tabla `credit_ledger` es estrictamente *append-only*. Se bloquean las operaciones `UPDATE` y `DELETE` mediante un trigger a nivel de PostgreSQL.
   * El balance actual no se almacena como un campo editable, sino que se calcula como:
     $$\text{Balance} = \sum \text{delta\_credits}$$
   * Toda deducción o recarga genera un registro auditable con identificador de transacción y motivo.
2. **Modelo de Precios Híbrido en Stripe:**
   * **Plataforma (Base):** Suscripción recurrente calculada por cantidad de desarrolladores activos (`quantity` sincronizada con Stripe Subscription Items).
   * **Consumo (Créditos):** Compras de paquetes de saldo vía Stripe Checkout Session o cobros automáticos mediante Stripe Payment Intents guardados.
   * **Exceso (Overage):** Al finalizar el ciclo mensual, las revisiones de PR que hayan superado las 50 incluidas por desarrollador se reportan como uso medido (*metered usage*) a razón de $1 USD por unidad.
3. **Control de Acceso API de Mínimo Privilegio (46 Scopes):**
   * Separación técnica entre *Personal Tokens* (asociados a la identidad del usuario dentro de la organización) y *Service Keys* (asociadas a la organización para CI/CD y runners desatendidos).
   * Verificación middleware en cada endpoint antes de ejecutar la acción.
   * Los tokens se guardan exclusivamente en formato hash SHA-256. El prefijo (`stx_live_...` o `stx_test_...`) permite identificación rápida sin comprometer el secreto.
4. **Servidor MCP Remoto (`/mcp`):**
   * Desplegado sobre transporte SSE (Server-Sent Events) para streaming de respuestas largas y HTTP POST para invocación de herramientas.
   * Autenticación vía Bearer Token mapeado a la organización y evaluado contra los scopes requeridos por cada herramienta.

---

## 3. Desglose de Tareas de Implementación

### Tarea 3.1 · Modelos de Datos en PostgreSQL (Billing, API Keys y Webhooks)

Crear las migraciones en `backend/apps/billing/models.py` y `backend/apps/api_access/models.py`:

```python
import uuid
from datetime import datetime
from enum import Enum
from sqlalchemy import (
    Column, String, Float, Boolean, DateTime, ForeignKey, 
    Text, Integer, Enum as SQLEnum, Index, ARRAY
)
from sqlalchemy.dialects.postgresql import UUID
from backend.core.database import Base

class LedgerEventType(str, Enum):
    CREDIT_PURCHASE = "CREDIT_PURCHASE"
    PENTEST_EXECUTION = "PENTEST_EXECUTION"
    AGENT_STEP = "AGENT_STEP"
    REFUND = "REFUND"
    MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT"

class CreditLedger(Base):
    __tablename__ = "credit_ledger"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    delta_credits = Column(Float, nullable=False)          # Positivo (+100.0) o negativo (-50.0)
    balance_after = Column(Float, nullable=False)          # Balance acumulado tras el movimiento
    event_type = Column(SQLEnum(LedgerEventType), nullable=False)
    reference_id = Column(String(128), nullable=True)      # ID de PentestRun o ID de sesión Stripe
    description = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_credit_ledger_org_created", "organization_id", "created_at"),
    )

class SubscriptionInfo(Base):
    __tablename__ = "subscription_info"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, unique=True)
    stripe_customer_id = Column(String(128), nullable=False, unique=True)
    stripe_subscription_id = Column(String(128), nullable=True, unique=True)
    plan_tier = Column(String(32), nullable=False, default="PRO")
    active_developers_count = Column(Integer, nullable=False, default=1)
    
    # Auto top-up
    auto_topup_enabled = Column(Boolean, nullable=False, default=False)
    auto_topup_threshold = Column(Float, nullable=False, default=20.0)
    auto_topup_amount = Column(Float, nullable=False, default=100.0)
    
    current_period_start = Column(DateTime, nullable=True)
    current_period_end = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

class TokenTypeEnum(str, Enum):
    PERSONAL = "PERSONAL"
    SERVICE_KEY = "SERVICE_KEY"

class ApiToken(Base):
    __tablename__ = "api_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True) # Null si es Service Key
    name = Column(String(128), nullable=False)
    token_type = Column(SQLEnum(TokenTypeEnum), nullable=False, default=TokenTypeEnum.SERVICE_KEY)
    
    key_prefix = Column(String(16), nullable=False)        # ej: "stx_live_a1b2"
    hashed_token = Column(String(64), nullable=False, unique=True, index=True) # SHA-256
    
    # Array de permisos granulares
    scopes = Column(ARRAY(String), nullable=False)
    
    expires_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

class WebhookEndpoint(Base):
    __tablename__ = "webhook_endpoints"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    target_url = Column(String(1024), nullable=False)
    secret_key = Column(String(128), nullable=False)       # Secreto HMAC para firma de entregas
    subscribed_events = Column(ARRAY(String), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
```

Trigger SQL obligatorio para bloquear modificaciones sobre `credit_ledger`:
```sql
CREATE OR REPLACE FUNCTION enforce_credit_ledger_immutability()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Operación rechazada: la tabla credit_ledger es append-only. UPDATE y DELETE están prohibidos.';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_credit_ledger_immutable
BEFORE UPDATE OR DELETE ON credit_ledger
FOR EACH ROW EXECUTE FUNCTION enforce_credit_ledger_immutability();
```

---

### Tarea 3.2 · Servicio de Créditos y Transacciones Atómicas (`backend/apps/billing/ledger_service.py`)

Implementar el gestor financiero con control de concurrencia:

```python
from sqlalchemy.orm import Session
from sqlalchemy import func
from backend.apps.billing.models import CreditLedger, LedgerEventType
from backend.core.exceptions import InsufficientCreditsError

class LedgerService:
    @staticmethod
    def get_balance(db: Session, organization_id: str) -> float:
        result = db.query(func.coalesce(func.sum(CreditLedger.delta_credits), 0.0))\
                   .filter(CreditLedger.organization_id == organization_id)\
                   .scalar()
        return round(float(result), 2)

    @staticmethod
    def deduct_credits(db: Session, organization_id: str, amount: float, event_type: LedgerEventType, reference_id: str, description: str) -> CreditLedger:
        if amount <= 0:
            raise ValueError("El monto a deducir debe ser estrictamente positivo.")

        # Bloqueo pesimista para evitar carreras en deducciones concurrentes
        current_balance = LedgerService.get_balance(db, organization_id)
        if current_balance < amount:
            raise InsufficientCreditsError(
                f"Saldo insuficiente ({current_balance:.2f} créditos). Esta operación requiere {amount:.2f} créditos."
            )

        new_balance = round(current_balance - amount, 2)
        entry = CreditLedger(
            organization_id=organization_id,
            delta_credits=-amount,
            balance_after=new_balance,
            event_type=event_type,
            reference_id=reference_id,
            description=description
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return entry

    @staticmethod
    def add_credits(db: Session, organization_id: str, amount: float, reference_id: str, description: str) -> CreditLedger:
        if amount <= 0:
            raise ValueError("El monto a acreditar debe ser positivo.")

        current_balance = LedgerService.get_balance(db, organization_id)
        new_balance = round(current_balance + amount, 2)
        
        entry = CreditLedger(
            organization_id=organization_id,
            delta_credits=amount,
            balance_after=new_balance,
            event_type=LedgerEventType.CREDIT_PURCHASE,
            reference_id=reference_id,
            description=description
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return entry
```

---

### Tarea 3.3 · Webhooks de Stripe y Facturación (`backend/apps/billing/stripe_handler.py`)

1. Implementar endpoint `POST /api/v1/billing/stripe-webhook`.
2. Validar firma del webhook con `stripe.Webhook.construct_event(payload, sig_header, settings.STRIPE_WEBHOOK_SECRET)`.
3. Eventos procesados:
   * **`checkout.session.completed`:** Si la metadata indica compra de créditos (`mode="payment"`), llamar a `LedgerService.add_credits` y actualizar el saldo.
   * **`customer.subscription.updated`:** Actualizar `plan_tier`, fechas de período y sincronizar `active_developers_count`.
   * **`invoice.payment_failed`:** Registrar alerta de pago fallido y notificar a los administradores de la organización.
4. Auto top-up: Al completarse una deducción en `LedgerService`, si el saldo restante cae por debajo de `auto_topup_threshold` y `auto_topup_enabled == True`, disparar una tarea Celery que invoque un PaymentIntent en Stripe contra el método de pago guardado.

---

### Tarea 3.4 · Matriz de 46 Scopes y Middleware de Autorización (`backend/core/permissions.py`)

1. **Catálogo Canónico de los 46 Scopes:**
   ```python
   ALL_SCOPES = [
       # Scans (3)
       "scans:read", "scans:write", "scans:message",
       # Vulnerabilities (2)
       "vulnerabilities:read", "vulnerabilities:write",
       # Dependencies & Supply Chain (3)
       "dependencies:read", "supply_chain:read", "supply_chain:write",
       # Containers & Schedules (4)
       "containers:read", "containers:write", "schedules:read", "schedules:write",
       # Assets & Organizations (4)
       "assets:read", "assets:write", "organizations:read", "organizations:write",
       # Members & Invitations (4)
       "members:read", "members:write", "invitations:read", "invitations:write",
       # Webhooks & Tokens (3)
       "webhooks:read", "webhooks:write", "tokens:write",
       # Audit & PR Reviews (4)
       "audit:read", "pr_reviews:read", "pr_reviews:write", "pr_reviews:read",
       # Connectors & Knowledge (4)
       "connectors:read", "connectors:write", "knowledge:read", "knowledge:write",
       # Uploads, Integrations & Chat (5)
       "uploads:write", "integrations:read", "integrations:write", "chat:read", "chat:write",
       # Analytics, LLM & Test users (5)
       "analytics:read", "llm:read", "llm:write", "test_users:read", "test_users:write",
       # Discovery, License, Logs & Billing (5)
       "discovery:read", "discovery:write", "license:read", "logs:read", "billing:read", "billing:write"
   ]
   ```
2. **Generador y Verificador de Tokens (`backend/core/tokens.py`):**
   * Generar token: Prefijo (`stx_live_`) + 32 bytes aleatorios (`secrets.token_urlsafe(32)`).
   * Almacenar exclusivamente `hashlib.sha256(token.encode()).hexdigest()`.
3. **Decorador de Permisos en Endpoints:**
   ```python
   def require_scope(required_scope: str):
       def decorator(func):
           @wraps(func)
           async def wrapper(*args, **kwargs):
               auth_context = kwargs.get("auth_context") # Inyectado por middleware
               if not auth_context or not auth_context.has_scope(required_scope):
                   raise HTTPException(status_code=403, detail=f"Permiso insuficiente. Requiere scope: {required_scope}")
               return await func(*args, **kwargs)
           return wrapper
       return decorator
   ```

---

### Tarea 3.5 · Despacho de Webhooks Salientes (`backend/apps/api_access/webhook_dispatcher.py`)

1. Tarea Celery: `dispatch_outgoing_webhook(event_name: str, payload: dict, organization_id: str)`.
2. Pasos:
   * Obtener todos los endpoints activos de la organización suscritos a `event_name` (o a `*`).
   * Serializar el payload JSON canónicamente.
   * Firmar el payload: computar HMAC SHA-256 utilizando el `secret_key` del webhook y adjuntarlo en el encabezado `X-Strix-Signature`.
   * Enviar petición HTTP POST con timeout de 5 segundos.
   * Si la entrega devuelve un código distinto de `2xx`: reintentar con backoff exponencial (hasta 5 intentos) registrando el estado de salud.

---

### Tarea 3.6 · Servidor Remoto MCP (`backend/mcp/server.py`)

Implementar el servidor Model Context Protocol sobre transporte SSE utilizando `FastMCP`:

```python
from mcp.server.fastmcp import FastMCP
from backend.apps.billing.ledger_service import LedgerService
from backend.apps.pentests.models import ScanModeEnum
from backend.workers.tasks import run_pentest_task
from backend.core.database import get_db_session

mcp_app = FastMCP(
    name="Strix Pentesting Platform",
    instructions="Servidor MCP oficial de Strix. Permite lanzar auditorías ofensivas, listar vulnerabilidades validadas con PoC y aplicar parches de corrección."
)

@mcp_app.tool()
def strix_start_pentest(target: str, scan_mode: str = "standard", ctx=None) -> str:
    """Inicia un pentest autónomo sobre un repositorio de código o dominio/API."""
    db = get_db_session()
    org_id = ctx.request_context.organization_id # Resuelto por autenticación SSE
    
    # 1. Validar y descontar saldo
    cost = 60.0 if scan_mode == "quick" else 150.0
    LedgerService.deduct_credits(db, org_id, cost, "PENTEST_EXECUTION", target, f"Pentest MCP sobre {target}")
    
    # 2. Encolar tarea Celery
    job = run_pentest_task.delay(organization_id=org_id, target=target, scan_mode=scan_mode)
    return f"Pentest iniciado con éxito. Run ID: {job.id}. Target: {target}. Modo: {scan_mode}."

@mcp_app.tool()
def strix_list_vulnerabilities(severity: str = None, status: str = "OPEN", ctx=None) -> list[dict]:
    """Lista las vulnerabilidades encontradas y validadas con su puntuación CVSS y vector de ataque."""
    db = get_db_session()
    org_id = ctx.request_context.organization_id
    
    query = db.query(Vulnerability).filter(Vulnerability.organization_id == org_id)
    if severity:
        query = query.filter(Vulnerability.severity == severity.upper())
    if status:
        query = query.filter(Vulnerability.status == status.upper())
        
    vulns = query.limit(50).all()
    return [
        {
            "id": str(v.id),
            "title": v.title,
            "severity": v.severity,
            "cvss": v.cvss_score,
            "target": v.affected_target,
            "has_poc": bool(v.poc_reproduction_raw),
            "has_autofix": bool(v.autofix_patch_diff)
        }
        for v in vulns
    ]

@mcp_app.tool()
def strix_get_poc(vulnerability_id: str, ctx=None) -> str:
    """Devuelve la Prueba de Concepto (comando curl o script ejecutable) que demuestra la vulnerabilidad."""
    db = get_db_session()
    org_id = ctx.request_context.organization_id
    vuln = db.query(Vulnerability).filter(Vulnerability.id == vulnerability_id, Vulnerability.organization_id == org_id).first()
    if not vuln:
        return "Vulnerabilidad no encontrada o acceso denegado."
    return vuln.poc_reproduction_raw

@mcp_app.tool()
def strix_apply_autofix(vulnerability_id: str, ctx=None) -> str:
    """Crea una rama Git y abre un Pull Request con el parche de solución validado."""
    db = get_db_session()
    org_id = ctx.request_context.organization_id
    # Invoca servicio de repositorios (Fase 3) para crear rama con diff
    return f"Parche aplicado. Pull Request de corrección abierto para la incidencia {vulnerability_id}."
```

---

## 4. Definition of Done (DoD) — Criterios de Aceptación

Para dar por concluida la Fase 5, se deben validar y marcar todas las casillas siguientes:

- [x] **Inmutabilidad Financiera Demostrada:** Un intento deliberado de ejecutar `UPDATE` o `DELETE` sobre la tabla `credit_ledger` en PostgreSQL falla con error lanzado por el trigger `trg_credit_ledger_immutable`. *(Bloque 5.1: `trg_protect_credit_ledger_append_only` `FOR EACH STATEMENT` sobre `UPDATE OR DELETE OR TRUNCATE`.)*
- [ ] **Balance Calculado Dinámicamente:** El saldo visible en la UI y API coincide con la suma de `delta_credits` de la organización.
- [ ] **Integración de Webhooks de Stripe:** El flujo de compra de créditos y cobro de suscripción recurrente se valida localmente utilizando Stripe CLI (`stripe listen --forward-to ...` y `stripe trigger checkout.session.completed`), actualizando el balance sin intervención manual.
- [ ] **Bloqueo por Saldo Insuficiente:** Intentar lanzar un pentest profundo sin saldo suficiente es rechazado con excepción controlada (`InsufficientCreditsError`) y mensaje explicativo al usuario.
- [ ] **Matriz de 46 Scopes Operativa:**
  * Se genera una Service Key configurada únicamente con `vulnerabilities:read`.
  * La clave puede consultar `/api/v1/vulnerabilities/`.
  * La misma clave recibe `HTTP 403 Forbidden` al intentar iniciar un escaneo en `/api/v1/pentests/`.
- [ ] **Webhooks Salientes Firmados:** Los eventos de la plataforma se despachan a un receptor HTTP de prueba con la firma HMAC en `X-Strix-Signature` y se verifica la validez del cálculo de la firma.
- [ ] **Servidor MCP Funcional:** Un cliente externo (Cursor, Claude Desktop o script de test) se conecta al endpoint `/mcp` mediante SSE, se autentica con token Bearer y ejecuta `strix_list_vulnerabilities` obteniendo la lista de hallazgos.
- [ ] **Aislamiento Multi-tenant Estricto:** Una clave de API o conexión MCP de la Organización A jamás puede ejecutar acciones, listar vulnerabilidades o descontar créditos de la Organización B.

---

## 5. Protocolo de Revisión Especializada (Paso 8 de AGENTS.md)

| Especialista | Verificación Obligatoria |
| :--- | :--- |
| **Security Reviewer** | Comprobar que los tokens de API almacenados en PostgreSQL no son reversibles (hash SHA-256 con salting si aplica). Verificar que el transporte SSE del servidor MCP aplica desconexión por inactividad y límites de concurrencia para evitar fugas de memoria o descriptores de sockets. |
| **Database Reviewer** | Verificar que la consulta de balance `SUM(delta_credits)` utiliza el índice `ix_credit_ledger_org_created` y no degrada el rendimiento al crecer la tabla. Comprobar que las deducciones de créditos se ejecutan bajo transacciones atómicas con aislamiento adecuado. |
| **Silent Failure Hunter** | Asegurar que si el webhook de Stripe recibe un evento con ID duplicado (reintentos de red de Stripe), el sistema procesa la recarga de forma estrictamente idempotente sin acreditar créditos por duplicado. |
| **Performance Optimizer** | Comprobar que el despachador de webhooks salientes utiliza tareas asíncronas no bloqueantes con límites de reintentos para no saturar los workers de Celery ante endpoints caídos del cliente. |