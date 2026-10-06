"""Alta, autenticación y reparto de trabajo de los agentes de escaneo.

## Por qué el token del agente no pasa por `get_current_tenant`

Porque un agente no es una persona y no debe poder hacer lo que hace una. Si compartieran el
mismo esquema de autenticación, bastaría con que un token de agente llegara a un endpoint de
panel para tener una sesión, y eso está a un `Depends` de distancia. Aquí la autenticación es una
función aparte que resuelve a un `ScannerAgent` y **no** puede producir un `TenantContext`, de
modo que el endpoint de panel no tiene forma de aceptarla aunque alguien lo monte mal.

## Por qué el trabajo se **tira** hacia el agente

Porque un agente es un proceso que el cliente enciende y apaga, detrás de un NAT y de un
cortafuegos que no controlamos. Un push exigiría que el cliente abriera un puerto entrante en su
red de producción, que es justo lo que nadie quiere. Tirar solo necesita **salida HTTPS**, que es
la dirección que los cortafuegos ya dejan pasar.

La contrapartida es que hay que resolver la muerte del agente a mitad de un trabajo, y de eso se
ocupa el **alquiler**: un trabajo reclamado tiene `lease_expires_at`, y vencido sin resultado
vuelve a la cola. Sin eso, un agente que se apaga deja un trabajo en curso para siempre, que es
un trabajo perdido y un hueco en la postura que nadie sabría leer.

## Por qué la reserva se toma con `FOR UPDATE SKIP LOCKED`

Por dos razones distintas, y las dos necesarias.

- **Sin `FOR UPDATE`**, dos agentes que piden en el mismo instante leen la misma fila antes de
  que ninguno la escriba, y ejecutan el mismo escaneo dos veces. Un orden no da exclusión
  mutua; hace falta el bloqueo explícito.
- **Sin `SKIP LOCKED`**, el segundo agente **espera** a que el primero confirme, y el segundo
  trabajo de la cola se sirve en serie. `SKIP LOCKED` hace que se salte la fila ocupada y se
  lleve la siguiente, que es lo que significa repartir trabajo.

Y el cambio de estado va **en la misma sentencia** que la selección. Si el `UPDATE` fuera
aparte, un proceso que muriera entre las dos dejaría el trabajo en `QUEUED` con la fila
bloqueada hasta que la transacción se deshiciera. Es un estado que se recupera solo, pero una
recuperación que no debería hacer falta.

## Por qué el resultado lleva huella y se comprueba al leer

Porque el resultado **es** la evidencia: dice qué puertos había abiertos, y dentro de seis meses
lo que vale es poder demostrar que el registro no se cambió después. La huella es el SHA-256 de
la forma canónica del JSON, y se recalcula **al leer**: una firma que no se comprueba al leer es
un adorno. La protección de escritura vive en un trigger de la base, que es donde no se puede
evitar escribiendo SQL.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import uuid
from typing import Final

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.apps.agents.models import (
    AGENT_TOKEN_PREFIX,
    AGENT_TOKEN_PREFIX_VISIBLE,
    AGENT_TOKEN_SECRET_BYTES,
    AgentJob,
    AgentJobKindEnum,
    AgentJobStatusEnum,
    AgentStatusEnum,
    ScannerAgent,
)
from backend.apps.agents.schemas import (
    AgentCreate,
    AgentJobReport,
    AgentJobRequest,
    AgentSummary,
    ScanCountByDay,
)
from backend.core.security import generate_api_token, hash_api_token

logger = logging.getLogger(__name__)

#: Cuánto tiempo tiene un agente para devolver un trabajo. Lo que importa es que sea **acotado**,
#: porque un trabajo sin plazo es un trabajo que se pierde en silencio.
LEASE_SEGUNDOS: Final[int] = 900

#: Puertos que se miran en un escaneo de red.
#:
#: No son "los puertos peligrosos": son los que aparecen en la superficie expuesta de un
#: contenedor y en la de un servicio interno. La lista es corta a propósito. Escanear los 65 535
#: puertos de cada dirección de un `/24` son cientos de millones de conexiones que no terminan
#: nunca, y un trabajo que no termina consume presupuesto sin devolver nada. Un cliente que
#: necesite otros puertos los pide; esta lista es el punto de partida, no una politica.
PUERTOS_POR_DEFECTO: Final[tuple[int, ...]] = (
    21, 22, 23, 25, 53, 80, 110, 111, 135, 139, 143, 389, 443, 445,
    1433, 1521, 2049, 2375, 2376, 3000, 3306, 3389, 5000, 5432, 5672,
    5900, 6379, 8000, 8080, 8443, 8888, 9090, 9200, 11211, 15672, 27017, 28017,
)

#: Cuánto se guarda el resultado de un escaneo de red, en filas de la respuesta, antes de
#: recortarlo. No es un límite de seguridad sino de tamaño: un `/16` son 65 535 direcciones y su
#: JSON puede pesar cientos de megabytes, que no es una respuesta HTTP ni una fila de base
#: razonable. Lo que se recorta se dice en el propio resultado, para que el recorte sea visible.
MAX_FILAS_RESULTADO: Final[int] = 2_000

#: Longitud máxima de una cadena dentro del resultado.
#:
#: ## Por qué 8192 y no más
#:
#: Porque lo más largo que llega de verdad es un `banner` de un servicio escaneado, y el
#: escaneo ya los lee acotados a 512 bytes por puerto —un `TCP` que devuelve 4 KB y se queda
#: esperando no es un servicio, es un ahorco de recursos—. Aquí el límite es la red de seguridad
#: para un `banner` enorme que llegue por otro camino, y se pone a 8 KB porque es un orden de
#: magnitud por encima de lo legítimo y por debajo de lo que empieza a doler en memoria.
#:
#: Y el recorte **se marca** en la propia cadena, porque un banner cortado que no lo dice es un
#: dato falso con forma de dato verdadero.
MAX_CARACTERES_RESULTADO: Final[int] = 8_192

#: Niveles de anidamiento a partir de los cuales `_acotar` sustituye el **contenedor** entero.
#:
#: ## Por qué seis y no tres
#:
#: Porque la forma real de un escaneo de red tiene cinco: `hosts` → cada `host` → `puertos` →
#: cada `puertos` → `banner`. Un tope de tres, que parecía de sobra, sustituía cada `ip` por un
#: marcador y hacía crecer el resultado. Seis deja la forma real con un nivel de margen y sigue
#: cortando antes de que un `JSONB` anidado a propósito agote la pila **en la ruta que lo
#: recibe**, que es la peor forma de quedarse sin responder.
MAX_PROFUNDIDAD_RESULTADO: Final[int] = 6


class AgentNotFoundError(LookupError):
    """El token no corresponde a ningún agente vigente."""


class AgentRevokedError(PermissionError):
    """El agente existe pero se dio de baja."""


class JobNotClaimableError(LookupError):
    """El trabajo no está disponible: no existe, no es de esta organización, o ya terminó."""


def entidad_inexistente() -> HTTPException:
    """El `404` de estos endpoints.

    `404` y no `403`: un `403` confirmaría que el recurso existe en alguna parte, y con eso
    basta para enumerar identificadores y saber qué tiene el tenant vecino. El `404` no
    distingue "no existe" de "no es tuyo", que es lo único que no filtra información.
    """

    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No encontrado")


def token_prefix_for(raw_token: str) -> str:
    """La etiqueta visible del token, para distinguir dos agentes sin ver secretos.

    Es el prefijo del tipo de token más los primeros caracteres del secreto. El prefijo va
    incluido porque es lo que permite saber de un vistazo, en un log, si una credencial es de un
    agente o de la API pública.
    """

    return raw_token[: len(AGENT_TOKEN_PREFIX) + AGENT_TOKEN_PREFIX_VISIBLE]


def huella_resultado(resultado: dict | None) -> str | None:
    """El SHA-256 de la forma canónica de un resultado, o `None` si no hay resultado.

    ## Por qué canónica y por qué ordenar las claves

    Porque `json.dumps` respeta el orden de inserción del diccionario, y serializar el mismo
    contenido dos veces puede dar textos distintos si cambia el recorrido. La huella tiene que
    ser del contenido y no de lo accidental, así que se ordena por clave y se separa con comas
    y dos puntos.

    El separador importa por un motivo concreto: sin él, `{"ab": "c"}` y `{"a": "bc"}` producen el
    mismo texto y por tanto la misma huella. Es una colisión alcanzable, no teórica.
    """

    if resultado is None:
        return None
    canónico = json.dumps(resultado, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canónico.encode("utf-8")).hexdigest()


def preservar_evidencia(trabajo: AgentJob) -> bool:
    """Si el resultado que se está leyendo sigue coincidiendo con su huella.

    Se recalcula en lugar de fiarse de la columna. Un trabajo sin resultado no está dañado:
    todavía no terminó, y `True` es la respuesta honesta para que la interfaz no lo marque como
    manipulado.
    """

    if trabajo.result is None:
        return True
    return huella_resultado(trabajo.result) == trabajo.result_digest


def recortar_resultado(resultado: dict | None) -> dict[str, object]:
    """Acota las listas de un resultado y declara el recorte.

    ## Por qué se recorta aquí y no en el agente

    Porque el agente es de **otro** despliegue y no es código de este repositorio: no se puede
    obligar a que recorte, y una versión antigua de un agente ya instalada devolvería lo que
    devolviera. Acotar en el servidor es lo único que funciona con los agentes que ya están
    fuera, y es también lo que evita que un `JSONB` de cientos de megatios entre en la tabla.

    ## Por qué se declara en el propio resultado

    Porque un recorte silencioso es un dato falso. Si el escaneo vio 4000 hosts y se guardan
    2000, el resultado tiene que decirlo; si no, el cliente lee un inventario que parece
    completo y no lo es, y en un informe de postura eso es peor que no tener informe.

    ## Por qué recorta **una sola vez** y no en profundidad

    Porque el único que llegaba hasta aquí era un elemento de primer nivel. Una estructura
    anidada —`{"a": {"b": [ ... ]}}`— pasaba intacta, y con ella un agente con un bug podía
    escribir un `JSONB` de cientos de megabytes. Un escaneo de red real tiene dos niveles
    (`hosts` → `puertos`) y ninguno más, así que no hace falta una recursión con ciclo ni un
    contador de profundidad: dos pasadas y se acabó.

    ## Por qué se acotan las **claves** y no solo las listas

    Porque una cadena de 400 MB en una clave es un `JSONB` de 400 MB igual, y el recorte de
    listas no lo tocaría. Los `banner` de un servicio escaneado vienen de la red del cliente y
    su tamaño no lo decide nadie aquí: por eso hay un tope por cadena, además del de listas.
    """

    if resultado is None:
        return {}
    # El `total` y el `recortado` de primer nivel se siguen publicando, porque son lo que lee
    # la tabla del panel. Los niveles mas hondos llevan la marca dentro, que es lo unico que
    # cabe ahi.
    #
    # El `_acotar` sobre un `dict` devuelve siempre un `dict`, y el `isinstance` no es
    # decorativo: es lo que le **dice** al comprobador de tipos que de aquí en adelante se puede
    # indexar. Sin él, `recortado` es `object` y todo lo de abajo es un error de tipos. Es el
    # mismo patron que usan las demas funciones de este modulo con lo que devuelve el ORM.
    recortado = _acotar(resultado)
    if not isinstance(recortado, dict):  # pragma: no cover - `_acotar` devuelve dict
        return {}
    for clave, valor in resultado.items():
        if isinstance(valor, list) and len(valor) > MAX_FILAS_RESULTADO:
            recortado[f"{clave}_total"] = len(valor)
            recortado[f"{clave}_recortado"] = True
    return recortado


def _acotar(valor: object, profundidad: int = 0) -> object:
    """Acota **una** estructura, recursivamente y con tope de profundidad.

    ## Por qué recursiva y no de dos niveles

    Porque la versión de dos niveles tenía un agujero que un caso de tres niveles —un `host` con
    dos listas dentro— atraviesa sin tocar nada. Y un `JSONB` que un agente con un bug puede
    escribir no tiene por qué tener la forma que el agente actual produce. Un recorte con
    forma fija solo protege la forma que el recorte conoce.

    ## Por qué hay tope de profundidad y no recursión libre

    Porque la estructura viene de la red y un agente con un bug —o alguien que se haga pasar por
    uno— puede mandar un anidamiento arbitrariamente profundo. Sin tope, `recortar_resultado` se
    convierte en un vector de agotamiento de pila **en la ruta que recibe el resultado**, que es la
    peor forma de que un recurso se quede sin responder.

    Tres niveles de sobra para lo que produce el agente: `hosts` → `puertos` → `banner`. A partir
    del cuarto no se acota, se **sustituye por un marcador**, porque un dato al que no se puede
    llegar no se puede recortar y no se puede declarar recortado.

    ## Por qué el límite superior es de bytes y no de elementos

    Porque el número de elementos no acota nada por sí solo: 2 000 entradas de 400 KB son 800 MB.
    Los dos límites juntos sí acotan, y por eso están los dos.
    """

    if isinstance(valor, str):
        if len(valor) > MAX_CARACTERES_RESULTADO:
            return f"{valor[:MAX_CARACTERES_RESULTADO]}... [recortado, {len(valor)} caracteres]"
        return valor
    if isinstance(valor, list | dict):
        # El tope se comprueba **aquí**, al entrar en un contenedor, y no en cada hoja.
        #
        # ## Por qué no en cada hoja, y qué pasaba
        #
        # Comprobarlo al principio de la función, sin mirar el tipo, sustituye **cada escalar**
        # que está a la profundidad tope por un marcador de cincuenta caracteres. Con la forma
        # real de un escaneo de red —`hosts` → `puertos` → `banner`— eso convertía 2 000
        # objetos `{ip: ...}` de ocho bytes en 2 000 cadenas de cincuenta, y un "recorte" que
        # pasaba de 46 KB a 112 KB. Se vio al medir el caso, no al leerlo.
        #
        # Con la comprobación aquí, un contenedor demasiado hondo sí se sustituye entero y sus
        # hojas no se tocan nunca. Un dato inalcanzable no se puede recortar ni declarar
        # recortado: se reemplaza.
        if profundidad >= MAX_PROFUNDIDAD_RESULTADO:
            return (
                f"<recortado: supera {MAX_PROFUNDIDAD_RESULTADO} niveles de anidamiento>"
            )
    if isinstance(valor, list):
        recortada = [_acotar(elemento, profundidad + 1) for elemento in valor[:MAX_FILAS_RESULTADO]]
        if len(valor) > MAX_FILAS_RESULTADO:
            # La marca va **dentro** de la lista, no al lado en el diccionario padre: una lista
            # anidada no tiene sitio al lado, y un recorte sin marcar en un anidamiento es un
            # recorte que miente en el sitio donde más cuesta verlo.
            recortada.append(
                f"<recortado: {len(valor) - MAX_FILAS_RESULTADO} elementos mas>"
            )
        return recortada
    if isinstance(valor, dict):
        return {clave: _acotar(contenido, profundidad + 1) for clave, contenido in valor.items()}
    return valor



# --------------------------------------------------------------------------- #
# Alta y revocación
# --------------------------------------------------------------------------- #


async def inscribir_agente(
    session: AsyncSession, organization_id: uuid.UUID, payload: AgentCreate
) -> tuple[ScannerAgent, str]:
    """Crea el agente y devuelve la fila junto al token **una sola vez**.

    El token no se vuelve a guardar en ninguna parte del proceso, y por eso la función devuelve
    la tupla: quien llama tiene que poner el secreto en la respuesta HTTP, y si se pierde ahí la
    única salida es dar de baja el agente y emitir otro. Un token del que se puede volver a
    pedir la copia es un token que acaba filtrándose.
    """

    raw_token = generate_api_token(AGENT_TOKEN_PREFIX, AGENT_TOKEN_SECRET_BYTES)
    agente = ScannerAgent(
        organization_id=organization_id,
        name=payload.name,
        token_hash=hash_api_token(raw_token),
        token_prefix=token_prefix_for(raw_token),
        agent_version=payload.agent_version,
        platform_hint=payload.platform_hint,
        sistema_objetivo=payload.sistema_objetivo.value,
        status=AgentStatusEnum.ACTIVE,
    )
    session.add(agente)
    await session.commit()
    await session.refresh(agente)
    logger.info(
        "Agente de escaneo inscrito: organization_id=%s id=%s", organization_id, agente.id
    )
    return agente, raw_token


async def revocar_agente(
    session: AsyncSession, organization_id: uuid.UUID, agent_id: uuid.UUID, motivo: str | None
) -> ScannerAgent:
    """Da de baja un agente **sin** borrar su historial.

    Se marca `REVOKED` y se anota el motivo, pero la fila se queda. La razón es la de
    `AgentStatusEnum`: revocar es una decisión de seguridad, y una decisión de seguridad necesita
    poder explicarse seis meses después. Además, los trabajos que ejecutó conservan su resultado,
    que es lo que el cliente pagó por obtener.

    Y **no** se borra la fila ni sus trabajos, porque eso los dejaría sin agente al que atribuir:
    un escaneo sin saber quién lo hizo es un dato sin dueño, y en un informe de postura eso es
    peor que un escaneo con el nombre de un agente que ya no está dado de alta. La revocación
    desactiva al agente; la atribución se queda.
    """

    agente = await cargar_agente_de(session, organization_id, agent_id)
    if agente.status is AgentStatusEnum.REVOKED:
        return agente
    agente.status = AgentStatusEnum.REVOKED
    agente.revoked_at = dt.datetime.now(dt.UTC)
    agente.revoked_reason = motivo
    await session.commit()
    await session.refresh(agente)
    logger.info(
        "Agente de escaneo revocado: organization_id=%s id=%s motivo=%s",
        organization_id,
        agent_id,
        motivo or "sin motivo",
    )
    return agente


async def cargar_agente_de(
    session: AsyncSession, organization_id: uuid.UUID, agent_id: uuid.UUID
) -> ScannerAgent:
    """El agente, filtrando por organización **dentro** del `WHERE`.

    R3: el filtro va en la consulta y no después, porque comprobar la pertenencia en Python
    significaría haber cargado ya la fila de otro tenant.
    """

    resultado = await session.execute(
        select(ScannerAgent).where(
            ScannerAgent.id == agent_id,
            ScannerAgent.organization_id == organization_id,
        )
    )
    agente = resultado.scalar_one_or_none()
    if agente is None:
        raise AgentNotFoundError(str(agent_id))
    return agente


# --------------------------------------------------------------------------- #
# Autenticación del agente
# --------------------------------------------------------------------------- #


async def autenticar_agente(session: AsyncSession, raw_token: str) -> ScannerAgent:
    """Resuelve un token de agente, o falla.

    ## Por qué el prefijo se descarta **antes** de tocar la base

    Un `mgf_live_...` es un token de la API pública y un `mgf_agent_...` es de un agente. La
    comprobación del prefijo es una constante frente a una búsqueda, y descarta de entrada la
    clase de credenciales más numerosa del sistema. No es una optimización: es no gastar una
    consulta de base en una petición que ya se sabe de otro tipo.

    ## Por qué el token revocado da un error distinto del inexistente

    A propósito. Al operador le dice "revócalo o emite otro", que es accionable; a un atacante
    que prueba tokens no le sirve, porque un token revocado no se puede convertir en uno vigente.
    Lo que no se hace es devolver un error genérico que no le diga a ninguno de los dos qué
    hacer.
    """

    if not raw_token.startswith(AGENT_TOKEN_PREFIX):
        raise AgentNotFoundError("el token no es de un agente")

    resultado = await session.execute(
        select(ScannerAgent).where(ScannerAgent.token_hash == hash_api_token(raw_token))
    )
    agente = resultado.scalar_one_or_none()
    if agente is None:
        raise AgentNotFoundError("token desconocido")
    if agente.status is AgentStatusEnum.REVOKED:
        raise AgentRevokedError(str(agente.id))
    return agente


async def registrar_latido(
    session: AsyncSession, agente: ScannerAgent, version: str | None, platform: str | None
) -> ScannerAgent:
    """Actualiza `last_seen_at` y lo que el agente declara de sí mismo.

    ## Por qué no escribe si nada cambió

    Porque este endpoint se llama cada pocos segundos por cada agente, y una escritura por
    latido en una tabla compartida es ruido que acaba notándose en `pg_stat_user_tables` de
    producción. Se compara con lo que ya había: si el latido trae lo mismo que ya estaba, no hay
    nada que actualizar y no se toca la fila. `last_seen_at` **sí** se escribe siempre, porque
    eso es justamente lo que el latido dice.

    Es la diferencia entre "tengo un agente dado de alta" y "tengo un agente vivo", que son
    preguntas distintas. La segunda es la que un cliente hace cuando un escaneo no llega, y sin
    este campo no hay forma de contestarla.
    """

    ahora = dt.datetime.now(dt.UTC)
    agente.last_seen_at = ahora
    if version and version != agente.agent_version:
        agente.agent_version = version
    if platform and platform != agente.platform_hint:
        agente.platform_hint = platform
    await session.commit()
    await session.refresh(agente)
    return agente


# --------------------------------------------------------------------------- #
# Encargos
# --------------------------------------------------------------------------- #


async def encolar_trabajo(
    session: AsyncSession,
    organization_id: uuid.UUID,
    requested_by: uuid.UUID | None,
    payload: AgentJobRequest,
) -> AgentJob:
    """Pone un trabajo en la cola, para que lo recoja cualquier agente del tenant.

    ## Por qué no se asigna a un agente concreto

    Porque en la red de un cliente puede haber tres agentes —uno por nodo, uno por VLAN— y ninguno
    sabe cuál de ellos alcanza qué segmento. Asignar en el alta convertiría al cliente en un
    enrutador de su propia topología, que es un mapa que cambia cada vez que alguien reinstala
    una máquina.

    El coste es que un trabajo puede acabar en el agente que peor lo ve, y por eso el trabajo
    **no se pierde**: termina en `FAILED` con el motivo, el operador ve que hay un agente que no
    llegó al destino, y lo vuelve a encolar. Un escaneo que no encuentra su destino es un dato;
    uno que se queda en la cola para siempre es un agujero.

    ## Por qué aquí se cobran créditos y antes no se cobraba nada

    Porque este módulo era, hasta ahora, **el único camino de escritura del producto que no pasaba
    por la contabilidad**. `queue_pentest` reserva con `apply_credit_delta` y comprueba
    `InsufficientCreditsError`; `encolar_trabajo` creaba una fila y ya está. La consecuencia no
    era un descuido de aesthetics: un miembro de un tenant podía encolar ilimitados escaneos de
    red —35 puertos sobre hasta 254 direcciones, con 64 hilos en la máquina del cliente— sin que
    nada apareciera en el ledger, y el trabajo real lo pagaba la plataforma.

    ## Por qué el cobro va en esta transacción y no en el worker

    Porque el asiento y el trabajo tienen que aparecer juntos o no aparecer. Si el cobro se
    hiciera al entregar, un cliente sin saldo podría ejecutar el escaneo entero y solo ver el
    error al final; y si se hiciera aparte, un fallo entre ambos deja un trabajo sin cobrar o un
    cobro sin trabajo. Aquí, `InsufficientCreditsError` sube **antes** del `add()`, así que no
    hay ni fila ni asiento, y el error que ve el cliente es el de saldo.
    """

    from backend.apps.billing.models import LedgerReasonEnum
    from backend.apps.billing.pricing import scan_credit_cost
    from backend.apps.billing.service import InsufficientCreditsError, apply_credit_delta
    from backend.apps.pentests.models import ScanModeEnum

    trabajo = AgentJob(
        organization_id=organization_id,
        requested_by=requested_by,
        kind=payload.kind,
        target=payload.target,
        priority_order=payload.priority_order,
        status=AgentJobStatusEnum.QUEUED,
    )
    session.add(trabajo)
    # El `flush` va **antes** del cobro, y no por orden: el `id` lo asigna el `default` de la
    # columna en el `INSERT`, no en el constructor. Sin este `flush`, `trabajo.id` es `None` y el
    # `reference_id` acaba siendo la cadena `"None"` — un asiento real con una referencia que no
    # identifica a nada, y la idempotencia que el comentario de abajo promete, perdida.
    #
    # Se detecto porque el test que busca el asiento por `reference_id` no lo encontraba, y no
    # por una asercion sobre el saldo: el saldo habia bajado igualmente, porque el cobro ocurre
    # igual. Es el tipo de fallo que solo aparece si el test mira **las dos** cosas.
    #
    # Y el `reference_id` es el id del trabajo, que es lo que hace el cobro idempotente: si esta
    # funcion volviera a llamarse con el mismo trabajo, el asiento seria el mismo.
    await session.flush()
    try:
        await apply_credit_delta(
            session=session,
            organization_id=organization_id,
            amount=-scan_credit_cost(ScanModeEnum.QUICK),
            reason=LedgerReasonEnum.SCAN_CONSUMPTION,
            reference_id=str(trabajo.id),
            actor_user_id=requested_by,
        )
    except InsufficientCreditsError:
        # El `rollback` es **obligatorio** y no decorativo, y lo detectó una prueba que contaba
        # filas: el `flush` anterior ya había escrito el trabajo, y sin esto el encolado
        # rechazado dejaba un `QUEUED` que ningún agente iba a reclamar y que el cliente no puede
        # ni ver ni borrar. Es peor que no cobrar: es trabajo fantasma en la cola.
        #
        # Y hace falta explícito porque `apply_credit_delta` no confirma cuando falla, y quien
        # llama en este punto ya ha escrito en la sesión.
        await session.rollback()
        raise
    # `apply_credit_delta` confirma la transaccion, asi que el trabajo queda persistido con ella.
    await session.refresh(trabajo)
    logger.info(
        "Trabajo de escaneo encolado: organization_id=%s id=%s kind=%s",
        organization_id,
        trabajo.id,
        trabajo.kind.value,
    )
    return trabajo


#: Los estados en los que un trabajo está **reservado**: lo tomó un agente y todavía no devolvió
#: nada. Solo estos son reponibles; un trabajo `COMPLETED` o `FAILED` no se toca nunca.
ESTADOS_RESERVADOS: Final[tuple[str, ...]] = (
    AgentJobStatusEnum.CLAIMED.value,
    AgentJobStatusEnum.RUNNING.value,
)


async def reponer_alquileres(session: AsyncSession, organization_id: uuid.UUID) -> int:
    """Devuelve a la cola los trabajos cuyo alquiler venció, y dice cuántos.

    ## Por qué un plazo en vez de esperar a que el agente avise

    Porque el agente **no puede** avisar si está muerto: si el proceso se apagó, se cayó la red
    o lo mataron, no hay nadie a quien escuchar. La reserva existe para ese caso, y su
    vencimiento es la única señal de que el trabajo se quedó a medias.

    ## Por qué el `UPDATE` lleva su propia condición de estado

    Porque entre el `SELECT` y el `UPDATE` otro proceso puede haber terminado el trabajo. Un
    `UPDATE` sin la condición repondría a la cola un trabajo ya `COMPLETED`, y su resultado
    quedaría pendiente de ser sobrescrito por el siguiente que lo reclame. Con el estado en el
    propio `UPDATE`, el que pierda la carrera no cambia nada.
    """

    ahora = dt.datetime.now(dt.UTC)
    vencidos = (
        await session.execute(
            select(AgentJob.id).where(
                AgentJob.organization_id == organization_id,
                AgentJob.status.in_(ESTADOS_RESERVADOS),
                AgentJob.lease_expires_at.is_not(None),
                AgentJob.lease_expires_at < ahora,
            )
        )
    ).scalars().all()

    if not vencidos:
        return 0

    await session.execute(
        update(AgentJob)
        .where(
            AgentJob.id.in_(vencidos),
            AgentJob.status.in_(ESTADOS_RESERVADOS),
        )
        .values(
            status=AgentJobStatusEnum.QUEUED.value,
            agent_id=None,
            lease_expires_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    # El número de filas afectadas se cuenta con una segunda consulta en vez de leer
    # `rowcount`, que `AsyncSession.execute` no expone de forma tipada. Sale más barato que
    # un `cast` para prometer un tipo que no se puede prometer, y la consulta lleva su propio
    # filtro de estado, así que cuenta los que **acaban** de volver a la cola y no otros que ya
    # estuvieran esperando.
    total = int(
        (
            await session.execute(
                select(func.count(AgentJob.id)).where(
                    AgentJob.organization_id == organization_id,
                    AgentJob.status == AgentJobStatusEnum.QUEUED.value,
                    AgentJob.agent_id.is_(None),
                    AgentJob.lease_expires_at.is_(None),
                    AgentJob.id.in_(vencidos),
                )
            )
        ).scalar_one()
    )
    # Se registra **siempre**, y no solo cuando `total` es mayor que cero.
    #
    # ## Por qué el cambio
    #
    # Porque el `if total:` envolvía el `logger.info` y dejaba fuera justo el caso que un
    # operador quiere confirmar: el temporizador pasó y no encontró nada. Ese es el resultado
    # que dice «el sistema está bien», y era el único que no se veía.
    #
    # Un `info` por organización y por ciclo no es ruido: `reponer_alquileres` lo llama una
    # tarea periódica, y un `0` en el log es una línea que se lee en dos segundos y evita media
    # hora de sospecha. Si molesta, el nivel se sube a `debug`; no se quita el `if`.
    logger.info(
        "Trabajos de escaneo devueltos a la cola por alquiler vencido: %d (organization_id=%s)",
        total,
        organization_id,
    )
    return total


async def reclamar_trabajo(
    session: AsyncSession, agente: ScannerAgent
) -> tuple[AgentJob, list[int]] | None:
    """Toma el trabajo más antiguo que esté pendiente, o devuelve `None` si no hay ninguno."""

    await reponer_alquileres(session, agente.organization_id)

    ahora = dt.datetime.now(dt.UTC)
    candidato = (
        await session.execute(
            select(AgentJob.id)
            .where(
                AgentJob.organization_id == agente.organization_id,
                AgentJob.status == AgentJobStatusEnum.QUEUED.value,
            )
            .order_by(AgentJob.priority_order, AgentJob.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
    ).scalar_one_or_none()

    if candidato is None:
        return None

    await session.execute(
        update(AgentJob)
        .where(
            AgentJob.id == candidato,
            AgentJob.status == AgentJobStatusEnum.QUEUED.value,
        )
        .values(
            status=AgentJobStatusEnum.CLAIMED.value,
            agent_id=agente.id,
            claimed_at=ahora,
            lease_expires_at=ahora + dt.timedelta(seconds=LEASE_SEGUNDOS),
            attempt_count=AgentJob.attempt_count + 1,
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()

    trabajo = await cargar_trabajo(session, agente.organization_id, candidato)
    if trabajo is None:  # pragma: no cover - solo si alguien lo borra entre medias
        raise JobNotClaimableError(str(candidato))
    return trabajo, list(PUERTOS_POR_DEFECTO)


async def marcar_en_curso(
    session: AsyncSession, agente: ScannerAgent, job_id: uuid.UUID
) -> AgentJob:
    """El agente empezó a trabajar de verdad.

    Separate de `CLAIMED` porque el instante de tomar el trabajo y el de empezarlo no son lo
    mismo, y la diferencia separa "el agente está trabajando" de "el agente aceptó y murió". Los
    dos son trabajo en curso para el cliente; para el operador son fallos distintos con arreglos
    distintos: el primero se espera, el segundo hay que reintentarlo.
    """

    trabajo = await cargar_trabajo_para_agente(session, agente, job_id)
    if trabajo.status is AgentJobStatusEnum.CLAIMED.value:
        trabajo.status = AgentJobStatusEnum.RUNNING
        await session.commit()
        await session.refresh(trabajo)
    return trabajo


async def entregar_trabajo(
    session: AsyncSession, agente: ScannerAgent, job_id: uuid.UUID, reporte: AgentJobReport
) -> AgentJob:
    """Cierra el trabajo con su resultado, o con su fallo.

    ## Por qué el fallo también se registra

    Porque "el escaneo falló" es un resultado, y uno que hay que poder auditar igual que un
    éxito. Un trabajo que termina en `FAILED` sin motivo no dice nada, y un fallo que no se puede
    investigar es un fallo que se repite para siempre sin que nadie sepa por qué.

    ## Por qué se bloquea la fila antes de mirar el estado

    Porque el estado se comprueba **y luego** se escribe, y entre las dos cosas cabe otra
    entrega. Dos reintentos de red del mismo agente —que es lo normal cuando el resultado pesa y
    la red cae a mitad— passarían los dos la comprobación de la línea siguiente, y el segundo
    `UPDATE` lo pararía el trigger de la base. La garantía sería entonces de la base, no del
    código, y la mitad de la invariante —que `result` y `result_digest` van juntos— seguiría
    viviendo en SQL.

    Con `FOR UPDATE` la fila se serializa aquí, la comprobación y la escritura son la misma
    cosa, y el trigger sigue estando —que es lo que protege de un `UPDATE` directo desde SQL—
    pero deja de ser la **única** cosa que protege.

    `reclamar_trabajo` ya lo hacía con `skip_locked`; esta función era la excepción en el módulo.
    """

    trabajo = await cargar_trabajo_para_agente(session, agente, job_id, bloquear=True)

    if trabajo.status in (AgentJobStatusEnum.COMPLETED.value, AgentJobStatusEnum.FAILED.value):
        # El trigger de la base lo impediría igual, pero llegar aquí significa que el cliente
        # está desincronizado: reenvió un resultado ya aceptado. Se devuelve el trabajo tal cual
        # en vez de reventar, porque un reenvío por reintento de red es normal y no un ataque.
        logger.info(
            "Entrega de un trabajo ya terminal ignorada: agent_id=%s job_id=%s", agente.id, job_id
        )
        return trabajo

    ahora = dt.datetime.now(dt.UTC)
    if reporte.failed:
        trabajo.status = AgentJobStatusEnum.FAILED
        trabajo.error_message = reporte.error_message or "el agente no dio motivo del fallo"
    else:
        trabajo.status = AgentJobStatusEnum.COMPLETED
        recortado = recortar_resultado(reporte.result)
        trabajo.result = recortado
        trabajo.result_digest = huella_resultado(recortado)
    trabajo.completed_at = ahora
    await session.commit()
    await session.refresh(trabajo)
    return trabajo


async def cargar_trabajo(
    session: AsyncSession,
    organization_id: uuid.UUID,
    job_id: uuid.UUID,
    *,
    bloquear: bool = False,
) -> AgentJob | None:
    """El trabajo de ese tenant, o `None`.

    ## Por qué `bloquear` es un parámetro y no siempre `True`

    Porque hay dos usos con exigencias distintas. Quien **va a escribir** necesita la fila
    bloqueada, para que la comprobación del estado y la escritura sean la misma operación. Quien
    solo **mira** —el panel, el detalle— no, y poner un `FOR UPDATE` en una lectura bloquearía a
    un agente que está entregando su resultado mientras alguien mira la tabla.
    """

    consulta = select(AgentJob).where(
        AgentJob.id == job_id,
        AgentJob.organization_id == organization_id,
    )
    if bloquear:
        consulta = consulta.with_for_update()
    return (await session.execute(consulta)).scalar_one_or_none()


async def cargar_trabajo_para_agente(
    session: AsyncSession,
    agente: ScannerAgent,
    job_id: uuid.UUID,
    *,
    bloquear: bool = False,
) -> AgentJob:
    """El trabajo, **solo si lo reclamó este agente**.

    ## Por qué el filtro de `agent_id` no es opcional

    Porque hasta aquí solo se comprobaba `organization_id`, y eso dejaba pasar lo que el trigger
    de inmutabilidad no cubre. El trigger protege la fila de un `UPDATE` hecho **desde SQL**, y
    este camino es la API: `entregar_trabajo` escribe `result`, `result_digest` y el estado
    terminal. Dos agentes del mismo tenant —y un tenant puede tener tres, uno por nodo— podían
    pisarse el resultado y la atribución de un escaneo sin que nada se quejara.

    La consecuencia era peor de lo que parece a primera vista, porque el resultado que se
    sobreescribe es **evidencia**: es el inventario de la red del cliente que el cliente pagó por
    obtener, y cuya trazabilidad sostiene un informe de postura. Que dos agentes puedan
    intercambiarse el resultado significa que el informe puede citar un escaneo que no es el que
    se ejecutó.

    ## Por qué `agent_id IS NULL` también se acepta

    Porque es el estado de un trabajo **vencido**: `reponer_alquileres` devuelve a la cola lo que
    no se entregó a tiempo, y al hacerlo deja `agent_id` a `NULL` a propósito, para que cualquier
    agente pueda reclamarlo de nuevo. Si no se aceptara, un trabajo devuelto a la cola no podría
    volver a reclamarse nunca y se quedaría en `QUEUED` para siempre.

    Y el `agent_id IS NULL` **no** es una vía para que un agente escriba el trabajo de otro: un
    trabajo con agente asignado solo se abre para el agente que lo tiene.
    """

    trabajo = await cargar_trabajo(
        session, agente.organization_id, job_id, bloquear=bloquear
    )
    if trabajo is None:
        raise JobNotClaimableError(str(job_id))
    if trabajo.agent_id is not None and trabajo.agent_id != agente.id:
        # No se dice *qué* agente lo tiene: un `403` con el identificador del otro agente
        # convertiría este endpoint en un oráculo de qué agente reclamó qué.
        raise JobNotClaimableError(str(job_id))
    return trabajo


async def listar_trabajos(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    estado: AgentJobStatusEnum | None,
    tipo: AgentJobKindEnum | None,
    limit: int,
    offset: int,
) -> tuple[list[AgentJob], int]:
    """Los trabajos del tenant, filtrados, con el total aparte de la página.

    El total se cuenta con el **mismo** filtro que la página. Contarlo sin filtro y filtrar
    después daría un número que no corresponde a lo que se ve, y ese número es el que se pinta
    arriba.

    ## Por qué el desempate por `id`

    Porque `created_at` es `now()` de servidor y los trabajos nacen a ráfaga: un webhook con
    cinco eventos encola cinco trabajos en la misma transacción, y los cinco comparten marca.
    Con `ORDER BY created_at DESC` y nada más, el reparto de ese empate entre las páginas lo
    decide el planificador, y esta lista **sí** está paginada: el operador ve la misma cola con
    filas repetidas y huecos según por dónde pase. Sin que nada lo indique, porque `total` sale
    bien y los identificadores son correctos.

    El desempate es sobre `AgentJob.id`, la clave primaria de la tabla que se pagina. No hay
    `JOIN`, así que `id` identifica cada fila de la salida sin ambigüedad, y no cambia qué
    filas se devuelven: solo el orden entre las que ya se devolvían.
    """

    condiciones = [AgentJob.organization_id == organization_id]
    if estado is not None:
        condiciones.append(AgentJob.status == estado.value)
    if tipo is not None:
        condiciones.append(AgentJob.kind == tipo.value)

    total = int(
        (
            await session.execute(select(func.count(AgentJob.id)).where(*condiciones))
        ).scalar_one()
    )
    filas = (
        (
            await session.execute(
                select(AgentJob)
                .where(*condiciones)
                .order_by(AgentJob.created_at.desc(), AgentJob.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return list(filas), total


async def nombre_de_agentes(
    session: AsyncSession, ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Los nombres de los agentes que aparecen en una lista de trabajos.

    Va en una consulta aparte y no con un `JOIN` por trabajo porque son los mismos pocos agentes
    para toda la página: una consulta con `IN` sobre cinco valores es una ida a la base, y un
    `JOIN` que se repite por fila es una por trabajo.
    """

    if not ids:
        return {}
    filas = (
        await session.execute(
            select(ScannerAgent.id, ScannerAgent.name).where(ScannerAgent.id.in_(ids))
        )
    ).all()
    return {identificador: nombre for identificador, nombre in filas}


