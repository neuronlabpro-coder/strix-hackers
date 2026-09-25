import { useState, type FormEvent } from 'react'
import { ShieldCheck } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { LanguageSwitcher } from '../../components/LanguageSwitcher'
import { useAuth } from './useAuth'

interface AuthPageProps {
  mode: 'login' | 'register'
}

export function AuthPage({ mode }: AuthPageProps) {
  const { t } = useTranslation(['auth', 'errors'])
  const navigate = useNavigate()
  const { login, register } = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [fullName, setFullName] = useState('')
  const [organizationName, setOrganizationName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const isRegisterMode = mode === 'register'

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError(null)
    setIsSubmitting(true)

    try {
      if (isRegisterMode) {
        await register({
          email,
          password,
          full_name: fullName,
          organization_name: organizationName,
        })
      } else {
        await login({ email, password })
      }
      navigate('/dashboard', { replace: true })
    } catch {
      setError(t(isRegisterMode ? 'errors:registrationFailed' : 'errors:invalidCredentials'))
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <main className="auth-layout">
      <section className="auth-card" aria-labelledby="auth-title">
        <div className="auth-language">
          <LanguageSwitcher />
        </div>
        <div className="brand-lockup">
          <div className="brand-mark" aria-hidden="true">
            <ShieldCheck size={18} />
          </div>
          <div>
            <p className="eyebrow">{t('auth:secureAccess')}</p>
            <h1 id="auth-title">{t(isRegisterMode ? 'auth:registerTitle' : 'auth:loginTitle')}</h1>
          </div>
        </div>
        <p className="auth-subtitle">
          {t(isRegisterMode ? 'auth:registerSubtitle' : 'auth:loginSubtitle')}
        </p>

        <form className="auth-form" onSubmit={handleSubmit}>
          <div className="form-field">
            <label htmlFor="email">{t('auth:email')}</label>
            <input
              id="email"
              name="email"
              type="email"
              autoComplete="email"
              placeholder={t('auth:emailPlaceholder')}
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              required
              aria-required="true"
              aria-invalid={Boolean(error)}
              aria-describedby={error ? 'auth-error' : undefined}
            />
          </div>

          {isRegisterMode ? (
            <>
              <div className="form-field">
                <label htmlFor="full-name">{t('auth:fullName')}</label>
                <input
                  id="full-name"
                  name="full_name"
                  type="text"
                  autoComplete="name"
                  placeholder={t('auth:fullNamePlaceholder')}
                  value={fullName}
                  onChange={(event) => setFullName(event.target.value)}
                  required
                  aria-required="true"
                />
              </div>
              <div className="form-field">
                <label htmlFor="organization-name">{t('auth:organizationName')}</label>
                <input
                  id="organization-name"
                  name="organization_name"
                  type="text"
                  autoComplete="organization"
                  placeholder={t('auth:organizationNamePlaceholder')}
                  value={organizationName}
                  onChange={(event) => setOrganizationName(event.target.value)}
                  required
                  aria-required="true"
                />
              </div>
            </>
          ) : null}

          <div className="form-field">
            <label htmlFor="password">{t('auth:password')}</label>
            <input
              id="password"
              name="password"
              type="password"
              autoComplete={isRegisterMode ? 'new-password' : 'current-password'}
              placeholder={t('auth:passwordPlaceholder')}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
              aria-required="true"
              aria-invalid={Boolean(error)}
              aria-describedby={error ? 'auth-error' : undefined}
            />
          </div>

          {error ? (
            <p className="form-error" id="auth-error" role="alert">
              {error}
            </p>
          ) : null}

          <button className="primary-button auth-submit" type="submit" disabled={isSubmitting}>
            {t(isSubmitting ? 'auth:submitting' : isRegisterMode ? 'auth:submitRegister' : 'auth:submitLogin')}
          </button>
        </form>

        <Link className="auth-switch" to={isRegisterMode ? '/login' : '/register'}>
          {t(isRegisterMode ? 'auth:switchToLogin' : 'auth:switchToRegister')}
        </Link>
      </section>
    </main>
  )
}
