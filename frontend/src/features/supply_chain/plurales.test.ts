/**
 * Por qué las claves con plural están **en plano** y no anidadas.
 *
 * ## El defecto
 *
 * `metrics.repositories` estaba escrito como un objeto con `_one` y `_other`, que es la forma
 * que documenta i18next y la que llevaba años en el fichero. En pantalla salía, literalmente, la
 * cadena de aviso de i18next:
 *
 *     key 'metrics.repositories (es)' returned an object instead of string.
 *
 * ## La causa, medida
 *
 * No es un error de las traducciones ni de cómo se llama a `t()`. Con **i18next 26.4.2**, el
 * lookup de una clave con plural hace dos consultas en este orden:
 *
 *     metrics.repositories_one   -> undefined
 *     metrics.repositories       -> { _one: ..., _other: ... }   <- encuentra el objeto
 *
 * La segunda gana, `found` es un objeto y el traductor devuelve el mensaje de arriba. Con la
 * clave **plana** —`metrics.repositories_one` como hoja de primer nivel— la primera consulta
 * acierta y sale la cadena.
 *
 * Se comprobaron las tres salidas que se intuían antes de mirar el código, y ninguna funciona
 * con esta versión:
 *
 * | Qué se probó                                      | Resultado |
 * | :------------------------------------------------ | :-------- |
 * | Clave anidada, como estaba                        | objeto    |
 * | `compatibilityJSON: 'v3'`                         | objeto    |
 * | `compatibilityJSON: 'v4'`                         | objeto    |
 * | **Clave plana, `metrics.repositories_one`**        | **funciona** |
 *
 * ## Por qué un test que mira el JSON y no el resultado
 *
 * Porque el resultado del `t()` depende de cómo se inicializa i18next en cada entrada, y un
 * test que monte el módulo entero para comprobar dos cadenas necesita más andamiaje del que
 * compensa. Lo que hay que fijar es la **forma del fichero**, que es lo que se rompió y lo que
 * alguien volvería a romper al reordenar las traducciones. Y hay un segundo motivo, más
 * importante: si alguien devuelve la clave a la forma anidada, un test que solo mirara el
 * `t()` en un entorno concreto podría seguir en verde.
 *
 * ## Por qué el marcador está en el JSON
 *
 * Porque un fichero de traducciones no es código y nadie lo lee antes de reordenarlo. El
 * marcador convierte la rareza en algo declarado: quien abra `supplyChain.json` y vea
 * `metrics.repositories_one` lee el motivo en la siguiente línea, en lugar de deducir que está
 * mal y «arreglarlo» volviendo a la forma anidada.
 */

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

// `src/features/supply_chain/` → `frontend/` → la raíz del repositorio. Cuatro niveles, porque
// este fichero vive un nivel más hondo que `src/lib/`. Con tres leía `frontend/frontend/src/…` y
// fallaba con un ENOENT que no llegaba a decir qué ruta quería.
const RAIZ = fileURLToPath(new URL('../../../../', import.meta.url))

const IDIOMAS = ['es', 'en'] as const

/**
 * Las claves con plural, por **nombre plano**.
 *
 * El sufijo va pegado a la ruta completa, así que en el JSON la hoja cuelga de la raíz del
 * namespace —`metrics.repositories_one` es una clave de primer nivel— y no de `metrics`. Es lo
 * que hace que `metrics.repositories` deje de resolver a un objeto y aparezca la frase.
 */
const CLAVES_CON_PLURAL = ['metrics.repositories', 'status.vulnerable'] as const

function cargar(idioma: (typeof IDIOMAS)[number]): Record<string, unknown> {
  return JSON.parse(
    readFileSync(join(RAIZ, `frontend/src/locales/${idioma}/supplyChain.json`), 'utf8'),
  ) as Record<string, unknown>
}

