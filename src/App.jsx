import { useState, useEffect, useCallback, useRef } from 'react'
import {
  BookOpen, RefreshCw, LogOut, Search, Play, Square,
  Zap, History, Shield, FlaskConical, FolderOpen, ArrowUpDown, AlertCircle, Globe
} from 'lucide-react'
import ConnectPanel     from './components/ConnectPanel.jsx'
import NovelCard        from './components/NovelCard.jsx'
import ScrapeModal      from './components/ScrapeModal.jsx'
import JobHistory       from './components/JobHistory.jsx'
import WatermarkEditor  from './components/WatermarkEditor.jsx'
import SiteTestModal    from './components/SiteTestModal.jsx'

import { getNovels, getCredentials, saveCredentials } from './lib/api.js'
import { useWatcher } from './hooks/useWatcher.js'
import CollectionsManager, { loadCollections, loadNovelCollections } from './components/CollectionsManager.jsx'
import { loadNovelStatuses, STATUS_OPTIONS } from './components/NovelStatus.jsx'
import styles from './App.module.css'

export default function App() {
  const [user,          setUser]          = useState(null)
  const [novels,        setNovels]        = useState([])
  const [loading,       setLoading]       = useState(false)
  const [search,        setSearch]        = useState('')
  const [filter,        setFilter]        = useState('all')   // all | watched | unwatched
  const [statusFilter,  setStatusFilter]  = useState('all')   // all | active | hiatus | completed | dropped | unset
  const [attentionFilter, setAttentionFilter] = useState(false) // show only errored/stale
  const [sourceFilter,  setSourceFilter]  = useState('all')   // all | <domain>
  const [scrapeTarget,  setScrapeTarget]  = useState(null)    // novel object for one-shot modal
  const [showHistory,   setShowHistory]   = useState(false)
  const [showWatermarks,setShowWatermarks]= useState(false)
  const [showTest,      setShowTest]      = useState(false)
  const [showCollections, setShowCollections] = useState(false)
  const [activeCollection, setActiveCollection] = useState('all')
  const [collections,   setCollections]   = useState(loadCollections)
  const [novelColMap,   setNovelColMap]   = useState(loadNovelCollections)
  const [novelStatuses, setNovelStatuses] = useState(loadNovelStatuses)
  const [sortBy,        setSortBy]        = useState('default') // default | title | lastChecked | chapters | status
  const [logs,          setLogsMap]       = useState({})      // { novelId: [{msg, type, ts}] }
  const toastRef = useRef(null)

  // ── Log helper (per novel) ────────────────────────────────────────────────
  const addLog = useCallback((novelId, msg, type = 'info') => {
    setLogsMap(prev => ({
      ...prev,
      [novelId]: [...(prev[novelId] || []).slice(-199), { msg, type, ts: Date.now() }]
    }))
  }, [])

  // ── Watcher hook ────────────────────────────────────────────
  const {
    watched, addWatch, removeWatch, updateWatch, runCheck,
    startWatch, stopWatch, running, isWatching, resetChapter, updateChainUrl,
    serverOnline, concurrencyLimit, setConcurrencyLimit, queueLength,
    staggerDelay, setStaggerDelay,
    batchScrapeAll, cancelBatch, batchProgress,
    // ── Job priority ───────────────────────────────────────────────────────
    jobQueueState, jobRunData, runNext, runNow,
  } = useWatcher(addLog)

  // ── Auto-login if creds saved ─────────────────────────────────────────────
  useEffect(() => {
    const { scraperKey, apiUrl } = getCredentials()
    if (scraperKey && apiUrl) {
      import('./lib/api.js').then(({ login }) =>
        login(apiUrl, scraperKey)
          .then(d => { setUser(d.user || { name: 'Scraper Admin', role: 'admin' }); fetchNovels() })
          .catch(() => {})
      )
    }
  }, [])

  async function fetchNovels() {
    setLoading(true)
    try {
      const data = await getNovels()
      setNovels(data.novels || [])
    } catch (e) {
      showToast('Failed to load novels: ' + e.message, 'err')
    } finally {
      setLoading(false)
    }
  }

  function handleConnected(u) {
    setUser(u)
    fetchNovels()
  }

  function handleLogout() {
    saveCredentials('', '')
    setUser(null)
    setNovels([])
  }

  // ── Toast ─────────────────────────────────────────────────────────────────
  function showToast(msg, type = 'ok') {
    if (toastRef.current) {
      toastRef.current.textContent = msg
      toastRef.current.className = `${styles.toast} ${styles['toast_' + type]} ${styles.toastVisible}`
      clearTimeout(toastRef._timer)
      toastRef._timer = setTimeout(() => {
        if (toastRef.current) toastRef.current.className = styles.toast
      }, 3500)
    }
  }

  // ── Filter & search ───────────────────────────────────────────────────────
  const watchedIds = new Set(watched.map(w => w.novelId))
  const filtered = novels.filter(n => {
    const matchSearch = !search || n.title.toLowerCase().includes(search.toLowerCase())
    const matchFilter = filter === 'all'
      || (filter === 'watched'   &&  watchedIds.has(n._id))
      || (filter === 'unwatched' && !watchedIds.has(n._id))
    const matchCollection = activeCollection === 'all'
      || (novelColMap[n._id] || []).includes(activeCollection)

    // ── Status filter ──────────────────────────────────────────────────────
    const novelStatus = novelStatuses[n._id] || null
    const matchStatus = statusFilter === 'all'
      || (statusFilter === 'unset' && !novelStatus)
      || novelStatus === statusFilter

    // ── Attention filter ───────────────────────────────────────────────────
    const novelLogs = logs[n._id] || []
    const lastLog   = novelLogs[novelLogs.length - 1]
    const isErrored = lastLog && lastLog.type === 'err'
    const wEntry    = watched.find(w => w.novelId === n._id)
    const isStale   = wEntry && isWatching(n._id) && wEntry.lastChecked
      && (Date.now() - wEntry.lastChecked) > 48 * 60 * 60 * 1000
    const matchAttention = !attentionFilter || isErrored || isStale

    // ── Source filter ──────────────────────────────────────────────────────
    const wEntryForSource = watched.find(w => w.novelId === n._id)
    const domain = wEntryForSource?.startUrl ? extractDomain(wEntryForSource.startUrl) : null
    const matchSource = sourceFilter === 'all' || domain === sourceFilter

    return matchSearch && matchFilter && matchCollection && matchStatus && matchAttention && matchSource
  })

  const watchingCount = watched.filter(w => isWatching(w.novelId)).length
  const runningCount  = Object.values(running).filter(Boolean).length

  // ── Sort ─────────────────────────────────────────────────────────────────
  const sorted = [...filtered].sort((a, b) => {
    if (sortBy === 'title')       return a.title.localeCompare(b.title)
    if (sortBy === 'chapters')    return (b.chapterCount || 0) - (a.chapterCount || 0)
    if (sortBy === 'lastChecked') {
      const wa = watched.find(w => w.novelId === a._id)
      const wb = watched.find(w => w.novelId === b._id)
      return (wb?.lastChecked || 0) - (wa?.lastChecked || 0)
    }
    if (sortBy === 'status') {
      const rank = n => isWatching(n._id) ? 0 : watchedIds.has(n._id) ? 1 : 2
      return rank(a) - rank(b)
    }
    return 0
  })

  // ── Health summary ────────────────────────────────────────────────────────
  const now = Date.now()
  const staleThreshold = 48 * 60 * 60 * 1000 // 48h
  const staleWatchers  = watched.filter(w => isWatching(w.novelId) && w.lastChecked && (now - w.lastChecked) > staleThreshold)
  const errorNovels    = watched.filter(w => {
    const novelLogs = logs[w.novelId] || []
    const last = novelLogs[novelLogs.length - 1]
    return last && last.type === 'err'
  })

  // ── Source site list (from watched novels' startUrls) ────────────────────
  const sourceSites = (() => {
    const domainCount = {}
    watched.forEach(w => {
      if (!w.startUrl) return
      const d = extractDomain(w.startUrl)
      if (d) domainCount[d] = (domainCount[d] || 0) + 1
    })
    return Object.entries(domainCount)
      .sort((a, b) => b[1] - a[1])
      .map(([domain, count]) => ({ domain, count }))
  })()

  // ── Not connected ────────────────────────────────────────────────────────
  if (!user) return <ConnectPanel onConnected={handleConnected} />

  return (
    <div className={styles.app}>
      {/* ── Sidebar ── */}
      <aside className={styles.sidebar}>
        <div className={styles.sidebarTop}>
          <div className={styles.logo}>
            <div className={styles.logoIcon}><BookOpen size={20}/></div>
            <div>
              <div className={styles.logoTitle}>Knight Novel</div>
              <div className={styles.logoSub}>Scraper Dashboard</div>
            </div>
          </div>

          <div className={styles.userCard}>
            <div className={styles.userAvatar}>{user.name?.[0]?.toUpperCase() || '?'}</div>
            <div className={styles.userInfo}>
              <div className={styles.userName}>{user.name}</div>
              <div className={styles.userRole}>{user.role}</div>
            </div>
          </div>

          <div className={styles.sideStats}>
            <div className={styles.sideStat}>
              <span className={styles.sideStatNum}>{novels.length}</span>
              <span className={styles.sideStatLabel}>Total Novels</span>
            </div>
            <div className={styles.sideStat}>
              <span className={styles.sideStatNum} style={{color:'var(--accent2)'}}>{watchedIds.size}</span>
              <span className={styles.sideStatLabel}>Watched</span>
            </div>
            <div className={styles.sideStat}>
              <span className={styles.sideStatNum} style={{color:watchingCount>0?'var(--success)':'var(--muted)'}}>{watchingCount}</span>
              <span className={styles.sideStatLabel}>Active</span>
            </div>
          </div>

          {/* Global controls */}
          <div className={styles.globalControls}>
            {watchingCount < watchedIds.size && watched.length > 0 && (
              <button className={styles.globalBtn} onClick={() => watched.forEach(w => !isWatching(w.novelId) && startWatch(w.novelId, w.intervalHours))}>
                <Play size={12}/> Start All Watchers
              </button>
            )}
            {watchingCount > 0 && (
              <button className={`${styles.globalBtn} ${styles.globalBtnStop}`} onClick={() => watched.forEach(w => stopWatch(w.novelId))}>
                <Square size={12}/> Stop All
              </button>
            )}
          </div>

          {/* ── Queue control panel ── */}
          <div className={styles.queuePanel}>
            <div className={styles.queuePanelTitle}>⚙ Queue Settings</div>

            {/* Concurrency slider */}
            <div className={styles.sliderRow}>
              <div className={styles.sliderHeader}>
                <span className={styles.sliderLabel}>Concurrent jobs</span>
                <span className={styles.sliderValue}>{concurrencyLimit}</span>
              </div>
              <input
                id="concurrency-slider"
                type="range" min="1" max="10" step="1"
                value={concurrencyLimit}
                className={styles.queueSlider}
                onChange={e => setConcurrencyLimit(Number(e.target.value))}
                title={`${concurrencyLimit} novel${concurrencyLimit !== 1 ? 's' : ''} scraping at once`}
              />
              <div className={styles.sliderHint}>1 = safe &nbsp;·&nbsp; 5 = fast</div>
            </div>

            {/* Stagger delay slider */}
            <div className={styles.sliderRow}>
              <div className={styles.sliderHeader}>
                <span className={styles.sliderLabel}>Stagger delay</span>
                <span className={styles.sliderValue}>{staggerDelay}s</span>
              </div>
              <input
                id="stagger-slider"
                type="range" min="0" max="30" step="1"
                value={staggerDelay}
                className={styles.queueSlider}
                onChange={e => setStaggerDelay(Number(e.target.value))}
                title={`Wait ${staggerDelay}s before starting each new job`}
              />
              <div className={styles.sliderHint}>0 = none &nbsp;·&nbsp; 30s = gentle</div>
            </div>

            {/* Batch scrape button + progress */}
            {batchProgress ? (
              <div className={styles.batchProgressWrap}>
                <div className={styles.batchProgressBar}>
                  <div
                    className={styles.batchProgressFill}
                    style={{ width: `${Math.round((batchProgress.done / batchProgress.total) * 100)}%` }}
                  />
                </div>
                <div className={styles.batchProgressStats}>
                  {batchProgress.done}/{batchProgress.total} done
                  {batchProgress.active > 0 && <span> · {batchProgress.active} active</span>}
                  {queueLength > 0 && <span> · {queueLength} queued</span>}
                </div>
                <button
                  id="cancel-batch-btn"
                  className={`${styles.batchBtn} ${styles.batchBtnStop}`}
                  onClick={cancelBatch}
                >
                  ✕ Cancel batch
                </button>
              </div>
            ) : (
              <button
                id="batch-scrape-btn"
                className={styles.batchBtn}
                disabled={watched.length === 0}
                title={watched.length === 0 ? 'Add some watched novels first' : `Run watch-check on all ${watched.length} watched novels`}
                onClick={() => {
                  // Build the novel list from watched entries, filtered to those in the current view
                  const targets = watched.map(w => ({ _id: w.novelId, novelId: w.novelId }))
                  batchScrapeAll(targets)
                }}
              >
                ⚡ Batch Scrape All Watched ({watched.length})
              </button>
            )}
          </div>

          {/* Running / queue status badges */}
          {runningCount > 0 && (
            <div className={styles.runningBadge}>
              <RefreshCw size={11} className={styles.spin}/> {runningCount} check{runningCount !== 1 ? 's' : ''} running
            </div>
          )}
          {queueLength > 0 && !batchProgress && (
            <div className={styles.queueBadge}>
              ⏳ {queueLength} check{queueLength !== 1 ? 's' : ''} queued
            </div>
          )}

          {/* Health summary */}
          {(staleWatchers.length > 0 || errorNovels.length > 0) && (
            <div className={styles.healthPanel}>
              <div className={styles.healthTitle}><AlertCircle size={11}/> Needs attention</div>
              {errorNovels.length > 0 && (
                <button
                  className={`${styles.healthItem} ${styles.healthItemBtn} ${attentionFilter ? styles.healthItemActive : ''}`}
                  style={{color:'var(--accent3)'}}
                  onClick={() => setAttentionFilter(v => !v)}
                  title={attentionFilter ? 'Click to clear filter' : 'Click to filter to these novels'}
                >
                  ✗ {errorNovels.length} novel{errorNovels.length !== 1 ? 's' : ''} errored
                  {attentionFilter ? ' · filtered ×' : ' · click to filter'}
                </button>
              )}
              {staleWatchers.length > 0 && (
                <button
                  className={`${styles.healthItem} ${styles.healthItemBtn} ${attentionFilter ? styles.healthItemActive : ''}`}
                  style={{color:'var(--warning)'}}
                  onClick={() => setAttentionFilter(v => !v)}
                  title={attentionFilter ? 'Click to clear filter' : 'Click to filter to these novels'}
                >
                  ⚠ {staleWatchers.length} stale watcher{staleWatchers.length !== 1 ? 's' : ''}
                  {attentionFilter ? ' · filtered ×' : ' · click to filter'}
                </button>
              )}
            </div>
          )}

          <div className={serverOnline ? styles.serverBadgeOn : styles.serverBadgeOff}>
            <span className={styles.serverDot}/>
            {serverOnline ? 'Python server connected' : 'Python server offline'}
          </div>


        </div>

        <div className={styles.sidebarBottom}>
          <button className={styles.sideToolBtn} onClick={() => setShowTest(true)}>
            <FlaskConical size={13}/> Test a Site
          </button>
          <button className={styles.sideToolBtn} onClick={() => { setShowCollections(true); setCollections(loadCollections()); setNovelColMap(loadNovelCollections()) }}>
            <FolderOpen size={13}/> Collections
          </button>
          <button className={styles.sideToolBtn} onClick={() => setShowHistory(true)}>
            <History size={13}/> Job History
          </button>
          <button className={styles.sideToolBtn} onClick={() => setShowWatermarks(true)}>
            <Shield size={13}/> Watermark Phrases
          </button>
          <button className={styles.logoutBtn} onClick={handleLogout}>
            <LogOut size={13}/> Disconnect
          </button>
        </div>
      </aside>

      {/* ── Main ── */}
      <main className={styles.main}>
        {/* Toolbar */}
        <div className={styles.toolbar}>
          <div className={styles.searchWrap}>
            <Search size={14} className={styles.searchIcon}/>
            <input
              className={styles.searchInput}
              placeholder="Search novels…"
              value={search}
              onChange={e => setSearch(e.target.value)}
            />
          </div>

          <div className={styles.filterTabs}>
            {['all','watched','unwatched'].map(f => (
              <button key={f} className={`${styles.filterTab} ${filter === f ? styles.filterTabActive : ''}`}
                onClick={() => setFilter(f)}>
                {f}
              </button>
            ))}
          </div>

          <button className={styles.refreshBtn} onClick={fetchNovels} disabled={loading}>
            <RefreshCw size={13} className={loading ? styles.spin : ''}/>
            {loading ? 'Loading…' : 'Refresh'}
          </button>

          <button className={styles.refreshBtn} onClick={() => setScrapeTarget({ _id: null, title: 'New scrape job' })}>
            <Play size={13}/> New Knight Novel Job
          </button>

          <div className={styles.sortWrap}>
            <ArrowUpDown size={12} className={styles.sortIcon}/>
            <select className={styles.sortSelect} value={sortBy} onChange={e => setSortBy(e.target.value)}>
              <option value="default">Default</option>
              <option value="title">Title A–Z</option>
              <option value="status">Status</option>
              <option value="lastChecked">Last Checked</option>
              <option value="chapters">Most Chapters</option>
            </select>
          </div>
        </div>

        {/* Collection filter bar */}
        {collections.length > 0 && (
          <div className={styles.collectionBar}>
            <button
              className={`${styles.colFilterBtn} ${activeCollection === 'all' ? styles.colFilterBtnActive : ''}`}
              onClick={() => setActiveCollection('all')}
            >
              All
            </button>
            {collections.map(col => {
              const colorHex = (['yellow','teal','purple','red','green','blue','orange','pink'].includes(col.color))
                ? { yellow:'#e8c547', teal:'#4ecdc4', purple:'#a78bfa', red:'#ff6b6b', green:'#4ade80', blue:'#60a5fa', orange:'#fb923c', pink:'#f472b6' }[col.color]
                : '#e8c547'
              const isActive = activeCollection === col.id
              return (
                <button
                  key={col.id}
                  className={`${styles.colFilterBtn} ${isActive ? styles.colFilterBtnActive : ''}`}
                  style={isActive ? { borderColor: colorHex, color: colorHex, background: colorHex + '18' } : {}}
                  onClick={() => setActiveCollection(col.id)}
                >
                  <span className={styles.colFilterDot} style={{ background: colorHex }} />
                  {col.name}
                </button>
              )
            })}
          </div>
        )}

        {/* ── Status filter bar ── */}
        <div className={styles.filterBarRow}>
          <span className={styles.filterBarLabel}>Status</span>
          <div className={styles.statusFilterBar}>
            <button
              className={`${styles.statusFilterBtn} ${statusFilter === 'all' ? styles.statusFilterBtnActive : ''}`}
              onClick={() => setStatusFilter('all')}
            >
              All
            </button>
            {STATUS_OPTIONS.map(s => (
              <button
                key={s.id}
                className={`${styles.statusFilterBtn} ${statusFilter === s.id ? styles.statusFilterBtnActive : ''}`}
                style={statusFilter === s.id ? { borderColor: s.border, color: s.color, background: s.bg } : {}}
                onClick={() => setStatusFilter(v => v === s.id ? 'all' : s.id)}
              >
                <span className={styles.statusDot} style={{ background: s.color }} />
                {s.label}
              </button>
            ))}
            <button
              className={`${styles.statusFilterBtn} ${statusFilter === 'unset' ? styles.statusFilterBtnActive : ''}`}
              onClick={() => setStatusFilter(v => v === 'unset' ? 'all' : 'unset')}
            >
              Unset
            </button>
          </div>
        </div>

        {/* ── Source site filter bar ── */}
        {sourceSites.length > 1 && (
          <div className={styles.filterBarRow}>
            <span className={styles.filterBarLabel}><Globe size={11}/> Source</span>
            <div className={styles.sourceFilterBar}>
              <button
                className={`${styles.sourceFilterBtn} ${sourceFilter === 'all' ? styles.sourceFilterBtnActive : ''}`}
                onClick={() => setSourceFilter('all')}
              >
                All sites
              </button>
              {sourceSites.map(({ domain, count }) => (
                <button
                  key={domain}
                  className={`${styles.sourceFilterBtn} ${sourceFilter === domain ? styles.sourceFilterBtnActive : ''}`}
                  onClick={() => setSourceFilter(v => v === domain ? 'all' : domain)}
                  title={domain}
                >
                  {domain}
                  <span className={styles.sourceCount}>{count}</span>
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Novel grid */}
        {loading && novels.length === 0 ? (
          <div className={styles.emptyState}>
            <RefreshCw size={32} className={styles.spin} style={{color:'var(--muted)'}}/>
            <p>Loading novels…</p>
          </div>
        ) : filtered.length === 0 ? (
          <div className={styles.emptyState}>
            <BookOpen size={36} style={{color:'var(--muted)'}}/>
            <p>{search ? `No novels matching "${search}"` : 'No novels found'}</p>
          </div>
        ) : (
          <div className={styles.novelGrid}>
            {sorted.map(novel => {
              const watchEntry = watched.find(w => w.novelId === novel._id)
              return (
                <div key={novel._id} className={styles.novelRow}>
                  <NovelCard
                    novel={novel}
                    watchEntry={watchEntry || null}
                    isWatching={isWatching(novel._id)}
                    isRunning={!!running[novel._id]}
                    logs={logs[novel._id] || []}
                    onAddWatch={addWatch}
                    onRemoveWatch={removeWatch}
                    onStartWatch={startWatch}
                    onStopWatch={stopWatch}
                    onRunOnce={runCheck}
                    onResetChapter={resetChapter}
                    onUpdateChainUrl={updateChainUrl}
                    novelCollections={collections.filter(c => (novelColMap[novel._id] || []).includes(c.id))}
                    onStatusChange={(id, statusId) => setNovelStatuses(loadNovelStatuses())}
                    isQueued={!!jobQueueState[novel._id]}
                    queuePosition={jobQueueState[novel._id] || null}
                    runNext={runNext}
                    runNow={runNow}
                    jobRunData={jobRunData}
                  />
                  {/* Scrape button outside card */}
                  <button className={styles.scrapeBtn} title="One-shot scrape" onClick={() => setScrapeTarget(novel)}>
                    <Zap size={13}/> Scrape
                  </button>
                </div>
              )
            })}
          </div>
        )}
      </main>

      {/* ── Collections manager ── */}
      {showCollections && (
        <CollectionsManager
          novels={novels}
          onClose={() => { setShowCollections(false); setCollections(loadCollections()); setNovelColMap(loadNovelCollections()) }}
        />
      )}

      {/* ── One-shot scrape modal ── */}
      {scrapeTarget && (
        <ScrapeModal novel={scrapeTarget} onClose={() => { setScrapeTarget(null); fetchNovels() }}/>
      )}

      {/* ── Site test modal ── */}
      {showTest && <SiteTestModal onClose={() => setShowTest(false)} />}



      {/* ── Job history panel ── */}
      {showHistory && <JobHistory onClose={() => setShowHistory(false)} />}

      {/* ── Watermark editor ── */}
      {showWatermarks && <WatermarkEditor onClose={() => setShowWatermarks(false)} />}

      {/* ── Toast ── */}
      <div className={styles.toast} ref={toastRef}/>
    </div>
  )
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function extractDomain(url) {
  try {
    const { hostname } = new URL(url)
    // Strip www. prefix for cleaner display
    return hostname.replace(/^www\./, '')
  } catch {
    return null
  }
}
