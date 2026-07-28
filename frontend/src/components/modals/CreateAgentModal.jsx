// Create agent — opens in BLOB MODE (describe in plain words → Generate
// prefills the manual fields); "fill manually instead" skips straight to the
// fold. Nothing is ever created from the blob text directly — the operator
// always reviews real fields before submitting.
import { useEffect, useState } from 'react'
import Button from '@mui/material/Button'
import TextField from '@mui/material/TextField'
import Checkbox from '@mui/material/Checkbox'
import FormControlLabel from '@mui/material/FormControlLabel'
import ModalShell from './ModalShell.jsx'
import { Field, useAlive, useSubmit, val, setVal } from './formUtils.jsx'

const RUNTIME_INFO = {
  claude: ['claude --dangerously-skip-permissions',
    'Unattended Claude; permission prompts are disabled. Native identity: CLAUDE.md'],
  codex: ['codex --dangerously-bypass-approvals-and-sandbox --disable hooks',
    'Unattended Codex; approvals and sandboxing are disabled. Native identity: AGENTS.md'],
  custom: ['required custom command',
    'Portable identity.md only; no runtime-specific file is written automatically'],
}

export default function CreateAgentModal({ api, toast, refresh, onClose }) {
  const { busy, submit } = useSubmit({ toast, refresh, onClose })
  const alive = useAlive()
  const [blobHidden, setBlobHidden] = useState(false)
  const [foldOpen, setFoldOpen] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [runtime, setRuntime] = useState('claude')
  const [launch, setLaunch] = useState(true)
  const [environment, setEnvironment] = useState('')
  const [envs, setEnvs] = useState([])
  const [cmdPh, runtimeNote] = RUNTIME_INFO[runtime]

  // The environment list is server-owned, so fetching it is one more thing that
  // can fail. It never blocks the form: a creation that broke because a list of
  // OPTIONAL setup routines did not load would be the worse failure, so the
  // select simply stays at its "none" choice.
  useEffect(() => {
    let alive = true
    ;(async () => {
      let r
      try { r = await api.environments() }
      catch (e) { r = null }
      if (!alive || !r || !r.ok) return
      setEnvs(r.environments || [])
    })()
    return () => { alive = false }
  }, [api])

  const generate = async () => {
    const text = (val('a-blob') || '').trim()
    if (!text) { toast('describe the agent first', true); return }
    setGenerating(true)
    let r
    try { r = await api.expand({ kind: 'agent', text }) }
    catch (e) { r = { ok: false, fallback: { role: text, identity: text } } }
    // A replacement modal renders the same input ids — a stale response must
    // not write into it (the old modal.js epoch check, in alive-ref form).
    if (!alive.current) return
    setGenerating(false)
    const f = (r && r.ok) ? r.fields : ((r && r.fallback) || {})
    setVal('a-name', f.name); setVal('a-role', f.role); setVal('a-identity', f.identity)
    setFoldOpen(true)
    toast(r && r.ok ? 'generated — review below'
      : 'could not generate — filled in your text, review below', !(r && r.ok))
  }

  const create = () => {
    const name = (val('a-name') || '').trim()
    if (!name) { toast('name required', true); return }
    if (runtime === 'custom' && !(val('a-launch-cmd') || '').trim()) {
      toast('custom runtime requires a launch command', true); return
    }
    submit(() => api.agentCreate({
      name, role: val('a-role'), identity: val('a-identity'),
      home: val('a-home') || undefined, repo: val('a-repo') || undefined,
      // Omitted, never sent blank: blank would name a nameless environment,
      // where absent is what lets the crew-wide default apply.
      environment: environment || undefined,
      runtime,
      launch_cmd: val('a-launch-cmd') || undefined,
      launch,
    }), `creating ${name}…`)
  }

  return (
    <ModalShell title="Create agent" onClose={onClose}>
      {!blobHidden && (
        <div id="a-blob-mode">
          <Field id="a-blob" label="Describe this agent in plain words" rows={3}
            ph="e.g. finds businesses with no website and cold-emails them" autoFocus />
          <div className="f-actions">
            <Button id="a-generate" variant="contained" disableElevation
              disabled={generating} onClick={generate}>Generate</Button>
            <span id="a-gen-spinner" className="spinner"
              style={{ display: generating ? '' : 'none' }}>generating…</span>
          </div>
          <div className="f-hint">
            <a href="#" id="a-manual-link" onClick={e => {
              e.preventDefault(); setBlobHidden(true); setFoldOpen(true)
            }}>fill manually instead</a>
          </div>
        </div>
      )}
      <details className="f-adv" id="a-form-fold" open={foldOpen}
        onToggle={e => setFoldOpen(e.target.open)}>
        <summary>Advanced / manual fields</summary>
        <Field id="a-name" label="Name" ph="leads" />
        <Field id="a-role" label="What does it do?" ph="finds businesses with no website" />
        <Field id="a-identity" label="Identity / mission" rows={3}
          ph="who this agent is and what it owns" />
        <Field id="a-home" label="Home folder" ph="defaults under $CREW_ROOT/<project>/<name>"
          note="blank uses the project-scoped Crew root; one non-overlapping home per agent" />
        <Field id="a-repo" label="Start on a copy of a repo" ph="/path/to/repo"
          note="instead of a home folder, give it a fresh branch (git worktree) of an existing repo" />
        <TextField select id="a-environment" label="Environment" value={environment}
          onChange={e => setEnvironment(e.target.value)} margin="dense"
          helperText="setup that runs in the workspace before the runtime starts; none uses the crew default"
          slotProps={{ select: { native: true }, inputLabel: { shrink: true } }}>
          <option value="">none</option>
          {envs.map(env => (
            <option key={env.name} value={env.name}>{env.name}</option>
          ))}
        </TextField>
        <TextField select id="a-runtime" label="Runtime" value={runtime}
          onChange={e => setRuntime(e.target.value)} margin="dense"
          helperText={<span id="a-runtime-note">{runtimeNote}</span>}
          slotProps={{ select: { native: true }, inputLabel: { shrink: true } }}>
          <option value="claude">Claude Code</option>
          <option value="codex">Codex CLI</option>
          <option value="custom">Custom command</option>
        </TextField>
        <Field id="a-launch-cmd" label="Launch command" ph={cmdPh}
          required={runtime === 'custom'}
          note="blank = default (runs unattended with permission prompts off — fine for its own isolated folder)" />
        <FormControlLabel className="f-check"
          label="Launch it now"
          control={<Checkbox checked={launch} onChange={e => setLaunch(e.target.checked)}
            slotProps={{ input: { id: 'a-launch' } }} />} />
      </details>
      <div className="f-actions">
        <Button id="a-go" variant="contained" disableElevation disabled={busy}
          onClick={create}>Create agent</Button>
      </div>
      <div className="f-hint">A name and what it does is all you need — crew gives
        it its own folder, writes its identity, and launches the runtime you choose.</div>
    </ModalShell>
  )
}
