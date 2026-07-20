// App.jsx — the one component that knows about all the parts (the role main.js
// played): owns the snapshot poll, builds the dock/graph/modal wiring, and
// hosts the toast. The graph canvas and the dock terminal remain imperative
// islands (graphEngine.js / dockCore.js + term.js) mounted inside React-owned
// skeletons — React never reconciles their children.
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { renderGraph, highlightDockedNode } from './graphEngine.js'
import { installKeys } from './keys.js'
import Header from './components/Header.jsx'
import GraphView from './components/GraphView.jsx'
import Dock from './components/Dock.jsx'
import Toast from './components/Toast.jsx'
import CreateAgentModal from './components/modals/CreateAgentModal.jsx'
import ConnectEdgeModal from './components/modals/ConnectEdgeModal.jsx'
import EditEdgeModal from './components/modals/EditEdgeModal.jsx'
import IdentityModal from './components/modals/IdentityModal.jsx'
import PendingModal from './components/modals/PendingModal.jsx'

function esc(s) {
  return (s || '').replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]))
}

export default function App() {
  const [snap, setSnap] = useState({ agents: [], edges: [] })
  const [toastMsg, setToastMsg] = useState(null)
  // One modal at a time: {kind, ...payload}. `key` remounts a fresh instance per
  // open so an in-flight submit from a previous form can never unlock a new one
  // (the role modal.js's epoch counter played).
  const [modal, setModal] = useState(null)
  const modalKeyRef = useRef(0)

  const snapRef = useRef(snap)
  snapRef.current = snap
  const modalRef = useRef(modal)
  modalRef.current = modal
  const dockRef = useRef(null)

  const toast = useCallback((text, err) => {
    setToastMsg({ text, err: !!err, at: Date.now() })
  }, [])

  const openModal = useCallback(kindAndProps => {
    modalKeyRef.current += 1
    setModal(kindAndProps)
  }, [])
  const closeModal = useCallback(() => setModal(null), [])

  // ---- graph poll (ported from main.js: coalesced, sig-suppressed, forcible) --
  const pollRef = useRef({
    rate: 1500, timer: null, inFlight: false,
    loadPromise: null, reloadQueued: false, lastSig: '',
  })

  const setGraphUnavailable = useCallback(message => {
    const g = document.getElementById('cgraph')
    if (!g) return
    // The error view replaces the rendered canvas. Invalidate the data signature
    // too: after the backend recovers, its first good snapshot may be identical
    // to the last one rendered — a cached signature would strand this view.
    pollRef.current.lastSig = ''
    g.setAttribute('aria-busy', 'false')
    g.innerHTML = '<div class="empty" id="graphStatus" role="status" '
      + 'aria-live="polite">backend unavailable: ' + esc(message) + '</div>'
  }, [])

  const loadGraph = useCallback(async force => {
    const p = pollRef.current
    if (p.loadPromise) {
      if (force) p.reloadQueued = true
      return p.loadPromise
    }
    p.loadPromise = (async () => {
      let j
      try { j = await api.graphSnapshot() }
      catch (e) { setGraphUnavailable((e && e.message) || 'snapshot request failed'); return }
      if (!j || !j.ok) {
        setGraphUnavailable((j && j.error) || 'is MorphDB + the crew server running?')
        return
      }
      const g = document.getElementById('cgraph')
      if (g) g.setAttribute('aria-busy', 'false')
      const sig = JSON.stringify({ a: j.agents, e: j.edges })
      const changed = force || sig !== p.lastSig
      if (changed) p.lastSig = sig
      snapRef.current = j
      setSnap(j)
      if (changed) renderCrewRef.current(j)
      if (dockRef.current) dockRef.current.syncDockedWorker()
    })()
    try { return await p.loadPromise }
    finally {
      p.loadPromise = null
      if (p.reloadQueued) {
        p.reloadQueued = false
        await loadGraph(true)
      }
    }
  }, [setGraphUnavailable])
  const loadGraphRef = useRef(loadGraph)
  loadGraphRef.current = loadGraph

  const refresh = useCallback(() => loadGraphRef.current(true), [])

  // ---- graph render (engine call; handlers close over refs, stay stable) ----
  const graphHandlers = useRef({
    onDockAgent: a => dockRef.current && dockRef.current.openDock(a),
    onConnect: (fromName, toName) =>
      openModalRef.current({ kind: 'connect', source: fromName, target: toName }),
    onEditEdge: e => openModalRef.current({ kind: 'editEdge', edge: e }),
    onCreateAgent: () => openModalRef.current({ kind: 'createAgent' }),
  })
  const openModalRef = useRef(openModal)
  openModalRef.current = openModal

  const renderCrewRef = useRef(null)
  renderCrewRef.current = (s) => {
    const docked = dockRef.current && dockRef.current.dockedWorker()
    renderGraph(s || snapRef.current, graphHandlers.current,
      { dockedName: (docked || {}).name })
  }

  // ---- poll loop ----
  useEffect(() => {
    const p = pollRef.current
    let disposed = false
    async function runPoll(force) {
      p.inFlight = true
      try { await loadGraphRef.current(!!force) }
      finally {
        p.inFlight = false
        if (!disposed) {
          p.timer = p.rate > 0 ? setTimeout(runPoll, p.rate) : null
        }
      }
    }
    p.runPoll = runPoll
    runPoll(true)
    const onResize = () => renderCrewRef.current()
    window.addEventListener('resize', onResize)
    return () => {
      disposed = true
      if (p.timer) clearTimeout(p.timer)
      window.removeEventListener('resize', onResize)
    }
  }, [])

  const onRateChange = useCallback(rate => {
    const p = pollRef.current
    p.rate = rate
    if (p.timer) { clearTimeout(p.timer); p.timer = null }
    // If a request is already running, its finally block observes the new rate
    // and schedules exactly one successor after that request completes.
    if (p.rate > 0 && !p.inFlight) p.timer = setTimeout(p.runPoll, p.rate)
  }, [])

  // ---- dock wiring (stable callbacks reading refs) ----
  const getWorkers = useCallback(() => snapRef.current.agents || [], [])
  const onDockChange = useCallback(() => {
    const docked = dockRef.current && dockRef.current.dockedWorker()
    highlightDockedNode((docked || {}).name)
  }, [])
  const onShowIdentity = useCallback(w => {
    openModalRef.current({ kind: 'identity', worker: w })
  }, [])
  const onDockController = useCallback(controller => { dockRef.current = controller }, [])

  // ---- pending tray ----
  const openPending = useCallback(async () => {
    let j
    try { j = await api.pendingList() }
    catch (e) { toast('request failed', true); return }
    if (!j || !j.ok) { toast((j && j.error) || 'failed', true); return }
    openModalRef.current({ kind: 'pending', rows: j.pending || [] })
  }, [toast])

  // ---- key dispatcher (once; ctx reads live refs) ----
  useEffect(() => installKeys({
    view: () => 'crew',
    paneFocused: () => !!(dockRef.current && dockRef.current.paneFocused()),
    modalOpen: () => !!modalRef.current,
    closeModal: () => setModal(null),
    dockOpen: () => !!(dockRef.current && dockRef.current.dockOpen()),
    closeDock: () => dockRef.current && dockRef.current.closeDock(),
    detachDock: () => dockRef.current && dockRef.current.detach(),
  }), [])

  const modalProps = { api, toast, refresh, onClose: closeModal }
  const mkey = modalKeyRef.current
  return (
    <>
      <Header
        agents={snap.agents || []}
        pendingCount={snap.pending_count || 0}
        onOpenPending={openPending}
        onRateChange={onRateChange}
      />
      <div id="main">
        <div id="crew" className="view on">
          <div id="crew-top">
            <GraphView onCreateAgent={() => openModal({ kind: 'createAgent' })} />
          </div>
          <Dock
            getWorkers={getWorkers}
            onDockChange={onDockChange}
            onShowIdentity={onShowIdentity}
            toast={toast}
            onController={onDockController}
          />
        </div>
      </div>
      {modal && modal.kind === 'createAgent' &&
        <CreateAgentModal key={mkey} {...modalProps} />}
      {modal && modal.kind === 'connect' &&
        <ConnectEdgeModal key={mkey} {...modalProps}
          source={modal.source} target={modal.target} />}
      {modal && modal.kind === 'editEdge' &&
        <EditEdgeModal key={mkey} {...modalProps} edge={modal.edge} />}
      {modal && modal.kind === 'identity' &&
        <IdentityModal key={mkey} {...modalProps} worker={modal.worker}
          edges={snap.edges || []} />}
      {modal && modal.kind === 'pending' &&
        <PendingModal key={mkey} {...modalProps} rows={modal.rows} />}
      <Toast msg={toastMsg} />
    </>
  )
}
