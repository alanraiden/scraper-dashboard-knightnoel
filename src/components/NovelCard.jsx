import { useState } from 'react'
import {
  Play, Square, RefreshCw, Trash2, ChevronDown, ChevronUp, RotateCcw,
  Clock, BookOpen, Wifi, WifiOff, AlertCircle, Search
} from 'lucide-react'
import { getNovelChapters } from '../lib/api.js'
import NovelStatusBadge, { getNovelStatus } from './NovelStatus.jsx'
import styles from './NovelCard.module.css'

export default function NovelCard({ novel, watchEntry, isWatching, isRunning, onAddWatch, onRemoveWatch, onStartWatch, onStopWatch, onRunOnce, onResetChapter, onUpdateChainUrl, logs, novelCollections = [], onStatusChange }) {
  const [expanded,      setExpanded]      = useState(false)
  const [showForm,      setShowForm]      = useState(!watchEntry)
  const [gaps,          setGaps]          = useState(null)
  const [gapLoading,    setGapLoading]    = useState(false)
  const [url,           setUrl]           = useState(watchEntry?.startUrl || '')
  const [chOneUrl,      setChOneUrl]      = useState(watchEntry?.chapterOneUrl || '')
  const [mode,          setMode]          = useState(watchEntry?.mode || 'index')
  const [selector,      setSelector]      = useState(watchEntry?.checkSelector || '')
  const [interval,      setInterval]      = useState(watchEntry?.intervalHours || 1)
  const [editingChain,  setEditingChain]  = useState(false)
  const [chainUrlDraft, setChainUrlDraft] = useState('')
  const [statusId,      setStatusId]      = useState(() => getNovelStatus(novel._id))

  // Last log entry — used for error badge
  const lastLog = logs[logs.length - 1]
  const hasError = lastLog && lastLog.type === 'err' && !isRunning

  // Sync form fields from watchEntry every time the edit form opens.
  // Without this, useState keeps the stale value from first render.
  async function checkGaps() {
    if (!novel.slug) { setGaps([]); return }
    setGapLoading(true)
    setGaps(null)
    try {
      const chapters = await getNovelChapters(novel.slug)
      if (!chapters?.length) { setGaps([]); return }
      const stored = new Set(chapters.map(c => c.number || 0))
      const maxNum = Math.max(...stored)
      const missing = []
      for (let i = 1; i <= maxNum; i++) {
        if (!stored.has(i)) missing.push(i)
      }
      setGaps(missing)
    } catch (e) {
      setGaps([])
    } finally {
      setGapLoading(false)
    }
  }

  function openEditForm() {
    setUrl(watchEntry?.startUrl || '')
    setChOneUrl(watchEntry?.chapterOneUrl || '')
    setMode(watchEntry?.mode || 'index')
    setSelector(watchEntry?.checkSelector || '')
    setInterval(watchEntry?.intervalHours || 1)
    setShowForm(true)
  }

  function handleSave() {
    if (!url.trim()) return
    onAddWatch({
      novelId:    novel._id,
      novelSlug:  novel.slug || '',   // ← required for the scraper API slug-based upload
      novelTitle: novel.title,
      startUrl:   url.trim(),
      chapterOneUrl: chOneUrl.trim() || url.trim(),
      mode,
      checkSelector: selector.trim(),
      intervalHours: Number(interval),
    })
    setShowForm(false)
  }

  const lastCheckedLabel = watchEntry?.lastChecked
    ? timeAgo(watchEntry.lastChecked)
    : 'never'

  const statusColor = isWatching
    ? 'var(--accent2)'
    : watchEntry ? 'var(--muted)' : 'var(--border)'

  return (
    <div className={`${styles.card} ${isRunning ? styles.active : ''} ${hasError ? styles.hasError : ''}`}>
      {/* ── Top row ── */}
      <div className={styles.top}>
        <div className={styles.statusDot} style={{ background: statusColor, boxShadow: isWatching ? `0 0 8px ${statusColor}` : 'none' }} />
        <div className={styles.info}>
          <div className={styles.title}>{novel.title}</div>
          <div className={styles.meta}>
            <span className={styles.pill}><BookOpen size={11}/> {novel.chapterCount} ch</span>
            {watchEntry && <span className={styles.pill}><Clock size={11}/> checked {lastCheckedLabel}</span>}
            {watchEntry?.lastChapter > 0 && <span className={`${styles.pill} ${styles.pillAccent}`}>last: Ch.{watchEntry.lastChapter}</span>}
            {isRunning && <span className={`${styles.pill} ${styles.pillRunning}`}><RefreshCw size={10} className={styles.spin}/> running…</span>}
            {hasError && (
              <span className={`${styles.pill} ${styles.pillError}`} title={lastLog.msg}>
                ✗ last run failed
              </span>
            )}
            {novelCollections.map(col => {
              const colorHex = { yellow:'#e8c547', teal:'#4ecdc4', purple:'#a78bfa', red:'#ff6b6b', green:'#4ade80', blue:'#60a5fa', orange:'#fb923c', pink:'#f472b6' }[col.color] || '#e8c547'
              return (
                <span key={col.id} className={styles.pill} style={{ borderColor: colorHex + '55', color: colorHex, background: colorHex + '12' }}>
                  {col.name}
                </span>
              )
            })}
            <NovelStatusBadge
              novelId={novel._id}
              statusId={statusId}
              onChange={id => { setStatusId(id); onStatusChange?.(novel._id, id) }}
            />
          </div>
        </div>

        <div className={styles.actions}>
          {watchEntry && !isWatching && (
            <button className={`${styles.btn} ${styles.btnPlay}`} title="Start auto-watcher"
              onClick={() => onStartWatch(novel._id, watchEntry.intervalHours)}
              disabled={isRunning}>
              <Play size={13}/>
            </button>
          )}
          {isWatching && (
            <button className={`${styles.btn} ${styles.btnStop}`} title="Stop watcher"
              onClick={() => onStopWatch(novel._id)}>
              <Square size={13}/>
            </button>
          )}
          {watchEntry && (
            <button className={`${styles.btn} ${styles.btnRefresh}`} title="Check now"
              onClick={() => onRunOnce(novel._id)} disabled={isRunning}>
              <RefreshCw size={13} className={isRunning ? styles.spin : ''}/>
            </button>
          )}
          {watchEntry && (
            <button className={`${styles.btn} ${styles.btnDanger}`} title="Remove"
              onClick={() => onRemoveWatch(novel._id)}>
              <Trash2 size={13}/>
            </button>
          )}
          <button className={`${styles.btn} ${styles.btnGhost}`} onClick={() => setExpanded(v => !v)}>
            {expanded ? <ChevronUp size={14}/> : <ChevronDown size={14}/>}
          </button>
        </div>
      </div>

      {/* ── Expanded section ── */}
      {expanded && (
        <div className={styles.body}>
          {/* Config form */}
          {showForm ? (
            <div className={styles.form}>
              <div className={styles.formTitle}>
                {watchEntry ? 'Edit scraper config' : 'Set up scraper'}
              </div>
              <div className={styles.field}>
                <label>Watch URL *</label>
                <input
                  type="url"
                  placeholder={
                    mode === 'index'  ? "https://site.com/novel/title/ (novel index page)" :
                    mode === 'chain'  ? "https://site.com/novel/title/chapter-1/ (will auto-update after each scrape)" :
                                       "https://site.com/novel/title/chapter-99/ (latest chapter page)"
                  }
                  value={url} onChange={e => setUrl(e.target.value)}
                />
                <span className={styles.hint}>
                  {mode === 'index'
                    ? "The novel's table-of-contents/index page — used to detect the latest chapter number."
                    : mode === 'chain'
                    ? "Starting chapter URL. After each scrape this is automatically updated to the last chapter found, so the next check picks up right where it left off."
                    : 'URL of the most recently released chapter — used to detect if new chapters exist.'}
                </span>
              </div>
              <div className={styles.field}>
                <label>Chapter 1 URL <span style={{color:'var(--muted)', fontWeight:400}}>(for first-time full scrape)</span></label>
                <input
                  type="url"
                  placeholder="https://site.com/novel/title/chapter-1/"
                  value={chOneUrl} onChange={e => setChOneUrl(e.target.value)}
                />
                <span className={styles.hint}>Where to start crawling from when scraping all chapters. Leave blank to use Watch URL.</span>
              </div>
              <div className={styles.row}>
                <div className={styles.field}>
                  <label>Mode</label>
                  <select value={mode} onChange={e => setMode(e.target.value)}>
                    <option value="index">index — novel index page</option>
                    <option value="latest">latest — most recent chapter page</option>
                    <option value="chain">chain — follow next-chapter links automatically</option>
                  </select>
                </div>
                <div className={styles.field}>
                  <label>Check interval (hours)</label>
                  <input type="number" min="0.25" max="24" step="0.25"
                    value={interval} onChange={e => setInterval(e.target.value)} />
                </div>
              </div>
              {mode === 'index' && (
                <div className={styles.field}>
                  <label>Chapter list selector <span style={{color:'var(--muted)', fontWeight:400}}>(optional)</span></label>
                  <input type="text"
                    placeholder="e.g. ul.chapter-list li:first-child a"
                    value={selector} onChange={e => setSelector(e.target.value)}
                  />
                  <span className={styles.hint}>CSS selector pointing to the latest chapter link. Leave blank for auto-detection.</span>
                </div>
              )}
              <div className={styles.formActions}>
                <button className={styles.btnPrimary} onClick={handleSave}>
                  {watchEntry ? 'Update' : 'Save & Enable'}
                </button>
                {watchEntry && <button className={styles.btnGhostSm} onClick={() => setShowForm(false)}>Cancel</button>}
              </div>
            </div>
          ) : (
            <div className={styles.configDisplay}>
              <div className={styles.configRow}>
                <span className={styles.configLabel}>URL</span>
                <span className={styles.configVal}>{watchEntry.startUrl}</span>
              </div>
              <div className={styles.configRow}>
                <span className={styles.configLabel}>Mode</span>
                <span className={styles.configVal}>{watchEntry.mode}</span>
              </div>
              {watchEntry.mode === 'chain' && (
                <div className={styles.chainRow}>
                  <span className={styles.configLabel}>Chain URL</span>
                  {editingChain ? (
                    <div className={styles.chainEdit}>
                      <input
                        className={styles.chainInput}
                        value={chainUrlDraft}
                        onChange={e => setChainUrlDraft(e.target.value)}
                        placeholder="https://site.com/novel/chapter-X/"
                        autoFocus
                      />
                      <button className={styles.btnPrimary} style={{padding:'2px 8px', fontSize:'0.75em'}} onClick={() => {
                        if (chainUrlDraft.trim()) onUpdateChainUrl?.(novel._id, chainUrlDraft.trim())
                        setEditingChain(false)
                      }}>Save</button>
                      <button className={styles.btnGhostSm} onClick={() => setEditingChain(false)}>✕</button>
                    </div>
                  ) : (
                    <div className={styles.chainDisplay}>
                      <span className={styles.chainUrl}>
                        {watchEntry.lastChapterUrl || watchEntry.startUrl}
                      </span>
                      <button className={styles.btnGhostSm} style={{marginLeft:'6px', flexShrink:0}} onClick={() => {
                        setChainUrlDraft(watchEntry.lastChapterUrl || watchEntry.startUrl)
                        setEditingChain(true)
                      }}>✎ Edit</button>
                    </div>
                  )}
                </div>
              )}
              <div className={styles.configRow}>
                <span className={styles.configLabel}>Interval</span>
                <span className={styles.configVal}>every {watchEntry.intervalHours}h</span>
              </div>
              <button className={styles.btnGhostSm} onClick={openEditForm}>Edit config</button>
              <button className={styles.btnReset} onClick={() => { const v = prompt("Reset last chapter pointer to:", watchEntry.lastChapter); if (v !== null && !isNaN(Number(v))) onResetChapter?.(novel._id, Number(v)) }} title="Fix stuck chapter counter">↺ Reset Ch. pointer</button>
              <button className={styles.btnGhostSm} onClick={checkGaps} disabled={gapLoading} title="Find missing chapters">
                <Search size={11}/> {gapLoading ? 'Checking…' : 'Find gaps'}
              </button>
              {gaps !== null && (
                <div className={styles.gapResult}>
                  {gaps.length === 0
                    ? <span className={styles.gapNone}>✓ No missing chapters</span>
                    : <span className={styles.gapFound}>
                        ✗ Missing: Ch.{gaps.slice(0, 20).join(', Ch.')}{gaps.length > 20 ? ` … +${gaps.length - 20} more` : ''} — next watcher run will fix these automatically
                      </span>
                  }
                </div>
              )}
            </div>
          )}

          {/* Log for this novel */}
          {logs?.length > 0 && (
            <div className={styles.logBox}>
              {logs.slice(-40).map((l, i) => (
                <div key={i} className={`${styles.logLine} ${styles['log_' + (l.type || 'info')]}`}>
                  <span className={styles.logTime}>{formatTime(l.ts)}</span>
                  {l.msg}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function timeAgo(ts) {
  const diff = Date.now() - ts
  if (diff < 60000) return 'just now'
  if (diff < 3600000) return Math.floor(diff / 60000) + 'm ago'
  if (diff < 86400000) return Math.floor(diff / 3600000) + 'h ago'
  return Math.floor(diff / 86400000) + 'd ago'
}
function formatTime(ts) {
  return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}
