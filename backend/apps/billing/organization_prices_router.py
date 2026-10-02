"""Precios pactados con cada organización, desde su ficha.

## Por qué esto vive en `billing` y no en `admin`

Porque un precio pactado es un precio, y el sitio donde viven los precios es `billing`. La
consola es la puerta, no el sitio de la regla. `admin_router` de este mismo paquete tiene el CRUD
del catálogo global; este tiene el de la negociación, y los dos comparten la tabla de precios de
plataforma por una razón: **un override no es un precio nuevo, es una excepción a uno existente**.

## Por qué el precio se asigna **después** del alta y no durante

Porque una organización nace sin saber cuánto va a gastar, y meterla con medio precio puesto
produce un estado que no existe en el negocio: nadie sabe si those números son un acuerdo o un
valor de arranque. Pactar después deja el alta limpia —el cliente ve el precio de plataforma, que
es el correcto mientras no haya acuerdo— y el pactado es un acto explícito con fecha, motivo y
autor.

## Por qué un override **caduca** y no se borra nunca

Porque un precio pactado es un **compromiso**, y el histórico de compromisos es lo que permite
explicar, dentro de un año, por qué un cliente pagó lo que pagó. Cuando termina el acuerdo se
escribe el siguiente con `valido_desde` en el futuro y el anterior se queda. El panel no tiene
«borrar» a propósito: la baja de un pactado es su sustitución.

## Por qué refrescar la caché en la misma petición que la escribe

Porque el operador que pacta un precio necesita que el **siguiente** escaneo de ese cliente lo
cobre, y si la caché se refrescara por temporizador habría una ventana —de hasta un reinicio—
en la que el panel dice una cifra y el cobro dice otra. Refrescar en la misma petición lo hace
inmediato para quien escribe; los procesos que no son este siguen viendo el valor anterior hasta
que se refresquen, que es el límite documentado del diseño de caché y no un descuido.

## Por qué se escribe en la traza de la plataforma

Porque un precio que se pacta y no queda registrado es un precio que nadie puede justificar. Y
`platform_price_changes` ya es append-only con su disparador, así que reutilizarla evita una
segunda traza con reglas distintas —que es como se termina sin poder demostrar nada— y obliga a
que la clave diga **de quién** es el override.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from backend.apps.admin.dependencies import SuperuserDependency
from backend.apps.billing.admin_router import PlatformPricingResponse
from backend.apps.billing.models import (
    CreditPack,
    OrganizationPriceOverride,
    PlatformPriceChange,
    PlatformPricing,
    PriceOperationEnum,
)
from backend.apps.billing.organization_prices import (
    cargar_overrides,
    precios_de,
)
from backend.apps.organizations.models import Organization
from backend.core.database import AsyncSession
from backend.core.middleware import SessionDependency

router = APIRouter(prefix="/api/v1/admin/organizations", tags=["admin-organizations-prices"])

#: El motivo con el que se registra en la traza. Distingue un pactado de una corrección de
#: plataforma, y ambos escriben en la misma tabla.
MOTIVO_CONTRATO = "precio pactado con la organizacion"


class OverrideResponse(BaseModel):
    """Un precio pactado, tal como está ahora mismo."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    operacion: PriceOperationEnum
    alcance: str | None
    valor: Decimal
    motivo: str
    valido_desde: datetime
    valido_hasta: datetime | None
    created_by: uuid.UUID | None
    created_at: datetime
    #: Si este es **el** que se cobra ahora.
    #:
    #: ## Por qué lo dice el servidor y no lo deduce el panel
    #:
    #: ## Por qué el panel no puede saberlo
    #:
    #: Porque la tabla admite pactados que coexisten: uno que empieza dentro de un mes y otro que
    #: sigue rigiendo hasta entonces, y ninguno de los dos está «caducado» en el sentido de
    #: `valido_hasta`. Cuál manda depende del reloj, así que un panel que decidiera por su cuenta
    #: tendría que reimplementar la regla —y la reimplementaría bien el lunes y mal el
    #: martes, cuando la regla cambie. Marcándolo en la respuesta, el panel solo pinta.
    #:
    #: Y hay tres estados, no dos: vigente, futuro y caducado. Un `bool` de «caducado» no alcanza,
    #: porque un pactado aún vigente y uno que empieza dentro de un mes se parecerían en la
    #: lista y el operador los leería igual.
    vigente: bool = False


