import base64

import pytest

from backend.core.crypto import CryptoError, decrypt_secret, encrypt_secret


def test_aes_gcm_round_trip_does_not_persist_plaintext() -> None:
    plaintext = "github-token-super-secreto"

    ciphertext = encrypt_secret(plaintext)

    assert plaintext not in ciphertext
    assert ciphertext != plaintext
    assert decrypt_secret(ciphertext) == plaintext


def test_aes_gcm_rejects_tampered_ciphertext() -> None:
    ciphertext = encrypt_secret("token")
    prefix, nonce, encoded_text = ciphertext.split(".")
    padding = "=" * (-len(encoded_text) % 4)
    encoded = bytearray(base64.urlsafe_b64decode(encoded_text + padding))
    encoded[-1] ^= 1

    with pytest.raises(CryptoError):
        decrypt_secret(
            f"{prefix}.{nonce}.{base64.urlsafe_b64encode(encoded).decode('ascii').rstrip('=')}"
        )


def test_aes_gcm_binds_ciphertext_to_tenant_provider_and_field() -> None:
    ciphertext = encrypt_secret(
        "tenant-token",
        organization_id="org-a",
        provider="GITHUB",
        field="access",
    )

    assert decrypt_secret(
        ciphertext,
        organization_id="org-a",
        provider="GITHUB",
        field="access",
    ) == "tenant-token"
    with pytest.raises(CryptoError):
        decrypt_secret(
            ciphertext,
            organization_id="org-b",
            provider="GITHUB",
            field="access",
        )
    with pytest.raises(CryptoError):
        decrypt_secret(
            ciphertext,
            organization_id="org-a",
            provider="GITHUB",
            field="refresh",
        )


def test_aes_gcm_rejects_malformed_ciphertext() -> None:
    with pytest.raises(CryptoError):
        decrypt_secret("not-a-valid-ciphertext")
