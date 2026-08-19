import { useState } from 'react'
import { BookOpen, Key, Link, LogIn, CheckCircle } from 'lucide-react'
import { saveCredentials, login } from '../lib/api.js'
import styles from './ConnectPanel.module.css'

export default function ConnectPanel({ onConnected }) {
  const savedUrl = localStorage.getItem('kn_api_url') || 'http://localhost:3000'
  const savedKey = localStorage.getItem('kn_scraper_key') || ''

  const [apiUrl,  setApiUrl]  = useState(savedUrl)
  const [apiKey,  setApiKey]  = useState(savedKey)
  const [error,   setError]   = useState('')
  const [loading, setLoading] = useState(false)

  async function handleConnect(e) {
    e.preventDefault()
    const url = apiUrl.trim().replace(/\/$/, '')
    const key = apiKey.trim()
    if (!url)  { setError('Enter the Knight Novel backend URL.'); return }
    if (!key)  { setError('Enter the Scraper API key.'); return }
    setLoading(true); setError('')
    try {
      const data = await login(url, key)
      if (!data.ok) throw new Error('Key rejected by server.')
      saveCredentials(url, key)
      onConnected(data.user)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className={styles.wrap}>
      <div className={styles.card}>
        <div className={styles.icon}><BookOpen size={26}/></div>
        <h2 className={styles.title}>Knight Novel Scraper</h2>
        <p className={styles.sub}>Connect to your local Knight Novel site to manage and scrape novels</p>

        <form className={styles.emailForm} onSubmit={handleConnect}>
          <div className={styles.field}>
            <label><Link size={13} style={{display:'inline',marginRight:5}}/>Backend URL</label>
            <div className={styles.inputWrap}>
              <input
                type="url"
                placeholder="http://localhost:3000"
                value={apiUrl}
                onChange={e => { setApiUrl(e.target.value); setError('') }}
                disabled={loading}
              />
            </div>
            <span className={styles.hint}>Your local Knight Novel Next.js dev server URL</span>
          </div>

          <div className={styles.field}>
            <label><Key size={13} style={{display:'inline',marginRight:5}}/>Scraper API Key</label>
            <div className={styles.inputWrap}>
              <input
                type="password"
                placeholder="skpr_…"
                value={apiKey}
                onChange={e => { setApiKey(e.target.value); setError('') }}
                disabled={loading}
              />
            </div>
            <span className={styles.hint}>
              Set in Knight Novel's <code>.env.local</code> as <code>SCRAPER_API_KEY</code>
            </span>
          </div>

          {error && <div className={styles.error}>{error}</div>}

          <button type="submit" className={styles.emailBtn} disabled={loading || !apiUrl.trim() || !apiKey.trim()}>
            {loading
              ? <><span className={styles.spinner}/> Connecting…</>
              : <><LogIn size={14}/> Connect</>}
          </button>
        </form>

        <div className={styles.infoBox}>
          <CheckCircle size={13} style={{flexShrink:0, color:'var(--success, #4ade80)'}}/>
          <div>
            Your API key is: <code style={{fontSize:'0.7rem', wordBreak:'break-all'}}>skpr_7f4a2b9e1c6d3f8a5b0e7d4c2a9f6e3b1d8c5a2</code>
            <br/>It's already set in Knight Novel's <code>.env.local</code>.
          </div>
        </div>

        <p className={styles.footer}>
          Make sure your Knight Novel dev server is running on <code>{apiUrl || 'http://localhost:3000'}</code>
        </p>
      </div>
    </div>
  )
}
