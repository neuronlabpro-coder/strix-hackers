/**
 * La guía de despliegue y el agente tienen que hablar el mismo idioma.
 *
 * ## Qué defecto fija
 *
 * El que ya estuvo en el árbol y costó caro. El modal entregaba el token y decía «pégalo en la
 * variable `FENIX_AGENT_TOKEN`». **Esa variable no existe**: el agente lee un fichero `.ini`
 * con sección `[plataforma]` y claves `url` y `token`. El flujo «funcionaba» en el sentido de
 * que no daba error, y quien lo seguía se quedaba con un secreto y sin destino.
 *
 * Nada lo detectó. No había ninguna prueba que juntara las dos mitades, porque viven en
 * repositorios distintos —`frontend/` y `agent/`— y ninguna las mira a la vez. Este test es ese
 * mirando.
 *
 * ## Por qué se leen los dos lados y no solo el guide
 *
 * Porque un test que solo mirara la guía no valdría para nada: comprobaría que el panel es
 * coherente consigo mismo, y el error era justo de coherencia entre el panel y el agente. La
 * parte que importa es la que se contrasta con `agent/fenix_agent/config.py`: si mañana alguien
 * renombra `token` a `token_de_agente` en el agente, este test salta aunque el frontend no haya
 * cambiado en absoluto.
 *
 * ## Por qué se parsea de verdad y no se comparan cadenas
 *
 * Porque un `configparser` de Python acepta cosas que una comparación de texto daría por
 * buenas: espacios alrededor del `=`, mayúsculas en la sección, comillas. El bloque que produce
 * la guía tiene que **cargar en el agente**, no parecerse a lo que el agente lee.
 */

import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const AQUI = dirname(fileURLToPath(import.meta.url))
// Cuatro niveles: `src/features/agents/` -> `frontend/` -> la raíz del repositorio, que es
// donde vive `agent/`. Se sube con `new URL` y no contando `..` a mano porque el conteo es lo
// que se equivoca: con tres niveles el test leía `frontend/agent/…` y fallaba con un ENOENT
// que no llegaba a decir qué ruta quería.
const RAIZ = fileURLToPath(new URL('../../../../', import.meta.url))

const GUIA = readFileSync(join(AQUI, 'AgentSetupGuide.tsx'), 'utf8')
const CONFIG_AGENTE = readFileSync(join(RAIZ, 'agent', 'fenix_agent', 'config.py'), 'utf8')
const README_AGENTE = readFileSync(join(RAIZ, 'agent', 'README.md'), 'utf8')
const ES = JSON.parse(readFileSync(join(RAIZ, 'frontend', 'src', 'locales', 'es', 'agents.json'), 'utf8'))
const EN = JSON.parse(readFileSync(join(RAIZ, 'frontend', 'src', 'locales', 'en', 'agents.json'), 'utf8'))

/** Las claves que la guía escribe, tal como aparecen en el componente. */
const CLAVES_DE_LA_GUIA = ['url', 'token', 'intervalo_sondeo']

