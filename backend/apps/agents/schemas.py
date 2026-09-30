"""Contratos de la API de agentes.

## Por qué el agente tiene su propia forma de autenticarse

Porque un agente no es una persona. Habla con su propio token, que solo puede pedir trabajo y
devolver resultados, y **no** llega a ningún endpoint de panel. Compartir el esquema de
`get_current_tenant` con él habría hecho que "es una credencial más" pareciera una decisión
inofensiva, cuando en realidad es lo que separa un escaneo de un acceso a la cuenta de un
cliente.

## Por qué el token se devuelve una vez y en su propia respuesta

Igual que en la API pública: se entrega en el alta y no se vuelve a guardar nunca, porque solo
se guarda su hash. La diferencia es que aquí la respuesta es la que el operador copia al
archivo de configuración del agente, así que el campo se llama `token` y no hay forma de que se
confunda con un identificador.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from backend.apps.agents.models import (
    AGENT_TOKEN_PREFIX,
    AgentJobKindEnum,
    AgentJobStatusEnum,
    AgentStatusEnum,
)

#: Un CIDR IPv4. Se limita a IPv4 a propósito: es lo que hace `ip_network` con `strict=False`
#: y lo que todo el camino de escaneo soporta hoy. Aceptar IPv6 aquí daría la impresión de que
#: se puede escanear un segmento v6, y no es cierto.
CIDR_IPV4 = re.compile(
    r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2}$",
)

#: Prefijo de una referencia de imagen de contenedor: registro opcional, y el nombre del
#: repositorio. No se valida el digest ni la etiqueta porque quien los pone es el cliente y el
#: que dice si existen es el agente; lo que se comprueba aquí es la **forma**, que es lo único
#: que se puede comprobar sin red.
REFERENCIA_IMAGEN = re.compile(
    r"""^
    (?:(?P<registro>[a-zA-Z0-9.\-]+(?::[0-9]+)?)/)?
    (?P<repositorio>[a-z0-9]+(?:[._\-/][a-z0-9]+)*)
    (?::(?P<etiqueta>[a-zA-Z0-9._\-]+))?
    (?:@(?P<digest>sha256:[a-f0-9]{64}))?
    $""",
    re.VERBOSE,
)


class SistemaObjetivoEnum(StrEnum):
    """El sistema operativo para el que el operador dice desplegar el agente.

    ## Por qué esto es un enum cerrado y no una cadena libre

    Porque la pregunta que responde no es «qué pone el operador» sino «qué instrucciones le
    enseño». Un texto libre daría tres respuestas: uno escribe `Win2022`, otro `windows`, otro
    `windows server 2022`, y la guía no puede decidir con cuál de los tres se topa. Con el enum,
    lo que no está en la lista es «no lo sé» y se enseña la variante que funciona en todos los
    sistemas.

    ## Por qué `desconocido` es un valor y no un `None`

    Porque `None` en la base de datos no distingue «el operador no lo dijo» de «dijo que no lo
    sabe», y las dos son la misma cosa para la guía. Guardarlo como valor hace que la columna
    nunca sea ambigua y que el listado pueda filtrar por él.
    """

    LINUX = "linux"
    WINDOWS = "windows"
    MACOS = "macos"
    DESCONOCIDO = "desconocido"


class AgentCreate(BaseModel):
    """Alta de un agente."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: Annotated[str, Field(min_length=1, max_length=128)]
    agent_version: Annotated[str | None, Field(max_length=32)] = None
    platform_hint: Annotated[str | None, Field(max_length=64)] = None

    #: El sistema para el que el operador **dice** que va a desplegar el agente.
    #:
    #: ## Por qué esto y no `platform_hint`
    #:
    #: Porque `platform_hint` lo manda el agente en su primer latido, y un agente que no ha
    #: conectado **no lo manda**. En la tabla, un alta recién hecha salía con «—» en la columna
    #: de plataforma, que es exactamente el momento en que la pregunta del operador es «¿esto
    #: funciona en su Windows Server?» y la respuesta es que no había forma de contestarla.
    #:
    #: ## Por qué uno declarado y otro medido
    #:
    #: Porque no son lo mismo y no conviene mezclarlos. El declarado es lo que el operador
    #: **dice**; el medido es lo que el agente **es**. Si un operador dice `windows` y el agente
    #: reporta `linux/amd64`, hay dos hechos, y el panel enseña los dos: el declarado para elegir
    #: las instrucciones y el medido para diagnosticar. El medido no sobrescribe el declarado:
    #: que un operador se equivocara al declararlo no es un error de la plataforma, y borrar su
    #: respuesta dejaría la fila sin la pista con la que empezar.
    sistema_objetivo: SistemaObjetivoEnum = SistemaObjetivoEnum.DESCONOCIDO

    @field_validator("name")
    @classmethod
    def _nombre_no_vacio(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("el nombre del agente no puede estar vacío")
        return value


class AgentEnrolled(AgentCreate):
    """Lo que se devuelve tras el alta: la fila más el token, una sola vez."""

    id: uuid.UUID
    token: str
    token_prefix: str
    status: AgentStatusEnum
    enrolled_at: dt.datetime

    model_config = ConfigDict(from_attributes=True)


class AgentItem(BaseModel):
    """Un agente en la lista del panel."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    token_prefix: str
    status: AgentStatusEnum
    agent_version: str | None
    platform_hint: str | None
    sistema_objetivo: SistemaObjetivoEnum
    enrolled_at: dt.datetime
    last_seen_at: dt.datetime | None
    revoked_at: dt.datetime | None
    revoked_reason: str | None


class AgentPage(BaseModel):
    """Lista de agentes del tenant, con la forma de las demás listas del proyecto."""

    items: list[AgentItem]
    total: int
    limit: int
    offset: int


class AgentJobRequest(BaseModel):
    """Encargo de escaneo.

    ## Por qué valida la forma del destino y no su existencia

    Porque la existencia **solo** se puede comprobar desde dentro de la red del cliente, que es
    donde está el registro o la red privada. Un backend que comprobara si `alpine:3.20` existe
    tendría que hacer una petición a Docker Hub en cada alta: es lento, es un dato que se queda
    viejo al día siguiente, y en el caso de un CIDR no hay nada que comprobar. La forma se
    valida aquí; la existencia la responde el agente, y si no existe el trabajo termina en
    `FAILED` con el motivo, que es un resultado honesto y no un error de validación.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    kind: AgentJobKindEnum
    target: Annotated[str, Field(min_length=1, max_length=512)]
    priority_order: Annotated[int, Field(ge=1, le=100)] = 1

    @field_validator("target")
    @classmethod
    def _destino_con_forma_del_tipo(cls, value: str, info: ValidationInfo) -> str:
        # `info.data` trae los campos **anteriores** a este en el orden de declaración. Por eso
        # `kind` tiene que declararse antes que `target`, y no es un detalle: si se invirtiera,
        # `info.data.get("kind")` devolvería `None` siempre y la comprobación de forma no se
        # ejecutaría nunca, en silencio y sin que ninguna prueba lo notara.
        kind = info.data.get("kind")
        if kind is AgentJobKindEnum.NETWORK_SCAN:
            if not CIDR_IPV4.match(value):
                raise ValueError(
                    "una red se indica como CIDR IPv4, por ejemplo 10.10.0.0/24. Un prefijo "
                    "mayor que /29 son 126 direcciones por trabajo, y cada escaneo tiene un "
                    "presupuesto de tiempo y de tokens que se agotaría en el primero"
                )
            return value
        if kind is AgentJobKindEnum.CONTAINER_SCAN:
            if not REFERENCIA_IMAGEN.match(value):
                raise ValueError(
                    "una imagen se indica como alpine:3.20, "
                    "ghcr.io/organizacion/aplicacion:1.4.0 o "
                    "registry.example.com:5000/equipo/servicio@sha256:..."
                )
            return value
        return value


class AgentJobItem(BaseModel):
    """Un trabajo tal y como lo ve el cliente."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: AgentJobKindEnum
    target: str
    status: AgentJobStatusEnum
    priority_order: int
    agent_id: uuid.UUID | None
    agent_name: str | None
    requested_by: uuid.UUID | None
    attempt_count: int
    claimed_at: dt.datetime | None
    completed_at: dt.datetime | None
    error_message: str | None
    #: `True` cuando el `result_digest` guardado coincide con el `result` que se está
    #: devolviendo. Es la comprobación de R4 hecha **al leer**, que es donde vale: una huella
    #: que no se comprueba al leer es una firma que nadie firma.
    evidence_intact: bool
    created_at: dt.datetime


class AgentJobDetail(AgentJobItem):
    """Un trabajo con su resultado, para la pantalla de detalle."""

    result: dict | None


class AgentJobPage(BaseModel):
    items: list[AgentJobItem]
    total: int
    limit: int
    offset: int


class AgentSummary(BaseModel):
    """Los números de cabecera, y lo que alimenta los gráficos.

    ## Por qué hay un resumen **por tipo de trabajo** y no uno solo

    Porque `/containers` y `/networks` son dos pantallas distintas con dos cosas distintas que
    contar, y un resumen único obligaría a cada una a filtrar en el navegador lo que el servidor
    ya sabe. En un resumen de contenedores, «hosts con puertos abiertos» es un cero que no
    significa nada; en uno de redes, «paquetes» lo es.

    ## Por qué `vivos` y `total_agentes` vienen **separados**

    Porque son la misma pregunta con dos respuestas posibles, y la que importa para decidir
    si un escaneo va a pasar es la primera. Un cliente con tres agentes dados de alta y ninguno
    conectado tiene "agentes" y no tiene nada, y la pantalla tiene que poder decirlo.
    """

    #: Agentes dados de alta y **conectados** en los últimos minutos.
    total_agentes: int
    vivos: int
    #: Un agente se cuenta como vivo si se ha identificado en esta ventana. Tres veces el
    #: intervalo de sondeo por defecto del agente, para que un sondeo perdido no lo declare
    #: caído.
    ventana_de_vida: int

    # --- Contenedores ---
    total_imagenes: int = Field(ge=0)
    total_paquetes: int = Field(ge=0)
    total_capas: int = Field(ge=0)
    #: Paquetes por ecosistema: `{"apk": 88, "dpkg": 412}`. Es el dato del gráfico de barras.
    paquetes_por_ecosistema: dict[str, int] = Field(default_factory=dict)
    #: Imágenes cuyo inventario se pudo leer, y las que no. La diferencia es lo que dice
    #: cuántas se leyeron de verdad.
    imagenes_inventariadas: int = Field(ge=0)
    imagenes_sin_inventario: int = Field(ge=0)

    # --- Redes ---
    total_redes: int = Field(ge=0)
    total_hosts: int = Field(ge=0)
    total_puertos: int = Field(ge=0)
    #: Puertos abiertos por número, ordenado de mayor a menor. Alimenta el gráfico de barras.
    puertos_por_numero: dict[str, int] = Field(default_factory=dict)
    #: Direcciones analizadas en total, que es lo que dice cuanto se ha cubierto de verdad.
    direcciones_analizadas: int = Field(ge=0)

    #: Escaneos por estado, con los cinco estados siempre presentes aunque valgan cero.
    #: Un gráfico de torta con un solo trozo no dice nada, y un estado ausente no es un estado
    #: que no ocurre: es un estado que no se ha dibujado.
    por_estado: dict[str, int] = Field(default_factory=dict)

    #: Escaneos por día, para la serie de barras de la cabecera.
    por_dia: list[ScanCountByDay] = Field(default_factory=list)


class ScanCountByDay(BaseModel):
    """Un dia y cuanto se escaneo ese dia."""

    dia: str
    escaneos: int = Field(ge=0)
    terminados: int = Field(ge=0)
    fallidos: int = Field(ge=0)


class AgentJobClaimed(BaseModel):
    """Lo que el agente recibe al pedir trabajo.

    Deliberadamente **no** lleva el token de ningún otro agente, ni una credencial de la
    plataforma, ni nada con lo que pueda hablar con el SaaS más allá de este endpoint.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: AgentJobKindEnum
    target: str
    #: Los puertos que el agente tiene que mirar. Los pone la plataforma para que un escaneo de
    #: red sea acotado: escanear los 65 535 puertos de cada host de un /24 son cientos de
    #: millones de conexiones y no termina nunca.
    ports: list[int] = Field(default_factory=list)
    #: Cuándo tiene que devolver el resultado. Vencido sin respuesta, el trabajo vuelve a la
    #: cola, y por eso el agente sabe que tiene este tiempo y no "el tiempo que tarde".
    #:
    #: Nulo a propósito en el tipo, no por descuido: el plazo lo pone el servidor al reservar el
    #: trabajo, y un `claim` de una fila sin `lease_expires_at` significa que alguien la escribió
    #: por la vía equivocada. Declararlo `nulo` hace que esa fila se note al construir la
    #: respuesta en vez de fallar más tarde con un `None` donde se esperaba una fecha.
    lease_expires_at: dt.datetime | None = None


class AgentJobReport(BaseModel):
    """Lo que el agente devuelve al terminar."""

    model_config = ConfigDict(extra="forbid")

    result: dict | None = None
    error_message: Annotated[str | None, Field(max_length=4_000)] = None
    #: `True` si el trabajo no se pudo completar, `False` si sí. Se declara explícitamente en
    #: lugar de deducirlo de que `error_message` venga vacío, porque un fallo que no dejó
    #: mensaje y un éxito con resultado vacío son cosas distintas y se distinguen por el estado.
    failed: bool = False

    @field_validator("result")
    @classmethod
    def _resultado_no_es_lista(cls, value: dict | None) -> dict | None:
        # Se exige un objeto y no una lista. El resultado de un escaneo son hechos
        # indexados —imagen, digest, paquetes, hosts— y una lista no tiene dónde colgar el
        # digest de la imagen que da nombre al resto. Aceptar las dos formas dejaría el
        # contenido a la imaginación de quien lee, que es justo lo que una evidencia no
        # puede ser.
        if value is not None and not isinstance(value, dict):
            raise ValueError("el resultado de un escaneo es un objeto, no una lista")
        return value


class AgentHeartbeat(BaseModel):
    """Latido del agente: dice que sigue vivo y en qué versión."""

    model_config = ConfigDict(extra="forbid")

    agent_version: Annotated[str | None, Field(max_length=32)] = None
    platform_hint: Annotated[str | None, Field(max_length=64)] = None


#: Reexportado para que quien monte el agente no tenga que saber de dónde sale el prefijo.
PREFIXO_TOKEN_AGENTE = AGENT_TOKEN_PREFIX
