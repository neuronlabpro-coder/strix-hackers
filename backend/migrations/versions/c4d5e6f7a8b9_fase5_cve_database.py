"""fase5_cve_database_and_telemetry

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-09-26 14:10:00

Crea `cve_records` como catálogo público de referencia con índice GIN de búsqueda
full-text, y siembra 50 registros representativos para que el panel y las
pruebas tengan contenido desde el primer arranque.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, Sequence[str], None] = "b3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (cve_id, severidad, cvss, epss, kev, días desde publicación, descripción)
#
# Los identificadores siguen el patrón real `CVE-<año>-<secuencia>` porque el CHECK
# de la tabla lo exige, y las fechas se calculan desde `now()` para que el orden
# por publicación del panel sea coherente en cualquier momento en que se aplique
# la migración.
SEED = [
    ("CVE-2026-100599", "CRITICAL", 9.8, 0.94, True, 2, "Inyección SQL en el panel de control de la appliance de gestión"),
    ("CVE-2026-100598", "CRITICAL", 9.1, 0.71, True, 4, "Ejecución remota de código sin autenticación en el servicio de copia de seguridad"),
    ("CVE-2026-100597", "CRITICAL", 9.4, 0.62, False, 6, "Deserialización insegura en el servicio de indexación"),
    ("CVE-2026-100596", "CRITICAL", 9.0, 0.35, False, 9, "Traversal de rutas en el gestor de descargas que permite leer archivos arbitrarios"),
    ("CVE-2026-100595", "CRITICAL", 9.3, 0.48, True, 11, "SSRF en el verificador de webhooks que alcanza la red interna"),
    ("CVE-2026-100594", "HIGH", 8.4, 0.28, False, 14, "Credenciales por defecto en la consola de administración heredada"),
    ("CVE-2026-100593", "HIGH", 7.8, 0.19, False, 17, "XSS almacenado en el campo de comentarios del ticket"),
    ("CVE-2026-100592", "HIGH", 8.1, 0.44, True, 20, "Bypass de autenticación por JWT con algoritmo sin verificar"),
    ("CVE-2026-100591", "HIGH", 7.5, 0.11, False, 23, "Exposición de secretos de API en los registros de la aplicación"),
    ("CVE-2026-100590", "HIGH", 8.6, 0.52, False, 26, "Escalada de privilegios local en el agente de instalación"),
    ("CVE-2026-100589", "HIGH", 7.2, 0.08, False, 29, "Denegación de servicio por consumo recursivo de JSON"),
    ("CVE-2026-100588", "HIGH", 8.9, 0.66, True, 33, "Inyección de comandos en el parámetro de host del proxy inverso"),
    ("CVE-2026-100587", "HIGH", 7.4, 0.15, False, 37, "Fuga de información en las cabeceras de respuesta del API"),
    ("CVE-2026-100586", "HIGH", 7.6, 0.22, False, 41, "CORS permisivo que expone el API a cualquier origen"),
    ("CVE-2026-100585", "HIGH", 8.0, 0.31, False, 45, "Algoritmo de firma débil que permite falsificar webhooks"),
    ("CVE-2025-200001", "HIGH", 7.7, 0.21, False, 120, "Desbordamiento de búfer en el parser de imágenes del servicio de miniaturas"),
    ("CVE-2025-200002", "MEDIUM", 6.5, 0.09, False, 150, "XSS reflejado en el parámetro de búsqueda del portal"),
    ("CVE-2025-200003", "MEDIUM", 5.9, 0.04, False, 180, "Retención de cabeceras de autenticación en caché compartida"),
    ("CVE-2025-200004", "MEDIUM", 6.8, 0.13, False, 210, "Validación insuficiente en el alta de usuarios que permite emails duplicados"),
    ("CVE-2025-200005", "MEDIUM", 5.3, 0.02, False, 240, "Condición de carrera al devolver reutilización de tokens de un solo uso"),
    ("CVE-2025-200006", "MEDIUM", 6.1, 0.06, False, 270, "Cookies de sesión sin el atributo Secure en el subdominio"),
    ("CVE-2025-200007", "MEDIUM", 5.7, 0.03, False, 300, "Paginación sin límite que permite enumerar recursos por fuerza bruta"),
    ("CVE-2025-200008", "MEDIUM", 6.4, 0.17, False, 330, "Cifrado débil del canal de sincronización entre nodos"),
    ("CVE-2024-300001", "MEDIUM", 4.9, 0.05, False, 500, "Filtración de información en los mensajes de error del login"),
    ("CVE-2024-300002", "MEDIUM", 5.5, 0.07, False, 530, "Ausencia de rate limit en el restablecimiento de contraseña"),
    ("CVE-2024-300003", "LOW", 3.7, 0.01, False, 560, "Versión de biblioteca obsoleta con vulnerabilidad de baja severidad"),
    ("CVE-2024-300004", "LOW", 2.6, 0.00, False, 590, "Encabezado de versión de software que facilita el reconocimiento"),
    ("CVE-2024-300005", "LOW", 3.3, 0.01, False, 620, "Content-Type permisivo en la descarga de archivos estáticos"),
    ("CVE-2023-400001", "HIGH", 7.1, 0.12, False, 700, "XSS en el nombre de archivo durante la previsualización"),
    ("CVE-2023-400002", "MEDIUM", 6.2, 0.04, False, 760, "Cookie de seguimiento sin SameSite en el flujo de OAuth"),
    ("CVE-2023-400003", "CRITICAL", 9.6, 0.88, True, 820, "Ejecución remota en el agente de monitoring heredado"),
    ("CVE-2023-400004", "LOW", 3.9, 0.02, False, 880, "Fuga de la pila técnica en la página de error"),
    ("CVE-2022-500001", "HIGH", 7.8, 0.18, False, 1000, "CSRF en la acción de cambio de contraseña"),
    ("CVE-2022-500002", "MEDIUM", 5.8, 0.05, False, 1100, "Almacenamiento de contraseñas con hash rápido"),
    ("CVE-2022-500003", "HIGH", 8.3, 0.34, True, 1200, "Inyección SQL en el módulo de reportes heredado"),
    ("CVE-2021-600001", "HIGH", 7.3, 0.10, False, 1400, "Permisos excesivos en el endpoint de exportación"),
    ("CVE-2021-600002", "MEDIUM", 6.0, 0.03, False, 1500, "HTTP sin cifrar en la consola de administración"),
    ("CVE-2021-600003", "LOW", 3.1, 0.00, False, 1600, "Divulgación de la ruta interna de archivos temporales"),
    ("CVE-2020-700001", "CRITICAL", 9.8, 0.97, True, 1900, "Ejecución remota sin autenticación en el servicio de ficheros"),
    ("CVE-2020-700002", "HIGH", 7.6, 0.16, False, 2000, "XSS reflejado en el buscador del sitio público"),
    ("CVE-2019-800001", "MEDIUM", 5.4, 0.02, False, 2200, "Contraseñas vacías permitidas en cuentas de servicio"),
    ("CVE-2019-800002", "HIGH", 8.0, 0.27, False, 2400, "Deserialización de objetos Java en el endpoint de importación"),
    ("CVE-2017-900001", "HIGH", 7.5, 0.14, True, 3200, "Divulgación de código fuente por configuración errónea del servidor"),
    ("CVE-2014-010001", "MEDIUM", 5.0, 0.01, False, 4400, "CSRF en la interfaz de configuración del dispositivo"),
    ("CVE-2012-300001", "MEDIUM", 5.3, 0.02, False, 5000, "Inyección SQL en el parámetro de ordenación"),
    ("CVE-1999-0001", "LOW", 3.5, 0.00, False, 9800, "Buffer overflow en el servicio de finger"),
    ("CVE-1999-0002", "MEDIUM", 6.7, 0.05, False, 9700, "Denegación de servicio por agotamiento de descriptores de fichero"),
    ("CVE-2001-0001", "HIGH", 7.0, 0.09, False, 9200, "Buffer overflow en el demonio de configuración"),
    ("CVE-2003-0001", "LOW", 2.1, 0.00, False, 8400, "Filtración de la versión exacta del software en el banner"),
    ("CVE-2008-0001", "MEDIUM", 6.0, 0.06, False, 6600, "Debilidad criptográfica en la generación de nonces"),
]


def upgrade() -> None:
    op.execute("CREATE TYPE cve_severity_enum AS ENUM ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW')")
    op.execute(
        """
        CREATE TABLE cve_records (
            cve_id VARCHAR(32) PRIMARY KEY,
            severity cve_severity_enum NOT NULL,
            cvss_score NUMERIC(4, 2) NOT NULL,
            epss_score NUMERIC(6, 5),
            is_kev BOOLEAN NOT NULL DEFAULT false,
            published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            description TEXT NOT NULL,
            CONSTRAINT ck_cve_records_cvss_range CHECK (cvss_score >= 0 AND cvss_score <= 10),
            CONSTRAINT ck_cve_records_epss_range CHECK (epss_score IS NULL OR (epss_score >= 0 AND epss_score <= 1)),
            CONSTRAINT ck_cve_records_id_format CHECK (cve_id ~ '^CVE-[0-9]{4}-[0-9]{4,}$')
        )
        """
    )
    op.execute("CREATE INDEX ix_cve_records_severity_published ON cve_records (severity, published_at)")
    op.execute("CREATE INDEX ix_cve_records_kev_published ON cve_records (is_kev, published_at)")
    # `pg_trgm` debe existir antes de crear el índice trgm: PostgreSQL valida la
    # clase de operadores en el momento del `CREATE INDEX`, no después.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    # El GIN index se construye sobre `to_tsvector` de la descripción. SQLAlchemy
    # no modela índices de expresión con una declaración legible, así que el
    # Alembic lo crea directamente y `alembic check` no intenta reconstruirlo.
    op.execute(
        """
        CREATE INDEX ix_cve_records_search ON cve_records
        USING GIN (to_tsvector('spanish'::regconfig, coalesce(description, '')))
        """
    )
    op.execute(
        """
        CREATE INDEX ix_cve_records_id_trgm ON cve_records
        USING GIN (cve_id gin_trgm_ops)
        """
    )

    connection = op.get_bind()
    for cve_id, severity, cvss, epss, is_kev, days_ago, description in SEED:
        connection.execute(
            sa.text(
                """
                INSERT INTO cve_records (
                    cve_id, severity, cvss_score, epss_score, is_kev,
                    published_at, description
                ) VALUES (
                    :cve_id, :severity, :cvss, :epss, :is_kev,
                    now() - make_interval(days => :days_ago), :description
                )
                ON CONFLICT (cve_id) DO NOTHING
                """
            ),
            {
                "cve_id": cve_id,
                "severity": severity,
                "cvss": cvss,
                "epss": epss,
                "is_kev": is_kev,
                "days_ago": days_ago,
                "description": description,
            },
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS cve_records")
    op.execute("DROP TYPE IF EXISTS cve_severity_enum")