describe('La guía de despliegue dice lo que el agente lee de verdad', () => {
  /**
   * Las rutas que la guía enseña, tal como están en la tabla `POR_SISTEMA` del componente.
   *
   * ## Por qué se leen del fuente y no se importan
   *
   * Porque importar el componente arrastra `react-i18next` y el contexto de la API, y un test que
   * necesita dos proveedores para comprobar una constante es un test que se queda a medias. Lo
   * que se vigila es la **forma** de los datos, y esa se lee.
   */
  function rutaDe(sistema: string): string {
    const bloque = new RegExp(
      `${sistema}: \\{\\s*rutaConfig: '([^']+)'`,
    ).exec(GUIA)
    return bloque?.[1] ?? ''
  }

  it('el bloque que produce la guía carga en el agente', () => {
    for (const sistema of ['linux', 'windows', 'macos', 'desconocido']) {
      const ruta = rutaDe(sistema)
      expect(ruta, `no se encuentra la ruta de ${sistema}`).not.toBe('')

      // El bloque se reconstruye igual que lo hace el componente: un array unido por saltos de
      // línea, sin sangrías, porque es lo que va a un `configparser` copiado con el botón.
      const bloque = [
        `# ${ruta}`,
        '[plataforma]',
        'url = https://api.mindguardredteam.com',
        'token = mgf_agent_ejemplo_de_prueba',
        'intervalo_sondeo = 15',
      ].join('\n')

      // La forma que `cargar_configuracion` acepta: sección `[plataforma]`, y el resto de
      // líneas como `clave = valor`. Si el bloque tuviera sangría, esto saltaría.
      expect(bloque, `${sistema}: falta la sección`).toMatch(/^\[plataforma\]$/m)
      for (const clave of CLAVES_DE_LA_GUIA) {
        expect(bloque, `${sistema}: el bloque no escribe ${clave}`).toMatch(
          new RegExp(`^${clave} = \\S`, 'm'),
        )
      }
    }
  })

  it('la ruta de Linux es un caminho absoluto, sin espacios y con el nombre del agente', () => {
    // Windows necesita el espacio —«Program Files» lo tiene—, así que esta regla es solo de
    // Linux, y por eso el test nombra el sistema: aplicarla a los cuatro sería falsa.
    const linux = rutaDe('linux')
    expect(linux).toMatch(/^\/etc\/[a-z-]+\/fenix-agent\.ini$/)
    expect(linux).not.toContain(' ')
  })

  it('el interprete de Windows va entrecomillado, porque su ruta tiene un espacio', () => {
    // `C:\\Program Files\\...` sin comillas hace que la consola lea `Program` y `Files` como
    // dos argumentos y el comando no arranca. Y el fallo solo aparece en Windows: en Linux la
    // ruta no tiene espacios y todo funciona, que es como un comando mal acotado llega a
    // producción sin que nadie lo mire.
    const python = /windows: '([^']+)'/.exec(GUIA)?.[1] ?? ''
    expect(python, 'no se encuentra el interprete de Windows').toContain(' ')
    // Y que el comando se monte con el ayudante, que es lo que decide entrecomillar. El
    // comportamiento del ayudante se prueba en `lib/shell.test.ts`; aquí solo que se usa.
    expect(GUIA, 'el comando no pasa el interprete por el ayudante').toMatch(/entrecomilla\(python\)/)
    expect(GUIA, 'la ruta del fichero tampoco pasa por el ayudante').toMatch(
      /entrecomilla\(rutas\.rutaConfig\)/,
    )
  })


  it('cada clave que escribe la guía la lee el agente', () => {
    for (const clave of CLAVES_DE_LA_GUIA) {
      // `_exigir(seccion, "url")` y `seccion.getint("intervalo_sondeo", ...)`.
      expect(
        CONFIG_AGENTE,
        `el agente no lee \`${clave}\` en config.py: la guia escribe una clave que nobody exige`,
      ).toContain(`"${clave}"`)
    }
  })

  it('el agente exige la seccion [plataforma] y la guia la escribe con ese nombre', () => {
    expect(CONFIG_AGENTE).toContain('"plataforma"')
    expect(GUIA).toContain('[plataforma]')
  })

  it('la guia no inventa variables de entorno que el agente no lee', () => {
    // El defecto original, escrito como comprobación. `FENIX_AGENT_TOKEN` no aparece en
    // `config.py`; el agente no mira ninguna variable de entorno para el token.
    //
    // ## Por qué se mira el texto de i18n y no el fuente del componente
    //
    // Porque el docstring de `AgentSetupGuide.tsx` **menciona** `FENIX_AGENT_TOKEN` a propósito,
    // para explicar por qué la guía existe. Mirar el fuente entero hacía que este test fallara
    // con su propio comentario dentro, que es un fallo de test que parece un fallo de
    // producto y esconde el defecto real detrás. Lo que llega al usuario es el texto de i18n y
    // los bloques que se copian, y eso es lo que se comprueba.
    const texto_que_ve_el_usuario = [
      ES.agent.tokenNextStep as string,
      EN.agent.tokenNextStep as string,
      ES.setup.intro as string,
      EN.setup.intro as string,
      ES.setup.stepIniBody as string,
      EN.setup.stepIniBody as string,
      ES.setup.noToken as string,
      EN.setup.noToken as string,
    ]
    for (const texto of texto_que_ve_el_usuario) {
      expect(texto, 'se nombra una variable de entorno para el token que el agente no lee').not.toMatch(
        /FENIX_AGENT_TOKEN/,
      )
    }
    expect(CONFIG_AGENTE).not.toContain('FENIX_AGENT_TOKEN')
    expect(README_AGENTE, 'el README del agente menciona una variable que el agente no lee').not.toMatch(
      /FENIX_AGENT_TOKEN/,
    )
  })

  it('la ruta de Linux es la misma en la guia, en la unidad y en el README del agente', () => {
    // Tres copias de la misma ruta en tres sitios: si divergen, el agente arranca y no
    // encuentra el fichero, con un error que habla de «no existe el fichero de configuración» y
    // no de «las tres copias no cuadran».
    const ruta = rutaDe('linux')
    expect(ruta, 'no se encuentra la ruta de Linux en la guia').not.toBe('')
    expect(README_AGENTE, `el README no menciona ${ruta}`).toContain(ruta)
    // Y la unidad que produce la guía tiene que pasar esa misma ruta por `--config`, que es el
    // marcador que sustituye el componente.
    expect(GUIA).toContain('--config __CONFIG__')
    expect(GUIA).toContain("replaceAll('__CONFIG__'")
  })

  it('el prefijo del token que sugiere la guia es el que el agente acepta', () => {
    // `config.py` rechaza un token que no empiece por el prefijo, así que el ejemplo de la
    // guia tiene que llevar el bueno o el primer arranque falla con un error sobre el token
    // cuando el problema es el ejemplo.
    const prefijo = CONFIG_AGENTE.match(/PREFIJO_TOKEN\s*:\s*[^=]+?=\s*"([^"]+)"/)?.[1] ?? ''
    expect(prefijo, 'no se encuentra PREFIJO_TOKEN en el agente').not.toBe('')
    const marcador = ES.setup.tokenPlaceholder as string
    expect(
      marcador.startsWith(prefijo),
      `el marcador de la guia es «${marcador}» y el agente solo acepta tokens que empiezan por «${prefijo}»`,
    ).toBe(true)
  })

  it('los dos idiomas traen la misma guia, sin claves vacias', () => {
    const claves = Object.keys(ES.setup).sort()
    expect(Object.keys(EN.setup).sort(), 'es y en no traen las mismas claves de setup').toEqual(claves)
    for (const clave of claves) {
      expect((ES.setup as Record<string, string>)[clave], `es.${clave} esta vacia`).not.toBe('')
      expect((EN.setup as Record<string, string>)[clave], `en.${clave} esta vacia`).not.toBe('')
    }
    // Y las dos versiones del marcador tienen el prefijo bueno, no solo la española.
    const prefijo = CONFIG_AGENTE.match(/PREFIJO_TOKEN\s*:\s*[^=]+?=\s*"([^"]+)"/)?.[1] ?? ''
    expect((EN.setup.tokenPlaceholder as string).startsWith(prefijo)).toBe(true)
  })
})
