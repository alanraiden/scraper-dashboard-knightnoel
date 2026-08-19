import { useState, useEffect } from 'react'
import { Shield, Plus, Trash2, X, Save, Info } from 'lucide-react'
import styles from './WatermarkEditor.module.css'

const STORAGE_KEY = 'ns_custom_watermarks'

/**
 * Storage format (v2):
 * [{ phrase: string, titleReplace: string | null }, ...]
 * Legacy v1 (plain string array) is auto-migrated on load.
 */
export function loadWatermarks() {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]')
    if (Array.isArray(raw) && raw.length > 0 && typeof raw[0] === 'string') {
      return raw.map(phrase => ({ phrase, titleReplace: null }))
    }
    return raw
  } catch {
    return []
  }
}

export function saveWatermarks(list) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(list))
}

export default function WatermarkEditor({ onClose }) {
  const [entries,      setEntries]      = useState(loadWatermarks)
  const [phrase,       setPhrase]       = useState('')
  const [titleReplace, setTitleReplace] = useState('')
  const [saved,        setSaved]        = useState(false)
  const [showHelp,     setShowHelp]     = useState(false)

  function add() {
    const trimmedPhrase = phrase.trim()
    if (!trimmedPhrase) return
    if (entries.some(e => e.phrase === trimmedPhrase)) return
    setEntries(prev => [...prev, {
      phrase:       trimmedPhrase,
      titleReplace: titleReplace.trim() || null,
    }])
    setPhrase('')
    setTitleReplace('')
    setSaved(false)
  }

  function remove(idx) {
    setEntries(prev => prev.filter((_, i) => i !== idx))
    setSaved(false)
  }

  function updateReplace(idx, val) {
    setEntries(prev => prev.map((e, i) =>
      i === idx ? { ...e, titleReplace: val.trim() || null } : e
    ))
    setSaved(false)
  }

  function save() {
    saveWatermarks(entries)
    setSaved(true)
    setTimeout(() => setSaved(false), 2000)
  }

  function handleKey(e) {
    if (e.key === 'Enter') add()
  }

  return (
    <div className={styles.overlay} onClick={e => e.target === e.currentTarget && onClose()}>
      <div className={styles.panel}>

        <div className={styles.header}>
          <div className={styles.title}><Shield size={15}/> Custom Watermark Phrases</div>
          <div style={{ display: 'flex', gap: 6 }}>
            <button className={styles.closeBtn} title="Help" onClick={() => setShowHelp(v => !v)}>
              <Info size={15}/>
            </button>
            <button className={styles.closeBtn} onClick={onClose}><X size={15}/></button>
          </div>
        </div>

        {showHelp && (
          <div className={styles.helpBox}>
            <strong>How it works:</strong>
            <ul>
              <li><b>Match phrase</b> — any <em>content line</em> containing this phrase is removed. Also applied to the chapter <em>title</em>.</li>
              <li><b>Replace in title with</b> — when the phrase is found in a title, swap it for this text instead of deleting the whole segment. Leave blank to just strip it out.</li>
            </ul>
            <em>Example:</em> phrase <code>NovelSite.com</code> with blank replacement → deleted from titles.<br/>
            phrase <code>Web Novel</code> → replace with <code>Novel</code> renames cleanly.
          </div>
        )}

        <div className={styles.body}>
          <p className={styles.description}>
            Phrases are matched case-insensitively in both <strong>content lines</strong> and <strong>chapter titles</strong>.
            Set "Replace in title with" to substitute instead of delete when found in a title.
          </p>

          <div className={styles.addBlock}>
            <div className={styles.addRow}>
              <input
                className={styles.input}
                type="text"
                placeholder='Phrase to match, e.g. "Translated by XYZ"'
                value={phrase}
                onChange={e => setPhrase(e.target.value)}
                onKeyDown={handleKey}
              />
              <button className={styles.addBtn} onClick={add} disabled={!phrase.trim()}>
                <Plus size={14}/> Add
              </button>
            </div>
            <div className={styles.replaceRow}>
              <span className={styles.replaceLabel}>Replace in title with:</span>
              <input
                className={`${styles.input} ${styles.replaceInput}`}
                type="text"
                placeholder="Leave blank to delete"
                value={titleReplace}
                onChange={e => setTitleReplace(e.target.value)}
                onKeyDown={handleKey}
              />
            </div>
          </div>

          {entries.length === 0 ? (
            <div className={styles.empty}>No custom phrases yet. Add some above.</div>
          ) : (
            <div className={styles.list}>
              <div className={styles.listHeader}>
                <span>Match phrase</span>
                <span>Replace in title with</span>
                <span/>
              </div>
              {entries.map((entry, i) => (
                <div key={i} className={styles.phrase}>
                  <span className={styles.phraseText}>{entry.phrase}</span>
                  <input
                    className={`${styles.input} ${styles.inlineReplace}`}
                    type="text"
                    placeholder="(delete)"
                    value={entry.titleReplace ?? ''}
                    onChange={e => updateReplace(i, e.target.value)}
                  />
                  <button className={styles.removeBtn} onClick={() => remove(i)}>
                    <Trash2 size={12}/>
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className={styles.footer}>
          <span className={styles.hint}>Changes take effect on the next scrape run.</span>
          <button className={`${styles.saveBtn} ${saved ? styles.saveBtnDone : ''}`} onClick={save}>
            <Save size={13}/>
            {saved ? 'Saved!' : 'Save Changes'}
          </button>
        </div>

      </div>
    </div>
  )
}
