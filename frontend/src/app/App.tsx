import { Suspense, lazy, useEffect } from 'react'
import { ShieldCheck } from 'lucide-react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { useAuth } from '../features/auth/useAuth'
import { AcceptInvitationPage } from '../features/auth/AcceptInvitationPage'
import { AuthPage } from '../features/auth/AuthPage'
import { VerifyEmailPage } from '../features/auth/VerifyEmailPage'
import { RequireSuperuser } from '../features/admin/RequireSuperuser'
import { BillingPage } from '../features/billing/BillingPage'
import { DashboardPage } from '../features/dashboard/DashboardPage'
import { ApiAccessPage } from '../features/api_access/ApiAccessPage'
import { IssuesPage } from '../features/issues/IssuesPage'
import { VulnerabilityDetailPage } from '../features/issues/VulnerabilityDetailPage'
import { KnowledgePage } from '../features/knowledge/KnowledgePage'
import { CvePage } from '../features/cve/CvePage'
import { PrReviewsPage } from '../features/prReviews/PrReviewsPage'
import { PentestsPage } from '../features/pentests/PentestsPage'
import { PentestRunPage } from '../features/pentests/PentestRunPage'
import { RepositoriesPage } from '../features/repositories/RepositoriesPage'
// `PlaceholderPage` sigue en uso por las rutas que todavia no tienen pantalla. El chat y el
// supply chain salieron de aqui; `/assets`, `/containers` y `/networks` siguen en el mismo
// estado.
import { PlaceholderPage } from '../features/shared/PlaceholderPage'
import { SettingsLayout } from '../features/settings/SettingsLayout'
import { SettingsGeneralPage } from '../features/settings/SettingsGeneralPage'
import { SettingsAuditPage } from '../features/settings/SettingsAuditPage'
import { SettingsMembersPage } from '../features/settings/SettingsMembersPage'
import { SettingsBillingPage } from '../features/settings/SettingsBillingPage'
import { SupportTicketsPage } from '../features/support/SupportTicketsPage'
import { SupportTicketPage } from '../features/support/SupportTicketPage'
import { ToastProvider } from '../features/shared/ToastProvider'
import { ShellLayout } from './ShellLayout'

/**
 * La superficie de ataque se carga bajo demanda, por el mismo motivo que la consola de
 * SuperAdmin y no por moda.
 *
 * ## Por qué
 *
 * Dos pantallas completas más en el bundle principal lo empujaron por encima del umbral que
 * avisa en el build. Medido: el paquete que descarga el 100 % de los clientes pasó de 495 a
 * 504 kB para servir dos rutas a las que se entra desde un escaneo, nunca desde el arranque.
 *
 * Subir `chunkSizeWarningLimit` habría silenciado el aviso sin mejorar nada: el peso sigue
 * ahí y lo sigue pagando quien no entra nunca. Partirlo lo quita del camino crítico de
 * verdad.
 *
 * ## Por qué aquí y no en todas las pantallas
 *
 * Porque casi nadie abre `/domains` sin venir del panel de un escaneo. Un cliente que entra
 * por `/dashboard` no debería pagar por la lista de activos ni por el inventario de
 * superficie; y quien sí entra, ya está navegando y un fragmento de red no se nota.
 */
const DomainsPage = lazy(() =>
  import('../features/domains/DomainsPage').then((m) => ({ default: m.DomainsPage })),
)
const IntegrationsPage = lazy(() =>
  import('../features/integrations/IntegrationsPage').then((m) => ({ default: m.IntegrationsPage })),
)
const AssetDiscoveryPage = lazy(() =>
  import('../features/assetDiscovery/AssetDiscoveryPage').then((m) => ({
    default: m.AssetDiscoveryPage,
  })),
)
/**
 * El chat se carga aparte, con la misma razon que las tres anteriores y no por capricho: es lo
 * que mantiene el bundle de entrada por debajo del limite de 500 KB de Vite, y eso no es un
 * numero arbitrario. Es el peso de React, del router, de i18next y de los iconos; el chat anade
 * seis componentes y un renderizador de Markdown, y con el import estatico se iba al bloque
 * principal, que es el que se descarga entero en la pantalla de login.
 *
 * Subir `chunkSizeWarningLimit` habria hecho desaparecer el aviso sin tocar nada de lo que lo
 * causa. Partir el bloque hace que el usuario que entra por el login no descargue un renderizador
 * de Markdown que no va a usar.
 */
