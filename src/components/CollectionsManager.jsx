import { useState } from 'react'
import { X, Plus, Trash2, FolderOpen, Edit2, Check } from 'lucide-react'
import styles from './CollectionsManager.module.css'

export const COLLECTIONS_KEY = 'ns_collections'
export const NOVEL_COLLECTIONS_KEY = 'ns_novel_collections'

export function loadCollections() {
  try { return JSON.parse(localStorage.getItem(COLLECTIONS_KEY) || '[]') } catch { return [] }
}
export function saveCollections(list) {
  localStorage.setItem(COLLECTIONS_KEY, JSON.stringify(list))
}
export function loadNovelCollections() {
  try { return JSON.parse(localStorage.getItem(NOVEL_COLLECTIONS_KEY) || '{}') } catch { return {} }
}
export function saveNovelCollections(map) {
  localStorage.setItem(NOVEL_COLLECTIONS_KEY, JSON.stringify(map))
}
export function getNovelCollectionIds(novelId) {
  const map = loadNovelCollections()
  return map[novelId] || []
}

const COLORS = [
  { id: 'yellow',  label: 'Gold',    hex: '#e8c547' },
  { id: 'teal',    label: 'Teal',    hex: '#4ecdc4' },
  { id: 'purple',  label: 'Purple',  hex: '#a78bfa' },
  { id: 'red',     label: 'Red',     hex: '#ff6b6b' },
  { id: 'green',   label: 'Green',   hex: '#4ade80' },
  { id: 'blue',    label: 'Blue',    hex: '#60a5fa' },
  { id: 'orange',  label: 'Orange',  hex: '#fb923c' },
  { id: 'pink',    label: 'Pink',    hex: '#f472b6' },
  { id: 'cyan',     label: 'Cyan',     hex: '#22d3ee' },
  { id: 'lime',     label: 'Lime',     hex: '#a3e635' },
  { id: 'indigo',   label: 'Indigo',   hex: '#6366f1' },
  { id: 'rose',     label: 'Rose',     hex: '#fb7185' },
  { id: 'emerald',  label: 'Emerald',  hex: '#bed300' },
  { id: 'sky',      label: 'Sky',      hex: '#38bdf8' },
  { id: 'amber',    label: 'Amber',    hex: '#fbbf24' },
  { id: 'violet',   label: 'Violet',   hex: '#321477' },
  { id: 'fuchsia',  label: 'Fuchsia',  hex: '#d946ef' },
  { id: 'slate',    label: 'Slate',    hex: '#64748b' },
  { id: 'gray',     label: 'Gray',     hex: '#68d100' },
  { id: 'zinc',     label: 'Zinc',     hex: '#71717a' },
  { id: 'stone',    label: 'Stone',    hex: '#a4004d' },
  { id: 'brown',    label: 'Brown',    hex: '#92400e' },
  { id: 'navy',     label: 'Navy',     hex: '#1e3a8a' },
]

