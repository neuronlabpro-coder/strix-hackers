/**
 * Si el checklist de configuración inicial se sigue pintando.
 *
 * ## El defecto que estas pruebas existen para no dejar volver
 *
 * El Dashboard se quedaba con «Primeros pasos · Configuración inicial · 3 de 3 completados» y un
 * «Configuración completa» en un producto que ya estaba configurado. Media pantalla dedicada a un
 * `100 %` que no dice nada que el resto del Dashboard no diga ya —el número de repositorios
 * vigilados está en `kpis.monitoredRepositories`, y el detalle en la lista de Repositorios—.
 *
 * ## Por qué desaparece y no se queda colapsado
 *
 * Está razonado en `visibilidadOnboarding.ts`. Aquí lo que se fija es que la regla no se degrade:
 * que un `is_complete` de verdad lo quite, y que **nada más** lo quite.
 *
 * ## Por qué se prueba la función y no el componente
 *
 * Porque lo que se decide es una comparación sobre el estado del checklist, y porque este
 * proyecto no tiene entorno de DOM. Un test de render exigiría además montar `useAuth` y el
 * `fetch`, para comprobar una línea.
 */

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

import { debePintarseElChecklist } from './visibilidadOnboarding'

const RAIZ = fileURLToPath(new URL('../../../../', import.meta.url))
const COMPONENTE = readFileSync(
  join(RAIZ, 'frontend/src/features/dashboard/OnboardingChecklist.tsx'),
  'utf8',
)
const PAGINA = readFileSync(
  join(RAIZ, 'frontend/src/features/dashboard/DashboardPage.tsx'),
  'utf8',
)

describe('debePintarseElChecklist', () => {
  it('con todo completado no se pinta', () => {
    expect(debePintarseElChecklist({ is_complete: true })).toBe(false)
  })

  it('con algo pendiente se pinta', () => {
    expect(debePintarseElChecklist({ is_complete: false })).toBe(true)
  })

  it('sin estado no se quita: no se sabe si queda algo pendiente', () => {
    // `null` es «la petición no ha vuelto, o volvió con error». Un bloque que desaparece
    // mientras no se sabe se lleva la información justo cuando puede haber algo que hacer. Y
    // además dejaría el hueco con un salto: aparecería y desaparecería al vuelo.
    expect(debePintarseElChecklist(null)).toBe(true)
  })

  it('desaparecer necesita un sí explícito, no la ausencia de un no', () => {
    // La forma que de verdad cuela el fallo es la que exige que el estado **esté** para
    // desaparecer: `estado !== null && estado.is_complete === true`. Es la que se escribe sin
    // pensar dos veces y esconde el bloque entero mientras la petición no ha vuelto. Se
    // comprueba aquí mismo, sobre la forma alternativa, para que el motivo quede escrito donde
    // se lee y para que se vea que la diferencia está exactamente en el `null`.
    const conLaFormaMala = (estado: { is_complete: boolean } | null): boolean =>
      estado !== null && estado.is_complete === true
    expect(conLaFormaMala(null), 'la forma mala esconde el checklist sin estado').toBe(false)
    expect(debePintarseElChecklist(null), 'la forma buena lo enseña').toBe(true)
  })

  it('el componente consulta la regla y no tiene su propia copia del criterio', () => {
    expect(COMPONENTE).toContain('debePintarseElChecklist')
    expect(COMPONENTE).toMatch(/if \(!debePintarseElChecklist\(status\)\) \{\s*return null/)
    // Y no queda la rama de «configuración completa» dentro del JSX, que con el `return null`
    // de arriba es código inalcanzable. Una rama que no se puede ejecutar hace dudar de si el
    // `return null` funciona.
    expect(COMPONENTE).not.toContain("t('complete')")
  })

  it('el bloque sigue montado en el Dashboard, que es lo que le permite volver', () => {
    // Si se quitara el componente del árbol, «vuelve a aparecer si algo se desconecta» no sería
    // cierto: nunca volvería. Esta es la prueba de que la desaparición es por render y no una
    // modificación del árbol.
    expect(PAGINA).toContain('<OnboardingChecklist />')
  })
})