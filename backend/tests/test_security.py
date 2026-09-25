import uuid
from datetime import timedelta

import pytest

from backend.core.config import settings
from backend.core.security import (
    InvalidTokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def test_password_hash_is_salted_and_verifiable() -> None:
    password = "contraseña-de-prueba-segura-123"
    first_hash = hash_password(password)
    second_hash = hash_password(password)

    assert first_hash != password
    assert first_hash != second_hash
    assert verify_password(password, first_hash)
    assert not verify_password("otra-contraseña", first_hash)


def test_password_hash_accepts_configured_maximum_length() -> None:
    password = "a" * settings.password_max_length

    hashed_password = hash_password(password)

    assert verify_password(password, hashed_password)


def test_access_token_contains_subject_and_expiration() -> None:
    user_id = uuid.uuid4()

    token = create_access_token(
        {"sub": str(user_id), "email": "user@example.com"},
        expires_delta=timedelta(minutes=5),
    )
    claims = decode_access_token(token)

    assert claims["sub"] == str(user_id)
    assert claims["email"] == "user@example.com"
    assert "exp" in claims


def test_invalid_access_token_is_rejected() -> None:
    with pytest.raises(InvalidTokenError):
        decode_access_token("token-invalido")