const ChatPage = lazy(() =>
  import('../features/chat/ChatPage').then((m) => ({ default: m.ChatPage })),
)
/**
 * El supply chain tambien se carga aparte.
 *
 * Es la misma razon que el chat y el mismo numero: 500 KB en el bloque de entrada. Este modulo
 * pesa porque trae la tabla, los siete chips de ecosistema y los tres estados de vulnerabilidad,
 * y quien entra por el login no va a ver ninguno de los tres.
 *
 * Subir `chunkSizeWarningLimit` habria hecho desaparecer el aviso sin tocar nada de lo que lo
 * causa. Partir el bloque hace que el bundle que se descarga al entrar no incluya una pantalla
 * que el usuario no ha abierto.
 */
const SupplyChainPage = lazy(() =>
  import('../features/supply_chain/SupplyChainPage').then((m) => ({ default: m.SupplyChainPage })),
)

/**
 * La consola de SuperAdmin se carga bajo demanda.
 *
 * ## Por qué
 *
 * Seis secciones que solo ve un superusuario iban dentro del bundle principal, que es el
 * que descarga **todo** cliente en cada arranque. Medido: 72 kB de más en el chunk que
 * carga el 100 % de los usuarios para el 0 % que puede entrar a esas pantallas.
 *
 * ## Por qué no se aplica a las pantallas de cliente
 *
 * Una pantalla de las que usa todo el mundo no gana nada con esto: sigue estando en el
 * bundle, solo llega un poco después, y el usuario ve un hueco donde antes veía contenido.
 * Aquí el caso es el contrario —la usa casi nadie y pesa lo suyo— y por eso el corte va
 * justo en este punto y no en otro.
 */
const AdminLayout = lazy(() =>
  import('../features/admin/AdminLayout').then((m) => ({ default: m.AdminLayout })),
)
const AdminOverviewPage = lazy(() =>
  import('../features/admin/AdminOverviewPage').then((m) => ({ default: m.AdminOverviewPage })),
)
const AdminTenantsPage = lazy(() =>
  import('../features/admin/AdminTenantsPage').then((m) => ({ default: m.AdminTenantsPage })),
)
const AdminUsersPage = lazy(() =>
  import('../features/admin/AdminUsersPage').then((m) => ({ default: m.AdminUsersPage })),
)
const AdminSalesPage = lazy(() =>
  import('../features/admin/AdminSalesPage').then((m) => ({ default: m.AdminSalesPage })),
)
const AdminAuditPage = lazy(() =>
  import('../features/admin/AdminAuditPage').then((m) => ({ default: m.AdminAuditPage })),
)
const LlmModelsPage = lazy(() =>
  import('../features/admin/LlmModelsPage').then((m) => ({ default: m.LlmModelsPage })),
)
const AdminTicketsPage = lazy(() =>
  import('../features/admin/AdminTicketsPage').then((m) => ({ default: m.AdminTicketsPage })),
)

function LoadingScreen() {
  const { t } = useTranslation('common')
  return (
    <main className="loading-screen" aria-live="polite">
      <span className="loading-mark" aria-hidden="true">
        <ShieldCheck size={18} />
      </span>
      <p>{t('loading')}</p>
    </main>
  )
}

/**
 * Recarga de sesión en marcha, sin tocar el token.
 *
 * Va en `ProtectedShell` y no en el error del propio `AuthContext` porque la pantalla de
 * "no pude cargar" es del shell: el shell es quien sabe que sigue habiendo sesión. Que la
 * decisión de reintentar la tome quien la ofrece evita que la aplicación tenga un camino
 * para reintentarlo por su cuenta y se repita en bucle.
 */

/**
 * Marcador de carga de las secciones que llegan bajo demanda.
 *
 * Reutiliza la misma pantalla de carga que el arranque en vez de inventar un esqueleto de
 * tarjetas: son el mismo estado —"el código aún no ha llegado"— y un esqueleto aquí
 * mostraría seis huecos de aspecto correto que el usuario tardaría en distinguir de una
 * tabla vacía de verdad.
 */
function LazySection() {
  return <LoadingScreen />
}

/**
 * Puerta de las páginas del panel.
 *
 * Distingue tres estados y no dos. La distinción que importa es `loadFailed`: con un token
 * presente que **no** se pudo validar, la respuesta no es mandar al login, porque el token
 * sigue ahí y funciona. Mandar al login en ese caso obliga a identificarse de nuevo para
 * conseguir un token idéntico, y es exactamente lo que pasaba al volver de GitHub o
 * GitLab cuando la primera petición fallaba por la red.
 */
function ProtectedShell() {
  const { t } = useTranslation('common')
  const { token, isLoading, loadFailed, retrySession } = useAuth()

  if (token) {
    if (loadFailed) {
      return (
        <section className="page-section">
          <div className="empty-card">
            <h2>{t('sessionLoadFailedTitle')}</h2>
            <p>{t('sessionLoadFailedDescription')}</p>
            <button className="primary-button" type="button" onClick={retrySession}>
              <span>{t('sessionRetry')}</span>
            </button>
          </div>
        </section>
      )
    }
    return isLoading ? <LoadingScreen /> : <ShellLayout />
  }
  return <Navigate to="/login" replace />
}