/**
 * El valor de una clave, sea anidada o **plana**.
 *
 * ## Por qué prueba la plana antes de partir por puntos
 *
 * Porque una clave plana **contiene** puntos —`metrics.repositories_one`— y partirla por `.`
 * buscaría `metrics` → `repositories_one` dentro, que no existe: las planas cuelgan de la raíz
 * del namespace. Con el orden inverso, el aserto sobre `metrics.repositories_one` daría
 * `undefined` y el test de plurales pasaría sin comprobar nada.
 *
 * Es el mismo motivo por el que la forma plana no se parece a lo que i18next documenta, y por
 * el que hace falta un test que mire el fichero: ninguna lectura ingeniosa del `t()` lo
 * explicaría.
 */
function hoja(datos: Record<string, unknown>, clave: string): unknown {
  const directa = datos[clave]
  if (directa !== undefined) {
    return directa
  }
  let actual: unknown = datos
  for (const parte of clave.split('.')) {
    if (typeof actual !== 'object' || actual === null) {
      return undefined
    }
    actual = (actual as Record<string, unknown>)[parte]
  }
  return actual
}

describe('Las claves con plural de Supply Chain', () => {
  it('están en plano, no anidadas, en los dos idiomas', () => {
    for (const idioma of IDIOMAS) {
      const datos = cargar(idioma)
      for (const ruta of CLAVES_CON_PLURAL) {
        const valor = hoja(datos, ruta)
        expect(
          valor,
          `${idioma}: ${ruta} no debe ser un objeto — con i18next 26 devuelve el aviso ` +
            `«returned an object instead of string» en lugar de la frase`,
        ).toBeUndefined()

        for (const sufijo of ['_one', '_other'] as const) {
          const plano = hoja(datos, `${ruta}${sufijo}`)
          expect(plano, `${idioma}: falta ${ruta}${sufijo}`).toBeTypeOf('string')
        }
      }
    }
  })

  it('el marcador del fichero explica por qué están en plano', () => {
    // Sin el marcador, la siguiente persona que abra el fichero deduce que está mal. Con él, lo
    // lee. Y un fichero de traducciones se reordena mucho más a menudo de lo que se lee.
    for (const idioma of IDIOMAS) {
      expect(cargar(idioma).marcadorDePlurales, `${idioma}: falta el marcador`).toBe('plano')
    }
  })

  it('los dos plurales llevan `count`, y las variantes no llevan guiones bajos dentro', () => {
    // `{{count}}` es lo que i18next interpola en la hoja ya elegida. Sin él, las dos variantes
    // dicen lo mismo y el plural no aporta nada — que es el caso de «CVE» en español, donde
    // `_one` y `_other` son idénticos y es correcto: en español no se pluraliza ese sigla.
    for (const idioma of IDIOMAS) {
      const datos = cargar(idioma)
      for (const ruta of CLAVES_CON_PLURAL) {
        for (const sufijo of ['_one', '_other'] as const) {
          const texto = hoja(datos, `${ruta}${sufijo}`)
          expect(texto, `${idioma}: ${ruta}${sufijo} no es texto`).toBeTypeOf('string')
          expect(texto as string, `${idioma}: ${ruta}${sufijo} sin {{count}}`).toContain('{{count}}')
        }
      }
    }
  })

  it('ningún otro namespace vuelve a meter un plural anidado', () => {
    // La causa es la versión de i18next, no este fichero: cualquier otro namespace que use la
    // forma anidada tiene el mismo defecto y nadie lo vería hasta que salga en una pantalla. Se
    // recorre el árbol entero en lugar de una lista, que es lo que se quedaría corto.
    for (const idioma of IDIOMAS) {
      const anidados = new Map<string, string>()
      const recorrer = (rama: unknown, ruta: string) => {
        if (typeof rama === 'object' && rama !== null) {
          for (const [clave, valor] of Object.entries(rama as Record<string, unknown>)) {
            if (clave === 'marcadorDePlurales') {
              continue
            }
            const siguiente = ruta ? `${ruta}.${clave}` : clave
            const esPlural = ['_zero', '_one', '_two', '_few', '_many', '_other'].includes(clave)
            if (esPlural) {
              anidados.set(ruta, siguiente)
              continue
            }
            recorrer(valor, siguiente)
          }
        }
      }
      recorrer(cargar(idioma), '')
      expect(
        [...anidados.keys()],
        `${idioma}: hay plurales anidados en ${[...anidados.keys()].join(', ')}`,
      ).toEqual([])
    }
  })
})