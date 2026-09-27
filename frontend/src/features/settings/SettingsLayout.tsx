import { ArrowLeft, Lock } from 'lucide-react'
import { Link, NavLink, Outlet } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { useAuth } from '../auth/useAuth'
import { settingsNavigation } from './settingsNavigation'

/**
 * Layout de Ajustes: sub-sidebar de 220 px a la izquierda y contenido centrado.
 *
 * ## Por qué 220 px fijos y no una rejilla fluida
 *
 * Las cinco entradas tienen etiquetas de longitud muy distinta —"Help & Support" mide casi
 * el doble que "General"— y el candado de Enterprise añade un icono al final. Con un ancho
 * elástico, cambiar de sección cambiaría el ancho del submenú y el texto saltaría bajo el
 * cursor, que es la forma más rápida de hacer que un menú parezca tembloroso.
 *
 * Fijo también da una alineación vertical estable con la tabla de la derecha: la primera
 * fila de la tabla no se mueve al pasar de Members a Billing, y el ojo la encuentra en el
 * mismo sitio cada vez.
 *
 * ## Por qué el sub-sidebar **no** es `position: fixed`
 *
 * El sidebar principal sí lo es, porque acompaña al scroll de una aplicación larga. Este no:
 * acompaña a un bloque de contenido que cabe en una o dos pantallas, y un submenú fijo
 * haría que al desplazarse por una tabla larga de auditoría la columna de secciones
 * flotara sobre ella. Va en el flujo, con `position: sticky`, que da el comportamiento de
 * "acompaña" sin sacarlo de la caja.
 *
 * ## Por qué el candado no oculta la entrada
 *
 * Ver la nota de `settingsNavigation`. Ocultarla sería más limpio y peor: el cliente no
 * sabría que existe un producto que no compró, y una oferta que no se ve no se vende.
 *
 * ## Por qué el superusuario ve todo
 *
 * Porque administra la plataforma entera. Un superusuario al que se le bloqueara Audit Logs
 * en su propio workspace no podría hacer su trabajo, y no por una razón de seguridad: el
 * servidor filtra el rastro por tenant de todos modos. El candado informa; no autoriza.
 */
export function SettingsLayout() {
  const { t } = useTranslation('settings')
  const { user, organizations, selectedOrganizationId } = useAuth()

  const organization = organizations.find((item) => item.id === selectedOrganizationId) ?? null
  const esSuperusuario = user?.is_superuser === true
  const esEnterprise = organization?.plan_tier === 'ENTERPRISE'

  /**
   * Si esta entrada se puede abrir.
   *
   * Es una función y no una bandera del array porque depende de dos cosas —el plan del
   * workspace **y** si quien mira es superusuario— que solo conoce el layout. La lista
   * declara el requisito; aquí se resuelve.
   */
  const bloqueada = (item: (typeof settingsNavigation)[number]): boolean =>
    item.requiresEnterprise === true && !esEnterprise && !esSuperusuario

  return (
    <div className="settings-shell">
      <nav className="settings-subnav" aria-label={t('sections.title')}>
        <div className="settings-subnav-header">
          <Link className="settings-back-link" to="/dashboard">
            <ArrowLeft size={15} aria-hidden="true" />
            <span>{t('sections.backToPanel')}</span>
          </Link>
          <p className="settings-subnav-title">{t('sections.title')}</p>
        </div>

        <ul className="settings-subnav-list">
          {settingsNavigation.map((item) => {
            const bloqueadaAhora = bloqueada(item)
            if (bloqueadaAhora) {
              return (
                <li key={item.path}>
                  {/*
                    Un `<span>` y no un `<NavLink>` cuando está bloqueada.

                    Es la diferencia entre un enlace que no lleva a ningún sitio y un control
                    que parece que sí. Un `NavLink` con `aria-disabled` seguiría siendo
                    enfocable y navegable con el teclado, y un usuario de teclado acabaría
                    en una vista que le dice que no tiene acceso —que es una respuesta a una
                    pregunta que no ha hecho. Un `span` no es enfocable y no hay a dónde ir.

                    El candado y el texto van envueltos en un `title` para que el motivo sea
                    accesible al puntero, que es donde se mira.
                  */}
                  <span
                    className="settings-subnav-link settings-subnav-link-locked"
                    title={t('locked.tooltip')}
                  >
                    <item.icon size={16} aria-hidden="true" />
                    <span>{t(item.labelKey)}</span>
                    <Lock className="settings-subnav-lock" size={13} aria-hidden="true" />
                  </span>
                </li>
              )
            }
            return (
              <li key={item.path}>
                <NavLink
                  className="settings-subnav-link"
                  to={item.path}
                  end={item.end === true}
                >
                  <item.icon size={16} aria-hidden="true" />
                  <span>{t(item.labelKey)}</span>
                </NavLink>
              </li>
            )
          })}
        </ul>
      </nav>

      <div className="settings-content">
        <main className="settings-content-inner">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
