// ── localServer.js ────────────────────────────────────────────────────────────
// Talks to the local Python scraper server (localhost:7337).
// Falls back gracefully if the server is not running.

const LOCAL_URL = 'http://localhost:7832'
const TIMEOUT   = 5000

async function localReq(path, options = {}) {
  const ctrl  = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), options.timeout || TIMEOUT)
  try {
    const res = await fetch(LOCAL_URL + path, { ...options, signal: ctrl.signal })
    clearTimeout(timer)
    const text = await res.text()
    let data
    try { data = JSON.parse(text) } catch { throw new Error('Non-JSON: ' + text.slice(0,80)) }
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`)
    return data
  } catch (e) {
    clearTimeout(timer)
    if (e.name === 'AbortError') throw new Error('Local server timed out')
    throw e
  }
}

// ── Health check — is the Python server running? ──────────────────────────────
export async function checkServerHealth() {
  try {
    const data = await localReq('/health', { timeout: 3000 })
    return data.status === 'ok'
  } catch {
    return false
  }
}

// ── Start a one-shot scrape job ───────────────────────────────────────────────
export async function startScrapeJob({ novelId, startUrl, fromChapter, apiUrl, token, delayMs, maxChapters }) {
  return localReq('/scrape/start', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ novelId, startUrl, fromChapter, apiUrl, token, delayMs, maxChapters }),
    timeout: 8000,
  })
}

// ── Poll a scrape job ─────────────────────────────────────────────────────────
export async function pollScrapeJob(jobId, since = 0) {
  return localReq(`/scrape/status/${jobId}?since=${since}`, { timeout: 8000 })
}

// ── Start watcher ─────────────────────────────────────────────────────────────
export async function startWatcher({ novelId, startUrl, mode, checkSelector, intervalHours, delayMs, apiUrl, token }) {
  return localReq('/watch/start', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ novelId, startUrl, mode, checkSelector, intervalHours, delayMs, apiUrl, token }),
    timeout: 8000,
  })
}

// ── Stop watcher ──────────────────────────────────────────────────────────────
export async function stopWatcher(novelId) {
  return localReq('/watch/stop', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ novelId }),
    timeout: 8000,
  })
}

// ── Get watcher log ───────────────────────────────────────────────────────────
export async function getWatcherLog(novelId, since = 0) {
  return localReq(`/watch/log/${novelId}?since=${since}`, { timeout: 8000 })
}

// ── List active watchers ──────────────────────────────────────────────────────
export async function listWatchers() {
  return localReq('/watch/list', { timeout: 5000 })
}

// ── Detect latest chapter ─────────────────────────────────────────────────────
export async function detectLatest(url, selector = '') {
  return localReq('/detect', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ url, selector }),
    timeout: 20000,
  })
}
