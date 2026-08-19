// ── scraper.js ────────────────────────────────────────────────────────────────
// Browser-side chapter scraper with multi-proxy fallback +
// Madara (WordPress manga/novel theme) direct API extractor.
//
// Many sites (LunoxScans, etc.) use the Madara WordPress theme which loads
// chapter content via JavaScript AJAX after the page loads.
// A CORS proxy only gets the raw HTML snapshot — no JS runs — so content
// appears empty. We fix this by calling the WordPress REST/AJAX APIs directly.

// ── CORS proxy chain ──────────────────────────────────────────────────────────
const PROXIES = [
  {
    name: 'allorigins',
    build: (url) => `https://api.allorigins.win/get?url=${encodeURIComponent(url)}`,
    extract: async (res) => { const j = await res.json(); return j.contents },
  },
  {
    name: 'corsproxy.io',
    build: (url) => `https://corsproxy.io/?${encodeURIComponent(url)}`,
    extract: async (res) => res.text(),
  },
  {
    name: 'codetabs',
    build: (url) => `https://api.codetabs.com/v1/proxy?quest=${encodeURIComponent(url)}`,
    extract: async (res) => res.text(),
  },
  {
    name: 'htmldriven',
    build: (url) => `https://cors.htmldriven.com/?url=${encodeURIComponent(url)}`,
    extract: async (res) => { try { const j = await res.json(); return j.body } catch { return res.text() } },
  },
]

let preferredProxyIndex = 0
const FETCH_TIMEOUT_MS  = 18000

// ── Content selectors ─────────────────────────────────────────────────────────
const CONTENT_SELECTORS = [
  // Madara theme
  'div.reading-content', 'div.text-left', 'div#chapter-content',
  'div.chapter-content', 'div.entry-content',
  // Generic
  'article.post-content', 'div.chapter-body', 'div#content',
  'div.storytext', 'div.chapter', 'div.post-content', 'div.main-content',
  'div[class*="chapter-content"]', 'div[id*="chapter-content"]',
  'div[class*="chapter-text"]', 'div#novel-content',
]

const MIN_WORDS = 150

const PAYWALL_SIGNALS = [
  /you\s+need\s+to\s+be\s+logged\s+in/i,
  /please\s+log\s+in/i,
  /not\s+a\s+member\?/i,
  /unlock\s+all\s+chapters/i,
  /get\s+instant\s+access/i,
  /join\s+now\s*🔓/i,
  /log\s+in\s+to\s+(read|unlock|access|view)/i,
  /sign\s+in\s+to\s+(read|unlock|continue)/i,
  /members?\s+only/i,
  /premium\s+chapter/i,
  /chapter\s+is\s+locked/i,
  /unlock\s+this\s+chapter/i,
  /subscribe\s+to\s+(read|unlock|access)/i,
  /login\s+or\s+create\s+an\s+account/i,
  /create\s+an\s+account\s+to/i,
  /this\s+is\s+a\s+premium\s+chapter/i,
  /unlock\s+chapter/i,
  /becomes\s+free\s+in/i,
  /available\s+in\s+\d+\s+day/i,
]

const NEXT_SELECTORS = [
  'a.next_page', "a[rel='next']", 'a.next-chap', 'a#next_chap',
  'a.btn-next', "a[title*='Next']", "a[title*='next']",
  'a[class*="next"]', '.nav-next a', '.chapter-nav .next a',
]

const PREV_SELECTORS = [
  'a.prev_page', "a[rel='prev']", 'a.prev-chap', 'a#prev_chap',
  'a.btn-prev', "a[title*='Prev']", "a[title*='prev']", "a[title*='Previous']",
  'a[class*="prev"]', '.nav-prev a', '.chapter-nav .prev a',
]

// ══════════════════════════════════════════════════════════════════════════════
//  MADARA (WordPress theme) DIRECT API EXTRACTOR
//  Used by LunoxScans, ReaperScans, Asura Scans, and hundreds of other sites
// ══════════════════════════════════════════════════════════════════════════════

function getMadaraOrigin(url) {
  try { return new URL(url).origin } catch { return null }
}

// Detect if a page is a Madara WordPress theme site
function isMadaraSite(doc, url) {
  const html = doc.documentElement?.innerHTML || ''
  return (
    html.includes('wp-manga') ||
    html.includes('madara') ||
    html.includes('/wp-content/themes/madara') ||
    html.includes('manga-reading') ||
    html.includes('admin-ajax.php') ||
    // URL pattern: /series/{slug}/chapter-{n}/
    /\/series\/[^/]+\/chapter-\d+/i.test(url)
  )
}

