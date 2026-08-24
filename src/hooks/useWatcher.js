// ── useWatcher.js ─────────────────────────────────────────────────────────────
// Auto-watcher: uses the local Python server when running, falls back to
// browser-side scraper if the server is not available.

import { useState, useEffect, useRef, useCallback } from 'react'
import { checkForNewChapters }          from '../lib/scraper.js'
import { createChapter, getNovelChapters, getNovels, getCredentials } from '../lib/api.js'
import {
  checkServerHealth, fetchHealthInfo, startJob, streamJob, detectLatestChapter,
  getServerWatches, upsertServerWatch, deleteServerWatch,
  startServerWatch, stopServerWatch, runServerWatchNow, patchServerWatch,
  patchServerConfig,
} from '../lib/localScraper.js'
import { saveJobToHistory } from '../components/JobHistory.jsx'
import { loadWatermarks }   from '../components/WatermarkEditor.jsx'

// kn_watched_novels: specific to this KN dashboard (avoids clash with the
// old NovaSphere scraper which used 'ns_watched_novels' on the same port).
const STORAGE_KEY = 'kn_watched_novels'

function loadWatched() {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]') } catch { return [] }
}
function saveWatched(list) { localStorage.setItem(STORAGE_KEY, JSON.stringify(list)) }
function hasContent(ch)    { return (ch.content || '').trim().length >= 50 }

// Build the payload the Python server needs to run a watch autonomously
function watchToServer(w) {
  return {
    novelId:       w.novelId,
    novelSlug:     w.novelSlug     || '',
    novelTitle:    w.novelTitle    || '',
    startUrl:      w.startUrl      || '',
    chapterOneUrl: w.chapterOneUrl || w.startUrl || '',
    mode:          w.mode          || 'chain',
    intervalHours: w.intervalHours || 6,
    checkSelector: w.checkSelector || '',
    lastChapter:   w.lastChapter   || 0,
    lastChecked:   w.lastChecked   || 0,
    lastChapterUrl:w.lastChapterUrl|| '',
    active:        !!w.enabled,
    delay:         w.delay         || 1.2,
  }
}

const CONCURRENCY_KEY = 'kn_watch_concurrency'
const STAGGER_KEY     = 'kn_stagger_delay'

function loadConcurrency() {
  const v = parseInt(localStorage.getItem(CONCURRENCY_KEY) || '1', 10)
  return Number.isFinite(v) && v >= 1 ? Math.min(v, 10) : 1
}
function loadStaggerDelay() {
  const v = parseFloat(localStorage.getItem(STAGGER_KEY) || '0')
  return Number.isFinite(v) && v >= 0 ? Math.min(v, 60) : 0
}

