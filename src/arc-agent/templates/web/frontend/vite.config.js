import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const backendPort = Number(process.env.ARC_WEB_PORT || '__ARC_WEB_PORT__')

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
  ],
  server: {
    proxy: {
      '/api': `http://127.0.0.1:${backendPort}`
    }
  }
})
