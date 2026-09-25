import { useEffect, useState } from 'react'
import { ShieldCheck } from 'lucide-react'
import { Link, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { verifyEmailRequest } from '../../lib/api'

export function VerifyEmailPage() {
  const { t } = useTranslation(['auth', 'errors'])
  const [searchParams] = useSearchParams()
  const token = searchParams.get('token') ?? ''
  const [status, setStatus] = useState<'idle' | 'loading' | 'success' | 'error'>(
    token ? 'loading' : 'idle',
  )

  useEffect(() => {
    if (!token || status !== 'idle') {
      return
    }

    void verifyEmailRequest(token)
      .then((response) => setStatus(response.verified ? 'success' : 'error'))
      .catch(() => setStatus('error'))
  }, [status, token])

  return (
    <main className="auth-layout">
      <section className="auth-card" aria-labelledby="verify-email-title">
        <div className="brand-lockup">
          <div className="brand-mark" aria-hidden="true">
            <ShieldCheck size={18} />
          </div>
          <div>
            <p className="eyebrow">{t('auth:secureAccess')}</p>
            <h1 id="verify-email-title">{t('auth:verifyEmailTitle')}</h1>
          </div>
        </div>
        {status === 'loading' || status === 'idle' ? <p>{t('auth:verifyingEmail')}</p> : null}
        {status === 'success' ? (
          <p className="form-success" role="status">
            {t('auth:emailVerified')}
          </p>
        ) : null}
        {status === 'error' ? (
          <p className="form-error" role="alert">
            {t('errors:invalidVerificationToken')}
          </p>
        ) : null}
        <Link className="auth-switch" to="/login">
          {t('auth:backToLogin')}
        </Link>
      </section>
    </main>
  )
}
