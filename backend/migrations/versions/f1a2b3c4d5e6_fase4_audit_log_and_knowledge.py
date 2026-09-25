"""fase4_audit_log_and_knowledge

Revision ID: f1a2b3c4d5e6
Revises: e8a0b2c4d6e8
Create Date: 2026-09-25 18:40:00

Crea `audit_log` (append-only, R4) y `knowledge_entries` (catálogo técnico de
remediación compartido y de solo lectura).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "e8a0b2c4d6e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE TYPE audit_action_enum AS ENUM ('STATUS_CHANGED', 'REPOSITORY_POLICY_UPDATED', 'REPOSITORY_CONNECTED', 'REPOSITORY_DISCONNECTED')")
    op.execute("CREATE TYPE knowledge_category_enum AS ENUM ('INJECTION', 'XSS', 'AUTH', 'CRYPTOGRAPHY', 'SECRET_EXPOSURE', 'DESERIALIZATION', 'SSRF', 'PATH_TRAVERSAL', 'LOGIC', 'DEPENDENCY')")
    op.execute("CREATE TYPE knowledge_severity_enum AS ENUM ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW')")

    op.execute(
        """
        CREATE TABLE audit_log (
            id UUID PRIMARY KEY,
            organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            actor_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            action audit_action_enum NOT NULL,
            entity_type VARCHAR(64) NOT NULL,
            entity_id UUID NOT NULL,
            from_state VARCHAR(64),
            to_state VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_audit_log_organization_id ON audit_log (organization_id)")
    op.execute("CREATE INDEX ix_audit_log_actor_user_id ON audit_log (actor_user_id)")
    op.execute("CREATE INDEX ix_audit_log_org_entity ON audit_log (organization_id, entity_type, entity_id)")
    op.execute("CREATE INDEX ix_audit_log_org_created_at ON audit_log (organization_id, created_at)")

    # R4: el rastro de auditoría es append-only. Sin esto, un operador con acceso
    # a la base podría reescribir la historia de un triaje y la trazabilidad de
    # SOC 2 / ISO 27001 quedaría sin valor.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION protect_audit_log_append_only()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION
                'El rastro de auditoria es append-only y no admite UPDATE, DELETE ni TRUNCATE.';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_protect_audit_log_append_only
        BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_log
        FOR EACH STATEMENT EXECUTE FUNCTION protect_audit_log_append_only()
        """
    )

    op.execute(
        """
        CREATE TABLE knowledge_entries (
            id UUID PRIMARY KEY,
            reference_code VARCHAR(32) NOT NULL UNIQUE,
            title VARCHAR(255) NOT NULL,
            category knowledge_category_enum NOT NULL,
            severity knowledge_severity_enum NOT NULL,
            risk_summary TEXT NOT NULL,
            vulnerable_example TEXT NOT NULL,
            secure_example TEXT NOT NULL,
            mitigation TEXT NOT NULL,
            owasp_category VARCHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_knowledge_entries_category ON knowledge_entries (category)")
    op.execute("CREATE INDEX ix_knowledge_entries_severity ON knowledge_entries (severity)")

    _seed_knowledge()


def _seed_knowledge() -> None:
    """Catálogo inicial de remediación.

    Los textos son deliberadamente concisos y técnicos: son apuntes de
    remediación, no documentación exhaustiva. R1 impide hardcodear contenido
    configurable en el runtime, pero una tabla de referencia estática no es
    configuración: es un activo editorial versionable que el equipo de seguridad
    mantiene con migraciones.
    """

    entries = [
        (
            "CWE-89",
            "SQL Injection",
            "INJECTION",
            "CRITICAL",
            "El atacante concatena su entrada en la consulta y puede leer, modificar o borrar cualquier dato accesible con las credenciales de la aplicacion.",
            "cursor.execute(f\"SELECT * FROM users WHERE email = '{email}'\")",
            "cursor.execute(\"SELECT * FROM users WHERE email = %s\", (email,))",
            "Usa siempre consultas parametrizadas. Nunca construyas SQL con f-strings ni concatenacion, ni siquiera escapando comillas a mano.",
            "A03:2021 - Injection",
        ),
        (
            "CWE-79",
            "Cross-Site Scripting",
            "XSS",
            "HIGH",
            "El navegador de la victima ejecuta JavaScript inyectado por el atacante en el contexto de la sesion de la aplicacion.",
            "element.innerHTML = `<p>${comentario}</p>`",
            "element.textContent = comentario",
            "Escapa al renderizar y aplica una CSP estricta. No uses innerHTML ni insertAdjacentHTML con datos de usuario.",
            "A03:2021 - Injection",
        ),
        (
            "CWE-798",
            "Credenciales hardcodeadas",
            "SECRET_EXPOSURE",
            "CRITICAL",
            "Un secreto presente en el codigo queda expuesto a todo el que lea el repositorio, incluidos historiales de Git y artefactos de compilacion.",
            "DB_PASSWORD = \"hunter2-prod\"",
            "DB_PASSWORD = os.environ[\"DB_PASSWORD\"]",
            "Externaliza los secretos al entorno o a un gestor dedicated. Rota cualquier credencial que haya estado versionada y purga el historial.",
            "A07:2021 - Identification and Authentication Failures",
        ),
        (
            "CWE-22",
            "Path Traversal",
            "PATH_TRAVERSAL",
            "HIGH",
            "Con rutas relativas controladas por el atacante, la aplicacion lee o escribe fuera del directorio previsto, incluyendo archivos de configuracion.",
            "open(f\"/uploads/{nombre_archivo}\")",
            "ruta = (BASE_DIR / nombre_archivo).resolve()\nif not ruta.is_relative_to(BASE_DIR):\n    raise HTTPException(403)",
            "Resuelve la ruta y verifica que siga dentro del directorio base antes de abrirla. Rechaza rutas absolutas y segmentos ...",
            "A01:2021 - Broken Access Control",
        ),
        (
            "CWE-502",
            "Deserializacion insegura",
            "DESERIALIZATION",
            "CRITICAL",
            "Deserializar objetos de una fuente no confiable permite al atacante ejecutar codigo arbitrario durante la reconstruccion del objeto.",
            "proyecto = pickle.loads(cuerpo_recibido)",
            "datos = json.loads(cuerpo_recibido)\nproyecto = Proyecto.model_validate(datos)",
            "Usa formatos de datos pasivos (JSON) con validacion de esquema. pickle, yaml.load y los formatos con reductores permiten ejecucion de codigo.",
            "A08:2021 - Software and Data Integrity Failures",
        ),
        (
            "CWE-918",
            "Server-Side Request Forgery",
            "SSRF",
            "HIGH",
            "El atacante controla la URL que el servidor solicita y puede alcanzar servicios internos que no estan expuestos a Internet.",
            "respuesta = requests.get(parametro_url_del_usuario)",
            "destino = resolver_y_validar_destino(parametro_url_del_usuario)\nrespuesta = requests.get(destino.url, timeout=5)",
            "Resuelve el destino y comprueba que no sea loopback, privada ni de enlace local. Desactiva la redireccion automatica y fija un tiempo limite.",
            "A10:2021 - Server-Side Request Forgery",
        ),
        (
            "CWE-287",
            "Autenticacion debiles",
            "AUTH",
            "HIGH",
            "Credenciales por defecto, sesiones sin rotar o comparaciones no constante tiempo permiten suplantar a un usuario legitimo.",
            "if contrasena == contrasena_del_usuario:",
            "if hmac.compare_digest(contrasena, hash_almacenado):",
            "Usa bcrypt o argon2, compara en tiempo constante, rota la sesion al cambiar el privilegio y exige verificacion de correo.",
            "A07:2021 - Identification and Authentication Failures",
        ),
        (
            "CWE-327",
            "Algoritmos criptograficos debiles",
            "CRYPTOGRAPHY",
            "MEDIUM",
            "MD5, SHA-1 y DES no resisten ataques de fuerza bruta y permiten reconstruir secretos o falsificar firmas.",
            "hash = hashlib.md5(contrasena.encode()).hexdigest()",
            "hash = bcrypt.hashpw(contrasena.encode(), bcrypt.gensalt())",
            "Para contrasenas usa bcrypt o argon2id. Para firmas usa HMAC-SHA256 o Ed25519. No inventes cifrado propio.",
            "A02:2021 - Cryptographic Failures",
        ),
        (
            "CWE-1104",
            "Componentes con vulnerabilidades conocidas",
            "DEPENDENCY",
            "MEDIUM",
            "Una dependencia transitiva con CVE conocida expone la aplicacion aunque el codigo propio no tenga el fallo.",
            "flask==1.0.0",
            "flask==3.1.0  # con Dependabot y revision de CVEs en CI",
            "Mantén un inventario SBOM, fija versiones parcheadas y bloquea la integracion continua cuando una dependencia tenga CVE alta sin corregir.",
            "A06:2021 - Vulnerable and Outdated Components",
        ),
        (
            "CWE-841",
            "Validacion de restricciones incorrecta",
            "LOGIC",
            "MEDIUM",
            "Sin validar el rango de una cantidad, un atacante puede provoke agotamiento de recursos, saldos negativos o saltarse un flujo de aprobacion.",
            "cantidad = int(parametro_cantidad)  # sin cota superior",
            "cantidad = Field(ge=1, le=1000)  # validado por esquema",
            "Valida rangos en el esquema, no en el controlador. Devuelve 422 ante valores fuera de dominio en lugar de ajustar en silencio.",
            "A04:2021 - Insecure Design",
        ),
    ]

    connection = op.get_bind()
    for (
        reference_code,
        title,
        category,
        severity,
        risk_summary,
        vulnerable_example,
        secure_example,
        mitigation,
        owasp_category,
    ) in entries:
        connection.execute(
            sa.text(
                """
                INSERT INTO knowledge_entries (
                    id, reference_code, title, category, severity, risk_summary,
                    vulnerable_example, secure_example, mitigation, owasp_category
                ) VALUES (
                    gen_random_uuid(), :code, :title, :category, :severity, :risk,
                    :vulnerable, :secure, :mitigation, :owasp
                )
                """
            ),
            {
                "code": reference_code,
                "title": title,
                "category": category,
                "severity": severity,
                "risk": risk_summary,
                "vulnerable": vulnerable_example,
                "secure": secure_example,
                "mitigation": mitigation,
                "owasp": owasp_category,
            },
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS knowledge_entries")
    op.execute("DROP TRIGGER IF EXISTS trg_protect_audit_log_append_only ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS protect_audit_log_append_only()")
    op.execute("DROP TABLE IF EXISTS audit_log")
    op.execute("DROP TYPE IF EXISTS knowledge_severity_enum")
    op.execute("DROP TYPE IF EXISTS knowledge_category_enum")
    op.execute("DROP TYPE IF EXISTS audit_action_enum")
