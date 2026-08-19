import { useState, useEffect } from 'react'
import { History, ChevronDown, ChevronUp, Trash2, X } from 'lucide-react'
import styles from './JobHistory.module.css'

// kn_job_history: KN-specific key (avoids mixing with old ns_job_history from
// the NovaSphere scraper which ran on the same localhost port).
const STORAGE_KEY = 'kn_job_history'
const MAX_JOBS    = 50

export function saveJobToHistory(job) {
  try {
    const history = loadHistory()
    history.unshift(job)
    localStorage.setItem(STORAGE_KEY, JSON.stringify(history.slice(0, MAX_JOBS)))
  } catch {}
}

function loadHistory() {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]') } catch { return [] }
}

export default function JobHistory({ onClose }) {
  const [history,   setHistory]   = useState(loadHistory)
  const [expanded,  setExpanded]  = useState(null)

  useEffect(() => {
    setHistory(loadHistory())
  }, [])

  function clearAll() {
    localStorage.removeItem(STORAGE_KEY)
    setHistory([])
  }

  function deleteOne(id) {
    const next = history.filter(j => j.id !== id)
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next))
    setHistory(next)
    if (expanded === id) setExpanded(null)
  }

  function statusColor(status) {
    if (status === 'done_ok')   return 'var(--success)'
    if (status === 'done_warn') return 'var(--warning)'
    if (status === 'error')     return 'var(--accent3)'
    return 'var(--muted)'
  }

  function statusLabel(job) {
    if (job.stats.uploaded > 0) return `✓ ${job.stats.uploaded} uploaded`
    if (job.stats.errors   > 0) return `✗ ${job.stats.errors} errors`
    return 'No new chapters'
  }

  return (
    <div className={styles.overlay} onClick={e => e.target === e.currentTarget && onClose()}>
      <div className={styles.panel}>

        <div className={styles.header}>
          <div className={styles.title}><History size={15}/> Job History</div>
          <div className={styles.headerActions}>
            {history.length > 0 && (
              <button className={styles.clearBtn} onClick={clearAll}>
                <Trash2 size={12}/> Clear all
              </button>
            )}
            <button className={styles.closeBtn} onClick={onClose}><X size={15}/></button>
          </div>
        </div>

        {history.length === 0 ? (
          <div className={styles.empty}>No job history yet. Run a scrape or watch check to see it here.</div>
        ) : (
          <div className={styles.list}>
            {history.map(job => (
              <div key={job.id} className={styles.job}>
                <div className={styles.jobHeader} onClick={() => setExpanded(expanded === job.id ? null : job.id)}>
                  <div className={styles.jobLeft}>
                    <span className={styles.jobDot} style={{ background: statusColor(job.status) }}/>
                    <div>
                      <div className={styles.jobTitle}>{job.novelTitle}</div>
                      <div className={styles.jobMeta}>
                        <span className={styles.jobType}>{job.mode === 'watch_check' ? 'Watch check' : 'Scrape'}</span>
                        <span className={styles.jobTime}>{formatTime(job.startedAt)}</span>
                        <span className={styles.jobDuration}>{duration(job.startedAt, job.finishedAt)}</span>
                      </div>
                    </div>
                  </div>
                  <div className={styles.jobRight}>
                    <span className={styles.jobStatus} style={{ color: statusColor(job.status) }}>
                      {statusLabel(job)}
                    </span>
                    <div className={styles.jobStats}>
                      <span title="Scraped">📄 {job.stats.scraped}</span>
                      <span title="Uploaded" style={{color:'var(--success)'}}>↑ {job.stats.uploaded}</span>
                      {job.stats.skipped > 0 && <span title="Skipped" style={{color:'var(--warning)'}}>⚠ {job.stats.skipped}</span>}
                      {job.stats.errors  > 0 && <span title="Errors"  style={{color:'var(--accent3)'}}>✗ {job.stats.errors}</span>}
                    </div>
                    <button className={styles.deleteBtn} onClick={e => { e.stopPropagation(); deleteOne(job.id) }}>
                      <Trash2 size={11}/>
                    </button>
                    {expanded === job.id ? <ChevronUp size={13}/> : <ChevronDown size={13}/>}
                  </div>
                </div>

                {expanded === job.id && (
                  <div className={styles.logBox}>
                    {(job.logs || []).map((l, i) => (
                      <div key={i} className={`${styles.logLine} ${styles['log_' + (l.type || 'info')]}`}>
                        <span className={styles.logTime}>
                          {new Date(l.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                        </span>
                        {l.msg}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function formatTime(ts) {
  if (!ts) return ''
  return new Date(ts).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function duration(start, end) {
  if (!start || !end) return ''
  const s = Math.round((end - start) / 1000)
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m ${s % 60}s`
}
