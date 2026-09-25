"""Cifrado autenticado para credenciales Git almacenadas en PostgreSQL."""

from __future__ import annotations

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend.core.config import settings

_CIPHER_VERSION = "v1"
_NONCE_BYTES = 12
_AAD = b"fenix-git-credential-v1"


class CryptoError(ValueError):
    """Error controlado al cifrar o descifrar un secreto Git."""


def _encryption_key() -> bytes:
    return settings.git_encryption_key_bytes


def _encode_part(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_part(value: str) -> bytes:
    if not value:
        raise CryptoError("El secreto cifrado contiene una parte vacía")
    try:
        padding = "=" * (-len(value) % 4)
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as error:
        raise CryptoError("El secreto cifrado no tiene un formato válido") from error


def _context_aad(
    organization_id: str | None,
    provider: str | None,
    field: str,
) -> bytes:
    if organization_id is None and provider is None:
        return _AAD
    if organization_id is None or provider is None or not field:
        raise CryptoError("El contexto de cifrado Git está incompleto")
    return b"fenix-git-credential-v1|" + (
        f"{organization_id}|{provider}|{field}".encode()
    )


def encrypt_secret(
    plaintext: str,
    *,
    organization_id: str | None = None,
    provider: str | None = None,
    field: str = "access",
) -> str:
    """Cifra un token con AES-256-GCM y devuelve solo material portable."""

    if not isinstance(plaintext, str):
        raise TypeError("El secreto debe ser texto")
    nonce = os.urandom(_NONCE_BYTES)
    encrypted = AESGCM(_encryption_key()).encrypt(
        nonce,
        plaintext.encode("utf-8"),
        _context_aad(organization_id, provider, field),
    )
    return f"{_CIPHER_VERSION}.{_encode_part(nonce)}.{_encode_part(encrypted)}"


def decrypt_secret(
    ciphertext: str,
    *,
    organization_id: str | None = None,
    provider: str | None = None,
    field: str = "access",
) -> str:
    """Verifica y descifra un token Git sin revelar valores en errores."""

    if not isinstance(ciphertext, str):
        raise CryptoError("El secreto cifrado debe ser texto")
    parts = ciphertext.split(".")
    if len(parts) != 3 or parts[0] != _CIPHER_VERSION:
        raise CryptoError("Versión de secreto cifrado no soportada")
    nonce = _decode_part(parts[1])
    encrypted = _decode_part(parts[2])
    if len(nonce) != _NONCE_BYTES or len(encrypted) <= 16:
        raise CryptoError("El secreto cifrado tiene un tamaño inválido")
    try:
        plaintext = AESGCM(_encryption_key()).decrypt(
            nonce,
            encrypted,
            _context_aad(organization_id, provider, field),
        )
        return plaintext.decode("utf-8")
    except (InvalidTag, UnicodeDecodeError) as error:
        raise CryptoError("No se pudo autenticar el secreto cifrado") from error
