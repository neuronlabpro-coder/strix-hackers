import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), ['VITE_'])
  const apiProxyTarget = env.VITE_API_PROXY_TARGET || 'http://localhost:8000'

  return {
    plugins: [react(), tailwindcss()],
    server: {
      host: '127.0.0.1',
      port: 5173,
      /*
       * El prefijo lleva **barra al final**: `'/api/'`, no `'/api'`.
       *
       * ## Por qué
       *
       * ## Por qué importa la barra
       *
       * ## Por qué importa
       *
       * ## Por qué
       *
       * ## Por qué la barra final
       *
       * ## Por qué
       *
       * ## Por qué `'/api'` se come `/api-access`
       *
       * ## Por qué
       *
       * ## Por qué
       *
       * ## Por qué `'/api'` tambié se come la ruta de la propia SPA
       *
       * Con `'/api'` el proxy casa con cualquier ruta que **empiece** por `/api`, y `/api-access` es
       * una de ellas: la página de acceso a la API de la plataforma devolvía 404 en desarrollo,
       * porque Vite intentó.forwardearla al backend en lugar de servir la SPA. En la captura se ve
       * como una página de nueve kilobytes.
       *
       * ## Por qué no se renombra la ruta
       *
       * ## Por qu00e9 no se toca la ruta
       *
       * ## Por qu00e9
       *
       * ## Por qué se arregla el proxy y no la ruta
       *
       * ## Por qué
       *
       * ## Por qué
       *
       * ## Por qué
       */
      proxy: {
        '^/api/': {
          target: apiProxyTarget,
          changeOrigin: true,
        },
      },
    },
  }
})
