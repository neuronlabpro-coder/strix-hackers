import type { OrganizationPriceOverride } from '../../types/api'

/**
 * La lógica de la cadena de precios pactados, separada del componente.
 *
 * ## Por qué existe
 *
 * Porque la parte de esta pantalla que se puede equivocar **sin dar error** es decidir qué fila se
 * enseña como vigente. Un componente que pinta mal sigue compilando, sigue renderizando y sigue
 * enseñando números: el fallo es que el comercial ve un estado sobre un precio que no es el que se
 * está cobrando, y eso no se pilla mirando el código.
 *
 * ## Por qué el estado no se deduce de `valido_hasta`
 *
 * Porque un pactado **programado** —uno que empieza dentro de un mes, para una renovación— tiene
 * `valido_hasta === null` igual que uno vigente. Los dos se parecerían en la lista y el comercial
 * leería lo mismo. La única fuente de verdad de si uno está en vigor es la marca `vigente` que
 * envía el servidor, porque la decide el reloj del servidor y no el del navegador: un panel abierto
 * en una pestaña que lleva días sin recargar sigue teniendo la hora de cuando se abrió.
 */

/** Los cuatro estados que la ficha sabe pintar. */
export type EstadoPactado = 'vigente' | 'programado' | 'caducado' | 'sustituido'

/**
 * El estado de un pactado, sin mirar el resto de la cadena.
 *
 * @param pactado La fila tal y como la envía el servidor.
 * @returns `vigente` si el servidor la marca en vigor; `caducado` si tiene fecha de fin ya pasada;
 *   `programado` en cualquier otro caso, que es el de un acuerdo que aún no ha empezado.
 */
export function estadoDePactado(pactado: OrganizationPriceOverride): EstadoPactado {
  if (pactado.vigente) {
    return 'vigente'
  }
  if (pactado.valido_hasta !== null) {
    return 'caducado'
  }
  return 'programado'
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

/** La clave de un pactado: la operación y, si la tiene, a qué pack se refiere. */
function claveDe(pactado: OrganizationPriceOverride): string {
  return pactado.alcance === null ? pactado.operacion : `${pactado.operacion}:${pactado.alcance}`
}

function empezo(pactado: OrganizationPriceOverride, ahora: number): boolean {
  return Date.parse(pactado.valido_desde) <= ahora
}

function caduca(pactado: OrganizationPriceOverride, ahora: number): boolean {
  return pactado.valido_hasta !== null && Date.parse(pactado.valido_hasta) <= ahora
}

/**
 * El estado de cada pactado, ya resuelto contra el resto de la cadena.
 *
 * ## Por qué hace falta y no basta con `estadoDePactado` uno a uno
 *
 * Porque un pactado puede haber empezado, no tener fecha de fin, no ser el vigente, y aun así no
 * estar "programado": está **sustituido**. Eso solo se sabe mirando si hay otro pactado más
 * reciente de la misma operación, así que la función necesita la lista entera.
 *
 * Y se ve en la pantalla: durante el rediseño, los cuatro pactados ya reemplazados salían
 * etiquetados como "Programado", que dice que ese precio empezará más tarde cuando lo cierto es que
 * empezó, se cobró y dejó de aplicarse porque se pactó otro.
 *
 * ## Por qué se deduce quién manda cuando el servidor no marcó a nadie
 *
 * Porque `vigente` llega de un proceso con caché que se refresca por evento, y en la ventana entre
 * un pactado nuevo y el refresco la cadena vuelve sin ninguno marcado. Se deduce con la misma regla
 * del negocio en lugar de dejar toda la cadena en "programado", que diría que hay cuatro subidas
 * futuras cuando en realidad hay una vigente.
 *
 * ## Por qué se comparan por clave y no por posición
 *
 * Porque la lista puede traer dos operaciones a la vez —un escaneo y un pack— y "el último de la
 * lista" no significa nada cuando hay más de una. La clave es operación más alcance, que es
 * exactamente lo que distingue dos filas de la misma tabla.
 *
 * @param pactados La cadena completa, en cualquier orden.
 * @returns Un diccionario de `id` a estado que cubre todos los pactados de la entrada.
 */
export function estadosDeLaCadena(
  pactados: readonly OrganizationPriceOverride[],
): Map<string, EstadoPactado> {
  const ahora = Date.now()
  const ordenados = ordenarCadena(pactados)

  // Para cada clave, el `id` de quien manda de entre los que ya han empezado y no han caducado.
  const queManda = new Map<string, string>()
  for (const clave of clavesDe(ordenados)) {
    const candidatos = ordenados.filter((p) => claveDe(p) === clave)
    const marcado = candidatos.find((p) => p.vigente)
    if (marcado !== undefined) {
      queManda.set(clave, marcado.id)
      continue
    }
    // Sin marca del servidor, gana el más reciente de los que ya empezaron: la lista va de más
    // reciente a más antigua, así que el primero que cumple es el bueno.
    const vigente = candidatos.find((p) => empezo(p, ahora) && !caduca(p, ahora))
    if (vigente !== undefined) {
      queManda.set(clave, vigente.id)
    }
  }

  const estados = new Map<string, EstadoPactado>()
  for (const pactado of ordenados) {
    if (pactado.vigente) {
      estados.set(pactado.id, 'vigente')
    } else if (caduca(pactado, ahora)) {
      estados.set(pactado.id, 'caducado')
    } else if (!empezo(pactado, ahora)) {
      estados.set(pactado.id, 'programado')
    } else {
      const esElQueManda = queManda.get(claveDe(pactado)) === pactado.id
      estados.set(pactado.id, esElQueManda ? 'vigente' : 'sustituido')
    }
  }
  return estados
}

function clavesDe(pactados: readonly OrganizationPriceOverride[]): string[] {
  return [...new Set(pactados.map(claveDe))]
}

export interface ResumenCadena {
  vigente: number
  programado: number
  caducado: number
  sustituido: number
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
 * Y los cinco contadores suman siempre el total: si un pactado se contara en dos casillas, la
 * cifra de "cuántos acuerdos hay" no cuadraría con la lista, y ese desajuste es el aviso más barato
 * de que la regla de estado se ha descolocado.
 */
export function resumenDeLaCadena(pactados: readonly OrganizationPriceOverride[]): ResumenCadena {
  const resumen: ResumenCadena = {
    vigente: 0,
    programado: 0,
    caducado: 0,
    sustituido: 0,
    total: pactados.length,
  }
  for (const pactado of pactados) {
    resumen[estadoDePactado(pactado)] += 1
  }
  return resumen
}
