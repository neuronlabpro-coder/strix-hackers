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
import './styles/chat.css'
import './styles/supplyChain.css'
import './styles/knowledge.css'
// El último a propósito: es la capa que es dueña del vocabulario visual de la consola
// de plataforma, y sobreescribe cuatro clases compartidas que otras hojas redeclaran con
// valores distintos. El porqué está al principio del fichero.
import './styles/console.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <App />
      </AuthProvider>
    </BrowserRouter>
  </StrictMode>,
)
