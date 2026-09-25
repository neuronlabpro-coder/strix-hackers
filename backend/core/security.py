"""Hash de contraseñas y emisión segura de tokens JWT."""

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
from jose import JWTError, jwt

from backend.core.config import settings


class InvalidTokenError(ValueError):
    """Error controlado para tokens ausentes, inválidos o expirados."""


def _password_digest(password: str) -> bytes:
    """Reduce la contraseña a un digest fijo antes de aplicar bcrypt."""

    if not password:
        raise ValueError("La contraseña no puede estar vacía")
    return hashlib.sha256(password.encode("utf-8")).digest()


def hash_password(password: str) -> str:
    """Genera un hash bcrypt con salt aleatorio y coste configurable."""

    digest = _password_digest(password)
    return bcrypt.hashpw(
        digest,
        bcrypt.gensalt(rounds=settings.bcrypt_rounds),
    ).decode("ascii")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifica una contraseña contra su hash sin propagar errores de backend."""

    try:
        return bcrypt.checkpw(
            _password_digest(plain_password),
            hashed_password.encode("ascii"),
        )
    except (UnicodeError, ValueError):
        return False


def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    """Codifica claims de autenticación con expiración explícita."""

    if not data:
        raise ValueError("El token requiere al menos un claim")

    token_lifetime = expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    claims = {
        **data,
        "exp": datetime.now(UTC) + token_lifetime,
    }
    return jwt.encode(
        claims,
        settings.secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def decode_access_token(token: str) -> dict[str, Any]:
    """Decodifica y valida un token usando únicamente el algoritmo configurado."""

    try:
        claims = jwt.decode(
            token,
            settings.secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError as error:
        raise InvalidTokenError("Token de acceso inválido") from error

    if not isinstance(claims, dict):
        raise InvalidTokenError("El token no contiene claims válidos")
    return claims
