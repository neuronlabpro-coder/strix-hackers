"""Hash de contraseñas y emisión segura de tokens JWT."""

import hashlib
import secrets
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


# --------------------------------------------------------------------------- #
# Credenciales de la API pública
# --------------------------------------------------------------------------- #
#
# Las primitivas de un token de API viven aquí y no en `apps/api_access` porque son
# criptografía, no lógica de negocio: el mismo par `generate`/`hash` lo usan el router
# que crea el token y la dependencia que lo valida, y si vivieran en la app, la
# dependencia tendría que importarse a sí misma.


def generate_api_token(
    prefix: str,
    secret_bytes: int,
) -> str:
    """Genera un secreto de token de API con el prefijo indicado.

    `secrets.token_hex` usa el generador criptográfico del sistema operativo, no el
    Mersenne Twister de `random`. Es la diferencia entre un token que nadie puede
    adivinar y uno que se puede predecir observando otros.

    El secreto se devuelve una sola vez y no se guarda en ninguna parte del proceso: el
    llamante lo mete en la respuesta HTTP y se pierde. Si se perdiera ahí, la única
    salida es revocar el token y emitir otro.
    """

    return f"{prefix}{secrets.token_hex(secret_bytes)}"


def hash_api_token(raw_token: str) -> str:
    """Devuelve el SHA-256 hexadecimal del token en claro.

    ## Por qué SHA-256 y no argon2 o bcrypt

    `argon2` y `bcrypt` existen paracontraseñas: valores que elige una persona y que
    tienen pocos bits de entropía, de modo que hay que ralentizar el ataque por fuerza
    bruta. Un token de API se genera con 256 bits aleatorios: no existe nadie que
    pueda recorrer ese espacio, y una función lenta solo añadiría latencia a cada
    petición autenticada de la plataforma.

    Lo que sí es imprescindible, y aquí se cumple, es que el secreto no sea
    recuperable desde la base: solo se guarda su resumen, y `verify` compara resumen
    contra resumen.
    """

    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
