import pytest
from pydantic import ValidationError

from backend.apps.organizations.schemas import RegisterRequest
from backend.apps.organizations.services import normalize_email


def test_normalize_email_strips_whitespace_and_lowercases() -> None:
    assert normalize_email("  Usuario.Test@Example.COM  ") == "usuario.test@example.com"


def test_registration_schema_rejects_invalid_email() -> None:
    with pytest.raises(ValidationError):
        RegisterRequest(
            email="not-an-email",
            password="contraseña-de-prueba-segura-123",
            full_name="Usuario de prueba",
        )
