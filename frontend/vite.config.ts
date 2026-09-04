import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const BACKEND = 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
      // Binary MediaRecorder frames travel over this socket.
      '/ws': { target: BACKEND, ws: true, changeOrigin: true },
    },
  },
})
