/**
 * Si un fallo de red significa que la sesión ya no vale.
 *
 * ## El fallo que provocaba
 *
 * `AuthContext` validaba la sesión al arrancar con `loadOrganizations(...).catch(() => {
 * clearSession(); setToken(null) })`. El `.catch` no miraba **qué** había fallado, así que
 * cualquier rechazo destruía la sesión:
 *
 * - un `502` del proxy o un `500` del backend;
 * - un `429` por límite de peticiones;
 * - un corte de red, que lanza `TypeError` y no `ApiError`;
 * - un `fetch` que el navegador cancela al montar y desmontar rápido.
 *
 * Ninguno de esos dice nada sobre la validez del token, y todos terminaban en la pantalla
 * de login con la sesión borrada del almacenamiento. El caso que más se complained era
 * **volver de GitHub o GitLab**: el usuario pasa fuera de la aplicación —que es
 * precisamente cuando la red puede haber cambiado— y la primera petición al volver es la
 * que falla. El flujo OAuth no rompía la sesión: **`fetch` no puede mirar el destino para
 * no romperla**.
 *
 * ## Por qué `403` sí cuenta
 *
 * `401` es inequívoco: el token no vale. `403` significa que el token es válido pero no
 * llega: en este caso, un workspace al que el usuario ya no pertenece, o una cuenta
 * desactivada. En los dos, volver a intentarlo con el mismo token no va a funcionar, así
 * que la sesión **sí** está muerta y reiniciar sesión es la respuesta correcta.
 *
 * ## Por qué el resto no
 *
 * Un `5xx`, un `429` y un error de red son **transitorios por definición**: la misma
 * petición puede funcionar un segundo después. Borrar el token en esos casos convierte un
 * parpadeo de la red en un cierre de sesión, que es lo que había que evitar.
 *
 * La diferencia con el logout explícito es que aquí la decisión no la toma la persona: si
 * el backend no está disponible, nadie puede decidir, y lo que hace el código por defecto
 * no puede ser "desconectar a todo el mundo".
 */
export function isSessionInvalid(error: unknown): boolean {
  // `fetch` lanza `TypeError` en un fallo de red y en un `AbortError`. Ninguno de los dos
  // sabe nada del token: se consideran transitorios y la sesión sobrevive.
  if (error instanceof TypeError) {
    return false
  }

  const status = (error as { status?: unknown } | null)?.status
  if (typeof status !== 'number') {
    // Un error sin código de estado no viene del servidor. Puede ser un `ApiError` de otra
    // versión del cliente o un fallo de una dependencia: se trata como transitorio, que es
    // la opción que no destruye nada que no se pueda recuperar.
    return false
  }

  return status === 401 || status === 403
}
