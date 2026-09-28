import { Plus } from 'lucide-react'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { LanguageSwitcher } from '../components/LanguageSwitcher'
import { SettingsSidebar } from '../components/SettingsSidebar'
import { Sidebar } from '../components/Sidebar'
import { useAuth } from '../features/auth/useAuth'
import { CreditBalancePill } from '../features/billing/CreditBalancePill'

/**
 * Rutas que ocupan el viewport completo y no hacen scroll de pagina.
 *
 * ## Por que una lista y no una condicion en el JSX
 *
 * Porque el motivo de que una ruta este aqui es una decision de diseno, y una decision de
 * diseno que solo existe en una expresion booleana se pierde en cuanto alguien anade la segunda
 * ruta y no la tercera. Con la lista a la vista, la pregunta "que mas deberia ir aqui" tiene
 * respuesta.
 *
 * ## Por que NO se aplica a todo el shell
 *
 * Porque el resto de paginas **necesitan** scroll de pagina: una tabla de vulnerabilidades con
 * doscientas filas tiene que poder bajar mas alla del pliegue. Poner `overflow: hidden` en el
 * shell las dejaria sin poder llegar al final, y el fallo seria invisible hasta que alguien
 * tiene una lista larga y no ve las ultimas filas.
 *
 * El modo se limita a lo que lo necesita de verdad: el chat, donde el scroll pertenece al hilo
 * de mensajes y no a la pagina.
 */
const RUTAS_DE_ALTURA_FIJA: ReadonlySet<string> = new Set(['/chat'])

export function ShellLayout() {
  // Un solo `useLocation` para las dos cosas que lo necesitan. Se declara aqui y no se
  // desdobla: dos llamadas al mismo hook para leer la misma ruta es una que alguien acaba
  // usando una y olvidando la otra, y el fallo es que la altura fija se aplica a una pagina y
  // el resto del shell cree que esta en otra.
  const location = useLocation()
  const alturaFija = RUTAS_DE_ALTURA_FIJA.has(location.pathname)
  const { t } = useTranslation('common')
  const navigate = useNavigate()
  const {
    organizations,
    selectedOrganizationId,
    user,
    selectOrganization,
    logout,
  } = useAuth()

  /**
   * ¿Estamos dentro de Ajustes?
   *
   * Se decide aquí y no dentro de `SettingsLayout` porque la columna izquierda la pinta el
   * shell: el sidebar principal es `position: fixed` y ocupa una caja **fuera** del flujo del
   * contenido. Un menú de Ajustes dentro de `SettingsLayout` se apila al lado de esa caja en
   * vez de ocuparla, y de ahí venía el doble sidebar que comprimía el contenido a media
   * pantalla.
   *
   * La comparación es por **prefijo de segmento** y no con `startsWith` a secas: si se
   * comparara la cadena entera, `/settings-something` entraría también. Hoy no existe esa
   * ruta, y cuando exista este sería el momento de romper.
   */
  const enAjustes =
    location.pathname === '/settings' || location.pathname.startsWith('/settings/')

  return (
    <div className={alturaFija ? 'app-shell app-shell-fija' : 'app-shell'}>
      {enAjustes ? (
        <SettingsSidebar />
      ) : (
        <Sidebar
          organizations={organizations}
          selectedOrganizationId={selectedOrganizationId}
          user={user}
          onSelectOrganization={selectOrganization}
          onCreateWorkspace={() => navigate('/settings')}
          onLogout={logout}
        />
      )}
      <div className="content-shell">
        <header className="topbar">
          <div className="topbar-status">
            <span className="status-dot" aria-hidden="true" />
            <span>{t('headerStatus')}</span>
          </div>
          <div className="topbar-actions">
            {/*
              El conmutador de idioma vive arriba y no junto al logo. En la cabecera del
              sidebar competía con el nombre del producto por el mismo corner, y en
              pantallas estrechas el bloque de marca se comía el ancho que necesita el
              selector de workspace, que es un control con función y no un ajuste.
            */}
            <LanguageSwitcher />
            <CreditBalancePill />
            <span className="topbar-workspace">{t('workspaceReady')}</span>
            <button className="primary-button" type="button" onClick={() => navigate('/pentests')}>
              <Plus size={17} aria-hidden="true" />
              <span>{t('newPentest')}</span>
            </button>
          </div>
        </header>
        <main className="main-content">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
