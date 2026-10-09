"""El worker solo acepta pentests con atestación fresca del guard del host."""

from __future__ import annotations

import os
from pathlib import Path

from fenix_egress_guard.attestation import AttestationError, verify
from fenix_egress_guard.policy import NETWORK_NAME, NETWORK_SUBNET

from .egress_fence import EgressFenceMissingError

STATUS_PATH = Path("/run/mindguard-fenix/egress-status.json")


def verify_attestation(
    *,
    path: Path = STATUS_PATH,
    expected_network: str | None = None,
    expected_subnet: str | None = None,
    expected_hash: str | None = None,
    boot_id: str | None = None,
    now: float | None = None,
) -> None:
    network = expected_network or os.environ.get("FENIX_STRIX_NETWORK", "")
    subnet = expected_subnet or os.environ.get("FENIX_STRIX_SUBNET", "")
    if network != NETWORK_NAME or subnet != str(NETWORK_SUBNET):
        raise EgressFenceMissingError("Red Strix distinta del /24 PROD aprobado")
    try:
        verify(
            path,
            network=network,
            subnet=subnet,
            policy_hash=expected_hash or os.environ.get("FENIX_EGRESS_POLICY_HASH", ""),
            boot_id=boot_id,
            now=now,
        )
    except AttestationError as error:
        raise EgressFenceMissingError(str(error)) from error
