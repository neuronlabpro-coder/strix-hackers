/**
 * El entrecomillado de una línea de comandos.
 *
 * ## Qué defecto fija
 *
 * El que la guía de despliegue del agente arrastró dos veces. El comando se armaba con
 * `.replace(/"/g, '')`, que quita **todas** las comillas: las que se habían puesto para proteger
 * el espacio de `C:\Program Files\Python312\python.exe` y las que protegían el resto.
 *
 * En Linux no se nota, porque `/etc/fenix-agent/fenix-agent.ini` no tiene espacios. En Windows
 * el comando no arranca, con un «no se reconoce el término» que no dice nada de comillas. Es el
 * peor de los dos casos: un comando que funciona en una plataforma y está mal en la otra llega a
 * producción sin que nadie lo mire, porque la mitad de los clientes lo copia y funciona.
 *
 * ## Por qué este test **importa** la función y no lee el fuente
 *
 * ## Por qué importa la función y no lee el fuente
 *
 * Porque leer el fuente para decidir si hay un `.replace` global es comprobar una **forma** del
 * texto, y la forma no es el comportamiento. Peor: la explicación del defecto —que está en el
 * docstring— menciona el `.replace` que se quiere prohibir, así que un test que leyera el fuente
 * fallaría con su propio comentario dentro. Ya pasó dos veces aquí.
 *
 * Importando la función, el test dice lo que quiere decir —«esto entrecomilla cuando toca»— y
 * no puede ser engañado por un comentario.
 */

import { describe, expect, it } from 'vitest'

import { entrecomilla } from './shell'

describe('El entrecomillado de una línea de comandos', () => {
  it('entrecomilla lo que tiene espacios, que es lo que rompe la consola', () => {
    // La ruta de Python en Windows. Sin comillas, `Program` y `Files` son dos argumentos.
    const interprete = 'C:\\Program Files\\Python312\\python.exe'
    expect(entrecomilla(interprete)).toBe(`"${interprete}"`)
  })

  it('deja intacto lo que no tiene espacios', () => {
    // En Linux entrecomillar estorba: el comando se lee a mano y los signos de puntuación
    // sobran en una ruta que no los necesita.
    expect(entrecomilla('/usr/bin/python3')).toBe('/usr/bin/python3')
    expect(entrecomilla('/etc/fenix-agent/fenix-agent.ini')).toBe('/etc/fenix-agent/fenix-agent.ini')
    expect(entrecomilla('C:\\ProgramData\\FenixAgent\\fenix-agent.ini')).toBe(
      'C:\\ProgramData\\FenixAgent\\fenix-agent.ini',
    )
  })

  it('el comando de Windows sale entero y utilizable', () => {
    // El caso completo, montado como lo monta la guía. Si alguien vuelve a quitar las
    // comillas con un replace global, esta línea deja de ser un comando y el test salta.
    const python = 'C:\\Program Files\\Python312\\python.exe'
    const ruta = 'C:\\ProgramData\\FenixAgent\\fenix-agent.ini'
    const comando = `${entrecomilla(python)} -m fenix_agent --config ${entrecomilla(ruta)}`

    expect(comando).toBe(
      '"C:\\Program Files\\Python312\\python.exe" -m fenix_agent --config C:\\ProgramData\\FenixAgent\\fenix-agent.ini',
    )
    // Lo que una shell lee como programa es la ruta entrecomillada entera, no media palabra.
    expect(comando.startsWith('"')).toBe(true)
    expect(comando.indexOf('python.exe" -m')).toBeGreaterThan(0)
  })

  it('el comando de Linux sale sin adornos', () => {
    const comando = `${entrecomilla('/usr/bin/python3')} -m fenix_agent --config ${entrecomilla(
      '/etc/fenix-agent/fenix-agent.ini',
    )}`
    expect(comando).toBe('python -m fenix_agent --config /etc/fenix-agent/fenix-agent.ini'.replace(
      'python -m',
      '/usr/bin/python3 -m',
    ))
    expect(comando).not.toContain('"')
  })
})