class PactadoResponse(OverrideResponse):
    """Lo que se acaba de pactar, y a quién deja de sustituir.

    ## Por qué el POST devuelve esto y no el pactado a secas

    Porque la operación es «cambiar el precio», y el resultado de cambiarla incluye qué dejaste
    de cobrar. Si la respuesta solo trae el número nuevo, el operador tiene que recargar la
    lista y comparar a ojo cuál se fue, que es justo el momento en el que alguien pacta sin
    saber a quién está pisando.

    Y los tres campos van juntos porque cada uno responde a una pregunta distinta: *quién*
    era, *cuánto* costaba y *por qué*. Con solo el `id` habría que ir a la base a por lo
    demás.
    """

    #: El pactado al que sustituye. `None` es el primer precio de esta operación para esta
    #: organización, que es el caso de un cliente nuevo.
    sustituye_id: uuid.UUID | None = None
    #: El valor que deja de cobrarse. Va aparte del `id` porque es el dato que de verdad quiere
    #: el comercial: no quién, sino **cuánto** deja de aplicarse.
    valor_sustituido: Decimal | None = None
    #: El motivo con el que se pactó el anterior, para reconstruir la cadena desde el panel.
    motivo_sustituido: str | None = None


class OverrideWrite(BaseModel):
    """Un precio a pactar. La organización viene de la ruta: no se pide en el cuerpo.

    ## Por qué la organización **no** va en el cuerpo

    Porque si fuera, un `POST` a `/organizations/A/price-overrides` con `organization_id: B`
    pactaría un precio a la organización equivocada sin que nada lo notara: la ruta dice una cosa
    y el cuerpo otra, y gana el cuerpo. Leyéndola solo de la ruta no hay forma de discrepar.
    """

    model_config = ConfigDict(extra="forbid")

    operacion: PriceOperationEnum
    #: Solo lo usa `CREDIT_PACK_AMOUNT`, y vale la cantidad de créditos del pack.
    alcance: str | None = Field(default=None, max_length=64)
    valor: Decimal
    #: Por qué se pactó. Referencia de la cotización, nombre del cliente, lo que haga falta para
    #: que dentro de un año alguien entienda de dónde salió el número.
    motivo: str = Field(min_length=3, max_length=255)
    #: Cuándo empieza a regir. `None` significa «ya», que es lo que se quiere el 99 % de las
    #: veces; se pone explícito para pactar una subida que empieza en la renovación.
    valido_desde: datetime | None = None
    #: Cuándo deja de regir. `None` significa «hasta que se pacte otro».
    valido_hasta: datetime | None = None

    @model_validator(mode="after")
    def comprobar_coherencia(self) -> OverrideWrite:
        if self.operacion is PriceOperationEnum.CREDIT_PACK_AMOUNT and not self.alcance:
            raise ValueError(
                "Un precio de pack necesita saber de qué pack habla: indique `alcance` con "
                "la cantidad de créditos"
            )
        if self.operacion is not PriceOperationEnum.CREDIT_PACK_AMOUNT and self.alcance:
            raise ValueError("`alcance` solo se usa para el precio de un pack de créditos")
        if self.valor <= 0:
            raise ValueError("Un precio pactado de cero o negativo no es un precio")
        if (
            self.valido_desde is not None
            and self.valido_hasta is not None
            and self.valido_hasta <= self.valido_desde
        ):
            raise ValueError("El pactado tiene que terminar después de empezar")
        return self


class PreciosDeOrganizacionResponse(BaseModel):
    """Los precios que rigen a una organización, y cuáles vienen de un pactado.

    ## Por qué se devuelve **el resultado**, no solo el pactado

    Porque lo que el operador quiere saber es «¿cuánto va a pagar este cliente?», y eso es el
    resultado de aplicar el pactado encima de la plataforma, no el pactado suelto. Mostrar solo
    el pactado obligaría a que el operador calculara mentalmente qué precio de plataforma
    sustituye, que es justo el error que hace que alguien pacte sin saber qué está pactando.
    """

    organization_id: uuid.UUID
    precios: PlatformPricingResponse
    overrides: list[OverrideResponse]
    #: Precio de plataforma sin aplicar ningún pactado, para poder comparar.
    precios_de_plataforma: PlatformPricingResponse


