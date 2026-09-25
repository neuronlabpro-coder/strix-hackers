import pytest
from pydantic import ValidationError

from backend.apps.organizations.schemas import RegisterRequest
from backend.apps.organizations.services import (
    hash_email_verification_token,
    normalize_email,
)


def test_normalize_email_strips_whitespace_and_lowercases() -> None:
    assert normalize_email("  Usuario.Test@Example.COM  ") == "usuario.test@example.com"


def test_email_verification_token_is_hashed_without_preserving_raw_token() -> None:
    token = "token-de-verificacion-de-prueba"

    hashed_token = hash_email_verification_token(token)

    assert hashed_token != token
    assert len(hashed_token) == 64
    assert hashed_token == hash_email_verification_token(token)


def test_registration_schema_rejects_invalid_email() -> None:
    with pytest.raises(ValidationError):
        RegisterRequest(
            email="not-an-email",
            password="contraseña-de-prueba-segura-123",
            full_name="Usuario de prueba",
        )
