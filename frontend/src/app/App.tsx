import { useEffect } from 'react'
import { ShieldCheck } from 'lucide-react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { useAuth } from '../features/auth/useAuth'
import { AcceptInvitationPage } from '../features/auth/AcceptInvitationPage'
import { AuthPage } from '../features/auth/AuthPage'
import { VerifyEmailPage } from '../features/auth/VerifyEmailPage'
import { DashboardPage } from '../features/dashboard/DashboardPage'
import { RepositoriesPage } from '../features/repositories/RepositoriesPage'
import { PlaceholderPage } from '../features/shared/PlaceholderPage'
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
        <Route path="/pentests" element={<PlaceholderPage titleKey="navigation:pentests" />} />
        <Route path="/issues" element={<PlaceholderPage titleKey="navigation:issues" />} />
        <Route
          path="/pr-reviews"
          element={<PlaceholderPage titleKey="navigation:prReviews" />}
        />
        <Route path="/repositories" element={<RepositoriesPage />} />
        <Route path="/knowledge" element={<PlaceholderPage titleKey="navigation:knowledge" />} />
        <Route path="/settings" element={<PlaceholderPage titleKey="navigation:settings" />} />
      </Route>
      <Route path="*" element={<Navigate to={token ? '/dashboard' : '/login'} replace />} />
    </Routes>
  )
}
