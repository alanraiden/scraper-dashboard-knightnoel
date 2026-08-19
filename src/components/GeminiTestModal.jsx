import { useState } from 'react'
import { X, Sparkles, AlertTriangle, CheckCircle, XCircle, Loader } from 'lucide-react'
import styles from './SiteTestModal.module.css'
import gemStyles from './GeminiTestModal.module.css'

const SERVER = '/api-local'

async function testGemini(text) {
  const res = await fetch(`${SERVER}/gemini/test`, {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ text }),
  })
  if (res.status === 503) {
    const d = await res.json()
    throw new Error(d.error || 'Gemini not configured')
  }
  if (!res.ok) throw new Error(`Server error ${res.status}`)
  return res.json()
}

export default function GeminiTestModal({ onClose }) {
  const [text,    setText]    = useState('')
  const [loading, setLoading] = useState(false)
  const [result,  setResult]  = useState(null)
  const [error,   setError]   = useState('')
  const [tab,     setTab]     = useState('original') // original | cleaned

  async function run() {
    if (!text.trim()) return
    setLoading(true)
    setResult(null)
    setError('')
    try {
      const data = await testGemini(text.trim())
      setResult(data)
      setTab('cleaned')
    } catch (e) {
      setError(
        e.message.includes('Failed to fetch') || e.message.includes('reach')
          ? 'Could not reach the Python server. Make sure scraper_server.py is running.'
          : e.message
      )
    } finally {
      setLoading(false)
    }
  }

  function handleKey(e) { if ((e.key === 'Enter') && (e.ctrlKey || e.metaKey)) run() }

  const scoreColor = result
    ? result.suspicion_score >= 0.6 ? 'var(--accent3)'
    : result.suspicion_score >= 0.3 ? 'var(--warning)'
    : 'var(--success)'
    : undefined

  // Bug 4 fix: medium range should not say "cleaned" — at 0.3–0.6 Gemini runs
  // but may remove nothing. Base the label on removed_words, not score alone.
  const wasCleaned = result && result.removed_words > 0
  const scoreLabel = result
    ? result.suspicion_score >= 0.6
      ? wasCleaned ? 'High — noise removed' : 'High — nothing to strip'
    : result.suspicion_score >= 0.3
      ? wasCleaned ? 'Medium — noise removed' : 'Medium — passed through clean'
    : 'Low — Gemini skipped'
    : ''

  return (
    <div className={styles.overlay} onClick={e => e.target === e.currentTarget && onClose()}>
      <div className={`${styles.modal} ${gemStyles.wide}`}>

        <div className={styles.header}>
          <div className={styles.title}><Sparkles size={15}/> Test Gemini Cleaner</div>
          <button className={styles.closeBtn} onClick={onClose}><X size={15}/></button>
        </div>

        <div className={styles.body}>
          <p className={styles.desc}>
            Paste raw chapter text to see the suspicion score and what Gemini would strip out.
            Nothing is uploaded — this is read-only. Requires Python server + <code>GEMINI_API_KEY</code>.
          </p>

          {/* Input */}
          <textarea
            className={gemStyles.textarea}
            placeholder="Paste scraped chapter text here…"
            value={text}
            onChange={e => setText(e.target.value)}
            onKeyDown={handleKey}
            disabled={loading}
            rows={8}
          />

          <div className={gemStyles.actions}>
            <span className={gemStyles.hint}>{text.trim() ? `${text.trim().split(/\s+/).length} words` : ''}</span>
            <button className={styles.runBtn} onClick={run} disabled={loading || !text.trim()}>
              {loading ? <><Loader size={13} className={styles.spin}/> Cleaning…</> : <><Sparkles size={13}/> Run Gemini</>}
            </button>
          </div>

          {error && (
            <div className={styles.errorBox}>
              <XCircle size={13}/> {error}
            </div>
          )}

          {result && (
            <div className={styles.results}>

              {/* Score bar */}
              <div className={gemStyles.scoreBar}>
                <div className={gemStyles.scoreLeft}>
                  <span className={gemStyles.scoreLabel}>Suspicion score</span>
                  <span className={gemStyles.scoreNum} style={{ color: scoreColor }}>
                    {result.suspicion_score.toFixed(2)}
                  </span>
                </div>
                <div className={gemStyles.scoreMeter}>
                  <div
                    className={gemStyles.scoreFill}
                    style={{ width: `${Math.round(result.suspicion_score * 100)}%`, background: scoreColor }}
                  />
                </div>
                <span className={gemStyles.scoreTag} style={{ color: scoreColor }}>{scoreLabel}</span>
              </div>

              {/* Stats grid */}
              <div className={styles.grid}>
                <div className={styles.row}>
                  <span className={styles.rowLabel}>Original words</span>
                  <span className={`${styles.rowVal} ${styles.mono}`}>{result.original_words.toLocaleString()}</span>
                </div>
                <div className={styles.row}>
                  <span className={styles.rowLabel}>After cleaning</span>
                  <span className={`${styles.rowVal} ${styles.mono}`}>{result.cleaned_words.toLocaleString()}</span>
                </div>
                <div className={styles.row}>
                  <span className={styles.rowLabel}>Words removed</span>
                  <span className={`${styles.rowVal} ${styles.mono}`}
                    style={{ color: result.removed_words > 0 ? 'var(--warning)' : 'var(--success)' }}>
                    {result.removed_words > 0 ? `−${result.removed_words}` : '0'}{' '}
                    {result.removed_words > 0 && <span style={{ color: 'var(--muted)', fontWeight: 400 }}>({(result.removed_ratio * 100).toFixed(1)}%)</span>}
                  </span>
                </div>
              </div>

              {/* Side-by-side tabs */}
              <div className={gemStyles.tabs}>
                <button
                  className={`${gemStyles.tab} ${tab === 'original' ? gemStyles.tabActive : ''}`}
                  onClick={() => setTab('original')}
                >Original</button>
                <button
                  className={`${gemStyles.tab} ${tab === 'cleaned' ? gemStyles.tabActive : ''}`}
                  onClick={() => setTab('cleaned')}
                >
                  Cleaned
                  {result.removed_words > 0 && <span className={gemStyles.tabBadge}>−{result.removed_words}</span>}
                </button>
              </div>

              <pre className={`${styles.preview} ${gemStyles.preview}`}>
                {tab === 'original' ? text.trim() : result.cleaned_text}
              </pre>

              {result.removed_words === 0 && (
                <div className={`${styles.verdict} ${styles.verdictOk}`}>
                  <CheckCircle size={12} style={{ display: 'inline', marginRight: 5 }}/>
                  Content looks clean — Gemini found nothing to strip.
                </div>
              )}
              {result.removed_words > 0 && result.removed_ratio <= 0.4 && (
                <div className={`${styles.verdict} ${styles.verdictWarn}`}>
                  <AlertTriangle size={12} style={{ display: 'inline', marginRight: 5 }}/>
                  Gemini removed {result.removed_words} word{result.removed_words !== 1 ? 's' : ''} of UI noise. Check the cleaned tab to verify it didn't touch story content.
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
