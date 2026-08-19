// NovelStatus.jsx — per-novel status labels + inline badge/picker
import { useState, useRef, useEffect } from 'react'
import styles from './NovelStatus.module.css'

const STORAGE_KEY = 'ns_novel_status'

export const STATUS_OPTIONS = [
  { id: 'active',    label: 'Active',    color: '#4ade80', bg: 'rgba(74,222,128,0.12)',  border: 'rgba(74,222,128,0.3)'  },
  { id: 'hiatus',    label: 'Hiatus',    color: '#fbbf24', bg: 'rgba(251,191,36,0.12)',  border: 'rgba(251,191,36,0.3)'  },
  { id: 'completed', label: 'Completed', color: '#60a5fa', bg: 'rgba(96,165,250,0.12)',  border: 'rgba(96,165,250,0.3)'  },
  { id: 'dropped',   label: 'Dropped',   color: '#ff6b6b', bg: 'rgba(255,107,107,0.12)', border: 'rgba(255,107,107,0.3)' },
]

export function loadNovelStatuses() {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}') } catch { return {} }
}
export function saveNovelStatuses(map) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(map))
}
export function getNovelStatus(novelId) {
  return loadNovelStatuses()[novelId] || null
}
export function setNovelStatus(novelId, statusId) {
  const map = loadNovelStatuses()
  if (statusId === null) { delete map[novelId] } else { map[novelId] = statusId }
  saveNovelStatuses(map)
}

// Inline badge that opens a picker on click
export default function NovelStatusBadge({ novelId, statusId, onChange }) {
  const [open, setOpen] = useState(false)
  const ref = useRef(null)
  const status = STATUS_OPTIONS.find(s => s.id === statusId)

  useEffect(() => {
    if (!open) return
    function onClickOutside(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false)
    }
    document.addEventListener('mousedown', onClickOutside)
    return () => document.removeEventListener('mousedown', onClickOutside)
  }, [open])

  function pick(id) {
    const next = id === statusId ? null : id   // click same = clear
    setNovelStatus(novelId, next)
    onChange?.(next)
    setOpen(false)
  }

  return (
    <div className={styles.wrap} ref={ref}>
      <button
        className={styles.badge}
        style={status
          ? { color: status.color, background: status.bg, borderColor: status.border }
          : {}}
        onClick={e => { e.stopPropagation(); setOpen(v => !v) }}
        title="Set novel status"
      >
        {status ? status.label : '+ Status'}
      </button>

      {open && (
        <div className={styles.picker} onClick={e => e.stopPropagation()}>
          {STATUS_OPTIONS.map(s => (
            <button
              key={s.id}
              className={`${styles.pickerBtn} ${statusId === s.id ? styles.pickerBtnActive : ''}`}
              style={{ '--sc': s.color, '--sb': s.bg, '--sbo': s.border }}
              onClick={() => pick(s.id)}
            >
              <span className={styles.pickerDot} style={{ background: s.color }} />
              {s.label}
              {statusId === s.id && <span className={styles.pickerCheck}>✓</span>}
            </button>
          ))}
          {statusId && (
            <button className={styles.pickerClear} onClick={() => pick(null)}>
              Clear status
            </button>
          )}
        </div>
      )}
    </div>
  )
}
