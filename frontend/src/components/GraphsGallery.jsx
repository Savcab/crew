// GraphsGallery — /?view=graphs: every project is one graph, Figma-style.
// Cards show name/description/agent count/live state; opening a graph jumps
// to the dashboard process that owns it (agents keep running everywhere —
// they live in tmux, not in any browser window). "New graph" creates the
// project and seeds a launched foreman that builds the crew from a
// description (chat-to-build entry).
import { useCallback, useEffect, useState } from 'react'
import Button from '@mui/material/Button'
import TextField from '@mui/material/TextField'
import Dialog from '@mui/material/Dialog'
import DialogTitle from '@mui/material/DialogTitle'
import DialogContent from '@mui/material/DialogContent'
import { api } from '../api.js'
import Toast from './Toast.jsx'

export default function GraphsGallery() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
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
    const t = setInterval(load, 4000)
    return () => clearInterval(t)
  }, [load])

  const openGraph = useCallback(async name => {
    if (busy) return
    setBusy(true)
    try {
      const j = await api.projectOpen(name)
      if (j && j.ok && j.url) {
        if (j.port === parseInt(window.location.port, 10)) {
          window.location.href = '/'
        } else {
          window.location.href = j.url
        }
        return
      }
      toast((j && j.error) || 'could not open graph', true)
    } catch (e) { toast('could not open graph', true) }
    finally { setBusy(false) }
  }, [busy, toast])

  const createGraph = useCallback(async () => {
    const name = (document.getElementById('g-name').value || '').trim()
    const description = (document.getElementById('g-desc').value || '').trim()
    if (!name) { toast('name required', true); return }
    setBusy(true)
    try {
      const j = await api.projectCreate({ name, description })
      if (!j || !j.ok) { toast((j && j.error) || 'create failed', true); return }
      if (j.warning) toast(j.warning, true)
      else toast(`created '${name}' — foreman is booting; opening…`)
      setCreating(false)
      await openGraph(name)
    } catch (e) { toast('create failed', true) }
    finally { setBusy(false) }
  }, [openGraph, toast])

  return (
    <div id="graphs">
      <header>
        <h1>crew</h1>
        <span className="meta">your graphs — each one is an independent crew;
          agents keep running when you leave</span>
        <Button id="newGraphBtn" variant="contained" disableElevation
          sx={{ ml: 'auto' }} onClick={() => setCreating(true)}>+ New graph</Button>
      </header>
      <div id="graphs-grid">
        {error && <div className="empty">backend unavailable: {error}</div>}
        {data && data.projects.map(p => (
          <div className={'graph-card' + (p.current ? ' current' : '')}
            key={p.name} role="button" tabIndex={0}
            onClick={() => openGraph(p.name)}
            onKeyDown={e => { if (e.key === 'Enter') openGraph(p.name) }}>
            <div className="gc-name">{p.name}
              {p.dashboard ? <span className="gc-live" title="dashboard running">●</span> : null}
              {p.current ? <span className="gc-badge">current</span> : null}
            </div>
            <div className="gc-desc">{p.description ||
              <span className="dim">no description</span>}</div>
            <div className="gc-meta">
              {p.agents == null ? '…' : `${p.agents} agent${p.agents === 1 ? '' : 's'}`}
              {' · '}{p.app}
            </div>
          </div>
        ))}
      </div>
      {creating && (
        <Dialog open onClose={() => setCreating(false)} maxWidth="sm" fullWidth>
          <DialogTitle sx={{ fontSize: 15, fontWeight: 'bold' }}>New graph</DialogTitle>
          <DialogContent>
            <TextField id="g-name" label="Name" placeholder="lead-gen" margin="dense"
              autoFocus slotProps={{ inputLabel: { shrink: true } }} />
            <TextField id="g-desc" label="What is this crew for?" margin="dense"
              placeholder="finds leads and builds demo sites for them"
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
