// Build straight into ../static: the python server (crew/server/app.py) serves
// `/` from static/index.html and assets from /static/*, so `npm run build` here
// is the whole deploy step — no node needed at crew runtime. base '/static/'
// makes the emitted asset URLs match the server's static route.
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))

export default defineConfig({
  plugins: [react()],
  base: '/static/',
  build: {
    outDir: path.resolve(here, '..', 'static'),
    emptyOutDir: true,
  },
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.test.{js,jsx}'],
  },
})
