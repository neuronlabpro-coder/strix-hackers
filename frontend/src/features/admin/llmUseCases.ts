/**
 * Los casos de uso que admite el catálogo de modelos.
 *
 * ## Por qué viven en su propio archivo y no junto al formulario de alta
 *
 * Dos motivos, y los dos son de mantenimiento:
 *
 * 1. **Para que los usen dos sitios.** El alta lo elige y la tabla de la consola lo reasigna
 *    en un modelo existente. Declarar la lista en cualquiera de los dos obligaría al otro a
 *    importarla de un módulo de componente, que es como una lista de opciones acaba
 *    duplicada con una diferencia de un caso.
 *
 * 2. **Para no romper el refresco en caliente.** Un `.tsx` que exporta un componente y además
 *    una constante obliga a Vite a recargar la página entera al guardar, porque no puede
 *    invalidar solo el módulo. En desarrollo, con la tabla de modelos abierta, se nota.
 *
 * ## Por qué el orden es el de lectura
 *
 * Primero el transversal, después el bucle ofensivo de arriba hacia abajo por coste —el
 * `DEEP_PENTEST` es el caro y el `QUICK_SCAN` el barato, y es como se comparan—, y el
 * último la generación de parche, que es el más diferenciado en su objetivo: no compite por
 * coste con los otros porque no hace lo mismo.
 */

import type { LLMUseCase } from '../../types/api'

export const LLM_USE_CASES: readonly LLMUseCase[] = [
  'ALL',
  'QUICK_SCAN',
  'DEEP_PENTEST',
  'AUTOFIX',
]
