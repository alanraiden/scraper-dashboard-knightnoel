// ── api.js ────────────────────────────────────────────────────────────────────
// All calls to the Knight Novel backend (http://localhost:3000).
//
// IMPORTANT: Browser requests use the Vite dev-server proxy at /api-kn/*
// which forwards them to http://localhost:3000 server-side — zero CORS issues.
// The Python scraper_server.py talks directly to http://localhost:3000 using
// the x-scraper-key header (no proxy needed — it's server-to-server).
//
// The scraper API key is stored in localStorage under "kn_scraper_key".
// The backend URL is stored under "kn_api_url" (default: http://localhost:3000).

// ── Proxy prefix — all browser fetches go through Vite's /api-kn proxy ───────
const PROXY = '/api-kn'

export function getCredentials() {
  return {
    apiUrl:     localStorage.getItem('kn_api_url')      || 'http://localhost:3000',
    scraperKey: localStorage.getItem('kn_scraper_key')  || '',
  }
}

export function saveCredentials(apiUrl, scraperKey) {
  localStorage.setItem('kn_api_url',      apiUrl)
  localStorage.setItem('kn_scraper_key',  scraperKey)
  // Keep these so the Python server can read them from the same localStorage values
  localStorage.setItem('ns_api_url', apiUrl)
  localStorage.setItem('ns_token',   scraperKey)
}

// Internal fetch helper — always goes through the Vite proxy (no CORS)
async function request(path, options = {}) {
  const { scraperKey } = getCredentials()

  const headers = {
    'Content-Type': 'application/json',
    'x-scraper-key': scraperKey,
  }
  Object.assign(headers, options.headers || {})

  // PROXY + path routes through Vite → localhost:3000 without CORS
  const res = await fetch(PROXY + path, {
    ...options,
    headers,
  })
  const text = await res.text()
  let data
  try { data = JSON.parse(text) } catch { throw new Error('Non-JSON: ' + text.slice(0, 120)) }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`)
  return data
}

// ── Auth ──────────────────────────────────────────────────────────────────────
// Validates the scraper API key against Knight Novel's /api/scraper/auth.
// Uses the proxy so no CORS issues even on first login.
export async function login(apiUrl, scraperKey) {
  // apiUrl param kept for API compat but we always use the proxy here.
  const res = await fetch(PROXY + '/api/scraper/auth', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ apiKey: scraperKey }),
  })
  const text = await res.text()
  let data
  try { data = JSON.parse(text) } catch { throw new Error('Non-JSON response from server') }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`)
  return data   // { ok: true, user: { name, role } }
}

export const getMe = () => request('/api/scraper/auth', { method: 'POST',
  body: JSON.stringify({ apiKey: getCredentials().scraperKey }) })
  .then(d => d.user)

// ── Novels ────────────────────────────────────────────────────────────────────
// KN returns { novels, total } — same shape the dashboard expects
export const getNovels = () => request('/api/scraper/novels')

export const getNovelBySlug = (slug) =>
  request(`/api/novels/${slug}`).then(d => d.novel)

// ── Chapters ──────────────────────────────────────────────────────────────────
// novelSlug: the novel's slug field (e.g. "shadow-slave")
// Returns [{ number, title }, ...]
export const getNovelChapters = (novelSlug) =>
  request(`/api/scraper/novels/${novelSlug}/chapters`)
  .then(d => d.chapters || [])

// Bulk import chapters — the main upload path.
// POST /api/scraper/novels/:slug/chapters  (POST on the chapters route = bulk import)
// chapters: [{ number, title, content }]
// Returns: { created, skipped, errors, message }
export const bulkImportChapters = (novelSlug, chapters, skipDuplicates = true) =>
  request(`/api/scraper/novels/${novelSlug}/chapters`, {
    method: 'POST',
    body:   JSON.stringify({ chapters, skipDuplicates }),
  })

// Single chapter — wraps bulk with 1 item
export const createChapter = (novelSlug, data) =>
  bulkImportChapters(novelSlug, [data], false)

// ── Health check ──────────────────────────────────────────────────────────────
export const checkHealth = async () => {
  const { scraperKey } = getCredentials()
  if (!scraperKey) return false
  try {
    const res = await fetch(PROXY + '/api/scraper/novels', {
      headers: { 'x-scraper-key': scraperKey },
      signal: AbortSignal.timeout(4000),
    })
    return res.ok
  } catch {
    return false
  }
}
