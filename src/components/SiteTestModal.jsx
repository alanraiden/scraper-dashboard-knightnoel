import { useState } from 'react'
import { X, FlaskConical, CheckCircle, XCircle, AlertTriangle, ArrowRight, Loader } from 'lucide-react'
import { testUrl } from '../lib/localScraper.js'
import styles from './SiteTestModal.module.css'

export default function SiteTestModal({ onClose }) {
  const [url,     setUrl]     = useState('')
  const [loading, setLoading] = useState(false)
  const [result,  setResult]  = useState(null)
  const [error,   setError]   = useState('')

  async function run() {
    const trimmed = url.trim()
    if (!trimmed) return
    setLoading(true)
    setResult(null)
    setError('')
    try {
      const data = await testUrl(trimmed)
      setResult(data)
    } catch (e) {
      setError(e.message.includes('Failed to fetch')
        ? 'Could not reach the Python server. Make sure scraper_server.py is running.'
        : e.message)
    } finally {
      setLoading(false)
    }
  }

  function handleKey(e) { if (e.key === 'Enter') run() }

  const ok      = result?.reachable && result?.content_length > 0
  const partial = result?.reachable && result?.content_length === 0

  return (
    <div className={styles.overlay} onClick={e => e.target === e.currentTarget && onClose()}>
      <div className={styles.modal}>

        {/* Header */}
        <div className={styles.header}>
          <div className={styles.title}><FlaskConical size={15}/> Test a Chapter URL</div>
          <button className={styles.closeBtn} onClick={onClose}><X size={15}/></button>
        </div>

        {/* Input */}
        <div className={styles.body}>
          <p className={styles.desc}>
            Paste a chapter URL to check if the scraper can reach and extract content from it.
            This only fetches one page — nothing is saved or uploaded.
          </p>

          <div className={styles.inputRow}>
            <input
              className={styles.input}
              type="url"
              placeholder="https://example.com/novel/title/chapter-1/"
              value={url}
              onChange={e => setUrl(e.target.value)}
              onKeyDown={handleKey}
              disabled={loading}
              autoFocus
            />
            <button className={styles.runBtn} onClick={run} disabled={loading || !url.trim()}>
              {loading ? <Loader size={14} className={styles.spin}/> : <ArrowRight size={14}/>}
              {loading ? 'Testing…' : 'Test'}
            </button>
          </div>

          {/* Error */}
          {error && (
            <div className={styles.errorBox}>
              <XCircle size={14}/> {error}
            </div>
          )}

          {/* Results */}
          {result && (
            <div className={styles.results}>

              {/* Status bar */}
              <div className={`${styles.statusBar} ${ok ? styles.statusOk : partial ? styles.statusWarn : styles.statusFail}`}>
                {ok
                  ? <><CheckCircle size={15}/> Site reachable — content extracted successfully</>
                  : partial
                  ? <><AlertTriangle size={15}/> Site reachable — but no content extracted</>
                  : <><XCircle size={15}/> Could not extract content (HTTP {result.status_code})</>
                }
              </div>

              {/* Warnings */}
              {result.warnings?.length > 0 && (
                <div className={styles.warnings}>
                  {result.warnings.map((w, i) => (
                    <div key={i} className={styles.warning}>
                      <AlertTriangle size={12}/> {w}
                    </div>
                  ))}
                </div>
              )}

              {/* Info grid */}
              <div className={styles.grid}>
                <Row label="HTTP Status"   value={result.status_code} highlight={result.status_code === 200 ? 'ok' : 'err'} />
                <Row label="Framework"     value={result.framework || 'Unknown/Static'} highlight={result.framework && result.framework !== 'Unknown/Static' ? 'warn' : null} />
                <Row label="Title Detected" value={result.title || '—'} />
                <Row label="Content Length" value={result.content_length ? `${result.content_length} chars / ${result.word_count} words` : 'None'} highlight={result.word_count > 200 ? 'ok' : result.word_count > 0 ? 'warn' : 'err'} />
                <Row label="Selector Used"  value={result.selector_used || 'None — JSON extraction used'} mono />
                <Row label="Next Chapter"   value={result.next_url || (result.framework ? 'Not in HTML — URL pattern will be inferred during crawl' : 'Not found')} highlight={result.next_url ? 'ok' : result.framework ? 'warn' : 'err'} truncate />
                <Row label="Prev Chapter"   value={result.prev_url || 'Not found'} truncate />
              </div>

              {/* Content preview */}
              {result.content_preview && (
                <div className={styles.previewSection}>
                  <div className={styles.previewLabel}>Extracted Content</div>
                  <div className={styles.preview}>{result.content_preview}</div>
                </div>
              )}

              {/* Verdict */}
              <div className={`${styles.verdict} ${ok ? styles.verdictOk : styles.verdictWarn}`}>
                {ok
                  ? `✓ This site works with the scraper. You can add the chapter-1 URL to the dashboard.`
                  : (result.is_nextjs || result.is_sveltekit || result.is_nuxt)
                  ? `⚠ ${result.framework} site detected. Content loads via JavaScript — the scraper tried JSON extraction. If content is empty, the site may encrypt its data or require authentication.`
                  : result.is_madara
                  ? '⚠ Madara/WordPress site — content loads via AJAX. Try running a real scrape to confirm the AJAX fallback works.'
                  : '✗ Content could not be extracted. This site may block scrapers or use a layout the scraper does not support yet.'}
              </div>

            </div>
          )}
        </div>

      </div>
    </div>
  )
}

function Row({ label, value, highlight, mono, truncate }) {
  const cls = highlight === 'ok'   ? styles.valOk
            : highlight === 'warn' ? styles.valWarn
            : highlight === 'err'  ? styles.valErr
            : ''
  return (
    <div className={styles.row}>
      <span className={styles.rowLabel}>{label}</span>
      <span className={`${styles.rowVal} ${cls} ${mono ? styles.mono : ''} ${truncate ? styles.truncate : ''}`}>
        {value}
      </span>
    </div>
  )
}
