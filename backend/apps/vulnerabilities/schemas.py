"""Esquemas Pydantic de la API de vulnerabilidades."""

from datetime import datetime
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field

from backend.apps.vulnerabilities.models import IssueStatusEnum, SeverityEnum


class VulnerabilityListItem(BaseModel):
    """Resumen de un hallazgo para listados paginados."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    run_id: UUID
    title: str
    severity: SeverityEnum
    cvss_score: float
    cve_id: str | None
    affected_target: str
    status: IssueStatusEnum
    discovered_at: datetime


class SeverityCount(BaseModel):
    """Conteo de hallazgos por severidad dentro de un conjunto ya filtrado.

    ## Por qué vive aquí y no en `dashboard.schemas`

    Porque cuenta hallazgos, no KPIs de panel, y lo consumen tres superficies distintas: el
    resumen del dashboard, la página de issues y el detalle de una ejecución. Duplicar el
    modelo tres veces es una manera de que un día los tres digan cosas distintas sobre el
    mismo dato; importarlo de aquí es lo que hace imposible esa divergencia.
    """

    model_config = ConfigDict(from_attributes=True)

    severity: SeverityEnum
    total: int = Field(ge=0)


class StatusCount(BaseModel):
    """Conteo de hallazgos por estado de remediación dentro de un conjunto ya filtrado."""

    model_config = ConfigDict(from_attributes=True)

    status: IssueStatusEnum
    total: int = Field(ge=0)


class VulnerabilityDetail(VulnerabilityListItem):
    """Detalle completo con evidencias técnicas."""

    description: str
    affected_line: str | None
    poc_reproduction_raw: str
    autofix_patch_diff: str | None
    updated_at: datetime


class AutofixRequest(BaseModel):
    """Identificador de la revisión que origina el hallazgo."""

    review_id: UUID


class VulnerabilityTriageRequest(BaseModel):
    """Triaje de un hallazgo: únicamente su estado de remediación.

    R4 hace la defensa en dos capas. `extra="forbid"` hace que cualquier campo
    forense enviado por el cliente (`title`, `poc_reproduction_raw`, `cvss_score`,
    `cve_id`, `affected_target`, `autofix_patch_diff`, `run_id`, ...) provoque un
    `422` en el borde, antes de tocar la base de datos. Si alguien alcanzara el
    endpoint por otra vía, el trigger `protect_vulnerability_evidence` seguiría
    bloqueando la escritura a nivel de PostgreSQL.
    """

    model_config = ConfigDict(extra="forbid")

    status: IssueStatusEnum = Field(
        description="Único campo mutable: el estado del ciclo de vida de remediación."
    )


class VulnerabilityTriageResponse(BaseModel):
    """Resultado del triaje con la marca temporal del cambio."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: IssueStatusEnum
    updated_at: datetime
    changed: bool = Field(
        description="Falso cuando el estado solicitado ya era el actual: no se audita."
    )


class AutofixResponse(BaseModel):
    """URL del Pull/Merge Request de remediación."""

    autofix_url: AnyHttpUrl


class RemediationResponse(BaseModel):
    """Lo que queda tras abrir una propuesta de remediación.

    ## Por qué devuelve el `status` y no solo la URL

    Porque el hallazgo ha cambiado de estado y quien llama necesita saberlo **sin** una
    segunda petición. La URL sola obligaría al panel a recargar la ficha para descubrir que el
    botón de generar propuesta ya no tiene sentido, y un panel que no se ha recargado sigue
    ofreciendo una acción que ya está hecha.

    Y por qué la URL es `str` y no `AnyHttpUrl` como en `AutofixResponse`: aquí la respuesta la
    consume la interfaz para pintar un enlace, y `AnyHttpUrl` serializa a un objeto que en
    algunos clientes llega como `{}`. `AutofixResponse` sí valida el formato porque la
    validación es lo que garantiza que el enlace es navegable, y esa garantía no se pierde:
    `create_autofix_branch_and_pr` rechaza una URL vacía y el cliente Git no devuelve otra
    cosa.
    """

    vulnerability_id: UUID
    #: URL del PR de remediación.
    #:
    #: ## Por qué `AnyHttpUrl` y no `str`
    #:
    #: Porque su hermano de la misma respuesta —`autofix_url`, unas líneas más arriba— **sí**
    #: valida, y la asimetría no tenía motivo. Hoy el valor viene de `html_url` de la API de
    #: GitHub o de `web_url` de la de GitLab, sobre TLS, así que un `javascript:` no llega.
    #:
    #: Pero el valor acaba en un `href` de un `<a>`, y la garantía de que no lo es depende de dos
    #: cosas que no están en este fichero: la API del proveedor y el tipo de la columna. Un
    #: cambio de proveedor, o un `str` más arriba, rompe la cadena sin que nada falle. El tipo
    #: lo dice en el sitio donde se decide el tipo.
    remediation_pr_url: AnyHttpUrl
    status: IssueStatusEnum


class VulnerabilityPage(BaseModel):
    """Página de vulnerabilidades de un tenant.

    ## Por qué `severity_breakdown` y `status_breakdown` acompañan a la página

    Porque el reparto de la lista **no** se puede deducir de los elementos de la página. Con
    `limit=25` y 4 000 hallazgos, contar severidades sobre las 25 filas de la página da una
    distribución que no es la del conjunto, y el panel la presents como si lo fuera: es la
    diferencia entre «hay 3 críticos» y «en esta página hay 3 críticos, y hay 41 en total».

    Los dos desgloses se calculan en SQL con **los mismos filtros** que la lista, así que
    cuentan todos los hallazgos que casan con el filtro activo, no solo los de la página. Por
    eso el filtro de severidad o de estado no los invalida: si filtras por `CRITICAL`, el
    desglose por severidad muestra `CRITICAL: n` y el resto a cero, que es exactamente la
    lectura correcta de esa pantalla.
    """

    items: list[VulnerabilityListItem]
    total: int
    limit: int
    offset: int
    severity_breakdown: list[SeverityCount]
    status_breakdown: list[StatusCount]
