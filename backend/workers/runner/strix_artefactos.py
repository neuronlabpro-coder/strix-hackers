"""Los artefactos que el motor deja de verdad, leídos como lo que son.

## Por qué este módulo existe

El runner historical esperaba un `results.json` con `scan_id`, `status` y `findings[]`. Ese
contrato **no existe**: un grep de todo el motor no devuelve ni un fichero que lo mencione,
y la imagen `strix_sandbox_image` ni siquiera trae el motor dentro (`exec: strix: not found`).
Esperar ese fichero era esperar algo que ningún camino del motor puede producir.

Lo que el motor **sí** produce está verificado sobre un escaneo real terminado, y son cuatro
ficheros en `<cwd>/strix_runs/<run_name>/`:

- `run.json`: el registro del run —estado, inicio, fin, objetivo, tokens y coste—.
- `findings.sarif`: SARIF 2.1.0. Mezcla **hallazgos** y **registros de cobertura**.
- `coverage.json`: cobertura, huecos y si el escaneo se completó entero.
- `penetration_test_report.md`: el informe legible que escribe una persona.

Este módulo las lee y devuelve **objetos**, no diccionarios: el consumidor no vuelve a
mirar dentro del JSON, y un cambio de forma del artefacto rompe aquí y no en tres sitios.

## Por qué SARIF necesita un discriminador y no basta con `kind != "pass"`

Porque un `result` de SARIF que no es `pass` **no es necesariamente un hallazgo**. En el
escaneo de referencia hay 18 resultados: 17 `kind:"pass"` y **uno `kind:"open"`**, y ese
`open` no es una vulnerabilidad sino una fila de cobertura con
`coverage_outcome: "needs_follow_up"` —el motor diciendo que una superficie no pudo
verificarse. Tratarlo como hallazgo inventaría una vulnerabilidad que no existe.

El discriminador real, y el que el propio motor documenta en `strix/report/sarif.py`
—«Consumers that only want alerts filter on `kind == "fail"`»— combina tres señales:

1. `kind == "fail"`, que es además el valor **por defecto** de SARIF cuando `kind` no está.
2. El `ruleId` **no** empieza por `strix-coverage/`, que es el prefijo que el motor usa para
   las reglas de cobertura.
3. `properties.strix.coverage_outcome` **no** está, porque solo lo rellenan las filas de
   cobertura.

Un hallazgo real cumple las tres; una fila de cobertura incumple al menos una. Con esto, el
run de referencia da **0 hallazgos y 18 registros de cobertura**, que es lo que pasó.

## Por qué un escaneo sin hallazgos no es un fallo

Porque el motor lo dice con transparencia: `coverage.json` trae `findings_filed: 0` y
`outcomes: {no_issue_found: 9, ruled_out: 8, needs_follow_up: 1}`. Eso es un escaneo que
funcionó y no encontró nada explotable. Convertirlo en error sería mentir sobre el motor y
ensuciar la pregunta que el panel tiene que responder: «¿qué encontró?».

Lo que **sí** es un fallo es `run.json` con `status` distinto de `completed`, y eso lanza
`StrixRunIncompleteError`: el motor no terminó el trabajo y el artefacto no describe un
escaneo, describe un intento interrumpido.

## Las defensas de lectura no son un detalle de este módulo

`leer_texto_protegido` es la **misma** lectura que usa el modo contenedor para su salida:
`lstat` antes de abrir para rechazar un enlace simbólico, `S_ISREG` en los dos lados,
`O_NOFOLLOW` al abrir, comparación de `st_dev`/`st_ino` entre el `stat` y el `fstat` para
detectar una sustitución entre medias, techo de tamaño y decodificación estricta en UTF-8.

El ataque que previene es concreto: un motor comprometido —o cualquier cosa con escritura en
el workspace— sustituye el fichero entre el `stat` y el `open`, y lo que se lee no es lo que se
comprobó. Aquí no se relaja nada, y por eso la función es compartida en vez de estar reescrita.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from backend.apps.vulnerabilities.models import IssueStatusEnum, SeverityEnum, Vulnerability
from backend.core.config import settings
from backend.workers.parser.normalizer import normalize_severity
from backend.workers.parser.strix_parser import redactar_secretos_de_ejecucion
from backend.workers.runner.exceptions import (
    SandboxOutputError,
    StrixRunIncompleteError,
)
from backend.workers.runner.telemetry import TokenUsage

logger = logging.getLogger(__name__)

#: El motor guarda los artefactos aqui, relativo al directorio de trabajo desde el que se
#: lanza. Ver `strix/core/paths.py` (`RUNS_DIR_NAME`).
DIRECTORIO_DE_RUNS = "strix_runs"

FICHERO_RUN = "run.json"
FICHERO_SARIF = "findings.sarif"
FICHERO_COBERTURA = "coverage.json"
FICHERO_INFORME = "penetration_test_report.md"

#: Prefijo de las reglas de cobertura. Ver `strix/report/sarif.py` (`_COVERAGE_RULE_PREFIX`).
PREFIJO_REGLA_COBERTURA = "strix-coverage/"

#: Los `kind` con los que el motor representa cobertura y **no** un hallazgo. Los tres salen
#: de `_OUTCOME_TO_KIND`: `pass` para `no_issue_found` y `ruled_out`, `notApplicable` para
#: `not_applicable` y `open` para `needs_follow_up`.
TIPOS_DE_COBERTURA = frozenset({"pass", "notApplicable", "open"})

#: El `kind` de un resultado SARIF es opcional y **por defecto es `fail`**. Un escaneo con
#: hallazgos puede, por tanto, no escribir `kind` en absoluto, y tratar la ausencia como
#: cobertura perdería el hallazgo. Ver el §"kind" de SARIF 2.1.0.
KIND_POR_DEFECTO = "fail"

#: El mismo mapa de severidad a puntuación que usa el propio motor para
#: `properties["security-severity"]` (`strix/report/sarif.py`, `_SEVERITY_TO_SCORE`).
#:
#: Se reutiliza en vez de inventar uno porque `vulnerabilities.cvss_score` es `NOT NULL`: si
#: el motor no publica CVSS hay que escribir un número, y el número que el motor escribiría
#: para esa severidad es el único que no contradice al artefacto que el propio motor dejó.
PUNTUACION_POR_SEVERIDAD: dict[SeverityEnum, float] = {
    SeverityEnum.CRITICAL: 9.5,
    SeverityEnum.HIGH: 8.0,
    SeverityEnum.MEDIUM: 5.5,
    SeverityEnum.LOW: 3.0,
    SeverityEnum.INFO: 1.0,
}

#: Bandas de CVSS v3 para derivar una severidad cuando el motor no da la etiqueta.
#:
#: Son los rangos que publica FIRST para v3.1 y que ya usa el panel para los sliders, así que
#: un número traido del artefacto y uno bandado aquí caen en la misma casilla.
BANDAS_DE_CVSS: tuple[tuple[float, SeverityEnum], ...] = (
    (9.0, SeverityEnum.CRITICAL),
    (7.0, SeverityEnum.HIGH),
    (4.0, SeverityEnum.MEDIUM),
    (0.1, SeverityEnum.LOW),
    (0.0, SeverityEnum.INFO),
)


# --------------------------------------------------------------------------- #
# Lectura protegida
# --------------------------------------------------------------------------- #


def leer_texto_protegido(path: Path, etiqueta: str, *, max_bytes: int | None = None) -> str:
    """Lee un artefacto del workspace aplicando todas las defensas de lectura.

    Es la MISMA funcion que usa el modo contenedor para su salida, con el nombre del fichero
    como unico parametro que cambia. Que sea compartida y no una copia es lo que garantiza que
    el modo host no se quede sin proteccion el dia que se olvide: si mañana se endurece aqui,
    endurece en los dos sitios a la vez.

    La comparacion de `st_dev`/`st_ino` entre el `stat` previo y el `fstat` posterior es la que
    detecta la sustitucion del fichero **entre** el `stat` y el `open`. Sin ella, `O_NOFOLLOW`
    solo impediria un enlace simbolico colocado en el momento del `open`, no uno que alguien
    cambie mientras se lee.

    `max_bytes` existe para poder probar el techo sin destrabar `Settings`, que es **congelado**
    a proposito. El valor por defecto sale siempre de `strix_max_output_bytes`; ningún camino de
    producción pasa otro.
    """

    techo = settings.strix_max_output_bytes if max_bytes is None else max_bytes
    try:
        path_stat = path.lstat()
        if not stat.S_ISREG(path_stat.st_mode):
            raise SandboxOutputError(f"{etiqueta} no es un archivo regular")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as artefacto:
            abierto_stat = os.fstat(artefacto.fileno())
            if not stat.S_ISREG(abierto_stat.st_mode):
                raise SandboxOutputError(f"{etiqueta} cambió a un tipo no regular")
            if (path_stat.st_dev, path_stat.st_ino) != (abierto_stat.st_dev, abierto_stat.st_ino):
                raise SandboxOutputError(f"{etiqueta} cambió durante la lectura")
            datos = artefacto.read(techo + 1)
    except SandboxOutputError:
        raise
    except OSError as error:
        raise SandboxOutputError(f"No se pudo leer {etiqueta}") from error
    if len(datos) > techo:
        raise SandboxOutputError(f"{etiqueta} supera el tamaño máximo permitido")
    try:
        return datos.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SandboxOutputError(f"{etiqueta} no está codificado en UTF-8") from error


def _leer_json(path: Path, etiqueta: str) -> dict[str, Any]:
    """Lee un artefacto JSON y exige que sea un objeto.

    Un artefacto que no es un objeto no es un artefacto: es un fichero de otra cosa, y tratarlo
    como un run sin estado seria inventar un escaneo que no ocurrio.
    """

    crudo = leer_texto_protegido(path, etiqueta)
    try:
        decodificado: object = json.loads(crudo)
    except json.JSONDecodeError as error:
        raise SandboxOutputError(f"{etiqueta} no es JSON válido") from error
    if not isinstance(decodificado, dict):
        raise SandboxOutputError(f"{etiqueta} no es un objeto JSON")
    return cast(dict[str, Any], decodificado)


# --------------------------------------------------------------------------- #
# Utilidades de forma
# --------------------------------------------------------------------------- #


def _texto(valor: object) -> str | None:
    """Un valor de texto no vacio, o `None`."""

    if isinstance(valor, str):
        limpio = valor.strip()
        return limpio or None
    return None


def _entero(valor: object) -> int | None:
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    if valor < 0:
        return None
    return int(valor)


def _numero(valor: object) -> float | None:
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    return float(valor)


def _instante(valor: object) -> datetime | None:
    """Un ISO-8601 con zona, en UTC.

    `run.json` escribe `2026-10-07T08:45:05.801086+00:00`. Sin zona, comparar dos runs por su
    `start_time` compararia instants que Python no sabe ordenar igual: dos zonas distintas son
    dos calendarios, y el mas reciente puede salir el otro.
    """

    texto = _texto(valor)
    if texto is None:
        return None
    try:
        instante = datetime.fromisoformat(texto)
    except ValueError:
        return None
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=UTC)
    return instante.astimezone(UTC)


def _bloque(valor: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], valor) if isinstance(valor, Mapping) else {}


def _recortar(texto: str | None, tope: int) -> str | None:
    if texto is None:
        return None
    if len(texto) <= tope:
        return redactar_secretos_de_ejecucion(texto)
    return redactar_secretos_de_ejecucion(texto[:tope])


def _primero(mapping: Mapping[str, Any], claves: tuple[str, ...]) -> object:
    for clave in claves:
        if clave in mapping:
            return mapping[clave]
    return None


# --------------------------------------------------------------------------- #
# Modelos de salida
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class HuecoCobertura:
    """Una superficie que el motor **no** pudo verificar, y por qué.

    No es basura ni un error: es la razón por la que un escaneo con cero hallazgos no significa
    automaticamente "no hay nada". Por eso viaja hasta la interfaz.
    """

    kind: str
    surface: str
    risk_area: str
    detail: str

    def a_json(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "surface": self.surface,
            "risk_area": self.risk_area,
            "detail": self.detail,
        }

    @classmethod
    def desde(cls, bruto: object) -> HuecoCobertura | None:
        bloque = _bloque(bruto)
        detail = _texto(bloque.get("detail"))
        if detail is None:
            return None
        return cls(
            kind=_texto(bloque.get("kind")) or "needs_follow_up",
            surface=_texto(bloque.get("surface")) or "unspecified surface",
            risk_area=_texto(bloque.get("risk_area")) or "unspecified risk",
            detail=detail,
        )


@dataclass(frozen=True, slots=True)
class CoberturaStrix:
    """Lo que el motor dice que reviso, y lo que no pudo revisar."""

    findings_filed: int
    surfaces_reviewed: int
    gaps: tuple[HuecoCobertura, ...]
    complete: bool
    scan_status: str
    exit_reason: str | None
    caveats: tuple[str, ...]
    agentes: tuple[tuple[str, str], ...]

    @property
    def advertencias(self) -> tuple[str, ...]:
        """Caveats y huecos, en texto, para el registro y para la interfaz."""

        return (*self.caveats, *(hueco.detail for hueco in self.gaps))

    def a_json(self) -> dict[str, object]:
        """La forma que se persiste en `pentest_runs.coverage`.

        Sin tildes en las claves porque son claves, y el valor es lo que se traduce en el
        panel. Los valores JSON van sin tildes; los comentarios y los textos, con ellas.
        """

        return {
            "findings_filed": self.findings_filed,
            "surfaces_reviewed": self.surfaces_reviewed,
            "gaps": [hueco.a_json() for hueco in self.gaps],
            "complete": self.complete,
            "scan_status": self.scan_status,
            "exit_reason": self.exit_reason,
            "caveats": list(self.caveats),
            "agents": [
                {"agent_name": nombre, "status": estado} for nombre, estado in self.agentes
            ],
        }


@dataclass(frozen=True, slots=True)
class HallazgoStrix:
    """Un hallazgo real del motor, en los campos de `Vulnerability` y no en otros.

    Se mapea al modelo que ya existe en vez de inventar un esquema: `severity` es
    `SeverityEnum`, el CVSS es un float de 0 a 10, y el resto son los mismos campos que ya
    llenaba el parser antiguo. Un esquema nuevo habria obligado a duplicar el vocabulario de
    severidades, el conteo del panel y las reglas de R4.
    """

    source_finding_id: str
    title: str
    description: str
    severity: SeverityEnum
    cvss_score: float
    cve_id: str | None
    affected_target: str
    affected_line: str | None
    poc_reproduction_raw: str
    autofix_patch_diff: str | None

    def a_vulnerabilidad(
        self,
        *,
        organization_id: Any,
        run_id: Any,
    ) -> Vulnerability:
        """Construye la entidad sin tocar la sesion.

        R3: `organization_id` es un parametro obligatorio y va en la propia fila. Nunca se
        deduce del run ni se deja en `None`, porque un hallazgo sin tenant es un hallazgo que
        puede leer cualquiera que tenga una sesion.
        """

        return Vulnerability(
            organization_id=organization_id,
            run_id=run_id,
            source_finding_id=self.source_finding_id,
            title=self.title,
            description=self.description,
            severity=self.severity,
            cvss_score=self.cvss_score,
            cve_id=self.cve_id,
            affected_target=self.affected_target,
            affected_line=self.affected_line,
            poc_reproduction_raw=self.poc_reproduction_raw,
            autofix_patch_diff=self.autofix_patch_diff,
            status=IssueStatusEnum.OPEN,
        )


@dataclass(frozen=True, slots=True)
class EjecucionStrix:
    """Todo lo que se lee de un run del motor, ya reducido a datos."""

    directorio: Path
    run_id: str
    run_name: str
    status: str
    start_time: datetime | None
    end_time: datetime | None
    scan_mode: str | None
    objetivo: str | None
    total_tokens: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    coste: Decimal | None
    hallazgos: tuple[HallazgoStrix, ...]
    registros_cobertura: int
    cobertura: CoberturaStrix | None
    informe_markdown: str | None
    cached_tokens: int | None = None

    @property
    def duration_seconds(self) -> Decimal | None:
        if self.start_time is None or self.end_time is None:
            return None
        segundos = Decimal(str((self.end_time - self.start_time).total_seconds()))
        return segundos if segundos >= 0 else None

    @property
    def advertencias(self) -> tuple[str, ...]:
        """Lo que el motor dice y no es un hallazgo: caveats y huecos de cobertura."""

        if self.cobertura is None:
            return ()
        return self.cobertura.advertencias

    @property
    def consumo_desglosado(self) -> TokenUsage | None:
        """Entrada y salida **por separado**, o `None` si el motor no las publica.

        ## Por qué esto no se deduce del total

        Porque la tarificación cobra la entrada y la salida a precios distintos, y repartir un
        total en dos mitades es inventar el desglose: el cargo saldría distinto al que corresponde
        y sería un cargo equivocado en la cartera de un tenant. El desglose existe de verdad en
        `run.json.llm_usage.providers`, donde cada proveedor trae sus `input_tokens` y sus
        `output_tokens`, y se suman.

        Y si el motor publica el total pero no el desglose, esto devuelve `None` y el run se queda
        con la reserva: cobrar a ciegas es peor que no cobrar el ajuste.
        """

        if self.prompt_tokens is None and self.completion_tokens is None:
            return None
        return TokenUsage(
            prompt_tokens=self.prompt_tokens or 0,
            completion_tokens=self.completion_tokens or 0,
        )

    @property
    def escaneo_limpio(self) -> bool:
        """Escaneó entero y no dejó hallazgos.

        No es lo mismo que `not self.hallazgos`: un run que terminó a medias con cero
        hallazgos tiene la lista vacía y no es un escaneo limpio.
        """

        return (
            self.status == "completed"
            and not self.hallazgos
            and (self.cobertura is None or self.cobertura.complete)
        )


# --------------------------------------------------------------------------- #
# Severidad y CVSS
# --------------------------------------------------------------------------- #


def severidad_desde_cvss(puntuacion: float) -> SeverityEnum:
    for minimo, severidad in BANDAS_DE_CVSS:
        if puntuacion >= minimo:
            return severidad
    return SeverityEnum.INFO


def _cvss_de_sarif(valor: object) -> float | None:
    """El CVSS del motor puede venir como numero o como objeto con `score`.

    `strix/report/sarif.py` copia el valor tal cual del reporte del agente, y un agente lo
    escribe de las dos formas. Aceptar solo una seria perder la mitad de los hallazgos con
    CVSS, que es justo la mitad que mas importa.
    """

    numero = _numero(valor)
    if numero is not None:
        return numero
    return _numero(_bloque(valor).get("score"))


def _severidad_de_resultado(resultado: Mapping[str, Any]) -> tuple[SeverityEnum, float, bool]:
    """Severidad y CVSS de un hallazgo, con el criterio de degradación documentado.

    El orden es: etiqueta del motor, luego CVSS, luego el `security-severity` de SARIF, y en
    ultimo termino `MEDIUM`. Ese ultimo valor **no** es arbitrario:

    - `INFO` escondería un hallazgo que el motor decidió registrar, y en un panel cuyo
      recuento es riesgo vivo, un hallazgo marcado como informativo desaparece de la postura.
    - `HIGH` o `CRITICAL` inventarían una gravedad que nadie ha medido.

    `MEDIUM` es el punto en el que un hallazgo sin severidad sigue siendo visible y no afirma
    nada que el motor no afirmara. El `bool` de salida dice si el motor dio la severidad, para
    que quien lo consume pueda avisar en vez de dejar que un dato inventado pase por medido.
    """

    strix = _bloque(resultado.get("properties")).get("strix")
    strix = _bloque(strix)

    etiqueta = _texto(strix.get("severity")) or _texto(resultado.get("level"))
    if etiqueta is not None:
        try:
            severidad = normalize_severity(etiqueta)
        except ValueError:
            severidad = None
        if severidad is not None:
            cvss = _cvss_de_sarif(strix.get("cvss"))
            if cvss is None:
                cvss = _numero(_bloque(resultado.get("properties")).get("security-severity"))
            puntuacion = cvss if cvss is not None else PUNTUACION_POR_SEVERIDAD[severidad]
            return severidad, min(max(puntuacion, 0.0), 10.0), True

    cvss = _cvss_de_sarif(strix.get("cvss"))
    if cvss is None:
        cvss = _numero(_bloque(resultado.get("properties")).get("security-severity"))
    if cvss is not None:
        acotada = min(max(cvss, 0.0), 10.0)
        return severidad_desde_cvss(acotada), acotada, True

    return SeverityEnum.MEDIUM, PUNTUACION_POR_SEVERIDAD[SeverityEnum.MEDIUM], False


# --------------------------------------------------------------------------- #
# Lectura de los artefactos
# --------------------------------------------------------------------------- #


def _es_cobertura(resultado: Mapping[str, Any]) -> bool:
    """¿Este `result` de SARIF es una fila de cobertura y no un hallazgo?

    Las tres señales, y por qué hacen falta las tres:

    1. `kind` en `TIPOS_DE_COBERTURA` cubre `pass`, `notApplicable` y `open`.
    2. El prefijo `strix-coverage/` del `ruleId` cubre una fila de cobertura que por lo que sea
       saliera sin `kind`.
    3. `coverage_outcome` cubre el caso simetrico: algo con `kind` de hallazgo que en realidad
       es una fila de cobertura.

    Con `kind` solo, el escaneo de referencia devolveria **un hallazgo falso**: su fila
    `needs_follow_up`, que es informacion de cobertura, llega como `kind:"open"`.
    """

    kind = _texto(resultado.get("kind")) or KIND_POR_DEFECTO
    if kind in TIPOS_DE_COBERTURA:
        return True
    rule_id = _texto(resultado.get("ruleId")) or ""
    if rule_id.startswith(PREFIJO_REGLA_COBERTURA):
        return True
    strix = _bloque(_bloque(resultado.get("properties")).get("strix"))
    return "coverage_outcome" in strix


def _reglas_por_id(ejecucion_sarif: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Las reglas del driver, indexadas por identificador.

    El `title` del hallazgo vive en la regla (`shortDescription.text`), no en el resultado:
    el resultado solo lleva `message.text`, que es `"titulo\\n\\ndescripcion"` montado por el
    motor. Sin mirar la regla, el titulo habria que adivinarlo partiendo un texto.
    """

    runs = ejecucion_sarif.get("runs")
    if not isinstance(runs, list) or not runs:
        return {}
    primer_run = _bloque(runs[0]) if isinstance(runs[0], Mapping) else {}
    reglas = _bloque(_bloque(primer_run.get("tool")).get("driver")).get("rules")
    indexadas: dict[str, Mapping[str, Any]] = {}
    if not isinstance(reglas, list):
        return indexadas
    for regla in reglas:
        bloque = _bloque(regla)
        identificador = _texto(bloque.get("id"))
        if identificador is not None:
            indexadas.setdefault(identificador, bloque)
    return indexadas


