import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import Icons from 'unplugin-icons/vite'
import { FileSystemIconLoader } from 'unplugin-icons/loaders'

// Мини-апп отдаёт веб-сервер бота по /constructor/ (app/constructor/runtime.py), API — /constructor/api.
// Все пути относительные (base: './', запросы на 'api/…'): приложение работает и по /constructor/,
// и на отдельном домене, который nginx проксирует на /constructor/.
// В dev /api проксируется в локально запущенного бота.
const API = process.env.API_URL || 'http://127.0.0.1:8080'

export default defineConfig({
  base: './',
  plugins: [
    react(),
    // Оригинальные иконки Telegram (src/icons/tg/*.svg) → React-компоненты: import X from '~icons/tg/<имя>'
    Icons({
      compiler: 'jsx',
      jsx: 'react',
      scale: 1,
      customCollections: { tg: FileSystemIconLoader('./src/icons/tg') },
    }),
  ],
  server: {
    host: true, // доступ с телефона в локальной сети
    port: 5173,
    allowedHosts: true, // для HTTPS-туннелей при тесте внутри Telegram
    proxy: { '/api': { target: API, changeOrigin: true, rewrite: (path) => `/constructor${path}` } },
  },
  build: { target: 'es2020', assetsInlineLimit: 8192 },
})
