/**
 * Entrecomillado de valores para una línea de comandos.
 *
 * ## Por qué esto es un módulo y no una función dentro de la guía
 *
 * Porque una función que solo se usa en un sitio parece que no merece un fichero, y entonces se
 * queda sin probar. Y las dos veces que se ha escrito esto mismo ha sido con un `.replace` que
 * quitaba **todas** las comillas, que en Linux no se nota porque las rutas no tienen espacios y
 * en Windows deja el comando entero roto.
 *
 * Separado, se importa, y se prueba con los casos que de verdad importan: un valor con espacio y
 * uno sin él.
 *
 * ## Por qué no se entrecomilla siempre
 *
 * Porque en Linux `"/usr/bin/python3"` funciona igual que `/usr/bin/python3`, y en Windows
 * `C:\Program Files\Python312\python.exe` **no** funciona sin comillas: `Program` y `Files`
 * parecen dos argumentos y la consola responde «no se reconoce el término». Entrecomillar
 * siempre resuelve el segundo caso a costa de ensuciar el primero, que es el caso que se lee y
 * se copia a mano.
 */

/**
 * Devuelve el valor entrecomillado si contiene un espacio, y tal cual si no.
 *
 * ## Por qué no comprueba más caracteres
 *
 * Porque los que de verdad rompen una línea de comandos en estos cuatro sistemas son el espacio
 * y las comillas, y el caso que hay que cubrir es el espacio: las rutas de `Program Files` y las
 * de `/etc`. Una lista de caracteres especiales sería más correcta en theory y más fácil de
 * equivocar en la práctica, y una función de entrecomillado que se equivoca en un carácter
 * unnoticed es peor que una que entrecomilla de más.
 */
export function entrecomilla(valor: string): string {
  return valor.includes(' ') ? `"${valor}"` : valor
}
