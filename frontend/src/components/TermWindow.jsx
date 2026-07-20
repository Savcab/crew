// TermWindow — the /?view=term surface: a full-window terminal viewer that
// listens to the graph window (termLink) and shows whichever agent was
// clicked there. It runs its own snapshot poll (worker records + dock sync),
// but renders no graph — put this window on the second monitor.
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { termWindowLink } from '../termLink.js'
import Dock from './Dock.jsx'
import Toast from './Toast.jsx'

export default function TermWindow() {
  const [docked, setDocked] = useState(null)
  const [toastMsg, setToastMsg] = useState(null)
  const dockRef = useRef(null)
  const snapRef = useRef({ agents: [] })
  const pendingRef = useRef(null)

  const toast = useCallback((text, err) => {
    setToastMsg({ text, err: !!err, at: Date.now() })
  }, [])
  const getWorkers = useCallback(() => snapRef.current.agents || [], [])
  const onDockChange = useCallback(() => {
    const w = dockRef.current && dockRef.current.dockedWorker()
    setDocked(w ? w.name : null)
  }, [])
  const onController = useCallback(c => { dockRef.current = c }, [])

  useEffect(() => {
    document.title = 'crew — terminal'
    let timer = null
    let disposed = false
    const openByName = name => {
      const w = (snapRef.current.agents || []).find(a => a.name === name)
      if (w && dockRef.current) { pendingRef.current = null; dockRef.current.openDock(w) }
      else pendingRef.current = name   // snapshot not in yet — open on next poll
    }
    async function poll() {
      try {
        const j = await api.graphSnapshot()
        if (j && j.ok) {
          snapRef.current = j
          if (pendingRef.current) openByName(pendingRef.current)
          if (dockRef.current) dockRef.current.syncDockedWorker()
        }
      } catch (e) { /* transient; next poll retries */ }
      if (!disposed) timer = setTimeout(poll, 1500)
    }
    poll()
    const bc = termWindowLink({ onOpen: openByName })
    return () => { disposed = true; if (timer) clearTimeout(timer); bc.close() }
  }, [])

  return (
    <div id="termwin">
      {!docked &&
        <div className="empty termwin-hint">
          terminal window — click an agent in the graph window to open it here
        </div>}
      <Dock getWorkers={getWorkers} onDockChange={onDockChange}
        onShowIdentity={() => {}} toast={toast} onController={onController} />
      <Toast msg={toastMsg} />
    </div>
  )
}
