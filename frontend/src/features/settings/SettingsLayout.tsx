import { Outlet } from 'react-router-dom'

/**
 * Layout de Ajustes: solo el contenido.
 *
 * ## Por qué ya no lleva submenú
 *
 * Porque se movió al shell. El menú de secciones está en `SettingsSidebar`, que ocupa la
 * columna izquierda **en lugar** del sidebar principal, y no en una columna de 220 px
 * apilada al lado.
 *
 * La razón de fondo es de espacio, no de gusto. Con los dos menús, 260 px de navegación
 * general más 220 px de secciones dejaban el contenido en algo más de la mitad del ancho
 * útil, y el rótulo «Nombre del workspace» se partía en dos líneas pegadas a su valor. El
 * texto apretado no es un detalle cosmético: es la señal de que la pantalla está rota, y se
 * lee como rotura aunque el contenido esté bien alineado.
 *
 * ## Por qué conserva la envoltura de una sola columna
 *
 * Porque `settings-content` limitaba el ancho de las tablas. Quitar el submenú no significa
 * quitar el límite: sin él, la tabla de miembros y la de auditoría se estirarían a 1440 px y
 * las líneas quedarían ilegibles de tan largas. El límite era correcto; lo que sobraba era la
 * columna que lo acompañaba.
 */
export function SettingsLayout() {
  return (
    <div className="settings-shell">
      <div className="settings-content">
        <main className="settings-content-inner">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