// Extract WordPress nonce from page HTML (needed for AJAX calls on some sites)
function extractNonce(html) {
  const patterns = [
    /["']nonce["']\s*:\s*["']([a-f0-9]{10})["']/i,
    /nonce\s*=\s*["']([a-f0-9]{10})["']/i,
    /ajaxNonce["']\s*:\s*["']([a-f0-9]{10})["']/i,
    /"nonce":"([a-f0-9]{10})"/i,
  ]
  for (const p of patterns) {
    const m = html.match(p)
    if (m) return m[1]
  }
  return null
}

// Extract chapter ID from Madara page HTML
function extractChapterId(html) {
  const patterns = [
    /["']chapter_id["']\s*:\s*["']?(\d+)/i,
    /data-id=["'](\d+)["'][^>]*class=["'][^"']*chapter/i,
    /class=["'][^"']*chapter[^"']*["'][^>]*data-id=["'](\d+)["']/i,
    /"chapter":{"id":(\d+)/i,
    /wp_manga_chapter_id\s*=\s*(\d+)/i,
  ]
  for (const p of patterns) {
    const m = html.match(p)
    if (m) return m[1]
  }
  return null
}

// Strategy 1: WordPress REST API — GET /wp-json/wp/v2/posts?slug=...
async function tryWpRestApi(origin, chapterSlug, opts = {}) {
  const { onLog } = opts
  // Try a few slug formats
  const slugVariants = [
    chapterSlug,
    chapterSlug.replace(/chapter-(\d+)/, 'chapter-$1'),
  ]
  for (const slug of slugVariants) {
    const apiUrl = `${origin}/wp-json/wp/v2/posts?slug=${encodeURIComponent(slug)}&_fields=content,title`
    onLog?.(`[Madara] Trying WP REST API: ${apiUrl}`, 'dim')
    try {
      // Try direct first (works if site has CORS open), then proxy
      let data = null
      try {
        const ctrl = new AbortController()
        const t = setTimeout(() => ctrl.abort(), 8000)
        const res = await fetch(apiUrl, { signal: ctrl.signal })
        clearTimeout(t)
        if (res.ok) data = await res.json()
      } catch {}

      if (!data) {
        // Try via proxy
        const proxyUrl = PROXIES[preferredProxyIndex].build(apiUrl)
        const ctrl = new AbortController()
        const t = setTimeout(() => ctrl.abort(), 12000)
        const res = await fetch(proxyUrl, { signal: ctrl.signal })
        clearTimeout(t)
        if (res.ok) {
          const raw = await PROXIES[preferredProxyIndex].extract(res)
          try { data = JSON.parse(raw) } catch {}
        }
      }

      if (Array.isArray(data) && data.length > 0 && data[0].content?.rendered) {
        const html = data[0].content.rendered
        const parser = new DOMParser()
        const doc = parser.parseFromString(html, 'text/html')
        const text = cleanText(doc.body.textContent || '')
        if (text.length > 100) {
          onLog?.(`[Madara] WP REST API succeeded`, 'dim')
          return stripWatermarks(text)
        }
      }
    } catch (e) {
      onLog?.(`[Madara] WP REST failed: ${e.message}`, 'dim')
    }
  }
  return null
}

// Strategy 2: Madara AJAX — POST /wp-admin/admin-ajax.php
async function tryMadaraAjax(origin, pageHtml, opts = {}) {
  const { onLog } = opts
  const nonce     = extractNonce(pageHtml)
  const chapterId = extractChapterId(pageHtml)

  if (!chapterId) {
    onLog?.(`[Madara] Could not find chapter_id in page HTML`, 'dim')
    return null
  }

  onLog?.(`[Madara] Trying AJAX (chapter_id=${chapterId}, nonce=${nonce || 'none'})`, 'dim')

  const ajaxUrl = `${origin}/wp-admin/admin-ajax.php`
  const body    = new URLSearchParams({
    action:     'manga_get_reading_page',
    'manga_chapter_id': chapterId,
    chapter_id: chapterId,
  })
  if (nonce) body.set('nonce', nonce)

  // Try direct POST first, then proxied
  const attempts = [
    () => fetch(ajaxUrl, { method: 'POST', body, headers: { 'Content-Type': 'application/x-www-form-urlencoded' } }),
    () => {
      const proxied = PROXIES[preferredProxyIndex].build(ajaxUrl)
      return fetch(proxied, { method: 'POST', body, headers: { 'Content-Type': 'application/x-www-form-urlencoded' } })
    },
  ]

  for (const attempt of attempts) {
    try {
      const ctrl = new AbortController()
      const t    = setTimeout(() => ctrl.abort(), 12000)
      const res  = await attempt()
      clearTimeout(t)
      const text = await res.text()
      // Response might be HTML fragment or JSON with HTML
      let html = text
      try { const j = JSON.parse(text); html = j.data || j.content || j.html || text } catch {}
      const parser = new DOMParser()
      const doc    = parser.parseFromString(html, 'text/html')
      const content = cleanText(doc.body.textContent || '')
      if (content.length > 100) {
        onLog?.(`[Madara] AJAX succeeded`, 'dim')
        return stripWatermarks(content)
      }
    } catch {}
  }
  return null
}

// Strategy 3: Find content embedded in <script> JSON blobs in the raw HTML
function tryScriptJsonExtraction(pageHtml, onLog) {
  onLog?.(`[Madara] Looking for content in script JSON blobs`, 'dim')
  const patterns = [
    // window.__NEXT_DATA__ or similar
    /window\.__(?:NEXT_DATA__|APP_STATE__|DATA__)\s*=\s*({.+?})\s*;/s,
    // wp.data or inline post data
    /"content"\s*:\s*\{[^}]*"rendered"\s*:\s*"((?:[^"\\]|\\.)*)"/,
    // postContent variable
    /postContent\s*=\s*["']([^"']{200,})/,
    // Raw text in a specific script
    /chapter[_-]?content[^=]*=\s*["'`]([^"'`]{200,})/i,
  ]
  for (const p of patterns) {
    try {
      const m = pageHtml.match(p)
      if (m) {
        let text = m[1]
        // Unescape if needed
        text = text.replace(/\\n/g, '\n').replace(/\\t/g, '\t').replace(/\\"/g, '"').replace(/\\\\/g, '\\')
        // Strip HTML tags if any
        const doc = new DOMParser().parseFromString(text, 'text/html')
        const clean = cleanText(doc.body.textContent || text)
        if (clean.length > 100) {
          onLog?.(`[Madara] Found content in script JSON`, 'dim')
          return stripWatermarks(clean)
        }
      }
    } catch {}
  }
  return null
}

// Strategy 4: Madara chapter list API for getting all chapters of a series
// GET /wp-json/wp/v2/posts?categories={manga_id}&per_page=100&orderby=menu_order
async function getMadaraChapterList(origin, seriesSlug, opts = {}) {
  const { onLog } = opts

  // Try the Madara-specific manga chapter endpoint
  const endpoints = [
    `${origin}/wp-json/wp/v2/posts?slug=${seriesSlug}&post_type=wp-manga&_fields=id,slug,chapters`,
    `${origin}/wp-json/madara/v1/posts/chapter?title=${seriesSlug}`,
    `${origin}/${seriesSlug}/?tab=chapter_list&action=ajax`,
  ]

  for (const ep of endpoints) {
    onLog?.(`[Madara] Chapter list: ${ep}`, 'dim')
    try {
      // Try direct, then proxy
      for (const useProxy of [false, true]) {
        const url = useProxy ? PROXIES[preferredProxyIndex].build(ep) : ep
        const ctrl = new AbortController()
        const t    = setTimeout(() => ctrl.abort(), 10000)
        const res  = await fetch(url, { signal: ctrl.signal })
        clearTimeout(t)
        if (!res.ok) continue
        const raw  = useProxy ? await PROXIES[preferredProxyIndex].extract(res) : await res.text()
        let data
        try { data = JSON.parse(raw) } catch { continue }
        if (Array.isArray(data) && data.length > 0) {
          onLog?.(`[Madara] Got chapter list (${data.length} items)`, 'dim')
          return data
        }
      }
    } catch {}
  }
  return null
}

// ── Master Madara extractor — tries all strategies in order ──────────────────
async function extractMadaraContent(chapterUrl, pageDoc, pageHtml, opts = {}) {
  const { onLog } = opts
  const origin    = getMadaraOrigin(chapterUrl)
  if (!origin) return null

  onLog?.(`[Madara] Site detected — trying direct API extraction`, 'info')

  // Extract chapter slug from URL
  const slugMatch = chapterUrl.match(/\/series\/[^/]+\/(chapter-\d+)/i)
                 || chapterUrl.match(/\/(chapter-\d+)\/?$/i)
  const chapterSlug = slugMatch?.[1] || ''

  // Strategy 1: WP REST API
  if (chapterSlug) {
    const content = await tryWpRestApi(origin, chapterSlug, opts)
    if (content) return content
  }

  // Strategy 2: Madara AJAX
  const ajaxContent = await tryMadaraAjax(origin, pageHtml, opts)
  if (ajaxContent) return ajaxContent

  // Strategy 3: Script JSON blobs
  const scriptContent = tryScriptJsonExtraction(pageHtml, onLog)
  if (scriptContent) return scriptContent

  onLog?.(`[Madara] All Madara strategies failed — falling back to DOM extraction`, 'dim')
  return null
}

// ══════════════════════════════════════════════════════════════════════════════
//  CORE FETCH + EXTRACT
// ══════════════════════════════════════════════════════════════════════════════

export async function fetchPage(url, opts = {}) {
  const { onLog } = opts
  const order = [
    preferredProxyIndex,
    ...PROXIES.map((_, i) => i).filter(i => i !== preferredProxyIndex),
  ]
  const errors = []

  for (const idx of order) {
    const proxy    = PROXIES[idx]
    const proxyUrl = proxy.build(url)
    try {
      onLog?.(`Trying proxy: ${proxy.name}`, 'dim')
      const ctrl  = new AbortController()
      const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS)
      const res   = await fetch(proxyUrl, { signal: ctrl.signal })
      clearTimeout(timer)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const html = await proxy.extract(res)
      if (!html || html.length < 100) throw new Error('Empty response')
      if (idx !== preferredProxyIndex) {
        onLog?.(`Switched to proxy: ${proxy.name}`, 'dim')
        preferredProxyIndex = idx
      }
      return { doc: new DOMParser().parseFromString(html, 'text/html'), html }
    } catch (e) {
      const reason = e.name === 'AbortError' ? 'timeout' : e.message
      onLog?.(`Proxy ${proxy.name} failed: ${reason}`, 'dim')
      errors.push(`${proxy.name}: ${reason}`)
    }
  }
  throw new Error(`All proxies failed.\n  ${errors.join('\n  ')}`)
}

// ── Text helpers ──────────────────────────────────────────────────────────────
function cleanText(text) {
  return text
    .replace(/\r\n/g, '\n').replace(/\r/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .replace(/[ \t]{2,}/g, ' ')
    .split('\n').map(l => l.trim()).join('\n')
    .trim()
}

const JUNK_RE = [
  /^prev(ious)?\s+chapter$/i, /^next\s+chapter$/i,
  /^←\s*(prev|previous|back)/i, /^(next|forward)\s*→/i,
  /^chapter\s+navigation/i, /^(prev|previous|next)\s*$/i,
  /use arrow keys/i, /\(or\s+a\s*\/\s*d\)/i,
  /^add\s+to\s+(library|bookmarks?|reading\s+list)$/i,
  /^\d+\s+comments?$/i, /^comments?$/i, /^reply$/i, /^like$/i,
  /^rate\s+this\s+(chapter|novel)$/i, /^table\s+of\s+contents?$/i,
  /translated\s+by/i, /translation\s+by/i, /translator[:\s]/i,
  /t\.?l\.?\s*note/i, /tl\s*note/i, /to\s+support\s+us/i,
  /support\s+the\s+(translation|translator|author)/i,
  /visit\s+our\s+(website|site|page)/i,
  /read\s+(more|ahead|the\s+latest)\s+(at|on)/i,
  /https?:\/\//i, /\w+\.(com|net|org|io|xyz|online|site)\b/i,
  /patreon\.com/i, /ko-?fi\.com/i, /buy\s+me\s+a\s+coffee/i,
  /if\s+you('re|\s+are)\s+reading\s+this/i,
  /this\s+chapter\s+was\s+(stolen|scraped|taken)/i,
  /join\s+our\s+(discord|group|server)/i, /discord\.gg\//i,
  /^[\-_*=~]{3,}$/, /lunox\s*scans?/i, /lunoxteam/i,
  // Paywall / login wall patterns
  /you\s+need\s+to\s+be\s+logged\s+in/i,
  /please\s+log\s+in/i,
  /not\s+a\s+member\?/i,
  /unlock\s+all\s+chapters/i,
  /get\s+instant\s+access/i,
  /join\s+now\s*🔓/i,
  /log\s+in\s+to\s+(read|unlock|access|view)/i,
  /sign\s+in\s+to\s+(read|unlock|continue)/i,
  /members?\s+only/i,
  /premium\s+chapter/i,
  /chapter\s+is\s+locked/i,
  /unlock\s+this\s+chapter/i,
  /subscribe\s+to\s+(read|unlock|access)/i,
]
const BLOCK_RE = [
  /(translator|tl|editor|proofreader)'?s?\s+note/i,
  /note\s+from\s+(the\s+)?(translator|editor)/i,
]

function stripWatermarks(text, customPhrases = []) {
  // Build per-call regex list: built-in junk + caller-supplied custom phrases
  const junk = customPhrases.length
    ? [...JUNK_RE, ...customPhrases.map(p => new RegExp(p.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i'))]
    : JUNK_RE
  const lines = text.split('\n')
  const out   = []
  let skipBlock = false
  for (const line of lines) {
    const s = line.trim()
    if (BLOCK_RE.some(r => r.test(s)))   { skipBlock = true }
    if (skipBlock && s === '')            { skipBlock = false; continue }
    if (skipBlock)                         continue
    if (s && junk.some(r => r.test(s)))   continue
    out.push(line)
  }
  return out.join('\n').replace(/\n{3,}/g, '\n\n').trim()
}

/**
 * Apply custom watermark entries to a chapter title.
 * entries: [{ phrase: string, titleReplace: string|null }]
 * Each matching phrase is replaced (or deleted if titleReplace is falsy).
 */
function cleanTitle(title, entries = []) {
  let t = title
  for (const e of entries) {
    if (!e.phrase) continue
    const re = new RegExp(e.phrase.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi')
    t = t.replace(re, e.titleReplace ?? '')
  }
  // Remove empty bracket pairs left after substitution, then collapse spaces
  t = t.replace(/\(\s*\)|\[\s*\]|\{\s*\}/g, '')
  t = t.replace(/[ \t]{2,}/g, ' ')
  t = t.replace(/^[\s\-–—|:,.]+/, '').replace(/[\s\-–—|:,.]+$/, '')
  return t.trim() || title
}

// ── DOM helpers ───────────────────────────────────────────────────────────────
function extractTitle(doc, fallbackNum) {
  const candidates = [
    doc.querySelector('h1'),
    doc.querySelector('h2'),
    doc.querySelector('[class*="chapter-title"]'),
    doc.querySelector('[class*="entry-title"]'),
    doc.querySelector('title'),
  ]
  for (const el of candidates) {
    const txt = el?.textContent?.trim()
    if (txt) return cleanText(txt)
  }
  return `Chapter ${fallbackNum}`
}

function extractContentFromDoc(doc) {
  const STRIP = [
    'script','style','nav','header','footer','aside',
    'figure','figcaption','iframe','ins','noscript','form','button',
    '.nav-previous','.nav-next','.chapter-nav','.post-navigation',
    '.ads','.ad-container','[class*="advertisement"]',
  ]
  STRIP.forEach(sel => { try { doc.querySelectorAll(sel).forEach(el => el.remove()) } catch {} })

  for (const sel of CONTENT_SELECTORS) {
    try {
      const block = doc.querySelector(sel)
      if (block) {
        const text = cleanText(block.innerText || block.textContent || '')
        if (text.length > 200) return stripWatermarks(text)
      }
    } catch {}
  }

  const divs = [...doc.querySelectorAll('div')]
  if (divs.length) {
    const biggest = divs.reduce((a, b) =>
      (b.textContent?.length || 0) > (a.textContent?.length || 0) ? b : a)
    const text = cleanText(biggest.textContent || '')
    if (text.length > 200) return stripWatermarks(text)
  }
  return ''
}

function findNextUrl(doc, currentUrl) {
  let href = null
  for (const sel of NEXT_SELECTORS) {
    try {
      const a = doc.querySelector(sel)
      if (a?.getAttribute('href')) { href = a.getAttribute('href'); break }
    } catch {}
  }
  if (!href) {
    for (const a of doc.querySelectorAll('a[href]')) {
      const txt = a.textContent.trim().toLowerCase()
      if (['next chapter','next','next chap','next →'].includes(txt)) {
        href = a.getAttribute('href'); break
      }
    }
  }
  if (!href) return null
  const resolved = href.startsWith('http') ? href : new URL(href, currentUrl).href
  // Strip fragment-only URLs (e.g. chapter-237/#respond) — same page, just an anchor
  const base = resolved.split('#')[0].replace(/\/$/, '')
  const cur  = currentUrl.split('#')[0].replace(/\/$/, '')
  if (base === cur) return null
  return resolved
}

function findPrevUrl(doc, currentUrl) {
  for (const sel of PREV_SELECTORS) {
    try {
      const a = doc.querySelector(sel)
      if (a?.getAttribute('href')) {
        const href = a.getAttribute('href')
        return href.startsWith('http') ? href : new URL(href, currentUrl).href
      }
    } catch {}
  }
  for (const a of doc.querySelectorAll('a[href]')) {
    const txt = a.textContent.trim().toLowerCase()
    if (['previous chapter','previous','prev chapter','prev chap','← prev'].includes(txt)) {
      try { return new URL(a.getAttribute('href'), currentUrl).href } catch {}
    }
  }
  return null
}

function inferChapterNumber(title, fallback) {
  const patterns = [
    /chapter[\s\-_#]?(\d+)/i,
    /ch[\s\-_.]?(\d+)/i,
    /#(\d+)/,
    /ep(?:isode)?[\s\-_.]?(\d+)/i,
    /(\d+)/,
  ]
  for (const p of patterns) {
    const m = title.match(p)
    if (m) return parseInt(m[1])
  }
  return fallback
}

// ── Full page fetch + smart content extraction ────────────────────────────────
// Returns { title, content, doc, nextUrl, prevUrl }
async function fetchAndExtract(url, fallbackNum, opts = {}) {
  const { onLog, watermarks = [] } = opts
  const { doc, html } = await fetchPage(url, opts)

  const title = extractTitle(doc, fallbackNum)

  // First try standard DOM extraction
  let content = extractContentFromDoc(doc)

  // If content is empty and page looks like Madara, try API strategies
  if (content.length < 50 && isMadaraSite(doc, url)) {
    content = await extractMadaraContent(url, doc, html, opts) || ''
  }

  // Strip built-in junk + any custom phrases from content
  const customPhrases = watermarks.map(e => e.phrase).filter(Boolean)
  content = stripWatermarks(content, customPhrases)

  return {
    title,
    content,
    doc,
    html,
    nextUrl: findNextUrl(doc, url),
    prevUrl: findPrevUrl(doc, url),
  }
}

// ══════════════════════════════════════════════════════════════════════════════
//  CHAPTER LIST DETECTION
// ══════════════════════════════════════════════════════════════════════════════

export async function detectLatestChapterOnIndexPage(indexUrl, checkSelector = '', opts = {}) {
  const { onLog } = opts
  const { doc, html } = await fetchPage(indexUrl, opts)

  // 1. User-supplied selector
  if (checkSelector) {
    try {
      const el = doc.querySelector(checkSelector)
      if (el) {
        const m = el.textContent.match(/(\d+)/)
        if (m) return { num: parseInt(m[1]), href: el.getAttribute('href') || '' }
      }
    } catch {}
  }

  // 2. Common chapter list selectors
  const listSelectors = [
    '.wp-manga-chapter a', '.chapter-list li a', '.chapters li a',
    'ul.chapter-list a', 'ul.row-content-chapter li a',
    '.listing-chapters_wrap li a', '.eph-num a', '.chbox a',
    'li.chapter a', 'li[class*="chapter"] a',
  ]
  for (const sel of listSelectors) {
    try {
      const links = [...doc.querySelectorAll(sel)]
      if (!links.length) continue
      const candidates = links.flatMap(a => {
        const txt  = a.textContent.trim()
        const href = a.getAttribute('href') || ''
        const m    = txt.match(/chapter[\s\-_#]?(\d+)/i)
                  || txt.match(/ch[\s\-_.]?(\d+)/i)
                  || href.match(/chapter[\-_]?(\d+)/i)
        return m ? [{ num: parseInt(m[1]), href: a.href || href }] : []
      })
      if (candidates.length) {
        const best = candidates.reduce((a, b) => b.num > a.num ? b : a)
        onLog?.(`Detected latest via "${sel}": Ch.${best.num}`, 'dim')
        return best
      }
    } catch {}
  }

  // 3. Brute-force all links
  const allLinks   = [...doc.querySelectorAll('a[href]')]
  const candidates = allLinks.flatMap(a => {
    const txt  = a.textContent.trim()
    const href = a.getAttribute('href') || ''
    const m    = txt.match(/chapter[\s\-_#]?(\d+)/i)
              || txt.match(/ch[\s\-_.]?(\d+)/i)
              || href.match(/chapter[\-_\/]?(\d+)/i)
    return m ? [{ num: parseInt(m[1]), href: a.href || href }] : []
  })
  if (candidates.length) {
    const best = candidates.reduce((a, b) => b.num > a.num ? b : a)
    onLog?.(`Detected latest via brute-force: Ch.${best.num}`, 'dim')
    return best
  }

  onLog?.('Could not auto-detect latest chapter from index page', 'warn')
  return { num: 0, href: '' }
}

// ══════════════════════════════════════════════════════════════════════════════
//  FORWARD CRAWL
// ══════════════════════════════════════════════════════════════════════════════

export async function scrapeForward(startUrl, {
  fromChapter = 0, maxChapters = 500, delay = 1200, onChapter, onLog, watermarks = [],
}) {
  const results = []
  let url = startUrl
  let index = 1
  const visited = new Set()

  while (url) {
    if (visited.has(url)) break
    if (results.length >= maxChapters) { onLog?.(`Max chapters (${maxChapters}) reached`, 'warn'); break }
    visited.add(url)

    onLog?.(`[${index}] ${url}`, 'info')

    let page
    try { page = await fetchAndExtract(url, index, { onLog, watermarks }) }
    catch (e) { onLog?.(`Fetch failed: ${e.message}`, 'err'); break }

    const title  = cleanTitle(page.title, watermarks)
    const chNum  = inferChapterNumber(title, index)

    if (chNum > fromChapter) {
      const wordCount = page.content.split(/\s+/).filter(Boolean).length
      const paywallHit = PAYWALL_SIGNALS.some(r => r.test(page.content.slice(0, 2000)))

      if (paywallHit) {
        onLog?.(`⊘ Ch.${chNum} — ${title} skipped: paywall/login wall detected`, 'warn')
      } else if (wordCount < MIN_WORDS) {
        onLog?.(`⊘ Ch.${chNum} — ${title} skipped: too short (${wordCount}w)`, 'warn')
      } else {
        const ch = { number: chNum, title, content: page.content, url }
        results.push(ch)
        onLog?.(`✓ Ch.${chNum} — ${title} (${wordCount}w)`, 'ok')
        onChapter?.(ch)
      }
    } else {
      onLog?.(`~ Ch.${chNum} already stored, skipping`, 'dim')
    }

    if (!page.nextUrl || page.nextUrl === url) { onLog?.('No more chapters.', 'info'); break }
    url = page.nextUrl
    index++
    await sleep(delay)
  }
  return results
}

// ══════════════════════════════════════════════════════════════════════════════
//  BACKWARD CRAWL
// ══════════════════════════════════════════════════════════════════════════════

export async function scrapeBackward(latestUrl, {
  fromChapter = 0, maxChapters = 500, delay = 1200, onChapter, onLog,
}) {
  const collected = []
  let url = latestUrl
  const visited = new Set()

  while (url) {
    if (visited.has(url) || collected.length >= maxChapters) break
    visited.add(url)

    onLog?.(`[back] ${url}`, 'info')

    let page
    try { page = await fetchAndExtract(url, 0, { onLog }) }
    catch (e) { onLog?.(`Fetch failed: ${e.message}`, 'err'); break }

    const chNum = inferChapterNumber(page.title, 0)
    if (chNum <= fromChapter) { onLog?.(`Reached Ch.${chNum} (already stored). Done.`, 'info'); break }

    onLog?.(`✓ Ch.${chNum} — ${page.title}`, 'ok')
    collected.push({ number: chNum, title: page.title, content: page.content, url })

    if (!page.prevUrl || page.prevUrl === url) break
    url = page.prevUrl
    await sleep(delay)
  }

  collected.reverse()
  collected.forEach(ch => onChapter?.(ch))
  return collected
}

// ══════════════════════════════════════════════════════════════════════════════
//  WATCHER CHECK
// ══════════════════════════════════════════════════════════════════════════════

export async function checkForNewChapters(watchEntry, storedLastChapter, opts) {
  const { onLog } = opts
  const { startUrl, mode, checkSelector } = watchEntry

  // ── Chain mode: follow next-chapter links from the last known chapter URL ──
  if (mode === 'chain') {
    const chainUrl = watchEntry.lastChapterUrl || startUrl
    onLog?.(`Chain check from: ${chainUrl}`, 'info')
    let page
    try {
      page = await fetchAndExtract(chainUrl, storedLastChapter, opts)
    } catch (e) {
      onLog?.(`Could not fetch last chapter page: ${e.message}`, 'err')
      return []
    }
    if (!page.nextUrl || page.nextUrl === chainUrl) {
      onLog?.(`No next chapter link found — up to date.`, 'dim')
      return []
    }
    onLog?.(`Next chapter found! Crawling from ${page.nextUrl}`, 'ok')
    return scrapeForward(page.nextUrl, { fromChapter: storedLastChapter, ...opts })
  }

  onLog?.(`Checking ${startUrl}`, 'info')

  let siteLatest = 0
  let chapterStartUrl = startUrl

  try {
    if (mode === 'index') {
      const { doc, html } = await fetchPage(startUrl, opts)

      // Find ALL chapter links and pick the lowest new one as forward-crawl start,
      // and the highest for "site latest" comparison.
      const listSelectors = [
        '.wp-manga-chapter a', '.chapter-list li a', '.chapters li a',
        'ul.chapter-list a', 'ul.row-content-chapter li a',
        '.listing-chapters_wrap li a', '.eph-num a', '.chbox a',
        'li.chapter a', 'li[class*="chapter"] a',
      ]
      let allCandidates = []
      for (const sel of listSelectors) {
        try {
          const links = [...doc.querySelectorAll(sel)]
          if (!links.length) continue
          const found = links.flatMap(a => {
            const txt  = a.textContent.trim()
            const href = a.getAttribute('href') || ''
            const m    = txt.match(/chapter[\s\-_#]?(\d+)/i)
                      || txt.match(/ch[\s\-_.]?(\d+)/i)
                      || href.match(/chapter[-_]?(\d+)/i)
            return m ? [{ num: parseInt(m[1]), href: a.href || href }] : []
          })
          if (found.length) { allCandidates = found; break }
        } catch {}
      }

      if (!allCandidates.length) {
        // Brute-force all links
        allCandidates = [...doc.querySelectorAll('a[href]')].flatMap(a => {
          const txt  = a.textContent.trim()
          const href = a.getAttribute('href') || ''
          const m    = txt.match(/chapter[\s\-_#]?(\d+)/i)
                    || txt.match(/ch[\s\-_.]?(\d+)/i)
                    || href.match(/chapter[-_\/]?(\d+)/i)
          return m ? [{ num: parseInt(m[1]), href: a.href || href }] : []
        })
      }

      if (allCandidates.length) {
        siteLatest = Math.max(...allCandidates.map(c => c.num))
        // Find lowest chapter strictly above storedLastChapter to start crawl from
        const newOnes = allCandidates.filter(c => c.num > storedLastChapter).sort((a,b) => a.num - b.num)
        if (newOnes.length) chapterStartUrl = newOnes[0].href
        onLog?.(`Found ${allCandidates.length} chapter links, latest: Ch.${siteLatest}`, 'dim')
      } else {
        // Fallback: use the user-provided CSS selector
        const result = await detectLatestChapterOnIndexPage(startUrl, checkSelector, opts)
        siteLatest = result.num
        chapterStartUrl = result.href || startUrl
      }
    } else {
      const { doc } = await fetchPage(startUrl, opts)
      const title   = extractTitle(doc, 0)
      siteLatest    = inferChapterNumber(title, 0)
      chapterStartUrl = startUrl
    }
  } catch (e) {
    onLog?.(`Could not detect latest chapter: ${e.message}`, 'err')
    return []
  }

  onLog?.(`Site latest: Ch.${siteLatest} | Stored: Ch.${storedLastChapter}`, 'info')

  if (siteLatest <= storedLastChapter) {
    onLog?.('No new chapters.', 'dim')
    return []
  }

  onLog?.(`${siteLatest - storedLastChapter} new chapter(s) detected! Fetching…`, 'ok')

  return mode === 'latest'
    ? scrapeBackward(chapterStartUrl, { fromChapter: storedLastChapter, ...opts })
    : scrapeForward(chapterStartUrl,  { fromChapter: storedLastChapter, ...opts })
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)) }
