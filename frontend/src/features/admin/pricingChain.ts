import type { OrganizationPriceOverride } from '../../types/api'

/**
 * La lógica de la cadena de precios pactados, separada del componente.
 *
 * ## Por qué existe
 *
 * Porque la parte de esta pantalla que se puede equivocar **sin dar error** es decidir qué fila se
 * enseña como vigente. Un componente que pinta mal sigue compilando, sigue renderizando y sigue
 * enseñando números: el fallo es que el comercial ve «vigente» sobre un precio que no se está
 * cobrando, y eso no se pilla mirando la pantalla.
 *
 * ## Por qué el estado no se deduce de `valido_hasta`
 *
 * Porque un pactado **programado** —uno que empieza dentro de un mes, para una renovación— tiene
 * `valido_hasta === null` igual que uno vigente. Los dos se parecerían en la lista y el comercial
 * leería lo mismo. La única fuente de verdad de si uno está en vigor es la marca `vigente` que
 * envía el servidor, porque la decide el reloj del servidor y no el del navegador: un panel abierto
 * en una pestaña que lleva días sin recargar sigue teniendo la hora de cuando se abrió.
 *
 * Y de ahí la asimetría de las dos reglas: `vigente` gana siempre, y `valido_hasta` solo se mira
 * cuando el servidor **no** lo marca vigente. Un pactado con ventana que el servidor declara
 * vigente está vigente, aunque le quede fecha de fin, porque en ese día la ventana sigue abierta.
 */

/** Los tres estados que la ficha sabe pintar. */
export type EstadoPactado = 'vigente' | 'futuro' | 'caducado'

/**
 * El estado de un pactado, para pintarlo.
 *
 * @param pactado La fila tal y como la envía el servidor.
 * @returns `vigente` si el servidor la marca en vigor; `caducado` si tiene fecha de fin; `futuro`
 *   en cualquier otro caso, que es el de un acuerdo que aún no ha empezado.
 */
export function estadoDePactado(pactado: OrganizationPriceOverride): EstadoPactado {
  if (pactado.vigente) {
    return 'vigente'
  }
  if (pactado.valido_hasta !== null) {
    return 'caducado'
  }
  return 'futuro'
}

/**
 * La cadena de pactados, del más reciente al más antiguo.
 *
 * ## Por qué se ordena aquí y no se pide ya ordenado al servidor
 *
 * Porque el servidor sí lo ordena, y aun así esta función vuelve a ordenar. Redundante a
 * propósito: el orden es parte de lo que la pantalla afirma —«el primero es el último acuerdo»— y
 * una garantía que depende de que el orden de otra capa no cambie es una garantía que se pierde el
 * día que esa capa cambia. Ordenar once filas no cuesta nada.
 *
 * ## Por qué no muta la entrada
 *
 * Porque el array viene de la respuesta del servidor, que es estado compartido con el resto de la
 * pantalla. Ordenarlo en el sitio cambiaría lo que ve cualquier otro que lo use, y el síntoma
 * sería una fila que salta de sitio al recargar.
 */
export function ordenarCadena(
  pactados: readonly OrganizationPriceOverride[],
): OrganizationPriceOverride[] {
  return [...pactados].sort((a, b) => Date.parse(b.valido_desde) - Date.parse(a.valido_desde))
}

export interface ResumenCadena {
  vigente: number
  futuro: number
  caducado: number
  total: number
}

/**
 * Cuántos pactados hay de cada clase.
 *
 * ## Por qué el resumen y no contar en el render
 *
 * Porque el operador necesita el número de programados para decidir si tiene una renovación
 * pendiente, y un contador dentro del JSX se recalcula en cada pintada sin que nadie lo note. Aquí
 * es un dato, y un dato se puede comprobar con un test.
 *
 * Y los cuatro contadores suman siempre el total: si un pactado se contara en dos casillas, la
 * cifra de "cuántos acuerdo hay" no cuadraría con la lista, y ese desajuste es el aviso más barato
 * de que la regla de estado se ha descolocado.
 */
export function resumenDeLaCadena(pactados: readonly OrganizationPriceOverride[]): ResumenCadena {
  const resumen: ResumenCadena = { vigente: 0, futuro: 0, caducado: 0, total: pactados.length }
  for (const pactado of pactados) {
    resumen[estadoDePactado(pactado)] += 1
  }
  return resumen
}
