// Identity card — read-only view of who an agent is + its channels, built
// entirely from the graph snapshot (the same data identity.md is rendered
// from). JSX auto-escaping replaces modal.js's hand-rolled esc().
import Button from '@mui/material/Button'
import ModalShell from './ModalShell.jsx'
import { useSubmit } from './formUtils.jsx'
import { identityChannels, capsText } from '../../modalShared.js'
import { statusLabel, liveStatus } from '../../status.js'

function Detail({ value }) {
  const text = String(value == null ? '' : value).trim()
  if (!text) return <span className="dim">—</span>
  const lines = text.split('\n')
  return (
    <span className="id-copy">
      {lines.map((line, i) => (i ? [<br key={i} />, line] : line))}
    </span>
  )
}

function Grants({ grants }) {
  const rows = Array.isArray(grants) ? grants : []
  if (!rows.length) return <span className="dim">none</span>
  return (
    <div>
      <ul className="id-list">
        {rows.map((grant, i) => (
          <li key={i}>
            <span className="mono">refs/{String((grant || {}).name || '?')}</span>
            {' → '}
            <span className="mono">{String((grant || {}).path || '?')}</span>
            {' '}({String((grant || {}).mode || 'ro')})
          </li>
        ))}
      </ul>
      <span className="f-note">Recorded intent, not filesystem enforcement.</span>
    </div>
  )
}

function CondText({ conds }) {
  return conds.length
    ? <>{conds.join(' · ')}</>
    : <span className="dim">any time</span>
}

export default function IdentityModal({ api, toast, refresh, onClose, worker, edges }) {
  const { busy, submit } = useSubmit({ toast, refresh, onClose })
  if (!worker) return null
  const name = worker.name
  const { out, inc } = identityChannels(name, edges)
  const st = liveStatus(worker)
  const isBlessed = worker.blessed !== false
  const isForeman = !!worker.can_edit_graph

  return (
    <ModalShell title={`${name} — identity`} onClose={onClose}>
      <div className="id-card">
        <div className="id-row"><span className="id-k">role</span>
          <span>{worker.role || <span className="dim">—</span>}</span></div>
        <div className="id-row"><span className="id-k">identity / mission</span>
          <Detail value={worker.identity} /></div>
        <div className="id-row"><span className="id-k">notes</span>
          <Detail value={worker.notes} /></div>
        <div className="id-row"><span className="id-k">file grants</span>
          <Grants grants={worker.grants} /></div>
        <div className="id-row"><span className="id-k">home</span>
          <span className="mono">{worker.home || <span className="dim">—</span>}</span></div>
        <div className="id-row"><span className="id-k">runtime</span>
          <span>{worker.runtime || 'claude'}</span></div>
        <div className="id-row"><span className="id-k">status</span>
          <span>{statusLabel(st)}</span></div>
        <div className="id-row"><span className="id-k">activity</span>
          <Detail value={worker.activity} /></div>
        <div className="id-row"><span className="id-k">blessed</span>
          <span>{isBlessed
            ? <span className="dim">yes</span>
            : <>
              <Button id="id-bless" size="small" disabled={busy}
                onClick={() => submit(() => api.agentBless(name), `blessed ${name}`)}>
                bless</Button>
              {' '}<span className="f-note" style={{ display: 'inline' }}>
                agent-authored, not yet reviewed</span>
            </>}</span></div>
        <div className="id-row"><span className="id-k">foreman</span>
          <span>
            <span className="dim">{isForeman ? 'yes' : 'no'}</span>{' '}
            <Button id="id-foreman-toggle" size="small" disabled={busy}
              onClick={() => submit(
                () => api.agentForeman({ name, revoke: isForeman }),
                isForeman ? `revoked foreman from ${name}` : `${name} is now foreman`)}>
              {isForeman ? 'revoke foreman' : 'make foreman'}</Button>
          </span></div>
        <div className="id-sec">talks to <span className="arrow">→</span></div>
        <ul className="id-list">
          {out.length ? out.map((r, i) => (
            <li key={i}><b>{r.peer}</b> — <CondText conds={r.conds} />
              {capsText(r.e) ? <span className="dim"> (cap: {capsText(r.e)})</span> : null}
              {r.reply ? <span className="dim"> · reply expected</span> : null}</li>
          )) : <li className="dim">no one yet — drag an edge from this node to connect it</li>}
        </ul>
        <div className="id-sec">hears from <span className="arrow">←</span></div>
        <ul className="id-list">
          {inc.length ? inc.map((r, i) => (
            <li key={i}><b>{r.peer}</b>
              {r.conds.length ? <> — <CondText conds={r.conds} /></> : null}
              {r.act ? <div className="dim">you: {r.act}</div> : null}</li>
          )) : <li className="dim">no one yet</li>}
        </ul>
      </div>
      <div className="f-hint">This mirrors the agent's identity.md. Edit a channel
        by clicking its edge in the graph.</div>
    </ModalShell>
  )
}
