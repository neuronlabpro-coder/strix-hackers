/**
 * Qué mensaje enseña el modal cuando la carga del inventario remoto falla.
 *
 * ## El defecto que este módulo existe para no dejar volver
 *
 * El `catch` de `loadInventory` tenía dos ramas: `409` para «conecta primero una credencial» y
 * **todo lo demás** para `modal.unsupportedProvider`, «este proveedor todavía no tiene conector».
 * Todo lo demás incluía el `502` que devuelve la ruta cuando el proveedor rechaza la credencial,
 * que es lo que ocurrió con el token OAuth del workspace de demostración: caducado dos días antes.
 *
 * El resultado en pantalla era una sola ventana diciendo «GitHub no tiene conector» mientras la
 * lista de repositorios de debajo tenía tres entradas de GitHub y el Dashboard, a la vez, decía
 * «3 de 3 completados». Tres afirmaciones que no pueden ser ciertas a la vez, y el diagnóstico
 * que se le da a quien lo ve —«no hay conector»— es el único de los tres que no se puede
 * comprobar mirando los datos.
 *
 * ## Por qué una tabla y no un `if` en el componente
 *
 * Porque el reparto entre códigos es una decisión, y una decisión sin nombre no se revisa: el
 * siguiente que añada un caso lo mete en la rama de `else` y vuelve a pasar lo mismo. Con una
 * tabla, la pregunta al añadir una fila es «¿qué código da el backend para este caso y quién lo
 * arregla?», y las dos respuestas están en el backend, escritas al lado de donde se contesta.
 *
 * ## Por qué devuelve una **clave** de traducción y no un texto
 *
 * Por R1. Un texto aquí quedaría en un componente y saldría en español en un panel que tiene
 * inglés. El componente la pasa por `t()`, y el texto que ve el usuario sale siempre de
 * `locales/`, nunca del `detail` del backend: el `detail` es un mensaje de servidor, en el idioma
 * del servidor, y el panel tiene dos.
 */

/**
 * Claves de `locales/*\/repositories.json` que el modal puede enseñar tras un fallo.
 *
 * Viven en `modal` porque es el bloque del que las lee el comprobador de claves faltantes: una
 * clave fuera de ahí sería una que quien traducise no encuentra al buscar el diálogo.
 */
export type ClaveDeErrorDeInventario =
  | 'modal.noCredential'
  | 'modal.credentialExpired'
  | 'modal.credentialRejected'
  | 'modal.providerRateLimit'
  | 'modal.unsupportedProvider'
  | 'modal.providerUnavailable'
  | 'modal.credentialUnreadable'
  | 'modal.inventoryFailed'

/**
 * Código de estado a causa, según `backend/apps/repositories/router.py`:
 *
 * - `409` — no hay credencial de este proveedor en esta organización (`_open_client`).
 * - `410` — la credencial guardada caducó (`_exigir_credencial_vigente`). Es el único código que
 *   dice el **por qué**: la fecha está en la base y se comprueba antes de llamar al proveedor.
 * - `401` — el proveedor no reconoce la credencial o le faltan permisos (`_translate_client_error`).
 *   Antes contestaba `502`, y por eso no se distinguía de una caída del proveedor.
 * - `429` — el proveedor agotó su cuota temporal (`_translate_client_error`).
 * - `501` — el proveedor no tiene conector de gestión (`_SUPPORTED_MANAGEMENT_PROVIDERS`).
 * - `500` — la credencial guardada existe pero no se pudo descifrar: el problema es del servidor.
 * - `502` — el proveedor no está disponible, o la operación no se pudo completar.
 */
const CLAVE_POR_ESTADO: ReadonlyMap<number, ClaveDeErrorDeInventario> = new Map([
  [409, 'modal.noCredential'],
  [410, 'modal.credentialExpired'],
  [401, 'modal.credentialRejected'],
  [429, 'modal.providerRateLimit'],
  [501, 'modal.unsupportedProvider'],
  [500, 'modal.credentialUnreadable'],
  [502, 'modal.providerUnavailable'],
])

/**
 * Traduce el fallo de la carga del inventario a la clave que hay que enseñar.
 *
 * ## Por qué `null` —sin respuesta del servidor— no es «sin conector»
 *
 * Porque `null` es un fallo de red, del navegador, o una petición cancelada, y ninguno de ellos
 * dice nada del conector. Caer en «no hay conector» cuando la petición ni siquiera ha salido es
 * la misma mentira que la que había, con un disfraz distinto: convierte un problema de red en
 * un diagnóstico de producto que no lleva a ninguna parte.
 *
 * ## Por qué el `default` no es `unsupportedProvider`
 *
 * Por lo mismo, y por una razón más: un código que no está en la tabla significa que el backend
 * ha cambiado. Afirmar «no hay conector» en ese caso es decir algo que este cliente no sabe. El
 * texto honesto para un caso desconocido es el que dice que no se pudo saber, y quien lo lea
 * tiene un número con el que mirar los logs.
 */
export function claveDeErrorDeInventario(estado: number | null): ClaveDeErrorDeInventario {
  return CLAVE_POR_ESTADO.get(estado ?? -1) ?? 'modal.inventoryFailed'
}