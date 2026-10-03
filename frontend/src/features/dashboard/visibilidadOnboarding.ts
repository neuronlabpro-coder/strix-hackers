/**
 * Si el checklist de configuración inicial se pinta, y si desaparece.
 *
 * ## La decisión
 *
 * El bloque se quita de la pantalla cuando ya no queda nada pendiente. No se queda colapsado, no
 * se queda con una barra al 100 % y un «Configuración completa»: se quita.
 *
 * ## Por qué desaparecer y no «dejarlo ahí, ya está completo»
 *
 * Porque un checklist sin terminar es una instrucción y un checklist terminado ya no es una
 * instrucción: es un `100 %` con tres palomitas al lado de datos que sí importan. Ocupaba media
 * pantalla del Dashboard en un producto que ya estaba configurado, y en esa media pantalla lo
 * único que se puede hacer es taparla con otra cosa.
 *
 * ## Por qué «completo» no se equipara a «relevante»
 *
 * Porque no hay forma de saber si un cliente que no tiene repositorios va a tenerlos. El producto
 * también escanea dominios sin repositorio, así que un tenant que solo quiere eso tiene
 * `import_repository` pendiente para siempre. Y eso no es un motivo para dejar el bloque
 * clavado en pantalla: es un motivo para no inventar la regla de que el bloque solo aparece
 * cuando hay un repositorio pendiente, que sería una regla más y no más honesta.
 *
 * La regla que sí se puede comprobar es una: **si no queda nada pendiente, no hay nada que
 * orientar ahí.** Y lo que el tenant tiene de verdad —cuántos repositorios vigila— ya se dice
 * en otro sitio: en la tarjeta de métricas del Dashboard (`kpis.monitoredRepositories`) y en la
 * lista de Repositorios. Meterlo también en el checklist sería un segundo sitio que se puede
 * quedar viejo.
 *
 * ## Por qué vuelve a aparecer si algo se desconecta
 *
 * Porque el componente se monta siempre y decide en cada render, no una vez al entrar. Si la
 * credencial caduca o se borra un repositorio, `is_complete` pasa a `false` en la siguiente
 * lectura del estado y el bloque vuelve a salir solo. Esa es la razón de no quitar el componente
 * del `DashboardPage`: borrarlo del árbol lo habría dejado muerto para siempre.
 *
 * ## Por qué se consulta `is_complete` y no `completed_steps === total_steps`
 *
 * Porque son el mismo dato y `is_complete` es el que ya viaja en la respuesta. Usar los dos
 * daría dos fuentes para la misma verdad, y se desincronizarían en cuanto una cambiara.
 */

/** Estado del checklist tal y como lo devuelve `GET /api/v1/onboarding/status`. */
export interface EstadoDeOnboarding {
  is_complete: boolean
}

/**
 * Si el checklist se pinta.
 *
 * ## Por qué solo desaparece con un `is_complete` **verdadero**, y nunca por defecto
 *
 * Porque `null` significa que todavía no se sabe: la petición no ha vuelto, o volvió con error. Un
 * bloque que desaparece mientras no se sabe si hay algo pendiente se lleva por delante la
 * información justo cuando puede haber algo que hacer, que es el único momento en que hace
 * falta. Desaparecer es una afirmación —«no queda nada por hacer»— y esa afirmación solo se
 * puede hacer con un dato, nunca con su ausencia.
 */
export function debePintarseElChecklist(estado: EstadoDeOnboarding | null): boolean {
  return estado?.is_complete !== true
}