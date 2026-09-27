"""Esquemas de la API pública de tokens.

La respuesta de alta es el único momento en que el secreto existe fuera del proceso que
lo generó, y por eso tiene su propio esquema y su propio comentario: no es un campo más,
es el campo que no se puede recuperar.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.apps.api_access.models import ApiToken, ApiTokenTypeEnum
from backend.apps.api_access.scopes import (
    ALL_SCOPES,
    SCOPE_CATALOG,
    Scope,
    is_default_scope,
    normalize_scopes,
    unknown_scopes,
)

#: Vigüedad máxima que se admite. Un token sin caducidad es una credencial permanente que
#: sobrevive a quien la creó, y el punto de este mecanismo es que deje de servir sin que
#: nadie tenga que acordarse de revocarla.
MAX_EXPIRY_DAYS = 365

#: Vigüedad por defecto cuando el cliente no dice nada. Noventa días cubren un trimestre
#: de integración y obligan a pasar por la pantalla de tokens al menos una vez al año,
#: que es justo la frecuencia con la que un cliente se acuerda de que tiene estos tokens.
DEFAULT_EXPIRY_DAYS = 90


class ApiTokenCreate(BaseModel):
    """Solicitud de alta de un token de API."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(min_length=1)

    #: Personal o de servicio. El valor por defecto es `personal` porque es lo que se quiere en
    #: la mayoria de los casos: un token que caduca con la persona es el que se puede revocar
    #: con criterio al darle de baja. `service_key` es la excepcion deliberada, para
    #: integraciones que deben sobrevivir a que la persona se vaya.
    token_type: ApiTokenTypeEnum = ApiTokenTypeEnum.PERSONAL

    #: Días hasta la caducidad, o `None` para un token que **no** caduca.
    #:
    #: El máximo sigue acotado a `MAX_EXPIRY_DAYS` cuando hay número, porque "un año" es lo
    #: que el backend sabe rotar. La ausencia de caducidad se pide con `null`, no con `0`: `0`
    #: días significaría "caduca hoy mismo", que es un valor sin sentido y que un cliente que
    #: lo enviara por error se llevaría un 422 sin explicación.
    #:
    #: `0` se acepta y se traduce a `None` a propósito, por el caso real de un selector HTML
    #: cuya primera opción es "Sin expiración" y cuyo valor es `0`. Se acepta con un
    #: `field_validator` que lo dice, en vez de con un `le=0` silencioso que dejaría pasar el
    #: valor sin que nadie supiera que significaba otra cosa.
    expires_in_days: int | None = Field(default=DEFAULT_EXPIRY_DAYS, ge=1, le=MAX_EXPIRY_DAYS)

    @field_validator("expires_in_days", mode="before")
    @classmethod
    def _normalizar_sin_expiracion(cls, value: object) -> object:
        """Traduce el `0` de un selector de "sin expiración" al `None` del dominio.

        Sin este validador, el `ge=1` rechazaria el `0` con un `422` cuyo texto dice "input
        should be greater than or equal to 1", que no dice ni qué se pidió ni por qué. Con el,
        el cliente obtiene un token sin caducidad cuando eso es lo que quiso, y un `422` con un
        texto que nombra el rango cuando lo que queria era otra cosa.
        """

        return None if value == 0 else value

    @field_validator("scopes")
    @classmethod
    def _reject_unknown_scopes(cls, value: list[str]) -> list[str]:
        """Rechaza los scopes que no están en el catálogo, nombrándolos.

        La validación va aquí y no solo en el servicio porque el error tiene que llegar
        como `422` diciendo qué scope no existe. Sin nombrarlo, el cliente tiene que
        comparar listas a mano para encontrar un typo de tres letras.

        Los duplicados se colapsan con `dict.fromkeys`, que conserva el orden de
        aparición. Pedir `["pentests:read", "pentests:read"]` no es un problema de
        seguridad, pero sí lo es de interfaz: las casillas aparecerían marcadas dos
        veces y el cliente guardaría un token con una lista de permisos redundante.
        """

        rechazados = unknown_scopes(value)
        if rechazados:
            raise ValueError(f"Scopes no reconocidos: {', '.join(rechazados)}")
        return list(dict.fromkeys(value))

    def resolved_expiration(self) -> datetime | None:
        """La fecha de caducidad, o `None` si el token no caduca.

        ## Por qué la fecha se calcula aqui y no en el servicio

        Porque "cuándo caduca" es una pregunta del **dominio** y su respuesta va en la fila. Si
        el servicio la calculara, cada ruta que creara un token tendría que acordarse de la
        conversión de días a fecha, y la primera que se olvidara produciría un token que caduca
        a medianoche del día de creación —que es un token válido, y por eso el fallo no se ve.

        Se calcula contra `datetime.now(UTC)` del servidor y no contra una fecha que llegue del
        cliente: un token cuya caducidad depende de la hora de quien lo crea es un token cuyo
        comportamiento no se puede reproducir.
        """

        if self.expires_in_days is None:
            return None
        return datetime.now(UTC) + timedelta(days=self.expires_in_days)

    def resolved_scopes(self) -> tuple[Scope, ...]:
        """Los scopes ya validados, en orden estable para que la fila sea comparable.

        Delega en el catálogo para que ordenar y deduplicar tengan una sola
        implementación. Dos peticiones con los mismos permisos en distinto orden tienen
        que producir la misma fila, o el panel ve dos tokens idénticos como si fueran
        distintos y el usuario no sabe que ya tiene ese token.
        """

        return normalize_scopes(self.scopes)


