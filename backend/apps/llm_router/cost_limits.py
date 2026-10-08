"""Los topes de gasto del proveedor: cuatro niveles, un orden y una explicación.

## El problema que este módulo resuelve

`STRIX_MAX_BUDGET_USD` y `STRIX_MAX_TURNS` vivían **solo** en el `.env`, y el `.env` es un valor
global: el mismo tope para un cliente gratuito y para un Enterprise. Vender «más margen» es
imposible con esa configuración, y prometerlo por escrito sin poder cumplirlo es peor que no
venderlo.

## Los cuatro niveles, y el orden exacto

```
override de organización  >  tipo de operación  >  plan  >  default global
```

Es el orden que declara `PRIORIDAD_DE_NIVELES`, que está **junto al enum** y no en el resolver: el
orden es la regla comercial y no puede estar escrito en dos sitios. El resolver lo recorre, una
prueba lo comprueba y la vista previa de la consola lo enseña: los tres leen el mismo.

## Por qué gana el **primer** nivel con un valor, y no «la fila más específica entera»

Porque un tope puede declarar solo el presupuesto y no los turnos —una organización que quiere más
dinero pero el mismo número de iteraciones— y tratar eso como «no dice nada» obligaría a duplicar
la fila entera para cambiar un solo campo. La resolución es **por campo**: gana el primer nivel que
tenga un valor **para ese campo**.

El precio de esa decisión es que los dos campos pueden venir de niveles distintos, y por eso la
resolución **no** es un par de valores sino un par de valores **con su procedencia**: qué nivel
ganó, qué fila ganó y por qué.

## Por qué la vigencia la decide el reloj y no una tarea

Porque un tope caduca **solo**, en su `valid_until`. No hay tarea que lo retire, y una que lo
hiciera introduciría una ventana en la que el tope caducado seguiría aplicando porque el barrido no
ha pasado todavía. El filtro compara con `ahora` **al resolver**, no al cargar.

Y compara con **las dos** fechas: un tope con `valid_from` en el futuro existe pero todavía no
manda, que es el caso normal de un aviso de renovación. Si el filtro mirara solo `valid_until`, ese
override empezaría a aplicar el día que se escribió —justo cuando el cliente aún está en el tope
anterior— y el comercial creería que ya había avisado.

## Por qué el `.env` es el **último** nivel y no una constante

Porque el valor por defecto global **tiene que ser configurable por despliegue** (R1) y **tiene que
quedar registrado en la resolución**. «No había nada configurado y se usó el del `.env`» es una
respuesta que el operador de soporte necesita ver. Si el default fuera una constante escrita en el
código, esa respuesta no tendría dónde contestarse y el 3,00 volvería a ser un número mágico con
cuatro sitios de trabajo.

## Por qué este módulo no cobra ni devuelve nada

Porque resolver un tope no es una operación de negocio: es una **lectura de política**. Cobrar,
devolver o ajustar aquí metería el dinero de un escaneo en un sitio donde se cobra de verdad, que es
`credit_ledger`. Todo lo de resolver lee; la única escritura es la de la propia tabla de políticas,
desde la consola de SuperAdmin.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from backend.apps.commercial.models import PentestProduct
from backend.apps.commercial.service import effective_agreement
from backend.apps.llm_router.models import (
    PRIORIDAD_DE_NIVELES,
    LLMCostLimit,
    NivelLimiteCosteEnum,
    OperacionCosteEnum,
)
from backend.apps.organizations.models import Organization, PlanTierEnum


class CamposLimite(StrEnum):
    """Los dos campos que un tope puede acotar.

    ## Por qué un enum y no dos atributos sueltos

    Porque la resolución es **genérica sobre el campo**: recorre niveles y, para cada uno, pregunta
    si declara ese campo. El recorrido se declara una vez en `CAMPOS` y las dos columnas se nombran
    con el mismo identificador que el enum, de modo que añadir un tercer tope —un límite de tiempo
    del proveedor, un tope de peticiones— es añadir un valor y una columna, y nada del recorrido
    cambia.
    """

    PRESUPUESTO = "max_budget_usd"
    TURNOS = "max_turns"


CAMPOS: tuple[CamposLimite, ...] = (CamposLimite.PRESUPUESTO, CamposLimite.TURNOS)

#: El default global administrado también es una fila. El valor del entorno solo entra si
#: ningún nivel vigente declara el campo.
NIVELES_CONSULTADOS: tuple[NivelLimiteCosteEnum, ...] = PRIORIDAD_DE_NIVELES


class MotivoDescarte(StrEnum):
    """Por qué un valor no ganó. **No** es texto visible: es un código estable.

    Viaja en la respuesta de la API y en el registro del worker, y por eso son códigos y no frases:
    un motivo escrito como texto se traduce, y un motivo traducido no se puede filtrar en un
    registro ni comparar entre dos ejecuciones. La traducción de cada código la pone el panel.
    """

    #: El primer nivel con un valor para ese campo. El ganador **no** es un descarte.
    NIVEL_MAS_ESPECIFICO = "NIVEL_MAS_ESPECIFICO"
    #: No hay ninguna fila viva para ese campo: se usa el default global.
    DEFAULT_GLOBAL = "DEFAULT_GLOBAL"
    #: La fila existe y vive, pero no declara ese campo: el campo lo decide un nivel más abajo.
    SIN_VALOR_EN_ESTE_NIVEL = "SIN_VALOR_EN_ESTE_NIVEL"
    #: La fila todavía no ha empezado (`valid_from` en el futuro).
    VIGENCIA_FUTURA = "VIGENCIA_FUTURA"
    #: La fila ya caducó (`valid_until` pasado).
    CADUCADO = "CADUCADO"
    #: La fila es de otro alcance: otro tenant, otra operación u otro plan.
    ALCANCE_DISTINTO = "ALCANCE_DISTINTO"


@dataclass(frozen=True, slots=True)
class CandidatoLimite:
    """Una fila de `llm_cost_limits` ya traída de la base, con su vigencia.

    Se separa del modelo SQLAlchemy por la misma razón que `PlatformPrices`: lo que viaja al
    resolver es un valor plano que se puede comparar en pruebas y que no lleva sesión ni
    `_sa_instance_state`.
    """

    scope: NivelLimiteCosteEnum
    max_budget_usd: Decimal | None
    max_turns: int | None
    valid_from: datetime
    valid_until: datetime | None
    regla_id: uuid.UUID | None = None
    organization_id: uuid.UUID | None = None
    operation: OperacionCosteEnum | None = None
    plan_tier: PlanTierEnum | None = None

    @classmethod
    def desde_fila(cls, fila: LLMCostLimit) -> CandidatoLimite:
        return cls(
            scope=fila.scope,
            max_budget_usd=fila.max_budget_usd,
            max_turns=fila.max_turns,
            valid_from=fila.valid_from,
            valid_until=fila.valid_until,
            regla_id=fila.id,
            organization_id=fila.organization_id,
            operation=fila.operation,
            plan_tier=fila.plan_tier,
        )

    def valor_de(self, campo: CamposLimite) -> Decimal | int | None:
        return self.max_budget_usd if campo is CamposLimite.PRESUPUESTO else self.max_turns

    def motivo_de_no_vigencia(self, ahora: datetime) -> MotivoDescarte | None:
        """Por qué esta fila no manda ahora, o `None` si sí manda."""

        if self.valid_from > ahora:
            return MotivoDescarte.VIGENCIA_FUTURA
        if self.valid_until is not None and self.valid_until <= ahora:
            return MotivoDescarte.CADUCADO
        return None


@dataclass(frozen=True, slots=True)
class OrigenLimite:
    """De dónde salió **un** valor, y con qué motivo ganó."""

    campo: CamposLimite
    nivel: NivelLimiteCosteEnum
    regla_id: uuid.UUID | None
    valor: Decimal | int
    motivo: MotivoDescarte


@dataclass(frozen=True, slots=True)
class DescarteLimite:
    """Un candidato que existía y **no** ganó. Es lo que permite explicarlo seis meses después."""

    nivel: NivelLimiteCosteEnum
    regla_id: uuid.UUID | None
    campo: CamposLimite
    motivo: MotivoDescarte


@dataclass(frozen=True, slots=True)
class ResolucionLimites:
    """Los dos topes, con su procedencia y la lista de lo que se descartó."""

    max_budget_usd: Decimal
    origen_presupuesto: OrigenLimite
    max_turns: int
    origen_turnos: OrigenLimite
    descartados: tuple[DescarteLimite, ...]

    def a_json(self) -> dict[str, object]:
        """La forma que viaja a la API. Las claves van sin tildes porque son claves."""

        return {
            "max_budget_usd": self.max_budget_usd,
            "max_turns": self.max_turns,
            "presupuesto_origen": _origen_a_json(self.origen_presupuesto),
            "turnos_origen": _origen_a_json(self.origen_turnos),
            "descartados": [
                {
                    "nivel": descarte.nivel.value,
                    "regla_id": str(descarte.regla_id) if descarte.regla_id is not None else None,
                    "campo": descarte.campo.value,
                    "motivo": descarte.motivo.value,
                }
                for descarte in self.descartados
            ],
        }


def _origen_a_json(origen: OrigenLimite) -> dict[str, object]:
    return {
        "campo": origen.campo.value,
        "nivel": origen.nivel.value,
        "regla_id": str(origen.regla_id) if origen.regla_id is not None else None,
        "valor": origen.valor,
        "motivo": origen.motivo.value,
    }


def _aplica_a(
    candidato: CandidatoLimite,
    organization_id: uuid.UUID | None,
    operation: OperacionCosteEnum | None,
    plan_tier: PlanTierEnum | None,
) -> bool:
    """¿La fila declara el alcance que se le está preguntando?

    ## Por qué el `GLOBAL` no mira nada

    Porque su alcance es «todos», y preguntarle si su organización coincide con la que se consulta
    lo convertiría en un override que solo aplica a un tenant, que es exactamente lo que `GLOBAL`
    no es.

    ## Por qué comparar con `None` devuelve `False` y no lanza

    Porque preguntar por los topes de una organización que no existe, o de una operación que no se
    ha declarado, **no** puede devolver el override de otro. `None` significa «no hay a quién
    preguntar», y la respuesta correcta es que ese nivel no aplica.
    """

    if candidato.scope is NivelLimiteCosteEnum.GLOBAL:
        return True
    if candidato.scope is NivelLimiteCosteEnum.ORGANIZACION:
        return organization_id is not None and candidato.organization_id == organization_id
    if candidato.scope is NivelLimiteCosteEnum.OPERACION:
        return operation is not None and candidato.operation == operation
    return plan_tier is not None and candidato.plan_tier == plan_tier


def resolver_limites(
    candidatos: tuple[CandidatoLimite, ...] | list[CandidatoLimite],
    *,
    organization_id: uuid.UUID | None,
    operation: OperacionCosteEnum | None,
    plan_tier: PlanTierEnum | None,
    default_max_budget_usd: Decimal,
    default_max_turns: int,
    ahora: datetime | None = None,
) -> ResolucionLimites:
    """Resuelve los dos topes y explica cada decisión. Función pura: no toca la base ni el reloj.

    ## Por qué los defaults vienen en la firma

    Porque son el nivel `GLOBAL` y su valor es **del despliegue** (R1). Meterlos dentro del módulo
    los volvería constantes —los 3,00 y los 200 que este proyecto tenía escritos en el código— y la
    prueba del nivel global dejaría de poder moverlos. Quien llama los lee de `Settings`.

    ## Por qué `ahora` es un parámetro y no `datetime.now()`

    Porque la caducidad es un caso de negocio y un caso de negocio se prueba eligiendo la fecha.
    Con el reloj dentro, la prueba dependería del día que se ejecuta, y una caducidad que solo se
    puede demostrar en un día concreto no se puede demostrar.

    ## Por qué se filtran por vigencia **aquí** y no en el SQL

    Porque el filtro por fechas es **la regla**, y la regla tiene que poder probarse sin base de
    datos. El SQL trae las filas del alcance y esta función decide cuál manda: si el filtro viviera
    en el `WHERE`, la caducidad solo se podría comprobar con una base de datos y una fila caducada
    en el registro, que es más lento de preparar y menos claro de leer que un `ahora` explícito.
    """

    momento = ahora if ahora is not None else datetime.now(UTC)
    if default_max_budget_usd <= 0:
        raise ValueError("El tope global de presupuesto tiene que ser positivo")
    if default_max_turns <= 0:
        raise ValueError("El tope global de turnos tiene que ser positivo")

    defaults: dict[CamposLimite, Decimal | int] = {
        CamposLimite.PRESUPUESTO: default_max_budget_usd,
        CamposLimite.TURNOS: default_max_turns,
    }
    origenes: dict[CamposLimite, OrigenLimite] = {}
    descartados: list[DescarteLimite] = []

    for campo in CAMPOS:
        for nivel in NIVELES_CONSULTADOS:
            del_nivel = [candidato for candidato in candidatos if candidato.scope is nivel]
            vivos: list[CandidatoLimite] = []
            for candidato in del_nivel:
                if not _aplica_a(candidato, organization_id, operation, plan_tier):
                    motivo = MotivoDescarte.ALCANCE_DISTINTO
                else:
                    motivo = candidato.motivo_de_no_vigencia(momento)
                if motivo is None:
                    vivos.append(candidato)
                    continue
                if candidato.valor_de(campo) is not None:
                    descartados.append(
                        DescarteLimite(
                            nivel=nivel,
                            regla_id=candidato.regla_id,
                            campo=campo,
                            motivo=motivo,
                        )
                    )

            ganando = next(
                (candidato for candidato in vivos if candidato.valor_de(campo) is not None), None
            )
            for perdedor in vivos:
                # El ganador **no** es un descarte: es el origen. Solo se registra el resto, que es
                # la fila que existía, estaba viva, declaraba el campo y perdió contra otra del
                # mismo nivel. Sin esta condición, la resolución se contradiría a sí misma: el
                # mismo identificador aparecería a la vez como origen y como descarte.
                if perdedor is not None and perdedor is not ganando:
                    if perdedor.valor_de(campo) is not None:
                        descartados.append(
                            DescarteLimite(
                                nivel=nivel,
                                regla_id=perdedor.regla_id,
                                campo=campo,
                                motivo=MotivoDescarte.SIN_VALOR_EN_ESTE_NIVEL,
                            )
                        )
            if ganando is not None:
                valor_ganador = ganando.valor_de(campo)
                if valor_ganador is not None:
                    origenes[campo] = OrigenLimite(
                        campo=campo,
                        nivel=nivel,
                        regla_id=ganando.regla_id,
                        valor=valor_ganador,
                        motivo=MotivoDescarte.NIVEL_MAS_ESPECIFICO,
                    )
                    break
        if campo not in origenes:
            origenes[campo] = OrigenLimite(
                campo=campo,
                nivel=NivelLimiteCosteEnum.GLOBAL,
                regla_id=None,
                valor=defaults[campo],
                motivo=MotivoDescarte.DEFAULT_GLOBAL,
            )

    return ResolucionLimites(
        max_budget_usd=Decimal(str(origenes[CamposLimite.PRESUPUESTO].valor)),
        origen_presupuesto=origenes[CamposLimite.PRESUPUESTO],
        max_turns=int(origenes[CamposLimite.TURNOS].valor),
        origen_turnos=origenes[CamposLimite.TURNOS],
        descartados=tuple(descartados),
    )


def filtros_de_alcance(
    *,
    organization_id: uuid.UUID | None,
    operation: OperacionCosteEnum | None,
    plan_tier: PlanTierEnum | None,
) -> ColumnElement[bool]:
    """Los cuatro predicados del alcance, uno por nivel.

    ## Por qué `== None` es seguro aquí

    Porque los `CHECK` de la tabla obligan a que una fila `ORGANIZACION` tenga `organization_id` no
    nulo, y a que una `OPERACION` tenga `operation` no nulo. `== None` se compila a `IS NULL` y
    combinado con `scope = 'ORGANIZACION'` no puede encontrar ninguna fila: es la forma de decir
    «busca el override de esta organización, y si no hay organización no busques ninguno» sin un
    `if` alrededor de la condición entera.
    """

    return or_(
        and_(
            LLMCostLimit.scope == NivelLimiteCosteEnum.ORGANIZACION,
            LLMCostLimit.organization_id == organization_id,
        ),
        and_(
            LLMCostLimit.scope == NivelLimiteCosteEnum.OPERACION,
            LLMCostLimit.operation == operation,
        ),
        and_(
            LLMCostLimit.scope == NivelLimiteCosteEnum.PLAN,
            LLMCostLimit.plan_tier == plan_tier,
        ),
        LLMCostLimit.scope == NivelLimiteCosteEnum.GLOBAL,
    )


async def cargar_candidatos(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID | None,
    operation: OperacionCosteEnum | None,
    plan_tier: PlanTierEnum | None,
) -> tuple[CandidatoLimite, ...]:
    """Las filas de política del alcance pregunta, **sin** filtrar por vigencia.

    ## R3: `organization_id` va en el `WHERE`

    Porque es una tabla **por tenant**. Sin el filtro, la fila de override de una organización
    aparecería como candidata al resolver los límites de otra y un cliente vería el tope que otro
    tiene pactado. No es una optimización: es el aislamiento.

    ## Por qué las filas caducadas **sí** se traen

    Porque un descarte por caducidad es la respuesta a «¿por qué este run no cogió el tope que yo
    puse?». Si se filtraran en el `WHERE`, esa fila no existiría para el resolver y su ausencia
    sería indistinguible de nunca haberla escrito.
    """

    resultado = await session.execute(
        select(LLMCostLimit)
        .where(
            filtros_de_alcance(
                organization_id=organization_id,
                operation=operation,
                plan_tier=plan_tier,
            )
        )
        .order_by(LLMCostLimit.valid_from.desc(), LLMCostLimit.id)
    )
    return tuple(CandidatoLimite.desde_fila(fila) for fila in resultado.scalars().all())


async def plan_de(session: AsyncSession, organization_id: uuid.UUID) -> PlanTierEnum | None:
    """El plan de la organización, o `None` si la organización no existe.

    `None` no es «plan FREE»: es «no hay organización», que es un caso distinto. Resolver los
    límites de un tenant inexistente tiene que devolver el default global sin que eso parezca una
    política de plan.
    """

    resultado = await session.execute(
        select(Organization.plan_tier).where(Organization.id == organization_id)
    )
    return resultado.scalar_one_or_none()


async def limites_de(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID | None,
    operation: OperacionCosteEnum | None,
    default_max_budget_usd: Decimal,
    default_max_turns: int,
    ahora: datetime | None = None,
    scan_mode: str | None = None,
) -> ResolucionLimites:
    """Resuelve los topes de un tenant y una operación leyendo la base.

    Es el camino que usa el worker antes de lanzar el motor y el que usa la vista previa de la
    consola. Está separado del resolver puro para que la parte que **decide** no dependa de la
    sesión: la regla se prueba entera sin base de datos y esto es solo la lectura.
    """

    tier = await plan_de(session, organization_id) if organization_id is not None else None
    candidatos = await cargar_candidatos(
        session,
        organization_id=organization_id,
        operation=operation,
        plan_tier=tier,
    )
    if scan_mode is not None and operation == operacion_de_scan_mode(scan_mode):
        producto = await session.scalar(
            select(PentestProduct).where(
                PentestProduct.scan_mode == scan_mode.upper(),
                PentestProduct.is_active.is_(True),
            )
        )
        if producto is not None and (
            producto.max_budget_usd is not None or producto.max_turns is not None
        ):
            candidatos += (
                CandidatoLimite(
                    scope=NivelLimiteCosteEnum.OPERACION,
                    max_budget_usd=producto.max_budget_usd,
                    max_turns=producto.max_turns,
                    valid_from=datetime(1970, 1, 1, tzinfo=UTC),
                    valid_until=None,
                    regla_id=producto.id,
                    operation=operation,
                ),
            )
    if organization_id is not None:
        acuerdo = await effective_agreement(session, organization_id, now=ahora)
        if acuerdo is not None and (
            acuerdo.max_budget_usd is not None or acuerdo.max_turns is not None
        ):
            candidatos += (
                CandidatoLimite(
                    scope=NivelLimiteCosteEnum.ORGANIZACION,
                    max_budget_usd=acuerdo.max_budget_usd,
                    max_turns=acuerdo.max_turns,
                    valid_from=acuerdo.valid_from,
                    valid_until=acuerdo.valid_until,
                    regla_id=acuerdo.id,
                    organization_id=organization_id,
                ),
            )
    return resolver_limites(
        candidatos,
        organization_id=organization_id,
        operation=operation,
        plan_tier=tier,
        default_max_budget_usd=default_max_budget_usd,
        default_max_turns=default_max_turns,
        ahora=ahora,
    )


def operacion_de_scan_mode(scan_mode: str) -> OperacionCosteEnum:
    """La clase de operación que corresponde a un modo de escaneo.

    ## Por qué `QUICK` es el único con categoría propia

    Porque es el único cuyo consumo está medido: el run de referencia, `mindguard-site_23ee`, es
    un `QUICK` y costó 1,85 USD sin tope declarado. Separarlo permite darle un tope que quepa su
    consumo real y dejar el resto más holgado, sin inventar cifras para modos que nadie ha medido.
    """

    return (
        OperacionCosteEnum.PENTEST_QUICK
        if scan_mode.strip().upper() == "QUICK"
        else OperacionCosteEnum.PENTEST_DEEP
    )


__all__ = (
    "CAMPOS",
    "NIVELES_CONSULTADOS",
    "CamposLimite",
    "CandidatoLimite",
    "DescarteLimite",
    "MotivoDescarte",
    "OrigenLimite",
    "ResolucionLimites",
    "cargar_candidatos",
    "filtros_de_alcance",
    "limites_de",
    "operacion_de_scan_mode",
    "plan_de",
    "resolver_limites",
)
