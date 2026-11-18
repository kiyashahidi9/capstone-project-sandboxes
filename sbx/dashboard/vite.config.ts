import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// forward /api/* to the API server, so the browser avoids CORS
// the dev server adds the admin key itself, so the key never reaches the browser
export default defineConfig(({ mode }) => {
  // read SANDBOX_API and SANDBOX_ADMIN_KEY from the .env at the repo root ('' = no VITE_ prefix needed)
  const env = loadEnv(mode, '../..', '')

  return {
    plugins: [react()],
    server: {
      proxy: {
        '/api': {
          target: env.SANDBOX_API || 'http://localhost:8000',
          rewrite: (path) => path.replace(/^\/api/, ''),
          headers: { Authorization: `Bearer ${env.SANDBOX_ADMIN_KEY ?? ''}` },
        },
      },
    },
  }
})