export default function CollectionsManager({ onClose, novels }) {
  const [collections, setCollections] = useState(loadCollections)
  const [novelCollections, setNovelCollections] = useState(loadNovelCollections)
  const [newName, setNewName] = useState('')
  const [newColor, setNewColor] = useState('yellow')
  const [editingId, setEditingId] = useState(null)
  const [editName, setEditName] = useState('')
  const [activeTab, setActiveTab] = useState('manage') // manage | assign

  function addCollection() {
    if (!newName.trim()) return
    const col = { id: Date.now().toString(), name: newName.trim(), color: newColor }
    const updated = [...collections, col]
    setCollections(updated)
    saveCollections(updated)
    setNewName('')
  }

  function deleteCollection(id) {
    const updated = collections.filter(c => c.id !== id)
    setCollections(updated)
    saveCollections(updated)
    // Remove this collection from all novels
    const map = { ...novelCollections }
    Object.keys(map).forEach(novelId => {
      map[novelId] = (map[novelId] || []).filter(cid => cid !== id)
    })
    setNovelCollections(map)
    saveNovelCollections(map)
  }

  function startEdit(col) {
    setEditingId(col.id)
    setEditName(col.name)
  }

  function saveEdit(id) {
    if (!editName.trim()) return
    const updated = collections.map(c => c.id === id ? { ...c, name: editName.trim() } : c)
    setCollections(updated)
    saveCollections(updated)
    setEditingId(null)
  }

  function toggleNovelCollection(novelId, colId) {
    const map = { ...novelCollections }
    const current = map[novelId] || []
    map[novelId] = current.includes(colId)
      ? current.filter(id => id !== colId)
      : [...current, colId]
    setNovelCollections(map)
    saveNovelCollections(map)
  }

  function getCollectionCount(colId) {
    return Object.values(novelCollections).filter(ids => ids.includes(colId)).length
  }

  return (
    <div className={styles.overlay} onClick={e => e.target === e.currentTarget && onClose()}>
      <div className={styles.modal}>
        <div className={styles.header}>
          <div className={styles.headerLeft}>
            <FolderOpen size={16} style={{ color: 'var(--accent)' }} />
            <span className={styles.title}>Collections</span>
          </div>
          <button className={styles.closeBtn} onClick={onClose}><X size={16} /></button>
        </div>

        <div className={styles.tabs}>
          <button className={`${styles.tab} ${activeTab === 'manage' ? styles.tabActive : ''}`} onClick={() => setActiveTab('manage')}>
            Manage Collections
          </button>
          <button className={`${styles.tab} ${activeTab === 'assign' ? styles.tabActive : ''}`} onClick={() => setActiveTab('assign')}>
            Assign Novels
          </button>
        </div>

        {activeTab === 'manage' && (
          <div className={styles.body}>
            {/* Create new */}
            <div className={styles.createRow}>
              <input
                className={styles.nameInput}
                placeholder="New collection name…"
                value={newName}
                onChange={e => setNewName(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && addCollection()}
              />
              <div className={styles.colorPicker}>
                {COLORS.map(c => (
                  <button
                    key={c.id}
                    className={`${styles.colorDot} ${newColor === c.id ? styles.colorDotActive : ''}`}
                    style={{ background: c.hex }}
                    title={c.label}
                    onClick={() => setNewColor(c.id)}
                  />
                ))}
              </div>
              <button className={styles.addBtn} onClick={addCollection} disabled={!newName.trim()}>
                <Plus size={14} /> Add
              </button>
            </div>

            {/* Collection list */}
            {collections.length === 0 ? (
              <div className={styles.empty}>No collections yet. Create one above.</div>
            ) : (
              <div className={styles.list}>
                {collections.map(col => {
                  const colorHex = COLORS.find(c => c.id === col.color)?.hex || '#e8c547'
                  const count = getCollectionCount(col.id)
                  return (
                    <div key={col.id} className={styles.colRow}>
                      <div className={styles.colDot} style={{ background: colorHex }} />
                      {editingId === col.id ? (
                        <input
                          className={styles.editInput}
                          value={editName}
                          onChange={e => setEditName(e.target.value)}
                          onKeyDown={e => { if (e.key === 'Enter') saveEdit(col.id); if (e.key === 'Escape') setEditingId(null) }}
                          autoFocus
                        />
                      ) : (
                        <span className={styles.colName}>{col.name}</span>
                      )}
                      <span className={styles.colCount}>{count} novel{count !== 1 ? 's' : ''}</span>
                      <div className={styles.colActions}>
                        {editingId === col.id ? (
                          <button className={styles.iconBtn} onClick={() => saveEdit(col.id)}><Check size={13} /></button>
                        ) : (
                          <button className={styles.iconBtn} onClick={() => startEdit(col)}><Edit2 size={13} /></button>
                        )}
                        <button className={`${styles.iconBtn} ${styles.iconBtnDanger}`} onClick={() => deleteCollection(col.id)}>
                          <Trash2 size={13} />
                        </button>
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        )}

        {activeTab === 'assign' && (
          <div className={styles.body}>
            {collections.length === 0 ? (
              <div className={styles.empty}>Create some collections first in the "Manage" tab.</div>
            ) : novels.length === 0 ? (
              <div className={styles.empty}>No novels loaded.</div>
            ) : (
              <div className={styles.assignList}>
                {novels.map(novel => {
                  const assigned = novelCollections[novel._id] || []
                  return (
                    <div key={novel._id} className={styles.assignRow}>
                      <span className={styles.assignTitle}>{novel.title}</span>
                      <div className={styles.assignTags}>
                        {collections.map(col => {
                          const colorHex = COLORS.find(c => c.id === col.color)?.hex || '#e8c547'
                          const isOn = assigned.includes(col.id)
                          return (
                            <button
                              key={col.id}
                              className={`${styles.assignTag} ${isOn ? styles.assignTagOn : ''}`}
                              style={isOn ? { borderColor: colorHex, color: colorHex, background: colorHex + '18' } : {}}
                              onClick={() => toggleNovelCollection(novel._id, col.id)}
                            >
                              {col.name}
                            </button>
                          )
                        })}
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