export default function App() {
  const { t } = useTranslation('common')
  const { token, isLoading } = useAuth()

  useEffect(() => {
    document.title = t('appName')
  }, [t])

  if (isLoading) {
    return <LoadingScreen />
  }

  return (
    <ToastProvider>
      <Routes>
        <Route
          path="/login"
          element={token ? <Navigate to="/dashboard" replace /> : <AuthPage mode="login" />}
        />
        <Route
          path="/register"
          element={token ? <Navigate to="/dashboard" replace /> : <AuthPage mode="register" />}
        />
        <Route path="/verify-email" element={<VerifyEmailPage />} />
        <Route path="/invitations/accept" element={<AcceptInvitationPage />} />
        <Route element={<ProtectedShell />}>
          <Route path="/dashboard" element={<DashboardPage />} />
          <Route path="/pentests" element={<PentestsPage />} />
          <Route path="/pentests/:runId" element={<PentestRunPage />} />
          <Route path="/issues" element={<IssuesPage />} />
          <Route path="/issues/:vulnerabilityId" element={<VulnerabilityDetailPage />} />
          <Route path="/repositories" element={<RepositoriesPage />} />
          <Route path="/knowledge" element={<KnowledgePage />} />
          <Route path="/cve" element={<CvePage />} />
          <Route path="/pr-reviews" element={<PrReviewsPage />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/domains" element={<DomainsPage />} />
          <Route path="/asset-discovery" element={<AssetDiscoveryPage />} />
          <Route path="/supply-chain" element={<SupplyChainPage />} />
          <Route
            path="/containers"
            element={
              <PlaceholderPage
                titleKey="navigation:containers"
                reasonKey="pending.containers"
              />
            }
          />
          <Route
            path="/networks"
            element={<PlaceholderPage titleKey="navigation:networks" reasonKey="pending.networks" />}
          />
          <Route path="/integrations" element={<IntegrationsPage />} />
          <Route path="/api-access" element={<ApiAccessPage />} />
          <Route path="/billing" element={<BillingPage />} />
          <Route path="/settings" element={<SettingsLayout />}>
            <Route index element={<Navigate to="/settings/general" replace />} />
            <Route path="general" element={<SettingsGeneralPage />} />
            <Route path="audit-logs" element={<SettingsAuditPage />} />
            <Route path="members" element={<SettingsMembersPage />} />
            <Route path="billing" element={<SettingsBillingPage />} />
            <Route path="support" element={<SupportTicketsPage />} />
            <Route
              path="support/:ticketId"
              element={
                <Suspense fallback={<LazySection />}>
                  <SupportTicketPage />
                </Suspense>
              }
            />
          </Route>
          {/*
            La consola de SuperAdmin tiene su propio layout y su propia puerta. Vive
            **fuera** de `ProtectedShell` a propósito: el shell de cliente resuelve el
            tenant en cada ruta y arrastra `X-Organization-Id` a todas, y la consola cruza
            tenants. Anidarla dentro habría que compensar ese tenant en cinco pantallas,
            y la compensación es justo lo que se olvidaría.
          */}
          <Route
            path="/admin"
            element={
              <RequireSuperuser>
                <Suspense fallback={<LazySection />}>
                  <AdminLayout />
                </Suspense>
              </RequireSuperuser>
            }
          >
            <Route
              index
              element={
                <Suspense fallback={<LazySection />}>
                  <AdminOverviewPage />
                </Suspense>
              }
            />
            <Route
              path="tenants"
              element={
                <Suspense fallback={<LazySection />}>
                  <AdminTenantsPage />
                </Suspense>
              }
            />
            <Route
              path="users"
              element={
                <Suspense fallback={<LazySection />}>
                  <AdminUsersPage />
                </Suspense>
              }
            />
            <Route
              path="sales"
              element={
                <Suspense fallback={<LazySection />}>
                  <AdminSalesPage />
                </Suspense>
              }
            />
            <Route
              path="audit"
              element={
                <Suspense fallback={<LazySection />}>
                  <AdminAuditPage />
                </Suspense>
              }
            />
            <Route
              path="llm"
              element={
                <Suspense fallback={<LazySection />}>
                  <LlmModelsPage />
                </Suspense>
              }
            />
            <Route
              path="tickets"
              element={
                <Suspense fallback={<LazySection />}>
                  <AdminTicketsPage />
                </Suspense>
              }
            />
          </Route>
        </Route>
        <Route path="*" element={<Navigate to={token ? '/dashboard' : '/login'} replace />} />
      </Routes>
    </ToastProvider>
  )
}
