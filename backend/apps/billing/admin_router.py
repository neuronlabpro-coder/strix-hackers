"""Consola de precios de plataforma: lectura, edición y traza de cambios.

## Por qué un router aparte y no dentro de `admin/router.py`

Porque precios no pertenece a ninguna organización y `admin/router.py` asume que todo lo que
administra cuelga de un tenant. Un módulo con su propio prefijo deja esa diferencia explícita
en la estructura de ficheros en vez de escondida en un `if` de dentro de un handler.

Y porque el orden de `include_router` en `main.py` decide qué ruta gana, igual que ya pasa con
`repositories_webhooks` y `api_access_mcp`: este router se registra **antes** que el de
administración, así que `/api/v1/admin/pricing` lo encuentra aquí sin colisionar con el
`/api/v1/admin/{algo}` que pueda existir más abajo.

## Por qué no hay ningún `DELETE` en este módulo

Por dos razones que apuntan en la misma dirección.

La primera es R4: un precio que se puede borrar no es un precio que se pueda auditar, y un
histórico donde faltan los huecos no sirve para reconstruir por qué un cobro salió como salió.
La segunda es más práctica: un borrado accidental desde el panel no se distingue de una
decisión, y un precio que desaparece deja a la plataforma cobrando con el catálogo del código
sin que nadie lo note. Por eso los packs y los tramos se **desactivan** con `is_active` en vez
de borrarse: la fila se queda, deja de ofrecerse, y su historial sigue siendo legible.

## Por qué cada cambio pide un motivo

Porque `clave` no puede distinguir dos cosas que se parecen mucho: una corrección de un error y
una subida de precio deliberada. Las dos escriben `scan_credit_cost`. Dentro de seis meses, sin
el motivo, no hay forma de saber cuál fue cuál, y esa es exactamente la pregunta que hace un
SOC 2 sobre una subida de tarifa.

## Por qué el motivo es obligatorio y no opcional

Porque un motivo opcional es un motivo ausente. Es el mismo argumento que hace que el
diagnóstico de una caída de producción sea obligatorio: un campo que el sistema no exige
acaba vacío la mayoría de las veces, y entonces no aporta nada.

## Por qué los cambios se aplican a la instantánea del proceso

Porque leer el precio de la base en cada cobro no es una opción: el worker de Strix cobra desde
una tarea de Celery sin sesión, y dos servicios más cobran desde webhooks. La alternativa
—enhebrar la sesión por esos caminos— obliga a cambiar la firma de cuatro funciones y a
repartir la regla de precio por medio dozen de ficheros, que es el defecto que todo esto vino a
arreglar.

El límite de este diseño, escrito aquí y no escondido: en un despliegue con **varios**
procesos, cambiar un precio solo lo ve al instante el proceso que atendió la escritura. Los
demás lo verán cuando se reinicien. Es el precio de no meter sesiones en el camino de cobro,
y se acepta porque el precio de un trabajo ya está congelado en su asiento del ledger desde que
se encoló: un trabajo en vuelo nunca se reliquida al precio nuevo.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.apps.admin.dependencies import SuperuserDependency
from backend.apps.billing.catalogo import cargar_catalogo
from backend.apps.billing.models import CreditPack, PlatformPriceChange, PlatformPricing, VolumeTier
from backend.apps.billing.pricing import cargar_precios
from backend.core.database import AsyncSession
from backend.core.middleware import SessionDependency

router = APIRouter(prefix="/api/v1/admin/pricing", tags=["admin-pricing"])

# --------------------------------------------------------------------------- #
# Esquemas
# --------------------------------------------------------------------------- #


class PlatformPricingResponse(BaseModel):
    """La fila única de precios, tal como está ahora mismo.

    ## Por qué los importes viajan como cadena y no como número

    Porque es un `Decimal` de SQLAlchemy con ocho decimales, y un número JSON es un binario en
    coma flotante donde `0.1` no es exactamente `0.1`. El frontend los pinta, nunca los suma.
    """

    model_config = ConfigDict(from_attributes=True)

    credits_per_usd: Decimal
    scan_credit_cost: Decimal
    quick_scan_credit_multiplier: Decimal
    low_credit_balance_threshold: Decimal
    custom_spend_minimum_usd: Decimal
    custom_spend_maximum_usd: Decimal
    pro_subscription_monthly_usd: Decimal
    updated_at: datetime | None = None


class PlatformPricingUpdate(BaseModel):
    """Cambios de los precios escalares. Todos opcionales: se envía solo lo que cambia.

    ## Por qué `None` significa «no cambiar» y no «dejar en blanco»

    Porque la fila se actualiza con un `setattr` por campo, y `None` es lo que el `exclude_none`
    del router descarta. Enviar `null` para poner un precio en blanco sería una petición sin
    sentido que el esquema tiene que rechazar, y ese rechazo es un `422` con el nombre del campo.
    """

    model_config = ConfigDict(extra="forbid")

    credits_per_usd: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=8)
    scan_credit_cost: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=8)
    quick_scan_credit_multiplier: Decimal | None = Field(
        default=None, gt=0, le=1, max_digits=18, decimal_places=8
    )
    low_credit_balance_threshold: Decimal | None = Field(
        default=None, ge=0, max_digits=18, decimal_places=8
    )
    custom_spend_minimum_usd: Decimal | None = Field(
        default=None, gt=0, max_digits=18, decimal_places=2
    )
    custom_spend_maximum_usd: Decimal | None = Field(
        default=None, gt=0, max_digits=18, decimal_places=2
    )
    pro_subscription_monthly_usd: Decimal | None = Field(
        default=None, gt=0, max_digits=18, decimal_places=2
    )
    motivo: str = Field(min_length=3, max_length=255)

    @model_validator(mode="after")
    def comprobar_que_algo_cambia(self) -> PlatformPricingUpdate:
        cambios = self.model_dump(exclude_none=True, exclude={"motivo"})
        if not cambios:
            raise ValueError("Envía al menos un precio para modificar")
        if (
            self.custom_spend_minimum_usd is not None
            and self.custom_spend_maximum_usd is not None
            and self.custom_spend_maximum_usd < self.custom_spend_minimum_usd
        ):
            raise ValueError(
                "El tope de gasto no puede ser menor que el mínimo: "
                "el rango de compra quedaría vacío y no se podría comprar nada"
            )
        return self


class CreditPackWrite(BaseModel):
    """Alta o edición de un pack de créditos."""

    model_config = ConfigDict(extra="forbid")

    credits: int = Field(ge=1, le=10_000_000)
    amount_usd: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    is_active: bool = True
    display_order: int = Field(default=0, ge=0, le=1000)
    motivo: str = Field(min_length=3, max_length=255)


class CreditPackPatch(BaseModel):
    """Edición parcial de un pack. La cantidad de créditos **no** se cambia.

    ## Por qué `credits` no es editable

    Porque es la identidad del pack en la traza de cambios y en el historial de ventas: un pack
    de 25 créditos que pasa a ser de 30 es un pack distinto, y mutarlo deja las compras
    anteriores apuntando a un producto que ya no existe. Para cambiarlo se desactiva el viejo y
    se crea el nuevo, y las dos filas quedan con su historial.
    """

    model_config = ConfigDict(extra="forbid")

    amount_usd: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=2)
    is_active: bool | None = None
    display_order: int | None = Field(default=None, ge=0, le=1000)
    motivo: str = Field(min_length=3, max_length=255)

    @model_validator(mode="after")
    def comprobar_que_algo_cambia(self) -> CreditPackPatch:
        if not any(
            valor is not None for valor in (self.amount_usd, self.is_active, self.display_order)
        ):
            raise ValueError("Envía al menos un campo para modificar")
        return self


class VolumeTierWrite(BaseModel):
    """Alta de un tramo de la escalera de descuento.

    ## Por qué el descuento es una fracción y no un porcentaje

    Porque el cálculo lo multiplica: el precio de un crédito es `(1 - descuento) / paridad`. Un
    porcentaje obligaría a dividir por cien en cada uno de esos sitios, y un solo olvidarse
    cambia el precio de un cliente sin que nada falle. La fracción hace la operación y la
    inversión en la misma unidad, y el `le=1` lo deja explícito.
    """

    model_config = ConfigDict(extra="forbid")

    spend_min_usd: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    discount: Decimal = Field(ge=0, le=1, max_digits=9, decimal_places=6)
    is_active: bool = True
    display_order: int = Field(default=0, ge=0, le=1000)
    motivo: str = Field(min_length=3, max_length=255)


class VolumeTierPatch(BaseModel):
    """Edición parcial de un tramo. El umbral **no** se cambia.

    ## Por qué el umbral no es editable y el descuento sí

    Porque el umbral es el que define a qué gasto empieza el tramo, y moverlo sin mover sus
    vecinos puede dejar dos tramos solapados o un hueco entre ellos. Cambiar el descuento es
    cambiar la política del tramo sin tocar el rango, y es la edición que se hace cada seis
    meses. Para mover un umbral se desactiva el tramo y se crea otro.
    """

    model_config = ConfigDict(extra="forbid")

    discount: Decimal | None = Field(
        default=None, ge=0, le=1, max_digits=9, decimal_places=6
    )
    is_active: bool | None = None
    display_order: int | None = Field(default=None, ge=0, le=1000)
    motivo: str = Field(min_length=3, max_length=255)

    @model_validator(mode="after")
    def comprobar_que_algo_cambia(self) -> VolumeTierPatch:
        if not any(
            valor is not None for valor in (self.discount, self.is_active, self.display_order)
        ):
            raise ValueError("Envía al menos un campo para modificar")
        return self


class CreditPackResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    credits: int
    amount_usd: Decimal
    is_active: bool
    display_order: int


class VolumeTierResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    spend_min_usd: Decimal
    discount: Decimal
    is_active: bool
    display_order: int


class PlatformPricingDetail(BaseModel):
    """Todo el catálogo de precios, para la pantalla de administración en una sola lectura.

    ## Por qué todo en una respuesta y no en cuatro

    Porque la pantalla edita las cuatro cosas y necesita saber los límites **actuales** antes de
    dejar escribir: si el slider de compra va de 10 a 13.333 créditos, el formulario del panel
    tiene que conocer ese rango o no puede avisar de que un precio nuevo dejaría fuera de
    catálogo lo que hoy está dentro. Cuatro lecturas son cuatro estados posibles de la misma
    pantalla, y con dos de ellas desfasadas los avisos mienten.
    """

    precios: PlatformPricingResponse
    packs: list[CreditPackResponse]
    tiers: list[VolumeTierResponse]
    credits_per_usd: Decimal
    minimo_creditos_comerciales: int
    maximo_creditos_comerciales: int
    #: `true` cuando los precios vienen de la base y `false` cuando vienen del código.
    #: El panel lo muestra, porque un despliegue a medio hacer cambia lo que se cobra sin que
    #: lo parezca en ninguna otra parte.
    desde_la_base: bool


class PriceChangeEntry(BaseModel):
    """Un asiento del histórico de precios. De solo lectura, por R4.

    ## Por qué `from_attributes`

    Porque lo que llega es una fila de SQLAlchemy, no un diccionario. Sin `from_attributes`,
    Pydantic busca los campos como atributos de un modelo con `dict()` y falla con un error que
    no menciona la fila ni el campo: el síntoma es un `ValidationError` de una línea que no
    señala dónde está el problema.
    """

    model_config = ConfigDict(from_attributes=True)

    clave: str
    valor_anterior: Decimal | None
    valor_nuevo: Decimal | None
    actor_user_id: uuid.UUID | None
    motivo: str | None
    changed_at: datetime


class PriceChangePage(BaseModel):
    items: list[PriceChangeEntry]
    total: int
    limit: int
    offset: int


# --------------------------------------------------------------------------- #
# Servicio
# --------------------------------------------------------------------------- #


async def _registrar_cambio(
    session: AsyncSession,
    *,
    clave: str,
    anterior: Decimal | None,
    nuevo: Decimal,
    actor_id: uuid.UUID | None,
    motivo: str,
) -> None:
    """Deja un asiento del cambio, en la **misma** transacción que el `UPDATE`.

    ## Por qué el mismo `session` y no una sesión aparte

    Porque si el asiento se escribiera después, y la escritura fallara, el precio quedaría
    cambiado sin rastro — que es exactamente el estado que R4 prohíbe. Con la misma sesión, el
    `commit` es único: o se ve el precio nuevo y su asiento, o no se ve ninguno.
    """

    session.add(
        PlatformPriceChange(
            clave=clave,
            valor_anterior=anterior,
            valor_nuevo=nuevo,
            actor_user_id=actor_id,
            motivo=motivo,
        )
    )


async def _refrescar_instantaneas(session: AsyncSession) -> None:
    """Vuelve a leer precios y catálogo para que el cambio aplique sin reiniciar.

    Se hace en la misma transacción y **después** del `commit`, no antes: si se hiciera antes, la
    instantánea describiría un estado que todavía no está confirmado en la base, y un `rollback`
    posterior dejaría el proceso cobrando con un precio que nadie guardó.
    """

    await cargar_precios(session)
    await cargar_catalogo(session)


async def _aplicar_a_la_fila(
    session: AsyncSession,
    fila: PlatformPricing,
    cambios: dict[str, Decimal],
    actor_id: uuid.UUID | None,
    motivo: str,
) -> None:
    """Aplica los cambios a la fila única y deja un asiento por cada uno.

    Cada campo por separado a propósito: un cambio de tres precios deja **tres** asientos, no
    uno con tres números. Un asiento por cambio es lo que hace que «quién subió esto y
    cuándo» se pueda responder con una consulta y no con un diff de dos semanas de entradas.
    """

    for campo, nuevo in cambios.items():
        await _registrar_cambio(
            session,
            clave=campo,
            anterior=Decimal(getattr(fila, campo)),
            nuevo=nuevo,
            actor_id=actor_id,
            motivo=motivo,
        )
        setattr(fila, campo, nuevo)
    fila.updated_by = actor_id


# --------------------------------------------------------------------------- #
# Rutas
# --------------------------------------------------------------------------- #


async def _leer_fila(session: AsyncSession) -> PlatformPricing:
    """La fila única de precios, o `404` si alguien la borró.

    ## Por qué `404` y no un precio de respuesta vacío

    Porque no borrarse está protegido por un disparador, así que un `404` aquí significa que
    alguien lo desactivó desde fuera de la aplicación —una corrección manual, un script de
    soporte— y eso hay que saberlo. Devolver un objeto con valores inventados dejaría al
    operador creyendo que está viendo el catálogo real.
    """

    fila = (
        await session.execute(select(PlatformPricing).where(PlatformPricing.id == 1))
    ).scalar_one_or_none()
    if fila is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "La tabla platform_pricing no tiene fila. "
                "Aplicale la migracion f4b5c6d7e8a9 "
                "o restaura la fila desde el catalogo del codigo."
            ),
        )
    return fila


@router.get("", response_model=PlatformPricingDetail)
async def read_pricing(
    _superuser: SuperuserDependency, session: SessionDependency
) -> PlatformPricingDetail:
    """Lee el catálogo completo de precios en una sola respuesta."""

    fila = await _leer_fila(session)
    packs = (
        await session.execute(
            select(CreditPack).order_by(CreditPack.display_order, CreditPack.credits)
        )
    ).scalars().all()
    tiers = (
        await session.execute(select(VolumeTier).order_by(VolumeTier.spend_min_usd))
    ).scalars().all()

    from backend.apps.billing.catalogo import catalogo_vigente
    from backend.apps.billing.schemas import credits_for_spend

    catalogo = catalogo_vigente()
    return PlatformPricingDetail(
        precios=PlatformPricingResponse.model_validate(fila),
        packs=[CreditPackResponse.model_validate(p) for p in packs],
        tiers=[VolumeTierResponse.model_validate(t) for t in tiers],
        credits_per_usd=fila.credits_per_usd,
        minimo_creditos_comerciales=credits_for_spend(catalogo.gasto_minimo_usd),
        maximo_creditos_comerciales=credits_for_spend(catalogo.gasto_maximo_usd),
        desde_la_base=True,
    )


@router.patch("", response_model=PlatformPricingResponse)
async def update_pricing(
    payload: PlatformPricingUpdate,
    _superuser: SuperuserDependency,
    session: SessionDependency,
) -> PlatformPricingResponse:
    """Edita los precios escalares y comerciales de la fila única."""

    fila = await _leer_fila(session)
    cambios = payload.model_dump(exclude_none=True, exclude={"motivo"})
    await _aplicar_a_la_fila(session, fila, cambios, _superuser.id, payload.motivo)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "La base rechazo el precio: revisa que el rango de compra no quede vacio "
                "(el tope no puede ser menor que el minimo) "
                "y que los descuentos no superen el 100 %"
            ),
        ) from exc
    await session.refresh(fila)
    await _refrescar_instantaneas(session)
    return PlatformPricingResponse.model_validate(fila)


@router.post("/packs", response_model=CreditPackResponse, status_code=status.HTTP_201_CREATED)
async def create_credit_pack(
    payload: CreditPackWrite,
    _superuser: SuperuserDependency,
    session: SessionDependency,
) -> CreditPackResponse:
    """Crea un pack de créditos y deja asiento de su precio."""

    pack = CreditPack(
        credits=payload.credits,
        amount_usd=payload.amount_usd,
        is_active=payload.is_active,
        display_order=payload.display_order,
    )
    session.add(pack)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ya existe un pack de {payload.credits} créditos",
        ) from exc
    await _registrar_cambio(
        session,
        clave=f"credit_pack:{pack.id}",
        anterior=None,
        nuevo=payload.amount_usd,
        actor_id=_superuser.id,
        motivo=payload.motivo,
    )
    await session.commit()
    await session.refresh(pack)
    await _refrescar_instantaneas(session)
    return CreditPackResponse.model_validate(pack)


@router.patch("/packs/{pack_id}", response_model=CreditPackResponse)
async def update_credit_pack(
    pack_id: uuid.UUID,
    payload: CreditPackPatch,
    _superuser: SuperuserDependency,
    session: SessionDependency,
) -> CreditPackResponse:
    """Edita un pack, o lo desactiva. No lo borra."""

    pack = (
        await session.execute(select(CreditPack).where(CreditPack.id == pack_id))
    ).scalar_one_or_none()
    if pack is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pack no encontrado")

    if payload.amount_usd is not None:
        await _registrar_cambio(
            session,
            clave=f"credit_pack:{pack.id}",
            anterior=pack.amount_usd,
            nuevo=payload.amount_usd,
            actor_id=_superuser.id,
            motivo=payload.motivo,
        )
        pack.amount_usd = payload.amount_usd
    if payload.is_active is not None:
        pack.is_active = payload.is_active
    if payload.display_order is not None:
        pack.display_order = payload.display_order
    await session.commit()
    await session.refresh(pack)
    await _refrescar_instantaneas(session)
    return CreditPackResponse.model_validate(pack)


@router.post("/tiers", response_model=VolumeTierResponse, status_code=status.HTTP_201_CREATED)
async def create_volume_tier(
    payload: VolumeTierWrite,
    _superuser: SuperuserDependency,
    session: SessionDependency,
) -> VolumeTierResponse:
    """Crea un tramo de la escalera de descuento y deja asiento de su valor."""

    tramo = VolumeTier(
        spend_min_usd=payload.spend_min_usd,
        discount=payload.discount,
        is_active=payload.is_active,
        display_order=payload.display_order,
    )
    session.add(tramo)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ya existe un tramo que empieza en {payload.spend_min_usd}",
        ) from exc
    await _registrar_cambio(
        session,
        clave=f"volume_tier:{tramo.id}",
        anterior=None,
        nuevo=payload.discount,
        actor_id=_superuser.id,
        motivo=payload.motivo,
    )
    await session.commit()
    await session.refresh(tramo)
    await _refrescar_instantaneas(session)
    return VolumeTierResponse.model_validate(tramo)


@router.patch("/tiers/{tier_id}", response_model=VolumeTierResponse)
async def update_volume_tier(
    tier_id: uuid.UUID,
    payload: VolumeTierPatch,
    _superuser: SuperuserDependency,
    session: SessionDependency,
) -> VolumeTierResponse:
    """Edita el descuento de un tramo, o lo desactiva. No lo borra."""

    tramo = (
        await session.execute(select(VolumeTier).where(VolumeTier.id == tier_id))
    ).scalar_one_or_none()
    if tramo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tramo no encontrado")

    if payload.discount is not None:
        await _registrar_cambio(
            session,
            clave=f"volume_tier:{tramo.id}",
            anterior=tramo.discount,
            nuevo=payload.discount,
            actor_id=_superuser.id,
            motivo=payload.motivo,
        )
        tramo.discount = payload.discount
    if payload.is_active is not None:
        tramo.is_active = payload.is_active
    if payload.display_order is not None:
        tramo.display_order = payload.display_order
    await session.commit()
    await session.refresh(tramo)
    await _refrescar_instantaneas(session)
    return VolumeTierResponse.model_validate(tramo)


@router.get("/changes", response_model=PriceChangePage)
async def read_price_changes(
    _superuser: SuperuserDependency,
    session: SessionDependency,
    clave: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PriceChangePage:
    """El histórico de cambios de precios, del más reciente al más antiguo.

    ## Por qué solo se lee y no se corrige

    Por R4, igual que el ledger y las evidencias: la traza de lo que rigió un cobro es
    evidencia. Un «corregir» sobre un asiento de precio sería inventar un pasado, y el precio de
    ese mentira lo paga un cliente al que se leExplain luego por qué se le cobró eso.
    """

    from sqlalchemy import func

    base = select(PlatformPriceChange).order_by(PlatformPriceChange.changed_at.desc())
    conteo = select(func.count()).select_from(PlatformPriceChange)
    if clave is not None:
        base = base.where(PlatformPriceChange.clave == clave)
        conteo = conteo.where(PlatformPriceChange.clave == clave)
    total = (await session.execute(conteo)).scalar_one()
    filas = (await session.execute(base.limit(limit).offset(offset))).scalars().all()
    return PriceChangePage(
        items=[PriceChangeEntry.model_validate(f) for f in filas],
        total=total,
        limit=limit,
        offset=offset,
    )
