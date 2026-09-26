import { useEffect } from 'react'
import { ShieldCheck } from 'lucide-react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { useAuth } from '../features/auth/useAuth'
import { AcceptInvitationPage } from '../features/auth/AcceptInvitationPage'
import { AuthPage } from '../features/auth/AuthPage'
import { VerifyEmailPage } from '../features/auth/VerifyEmailPage'
import { AdminPage } from '../features/admin/AdminPage'
import { LlmModelsPage } from '../features/admin/LlmModelsPage'
import { RequireSuperuser } from '../features/admin/RequireSuperuser'
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
          <Route
            path="/settings"
            element={<PlaceholderPage titleKey="navigation:settings" reasonKey="pending.settings" />}
          />
          <Route path="/admin/llm" element={<LlmModelsPage />} />
          <Route
            path="/admin"
            element={
              <RequireSuperuser>
                <AdminPage />
              </RequireSuperuser>
            }
          />
        </Route>
        <Route path="*" element={<Navigate to={token ? '/dashboard' : '/login'} replace />} />
      </Routes>
    </ToastProvider>
  )
}