export function useWatcher(addLog) {
  const [watched,          setWatched]          = useState(loadWatched)
  const [running,          setRunning]          = useState({})
  const [serverOnline,     setServerOnline]     = useState(false)
  const [concurrencyLimit, _setConcurrencyLimit]= useState(loadConcurrency)
  const [staggerDelay,     _setStaggerDelay]    = useState(loadStaggerDelay)
  const [queueLength,      setQueueLength]      = useState(0)
  // ── Batch scrape progress ─────────────────────────────────────────────────
  const [batchProgress,    setBatchProgress]    = useState(null) // null | { total, done, active }
  const batchCancelRef = useRef(false)
  const timersRef      = useRef({})
  const healthRef      = useRef(null)
  // ── Semaphore state (mutable refs — never trigger re-renders) ─────────────
  const activeRef      = useRef(0)   // how many slots are currently occupied
  const waitQueueRef   = useRef([])  // [{ novelId, resolve }] waiting for a slot
  const concLimitRef   = useRef(loadConcurrency())

  const setConcurrencyLimit = useCallback((val) => {
    const n = Math.max(1, Math.min(10, Number(val) || 1))
    concLimitRef.current = n
    _setConcurrencyLimit(n)
    localStorage.setItem(CONCURRENCY_KEY, String(n))
    // Push to Python server
    patchServerConfig({ concurrency: n }).catch(() => {})
    // Drain the queue if the new limit allows more slots
    _drainQueue()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const setStaggerDelay = useCallback((val) => {
    const s = Math.max(0, Math.min(60, Number(val) || 0))
    _setStaggerDelay(s)
    localStorage.setItem(STAGGER_KEY, String(s))
    // Push to Python server
    patchServerConfig({ stagger_delay: s }).catch(() => {})
  }, [])

  function _drainQueue() {
    while (activeRef.current < concLimitRef.current && waitQueueRef.current.length > 0) {
      const next = waitQueueRef.current.shift()
      setQueueLength(waitQueueRef.current.length)
      activeRef.current++
      next.resolve()
    }
  }

  useEffect(() => { saveWatched(watched) }, [watched])

  // ── Poll server health + sync watches every 10 seconds ───────────────────
  useEffect(() => {
    async function check() {
      const info = await fetchHealthInfo()
      const online = !!info
      setServerOnline(prev => {
        // On first connect: push all local watches to the server
        if (!prev && online) {
          const local = loadWatched()
          const { apiUrl, scraperKey } = getCredentials()
          local.forEach(w => {
            upsertServerWatch({
              ...watchToServer(w),
              apiUrl,
              token: scraperKey,
            }).catch(() => {})
          })
        }
        return online
      })
    }
    check()
    healthRef.current = setInterval(check, 10000)
    return () => clearInterval(healthRef.current)
  }, [])

  const addWatch = useCallback((entry) => {
    const { apiUrl, scraperKey } = getCredentials()
    setWatched(prev => {
      const existing = prev.find(w => w.novelId === entry.novelId)
      const merged = existing
        ? prev.map(w =>
            w.novelId === entry.novelId
              ? { ...w, ...entry, lastChapter: w.lastChapter, lastChecked: w.lastChecked }
              : w
          )
        : [...prev, {
            ...entry,
            lastChapter:   0,
            lastChecked:   null,
            enabled:       false,
            chapterOneUrl: entry.chapterOneUrl || entry.startUrl,
          }]
      // Mirror to Python server (fire and forget)
      const saved = merged.find(w => w.novelId === entry.novelId)
      if (saved) {
        upsertServerWatch({ ...watchToServer(saved), apiUrl, token: scraperKey }).catch(() => {})
      }
      return merged
    })
  }, [])

  const removeWatch = useCallback((novelId) => {
    stopWatch(novelId)
    setWatched(prev => prev.filter(w => w.novelId !== novelId))
    deleteServerWatch(novelId).catch(() => {})
  }, [])

  const updateWatch = useCallback((novelId, patch) => {
    setWatched(prev => prev.map(w => w.novelId === novelId ? { ...w, ...patch } : w))
  }, [])

  const resetChapter = useCallback((novelId, num) => {
    updateWatch(novelId, { lastChapter: num })
    addLog(novelId, `Chapter pointer manually reset to Ch.${num}`, 'warn')
  }, [updateWatch, addLog])

  // ── Core check: runs via local server if available, else browser scraper ──
  // Private impl — called only after the semaphore grants a slot
  const _doRunCheck = useCallback(async (novelId) => {
    setRunning(prev => ({ ...prev, [novelId]: true }))

    const currentList = loadWatched()
    const entry = currentList.find(w => w.novelId === novelId)
    if (!entry) { setRunning(prev => ({ ...prev, [novelId]: false })); return }

    // Get authoritative chapter list from the API and find the lowest gap
    // entry.novelSlug is the KN novel slug.
    // If it's missing (legacy entry saved before novelSlug was stored), fetch
    // it directly from the KN novels API and backfill it in localStorage.
    let storedLast = entry.lastChapter || 0
    let novelSlug = entry.novelSlug || ''
    if (!novelSlug) {
      try {
        const data = await getNovels()
        const match = (data.novels || []).find(n => n._id === novelId)
        if (match?.slug) {
          novelSlug = match.slug
          setWatched(prev => prev.map(w =>
            w.novelId === novelId ? { ...w, novelSlug } : w
          ))
          addLog(novelId, `Backfilled novelSlug: '${novelSlug}'`, 'dim')
        } else {
          addLog(novelId, `Could not resolve slug for novel ${novelId} — upload will fail`, 'err')
        }
      } catch (e) {
        addLog(novelId, `Could not fetch novels to resolve slug: ${e.message}`, 'warn')
      }
    }
    try {
      if (novelSlug) {
        const chapters = await getNovelChapters(novelSlug)
        if (chapters?.length) {
          const stored = new Set(chapters.map(c => c.number || 0))
          const maxNum = Math.max(...stored)
          // Find the lowest missing chapter number in the sequence 1..maxNum
          let lowestGap = null
          for (let i = 1; i <= maxNum; i++) {
            if (!stored.has(i)) { lowestGap = i; break }
          }
          if (lowestGap !== null) {
            // Start from just before the gap so the scraper picks it up
            storedLast = lowestGap - 1
            addLog(novelId, `Gap detected — missing from Ch.${lowestGap} (scanning from Ch.${storedLast})`, 'warn')
          } else {
            storedLast = maxNum
            addLog(novelId, `Last uploaded chapter: Ch.${storedLast}`, 'dim')
          }
        }
      } else {
        addLog(novelId, `No novel slug stored — using last chapter pointer Ch.${storedLast}`, 'warn')
      }
    } catch (e) {
      addLog(novelId, `Could not fetch chapter list: ${e.message}`, 'warn')
    }

    const isServerUp = await checkServerHealth()

    if (isServerUp) {
      // ── Path A: Python local server ───────────────────────────────────────
      addLog(novelId, `── Watch check (via local Python server) ──`, 'info')
      const apiUrl = localStorage.getItem('kn_api_url') || localStorage.getItem('ns_api_url') || ''
      const token  = localStorage.getItem('kn_scraper_key') || localStorage.getItem('ns_token')   || ''

      // For watch_check:
      //   index mode  → start_url must be the novel INDEX page
      //   latest mode → start_url must be the latest chapter page
      // chapterOneUrl is the chapter-1 URL used only for full scrapes, not watch checks.
      const watchUrl = entry.startUrl  // always the index/latest URL set by user in NovelCard

      let jobId
      try {
        // For chain mode, crawl starts from the last known chapter URL (not chapter-1)
        const crawlStartUrl = entry.mode === 'chain'
          ? (entry.lastChapterUrl || entry.startUrl)
          : (entry.chapterOneUrl || entry.startUrl)
        jobId = await startJob({
          mode:             'watch_check',
          start_url:        entry.mode === 'chain' ? crawlStartUrl : entry.startUrl,
          chapter_one_url:  crawlStartUrl,
          novel_id:         novelId,
          novel_slug:       novelSlug,   // ← use resolved slug (backfilled from novels list if missing)
          api_url:          apiUrl,
          token:            token,
          from_chapter:     storedLast,
          // Chain mode: server starts crawling from the last known chapter page,
          // so index starts at storedLast (not 1) — fixes infer_chapter_number fallback
          index_offset:     entry.mode === 'chain' ? storedLast : 0,
          delay:            entry.delay || 1.2,
          max_chapters:     500,
          watch_mode:       entry.mode || 'index',
          check_selector:   entry.checkSelector || '',
          custom_watermarks: loadWatermarks(),
        })
      } catch (e) {
        addLog(novelId, `Could not start server job: ${e.message}`, 'err')
        setRunning(prev => ({ ...prev, [novelId]: false }))
        return
      }

      addLog(novelId, `Server job started (id: ${jobId})`, 'dim')

      await new Promise((resolve) => {
        let highestUploaded = storedLast
        let lastUploadedUrl = null
        const jobStartedAt  = Date.now()
        const jobLogs       = []
        const cleanup = streamJob(jobId, {
          onLog: (entry) => {
            // Intercept the chain-mode URL marker — don't show it in the UI
            if (entry.msg.startsWith('__last_chapter_url__:')) {
              lastUploadedUrl = entry.msg.replace('__last_chapter_url__:', '').trim()
              return
            }
            addLog(novelId, entry.msg, entry.type)
            jobLogs.push(entry)
            const m = entry.msg.match(/(?:✓\s*)?[Uu]ploaded Ch\.(\d+)/)
            if (m) {
              const n = parseInt(m[1])
              if (n > highestUploaded) highestUploaded = n
            }
          },
          onStats: () => {},
          onDone: (stats) => {
            // Save to job history
            saveJobToHistory({
              id:         jobId,
              novelId,
              novelTitle: entry.novelTitle || novelId,
              mode:       'watch_check',
              startedAt:  jobStartedAt,
              finishedAt: Date.now(),
              status:     stats.uploaded > 0 ? 'done_ok' : stats.errors > 0 ? 'error' : 'done_warn',
              stats,
              logs:       jobLogs,
            })
            if (highestUploaded > storedLast) {
              setWatched(prev => prev.map(w => {
                if (w.novelId !== novelId) return w
                const patch = { lastChapter: highestUploaded, lastChecked: Date.now() }
                // Chain mode: update the stored chapter URL to the last one uploaded
                if (w.mode === 'chain' && lastUploadedUrl) patch.lastChapterUrl = lastUploadedUrl
                return { ...w, ...patch }
              }))
            } else {
              setWatched(prev => prev.map(w =>
                w.novelId === novelId ? { ...w, lastChecked: Date.now() } : w
              ))
            }
            cleanup()
            resolve()
          },
        })
      })

    } else {
      // ── Path B: Browser scraper fallback ──────────────────────────────────
      addLog(novelId, `── Watch check (browser scraper — local server not running) ──`, 'warn')
      addLog(novelId, `Tip: run "python scraper-server/scraper_server.py" for better results`, 'dim')

      const browserJobId  = `browser-${Date.now()}`
      const browserStart  = Date.now()
      const browserLogs   = []   // collect logs locally since we can't access App's logs state
      const logAndCollect = (msg, type) => {
        addLog(novelId, msg, type)
        browserLogs.push({ msg, type, ts: Date.now() })
      }
      let newChapters = []
      try {
        newChapters = await checkForNewChapters(entry, storedLast, {
          onLog: logAndCollect,
        })
      } catch (e) {
        logAndCollect(`Scrape error: ${e.message}`, 'err')
      }

      if (!newChapters.length) {
        addLog(novelId, `── Done: no new chapters ──`, 'dim')
        setWatched(prev => prev.map(w =>
          w.novelId === novelId ? { ...w, lastChecked: Date.now() } : w
        ))
        setRunning(prev => ({ ...prev, [novelId]: false }))
        return 0
      }

      const uploadable = newChapters.filter(ch => hasContent(ch))
      const skipped    = newChapters.filter(ch => !hasContent(ch))
      if (skipped.length)
        addLog(novelId, `⚠ ${skipped.length} chapter(s) skipped — no text content`, 'warn')

      let pushed = 0
      let highestUploaded = storedLast

      for (const ch of uploadable) {
        try {
          await createChapter(novelSlug, { number: ch.number, title: ch.title, content: ch.content.trim() })
          logAndCollect(`✓ Uploaded Ch.${ch.number} — ${ch.title}`, 'ok')
          pushed++
          if (ch.number > highestUploaded) highestUploaded = ch.number
        } catch (e) {
          if (e.message.toLowerCase().includes('already exists')) {
            logAndCollect(`~ Ch.${ch.number} already exists`, 'dim')
            if (ch.number > highestUploaded) highestUploaded = ch.number
          } else {
            logAndCollect(`✗ Ch.${ch.number} failed: ${e.message}`, 'err')
          }
        }
        await sleep(150)
      }

      setWatched(prev => prev.map(w => {
        if (w.novelId !== novelId) return w
        const patch = { lastChapter: highestUploaded, lastChecked: Date.now() }
        // Chain mode: save the URL of the highest uploaded chapter so next check starts from there
        if (w.mode === 'chain') {
          const highestCh = uploadable.find(ch => ch.number === highestUploaded)
          if (highestCh?.url) patch.lastChapterUrl = highestCh.url
        }
        return { ...w, ...patch }
      }))
      saveJobToHistory({
        id:         browserJobId,
        novelId,
        novelTitle: entry.novelTitle || novelId,
        mode:       'watch_check',
        startedAt:  browserStart,
        finishedAt: Date.now(),
        status:     pushed > 0 ? 'done_ok' : 'done_warn',
        stats:      { scraped: newChapters.length, uploaded: pushed, skipped: skipped.length, errors: 0 },
        logs:       browserLogs,
      })
      logAndCollect(
        pushed > 0 ? `── Done: ${pushed} chapter(s) uploaded ──` : `── Done: 0 uploaded ──`,
        pushed > 0 ? 'ok' : 'warn'
      )
    }

    setRunning(prev => ({ ...prev, [novelId]: false }))
  }, [addLog])

  // ── Public runCheck — enqueues then delegates to _doRunCheck ─────────────
  const runCheck = useCallback(async (novelId) => {
    // If a slot is free, take it immediately; otherwise wait in queue
    if (activeRef.current < concLimitRef.current) {
      activeRef.current++
    } else {
      await new Promise((resolve) => {
        waitQueueRef.current.push({ novelId, resolve })
        setQueueLength(waitQueueRef.current.length)
      })
    }
    try {
      await _doRunCheck(novelId)
    } finally {
      activeRef.current--
      _drainQueue()
    }
  }, [_doRunCheck]) // eslint-disable-line react-hooks/exhaustive-deps

  // ── Batch scrape: fire all watched novels sequentially with stagger ──────
  const batchScrapeAll = useCallback(async (novelList) => {
    if (!novelList || novelList.length === 0) return
    batchCancelRef.current = false
    setBatchProgress({ total: novelList.length, done: 0, active: 0 })

    const promises = []
    for (let i = 0; i < novelList.length; i++) {
      if (batchCancelRef.current) break
      const novel = novelList[i]

      // Respect stagger delay between submissions
      if (i > 0) {
        const delay = parseFloat(localStorage.getItem(STAGGER_KEY) || '0') * 1000
        if (delay > 0) await sleep(delay)
      }
      if (batchCancelRef.current) break

      setBatchProgress(prev => prev ? { ...prev, active: (prev.active || 0) + 1 } : null)
      const p = runCheck(novel._id || novel.novelId).then(() => {
        setBatchProgress(prev => prev
          ? { ...prev, done: prev.done + 1, active: Math.max(0, (prev.active || 1) - 1) }
          : null
        )
      })
      promises.push(p)
    }

    // Wait for all that were dispatched
    await Promise.allSettled(promises)
    setBatchProgress(null)
    batchCancelRef.current = false
  }, [runCheck])

  const cancelBatch = useCallback(() => {
    batchCancelRef.current = true
    setBatchProgress(null)
  }, [])

  const startWatch = useCallback((novelId, intervalHours = 1) => {
    if (timersRef.current[novelId]) return
    runCheck(novelId)
    const id = setInterval(() => runCheck(novelId), intervalHours * 60 * 60 * 1000)
    timersRef.current[novelId] = id
    updateWatch(novelId, { enabled: true })
    // Mirror to server — server scheduler will also fire independently
    startServerWatch(novelId).catch(() => {})
    addLog(novelId, `Auto-watcher started (every ${intervalHours}h) — server scheduler active`, 'info')
  }, [runCheck, updateWatch, addLog])

  const stopWatch = useCallback((novelId) => {
    const id = timersRef.current[novelId]
    if (id) { clearInterval(id); delete timersRef.current[novelId] }
    updateWatch(novelId, { enabled: false })
    stopServerWatch(novelId).catch(() => {})
    addLog(novelId, `Auto-watcher stopped`, 'warn')
  }, [updateWatch, addLog])

  // ── Auto-restart watchers that were enabled before page reload ───────────
  useEffect(() => {
    const prevEnabled = loadWatched().filter(w => w.enabled)
    if (!prevEnabled.length) return
    // Small delay so runCheck has a stable reference before we call startWatch
    const t = setTimeout(() => {
      prevEnabled.forEach(w => {
        if (!timersRef.current[w.novelId]) {
          startWatch(w.novelId, w.intervalHours || 1)
          addLog(w.novelId, `Auto-watcher resumed after page reload`, 'info')
        }
      })
    }, 500)
    return () => clearTimeout(t)
  }, [startWatch, addLog])

  // ── Cleanup all timers on unmount ─────────────────────────────────────────
  useEffect(() => {
    return () => Object.values(timersRef.current).forEach(clearInterval)
  }, [])

  const updateChainUrl = useCallback((novelId, url) => {
    updateWatch(novelId, { lastChapterUrl: url })
    addLog(novelId, `Chain URL manually updated to: ${url}`, 'info')
  }, [updateWatch, addLog])

  const isWatching = (novelId) => !!timersRef.current[novelId]

  return {
    watched, addWatch, removeWatch, updateWatch, runCheck,
    startWatch, stopWatch, running, isWatching, resetChapter,
    updateChainUrl, serverOnline,
    concurrencyLimit, setConcurrencyLimit, queueLength,
    staggerDelay, setStaggerDelay,
    batchScrapeAll, cancelBatch, batchProgress,
  }
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)) }
