"""Lógica de dominio de la superficie de ataque.

## La regla de colisión entre tenants, y por qué no es "último gana"

Un dominio verificado es un activo **empresarial**: si la organización A ha demostrado
controlar `banco.com`, la organización B no puede acercarse claiming que lo controla, porque
eso le daría derecho a escanear la infraestructura de otra empresa. Es el mismo ataque que
un `--recon` de un tercero, y la razón por la que la verificación existe.

La regla es:

- **Verificado por otra organización** → `409`. Siempre. Aunque el nombre se registre por
  primera vez. Es el caso que no se puede relajar.
- **Registrado sin verificar por otra organización** → `409` también, y aquí la decisión
  merece justificarse, porque parece más estricta de lo necesario.

Un alta pendiente es un *reclamo*: dice "quiero escanear este dominio". Si dos workspaces
reclaman el mismo nombre sin verificar, el que llegue segundo podría publicar el TXT y
quedarse con la verificación, y entonces el primero descubriría la infraestructura de un
dominio que el segundo le aveva reclamado. La **propiedad no se puede establecer dos veces**,
así que se resuelve en el alta, que es cuando todavía es barato.

La alternativa —dejar los dos pendientes y que gane el primero que verifique— es un
`SELECT ... FOR UPDATE` sobre un nombre que no está protegido por nada, y un cliente que
puede esperar. Se resuelve en el alta.

Por eso el `409` del caso pendiente **no** dice "ya está registrado" sin más: el panel
necesita distinguir los dos casos, porque uno es un conflicto real y el otro es un nombre
libre que este workspace no ha reclamado todavía.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from backend.apps.assets.models import (
    AssetTypeEnum,
    DiscoveredAsset,
    DomainClaimConflict,
    VerifiedDomain,
)
from backend.apps.assets.schemas import (
    AssetItem,
    AssetListResponse,
    DomainCreate,
    DomainItem,
    DomainListResponse,
    DomainVerificationFilter,
    normalize_domain,
)
from backend.apps.assets.verifier import (
    DnsLookupOutcome,
    VerificationResult,
    build_txt_record,
    build_txt_record_name,
    verify_domain_txt,
)
from backend.core.config import settings
from backend.core.filtros_texto import coincide, rango_creado

#: Longitud del token de verificación, en bytes, antes de codificar en hexadecimal.
#:
#: 32 bytes son 256 bits de entropía, que es el tamaño de una clave. Es mucho más de lo que
#: hace falta para una prueba de propiedad —con 8 bytes nadie adivinaría un dominio— y es
#: precisamente el margen que hace que el token no sea adivinable por fuerza bruta contra la
#: API, donde un atacante podría pedir verificaciones sin límite.
VERIFICATION_TOKEN_BYTES = 32

#: Techo de activos por ejecución de descubrimiento.
#:
#: Existe porque el descubrimiento trabaja sobre una lista de subdominios candidatos, y sin
#: tope un dominio con miles de entradas válidas produce una transacción que dura minutos y
#: bloquea filas. El tope corta el trabajo y deja el resto para la siguiente ejecución, que
#: actualiza `last_scanned_at` de lo que ya conoce.
#:
#: **No** es una constante de este módulo: es una cuota de escaneo y R1 las saca del código.
#: El valor sale de `Settings.asset_discovery_max_assets`, y esta función solo es el valor por
#: defecto para un `Settings` que no se ha construido. Lo que decide el recorte es siempre la
#: configuración.
DEFAULT_MAX_ASSETS_PER_DISCOVERY = 500


def max_assets_per_discovery(tope: int | None = None) -> int:
    """El techo de activos por ejecución, leído de la configuración.

    Acepta el valor explícito por la misma razón que `discovery.candidate_prefixes`:
    `Settings` es inmutable y una prueba no puede escribir en él. En producción se llama
    sin argumento y el valor sale siempre de la configuración, que es donde R1 quiere que
    estén las cuotas de escaneo.
    """

    if tope is not None:
        return tope
    return settings.asset_discovery_max_assets


class AssetError(RuntimeError):
    """Fallo controlado de la superficie de ataque.

    De dominio, no `HTTPException`: la decisión la toma el servicio y la ruta la traduce.
    """


def _escape_like(termino: str) -> str:
    """Escapa los comodines de `LIKE` para que el término se busque literal.

    ## Por qué aquí el escape importa más que en otros buscadores

    Porque un nombre de dominio lleva `_`, que es el **separador de wildcard de DNS**:
    `acme_corp.com` y `acmeXcorp.com` son nombres válidos y distintos. Buscar `acme_corp` sin
    escapar devolvería también `acmeXcorp`, que el usuario no pidió y que no puede distinguir de
    un fallo del buscador. Y `%` sin escapar devuelve la tabla entera, porque `%` casa con
    cualquier cosa.

    ## Por qué hay una copia aquí y no se importa `core.filtros_texto`

    Porque ese módulo es **nuevo** —se escribió en paralelo a este cambio— y su `rango_creado`
    devuelve `list[ColumnOperators]`, que no encaja en el `where(...)` de SQLAlchemy bajo el
    tipado estricto del proyecto: importarlo dejaba `pyright` en rojo con dos errores que no son
    de este filtro. Importar un módulo a medio escribir para ahorrar seis líneas cambia un
    filtro por un error de tipos, y el filtro es lo que se está entregando.

    La consolidación —que es lo correcto— es un cambio propio, con sus pruebas, y la
    comparación está en `core/filtros_texto.py`. Se deja escrito aquí para que no se pierda: hay
    ahora cuatro copias del escape y una de ellas es el sitio donde un `%` sin escapar
    devolvería la superficie de ataque entera.
    """

    return termino.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _rango_de_creacion(
    desde: date | None, hasta: date | None
) -> list[ColumnElement[bool]]:
    """Las dos condiciones de un rango sobre `created_at`, con el último día **entero**.

    ## Por qué el corte superior es la medianoche del día **siguiente**

    Porque «del 1 al 5» son cinco días, no cinco días menos el último. Con un `<=` sobre la
    medianoche del propio `hasta`, ese día solo aportaría las altas de exactamente las 00:00:
    un resultado que nadie quiere y que además depende de la zona horaria de quien pregunta.

    ## Por qué en UTC y no en hora local

    Porque `created_at` es `timestamptz` y PostgreSQL compara instantes. Un corte en hora local
    daría resultados distintos según desde qué zona se consulte, y «del lunes al martes»
    dejaría de ser una frase que significa lo mismo para todo el mundo.

    ## Por qué un rango invertido devuelve vacío y no un `422`

    Porque las dos condiciones son incompatibles por construcción, así que la lista vacía ya es
    la respuesta que corresponde. Un error de validación obligaría al panel a manejar un estado
    que nunca se da.
    """

    condiciones: list[ColumnElement[bool]] = []
    if desde is not None:
        condiciones.append(VerifiedDomain.created_at >= _medianoche_utc(desde))
    if hasta is not None:
        condiciones.append(
            VerifiedDomain.created_at < _medianoche_utc(hasta) + timedelta(days=1)
        )
    return condiciones


def _medianoche_utc(dia: date) -> datetime:
    """Medianoche UTC del día pedido. Ver la nota de `_rango_de_creacion`."""

    return datetime(dia.year, dia.month, dia.day, tzinfo=UTC)


class DomainNotFoundError(LookupError):
    """El dominio no existe **en este workspace**.

    Un solo tipo para "no existe" y "es de otro workspace": el cliente no puede
    distinguirlos, y esa indistinguibilidad es la garantía de que la respuesta no confirma
    la existencia de nada ajeno.
    """


class DomainConflictError(AssetError):
    """El dominio ya está reclamado, y por qué.

    ## Por qué el motivo viaja en la excepción y no solo en el texto

    Porque el panel tiene que ofrecer una acción distinta según el caso: si el dominio ya
    está en este workspace, el botón correcto es "ir al dominio"; si lo tiene otro, no hay
    ninguna acción y solo un mensaje. Con un solo `detail` en el `409` el panel tendría que
    interpretar la frase en español para decidir, lo que además rompe en inglés.

    `already_verified` sigue viajando porque el caso de otro workspace tiene a su vez dos
    variantes que el usuario distingue —reclamo pendiente o dominio ya verificado— y que
    no se leen del texto.
    """

    def __init__(
        self,
        message: str,
        *,
        motivo: DomainClaimConflict,
        already_verified: bool = False,
    ) -> None:
        super().__init__(message)
        self.motivo = motivo
        self.already_verified = already_verified


def generate_verification_token() -> str:
    """Un token de verificación nuevo.

    Se usa `secrets` y no `random`: un token predecible es la diferencia entre una prueba de
    propiedad y un trámite, porque el atacante que puede adivinarlo publica el TXT él mismo y
    pasa la verificación.
    """

    return secrets.token_hex(VERIFICATION_TOKEN_BYTES)


async def _dominio_ya_registrado(
    session: AsyncSession, domain_name: str, organization_id: uuid.UUID
) -> tuple[DomainClaimConflict, VerifiedDomain] | None:
    """El mismo nombre en algún workspace, con el motivo del conflicto.

    Busca **globalmente** y luego compara, en vez de filtrar por tenant. Al revés sería más
    rápido, pero no respondería a la pregunta: la colisión solo importa si es de **otro**
    workspace, y filtrar por el tenant activo la haría invisible.

    Devuelve el propio workspace como un caso más y no como "no hay conflicto" a propósito.
    Es lo que permite que el `409` diga la verdad: si el dominio ya está en esta
    organización, decirle al usuario que pertenece a otra lo manda a buscar un workspace que
    no existe.
    """

    dominio = (
        await session.execute(
            select(VerifiedDomain)
            .where(VerifiedDomain.domain_name == domain_name)
            .limit(1)
        )
    ).scalar_one_or_none()
    if dominio is None:
        return None
    if dominio.organization_id == organization_id:
        return (DomainClaimConflict.ESTE_WORKSPACE, dominio)
    return (DomainClaimConflict.OTRO_WORKSPACE, dominio)


async def create_domain(
    session: AsyncSession, organization_id: uuid.UUID, payload: DomainCreate
) -> VerifiedDomain:
    """Da de alta un dominio para el workspace y genera su token de verificación.

    ## Por qué el conflicto se comprueba **antes** de insertar y no solo con el `UNIQUE`

    Porque la violación de una restricción no es un caso de error normal: obliga a hacer
    `rollback` de la transacción entera y llega tarde, cuando ya se ha enviado el `INSERT`.
    Funciona, pero produce dos efectos que no queremos.

    Uno es el mensaje: el `UNIQUE` no distingue "ya lo tienes tú" de "lo tiene otro", y el
    `409` acabaría diciendo "otra organización" a un usuario que ya lo tenía. El otro es la
    transacción: un `rollback` por un duplicado que se podía detectar con un `SELECT` tira
    abajo trabajo que no tenía por qué tirar.

    El `IntegrityError` **se queda** como backstop, porque hay una carrera que ninguna
    comprobación previa cierra: dos altas simultáneas del mismo nombre pasan las dos la
    comprobación y una se lleva la violación. Quitarla sería confiar en que nunca habrá dos
    peticiones a la vez, que es justo lo que no se puede prometer en un servidor web.
    """

    nombre = normalize_domain(payload.domain_name)
    if not nombre:
        raise AssetError("El dominio no es válido")

    existente = await _dominio_ya_registrado(session, nombre, organization_id)
    if existente is not None:
        motivo, dominio = existente
        raise DomainConflictError(
            (
                "El dominio ya está registrado en tu espacio de trabajo"
                if motivo is DomainClaimConflict.ESTE_WORKSPACE
                else "El dominio ya está registrado por otra organización"
            ),
            motivo=motivo,
            already_verified=dominio.is_verified,
        )

    dominio = VerifiedDomain(
        organization_id=organization_id,
        domain_name=nombre,
        # El token **no** es un hash: se compara con el registro TXT. Ver la nota de
        # `VerifiedDomain.verification_token`.
        verification_token=generate_verification_token(),
        verification_method=payload.verification_method,
    )
    session.add(dominio)
    try:
        await session.commit()
    except IntegrityError as error:
        # La carrera: dos altas simultáneas del mismo nombre. El `UNIQUE` decide, y el
        # perdedor recibe un `409` en vez de una fila duplicada.
        await session.rollback()
        raise DomainConflictError(
            "El dominio ya está registrado por otra organización",
            motivo=DomainClaimConflict.CARRERA,
        ) from error
    return dominio


async def list_domains(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    search: str | None = None,
    verification: DomainVerificationFilter | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
    limit: int = 25,
    offset: int = 0,
) -> DomainListResponse:
    """Los dominios del workspace, verificados primero, con filtros y paginación.

    ## Por qué el filtro por `organization_id` es el primero y no un `if`

    Porque es la condición que R3 no negocia, y un endpoint que solo filtra cuando se le pasa
    otro criterio sería un listado sin filtro devolviendo la superficie de todos los tenants.
    Se pone antes de cualquier otro para que sea la condición que la consulta no puede perder
    de vista, y porque el `organization_id` es la única columna que tiene que estar en la
    consulta de recuento **y** en la de las filas: un `total` que contara de más sería una
    barra de paginación que promete páginas que al pulsarlas salen vacías.

    ## Por qué el recuento de activos viene en la misma consulta

    Hacerlo por fila serían N consultas para pintar una columna, y el `asset_count` es lo que
    permite al panel decir qué parte de la superficie está cubierta sin abrir cada dominio.

    ## Por qué se cuenta aparte y no sobre la consulta agrupada

    Porque la consulta de filas lleva `GROUP BY VerifiedDomain.id` para el recuento de activos,
    y contar sobre ella obligaría a resolver los `JOIN` de activos solo para descartar el
    resultado. `total` es el número de dominios que cumplen los filtros, y eso lo dice
    `verified_domains` con dos condiciones y sin tocar `discovered_assets`.

    ## Por qué el texto busca **solo** en el nombre del dominio

    Porque es lo único que el usuario teclea para encontrar un dominio, y es la única columna de
    texto de la tabla. Las otras dos cosas por las que se recuerda un dominio —el nombre del
    registro TXT y su valor— se derivan de `domain_name` y del token, así que un término que
    coincide con el nombre ya los contiene: buscarlos aparte solo añadiría una condición al
    `WHERE` para devolver las mismas filas.

    ## Por qué `verified_at` no se puede usar como rango

    Porque es `NULL` mientras el dominio no está verificado, así que un filtro sobre ella no
    recorta filas: las **borra**. En cuanto se tocara cualquiera de las dos fechas, todos los
    dominios pendientes desaparecerían de la tabla, que es justo lo que el usuario quiere ver
    cuando busca «¿cuáles me quedan por publicar?». `created_at` no es `NULL` nunca.

    ## Por qué el orden termina en `id`

    Porque `is_verified` y `created_at` no son únicos: dos dominios verificados dados en el
    mismo milisegundo —dos altas seguidas del mismo script— se ordenan de forma arbitraria, y
    con paginación por `offset` eso significa que la fila 26 puede aparecer dos veces o no
    aparecer al pasar a la siguiente. Añadir `id` —que es único— hace el orden total y la
    paginación estable. La misma razón por la que el listado de revisiones de PR ordena por
    `(created_at, id)`.

    **Esto no está cubierto por una prueba, y se sabe.** Se intentó: se quitó el `id` del
    `ORDER BY` y la prueba de orden estable siguió en verde, porque con pocas filas PostgreSQL
    lee por el índice de la clave primaria y sale en orden de `id` de todas formas. El defecto
    solo se manifiesta cuando el planificador elige otra ruta —muchas filas, otro índice— y
    eso no se puede forzar desde fuera sin una tabla de mil filas y una pista al planificador,
    que es un test que mide el plan de ejecución de PostgreSQL y no este código. Se deja la
    regla escrita aquí en lugar de una prueba que daría verde sin comprobar nada.
    """

    filtros = [VerifiedDomain.organization_id == organization_id]
    # `search` llega con los espacios que el usuario dejó en el campo, y un término que solo
    # sean espacios no es una búsqueda: es un filtro vacío. Se descarta para que el botón de
    # limpiar pueda comparar contra el mismo criterio que la consulta aplicó.
    if termino := (search or "").strip():
        filtros.append(
            func.lower(VerifiedDomain.domain_name).like(
                f"%{_escape_like(termino.lower())}%", escape="\\"
            )
        )
    if verification is not None:
        filtros.append(
            VerifiedDomain.is_verified.is_(
                verification is DomainVerificationFilter.VERIFIED
            )
        )
    filtros.extend(_rango_de_creacion(created_from, created_to))

    total = int(
        (
            await session.execute(
                select(func.count(VerifiedDomain.id)).where(*filtros)
            )
        ).scalar_one()
    )
    filas = (
        await session.execute(
            select(VerifiedDomain, func.count(DiscoveredAsset.id))
            .outerjoin(DiscoveredAsset, DiscoveredAsset.domain_id == VerifiedDomain.id)
            .where(*filtros)
            .group_by(VerifiedDomain.id)
            .order_by(
                VerifiedDomain.is_verified.desc(),
                VerifiedDomain.created_at.desc(),
                VerifiedDomain.id,
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return DomainListResponse(
        items=[build_domain_item(dominio, int(cuenta)) for dominio, cuenta in filas],
        total=total,
        limit=limit,
        offset=offset,
    )


def build_domain_item(dominio: VerifiedDomain, asset_count: int) -> DomainItem:
    """Construye la respuesta de un dominio con su recuento de activos.

    Es **pública** y no un detalle interno porque la ruta de alta la necesita: el `POST`
    devuelve un único dominio recién creado, y para darle formato hace falta la misma
    construcción que usa el listado. Reimplementarla en la ruta sería el sitio donde el
    `txt_record_value` dejaría de construirse con `build_txt_record` y empezaría a
    concatenar el prefijo a mano, que es la divergencia exacta que el esquema evita.
    """

    return DomainItem(
        id=dominio.id,
        domain_name=dominio.domain_name,
        is_verified=dominio.is_verified,
        verified_at=dominio.verified_at,
        verification_method=dominio.verification_method,
        # El nombre sale completo y con el dominio del propio registro, no como una
        # etiqueta suelta: lo que el panel muestra es exactamente lo que el servidor
        # consulta, y si el panel compusiera el nombre por su cuenta serían dos sitios que
        # pueden divergir.
        txt_record_name=build_txt_record_name(dominio.domain_name),
        txt_record_value=build_txt_record(dominio.verification_token),
        created_at=dominio.created_at,
        updated_at=dominio.updated_at,
        asset_count=asset_count,
    )


async def get_domain(
    session: AsyncSession, organization_id: uuid.UUID, domain_id: uuid.UUID
) -> VerifiedDomain:
    """El dominio **del workspace activo**, o `DomainNotFoundError`.

    El filtro va en el `WHERE` junto al `id`. Filtrar en Python traería la fila de otro
    workspace a memoria para decidir que no es del usuario: es R3 resuelto tarde y en el
    sitio equivocado.
    """

    dominio = (
        await session.execute(
            select(VerifiedDomain).where(
                VerifiedDomain.id == domain_id,
                VerifiedDomain.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if dominio is None:
        raise DomainNotFoundError("Dominio no encontrado")
    return dominio


async def verify_domain(
    session: AsyncSession, organization_id: uuid.UUID, domain_id: uuid.UUID
) -> tuple[VerifiedDomain, VerificationResult]:
    """Comprueba el TXT del dominio y lo marca verificado si coincide.

    ## Por qué solo se marca verificado y nunca se desmarca

    Porque la verificación responde a "¿este workspace controla este dominio?", y eso es un
    hecho que se establece una vez. Un `PATCH` que lo pusiera a `False` dejaría el workspace
    sin descubrimiento sobre un dominio que sí controla, y nadie benefitaría: la única forma
    de perder la propiedad es que el cliente borre el registro TXT, y en ese caso el
    descubrimiento debería fallar, no el alta de un recerdo.

    Se deja el comentario porque "se puede volver a verificar" y "se puede desverificar" son
    cosas distintas y la segunda no existe a propósito.
    """

    dominio = await get_domain(session, organization_id, domain_id)
    resultado = await verify_domain_txt(dominio.domain_name, dominio.verification_token)

    if resultado.verified and not dominio.is_verified:
        dominio.is_verified = True
        dominio.verified_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(dominio)

    return dominio, resultado


async def delete_domain(
    session: AsyncSession, organization_id: uuid.UUID, domain_id: uuid.UUID
) -> None:
    """Borra un dominio y, en cascada, sus activos.

    Solo se borra lo que **no** está verificado. Un dominio verificado es parte de la
    superficie de ataque registrada del cliente y borrarlo sería hacer desaparecer un
    inventario que ya se pagó, sin dejar rastro. Para quitar un dominio verificado hay que
    dar de baja el workspace, que es una decisión de otro tamaño.
    """

    dominio = await get_domain(session, organization_id, domain_id)
    if dominio.is_verified:
        raise AssetError(
            "No se puede eliminar un dominio verificado: es parte del inventario registrado"
        )
    await session.delete(dominio)
    await session.commit()


async def list_assets(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    domain_id: uuid.UUID | None = None,
    asset_type: AssetTypeEnum | None = None,
    query: str | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> AssetListResponse:
    """Activos del workspace, filtrables por dominio, por tipo, por texto y por fecha de alta.

    El filtro por `organization_id` va siempre, con o sin los otros. Un endpoint que solo
    filtra cuando se le pasa un `domain_id` sería un `GET /assets` sin filtro devolviendo la
    superficie de todos los tenants.

    ## Por qué el texto busca en el valor, el nombre de servicio y el dominio

    Porque un host se recuerda por tres cosas distintas y no hay forma de saber cuál escribió
    quien busca: `api.empresa.com` se busca por el nombre, por `api`, o por el puerto que
    apareció en la columna de tecnologías. Con una sola columna, dos de cada tres búsquedas
    darían página vacía y el buscador parecería roto.

    ## Por qué el rango va sobre `created_at` y no sobre `last_scanned_at`

    Porque `last_scanned_at` es `NULL` en un activo recién descubierto y no se vuelve a escribir
    hasta el siguiente escaneo. Un rango sobre ella **borraría** de la tabla todos los activos
    que aún no se han vuelto a mirar —que son precisamente los recién detectados—, y el
    usuario leería que el descubrimiento no ha guardado nada. `created_at` no puede ser `NULL`
    y es además la columna por la que se ordena el listado, así que el rango recorta filas y la
    paginación las cuenta sobre la misma fecha.

    ## Por qué el `JOIN` con `verified_domains` es **incondicional**

    Porque el texto busca también en `VerifiedDomain.domain_name`, y sin el `JOIN` esa condición
    referencia una tabla que el `COUNT` no trae: PostgreSQL la resuelve como un **producto
    cartesiano** y devuelve cada activo cruzado con **cada** dominio de la base. El resultado es
    un `total` inflado por el número de dominios que haya, y una lista de filas cuyo nombre de
    dominio puede no ser el suyo.

    SQLAlchemy lo avisa (`SAWarning: SELECT statement has a cartesian product between FROM
    element(s) "discovered_assets" and FROM element "verified_domains"`), y el aviso es
    correcto. Aquí no es una fuga entre tenants —el `organization_id` filtra bien— sino un
    recuento y un nombre que salen de cualquier otro sitio, que es la misma clase de defecto
    que se corrigió en `repositories/router.py`.

    El `JOIN` va en las **dos** consultas por eso. En la de filas ya estaba; en la de recuento se
    ha añadido con el filtro de texto, porque es al añadir esa condición cuando la consulta
    empezó a referenciar la tabla unida.
    """

    filtros = [DiscoveredAsset.organization_id == organization_id]
    if domain_id is not None:
        filtros.append(DiscoveredAsset.domain_id == domain_id)
    if asset_type is not None:
        filtros.append(DiscoveredAsset.asset_type == asset_type)
    if termino := (query or "").strip():
        filtros.append(
            coincide(
                [
                    DiscoveredAsset.value,
                    DiscoveredAsset.service_name,
                    VerifiedDomain.domain_name,
                ],
                termino,
            )
        )
    filtros.extend(rango_creado(DiscoveredAsset.created_at, created_from, created_to))

    # El `JOIN` también en el recuento. Sin él, la condición sobre `domain_name` del filtro de
    # texto se resuelve contra una tabla que no está en el `FROM` y el `total` sale multiplicado
    # por el número de dominios de la base. Ver la nota del docstring.
    total = int(
        (
            await session.execute(
                select(func.count(DiscoveredAsset.id))
                .join(VerifiedDomain, VerifiedDomain.id == DiscoveredAsset.domain_id)
                .where(*filtros)
            )
        ).scalar_one()
    )
    filas = (
        (
            await session.execute(
                select(DiscoveredAsset, VerifiedDomain.domain_name)
                .join(VerifiedDomain, VerifiedDomain.id == DiscoveredAsset.domain_id)
                .where(*filtros)
                .order_by(
                    DiscoveredAsset.last_scanned_at.desc().nullslast(),
                    DiscoveredAsset.created_at.desc(),
                    DiscoveredAsset.id,
                )
                .limit(limit)
                .offset(offset)
            )
        )
        .all()
    )
    return AssetListResponse(
        items=[
            AssetItem(
                id=activo.id,
                domain_id=activo.domain_id,
                domain_name=dominio,
                asset_type=activo.asset_type,
                value=activo.value,
                service_name=activo.service_name,
                technologies=list(activo.technologies or []),
                last_scanned_at=activo.last_scanned_at,
                created_at=activo.created_at,
            )
            for activo, dominio in filas
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


async def contar_activos(
    session: AsyncSession, domain_id: uuid.UUID
) -> int:
    """Cuántos activos hay bajo un dominio, para la respuesta de encolado."""

    return int(
        (
            await session.execute(
                select(func.count(DiscoveredAsset.id)).where(
                    DiscoveredAsset.domain_id == domain_id
                )
            )
        ).scalar_one()
    )


async def merge_assets(
    session: AsyncSession,
    domain_id: uuid.UUID,
    organization_id: uuid.UUID,
    encontrados: list[tuple[AssetTypeEnum, str, str | None, list[str]]],
    *,
    max_assets: int | None = None,
) -> int:
    """Inserta o actualiza activos de forma **idempotente**. Devuelve cuántos son nuevos.

    ## Por qué `upsert` y no borrar y volver a insertar

    Porque `created_at` es parte de la información del activo: cuándo apareció por primera vez
    en la superficie del cliente. Borrar y reinsertar pondría todos los activos con la fecha
    de la última ejecución, y el inventario perdería la única señal de qué es nuevo y qué
    lleva años ahí. Con el `upsert`, la fila conserva su `created_at` y solo se mueve
    `last_scanned_at`.

    ## Por qué las tecnologías se **fusionan** y no se reemplazan

    Por lo mismo. Un escaneo posterior que no detecte Cloudflare no significa que Cloudflare
    haya desaparecido: puede que el escaneo no lo haya alcanzado. Reemplazar la lista haría
    que el inventario perdiera información cada vez que un escaneo viniera incompleto, y esa
    es la forma más fácil de que un cliente deje de fiarse de la herramienta.

    La fusión conserva el orden de inserción de lo ya conocido, que es el orden en que se
    descubrió cada tecnología, y añade al final lo nuevo.
    """

    ahora = datetime.now(UTC)
    nuevos = 0
    for tipo, valor, servicio, tecnologias in encontrados[
        : max_assets_per_discovery(max_assets)
    ]:
        existing = (
            await session.execute(
                select(DiscoveredAsset).where(
                    DiscoveredAsset.domain_id == domain_id,
                    DiscoveredAsset.asset_type == tipo,
                    DiscoveredAsset.value == valor,
                )
            )
        ).scalar_one_or_none()

        if existing is None:
            session.add(
                DiscoveredAsset(
                    domain_id=domain_id,
                    organization_id=organization_id,
                    asset_type=tipo,
                    value=valor,
                    service_name=servicio,
                    technologies=_sin_duplicados(tecnologias),
                    last_scanned_at=ahora,
                )
            )
            nuevos += 1
            continue

        fusionadas = _sin_duplicados([*(existing.technologies or []), *tecnologias])
        existing.technologies = fusionadas
        if servicio is not None:
            existing.service_name = servicio
        existing.last_scanned_at = ahora

    await session.commit()
    return nuevos


def _sin_duplicados(valores: list[str]) -> list[str]:
    """Únicos conservando el orden de aparición.

    `dict.fromkeys` es el idioma corto para esto en Python y es más rápido que un `set` con
    lista intermedia. La comparación es exacta y sin normalizar: `Nginx` y `nginx` son
    etiquetas distintas que vendrán de detectores distintos, y unificarlas sería decidir
    qué forma es la buena sin ningún dato que lo diga.
    """

    return [valor for valor in dict.fromkeys(valores) if valor.strip()]


__all__ = [
    "DEFAULT_MAX_ASSETS_PER_DISCOVERY",
    "VERIFICATION_TOKEN_BYTES",
    "AssetError",
    "DnsLookupOutcome",
    "DomainClaimConflict",
    "DomainConflictError",
    "DomainNotFoundError",
    "build_domain_item",
    "contar_activos",
    "create_domain",
    "delete_domain",
    "generate_verification_token",
    "get_domain",
    "list_assets",
    "list_domains",
    "max_assets_per_discovery",
    "merge_assets",
    "verify_domain",
]
