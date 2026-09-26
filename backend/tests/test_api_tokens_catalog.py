"""Pruebas del catálogo de scopes y del contrato del modelo `ApiToken`.

El catálogo es contrato público: aparece en la documentación, en la interfaz del panel y
en los scripts de los clientes. Un scope que desaparece rompe integraciones que no se
enteran hasta que reciben un 403 en producción, y un scope que se añade por descuido
concede un permiso que nadie pidió. Estas pruebas fijan ambas cosas.

## Por qué SHA-256 y no una función lenta

`argon2` y `bcrypt` existen para contraseñas, que el usuario elige y que tienen pocos
bits de entropía. Un token de API se genera con 256 bits aleatorios del sistema: las
probabilidades de acierto de un atacante que haga fuerza bruta sobre el hash son
irrelevantes, y la función lenta solo añadiría latencia a cada petición autenticada.
Lo que sí es imprescindible es que el secreto **no** sea recuperable desde la base,
y eso lo garantiza que solo se guarde su SHA-256.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import Table

from backend.apps.api_access.models import (
    API_TOKEN_PREFIX,
    TOKEN_HASH_LENGTH,
    TOKEN_SECRET_BYTES,
    ApiToken,
    token_prefix_for,
)
from backend.apps.api_access.scopes import (
    ALL_SCOPES,
    SCOPE_CATALOG,
    Scope,
    scope_is_known,
)
from backend.core.security import generate_api_token, hash_api_token


def _token() -> str:
    """Genera un token con los parametros de la tabla, no con los de la primitiva."""

    return generate_api_token(API_TOKEN_PREFIX, TOKEN_SECRET_BYTES)

# --------------------------------------------------------------------------- #
# Catálogo
# --------------------------------------------------------------------------- #


def test_the_catalog_has_exactly_forty_six_scopes() -> None:
    """El número es parte del contrato, no un resultado de contar.

    Se afirma el número exacto y no `len(ALL_SCOPES)`, porque una prueba que compara un
    valor consigo misma sigue pasando cuando alguien borra un permiso por error.
    """

    assert len(ALL_SCOPES) == 46, f"el catálogo tiene {len(ALL_SCOPES)} scopes y son 46"
    # Y los 46 se reparten en 16 grupos, que son los que ve el panel.
    assert len(SCOPE_CATALOG) == 16


def test_every_group_declares_the_scopes_the_owner_asked_for() -> None:
    """Cada grupo tiene exactamente los permisos pactados, sin sobras ni faltantes.

    Se compara contra un diccionario escrito a mano en vez de validar el enum, para que
    un grupo que quedara vacío por una edición también falle en vez de pasar por tener
    cero scopes.
    """

    esperado = {
        "pentests": {"read", "create", "abort", "delete"},
        "vulnerabilities": {"read", "triage", "export"},
        "repositories": {"read", "connect", "sync", "delete"},
        "pr_reviews": {"read", "trigger"},
        "knowledge": {"read", "write", "delete"},
        "cve": {"read", "search"},
        "billing": {"read", "checkout", "ledger_read"},
        "tokens": {"read", "create", "revoke"},
        "webhooks": {"read", "create", "update", "delete"},
        "organization": {"read", "update"},
        "members": {"read", "invite", "remove"},
        "audit": {"read"},
        "enterprise": {
            "networks_read",
            "networks_write",
            "containers_read",
            "containers_write",
            "supply_chain_read",
            "supply_chain_write",
        },
        "llm": {"models_read", "usage_read"},
        "admin": {"models_manage", "analytics_read"},
        "mcp": {"connect", "invoke"},
    }
    real = {
        grupo: {scope.action for scope in scopes} for grupo, scopes in SCOPE_CATALOG.items()
    }
    assert real == esperado
    # 16 grupos. Se afirma para que un grupo nuevo obligue a decidir si suma o no.
    assert len(SCOPE_CATALOG) == 16


def test_the_scope_string_is_group_and_action() -> None:
    """`pentests:read`, no `pentests.read` ni `PENTESTS_READ`.

    El separador es lo que separa el grupo de la acción, y cualquier otro obligaría a
    traducir la convención en cada cliente que la use.
    """

    assert Scope.PENTESTS_READ == "pentests:read"
    assert Scope.BILLING_LEDGER_READ == "billing:ledger_read"
    assert Scope.ENTERPRISE_SUPPLY_CHAIN_WRITE == "enterprise:supply_chain_write"
    for scope in ALL_SCOPES:
        group, separator, action = scope.value.partition(":")
        assert separator == ":", scope
        assert group and action, scope
        assert " " not in scope.value


def test_unknown_scopes_are_rejected_and_known_ones_are_not() -> None:
    assert scope_is_known("pentests:read") is True
    assert scope_is_known("pentests:write") is False
    assert scope_is_known("admin:everything") is False
    assert scope_is_known("") is False
    # Una cadena de espacios colaría con una comparación ingenua contra un conjunto
    # de valores que incluye la cadena vacía.
    assert scope_is_known("   ") is False


def test_the_enum_cannot_hold_a_value_the_catalog_does_not_contain() -> None:
    """`Scope("pentests:inventado")` tiene que fallar.

    Sin esto, un typo en una dependencia pasaría el chequeo en tiempo de ejecución y
    devolvería 403 para siempre, sin que el cliente tuviera forma de saber cuál es el
    scope correcto.
    """

    with pytest.raises(ValueError):
        Scope("pentests:inventado")


# --------------------------------------------------------------------------- #
# Generación y hash del secreto
# --------------------------------------------------------------------------- #


def test_generated_token_has_the_documented_shape() -> None:
    raw = _token()
    assert raw.startswith(API_TOKEN_PREFIX)
    secret = raw.removeprefix(API_TOKEN_PREFIX)
    assert len(secret) == TOKEN_SECRET_BYTES * 2
    assert re.fullmatch(r"[0-9a-f]{64}", secret), secret


def test_generated_tokens_are_never_repeated() -> None:
    """Unicidad del secreto, de la que depende toda la tabla.

    Sin esto, dos tokens distintos con el mismo hash serían la misma credencial, y
    revocar uno desactivaría el acceso del otro sin que nadie lo pidiera.
    """

    digests = {hash_api_token(_token()) for _ in range(5_000)}
    assert len(digests) == 5_000


def test_hash_is_stable_and_does_not_contain_the_secret() -> None:
    raw = _token()
    digest = hash_api_token(raw)
    assert digest == hash_api_token(raw)
    assert len(digest) == TOKEN_HASH_LENGTH
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert raw not in digest
    assert raw.removeprefix(API_TOKEN_PREFIX) not in digest


def test_hash_is_not_the_plain_secret() -> None:
    """Guardar el token en claro convertiría una fuga de la base en acceso total.

    Es una aserción casi tautológica sobre SHA-256, y se queda porque la consecuencia de
    revertirla es un incidente, no un fallo de una prueba.
    """

    raw = API_TOKEN_PREFIX + "ab" * 32
    assert hash_api_token(raw) != raw


# --------------------------------------------------------------------------- #
# Modelo
# --------------------------------------------------------------------------- #


def test_token_prefix_is_a_label_not_a_fragment_of_credential() -> None:
    """Cuatro caracteres sobre 64 identifican la fila sin ayudar a un atacante.

    Para adivinar el resto hay que probar 16^60 posibilidades, no 16^4: el prefijo
    acorta la lista de candidatos, no la elimina. Con más caracteres dejaría de ser una
    etiqueta y empezaría a ser un cracking asistido.
    """

    raw = API_TOKEN_PREFIX + "0123456789abcdef" * 4
    prefix = token_prefix_for(raw)
    assert prefix == f"{API_TOKEN_PREFIX}0123"
    assert len(prefix) <= 16
    assert raw not in prefix
    assert raw.removeprefix(API_TOKEN_PREFIX) not in prefix


def test_prefixes_can_collide_which_is_why_the_lookup_is_by_hash() -> None:
    """Cuatro hexadecimales dan 65.536 combinaciones, así que las repeticiones ocurren.

    Esto documenta por qué `token_prefix` **no** puede ser la clave de búsqueda: sirve
    para que el usuario distinga sus tokens en la lista, y nada más.
    """

    prefijos = {token_prefix_for(_token()) for _ in range(5_000)}
    assert len(prefijos) < 5_000


@pytest.mark.parametrize(
    ("columna", "nulo", "longitud"),
    [
        ("id", False, None),
        ("organization_id", False, None),
        ("name", False, 100),
        ("token_prefix", False, 16),
        ("token_hash", False, 64),
        ("scopes", False, None),
        ("expires_at", True, None),
        ("last_used_at", True, None),
        ("revoked_at", True, None),
        ("created_at", False, None),
    ],
)
def test_model_columns_match_the_declared_contract(
    columna: str, nulo: bool, longitud: int | None
) -> None:
    tabla = cast(Table, ApiToken.__table__)
    columnas = tabla.columns
    assert columna in columnas, f"falta la columna {columna}"
    definition = columnas[columna]
    assert definition.nullable is nulo, f"{columna}: nulabilidad"
    if longitud is not None:
        assert getattr(definition.type, "length", None) == longitud, (
            f"{columna}: longitud"
        )


def test_organization_id_is_restricted_not_cascaded() -> None:
    """R4: un tenant con tokens conserva su fila porque también conserva su rastro.

    `CASCADE` borraría los tokens en silencio. `RESTRICT` hace visible la política: si
    algún día se intentara borrar un tenant físicamente, saltaría el error de la clave
    foránea en lugar de dejar al cliente sin credenciales sin que nadie se entere.
    """

    claves = list(ApiToken.__table__.columns["organization_id"].foreign_keys)
    assert len(claves) == 1
    assert claves[0].ondelete == "RESTRICT"


def test_token_hash_is_unique_so_a_collision_is_impossible_not_unlikely() -> None:
    """La unicidad la impone la base, no la aplicación.

    Un `UNIQUE` en el esquema convierte una colisión de SHA-256 en un error de
    inserción, en vez de en dos filas donde revocar una credencial afecta a la otra.
    """

    indices = cast(Table, ApiToken.__table__).indexes
    uniques = {
        tuple(column.name for column in index.columns)
        for index in indices
        if index.unique
    }
    assert ("token_hash",) in uniques, uniques


def test_token_hash_is_indexed() -> None:
    """Cada petición autenticada busca por hash: sin índice es un barrido secuencial.

    Se comprueba sobre los metadatos, que es donde se define el índice. Su
    efectividad se mide después con datos reales, no aquí.
    """

    indices = {
        nombre
        for nombre in (i.name for i in cast(Table, ApiToken.__table__).indexes)
        if nombre
    }
    assert any("hash" in name for name in indices), indices


def test_scopes_default_to_an_empty_tuple_and_never_null() -> None:
    """Un token sin scopes asignados es un token que no puede hacer nada.

    El `default` es lo que impide que una fila creada por una ruta que olvide el campo
    quede con `scopes = None` y reviente al leerla.

    Se afirma el **comportamiento** y no la identidad con `list`: SQLAlchemy envuelve el
    invokable en otra funcion que se llama igual y se comporta igual, asi que `is list`
    seria una prueba de un detalle de la libreria que ademas falla.
    """

    columna = cast(Table, ApiToken.__table__).columns["scopes"]
    assert columna.nullable is False
    assert columna.default is not None
    generador = cast(Any, columna.default)
    assert callable(generador.arg)
    # SQLAlchemy lo invoca con un contexto de ejecucion; `None` basta para comprobar
    # que el valor por defecto es una lista vacia y no `None`.
    assert generador.arg(None) == []


def test_datetime_columns_are_timezone_aware() -> None:
    """Una expiración sin zona horaria se compara mal en cuanto alguien cruza UTC.

    PostgreSQL guarda `timestamptz`; si la columna fuera `timestamp` sin zona, un
    `expires_at` calculado en hora local compararía bien en el servidor y mal en el
    cliente, y el fallo aparecería solo en uno de los dos.
    """

    for nombre in ("expires_at", "last_used_at", "revoked_at", "created_at"):
        columna = cast(Table, ApiToken.__table__).columns[nombre]
        assert getattr(columna.type, "timezone", None) is True, nombre


def test_last_used_at_has_no_default() -> None:
    """`last_used_at` lo escribe la autenticación, no el reloj de la fila.

    Un `now()` de servidor lo llenaría al insertar y el panel mostraría un token recién
    creado como "usado hace 0 s", una mentira que hace imposible detectar un token
    olvidado durante meses.
    """

    columna = cast(Table, ApiToken.__table__).columns["last_used_at"]
    assert columna.default is None
    assert columna.server_default is None


def test_expired_and_revoked_are_separate_states() -> None:
    """Son dos columnas distintas porque soporte necesita responder a dos preguntas.

    "¿Caducó o lo revocaste?" no tiene la misma respuesta ni la misma acción: la
    primera se arregla renovando, la segunda no tiene arreglo. Guardarlas en una sola columna
    obligaría a adivinar.
    """

    organization_id = uuid.uuid4()
    ahora = datetime.now(UTC)
    expirado = ApiToken(
        organization_id=organization_id,
        name="expirado",
        token_prefix="mgf_live_a1b2",
        token_hash="0" * 64,
        expires_at=ahora - timedelta(days=1),
    )
    revocado = ApiToken(
        organization_id=organization_id,
        name="revocado",
        token_prefix="mgf_live_c3d4",
        token_hash="1" * 64,
        scopes=[Scope.PENTESTS_READ],
        revoked_at=ahora,
    )
    assert expirado.revoked_at is None
    assert revocado.revoked_at is not None
    assert revocado.expires_at is None
    assert expirado.expires_at is not None
    assert organization_id is not None