def _resultados_sarif(
    documento: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    runs = documento.get("runs")
    if not isinstance(runs, list) or not runs:
        raise SandboxOutputError("El SARIF no contiene ningún run")
    primer_run = _bloque(runs[0]) if isinstance(runs[0], Mapping) else {}
    resultados = primer_run.get("results")
    if not isinstance(resultados, list):
        raise SandboxOutputError("El SARIF no contiene una lista de resultados")
    return (
        [cast(Mapping[str, Any], item) for item in resultados if isinstance(item, Mapping)],
        _reglas_por_id(documento),
    )


def _identificador_de_hallazgo(
    resultado: Mapping[str, Any],
    reglas: Mapping[str, Mapping[str, Any]],
    indice: int,
) -> str:
    """Un identificador estable y único dentro del run.

    `vulnerabilities` tiene `UNIQUE (run_id, source_finding_id)`, así que el identificador tiene
    que ser único **y** tiene que ser el mismo en dos escaneos del mismo hallazgo, o el
    histórico se parte. Por orden:

    1. `properties.strix.id`, que es el identificador que el propio motor asigna al reporte.
    2. `partialFingerprints.primaryLocationLineHash`, que el motor calcula **a proposito** sin
       el texto del LLM para sobrevivir a reformulaciones del título.
    3. `strix_vuln_class_hash`, que sobrevive incluso a un renombrado de fichero.
    4. `ruleId` más el índice, que es lo último pero nunca falla: el SARIF **siempre** tiene
       resultados, incluso con cero hallazgos (`test_write_sarif_always_emits_for_zero_findings`).
    """

    strix = _bloque(_bloque(resultado.get("properties")).get("strix"))
    candidato = _texto(strix.get("id"))
    if candidato:
        return candidato[:128]
    huellas = _bloque(resultado.get("partialFingerprints"))
    candidato = _texto(huellas.get("primaryLocationLineHash"))
    if candidato:
        return candidato[:128]
    candidato = _texto(_bloque(resultado.get("properties")).get("strix_vuln_class_hash"))
    if candidato:
        return candidato[:128]
    regla = _texto(resultado.get("ruleId")) or "hallazgo"
    regla_corta = reglas.get(regla, {})
    nombre = _texto(_bloque(regla_corta.get("shortDescription")).get("text")) or ""
    return f"{regla}#{indice}:{nombre[:40]}".replace(" ", "-")[:128]


def _poc_de_resultado(resultado: Mapping[str, Any], respaldo: str) -> str:
    """La evidencia de reproduccion, que en `vulnerabilities` es `NOT NULL`.

    El SARIF **no** lleva el payload: el motor lo saca a proposito (`strix/report/sarif.py`,
    `test_write_sarif_never_embeds_poc_script`), asi que lo unico que hay es la descripcion del
    PoC, el impacto o el analisis tecnico. Si no hay ninguno de los tres se persiste el mensaje
    del resultado, que es la evidencia que el motor si escribio, y no una cadena vacia: una
    columna `NOT NULL` rellenada con `""` seria un hallazgo sin evidencia, que es peor que uno
    con evidencia poor.
    """

    strix = _bloque(_bloque(resultado.get("properties")).get("strix"))
    partes = [
        _texto(_bloque(strix.get("poc")).get("description")),
        _texto(strix.get("impact")),
        _texto(strix.get("technical_analysis")),
    ]
    evidencia = "\n\n".join(parte for parte in partes if parte)
    return evidencia or respaldo


def _autofix_de_resultado(resultado: Mapping[str, Any]) -> str | None:
    """El parche sugerido por el motor, si el SARIF lo trae.

    El motor lo emite como `fixes[].artifactChanges[].replacements[].insertedContent.text`
    (`_build_fixes`). Un hallazgo sin parche es `None`, no cadena vacia: la columna distingue
    "no proposed" de "propuesto y vacio", que es lo que hace el parche inmutable de R4.
    """

    fixes = resultado.get("fixes")
    if not isinstance(fixes, list):
        return None
    insertados: list[str] = []
    for fix in fixes:
        cambios = _bloque(fix).get("artifactChanges")
        if not isinstance(cambios, list):
            continue
        for cambio in cambios:
            reemplazos = _bloque(cambio).get("replacements")
            if not isinstance(reemplazos, list):
                continue
            for reemplazo in reemplazos:
                texto = _texto(_bloque(reemplazo).get("insertedContent"))
                if texto is None:
                    texto = _texto(_bloque(_bloque(cambio).get("deletedRegion")).get("text"))
                if texto:
                    insertados.append(texto)
    if not insertados:
        return None
    return redactar_secretos_de_ejecucion("\n".join(insertados))


def _linea_de_resultado(resultado: Mapping[str, Any]) -> str | None:
    """La primera linea de codigo con ubicacion fisica, o `None`.

    Solo las fisicas llevan linea. Un hallazgo de DAST se ancla en `logicalLocations` con el
    endpoint, y anclarlo a un numero de linea inventado seria escribir algo falso en una columna
    que el panel muestra como evidencia.
    """

    for ubicacion in _iter(resultado.get("locations")):
        fisica = _bloque(ubicacion).get("physicalLocation")
        if not isinstance(fisica, Mapping):
            continue
        inicio = _entero(_bloque(_bloque(fisica).get("region")).get("startLine"))
        if inicio is not None:
            return str(inicio)
    return None


def _anclajes_de_resultado(resultado: Mapping[str, Any]) -> tuple[str, ...]:
    """Los nombres logicos del resultado: el endpoint, el recurso o la superficie."""

    anclajes: list[str] = []
    for ubicacion in _iter(resultado.get("locations")):
        for logica in _iter(_bloque(ubicacion).get("logicalLocations")):
            nombre = _texto(_bloque(logica).get("fullyQualifiedName"))
            if nombre and nombre not in anclajes:
                anclajes.append(nombre)
    return tuple(anclajes)


def _iter(valor: object) -> Iterator[object]:
    if isinstance(valor, list):
        yield from valor


def leer_hallazgos(
    documento: Mapping[str, Any],
    *,
    objetivo_por_defecto: str | None,
) -> tuple[tuple[HallazgoStrix, ...], int]:
    """Separa hallazgos de cobertura y devuelve los primeros.

    El segundo valor es **cuántos registros de cobertura** había, y se devuelve aunque no haya
    hallazgos: es el dato que distingue «se escaneó y no había nada» de «no se pudo escanear».
    """

    resultados, reglas = _resultados_sarif(documento)
    if len(resultados) > settings.strix_max_findings:
        raise SandboxOutputError("El SARIF supera el número máximo de resultados")

    hallazgos: list[HallazgoStrix] = []
    registros_cobertura = 0
    vistos: set[str] = set()

    for indice, resultado in enumerate(resultados):
        if _es_cobertura(resultado):
            registros_cobertura += 1
            continue
        severidad, puntuacion, _del_motor = _severidad_de_resultado(resultado)
        strix = _bloque(_bloque(resultado.get("properties")).get("strix"))
        identificador = _identificador_de_hallazgo(resultado, reglas, indice)
        if identificador in vistos:
            raise SandboxOutputError(
                f"El SARIF repite el identificador de hallazgo {identificador}"
            )
        vistos.add(identificador)

        regla = reglas.get(_texto(resultado.get("ruleId")) or "", {})
        titulo = _texto(_bloque(regla.get("shortDescription")).get("text"))
        mensaje = _texto(_bloque(resultado.get("message")).get("text")) or ""
        if titulo is None:
            titulo = mensaje.split("\n\n", maxsplit=1)[0].strip() or "Hallazgo sin titulo"
        descripcion = mensaje
        if descripcion.startswith(titulo):
            descripcion = descripcion[len(titulo) :].strip() or titulo

        objetivo = _texto(strix.get("target")) or _texto(strix.get("endpoint"))
        if objetivo is None:
            anclajes = _anclajes_de_resultado(resultado)
            objetivo = anclajes[0] if anclajes else objetivo_por_defecto
        if objetivo is None:
            raise SandboxOutputError(
                "El SARIF no dice contra qué target es el hallazgo y el run tampoco"
            )

        hallazgos.append(
            HallazgoStrix(
                source_finding_id=identificador,
                title=titulo[:255],
                description=_recortar(
                    descripcion, settings.strix_max_description_chars
                )
                or titulo[:255],
                severity=severidad,
                cvss_score=puntuacion,
                cve_id=_texto(strix.get("cve")),
                affected_target=objetivo[:512],
                affected_line=_linea_de_resultado(resultado),
                poc_reproduction_raw=_recortar(
                    _poc_de_resultado(resultado, descripcion or titulo),
                    settings.strix_max_poc_chars,
                )
                or (descripcion or titulo),
                autofix_patch_diff=_recortar(
                    _autofix_de_resultado(resultado), settings.strix_max_autofix_chars
                ),
            )
        )
    return tuple(hallazgos), registros_cobertura


def leer_cobertura(documento: Mapping[str, Any]) -> CoberturaStrix | None:
    """Lee `coverage.json`. Devuelve `None` solo si el documento no trae nada legible.

    `None` no es «escaneado sin cobertura»: es «no hay dato». La interfaz los tiene que separar,
    y por eso es un valor y no un `CoberturaStrix` vacio con ceros, que se leería como un run
    que no revisó nada.
    """

    resumen = _bloque(documento.get("summary"))
    completitud = _bloque(documento.get("completeness"))
    huecos: list[HuecoCobertura] = []
    for bruto in _iter(documento.get("gaps")):
        hueco = HuecoCobertura.desde(bruto)
        if hueco is not None:
            huecos.append(hueco)

    agentes: list[tuple[str, str]] = []
    for bruto in _iter(_bloque(documento.get("machine_observed")).get("agents")):
        bloque = _bloque(bruto)
        nombre = _texto(bloque.get("agent_name"))
        if nombre is not None:
            agentes.append((nombre[:200], (_texto(bloque.get("status")) or "unknown")[:40]))

    caveats = tuple(
        aviso
        for aviso in (
            _texto(item) for item in _iter(completitud.get("caveats"))
        )
        if aviso
    )
    surfaces = _entero(resumen.get("surfaces_reviewed"))
    filed = _entero(resumen.get("findings_filed"))
    complete = completitud.get("complete")
    return CoberturaStrix(
        findings_filed=filed if filed is not None else 0,
        surfaces_reviewed=surfaces if surfaces is not None else 0,
        gaps=tuple(huecos),
        complete=bool(complete) if isinstance(complete, bool) else False,
        scan_status=_texto(completitud.get("scan_status")) or "unknown",
        exit_reason=_texto(completitud.get("exit_reason")),
        caveats=caveats,
        agentes=tuple(agentes),
    )


def _iterar_directorios_de_run(workspace: Path) -> Iterator[Path]:
    """Los directorios de run del workspace, por patron y no por nombre.

    El motor genera el nombre solo (`mindguard-site_23ee`, `mindguard-site_8023`) y **no**
    acepta `--run-name`: `-r` es `--resume` y combinarla con `--target` es error. Por eso el
    directorio se busca por forma y no se puede adivinar por adelantado.
    """

    raiz = workspace / DIRECTORIO_DE_RUNS
    if not raiz.is_dir():
        return
    for entrada in sorted(raiz.iterdir()):
        if entrada.is_dir() and (entrada / FICHERO_RUN).is_file():
            yield entrada


def localizar_directorio_de_run(workspace: Path) -> Path:
    """El directorio del run mas reciente, y por que se ha elegido ese.

    Cuando hay mas de uno gana el `run.json.start_time` mayor, y no el `mtime` del directorio:
    `run.json` se reescribe al cambiar de estado, asi que su contenido es la hora real del motor
    y el `mtime` de un directorio depende de cuando se creo el fichero. Con un unico directorio
    —el caso normal, porque el workspace es de un solo run— la eleccion es trivial.
    """

    candidatos = list(_iterar_directorios_de_run(workspace))
    if not candidatos:
        raise SandboxOutputError(
            f"El motor no dejó ningún directorio de run bajo {workspace / DIRECTORIO_DE_RUNS}"
        )
    if len(candidatos) == 1:
        return candidatos[0]

    con_fecha: list[tuple[datetime, Path]] = []
    sin_fecha: list[Path] = []
    for directorio in candidatos:
        registro = _json_de_run(directorio)
        instante = _instante(registro.get("start_time")) if registro else None
        if instante is None:
            sin_fecha.append(directorio)
        else:
            con_fecha.append((instante, directorio))

    if not con_fecha:
        elegido = max(
            candidatos,
            key=lambda directorio: (directorio / FICHERO_RUN).stat().st_mtime,
        )
    else:
        elegido = max(con_fecha, key=lambda par: par[0])[1]
    logger.info(
        "El workspace %s tiene %d runs del motor; se toma '%s', el más reciente por "
        "run.json.start_time",
        workspace,
        len(candidatos),
        elegido.name,
    )
    if sin_fecha:
        logger.warning(
            "Hay %d directorio(s) de run sin start_time legible en %s y se han descartado: %s",
            len(sin_fecha),
            workspace,
            ", ".join(directorio.name for directorio in sin_fecha),
        )
    return elegido


def _json_de_run(directorio: Path) -> dict[str, Any] | None:
    try:
        return _leer_json(directorio / FICHERO_RUN, FICHERO_RUN)
    except SandboxOutputError:
        return None


def leer_ejecucion(workspace: Path) -> EjecucionStrix:
    """Lee los cuatro artefactos del run mas reciente del workspace.

    Lanza `StrixRunIncompleteError` si `run.json` no dice `completed`, porque un artefacto de
    un run interrumpido no describe un escaneo: describe el intento. Y lanza
    `SandboxOutputError` si falta `run.json` o `findings.sarif`, que son los dos que no
    pueden faltar en un run que llego a escribir.
    """

    directorio = localizar_directorio_de_run(workspace)
    registro = _leer_json(directorio / FICHERO_RUN, FICHERO_RUN)
    status = _texto(registro.get("status")) or "unknown"
    if status != "completed":
        raise StrixRunIncompleteError(
            f"El motor terminó con status '{status}' y su artefacto no describe un escaneo "
            f"completo (run {registro.get('run_name', directorio.name)})"
        )

    objetivo = _objetivo_del_run(registro)
    uso = _bloque(registro.get("llm_usage"))
    coste = _numero(uso.get("cost"))
    entrada, salida = _tokens_de_raiz(uso)

    hallazgos, registros_cobertura = leer_hallazgos(
        _leer_json(directorio / FICHERO_SARIF, FICHERO_SARIF),
        objetivo_por_defecto=objetivo,
    )

    cobertura: CoberturaStrix | None = None
    ruta_cobertura = directorio / FICHERO_COBERTURA
    if ruta_cobertura.is_file():
        cobertura = leer_cobertura(_leer_json(ruta_cobertura, FICHERO_COBERTURA))
    else:
        logger.warning(
            "El run %s no dejó %s; no hay cobertura que mostrar",
            directorio.name,
            FICHERO_COBERTURA,
        )

    informe: str | None = None
    ruta_informe = directorio / FICHERO_INFORME
    if ruta_informe.is_file():
        informe = leer_texto_protegido(ruta_informe, FICHERO_INFORME)

    ejecucion = EjecucionStrix(
        directorio=directorio,
        run_id=_texto(registro.get("run_id")) or directorio.name,
        run_name=_texto(registro.get("run_name")) or directorio.name,
        status=status,
        start_time=_instante(registro.get("start_time")),
        end_time=_instante(registro.get("end_time")),
        scan_mode=_texto(registro.get("scan_mode")),
        objetivo=objetivo,
        total_tokens=_entero(uso.get("total_tokens")),
        prompt_tokens=entrada,
        completion_tokens=salida,
        coste=Decimal(str(coste)) if coste is not None else None,
        hallazgos=hallazgos,
        registros_cobertura=registros_cobertura,
        cobertura=cobertura,
        informe_markdown=informe,
        cached_tokens=_tokens_cacheados(uso),
    )
    if ejecucion.advertencias:
        logger.info(
            "El run %s terminó con %d aviso(s) del motor: %s",
            ejecucion.run_name,
            len(ejecucion.advertencias),
            " | ".join(ejecucion.advertencias),
        )
    return ejecucion


def _tokens_cacheados(uso: Mapping[str, Any]) -> int | None:
    detalles = uso.get("input_tokens_details")
    if not isinstance(detalles, list):
        return None
    valores = [_entero(_bloque(detalle).get("cached_tokens")) for detalle in detalles]
    if not valores or any(valor is None or valor < 0 for valor in valores):
        return None
    return sum(valor for valor in valores if valor is not None)


def _tokens_de_raiz(uso: Mapping[str, Any]) -> tuple[int | None, int | None]:
    """`input_tokens` y `output_tokens` de la raiz de `llm_usage`, o `(None, None)`.

    ## Por qué la raiz y **no** la suma de `providers`

    Porque `providers` **no** publica `output_tokens`. En el run de referencia trae
    `InferenceNet`, `Mistral` y `Wafer`, cada uno con `requests`, `input_tokens`, `cached_tokens`,
    `cost`, `cache_misses` y `missed_tokens` — y ninguno con `output_tokens`. Sumarlos daría una
    salida de cero, es decir, tarificar como si el modelo no hubiera generado **nada**, que es una
    infratarificación real en la cartera de un tenant.

    La raiz sí trae los dos: `input_tokens: 16819076` y `output_tokens: 46417`.

    ## Lo que aquí **no** se hace, y por qué

    No se reparte `total_tokens` en dos mitades cuando falta uno de los dos. La tarificación cobra
    la entrada y la salida a precios distintos, así que un reparto es un cargo inventado: mejor
    devolver `None` y que el run se quede con la reserva, que es la vía conservadora.

    ## Lo que tampoco se descuenta, aunque se podría

    Que `input_tokens` incluye los tokens que el proveedor sirvió de su caché
    (`input_tokens_details.cached_tokens`: 16.443.264 de 16.819.076). Restarlos daría el coste real
    del proveedor, pero un token cacheado tiene **otro** precio y ese precio no está en
    `LLMModelConfig`: hay un único `base_cost_input_m`. Inventar aquí un factor de caché sería un
    número sin fuente. Se reporta lo que el motor resume, que es lo único que la plataforma puede
    tarificar con el catálogo que tiene.
    """

    entrada = _entero(uso.get("input_tokens"))
    salida = _entero(uso.get("output_tokens"))
    if entrada is None and salida is None:
        return None, None
    return entrada, salida


def _objetivo_del_run(registro: Mapping[str, Any]) -> str | None:
    """El target tal como lo escribio el motor: `targets_info[].original`.

    Se usa como respaldo del `affected_target` de un hallazgo, porque un SARIF con localizacion
    sintetica y sin `strix.target` no dice contra que se escaneo, y un hallazgo sin target es un
    hallazgo que no se puede volver a preguntar.
    """

    objetivos = registro.get("targets_info")
    if not isinstance(objetivos, list):
        return None
    for bruto in objetivos:
        original = _texto(_bloque(bruto).get("original"))
        if original:
            return original[:512]
    return None


__all__ = (
    "BANDAS_DE_CVSS",
    "DIRECTORIO_DE_RUNS",
    "FICHERO_COBERTURA",
    "FICHERO_INFORME",
    "FICHERO_RUN",
    "FICHERO_SARIF",
    "PUNTUACION_POR_SEVERIDAD",
    "CoberturaStrix",
    "EjecucionStrix",
    "HallazgoStrix",
    "HuecoCobertura",
    "leer_cobertura",
    "leer_ejecucion",
    "leer_hallazgos",
    "leer_texto_protegido",
    "localizar_directorio_de_run",
    "severidad_desde_cvss",
)