@router.get("/{organization_id}/price-overrides", response_model=PreciosDeOrganizacionResponse)
async def leer_precios_de_organizacion(
    organization_id: uuid.UUID,
    _superuser: SuperuserDependency,
    session: SessionDependency,
) -> PreciosDeOrganizacionResponse:
    """Los precios que rigen a esta organización, y de dónde sale cada uno."""

    await _exigir_organizacion(session, organization_id)

    fila = (
        await session.execute(select(PlatformPricing).where(PlatformPricing.id == 1))
    ).scalar_one_or_none()
    if fila is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="La tabla platform_pricing no tiene fila; no hay precios de los que partir.",
        )

    pactados = list(
        (
            await session.execute(
                select(OrganizationPriceOverride)
                .where(OrganizationPriceOverride.organization_id == organization_id)
                .order_by(OrganizationPriceOverride.valido_desde.desc())
            )
        )
        .scalars()
        .all()
    )
    ids_vigentes = _ids_vigentes(pactados)

    # Se recarga la caché antes de resolver, para que lo que se pinta sea lo que se cobra **en
    # este proceso**, no lo que quedó en memoria de una petición anterior.
    await cargar_overrides(session)
    precios = precios_de(organization_id)

    # Se ensambla desde la **fila**, no desde `PlatformPrices`.
    #
    # ## Por qué no se devuelve el dataclass directamente
    #
    # Porque `PlatformPrices` solo lleva los cuatro escalares de cobro de escaneo, y la respuesta
    # quiere los siete: los tres importes comerciales viven en la fila y en el catálogo, no en ese
    # dataclass. Pasar el dataclass al esquema de respuesta daba un `500` con «field required»
    # en tres campos, y el error no decía nada de qué le faltaba al objeto.
    #
    # Y ensamblar desde la fila es además lo correcto: lo que el operador quiere ver es **el
    # precio que va a cobrar este cliente**, y ese precio es la fila con los pactados encima. Un
    # dataclass escalado no incluiría los tres importes comerciales, que también pueden estar
    # negociados.
    precios_pagados = dict(fila.__dict__)
    for campo in ("scan_credit_cost", "quick_scan_credit_multiplier", "credits_per_usd"):
        precios_pagados[campo] = getattr(precios, campo)

    return PreciosDeOrganizacionResponse(
        organization_id=organization_id,
        precios=PlatformPricingResponse(**precios_pagados),
        overrides=[
            OverrideResponse(
                **{
                    **OverrideResponse.model_validate(o).model_dump(),
                    "vigente": o.id in ids_vigentes,
                }
            )
            for o in pactados
        ],
        precios_de_plataforma=PlatformPricingResponse.model_validate(fila),
    )