# --------------------------------------------------------------------------- #
# El resumen de la cabecera
# --------------------------------------------------------------------------- #

#: Ventana en la que un agente se considera conectado. Tres veces el intervalo de sondeo por
#: defecto del agente (`intervalo_sondeo = 15`), para que un sondeo perdido —una descarga de la
#: imagen que tarda diez segundos, un corte de red de medio minuto— no lo declare caído y llame
#: a un operador que no tiene nada que arreglar.
VENTANA_DE_VIDA_SEGUNDOS: Final[int] = 300

#: Dias que abarca la serie de la cabecera. Quince son dos semanas, que es lo que alcanza a
#: mirar un cliente para ver si el inventario de sus contenedores se esta manteniendo.
DIAS_DE_SERIE: Final[int] = 15


async def resumen_agente(
    session: AsyncSession, organization_id: uuid.UUID, *, solo_redes: bool = False
) -> AgentSummary:
    """Los numeros de la cabecera, de un tenant.

    ## Por qué se cuenta en Python y no con SQL

    Porque los numeros vienen de dentro de un `JSONB` que **escribió un agente de otro
    despliegue**, y su forma depende de la version de ese agente. `result->>'total_paquetes'` es
    un entero para un escaneo de contenedor y `NULL` para uno de red, y `result->>'hosts'` al
    revés. Una agregacion en SQL tendria que conocer las dos formas, y la que no conoce la
    version nueva daria un cero silencioso en vez de un error.

    Leyendo los resultados y sumando en Python, una forma que no se reconoce se cuenta como lo
    que es: no se ha podido leer. Y el recuento de «lo que no se ha podido leer» se publica, que
    es el numero que hace que los demas sean creibles.

    ## Por qué `solo_redes` **filtra la consulta** y no el recuento

    Porque la respuesta del resumen de redes no tiene que traer el inventario de cuatrocientos
    paquetes de una imagen para dibujar un grafico de puertos. Con el filtro en la consulta, la
    fila se lee una vez y no dos, y la respuesta es la que la pantalla necesita.
    """

    ahora = dt.datetime.now(dt.UTC)
    ventana = ahora - dt.timedelta(seconds=VENTANA_DE_VIDA_SEGUNDOS)

    total_agentes = int(
        (
            await session.execute(
                select(func.count(ScannerAgent.id)).where(
                    ScannerAgent.organization_id == organization_id,
                    ScannerAgent.status == AgentStatusEnum.ACTIVE.value,
                )
            )
        ).scalar_one()
    )
    vivos = int(
        (
            await session.execute(
                select(func.count(ScannerAgent.id)).where(
                    ScannerAgent.organization_id == organization_id,
                    ScannerAgent.status == AgentStatusEnum.ACTIVE.value,
                    ScannerAgent.last_seen_at.is_not(None),
                    ScannerAgent.last_seen_at >= ventana,
                )
            )
        ).scalar_one()
    )

    por_estado = {estado.value: 0 for estado in AgentJobStatusEnum}
    condiciones_estado = [AgentJob.organization_id == organization_id]
    if solo_redes:
        condiciones_estado.append(AgentJob.kind == AgentJobKindEnum.NETWORK_SCAN.value)
    else:
        condiciones_estado.append(AgentJob.kind == AgentJobKindEnum.CONTAINER_SCAN.value)
    for estado, cuenta in (
        await session.execute(
            select(AgentJob.status, func.count(AgentJob.id))
            .where(*condiciones_estado)
            .group_by(AgentJob.status)
        )
    ).all():
        por_estado[str(estado)] = int(cuenta)

    # Los resultados, uno a uno. Son los de un tenant y con un tope: un escaneo de red de un
    # /16 puede traer cientos de hosts, y leerlos todos para contar es trabajo de la base que
    # no aporta nada al contador. El tope es declarativo y se dice, para que un número que
    # parece completo no lo sea sin avisar.
    #
    # El filtro de tipo va **en la consulta** cuando la pantalla es de un solo tipo. La cuenta
    # de abajo saltaria los del otro tipo, pero la fila se habria leido igual, y con un
    # inventario de 400 paquetes en la respuesta eso son cientos de kilobytes que nadie pidió.
    #
    # El `order_by` lleva desempate por `id` por el mismo motivo que las listas paginadas, y
    # aquí el daño es un **`LIMIT` sin `OFFSET`**: no hay páginas, pero sí un tope, y sin un
    # segundo criterio los 200 trabajos que entran en la cuenta son distintos en cada llamada
    # cuando hay ráfagas. El síntoma no es una página duplicada: es que el KPI de «imágenes» o
    # de «paquetes» de la cabecera se mueve solo entre una pantalla y la siguiente, sin que
    # haya cambiado ningún escaneo. Es el peor de los casos porque un número que se corrige
    # solo es un número que miente.
    tope = 200
    condiciones = [
        AgentJob.organization_id == organization_id,
        AgentJob.result.is_not(None),
    ]
    if solo_redes:
        condiciones.append(AgentJob.kind == AgentJobKindEnum.NETWORK_SCAN.value)
    else:
        condiciones.append(AgentJob.kind == AgentJobKindEnum.CONTAINER_SCAN.value)
    resultados = (
        await session.execute(
            select(AgentJob.kind, AgentJob.result, AgentJob.created_at, AgentJob.status)
            .where(*condiciones)
            .order_by(AgentJob.created_at.desc(), AgentJob.id.desc())
            .limit(tope)
        )
    ).all()

    total_imagenes = 0
    total_paquetes = 0
    total_capas = 0
    inventariadas = 0
    sin_inventario = 0
    paquetes_por_ecosistema: dict[str, int] = {}

    total_redes = 0
    total_hosts = 0
    total_puertos = 0
    direcciones_analizadas = 0
    puertos_por_numero: dict[str, int] = {}

    for kind, resultado, _, _ in resultados:
        # El escaneo cuenta **antes** de mirar la forma del resultado.
        #
        # La version anterior hacia `continue` si el `result` no era un objeto, y eso hacia que
        # una imagen cuyo resultado tiene una forma desconocida desapareciera del recuento: el
        # KPI decia una imagen menos y el de "sin inventario" decia cero, de modo que el numero
        # de lo que no se ha podido leer era justo el que no se contaba. Un total que se corrige
        # a si mismo para que los numeros cuadren es un total que miente de otra forma.
        if kind == AgentJobKindEnum.CONTAINER_SCAN:
            total_imagenes += 1
            if not isinstance(resultado, dict):
                sin_inventario += 1
                continue
            total_capas += _entero(resultado.get("total_capas"))
            paquetes = resultado.get("paquetes")
            if isinstance(paquetes, list):
                total_paquetes += len(paquetes)
                inventariadas += 1
                for paquete in paquetes:
                    if isinstance(paquete, dict):
                        ecosistema = str(paquete.get("ecosystem", "otro"))
                        paquetes_por_ecosistema[ecosistema] = (
                            paquetes_por_ecosistema.get(ecosistema, 0) + 1
                        )
            else:
                sin_inventario += 1
        elif kind == AgentJobKindEnum.NETWORK_SCAN:
            total_redes += 1
            if not isinstance(resultado, dict):
                continue
            direcciones_analizadas += _entero(resultado.get("direcciones_analizadas"))
            hosts = resultado.get("hosts")
            if isinstance(hosts, list):
                total_hosts += len(hosts)
                for host in hosts:
                    if not isinstance(host, dict):
                        continue
                    puertos = host.get("puertos")
                    if not isinstance(puertos, list):
                        continue
                    total_puertos += len(puertos)
                    for puerto in puertos:
                        if isinstance(puerto, dict):
                            numero = str(puerto.get("puerto", "?"))
                            puertos_por_numero[numero] = puertos_por_numero.get(numero, 0) + 1

    # Los diez puertos mas frecuentes, y el resto se cuenta como «otros». Un grafico de barras
    # con ochenta barras en una tarjeta de 320 px no es un grafico, es un histograma ilegible.
    mas_frecuentes = sorted(puertos_por_numero.items(), key=lambda par: (-par[1], par[0]))
    principales = dict(mas_frecuentes[:10])
    if len(mas_frecuentes) > 10:
        principales["otros"] = sum(cuenta for _, cuenta in mas_frecuentes[10:])

    por_dia = await _serie_diaria(session, organization_id, ahora, solo_redes)

    return AgentSummary(
        total_agentes=total_agentes,
        vivos=vivos,
        ventana_de_vida=VENTANA_DE_VIDA_SEGUNDOS,
        total_imagenes=total_imagenes,
        total_paquetes=total_paquetes,
        total_capas=total_capas,
        paquetes_por_ecosistema=dict(sorted(paquetes_por_ecosistema.items())),
        imagenes_inventariadas=inventariadas,
        imagenes_sin_inventario=sin_inventario,
        total_redes=total_redes,
        total_hosts=total_hosts,
        total_puertos=total_puertos,
        puertos_por_numero=principales,
        direcciones_analizadas=direcciones_analizadas,
        por_estado=por_estado,
        por_dia=por_dia,
    )


