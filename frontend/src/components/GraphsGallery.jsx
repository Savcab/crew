// GraphsGallery — /?view=graphs, styled after Figma's home: flat chrome (this
// page is clearly NOT a canvas), a left sidebar with search, and a grid of
// file cards whose top pane is a THUMBNAIL of the actual graph shape (mini
// force layout over the project's real nodes/edges). Opening a card jumps to
// the dashboard process that owns that graph; agents keep running everywhere
// (tmux, not browser windows). "New graph" seeds a launched chat-to-build
// foreman.
import { useCallback, useEffect, useMemo, useState } from 'react'
import Button from '@mui/material/Button'
import TextField from '@mui/material/TextField'
import Dialog from '@mui/material/Dialog'
import DialogTitle from '@mui/material/DialogTitle'
import DialogContent from '@mui/material/DialogContent'
import { api } from '../api.js'
import Toast from './Toast.jsx'

// Deterministic mini layout for the card thumbnail: the engine's spiral seed
// plus a few repulsion/spring passes, normalized into the SVG viewBox. No
// randomness — the same graph always draws the same shape.
function layoutPreview(count, links) {
  const pos = Array.from({ length: count }, (_, i) => ({
    x: Math.cos(i * 2.4) * (30 + i * 9),
    y: Math.sin(i * 2.4) * (24 + i * 7),
  }))
  for (let iter = 0; iter < 60; iter++) {
    for (let i = 0; i < count; i++) {
      for (let j = i + 1; j < count; j++) {
        let dx = pos[i].x - pos[j].x, dy = pos[i].y - pos[j].y
        let d2 = dx * dx + dy * dy; if (d2 < 1) d2 = 1
        const d = Math.sqrt(d2), f = 260 / d2
        const ux = dx / d, uy = dy / d
        pos[i].x += ux * f; pos[i].y += uy * f
        pos[j].x -= ux * f; pos[j].y -= uy * f
      }
    }
    for (const [a, b] of links) {
      if (!pos[a] || !pos[b]) continue
      const dx = pos[b].x - pos[a].x, dy = pos[b].y - pos[a].y
      const d = Math.hypot(dx, dy) || 1
      const f = (d - 46) * 0.04, ux = dx / d, uy = dy / d
      pos[a].x += ux * f; pos[a].y += uy * f
      pos[b].x -= ux * f; pos[b].y -= uy * f
    }
  }
  return pos
}

function MiniGraph({ preview }) {
  const W = 300, H = 150, PAD = 22
  const shape = useMemo(() => {
    if (!preview || !preview.nodes || !preview.nodes.length) return null
    const pos = layoutPreview(preview.nodes.length, preview.links || [])
    let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity
    for (const p of pos) {
      minx = Math.min(minx, p.x); maxx = Math.max(maxx, p.x)
      miny = Math.min(miny, p.y); maxy = Math.max(maxy, p.y)
    }
    const sw = Math.max(1, maxx - minx), sh = Math.max(1, maxy - miny)
    const scale = Math.min((W - PAD * 2) / sw, (H - PAD * 2) / sh, 3)
    return pos.map(p => ({
      x: W / 2 + (p.x - (minx + maxx) / 2) * scale,
      y: H / 2 + (p.y - (miny + maxy) / 2) * scale,
    }))
  }, [preview])
  return (
    <svg className="gc-thumb" viewBox={`0 0 ${W} ${H}`} aria-hidden="true">
      <defs>
        <pattern id="gdots" width="14" height="14" patternUnits="userSpaceOnUse">
          <circle cx="1" cy="1" r="1" fill="#1d232b" />
        </pattern>
      </defs>
      <rect width={W} height={H} fill="#0d1117" />
      <rect width={W} height={H} fill="url(#gdots)" />
      {shape && (preview.links || []).map(([a, b], i) => (
        shape[a] && shape[b] &&
        <line key={i} x1={shape[a].x} y1={shape[a].y}
          x2={shape[b].x} y2={shape[b].y} stroke="#4d6b94" strokeWidth="1.4" />
      ))}
      {shape && shape.map((p, i) => (
        <g key={i}>
          <rect x={p.x - 11} y={p.y - 6} width="22" height="12" rx="3.5"
            fill="#161d27" stroke="#2c3a4d" strokeWidth="1" />
          <circle cx={p.x - 6} cy={p.y} r="1.8" fill="#58a6ff" />
        </g>
      ))}
      {!shape &&
        <text x={W / 2} y={H / 2 + 4} textAnchor="middle" fill="#5b636d"
          fontSize="11" fontFamily="inherit">empty graph</text>}
    </svg>
  )
}

function ago(ts) {
  if (!ts) return null
  const s = Math.max(0, Date.now() / 1000 - ts)
  if (s < 3600) return 'created recently'
  if (s < 86400) return `created ${Math.floor(s / 3600)}h ago`
  return `created ${Math.floor(s / 86400)}d ago`
}