@router.post(
    "/{organization_id}/price-overrides",
    response_model=PactadoResponse,
    status_code=status.HTTP_201_CREATED,
)
async def pactar_precio(
    organization_id: uuid.UUID,
    payload: OverrideWrite,
    _superuser: SuperuserDependency,
    session: SessionDependency,
) -> PactadoResponse:
    """Pacta un precio con esta organización.

    ## Por qué esto **no** cierra el pactado anterior

    Porque la tabla es append-only: hay un disparador `BEFORE UPDATE OR DELETE OR TRUNCATE` que
    hace fallar cualquier `UPDATE` con «organization_price_overrides es append-only». La primera
    versión de este endpoint intentaba cerrar el vigente con `valido_hasta = desde` y fallaba
    con un `500` que no decía nada útil, solo el error del servidor de PostgreSQL.

    Y el fallo no era un descuido: era que el modelo era imposible. Un índice único parcial
    sobre `valido_hasta IS NULL` exige un solo abierto por operación, y un append-only impide
    cerrarlo. Juntos, dejaban el primer pactado de un cliente congelado para siempre.

    ## Por qué sustituir es **escribir otro**, no cerrar

    Porque «vigente» significa «el de `valido_desde` más reciente que no haya caducado ni
    empiece en el futuro». Al insertar uno nuevo, el anterior deja de ganar por ser más viejo
    y se queda como histórico, con su motivo y su autor. Ninguna escritura toca ninguna fila
    anterior, así que la inmutabilidad se respeta de verdad y no de palabra.

    ## Por qué ya no hay `409` aquí

    Porque el `409` existía para decir «ya hay un precio vigente, ciérralo primero». Cerrar ya
    no es una operación, así que esa situación no es un error sino **el caso normal**: pactar
    dos veces es renovar. Un `409` obligaría al operador a inventarse un truco para cambiar un
    precio, que es justo la clase de apaño improvisado que después nadie sabe explicar.

    ## Por qué se devuelve qué sustituye a quién

    Porque pactar 4 créditos sobre un pactado de 3 y no enterarse de que el 3 ha dejado de
    mandar es un error caro. El comercial necesita ver, en la misma respuesta, qué valor
    queda fuera de juego; y dentro de un año, al leer la traza, tiene que poder ver la cadena
    entera sin consultar la base.
    """

    await _exigir_organizacion(session, organization_id)

    if payload.operacion is PriceOperationEnum.CREDIT_PACK_AMOUNT:
        await _exigir_pack_existe(session, payload.alcance)

    ahora = datetime.now(UTC)
    desde = payload.valido_desde or ahora

    # Quién deja de regir, si alguien. Solo una lectura: no se toca ninguna fila anterior.
    #
    # ## Por qué el filtro de `valido_desde < desde` y no `!= <=>`
    #
    # Porque si se pacta con fecha **futura**, lo que se está sustituyendo es el precio que
    # regirá hasta entonces — y aún no lo es. Si el filtro exigiera un `valido_desde`
    # anterior, la respuesta diría que no sustituye a nadie, que es verdad literal y mentira
    # útil: el comercial pactó una subida para dentro de un mes sobre un precio que hoy sigue
    # aplicando, y le diríamos que no había precio anterior.
    #
    # Y se toman **todos** los anteriores, no solo el que ganaba, ordenados de forma estable, para
    # que la respuesta sea siempre la misma ante la misma entrada.
    condicion = (
        OrganizationPriceOverride.alcance.is_(None)
        if payload.alcance is None
        else OrganizationPriceOverride.alcance == payload.alcance
    )
    sustitutos = list(
        (
            await session.execute(
                select(OrganizationPriceOverride)
                .where(
                    OrganizationPriceOverride.organization_id == organization_id,
                    OrganizationPriceOverride.operacion == payload.operacion,
                    condicion,
                    OrganizationPriceOverride.valido_desde < desde,
                )
                .order_by(OrganizationPriceOverride.valido_desde.desc())
            )
        )
        .scalars()
        .all()
    )

    override = OrganizationPriceOverride(
        organization_id=organization_id,
        operacion=payload.operacion,
        alcance=payload.alcance,
        valor=payload.valor,
        motivo=payload.motivo,
        valido_desde=desde,
        valido_hasta=payload.valido_hasta,
        created_by=_superuser.id,
    )
    session.add(override)

    # Quién queda fuera de juego, para la respuesta y para la traza.
    #
    # ## Por qué no basta con el más reciente
    #
    # Porque el más reciente puede haber **caducado ya**. Si el acuerdo anterior tenía fecha
    # de fin y esa fecha pasó, ese cliente está pagando el precio de plataforma, y decir que
    # el pactado nuevo «sustituye a 3 créditos» pondría en la traza un número que no era el
    # que se estaba cobrando. Dentro de un año, con la traza delante, esa es una afirmación
    # falsa sobre dinero.
    #
    # Así que se busca el más reciente que **estaba aplicando** en el instante en que empieza
    # el nuevo, y solo si no hay ninguno se cae al más reciente a secas, para que la cadena nunca
    # parezca empezar de la nada.
    anulado: OrganizationPriceOverride | None = None
    for candidato in sustitutos:
        if candidato.valido_hasta is None or candidato.valido_hasta > desde:
            anulado = candidato
            break
    if anulado is None and sustitutos:
        anulado = sustitutos[0]
    #
    # Y `None` sin mas es el primer pactado de esta operación para esta organización. No es un
    # error: es la organización que pagará el precio de plataforma.

    # Si el pactado empieza ya, es el vigente; si empieza en el futuro, no lo es todavía.
    #
    # ## Por qué se puede decidir con la fila que acabamos de insertar
    #
    # Porque todos los `sustitutos` filtrados tienen `valido_desde` **anterior** a este, así que
    # si el nuevo ya ha empezado no hay nadie que se le adelante. Solo habría que consultar de
    # nuevo si dos peticiones entran con la misma `valido_desde` al microsegundo, y en ese caso
    # ambos valen lo mismo así que da igual cuál se marque.
    ya_empieza = override.valido_desde <= datetime.now(UTC)
    sigue_vivo = override.valido_hasta is None or override.valido_hasta > datetime.now(UTC)
    session.add(
        PlatformPriceChange(
            clave=f"override:{organization_id}:{payload.operacion.value}"
            + (f":{payload.alcance}" if payload.alcance else ""),
            valor_anterior=anulado.valor if anulado is not None else None,
            valor_nuevo=payload.valor,
            actor_user_id=_superuser.id,
            motivo=f"{MOTIVO_CONTRATO} — {payload.motivo}",
        )
    )
    await session.commit()
    await session.refresh(override)
    await cargar_overrides(session)

    # Se devuelve a quién sustituye porque pactar sin saber a quién se pisa es el error caro:
    # el comercial vería «4 créditos» y no que el 3 ha dejado de mandarse.
    return PactadoResponse(
        **{
            **OverrideResponse.model_validate(override).model_dump(),
            "vigente": ya_empieza and sigue_vivo,
        },
        sustituye_id=anulado.id if anulado is not None else None,
        valor_sustituido=anulado.valor if anulado is not None else None,
        motivo_sustituido=anulado.motivo if anulado is not None else None,
    )


