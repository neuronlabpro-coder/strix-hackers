"""Catálogo comercial vigente: packs, escalera de descuento y límites de compra.

Es el hermano de `pricing.py`. Allí viven los cuatro precios **escalares** —la paridad y lo que
cuesta un escaneo—, que se leen en sitios sin sesión: el worker de Strix, dos servicios que
cobran desde un webhook. Aquí viven los **listas**: packs, tramos y límites.

## Por qué un módulo aparte y no todo en `pricing.py`

Porque se leen en sitios distintos y se editan en sitios distintos. Los escalares se consumen
al cobrar un escaneo, con frecuencia y sin sesión. El catálogo se consume al montar el resumen
de facturación, una vez por pantalla. Mezclarlos obligaría a que un cambio de pack —que es una
decisión comercial de meses— pasara por el mismo código y los mismos tests que un cambio del
precio de un escaneo, que es lo que se toca cuando se ajusta el margen.

## Por qué `VOLUME_DISCOUNT_TIERS` es la fuente del precio y `CREDIT_PACKS` no

Porque el precio de una cantidad de créditos lo decide **la escalera**, y los packs son lo que
el panel enseña. Eso no es un detalle: es lo que hace segura la función `price_for_credits`.

Si el precio consultara también la tabla de packs, bastaría con que un pack estuviera por
debajo de lo que la escalera da para esa misma cantidad para que el mismo crédito costara dos
importes distintos según por dónde se mirara, y para que comprar más saliera más barato. La
propiedad que el proyecto comprueba —`price_for_credits` es monótona— dejaría de cumplirse sin
que nada fallara.

De ahí la regla, y está escrita en el sitio donde se rompería: **`Catalogo.packs` no lo lee
ningún cálculo**. Solo lo lee `summary.available_packs()`, que pinta.

## Por qué los tramos se leen ordenados y por qué el orden es parte del contrato

`discount_for_spend` y `price_for_credits` recorren los tramos **en orden de umbral** y usan
`indice + 1` para leer el umbral siguiente, que es la cota superior del tramo actual. Si el
`SELECT` no ordena, un `display_order` mal puesto produce cotas incorrectas y el precio deja de
ser monótono **sin lanzar nada**. Por eso `cargar_catalogo` ordena en la consulta y no en
Python: ordenar en Python obligaría a que quien lee acordase de hacerlo.

## Por qué el tramo base es un invariante y no una coincidencia

`discount_for_spend` devuelve `Decimal("0.00")` cuando no hay ningún tramo que cubra el gasto,
no el primer tramo de la lista. La diferencia importa cuando un operador desactiva el tramo
base: con el índice a cero, la función lanzaría `IndexError` en **cada cobro**, y no en cada
carga de la pantalla, que es donde se notaría al probarlo.

Un tramo base desactivado tampoco rompe el precio: `discount_for_spend` cae al literal y el
primer tramo activo empieza a aplicar su descuento por debajo de su propio umbral, que es un
error comercial pero no un fallo.

## Por qué el mínimo y el máximo de gasto son parte del catálogo y no constantes

Porque `CUSTOM_CREDITS_MINIMUM` y `CUSTOM_CREDITS_MAXIMUM` se **derivan** de ellos. Escribirlos
al lado es la misma clase de error que R1 prohíbe, con dos números en vez de uno: si el mínimo de
gasto subiera a $50 y el mínimo de créditos se quedara en 10, el endpoint aceptaría una compra
que el catálogo no tiene, y el error aparecería en una conciliación y no en un test.

## Por qué el respaldo son las constantes del código y no un valor inventado

Porque la tabla puede no existir —una base restaurada de antes de la migración— o estar vacía.
En ese caso la plataforma tiene que seguir vendiendo. Y el respaldo tiene que ser **el
catálogo que el proyecto ya tenía**, no un catálogo inventado aquí: si el respaldo fuese un
número nuevo, un despliegue a medio hacer cobraría con un catálogo que nadie ha visto.

Y de ahí la fragilidad, que es deliberada y está vigilada: leer las constantes del código exige
importar `billing.schemas`, que a su vez importa este módulo para leer los límites. El
import es local a propósito —dentro de la función, no arriba— para que no haya ciclo, y solo
funciona porque las constantes que se leen están todas definidas **antes** de la línea que las
deriva. La prueba `test_el_catalogo_de_arranque_coincide_con_el_codigo` vigila esa coincidencia:
si alguien mueve una constante de sitio o cambia su valor sin cambiar el respaldo, la prueba
avisa diciendo qué número se ha quedado viejo.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from backend.apps.billing.models import CreditPack, PlatformPricing, VolumeTier
from backend.core.database import AsyncSession

#: La escala con la que se guardan los tramos y los packs: dos decimales de dólares.
_PRECISION_IMPORTE = Decimal("0.01")

#: Descuento cuando ningún tramo cubre el gasto.
#:
#: ## Por qué un literal y no `tramos[0][1]`
#:
#: ## Por qué no el primer tramo
#:
#: Porque el primer tramo puede no ser el base. El catálogo es editable desde el panel, y un
#: operador que desactive el tramo base haría que `tramos[0]` sea el de $251. Con un índice a
#: cero, `discount_for_spend` lanzaría `IndexError` en **cada cobro**. Con este literal, un
#: catálogo sin tramo base cobra sin descuento, que es un error comercial visible y no una
#: caída.
DESCUENTO_SIN_TRAMO = Decimal("0.00")


@dataclass(frozen=True, slots=True)
class Catalogo:
    """El catálogo comercial vigente, en un valor inmutable y en el orden en que se aplica.

    ## Por qué `tramos` es una tupla ordenada y no un diccionario

    Porque el orden **es** la semántica. El precio de una cantidad depende del umbral del tramo
    siguiente, así que un conjunto sin orden no es el mismo catálogo que el que se cobra. Y
    porque una tupla no se puede reordenar por accidente desde quien la recibe.

    ## Por qué `packs` lleva los importes y no solo las cantidades

    Porque el panel pinta los dos, y el importe de un pack es una decisión comercial propia, no
    una función de su cantidad. Que dos packs tengan la misma paridad hoy es una coincidencia
    del catálogo, no una regla del sistema.
    """

    tramos: tuple[tuple[Decimal, Decimal], ...]
    packs: tuple[tuple[int, Decimal], ...]
    gasto_minimo_usd: Decimal
    gasto_maximo_usd: Decimal
    suscripcion_mensual_usd: Decimal

    def descuento_para(self, gasto: Decimal) -> Decimal:
        """El descuento que aplica un gasto, sin depender de que haya tramo base."""

        aplicable = DESCUENTO_SIN_TRAMO
        for minimo, descuento in self.tramos:
            if gasto >= minimo:
                aplicable = descuento
            else:
                break
        return aplicable

    def usd_por_credito(self) -> Decimal:
        """Dólares que cuesta un crédito **sin descuento**.

        ## Por qué `1 / paridad` y no un uno fijo

        Porque un dólar son `paridad` créditos, así que un crédito son `1 / paridad` dólares.
        Con la paridad en 1,00 la división da 1,00 y no se nota; con paridad 2,50 un crédito
        son $0,40, y un catálogo que usara el uno fijo cobraría un 150 % de más sin decir nada.
        """

        from backend.apps.billing.pricing import credits_per_usd

        return (Decimal("1") / credits_per_usd()).quantize(_PRECISION_IMPORTE)

    def usd_por_credito_con_descuento(self, descuento: Decimal) -> Decimal:
        """Dólares que cuesta un crédito en el tramo de ese descuento.

        ## Por qué son dos funciones y no una con el descuento opcional

        Porque son dos números que se usan en sitios distintos y confundirlos cambia el precio
        sin cambiar ningún otro número. `usd_por_credito` es el precio de lista, y es lo que
        pinta la barra de descuentos para que el cliente vea lo que ahorra. `usd_por_credito_
        con_descuento` es lo que se cobra, y es lo que entra en `price_for_credits`.

        ## Por qué el descuento multiplica y no se resta

        Porque el descuento es una fracción del precio de lista. Si se restara —`1,00 - 0,10`
        con paridad 1,00— daría el mismo número por casualidad, y con paridad 0,50 daría
        `2,00 - 0,10 = 1,90` en vez de `2,00 x 0,90 = 1,80`: un 10 % de descuento aplicado a
        una parte fija en vez de al precio entero.
        """

        from backend.apps.billing.pricing import credits_per_usd

        bruto = Decimal("1") / credits_per_usd()
        return (bruto * (Decimal("1.00") - descuento)).quantize(_PRECISION_IMPORTE)


#: El catálogo vivo. `None` significa «nadie ha cargado nada todavía».
_vigente: Catalogo | None = None


def catalogo_vigente() -> Catalogo:
    """El catálogo que rige ahora, con respaldo en las constantes del código.

    Nunca lanza. Si la base no se pudo leer, devuelve el catálogo que el proyecto ya tenía, que
    es lo que permite desplegar la base y el código por separado sin dejar de vender.
    """

    if _vigente is None:
        return catalogo_de_arranque()
    return _vigente


def fijar_catalogo(catalogo: Catalogo | None) -> None:
    """Fija —o con `None` descarta— el catálogo vivo.

    Existe `None` para que los tests puedan volver al estado de arranque sin saber qué dejó
    puesto el test anterior. Un test que cambia el precio y no lo devuelve deja el proceso
    vendiendo a un catálogo que nadie eligió, y el fallo aparece en otro fichero con un nombre
    que no habla de precios.
    """

    global _vigente
    _vigente = catalogo


def catalogo_de_arranque() -> Catalogo:
    """El catálogo que declara el código, sin tocar la base.

    ## Por qué el import es local y no arriba del módulo

    Porque `billing.schemas` importa este módulo, para derivar de aquí los límites de compra
    que usa en sus `Field`. Si este módulo importara `schemas` arriba, sería un ciclo y el
    import de cualquiera de los dos reventaría. Con el import dentro de la función, el ciclo no
    existe en tiempo de importación: solo se resuelve cuando alguien pide el catálogo, y para
    entonces `schemas` ya está cargado por completo.

    ## Por qué eso sí es frágil, y por qué se asume

    Porque este catálogo se construye **durante** la importación de `schemas`, en las líneas
    que derivan `CUSTOM_CREDITS_MINIMUM` y `CUSTOM_CREDITS_MAXIMUM`. En ese momento
    `schemas` está a medias, y solo funciona porque todas las constantes que se leen aquí están
    definidas por encima de esa línea.

    Se asume porque la alternativa —leer la base— no existe: importar un módulo no puede abrir
    una sesión, y el precio no puede faltar. Se vigila con una prueba que compara este catálogo
    con las constantes del código y que, si deja de coincidir, dice qué número se ha quedado
    viejo.
    """

    from backend.apps.billing import schemas

    return Catalogo(
        tramos=tuple(schemas.VOLUME_DISCOUNT_TIERS),
        packs=tuple(sorted(schemas.CREDIT_PACKS.items())),
        gasto_minimo_usd=schemas.CUSTOM_SPEND_MINIMUM_USD,
        gasto_maximo_usd=schemas.CUSTOM_SPEND_MAXIMUM_USD,
        suscripcion_mensual_usd=schemas.PRO_SUBSCRIPTION_MONTHLY_USD,
    )


async def cargar_catalogo(session: AsyncSession) -> Catalogo | None:
    """Lee el catálogo de la base y lo fija. Devuelve `None` si **no hay fila**.

    ## Por qué `None` en vez de lanzar

    Porque «la tabla existe y está vacía» y «la tabla no existe» son el mismo fallo desde donde
    se lee, y los dos tienen la misma respuesta correcta: vender con el catálogo de arranque y
    avisar. Si esto lanzara, arrancar la plataforma dependería de que una migración concreta se
    hubiera aplicado, que es un acoplamiento que ningún otro módulo del proyecto tiene.

    ## Por qué el mínimo y el máximo de gasto se leen de la fila y no de los tramos

    Porque son dos números con significado propio —«hasta dónde llega el slider»— y no se
    derivan de los umbrales de descuento. El primer tramo empieza en el mínimo, pero eso es una
    consecuencia de cómo seinearían hoy, y si el operador añadiera un tramo por debajo del
    mínimo, derivarlo haría que el mínimo se moviera solo y el slider cambiaría de límites sin
    que nadie lo pidiera.

    ## Por qué el orden va en la consulta

    Porque `discount_para` usa el **siguiente** umbral como cota del tramo actual, y eso exige
    que la tupla llegue ordenada. Ordenar en Python obligaría a que quien lee se acuerde de
    hacerlo, y un `ORDER BY` que se olvida no falla: cobra mal.
    """

    global _vigente
    from sqlalchemy import select

    fila = (
        await session.execute(select(PlatformPricing).where(PlatformPricing.id == 1))
    ).scalar_one_or_none()
    if fila is None:
        _vigente = None
        return None

    packs = (
        await session.execute(
            select(CreditPack)
            .where(CreditPack.is_active.is_(True))
            .order_by(CreditPack.display_order, CreditPack.credits)
        )
    ).scalars().all()
    tramos = (
        await session.execute(
            select(VolumeTier)
            .where(VolumeTier.is_active.is_(True))
            .order_by(VolumeTier.spend_min_usd)
        )
    ).scalars().all()

    _vigente = Catalogo(
        tramos=tuple(
            (tramo.spend_min_usd.quantize(_PRECISION_IMPORTE), tramo.discount) for tramo in tramos
        ),
        packs=tuple((pack.credits, pack.amount_usd.quantize(_PRECISION_IMPORTE)) for pack in packs),
        gasto_minimo_usd=fila.custom_spend_minimum_usd,
        gasto_maximo_usd=fila.custom_spend_maximum_usd,
        suscripcion_mensual_usd=fila.pro_subscription_monthly_usd,
    )
    return _vigente
