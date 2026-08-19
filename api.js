// ── api.js ────────────────────────────────────────────────────────────────────
// All calls to the idenwebstudio backend.
// Credentials are stored in localStorage under "ns_api_url" and "ns_token".

export function getCredentials() {
  return {
    apiUrl: localStorage.getItem('ns_api_url') || '',
    token:  localStorage.getItem('ns_token')   || '',
  }
}

export function saveCredentials(apiUrl, token) {
  localStorage.setItem('ns_api_url', apiUrl)
  localStorage.setItem('ns_token', token)
}

async function request(path, options = {}) {
  const { apiUrl, token } = getCredentials()
  if (!apiUrl) throw new Error('API URL not configured')

  const headers = { 'Content-Type': 'application/json' }
  if (token) headers['Authorization'] = 'Bearer ' + token
  Object.assign(headers, options.headers || {})

  const res = await fetch(apiUrl.replace(/\/$/, '') + path, {
    ...options,
    headers,
  })
  const text = await res.text()
  let data
  try { data = JSON.parse(text) } catch { throw new Error('Non-JSON: ' + text.slice(0, 120)) }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`)
  return data
}

async function requestForm(path, method, formData) {
  const { apiUrl, token } = getCredentials()
  if (!apiUrl) throw new Error('API URL not configured')
  const headers = {}
  if (token) headers['Authorization'] = 'Bearer ' + token
  const res = await fetch(apiUrl.replace(/\/$/, '') + path, { method, headers, body: formData })
  const text = await res.text()
  let data
  try { data = JSON.parse(text) } catch { throw new Error('Non-JSON: ' + text.slice(0, 120)) }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`)
  return data
}

// ── Auth ──────────────────────────────────────────────────────────────────────
export const login = (email, password) =>
  request('/api/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) })

export const getMe = () => request('/api/auth/me')

// ── Novels ────────────────────────────────────────────────────────────────────
export const getNovels = (params = {}) =>
  request('/api/novels?' + new URLSearchParams({ limit: 100, sort: 'added', ...params }))

export const getNovelById = (novelId) =>
  request(`/api/novels/${novelId}`)

export const getNovelBySlug = (slug) =>
  request(`/api/novels/slug/${slug}`)

// formData should be a FormData object (supports cover image upload)
export const createNovel = (formData) =>
  requestForm('/api/novels', 'POST', formData)

export const updateNovel = (novelId, formData) =>
  requestForm(`/api/novels/${novelId}`, 'PUT', formData)

export const deleteNovel = (novelId) =>
  request(`/api/novels/${novelId}`, { method: 'DELETE' })

// ── Chapters ──────────────────────────────────────────────────────────────────
export const getNovelChapters = (novelId) =>
  request(`/api/novels/${novelId}/chapters`)

export const getChapter = (novelId, chapterNum) =>
  request(`/api/novels/${novelId}/chapters/${chapterNum}`)

// Single chapter create (used for one-off uploads)
export const createChapter = (novelId, data) =>
  request(`/api/novels/${novelId}/chapters`, {
    method: 'POST',
    body: JSON.stringify(data),
  })

// Bulk chapter import — send all scraped chapters in one request.
// chapters: [{ number, title, content }, ...]
// skipDuplicates: if true, existing chapter numbers are silently skipped (default true)
// Returns: { created, skipped, errors, message }
export const bulkImportChapters = (novelId, chapters, skipDuplicates = true) =>
  request(`/api/novels/${novelId}/chapters/bulk`, {
    method: 'POST',
    body: JSON.stringify({ chapters, skipDuplicates }),
  })

export const updateChapter = (novelId, chapterNum, data) =>
  request(`/api/novels/${novelId}/chapters/${chapterNum}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  })

export const deleteChapter = (novelId, chapterNum) =>
  request(`/api/novels/${novelId}/chapters/${chapterNum}`, { method: 'DELETE' })

// ── Comments ──────────────────────────────────────────────────────────────────
export const getComments = (novelId, params = {}) =>
  request(`/api/novels/${novelId}/comments?` + new URLSearchParams(params))

export const postComment = (novelId, text, chapterNum = null) =>
  request(`/api/novels/${novelId}/comments`, {
    method: 'POST',
    body: JSON.stringify({ text, chapterNum }),
  })

export const deleteComment = (novelId, commentId) =>
  request(`/api/novels/${novelId}/comments/${commentId}`, { method: 'DELETE' })

export const likeComment = (novelId, commentId) =>
  request(`/api/novels/${novelId}/comments/${commentId}/like`, { method: 'POST' })

// ── Misc ──────────────────────────────────────────────────────────────────────
export const rateNovel = (novelId, rating) =>
  request(`/api/novels/${novelId}/rate`, {
    method: 'POST',
    body: JSON.stringify({ rating }),
  })

export const checkHealth = () =>
  request('/api/health')
