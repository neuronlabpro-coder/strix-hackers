import { useEffect, useRef, useState } from 'react'
import { ShieldCheck } from 'lucide-react'
import { Link, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { acceptInvitationRequest } from '../../lib/api'
import { useAuth } from './useAuth'

export function AcceptInvitationPage() {
  const { t } = useTranslation('auth')
  const { token: accessToken } = useAuth()
  const [searchParams] = useSearchParams()
  const invitationToken = searchParams.get('token') ?? ''
  const [status, setStatus] = useState<'idle' | 'loading' | 'success' | 'error'>(
    invitationToken && accessToken ? 'idle' : 'error',
  )
  const attemptedToken = useRef<string | null>(null)

  useEffect(() => {
    if (!invitationToken || !accessToken || attemptedToken.current === invitationToken) {
      return
    }

    attemptedToken.current = invitationToken
    setStatus('loading')
    void acceptInvitationRequest(invitationToken, accessToken)
      .then(() => setStatus('success'))
      .catch(() => setStatus('error'))
  }, [accessToken, invitationToken])

  const loginPath = `/login?returnTo=${encodeURIComponent(
    `/invitations/accept?token=${invitationToken}`,
  )}`

  return (
    <main className="auth-layout">
      <section className="auth-card" aria-labelledby="accept-invitation-title">
        <div className="brand-lockup">
          <div className="brand-mark" aria-hidden="true">
            <ShieldCheck size={18} />
          </div>
          <div>
            <p className="eyebrow">{t('secureAccess')}</p>
            <h1 id="accept-invitation-title">{t('acceptInvitationTitle')}</h1>
          </div>
        </div>
        {status === 'loading' || status === 'idle' ? <p>{t('acceptingInvitation')}</p> : null}
        {status === 'success' ? (
          <p className="form-success" role="status">
            {t('invitationAccepted')}
          </p>
        ) : null}
        {status === 'error' ? (
          <p className="form-error" role="alert">
            {t('invalidInvitation')}
          </p>
        ) : null}
        {accessToken ? (
          <Link className="auth-switch" to="/dashboard">
            {t('backToDashboard')}
          </Link>
        ) : (
          <Link className="auth-switch" to={loginPath}>
            {t('backToLogin')}
          </Link>
        )}
      </section>
    </main>
  )
}
