"""Precio en créditos de las operaciones facturables.

Vive aquí y no en el router de pentests porque el worker de Strix también lo
necesita para ajustar la reserva, y el router importa al worker: dejarlo en el
router cerraría un ciclo de importación.
"""

from __future__ import annotations

from decimal import Decimal

from backend.apps.pentests.models import ScanModeEnum
from backend.core.config import settings

_CREDIT_QUANTUM = Decimal("0.0001")


def scan_credit_cost(scan_mode: ScanModeEnum) -> Decimal:
    """Coste en créditos de un escaneo según su modo.

    R1: el precio vive en configuración, no en el código. Un escaneo `QUICK` se
    cobra proporcionalmente porque consume una fracción de los tokens de uno
    `DEEP`, y la proporción también es configurable.
    """

    base = Decimal(settings.scan_credit_cost)
    if scan_mode == ScanModeEnum.QUICK:
        return (base * Decimal(settings.quick_scan_credit_multiplier)).quantize(
            _CREDIT_QUANTUM
        )
    return base.quantize(_CREDIT_QUANTUM)
