"""Normalización, ingesta y consulta del catálogo CVE."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.cve_database.models import CVERecord, CVESeverityEnum

logger = logging.getLogger(__name__)

# El CVE existe desde 1999. Un año anterior significa un feed corrupto o un
# identificador sintético, y aceptarlo ensuciaría el filtro por año.
_MIN_CVE_YEAR = 1999
_CVE_ID_PATTERN = re.compile(r"^CVE-(\d{4})-(\d{4,})$")
_SEARCH_VECTOR = "to_tsvector('spanish', coalesce(cve_id, '') || ' ' || coalesce(description, ''))"
_MAX_CVSS = Decimal("10")


class CveFeedError(RuntimeError):
    """El feed oficial no se pudo descargar o su formato no es utilizable."""


def normalize_cve_id(raw: object) -> str:
    """Normaliza un identificador CVE a su forma canónica en mayúsculas."""

    if not isinstance(raw, str):
        raise ValueError("El identificador CVE debe ser texto")
    candidate = raw.strip().upper()
    match = _CVE_ID_PATTERN.match(candidate)
    if match is None:
        raise ValueError(f"Identificador CVE inválido: {raw!r}")
    if int(match.group(1)) < _MIN_CVE_YEAR:
        raise ValueError(f"Año de CVE anterior a {_MIN_CVE_YEAR}: {raw!r}")
    return candidate


def cve_year(cve_id: str) -> int:
    """Extrae el año de un identificador CVE ya normalizado."""

    match = _CVE_ID_PATTERN.match(cve_id)
    if match is None:
        raise ValueError(f"Identificador CVE inválido: {cve_id!r}")
    return int(match.group(1))


def _parse_decimal(value: object, *, field: str) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field} no es un número válido") from error


def _parse_published(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("published_at no es una fecha ISO válida") from error
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    raise ValueError("published_at es obligatorio")


def parse_feed_entry(entry: Mapping[str, object]) -> dict[str, object]:
    """Convierte una entrada del feed oficial en los campos de `CVERecord`.

    Devuelve los valores ya validados. Lanza `ValueError` con el motivo para que
    `sync_cve_catalog` pueda contar el descarte sin abortar el resto del lote: un
    feed con un registro corrupto no debe impedir sincronizar los otros diez mil.
    """

    cve_id = normalize_cve_id(entry.get("cve_id"))
    severity_raw = entry.get("severity")
    if not isinstance(severity_raw, str):
        raise ValueError(f"{cve_id}: severity es obligatorio")
    try:
        severity = CVESeverityEnum(severity_raw.strip().upper())
    except ValueError as error:
        raise ValueError(f"{cve_id}: severidad desconocida {severity_raw!r}") from error

    cvss = _parse_decimal(entry.get("cvss_score"), field=f"{cve_id}: cvss_score")
    if cvss is None or not (Decimal(0) <= cvss <= _MAX_CVSS):
        raise ValueError(f"{cve_id}: cvss_score fuera de rango o ausente")

    epss = _parse_decimal(entry.get("epss_score"), field=f"{cve_id}: epss_score")
    if epss is not None and not (Decimal(0) <= epss <= Decimal(1)):
        raise ValueError(f"{cve_id}: epss_score fuera de rango")

    description = entry.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"{cve_id}: description es obligatoria")

    return {
        "cve_id": cve_id,
        "severity": severity,
        "cvss_score": cvss,
        "epss_score": epss,
        "is_kev": bool(entry.get("is_kev", False)),
        "published_at": _parse_published(entry.get("published_at")),
        "description": description.strip(),
    }


@dataclass(frozen=True, slots=True)
class CveSyncReport:
    """Resultado de una sincronización, para que el operador vea qué pasó."""

    received: int
    inserted: int
    updated: int
    skipped_older: int
    skipped_invalid: int

    @property
    def applied(self) -> int:
        return self.inserted + self.updated


async def sync_cve_catalog(
    session: AsyncSession,
    feed: Iterable[Mapping[str, object]],
    *,
    since: datetime | None = None,
) -> CveSyncReport:
    """Inserta o actualiza registros del feed oficial. Es idempotente.

    `since` acota la ingesta a lo publicado después de esa marca, que es lo que
    hace posible una descarga incremental: reingerir el feed completo cada noche
    sería correcto pero caro, y no se puede distinguir lo nuevo de lo viejo
    sin mirar `published_at`.
    """

    received = inserted = updated = skipped_older = skipped_invalid = 0
    for raw_entry in feed:
        if not isinstance(raw_entry, Mapping):
            skipped_invalid += 1
            continue
        received += 1
        try:
            values = parse_feed_entry(cast(Mapping[str, object], raw_entry))
        except ValueError as error:
            logger.warning("Entrada de feed CVE descartada: %s", error)
            skipped_invalid += 1
            continue
        published_at = cast(datetime, values["published_at"])
        if since is not None and published_at < since:
            skipped_older += 1
            continue
        existing = (
            await session.execute(
                select(CVERecord).where(CVERecord.cve_id == values["cve_id"])
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(CVERecord(**values))
            inserted += 1
            continue
        # Solo se cuenta como actualizado lo que realmente cambió. Una resincronización
        # que reescribe las mismas filas es un no-op, y reportarla como `updated`
        # convertiría el informe de la tarea periódica en una mentira: el operador
        # vería "1500 actualizados" cada noche y no sabría si el feed trajo algo.
        changed = False
        for field, value in values.items():
            if getattr(existing, field) != value:
                setattr(existing, field, value)
                changed = True
        if changed:
            updated += 1
    await session.commit()
    logger.info(
        "Sincronización CVE: recibidos=%s insertados=%s actualizados=%s "
        "anteriores=%s descartados=%s",
        received,
        inserted,
        updated,
        skipped_older,
        skipped_invalid,
    )
    return CveSyncReport(
        received=received,
        inserted=inserted,
        updated=updated,
        skipped_older=skipped_older,
        skipped_invalid=skipped_invalid,
    )


def _escape_like(term: str) -> str:
    """Escapa los comodines de LIKE para que el término se busque literal.

    Sin esto, buscar `a_b` devolvería `axb`: los identificadores CVE no
    contienen `_`, pero una búsqueda de texto libre sí puede, y el resultado
    sería una lista de coincidencias que el usuario no pidió.
    """

    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_search_filters(
    *,
    query: str | None,
    severity: CVESeverityEnum | None,
    is_kev_only: bool,
    year: int | None,
) -> list[Any]:
    """Construye los filtros del buscador CVE.

    Un término con guiones (`CVE-2026-100599`) no debe pasar por el
    `to_tsvector`, que partiría el texto en tokens y perdería el identificador.
    Se busca primero por coincidencia del `cve_id` y, en paralelo, por texto
    completo sobre la descripción.
    """

    filters: list[Any] = []
    if severity is not None:
        filters.append(CVERecord.severity == severity)
    if is_kev_only:
        filters.append(CVERecord.is_kev.is_(True))
    if year is not None:
        filters.append(CVERecord.cve_id.like(f"CVE-{year}-%"))
    if query:
        term = query.strip()
        if term:
            filters.append(
                CVERecord.cve_id.ilike(f"%{_escape_like(term)}%", escape="\\")
                | func.to_tsvector("spanish", CVERecord.description).op("@@")(
                    func.plainto_tsquery("spanish", term)
                )
            )
    return filters


async def available_years(session: AsyncSession) -> list[int]:
    """Años presentes en el catálogo, del más reciente al más antiguo.

    El orden se aplica en Python y no en SQL: PostgreSQL exige que la expresión
    de `ORDER BY` aparezca en la lista de `SELECT` cuando hay `DISTINCT`, y
    duplicar el `substring` en la consulta por eso sería peor que ordenar veinte
    números en memoria.
    """

    result = await session.execute(select(func.substring(CVERecord.cve_id, 5, 4)).distinct())
    return sorted((int(year) for year in result.scalars().all()), reverse=True)


async def count_kev(session: AsyncSession) -> int:
    """Total de vulnerabilidades con explotación activa, independientemente del límite."""

    result = await session.execute(
        select(func.count(CVERecord.cve_id)).where(CVERecord.is_kev.is_(True))
    )
    return int(result.scalar_one())


async def fetch_trending_kev(
    session: AsyncSession, *, limit: int = 20
) -> Sequence[CVERecord]:
    """KEV exploited más recientes: lo que un atacante está usando ahora mismo."""

    result = await session.execute(
        select(CVERecord)
        .where(CVERecord.is_kev.is_(True))
        .order_by(CVERecord.published_at.desc(), CVERecord.cve_id)
        .limit(limit)
    )
    return result.scalars().all()


async def fetch_record(session: AsyncSession, cve_id: str) -> CVERecord | None:
    result = await session.execute(select(CVERecord).where(CVERecord.cve_id == cve_id))
    return result.scalar_one_or_none()
