"""Cálculo puro del Security Score de postura a partir de hallazgos abiertos."""

from __future__ import annotations

import math
from collections.abc import Mapping

_SEVERITY_PENALTY_WEIGHTS: Mapping[str, int] = {
    "CRITICAL": 20,
    "HIGH": 8,
    "MEDIUM": 4,
    "LOW": 1,
    "INFO": 0,
}
_PENALTY_SCALE = 4
_MAX_SCORE = 100


def compute_security_score(open_findings_by_severity: Mapping[str, int]) -> int:
    """Convierte el conteo de hallazgos abiertos en una salud de 0 a 100.

    La fórmula es monótona y auditable: cada severidad aporta un peso proporcional
    (CRITICAL 5, HIGH 2, MEDIUM 1, LOW 0.25, INFO 0) y el resultado es
    ``100 - media_ponderada`` redondeada hacia arriba y acotada a 0. Un tenant sin
    hallazgos abiertos arranca en 100 y 20 hallazgos críticos lo llevan a 0.
    """

    total_penalty = 0
    for severity, count in open_findings_by_severity.items():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("Los conteos de severidad deben ser enteros no negativos")
        total_penalty += _SEVERITY_PENALTY_WEIGHTS.get(severity, 0) * count
    penalty = math.ceil(total_penalty / _PENALTY_SCALE)
    return max(0, _MAX_SCORE - penalty)