async def _exigir_organizacion(session: AsyncSession, organization_id: uuid.UUID) -> Organization:
    """La organización, o `404` con un mensaje que dice qué hacer."""

    organizacion = (
        await session.execute(
            select(Organization).where(Organization.id == organization_id)
        )
    ).scalar_one_or_none()
    if organizacion is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Organización no encontrada. "
                "Los precios se pactan sobre una organización existente."
            ),
        )
    return organizacion


async def _exigir_pack_existe(session: AsyncSession, alcance: str | None) -> CreditPack:
    """El pack que se está pactando, o un error que lo diga.

    ## Por qué se comprueba

    Porque un `alcance` que no corresponde a ningún pack pactaría un precio
    **imposible de aplicar**:
    la resolución nunca lo encontraría y el comercial creería que el pack está negociado. Es un
    acuerdo que no existe, y no dar error es la forma más cara de que eso pase.
    """

    try:
        creditos = int(alcance or "")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="`alcance` debe ser la cantidad de créditos del pack, como un número entero",
        ) from exc

    pack = (
        await session.execute(select(CreditPack).where(CreditPack.credits == creditos))
    ).scalar_one_or_none()
    if pack is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"No existe un pack de {creditos} créditos. "
                "Crea el pack primero y luego pacta su precio."
            ),
        )
    return pack


def _ids_vigentes(
    pactados: list[OrganizationPriceOverride],
) -> set[uuid.UUID]:
    """Cuáles de estos pactados se están cobrando ahora mismo.

    ## Por qué esta función y no llamar a `overrides_de`

    Porque `overrides_de` habla **valores** y devuelve un diccionario por operación, y lo que la
    lista del panel necesita son **identificadores**: una fila que ya no manda tiene que verse
    distinta de una que sí, y para eso hay que saber cuál es cuál.

    ## Por qué vuelve a implementar la regla en vez de importarla

    ## Por qué no la importa y la repite

    Porque la regla que decide el vigente es de una línea y media: `ya empezó`, `no ha
    caducado`, y entre los que cumplen, el más reciente. Si se invoca a `overrides_de` para
    saberlo, habría que meter las filas en el formato de la caché y luego deshacerlo para
    mirar un resultado que es un `Decimal`, no un `UUID`.

    Y el riesgo de repetirla no es que se desincronice —esta función y `overrides_de` están a
    unas treinta líneas una de otra, y `test_organization_prices.py` las exercita a las dos con
    la misma cadena de pactados, incluido el caso del futuro—, sino que se olvidara de un caso. Y
    un caso olvidado aquí se ve: el panel pinta «vigente» sobre un precio que no se cobra.
    """

    ahora = datetime.now(UTC)
    ya_empezados: dict[tuple[PriceOperationEnum, str | None], OrganizationPriceOverride] = {}
    for pactado in pactados:
        if pactado.valido_desde > ahora:
            continue
        if pactado.valido_hasta is not None and pactado.valido_hasta <= ahora:
            continue
        clave = (pactado.operacion, pactado.alcance)
        actual = ya_empezados.get(clave)
        if actual is None or pactado.valido_desde > actual.valido_desde:
            ya_empezados[clave] = pactado
    return {p.id for p in ya_empezados.values()}
