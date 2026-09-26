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
import { PlaceholderPage } from '../features/shared/PlaceholderPage'
import { ToastProvider } from '../features/shared/ToastProvider'
import { ShellLayout } from './ShellLayout'

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

function ProtectedShell() {
  const { token } = useAuth()
  return token ? <ShellLayout /> : <Navigate to="/login" replace />
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
          <Route
            path="/chat"
            element={<PlaceholderPage titleKey="navigation:chat" reasonKey="pending.chat" />}
          />
          <Route
            path="/domains"
            element={<PlaceholderPage titleKey="navigation:domains" reasonKey="pending.domains" />}
          />
          <Route
            path="/asset-discovery"
            element={
              <PlaceholderPage
                titleKey="navigation:assetDiscovery"
                reasonKey="pending.assetDiscovery"
              />
            }
          />
          <Route
            path="/supply-chain"
            element={
              <PlaceholderPage
                titleKey="navigation:supplyChain"
                reasonKey="pending.supplyChain"
              />
            }
          />
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
          <Route
            path="/integrations"
            element={
              <PlaceholderPage
                titleKey="navigation:integrations"
                reasonKey="pending.integrations"
              />
            }
          />
          <Route path="/api-access" element={<ApiAccessPage />} />
          <Route path="/billing" element={<BillingPage />} />
          <Route
            path="/settings"
            element={<PlaceholderPage titleKey="navigation:settings" reasonKey="pending.settings" />}
          />
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
          </Route>
        </Route>
        <Route path="*" element={<Navigate to={token ? '/dashboard' : '/login'} replace />} />
      </Routes>
    </ToastProvider>
  )
}
