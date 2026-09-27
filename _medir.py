"""Medida los campos reales de los modelos que el seeder tiene que rellenar."""

import io
import os
import re
import sys

sys.path.insert(0, os.getcwd())

from sqlalchemy import MetaData  # noqa: E402

from backend.core.database import Base  # noqa: E402

import backend.apps.api_access.models  # noqa: E402,F401
import backend.apps.assets.models  # noqa: E402,F401
import backend.apps.audit.models  # noqa: E402,F401
import backend.apps.billing.models  # noqa: E402,F401
import backend.apps.llm_router.models  # noqa: E402,F401
import backend.apps.organizations.models  # noqa: E402,F401
import backend.apps.pentests.models  # noqa: E402,F401
import backend.apps.repositories.models  # noqa: E402,F401
import backend.apps.support.models  # noqa: E402,F401
import backend.apps.vulnerabilities.models  # noqa: E402,F401

INTERESAN = {
    "organizations",
    "users",
    "memberships",
    "repositories",
    "pull_request_reviews",
    "pentest_runs",
    "vulnerabilities",
    "verified_domains",
    "discovered_assets",
    "support_tickets",
    "ticket_messages",
    "credit_ledger",
    "stripe_events",
    "audit_log",
    "api_tokens",
    "llm_model_configs",
    "support_ticket_sequences",
}

salida = []
for tabla in Base.metadata.sorted_tables:
    if tabla.name not in INTERESAN:
        continue
    salida.append(f"### {tabla.name}")
    for columna in tabla.columns:
        flags = []
        if columna.primary_key:
            flags.append("PK")
        if columna.foreign_keys:
            flags.append("FK->" + ",".join(sorted(fk.column.table.name for fk in columna.foreign_keys)))
        if not columna.nullable:
            flags.append("NOT NULL")
        if columna.default is not None or columna.server_default is not None:
            flags.append("default")
        if columna.unique:
            flags.append("UNIQUE")
        tipo = str(columna.type)
        if len(tipo) > 42:
            tipo = tipo[:39] + "..."
        salida.append(f"  {columna.name:34} {tipo:45} {' '.join(flags)}")
    salida.append("")

with io.open("_modelos.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(salida))
print("tablas medidas:", len(salida))
