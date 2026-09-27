import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'

import App from './app/App'
import { AuthProvider } from './features/auth/AuthContext'
import './i18n'
import './styles/index.css'
import './styles/settings.css'
import './styles/billing.css'
import './styles/support.css'
import './styles/assets.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <App />
      </AuthProvider>
    </BrowserRouter>
  </StrictMode>,
)
