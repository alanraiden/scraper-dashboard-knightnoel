import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      // All /api-local/* requests are forwarded to the Python scraper server.
      // This runs server-side in Node, so there is zero browser CORS involvement.
      '/api-local': {
        target: 'http://127.0.0.1:7832',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api-local/, ''),
      },
      // All /api-kn/* requests are forwarded to the Knight Novel Next.js dev server.
      // This avoids browser CORS issues when the dashboard (port 5174) calls KN (port 3000).
      '/api-kn': {
        target: 'http://localhost:3000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api-kn/, ''),
      },
    },
  },
})
