// ── localScraper.js ───────────────────────────────────────────────────────────
// Talks to the Python scraper_server.py running on localhost:7832.
// Requests go through Vite's dev-server proxy at /api-local/* which forwards
// them to http://127.0.0.1:7832 server-side — no browser CORS issue possible.
// Falls back gracefully if the server is not running.

const SERVER = '/api-local'

// ── Check if the local server is running ─────────────────────────────────────
export async function checkServerHealth() {
  try {
    const ctrl = new AbortController()
    const t    = setTimeout(() => ctrl.abort(), 3000)
    const res  = await fetch(`${SERVER}/health`, { signal: ctrl.signal })
    clearTimeout(t)
    return res.ok
  } catch {
    return false
  }
}

// ── Fetch full health info including Gemini status ────────────────────────────
export async function fetchHealthInfo() {
  try {
    const ctrl = new AbortController()
    const t    = setTimeout(() => ctrl.abort(), 3000)
    const res  = await fetch(`${SERVER}/health`, { signal: ctrl.signal })
    clearTimeout(t)
    if (!res.ok) return null
    return res.json()  // { status, gemini: { enabled, model, ... }, ... }
  } catch {
    return null
  }
}

// ── Start a scrape job on the local server ────────────────────────────────────
// Returns job_id string
export async function startJob(params) {
  const res = await fetch(`${SERVER}/jobs`, {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.error || `Server error ${res.status}`)
  }
  const data = await res.json()
  return data.job_id
}

// ── Poll a job for status + full logs ─────────────────────────────────────────
export async function pollJob(jobId) {
  const res = await fetch(`${SERVER}/jobs/${jobId}`)
  if (!res.ok) throw new Error(`Job not found: ${jobId}`)
  return res.json()  // { status, stats, logs }
}

// ── Stream job logs via SSE ───────────────────────────────────────────────────
// onLog(entry)   called for each new log line  { msg, type, ts }
// onStats(stats) called on each stats update   { scraped, uploaded, skipped, errors }
// onDone(stats)  called when job completes
// Returns a cleanup function to close the stream
export function streamJob(jobId, { onLog, onStats, onDone }) {
  const es = new EventSource(`${SERVER}/jobs/${jobId}/stream`)

  es.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data)
      if (data.type === 'log')   onLog?.(data.entry)
      if (data.type === 'stats') onStats?.(data.stats)
      if (data.type === 'done')  { onDone?.(data.stats); es.close() }
      if (data.error)            { onLog?.({ msg: data.error, type: 'err', ts: Date.now() }); es.close() }
    } catch {}
  }

  es.onerror = () => {
    onLog?.({ msg: 'Stream connection lost', type: 'err', ts: Date.now() })
    es.close()
  }

  return () => es.close()
}

// ── Test a single chapter URL ─────────────────────────────────────────────────
export async function testUrl(url) {
  const res = await fetch(`${SERVER}/test-url`, {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ url }),
  })
  if (!res.ok) throw new Error(`Test failed: ${res.status}`)
  return res.json()
}
export async function detectLatestChapter(url, checkSelector = '') {
  const res = await fetch(`${SERVER}/detect`, {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ url, check_selector: checkSelector }),
  })
  if (!res.ok) throw new Error(`Detection failed: ${res.status}`)
  return res.json()  // { latest_num, chapter_url }
}

// =============================================================================
//  WATCHES API  — server-side 24/7 watcher
// =============================================================================

// GET /watches — returns { watches: [...], count }
export async function getServerWatches() {
  const res = await fetch(`${SERVER}/watches`)
  if (!res.ok) throw new Error(`Failed to fetch watches: ${res.status}`)
  return res.json()
}

// POST /watches — upsert a watch on the server (includes apiUrl + token)
export async function upsertServerWatch(watch) {
  const res = await fetch(`${SERVER}/watches`, {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(watch),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.error || `Server error ${res.status}`)
  }
  return res.json()
}

// DELETE /watches/<novelId>
export async function deleteServerWatch(novelId) {
  const res = await fetch(`${SERVER}/watches/${novelId}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(`Failed to delete watch: ${res.status}`)
  return res.json()
}

// POST /watches/<novelId>/start — enable scheduler for this watch
export async function startServerWatch(novelId) {
  const res = await fetch(`${SERVER}/watches/${novelId}/start`, { method: 'POST' })
  if (!res.ok) throw new Error(`Failed to start watch: ${res.status}`)
  return res.json()
}

// POST /watches/<novelId>/stop — disable scheduler for this watch
export async function stopServerWatch(novelId) {
  const res = await fetch(`${SERVER}/watches/${novelId}/stop`, { method: 'POST' })
  if (!res.ok) throw new Error(`Failed to stop watch: ${res.status}`)
  return res.json()
}

// POST /watches/<novelId>/run — trigger an immediate check (returns { job_id })
export async function runServerWatchNow(novelId) {
  const res = await fetch(`${SERVER}/watches/${novelId}/run`, { method: 'POST' })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.error || `Server error ${res.status}`)
  }
  return res.json()
}

// PATCH /watches/<novelId> — update runtime fields (lastChapter, lastChecked, etc.)
export async function patchServerWatch(novelId, fields) {
  const res = await fetch(`${SERVER}/watches/${novelId}`, {
    method:  'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(fields),
  })
  if (!res.ok) throw new Error(`Failed to patch watch: ${res.status}`)
  return res.json()
}

// PATCH /config — set concurrency limit on the Python server
export async function patchServerConfig(fields) {
  const res = await fetch(`${SERVER}/config`, {
    method:  'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(fields),
  })
  if (!res.ok) throw new Error(`Failed to patch config: ${res.status}`)
  return res.json()
}

// GET /queue — current queue depth + active job count
export async function getServerQueue() {
  const res = await fetch(`${SERVER}/queue`)
  if (!res.ok) throw new Error(`Failed to get queue: ${res.status}`)
  return res.json()  // { concurrency, active, queued, queue: [...] }
}
