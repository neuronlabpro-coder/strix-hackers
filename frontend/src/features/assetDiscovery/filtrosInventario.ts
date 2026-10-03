import type { AssetType } from '../../types/assets'

/**
 * Los filtros del inventario de activos, en el estado en que los pinta la pantalla.
 *
 * ## Por qué vive en su propio módulo y no dentro del componente
 *
 * Porque un `.tsx` que exporta además de su componente una función o una constante rompe el
 * refresco en caliente de React, y `oxlint` lo avisa con `only-export-components`. El aviso es
 * cierto y el arreglo no es silenciar la regla: es que estas dos cosas no son un componente.
 *
 * Y porque la regla de «hay filtros» es aritmética pura sobre cinco campos, que es exactamente
 * lo que este proyecto puede comprobar sin montar React: sus pruebas son de lógica.
 */

/** Filtro de tipo. `'ALL'` no es un valor de la API: es «sin filtro». */
export type TypeFilter = AssetType | 'ALL'

export interface InventarioQuery {
  type: TypeFilter
  /** Identificador del dominio, no su nombre: es lo que viaja en la query. */
  domainId: string
  search: string
  /**
   * Las fechas son cadenas `AAAA-MM-DD` y no `Date`: es lo que da el `<input type="date">` y lo
   * que viaja en la query. Convertirlas a `Date` en el cliente obligaría a decidir una zona
   * horaria para un filtro por día natural, y esa decisión no está en ningún sitio del
   * proyecto. El backend corta el rango en UTC y ya se ha explicado por qué allí.
   */
  createdFrom: string
  createdTo: string
}

export const EMPTY_QUERY: InventarioQuery = {
  type: 'ALL',
  domainId: '',
  search: '',
  createdFrom: '',
  createdTo: '',
}

/**
 * Si hay algún filtro puesto, y por tanto si el botón de limpiar tiene algo que borrar.
 *
 * ## Por qué el texto va **recortado**
 *
 * Porque el servidor hace `.strip()` antes de aplicar el término, así que un campo con tres
 * espacios filtra exactamente lo mismo que uno vacío. Sin el `trim`, el botón aparecería sobre
 * una lista sin filtrar y al pulsarlo no cambiaría nada: el peor resultado posible para un
 * botón, porque enseña al operador que ese control a veces no hace nada.
 *
 * ## Por qué `'ALL'` **no** cuenta como filtro
 *
 * Porque `'ALL'` es la ausencia de filtro, no un valor de la API. Si contara, el botón
 * aparecería en la pantalla recién abierta y no limpiaría nada.
 */
export function hayFiltrosPuestos(filtros: InventarioQuery): boolean {
  return Boolean(
    filtros.type !== 'ALL' ||
      filtros.domainId !== '' ||
      filtros.search.trim() !== '' ||
      filtros.createdFrom !== '' ||
      filtros.createdTo !== '',
  )
}
