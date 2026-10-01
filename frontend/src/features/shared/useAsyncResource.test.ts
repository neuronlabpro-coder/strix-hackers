/**
 * Pruebas de la derivación de estado de `useAsyncResource`.
 *
 * ## El defecto que estas pruebas existen para no dejar volver
 *
 * El hook derivaba `isLoading` como «la clave pedida no es la de los datos» y `loadFailed` como
 * «esta petición falló». Cuando una petición **falla**, las dos cosas son ciertas: nunca llegó
 * una respuesta, así que `claveDeLosDatos` sigue siendo `null`, y `claveQueFallo` ya es la clave
 * pedida.
 *
 * Y como las pantallas del panel pintan el estado como
 * `isLoading ? cargando : loadFailed ? error : datos`, la primera rama se comía el error. El
 * síntoma era una pantalla en «Cargando precios…» eterna con la consola del navegador llena de
 * 404, que es exactamente lo que ocurrió con la sección de precios sin desplegar.
 *
 * ## Por qué se prueba la función suelta y no el hook
 *
 * Porque la derivación es aritmética pura sobre tres claves, y se puede comprobar entera sin
 * montar React. Este proyecto no tiene `@testing-library/react` ni entorno de DOM: sus pruebas
 * son de lógica, y esta es justo la clase de cosa que se comprueba sin ellos.
 *
 * ## Por qué no una prueba por pantalla
 *
 * Porque el defecto estaba en la derivación, no en las pantallas. Arreglar las diez pantallas
 * una a una habría dejado la derivación igual, y la siguiente pantalla nueva habría repetido el
 * fallo sin que nadie lo notara.
 */

import { describe, expect, it } from 'vitest'
import { estadoDeLaPeticion } from './useAsyncResource'

describe('estadoDeLaPeticion', () => {
  it('sin clave no hay carga ni fallo: no se ha pedido nada', () => {
    expect(estadoDeLaPeticion(null, null, null)).toEqual({ cargando: false, fallo: false })
  })

  it('con datos de esta misma clave ya no está cargando', () => {
    expect(estadoDeLaPeticion('a', 'a', null)).toEqual({ cargando: false, fallo: false })
  })

  it('una peticion en vuelo está cargando', () => {
    expect(estadoDeLaPeticion('b', 'a', null)).toEqual({ cargando: true, fallo: false })
  })

  it('un fallo NO se queda en cargando: es un fallo y se puede ver', () => {
    // Este es el test del defecto. Con la derivación anterior, `cargando` era `true` para
    // siempre y ninguna pantalla llegaba a pintar su rama de error.
    expect(estadoDeLaPeticion('a', null, 'a')).toEqual({ cargando: false, fallo: true })
  })

  it('un fallo de otra clave no afecta a la peticion actual', () => {
    // El fallo es de **esta** petición. Sin esta condición, cambiar de filtro tras un fallo
    // dejaría el error de la vista previa sobre una lista que sí se está cargando.
    expect(estadoDeLaPeticion('b', 'a', 'a')).toEqual({ cargando: true, fallo: false })
  })

  it('tras un fallo, una clave nueva vuelve a estar cargando', () => {
    // Si `cargando` se hubiera tapado con un «no ha fallado nunca» en vez de excluir solo la
    // clave que falló, la pantalla se quedaría en el error del filtro anterior mientras la
    // petición nueva está en curso. Esta prueba es la que distingue las dos correcciones.
    expect(estadoDeLaPeticion('b', 'a', 'a')).toEqual({ cargando: true, fallo: false })
  })

  it('un estado imposible no puede aparecer: la clave con exito tiene el fallo limpio', () => {
    // El hook limpia `claveQueFallo` en cuanto llega la respuesta, así que una clave **no**
    // puede tener datos y estar fallada a la vez. Por eso el par `(datos, fallo)` no es
    // arbitrario, y por eso esta prueba fija cuál de los dos manda si alguien llega a
    // construir ese estado: el fallo, que es el estado que hay que poder pintar.
    //
    // Y el boton «Reintentar» no cambia la clave —cambia el tick y se vuelve a pedir—, así que
    // el estado en vuelo es `(a, null, null)`: cargando. Un fallo **no** deja la pantalla
    // atrapada, y eso es lo que comprueba la prueba de arriba.
    expect(estadoDeLaPeticion('a', 'a', 'a')).toEqual({ cargando: false, fallo: true })
    expect(estadoDeLaPeticion('a', null, null)).toEqual({ cargando: true, fallo: false })
  })

  it('nunca hay carga y fallo a la vez', () => {
    // La invariante que de hecho importa, comprobada en todos los estados posibles del par de
    // claves. Una pantalla solo puede estar en un sitio.
    const claves = [null, 'a', 'b']
    for (const clave of claves) {
      for (const claveDeLosDatos of claves) {
        for (const claveQueFallo of claves) {
          const { cargando, fallo } = estadoDeLaPeticion(clave, claveDeLosDatos, claveQueFallo)
          expect(
            cargando && fallo,
            `con clave=${clave} datos=${claveDeLosDatos} fallo=${claveQueFallo}`,
          ).toBe(false)
        }
      }
    }
  })
})