class ApiTokenResponse(BaseModel):
    """Metadatos de un token.

    No incluye `raw_token` **a propósito**: ese campo tiene su propio esquema, el del
    alta. Un esquema único con un campo opcional que se rellena solo en el `POST` es una
    forma cómoda de convertir "el token aparece en la lista" en un error el día que
    alguien serialice sin el campo.

    Tampoco expone `token_hash`. No es que se oculte en la respuesta: es que la
    respuesta se construye por una lista explícita de campos, así que un campo nuevo en
    el modelo no aparece por descuido.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    token_prefix: str
    scopes: list[str]
    #: Personal o de servicio. Viaja en la respuesta y no se deduce el cliente.
    token_type: ApiTokenTypeEnum
    created_at: datetime
    #: `None` cuando el token no caduca, que es la forma de que el panel lo
    #: distinga de una fecha de hace un segundo. Un token sin caducidad con la fecha a `None`
#: se leeria como "caducado" en un cliente que compare con `new Date()` sin mirar.
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None

    @classmethod
    def from_model(cls, token: ApiToken) -> Self:
        """Construye la respuesta desde el modelo nombrando cada campo.

        Se escribe a mano en vez de usar `from_attributes` porque el modelo tiene
        `token_hash` y `organization_id` y `from_attributes` copiaría todo lo que
        coincida por nombre. La lista explícita es la que garantiza que añadir una
        columna al modelo no la añada a la respuesta.
        """

        return cls(
            id=token.id,
            name=token.name,
            token_prefix=token.token_prefix,
            scopes=list(token.scopes),
            created_at=token.created_at,
        token_type=token.token_type,
            expires_at=token.expires_at,
            last_used_at=token.last_used_at,
            revoked_at=token.revoked_at,
        )


class ApiTokenCreatedResponse(ApiTokenResponse):
    """Alta de un token: los metadatos **más** el secreto, una única vez.

    El campo lleva un `repr` desactivado a propósito. Si este objeto acaba en el log de
    errores de una dependencia o de un middleware, su representación no debe llevar el
    secreto dentro. La consecuencia es que hay que revisar a mano que ninguna ruta de
    esta app registre cuerpos de respuesta.
    """

    raw_token: str = Field(
        description="El secreto en claro. Solo se devuelve aquí; no es recuperable.",
        repr=False,
    )


class ApiTokenPage(BaseModel):
    """Listado de tokens del tenant activo."""

    items: list[ApiTokenResponse]
    total: int
    include_revoked: bool


class ApiScopeDefinitionResponse(BaseModel):
    """Un permiso del catálogo y sus metadatos de presentación."""

    scope: str
    action: str
    group: str
    label_key: str
    is_privileged: bool
    #: Si el permiso entra en el juego por defecto del token nuevo.
    #:
    #: Viaja en el catalogo y no se deduce en el panel para que el boton «Defaults» no
    #: tenga una lista propia: una segunda lista se desincroniza de la primera en cuanto
    #: alguien anade un permiso y olvida tocar las dos, y el sintoma es un token nuevo que
    #: puede hacer menos de lo que el panel promete.
    is_default: bool


class ApiScopeGroupResponse(BaseModel):
    """Los permisos de un recurso, en el orden en que el panel los presenta."""

    group: str
    scopes: list[ApiScopeDefinitionResponse]


class ApiScopeCatalogResponse(BaseModel):
    """Los 46 scopes, agrupados.

    El panel los pide en vez de tenerlos escritos. Un catálogo duplicado en el frontend es
    un segundo lugar donde un permiso puede existir sin que el backend lo conceda, o
    al revés: el usuario marca una casilla y el `422` le dice que ese permiso no existe,
    que es la peor forma de descubrir un desajuste que una sola fuente habría evitado.

    El `total` viaja en la respuesta para que el panel pueda decir "12 de 46" sin contar
    por su cuenta, y para que una prueba pueda afirmar el número contra el servidor.
    """

    groups: list[ApiScopeGroupResponse]
    total: int


def scope_catalog_response() -> ApiScopeCatalogResponse:
    """Construye la respuesta del catálogo desde la definición del módulo."""

    return ApiScopeCatalogResponse(
        groups=[
            ApiScopeGroupResponse(
                group=name,
                scopes=[
                    ApiScopeDefinitionResponse(
                        scope=definition.scope.value,
                        action=definition.action,
                        group=definition.group,
                        label_key=definition.label_key,
                        is_privileged=definition.is_privileged,
                        is_default=is_default_scope(definition.scope),
                    )
                    for definition in definitions
                ],
            )
            for name, definitions in SCOPE_CATALOG.items()
        ],
        total=len(ALL_SCOPES),
    )
