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
    // Feature dossiers pin the hashed bundle filenames they shipped, and the
    // docs validator requires those paths to exist in the CURRENT tree as
    // well as at the delivery revision — so a rebuild must never delete its
    // predecessors' bundles. Superseded assets accumulate by design.
    emptyOutDir: false,
  },
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.test.{js,jsx}'],
  },
})
