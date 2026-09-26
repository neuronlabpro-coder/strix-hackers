"""Descarga y sincronización de los feeds oficiales de CVE y KEV.

El worker no habla con `cve.org` en modo síncrono durante una petición: la
sincronización es una tarea periódica que deja la tabla lista para que el panel
lea sin escribir nunca en un GET.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.apps.cve_database.models import CVESeverityEnum
from backend.apps.cve_database.service import (
    CveFeedError,
    CveSyncReport,
    normalize_cve_id,
    sync_cve_catalog,
)
from backend.core.config import settings
from backend.core.database import create_database_engine
from backend.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# Rangos de CVSS por severidad. Es la convención de NVD y de FIRST; se calcula en
# lugar de leerse del feed porque no todas las fuentes publican la severidad
# textual y todas publican la puntuación.
_SEVERITY_THRESHOLDS: tuple[tuple[float, CVESeverityEnum], ...] = (
    (9.0, CVESeverityEnum.CRITICAL),
    (7.0, CVESeverityEnum.HIGH),
    (4.0, CVESeverityEnum.MEDIUM),
    (0.0, CVESeverityEnum.LOW),
)


def severity_from_cvss(score: float) -> CVESeverityEnum:
    """Deriva la severidad normalizada a partir del CVSS."""

    for threshold, severity in _SEVERITY_THRESHOLDS:
        if score >= threshold:
            return severity
    return CVESeverityEnum.LOW


def _as_float(value: object) -> float | None:
    """Convierte un valor del feed a `float`, o `None` si no es un número.

    Los feeds JSON no tienen esquema fuerte: `score` llega como número, como cadena o
    como `null` según el endpoint. Aceptar cualquiera de los tres es correcto, pero
    capturar el `TypeError` de `float(None)` en cada llamada es ruido.

    El `bool` se descarta antes porque `float(True)` es `1.0` en Python y un `true`
    textual en el feed es un dato corrupto, no un 1.
    """

    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _to_feed_entry(record: Mapping[str, Any]) -> dict[str, object] | None:
    """Traduce una entrada del feed de CIRCL/NVD al formato interno."""

    raw_id = record.get("id") or record.get("cve_id")
    try:
        cve_id = normalize_cve_id(raw_id)
    except ValueError:
        return None
    score_raw = record.get("cvss")
    if not isinstance(score_raw, Mapping):
        return None
    cvss = _as_float(score_raw.get("score"))
    if cvss is None or not 0.0 <= cvss <= 10.0:
        return None
    summary = record.get("summary")
    published = record.get("published")
    if not isinstance(summary, str) or not summary.strip():
        return None
    if not isinstance(published, str) or not published.strip():
        return None
    epss = record.get("epss")
    epss_value: Decimal | None = None
    if isinstance(epss, list) and epss and isinstance(epss[0], Mapping):
        value = _as_float(epss[0].get("epss"))
        if value is not None and 0.0 <= value <= 1.0:
            epss_value = Decimal(str(value))
    return {
        "cve_id": cve_id,
        "severity": severity_from_cvss(cvss).value,
        "cvss_score": Decimal(str(cvss)),
        "epss_score": epss_value,
        "is_kev": False,
        "published_at": published,
        "description": summary.strip(),
    }


def _kev_to_feed_entries(payload: Mapping[str, Any]) -> list[dict[str, object]]:
    """Traduce el catálogo KEV de CISA a los campos internos.

    KEV solo publica severidad `CRITICAL` y no aporta CVSS ni EPSS, así que se
    combinan los identificadores con lo que ya sepa el catálogo CVE en vez de
    inventar una puntuación.
    """

    vulnerabilities = payload.get("vulnerabilities")
    if not isinstance(vulnerabilities, list):
        raise CveFeedError("El feed KEV no contiene la lista de vulnerabilidades")
    entries: list[dict[str, object]] = []
    for item in vulnerabilities:
        if not isinstance(item, Mapping):
            continue
        try:
            cve_id = normalize_cve_id(item.get("cveID"))
        except ValueError:
            continue
        entries.append(
            {
                "cve_id": cve_id,
                "severity": CVESeverityEnum.CRITICAL.value,
                "cvss_score": Decimal("9.8"),
                "epss_score": None,
                "is_kev": True,
                "published_at": item.get("dateAdded") or datetime.now(UTC).isoformat(),
                "description": (
                    item.get("shortDescription")
                    if isinstance(item.get("shortDescription"), str)
                    else "Vulnerabilidad con explotación activa confirmada por CISA"
                ),
            }
        )
    return entries


async def _download_json(url: str) -> Mapping[str, Any]:
    try:
        async with httpx.AsyncClient(
            timeout=settings.cve_sync_http_timeout_seconds
        ) as client:
            response = await client.get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
            payload: object = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise CveFeedError(f"No se pudo descargar el feed {url}") from error
    if not isinstance(payload, dict):
        raise CveFeedError(f"El feed {url} no devolvió un objeto JSON")
    return payload


async def fetch_official_feeds() -> list[dict[str, object]]:
    """Descarga y normaliza ambos feeds oficiales."""

    nvd_payload, kev_payload = await asyncio.gather(
        _download_json(settings.cve_nvd_feed_url),
        _download_json(settings.cve_kev_feed_url),
    )
    nvd_raw = nvd_payload.get("data")
    if not isinstance(nvd_raw, list):
        raise CveFeedError("El feed CVE no contiene la lista de vulnerabilidades")
    entries = [
        entry
        for entry in (_to_feed_entry(item) for item in nvd_raw if isinstance(item, Mapping))
        if entry is not None
    ]
    entries.extend(_kev_to_feed_entries(kev_payload))
    return entries


async def _sync_with_feeds(since: datetime | None) -> CveSyncReport:
    """Descarga los feeds y los sincroniza en una sesión propia.

    El motor se crea y se destruye dentro de la función en lugar de usar la sesión
    global: el worker es un proceso de larga vida y una conexión por tarea periódica
    que se filtra acabaría agotando el pool de PostgreSQL sin ningún error visible.
    """

    engine = create_database_engine(settings)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with factory() as session:
            return await sync_cve_catalog(session, await fetch_official_feeds(), since=since)
    finally:
        await engine.dispose()


def _sync_sync(since: datetime | None) -> CveSyncReport:
    """Punto de entrada síncrono para Celery, que ejecuta las corrutinas con asyncio."""

    return asyncio.run(_sync_with_feeds(since))


@celery_app.task(name="cve.sync_catalog")
def sync_cve_catalog_task(lookback_hours: int | None = None) -> dict[str, int]:
    """Tarea periódica de sincronización del catálogo CVE.

    Acepta una ventana de lookback explícita para que el primer poblamiento
    pueda pedir un rango amplio y las ejecuciones posteriores solo miren lo
    reciente. Un feed caído devuelve 0 en vez de propagar el error: el catálogo
    conserva lo que ya tenía, que es mejor que vaciarlo.
    """

    hours = lookback_hours if lookback_hours is not None else settings.cve_sync_interval_hours * 2
    since = datetime.now(UTC) - timedelta(hours=hours)
    try:
        report = _sync_sync(since)
    except CveFeedError:
        logger.exception("No se pudo sincronizar el catálogo CVE")
        return {
            "received": 0,
            "inserted": 0,
            "updated": 0,
            "skipped_older": 0,
            "skipped_invalid": 0,
        }
    return {
        "received": report.received,
        "inserted": report.inserted,
        "updated": report.updated,
        "skipped_older": report.skipped_older,
        "skipped_invalid": report.skipped_invalid,
    }
