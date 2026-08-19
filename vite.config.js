import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',   // ← LAN-accessible (http://192.168.x.x:5174)
    port: 5174,
    proxy: {
      // All /api-local/* requests are forwarded to the Python scraper server.
      '/api-local': {
        target: 'http://127.0.0.1:7832',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api-local/, ''),
      },
      // All /api-kn/* requests are forwarded to the Knight Novel Next.js app (Vercel or local).
      '/api-kn': {
        target: 'http://localhost:3000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api-kn/, ''),
      },
    },
  },
})
