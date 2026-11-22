import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The dev server proxies all API paths to the FastAPI backend on :8000, so the UI can use
// same-origin relative URLs (no CORS juggling in the browser during the demo).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/records': 'http://localhost:8000',
      '/portal': 'http://localhost:8000',
      '/webhooks': 'http://localhost:8000',
      '/metrics': 'http://localhost:8000',
      '/__control': 'http://localhost:8000',
    },
  },
})
