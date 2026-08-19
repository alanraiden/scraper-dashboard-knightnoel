import { useState, useEffect } from 'react'
import { checkServerHealth } from '../lib/localServer.js'
import styles from './ServerStatus.module.css'

export default function ServerStatus({ onStatusChange }) {
  const [status, setStatus] = useState('unknown') // unknown | online | offline

  async function check() {
    const ok = await checkServerHealth()
    const s  = ok ? 'online' : 'offline'
    setStatus(s)
    onStatusChange?.(s)
  }

  useEffect(() => {
    check()
    const id = setInterval(check, 15000) // re-check every 15s
    return () => clearInterval(id)
  }, [])

  return (
    <div className={`${styles.badge} ${styles[status]}`} onClick={check} title="Click to recheck">
      <span className={styles.dot}/>
      {status === 'online'  && 'Python server online'}
      {status === 'offline' && 'Python server offline'}
      {status === 'unknown' && 'Checking server…'}
    </div>
  )
}
