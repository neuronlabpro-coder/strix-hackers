/**
 * La fecha de un hilo del menu lateral.
 *
 * ## Por que relativo
 *
 * Porque el menu lateral responde a "¿cual era el hilo de ayer?" y no a "¿que dia fue el 14 de
 * marzo?". Una relativa envejece sola y mantiene la informacion util sin que el usuario tenga
 * que traducirla.
 *
 * ## Por que se corta a treinta dias
 *
 * Porque pasado ese punto el numero de dias es un dato que hay que contar. "Hace mas de un mes"
 * es una idea que se entiende sin esfuerzo; "hace 47 dias" no.
 *
 * ## Por que `Intl.RelativeTimeFormat` y no una cadena hecha a mano
 *
 * Porque el plural y el genero los pone el runtime, no el codigo: "hace 1 dia" y "hace 2 dias"
 * salen de una regla con dos entradas, y las dos son el mismo `count` con distinto valor. Y
 * porque un formateador escrito a mano solo tendria el español: el panel declara dos idiomas, y
 * traducir una regla de plural en dos sitios es una cuenta pendiente que se paga en cuanto el
 * tercer idioma llegue.
 *
 * Vive fuera del componente y no dentro por una razon concreta: `react-refresh` solo funciona si
 * un archivo exporta componentes y nada mas. Exportar tambien esta funcion desde el componente
 * desactiva el recarga en caliente de todo el archivo, y el usuario deja de ver sus cambios al
 * guardar mientras edita el menu lateral.
 */

/** A partir de aqui la fecha relativa deja de ayudar. */
const LIMITE_DE_DIAS = 30

const MS_POR_DIA = 24 * 60 * 60 * 1000

export function relativeDate(isoDate: string, locale: string): string {
  const fecha = new Date(isoDate)
  if (Number.isNaN(fecha.getTime())) return ''

  const relativo = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' })
  const dias = Math.floor((Date.now() - fecha.getTime()) / MS_POR_DIA)
  if (dias > LIMITE_DE_DIAS) {
    // `toISOString` corta en `AAAA-MM-DD` sin tener en cuenta la zona horaria del navegador, y
    // a las ocho de la tarde en un huso negativo el hilo de "hoy" aparece con la fecha de ayer.
    // `toLocaleDateString` con la zona si la tiene en cuenta, que es lo que quiere un usuario
    // que esta mirando la hora de su propio reloj.
    return new Intl.DateTimeFormat(locale, { dateStyle: 'medium' }).format(fecha)
  }
  return relativo.format(-dias, 'day')
}