def _entero(valor: object) -> int:
    """Un entero del JSON del agente, o cero.

    El JSON lo escribio otro despliegue y su forma no esta garantizada: una clave puede venir
    como `null`, como cadena o no venir. Un `int()` a pelo seria una excepcion en mitad de una
    peticion de resumen, y un resumen que falla no se ve.
    """

    if isinstance(valor, bool):
        return 0
    if isinstance(valor, int):
        return valor
    if isinstance(valor, str) and valor.isdigit():
        return int(valor)
    return 0


async def _serie_diaria(
    session: AsyncSession,
    organization_id: uuid.UUID,
    ahora: dt.datetime,
    solo_redes: bool,
) -> list[ScanCountByDay]:
    """Escaneos por dia, con los dias sin escaneo a cero.

    Los dias vacios se rellenan porque un grafico con huecos **lee** como si faltaran datos, y
    un cliente ve «solo escaneé el lunes» en vez de «no escaneé el martes». La serie devuelve
    todos los dias de la ventana, en orden, para que el grafico no tenga que inventar los
    intermedios.
    """

    desde = ahora - dt.timedelta(days=DIAS_DE_SERIE - 1)
    condiciones = [
        AgentJob.organization_id == organization_id,
        AgentJob.created_at >= desde,
    ]
    if solo_redes:
        condiciones.append(AgentJob.kind == AgentJobKindEnum.NETWORK_SCAN.value)
    else:
        condiciones.append(AgentJob.kind == AgentJobKindEnum.CONTAINER_SCAN.value)
    completado = AgentJobStatusEnum.COMPLETED.value
    fallido = AgentJobStatusEnum.FAILED.value
    filas = (
        await session.execute(
            select(
                func.date_trunc("day", AgentJob.created_at).label("dia"),
                func.count(AgentJob.id),
                func.count(AgentJob.id).filter(AgentJob.status == completado),
                func.count(AgentJob.id).filter(AgentJob.status == fallido),
            )
            .where(*condiciones)
            .group_by("dia")
        )
    ).all()

    por_dia = {
        dia.date(): (int(total), int(ok), int(malos)) for dia, total, ok, malos in filas
    }
    serie: list[ScanCountByDay] = []
    for desplazamiento in range(DIAS_DE_SERIE):
        dia = (desde + dt.timedelta(days=desplazamiento)).date()
        total, ok, malos = por_dia.get(dia, (0, 0, 0))
        serie.append(
            ScanCountByDay(dia=dia.isoformat(), escaneos=total, terminados=ok, fallidos=malos)
        )
    return serie
