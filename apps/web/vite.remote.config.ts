import { renameSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import react from '@vitejs/plugin-react'
import { defineConfig, type Plugin } from 'vite'

// The remote page is deployed on its own origin, and Google sign-in returns to that
// origin's root. Serve and publish remote.html as the root document.
const OUT_DIR = resolve(import.meta.dirname, 'dist-remote')

const remoteIndex = (): Plugin => ({
  name: 'txintrade-remote-index',
  configureServer(server) {
    server.middlewares.use((request, _response, next) => {
      if (request.url === '/' || request.url?.startsWith('/?')) request.url = `/remote.html${request.url.slice(1)}`
      next()
    })
  },
  closeBundle() {
    renameSync(resolve(OUT_DIR, 'remote.html'), resolve(OUT_DIR, 'index.html'))
    // Security headers for the hosted page; it talks only to the txinTrade cloud.
    const api = (process.env.VITE_TXINTRADE_CLOUD_ORIGIN ?? 'https://api.txintrade.com').replace(/\/$/, '')
    const csp = [
      "default-src 'self'", "script-src 'self'", "style-src 'self' 'unsafe-inline'", "img-src 'self' data:",
      "font-src 'self'", `connect-src 'self' ${api} ${api.replace(/^http/, 'ws')}`,
      "frame-ancestors 'none'", "base-uri 'self'", "form-action 'self'", "object-src 'none'",
    ].join('; ')
    writeFileSync(resolve(OUT_DIR, '_headers'), [
      '/*', `  Content-Security-Policy: ${csp}`, '  X-Content-Type-Options: nosniff',
      '  Referrer-Policy: no-referrer', '  Permissions-Policy: camera=(), microphone=(), geolocation=()',
      // Browsers that have seen the page once go straight to HTTPS afterwards.
      '  Strict-Transport-Security: max-age=31536000; includeSubDomains',
      // The default revalidation, plus no-transform: Cloudflare must not inject its analytics beacon.
      '  Cache-Control: public, max-age=0, must-revalidate, no-transform', '',
    ].join('\n'))
  },
})

export default defineConfig({
  plugins: [react(), remoteIndex()],
  // The cloud session cookie is set for "localhost"; open the page on the same host name.
  server: { host: 'localhost', port: 5174, strictPort: true },
  preview: { host: 'localhost', port: 5174, strictPort: true },
  build: { outDir: OUT_DIR, rollupOptions: { input: 'remote.html' } },
})
