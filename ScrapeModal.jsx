import { useState, useRef, useEffect } from 'react'
import { X, Play, Square, Server, Wifi } from 'lucide-react'
import { scrapeForward }                   from '../lib/scraper.js'
import { createChapter, bulkImportChapters, getNovelChapters } from '../lib/api.js'
import { checkServerHealth, startJob, streamJob } from '../lib/localScraper.js'
import { loadWatermarks }                  from './WatermarkEditor.jsx'
import styles from './ScrapeModal.module.css'

export default function ScrapeModal({ novel, onClose }) {
  const [url,        setUrl]        = useState('')
  const [delay,      setDelay]      = useState(1.2)
  const [maxCh,      setMaxCh]      = useState(500)
  const [fromCh,     setFromCh]     = useState(0)
  const [autoFrom,   setAutoFrom]   = useState(true)
  const [rescanUrl,  setRescanUrl]  = useState('')   // optional: start crawl from a specific chapter URL
  const [logs,       setLogs]       = useState([])
  const [running,    setRunning]    = useState(false)
  const [stats,      setStats]      = useState({ scraped: 0, uploaded: 0, skipped: 0, errors: 0 })
  const [serverUp,   setServerUp]   = useState(false)
  const stopRef   = useRef(false)
  const cleanupRef= useRef(null)
  const logBoxRef = useRef(null)

  useEffect(() => {
    checkServerHealth().then(setServerUp)
  }, [])

  function addLog(msg, type = 'info') {
    setLogs(prev => [...prev, { msg, type, ts: Date.now() }])
    setTimeout(() => {
      if (logBoxRef.current) logBoxRef.current.scrollTop = logBoxRef.current.scrollHeight
    }, 30)
  }

  async function run() {
    if (!url.trim()) return
    setRunning(true); stopRef.current = false
    setLogs([]); setStats({ scraped: 0, uploaded: 0, skipped: 0, errors: 0 })

    // If user gave a rescan URL, use that as the crawl start.
    // fromChapter is auto-detected or manually set — chapters at or below it are skipped.
    const crawlStartUrl = rescanUrl.trim() || url.trim()

    let startFrom = fromCh
    if (autoFrom) {
      try {
        const chapters = await getNovelChapters(novel._id)
        startFrom = chapters?.length ? Math.max(...chapters.map(c => c.number || 0)) : 0
        addLog(`Auto-detected last stored chapter: Ch.${startFrom}`, 'info')
      } catch (e) {
        addLog(`Could not auto-detect: ${e.message}`, 'warn')
      }
    }

    if (rescanUrl.trim()) {
      addLog(`Rescan mode: crawl starts at ${crawlStartUrl}`, 'info')
    }

    const isUp = await checkServerHealth()
    setServerUp(isUp)

    if (isUp) {
      // ── Use Python local server ──────────────────────────────────────────
      addLog(`Using local Python server (full server-side scraping)`, 'info')
      let jobId
      try {
        jobId = await startJob({
          mode:             'scrape',
          start_url:        crawlStartUrl,
          novel_id:         novel._id,
          api_url:          localStorage.getItem('ns_api_url') || '',
          token:            localStorage.getItem('ns_token')   || '',
          from_chapter:     startFrom,
          delay:            delay,
          max_chapters:     maxCh,
          custom_watermarks: loadWatermarks(),
        })
        addLog(`Server job started (id: ${jobId})`, 'dim')
      } catch (e) {
        addLog(`Could not start server job: ${e.message}`, 'err')
        setRunning(false); return
      }

      cleanupRef.current = streamJob(jobId, {
        onLog:   (entry) => { if (!stopRef.current) addLog(entry.msg, entry.type) },
        onStats: (s)     => { if (!stopRef.current) setStats(s) },
        onDone:  (s)     => { setStats(s); setRunning(false); cleanupRef.current = null },
      })

    } else {
      // ── Browser fallback ─────────────────────────────────────────────────
      addLog(`Local server not running — using browser scraper (CORS proxy)`, 'warn')
      addLog(`For better results start: python scraper-server/scraper_server.py`, 'dim')

      let scraped = 0, uploaded = 0, skipped = 0, errors = 0
      const pendingBatch = []

      // Flush collected chapters to the backend via bulk import
      async function flushBatch() {
        if (pendingBatch.length === 0) return
        const batch = pendingBatch.splice(0)
        addLog(`↑ Uploading batch of ${batch.length} chapter(s)…`, 'info')
        try {
          const result = await bulkImportChapters(novel._id, batch, true)
          uploaded += result.created || 0
          skipped  += result.skipped || 0
          errors   += result.errors?.length || 0
          setStats(s => ({ ...s, uploaded, skipped, errors }))
          if (result.errors?.length) {
            result.errors.forEach(e => addLog(`✗ Ch.${e.number}: ${e.reason}`, 'err'))
          }
          addLog(`✓ Batch done — ${result.created} created, ${result.skipped} skipped`, 'ok')
        } catch (e) {
          errors += batch.length
          setStats(s => ({ ...s, errors }))
          addLog(`✗ Bulk upload failed: ${e.message}`, 'err')
        }
      }

      try {
        await scrapeForward(crawlStartUrl, {
          fromChapter: startFrom,
          maxChapters: maxCh,
          delay:       delay * 1000,
          watermarks:  loadWatermarks(),
          onLog:       addLog,
          onChapter:   async (ch) => {
            if (stopRef.current) return
            scraped++
            setStats(s => ({ ...s, scraped }))
            const content = (ch.content || '').trim()
            if (content.length < 50) {
              skipped++
              addLog(`⚠ Ch.${ch.number} skipped — no text content`, 'warn')
              setStats(s => ({ ...s, skipped }))
              return
            }
            pendingBatch.push({ number: ch.number, title: ch.title, content })
            addLog(`✎ Ch.${ch.number} — ${ch.title} queued (${content.split(/\s+/).filter(Boolean).length}w)`, 'dim')
            // Flush every 20 chapters to avoid huge single payloads
            if (pendingBatch.length >= 20) await flushBatch()
          },
        })
        // Upload any remaining chapters
        await flushBatch()
      } catch (e) {
        addLog(`Fatal error: ${e.message}`, 'err')
        await flushBatch()  // try to save whatever was collected before the error
      }
      addLog(`── Finished: ${scraped} found · ${uploaded} uploaded · ${skipped} skipped · ${errors} errors ──`,
        uploaded > 0 ? 'info' : 'warn')
      setRunning(false)
    }
  }

  function stop() {
    stopRef.current = true
    if (cleanupRef.current) { cleanupRef.current(); cleanupRef.current = null }
    setRunning(false)
    addLog('Stopped by user.', 'warn')
  }

  return (
    <div className={styles.overlay} onClick={e => e.target === e.currentTarget && onClose()}>
      <div className={styles.modal}>

        <div className={styles.header}>
          <div>
            <div className={styles.modalTitle}>Scrape Chapters</div>
            <div className={styles.modalSub}>{novel.title}</div>
          </div>
          <div style={{display:'flex',alignItems:'center',gap:10}}>
            <div className={`${styles.serverBadge} ${serverUp ? styles.serverUp : styles.serverDown}`}>
              {serverUp ? <Server size={11}/> : <Wifi size={11}/>}
              {serverUp ? 'Python server' : 'Browser fallback'}
            </div>
            <button className={styles.closeBtn} onClick={onClose}><X size={16}/></button>
          </div>
        </div>

        <div className={styles.body}>

          {!serverUp && (
            <div className={styles.serverWarning}>
              <strong>Python server not running.</strong> Using browser scraper (CORS proxy).
              For JS-rendered sites like LunoxScans, start the local server first:
              <code>python scraper-server/scraper_server.py</code>
            </div>
          )}

          <div className={styles.field}>
            <label>Starting chapter URL *</label>
            <input type="url" placeholder="https://oldsite.com/novel/name/chapter-1/"
              value={url} onChange={e => setUrl(e.target.value)} disabled={running}/>
            <span className={styles.hint}>
              {serverUp
                ? 'Server-side scraping — works with all sites including JS-rendered pages.'
                : 'Browser scraping via CORS proxy — may not work on JS-rendered sites.'}
            </span>
          </div>

          <div className={styles.field}>
            <label>Rescan from chapter URL <span style={{color:'var(--muted)', fontWeight:400}}>(optional)</span></label>
            <input type="url" placeholder="https://oldsite.com/novel/name/chapter-22/ — leave blank to start from chapter 1 URL above"
              value={rescanUrl} onChange={e => setRescanUrl(e.target.value)} disabled={running}/>
            <span className={styles.hint}>
              Paste the URL of the chapter to start crawling from. The scraper will begin here instead of the URL above — useful for rescanning from the middle without re-fetching everything from chapter 1.
            </span>
          </div>

          <div className={styles.row}>
            <div className={styles.field}>
              <label>Delay between requests (sec)</label>
              <input type="number" min="0.5" max="30" step="0.1"
                value={delay} onChange={e => setDelay(Number(e.target.value))} disabled={running}/>
            </div>
            <div className={styles.field}>
              <label>Max chapters</label>
              <input type="number" min="1" max="5000"
                value={maxCh} onChange={e => setMaxCh(Number(e.target.value))} disabled={running}/>
            </div>
          </div>

          <div className={styles.checkRow}>
            <label className={styles.checkLabel}>
              <input type="checkbox" checked={autoFrom}
                onChange={e => setAutoFrom(e.target.checked)} disabled={running}/>
              <span>Auto-detect starting point from existing chapters</span>
            </label>
          </div>

          {!autoFrom && (
            <div className={styles.field}>
              <label>Skip chapters up to (exclusive)</label>
              <input type="number" min="0"
                value={fromCh} onChange={e => setFromCh(Number(e.target.value))} disabled={running}/>
            </div>
          )}

          {(running || logs.length > 0) && (
            <div className={styles.statsRow}>
              <div className={styles.stat}>
                <span className={styles.statNum}>{stats.scraped}</span>
                <span className={styles.statLabel}>Found</span>
              </div>
              <div className={styles.stat}>
                <span className={styles.statNum} style={{color:'var(--success)'}}>{stats.uploaded}</span>
                <span className={styles.statLabel}>Uploaded</span>
              </div>
              <div className={styles.stat}>
                <span className={styles.statNum} style={{color:'var(--warning)'}}>{stats.skipped}</span>
                <span className={styles.statLabel}>No Content</span>
              </div>
              <div className={styles.stat}>
                <span className={styles.statNum} style={{color:'var(--accent3)'}}>{stats.errors}</span>
                <span className={styles.statLabel}>Errors</span>
              </div>
            </div>
          )}

          {logs.length > 0 && (
            <div className={styles.logBox} ref={logBoxRef}>
              {logs.map((l, i) => (
                <div key={i} className={`${styles.logLine} ${styles['log_' + l.type]}`}>
                  <span className={styles.logTime}>
                    {new Date(l.ts).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'})}
                  </span>
                  {l.msg}
                </div>
              ))}
            </div>
          )}
        </div>

        <div className={styles.footer}>
          {!running
            ? <button className={styles.btnRun} onClick={run} disabled={!url.trim()}>
                <Play size={14}/> Start Scraping
              </button>
            : <button className={styles.btnStop} onClick={stop}>
                <Square size={14}/> Stop
              </button>
          }
          <button className={styles.btnCancel} onClick={onClose} disabled={running}>Close</button>
        </div>

      </div>
    </div>
  )
}
