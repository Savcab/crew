// Environments — the setup a new agent's workspace gets BEFORE its runtime
// starts: an optional prereq check plus an ordered list of commands, run as the
// operator inside the agent's home. Two are built in (a fresh git worktree, a
// Graphite stack) and are owned by the server, so this tab renders them
// read-only rather than offering an edit the server would only refuse.
//
// The command list is free text here, which is the OPPOSITE of the Harnesses
// tab's select-only rule — deliberately. A harness launch command must be one
// of the choices the server published, so a text box there would invent
// failures; an environment IS whatever setup the operator writes, so anything
// less than free text could not express it.
//
// There is no Save button: the default select and the add/remove controls each
// send ONE action and repaint from the server's answer, so what is on screen is
// always what is stored — never a pending edit that looks applied.
import { useCallback, useEffect, useState } from 'react'
import Button from '@mui/material/Button'
import { val, setVal } from './modals/formUtils.jsx'

const NEW_FIELDS = [
  'env-new-name', 'env-new-prereq', 'env-new-commands', 'env-new-description',
]

// The textarea is the editor; one non-blank line is one command.
const commandLines = text =>
  (text || '').split('\n').map(line => line.trim()).filter(Boolean)

export default function EnvironmentsTab({ api, toast }) {
  const [envs, setEnvs] = useState(null)
  const [chosen, setChosen] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const apply = useCallback(answer => {
    setEnvs(answer.environments || [])
    setChosen(answer.default || '')
  }, [])

  useEffect(() => {
    let alive = true
    ;(async () => {
      let r
      try { r = await api.environments() }
      catch (e) { r = { ok: false, error: (e && e.message) || 'request failed' } }
      if (!alive) return
      if (!r || !r.ok) { setError((r && r.error) || 'failed'); return }
      apply(r)
    })()
    return () => { alive = false }
  }, [api, apply])

  // Every mutation has the same shape: one action, then repaint from the
  // answer. Returns the answer ONLY when the write landed, so a caller can
  // clear its form on success and leave it standing on a refusal.
  const write = async payload => {
    if (busy) return null
    setBusy(true)
    let r
    try { r = await api.environmentsUpdate(payload) }
    catch (e) { r = { ok: false, error: (e && e.message) || 'request failed' } }
    setBusy(false)
    if (!r || !r.ok) { toast((r && r.error) || 'failed', true); return null }
    apply(r)
    return r
  }

  // Blank clears the default — the same convention the settings endpoint uses
  // for dropping an override.
  const pickDefault = async name => {
    const r = await write({ action: 'set_default', name })
    if (r) {
      toast(name ? `default environment: ${name}` : 'default environment cleared')
    }
  }

  const add = async () => {
    const name = (val('env-new-name') || '').trim()
    if (!name) { toast('name required', true); return }
    const commands = commandLines(val('env-new-commands'))
    // An environment IS its commands; posting an empty one only invents a
    // server refusal.
    if (!commands.length) { toast('at least one command required', true); return }
    const r = await write({
      action: 'add',
      name,
      commands,
      prereq: (val('env-new-prereq') || '').trim(),
      description: (val('env-new-description') || '').trim(),
    })
    if (!r) return
    NEW_FIELDS.forEach(id => setVal(id, ''))
    toast(`environment ${name} added`)
  }

  const remove = async name => {
    const r = await write({ action: 'remove', name })
    if (r) toast(`environment ${name} removed`)
  }

  if (error) {
    return (
      <div className="empty" id="env-error">
        could not load environments: {error}
      </div>
    )
  }
  if (!envs) return <div className="f-hint" id="env-loading">loading…</div>

  return (
    <>
      <div className="set-row">
        <label className="set-label" htmlFor="env-default">
          Default environment
        </label>
        <select className="set-select" id="env-default" value={chosen}
          disabled={busy} onChange={e => pickDefault(e.target.value)}>
          <option value="">none</option>
          {envs.map(env => (
            <option key={env.name} value={env.name}>{env.name}</option>
          ))}
        </select>
        <div className="set-note">
          What an agent gets when whoever creates it picks none. Stored as soon
          as you choose it — this tab has no Save.
        </div>
      </div>

      <div className="env-list">
        {envs.map(env => (
          <div className="env-card" id={`env-card-${env.name}`} key={env.name}>
            <div className="env-head">
              <span className="env-name">{env.name}</span>
              {env.builtin
                ? <span className="env-tag">built-in</span>
                : <button type="button" className="env-del" disabled={busy}
                  id={`env-remove-${env.name}`}
                  onClick={() => remove(env.name)}>remove</button>}
            </div>
            {env.description
              ? <div className="env-desc">{env.description}</div> : null}
            {env.prereq
              ? <div className="env-prereq">requires <code>{env.prereq}</code></div>
              : null}
            <ol className="env-cmds">
              {(env.commands || []).map((cmd, i) => (
                <li key={i}><code>{cmd}</code></li>
              ))}
            </ol>
          </div>
        ))}
      </div>

      <div className="env-add" id="env-new">
        <div className="set-label">Add an environment</div>
        <input className="set-input" id="env-new-name" placeholder="name, e.g. node-deps" />
        <input className="set-input" id="env-new-prereq"
          placeholder="prereq check, optional — e.g. gt --version" />
        <textarea className="set-area" id="env-new-commands" rows={4}
          placeholder={'one command per line\nnpm ci\nnpm run build'} />
        <input className="set-input" id="env-new-description"
          placeholder="description, optional" />
        <div className="set-actions">
          <Button id="env-add" variant="contained" disableElevation
            disabled={busy} onClick={add}>Add</Button>
        </div>
        <div className="set-note">
          Commands run in order, as you, inside the agent's home before its
          runtime starts. A prereq that fails refuses the spawn instead of
          launching the agent into a half-built workspace.
        </div>
      </div>
    </>
  )
}
