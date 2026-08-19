import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  // Load .env so we can read VITE_KN_URL at config time
  const env = loadEnv(mode, process.cwd(), '')

  // On Ubuntu: set VITE_KN_URL=https://your-vercel-app.vercel.app in .env
  // On Windows (local dev): leave unset → defaults to localhost:3000
  const knTarget = env.VITE_KN_URL || 'http://localhost:3000'

  return {
    plugins: [react()],
    server: {
      host: '0.0.0.0',   // ← LAN-accessible (http://192.168.x.x:5174)
      port: 5174,
      proxy: {
        // All /api-local/* → Python scraper server
        '/api-local': {
          target: 'http://127.0.0.1:7832',
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api-local/, ''),
        },
        // All /api-kn/* → Knight Novel app (Vercel or local)
        '/api-kn': {
          target: knTarget,
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api-kn/, ''),
        },
      },
    },
  }
})