export default function GraphsGallery() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [query, setQuery] = useState('')
  const [creating, setCreating] = useState(false)
  const [busy, setBusy] = useState(false)
  const [toastMsg, setToastMsg] = useState(null)
  const toast = useCallback((text, err) => {
    setToastMsg({ text, err: !!err, at: Date.now() })
  }, [])

  const load = useCallback(async () => {
    try {
      const j = await api.projects()
      if (j && j.ok) { setData(j); setError(null) }
      else setError((j && j.error) || 'failed to list graphs')
    } catch (e) { setError(e.message || 'backend unavailable') }
  }, [])
  useEffect(() => {
    document.title = 'crew — graphs'
    load()
    const t = setInterval(load, 5000)
    return () => clearInterval(t)
  }, [load])

  const openGraph = useCallback(async name => {
    if (busy) return
    setBusy(true)
    try {
      const j = await api.projectOpen(name)
      if (j && j.ok && j.url) {
        window.location.href =
          j.port === parseInt(window.location.port, 10) ? '/' : j.url
        return
      }
      toast((j && j.error) || 'could not open graph', true)
    } catch (e) { toast('could not open graph', true) }
    finally { setBusy(false) }
  }, [busy, toast])

  const createGraph = useCallback(async () => {
    const title = (document.getElementById('g-name').value || '').trim()
    const description = (document.getElementById('g-desc').value || '').trim()
    if (!title) { toast('name required', true); return }
    setBusy(true)
    try {
      const j = await api.projectCreate({ title, description })
      if (!j || !j.ok) { toast((j && j.error) || 'create failed', true); return }
      if (j.warning) toast(j.warning, true)
      else toast(`created '${j.title || title}' — foreman is booting; opening…`)
      setCreating(false)
      await openGraph(j.project)
    } catch (e) { toast('create failed', true) }
    finally { setBusy(false) }
  }, [openGraph, toast])

  const projects = (data && data.projects) || []
  const q = query.trim().toLowerCase()
  const visible = q
    ? projects.filter(p => p.name.toLowerCase().includes(q)
      || (p.title || '').toLowerCase().includes(q)
      || (p.description || '').toLowerCase().includes(q))
    : projects

  return (
    <div id="graphs">
      <aside id="graphs-side">
        <div className="gs-brand">crew</div>
        <input id="graphsSearch" className="gs-search" type="search"
          placeholder="Search graphs…" value={query}
          onChange={e => setQuery(e.target.value)} />
        <nav className="gs-nav">
          <div className="gs-item active">
            <span className="gs-ico">▦</span> All graphs
            <span className="gs-count">{projects.length || ''}</span>
          </div>
        </nav>
        <div className="gs-foot">
          Each graph is an independent crew.<br />
          Agents keep running when you leave — they live in tmux, not in this
          window.
        </div>
      </aside>
      <main id="graphs-main">
        <div id="graphs-bar">
          <span className="gb-title">All graphs</span>
          <Button id="newGraphBtn" variant="contained" disableElevation
            sx={{ ml: 'auto' }} onClick={() => setCreating(true)}>+ New graph</Button>
        </div>
        <div id="graphs-grid">
          {error && <div className="empty">backend unavailable: {error}</div>}
          {!error && data && !visible.length &&
            <div className="empty">no graphs match “{query}”</div>}
          {visible.map(p => (
            <div className={'graph-card' + (p.current ? ' current' : '')}
              key={p.name} role="button" tabIndex={0}
              onClick={() => openGraph(p.name)}
              onKeyDown={e => { if (e.key === 'Enter') openGraph(p.name) }}>
              <MiniGraph preview={p.preview} />
              <div className="gc-foot">
                <span className="gc-ico" aria-hidden="true">⬡</span>
                <div className="gc-text">
                  <div className="gc-name">{p.title || p.name}
                    {p.dashboard ? <span className="gc-live" title="dashboard running">●</span> : null}
                    {p.current ? <span className="gc-badge">current</span> : null}
                  </div>
                  <div className="gc-meta">
                    {p.agents == null ? '…'
                      : `${p.agents} agent${p.agents === 1 ? '' : 's'} · ${p.edges == null ? '?' : p.edges} edge${p.edges === 1 ? '' : 's'}`}
                  </div>
                  <div className="gc-desc" title={p.description || ''}>
                    {p.description || <span className="dim">no description</span>}
                  </div>
                </div>
              </div>
            </div>
          ))}
        </div>
      </main>
      {creating && (
        <Dialog open onClose={() => setCreating(false)} maxWidth="sm" fullWidth>
          <DialogTitle sx={{ fontSize: 15, fontWeight: 'bold' }}>New graph</DialogTitle>
          <DialogContent>
            <TextField id="g-name" label="Name" placeholder="AWB on crew"
              helperText="any text — crew derives a machine id for sessions/paths"
              margin="dense" fullWidth autoFocus
              slotProps={{ inputLabel: { shrink: true } }} />
            <TextField id="g-desc" label="What is this crew for?" margin="dense"
              placeholder="finds leads and builds demo sites for them" fullWidth
              multiline minRows={2} slotProps={{ inputLabel: { shrink: true } }} />
            <div className="f-hint">A foreman agent is created and launched with
              the new graph — open its terminal and describe the system you want;
              it builds the crew for you.</div>
            <div className="f-actions">
              <Button id="g-go" variant="contained" disableElevation
                disabled={busy} onClick={createGraph}>Create graph</Button>
            </div>
          </DialogContent>
        </Dialog>
      )}
      <Toast msg={toastMsg} />
    </div>
  )
}
