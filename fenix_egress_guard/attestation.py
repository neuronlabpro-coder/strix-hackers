"""Verificación compartida de la atestación efímera del guard del host."""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path

MAX_ATTEST_AGE_SECONDS = 5


class AttestationError(ValueError):
    """La atestación no autoriza operaciones Docker."""


def verify(
    path: Path,
    *,
    network: str,
    subnet: str,
    policy_hash: str,
    boot_id: str | None = None,
    now: float | None = None,
) -> None:
    if not network or not subnet or not re.fullmatch(r"[0-9a-f]{64}", policy_hash):
        raise AttestationError("Política de egress esperada ausente o inválida")
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
            raise AttestationError("Atestación ausente o inválida")
        status = json.loads(path.read_text(encoding="utf-8"))
        current_boot = (
            boot_id
            if boot_id is not None
            else Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        )
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise AttestationError("Atestación ilegible") from error
    if not isinstance(status, dict) or any(
        (
            status.get("status") != "PASS",
            status.get("network_name") != network,
            status.get("network_subnet") != subnet,
            status.get("boot_id") != current_boot,
            status.get("policy_hash") != policy_hash,
        )
    ):
        raise AttestationError("Atestación no coincide")
    checked = status.get("checked_at")
    expires = status.get("expires_at")
    moment = time.time() if now is None else now
    if (
        type(checked) not in (int, float)
        or type(expires) not in (int, float)
        or not math.isfinite(checked)
        or not math.isfinite(expires)
        or checked > moment
        or expires <= moment
        or expires <= checked
        or expires - checked > MAX_ATTEST_AGE_SECONDS
    ):
        raise AttestationError("Atestación caducada o temporalmente inválida")
