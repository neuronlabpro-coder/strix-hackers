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
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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
    session: AsyncSession, organization_id: uuid.UUID
) -> list[DomainItem]:
    """Los dominios del workspace, verificados primero.

    El recuento de activos viene en la misma consulta. Hacerlo por fila serían N consultas
    para pintar una columna, y el `asset_count` es lo que permite al panel decir qué parte de
    la superficie está cubierta sin abrir cada dominio.
    """

    filas = (
        await session.execute(
            select(VerifiedDomain, func.count(DiscoveredAsset.id))
            .outerjoin(DiscoveredAsset, DiscoveredAsset.domain_id == VerifiedDomain.id)
            .where(VerifiedDomain.organization_id == organization_id)
            .group_by(VerifiedDomain.id)
            .order_by(VerifiedDomain.is_verified.desc(), VerifiedDomain.created_at.desc())
        )
    ).all()
    return [build_domain_item(dominio, int(cuenta)) for dominio, cuenta in filas]


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
    limit: int = 50,
    offset: int = 0,
) -> AssetListResponse:
    """Activos del workspace, filtrables por dominio y por tipo.

    El filtro por `organization_id` va siempre, con o sin los otros. Un endpoint que solo
    filtra cuando se le pasa un `domain_id` sería un `GET /assets` sin filtro devolviendo la
    superficie de todos los tenants.
    """

    filtros = [DiscoveredAsset.organization_id == organization_id]
    if domain_id is not None:
        filtros.append(DiscoveredAsset.domain_id == domain_id)
    if asset_type is not None:
        filtros.append(DiscoveredAsset.asset_type == asset_type)

    total = int(
        (
            await session.execute(
                select(func.count(DiscoveredAsset.id)).where(*filtros)
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
