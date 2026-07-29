// SettingsPage — /?view=settings, a full page (its own view like GraphsGallery,
// no App and no header) rather than a modal: settings are a place you go, and
// the left sidebar is where later groups slot in beside "Harnesses".
//
// Each harness's startup command is a SELECT of curated choices, never a
// typable shell line — the server refuses anything that is not one of the
// choices it published, so offering a text box would only invent failures.
// Picking the FIRST choice (the default) clears the stored override.
//
// An environment variable of the same setting outranks anything stored here, so
// each row states where its effective value actually comes from — otherwise a
// saved value that an env var is quietly overriding looks like it took effect.
import { useCallback, useEffect, useState } from 'react'
import Button from '@mui/material/Button'
import { api as defaultApi } from '../api.js'
import EnvironmentsTab from './EnvironmentsTab.jsx'
import Toast from './Toast.jsx'

// The array + content switch is the seam: a group is an entry here plus a
// branch below, not a rewrite of the page. `foot` is the sidebar's standing
// caveat for that group — the two groups take effect at different moments, and
// one blanket sentence for both would be wrong for whichever it was not
// written about.
const TABS = [
  {
    id: 'harnesses', label: 'Harnesses', ico: '⌨',
    foot: 'These apply to every agent this crew launches from now on; running '
      + 'agents keep the command they started with.',
  },
  {
    id: 'environments', label: 'Environments', ico: '📦',
    foot: 'An environment prepares an agent\'s workspace before its runtime '
      + 'starts. Its commands run as you, so defining them is human-only — '
      + 'agents can pick one, never write one.',
  },
]

// What the select should show for a row as the server currently has it: the
// stored override when there is one, otherwise the default.
const current = row => (row.override == null ? (row.default || '') : row.override)

// The choices the server published, plus — only when a stored override is not
// among them — the legacy custom value it was saved with. Dropping it silently
// would rewrite the operator's setting the next time they saved anything else.
function optionsFor(row) {
  const choices = (row.choices || []).length
    ? row.choices
    : [{ label: 'Default', command: row.default || '' }]
  const stored = row.override
  return stored && !choices.some(c => c.command === stored)
    ? [...choices, { label: 'Stored custom command', command: stored }]
    : choices
}

function sourceNote(row) {
  if (row.source === 'env') {
    return `environment override active — it wins over anything saved here: ${row.effective}`
  }
  return row.source === 'settings' ? 'stored override' : 'default'
}

export default function SettingsPage({ api = defaultApi }) {
  const [tab, setTab] = useState(TABS[0].id)
  const [rows, setRows] = useState(null)
  // key → the command currently selected in that row's <select>.
  const [picked, setPicked] = useState({})
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [toastMsg, setToastMsg] = useState(null)
  const toast = useCallback((text, err) => {
    setToastMsg({ text, err: !!err, at: Date.now() })
  }, [])

  // Repaint from a server answer. `written` names the keys this save actually
  // landed; every other row keeps the operator's unsaved selection, so a
  // partial failure neither loses their pick nor pretends it was stored.
  const applyRows = useCallback((next, written) => {
    setRows(next)
    setPicked(prev => Object.fromEntries(next.map(row => [
      row.key,
      written && !written.has(row.key) && prev[row.key] != null
        ? prev[row.key] : current(row),
    ])))
  }, [])

  useEffect(() => { document.title = 'crew — settings' }, [])

  useEffect(() => {
    let alive = true
    ;(async () => {
      let r
      try { r = await api.settings() }
      catch (e) { r = { ok: false, error: (e && e.message) || 'request failed' } }
      if (!alive) return
      if (!r || !r.ok) {
        const message = (r && r.error) || 'failed'
        setError(message)
        toast(message, true)
        return
      }
      applyRows(r.settings || [])
    })()
    return () => { alive = false }
  }, [api, applyRows, toast])

  // Only changed rows are written, one request each: the server takes a single
  // key per call, and a partial failure must leave the earlier writes visible
  // rather than pretending the whole page failed.
  const save = async () => {
    if (busy || !rows) return
    const edits = rows
      .filter(row => picked[row.key] !== current(row))
      // The first choice IS the default, and an empty value is what clears the
      // stored override — so choosing it must send blank, not the command.
      .map(row => ({
        key: row.key,
        value: picked[row.key] === row.default ? '' : picked[row.key],
      }))
    if (!edits.length) { toast('no changes to save'); return }
    setBusy(true)
    const written = new Set()
    let latest = null
    for (const edit of edits) {
      let r
      try { r = await api.settingsUpdate(edit.key, edit.value) }
      catch (e) { r = { ok: false, error: (e && e.message) || 'request failed' } }
      if (!r || !r.ok) {
        setBusy(false)
        if (latest) applyRows(latest, written)
        toast((r && r.error) || 'failed', true)
        return
      }
      written.add(edit.key)
      latest = r.settings || latest
    }
    setBusy(false)
    if (latest) applyRows(latest, written)
    toast('settings saved')
  }

  const active = TABS.find(t => t.id === tab) || TABS[0]
  return (
    <div id="settings">
      <aside id="settings-nav">
        <div className="gs-brand">crew</div>
        <a className="gs-back" id="settings-back" href="/">← back to the graph</a>
        <nav className="gs-nav">
          {TABS.map(t => (
            <div key={t.id} id={`settings-tab-${t.id}`}
              className={'gs-item' + (t.id === tab ? ' active' : '')}
              role="button" tabIndex={0} onClick={() => setTab(t.id)}
              onKeyDown={e => { if (e.key === 'Enter') setTab(t.id) }}>
              <span className="gs-ico" aria-hidden="true">{t.ico}</span> {t.label}
            </div>
          ))}
        </nav>
        <div className="gs-foot">{active.foot}</div>
      </aside>
      <main id="settings-main">
        <div id="settings-bar">
          <span className="gb-title">Settings · {active.label}</span>
        </div>
        <div id="settings-body">
          {tab === 'environments' &&
            <EnvironmentsTab api={api} toast={toast} />}
          {tab === 'harnesses' && <>
          {!rows && !error &&
            <div className="f-hint" id="settings-loading">loading…</div>}
          {error &&
            <div className="empty" id="settings-error">
              could not load settings: {error}
            </div>}
          {rows && rows.map(row => (
            <div className="set-row" key={row.key}>
              <label className="set-label" htmlFor={`setting-${row.key}`}>
                {row.label}
              </label>
              <select className="set-select" id={`setting-${row.key}`}
                value={picked[row.key] == null ? '' : picked[row.key]}
                onChange={e => setPicked(
                  p => ({ ...p, [row.key]: e.target.value }))}>
                {optionsFor(row).map(c => (
                  <option key={c.command} value={c.command}>
                    {`${c.label} — ${c.command}`}
                  </option>
                ))}
              </select>
              <div className="set-note">{sourceNote(row)}</div>
            </div>
          ))}
          {rows &&
            <div className="set-actions">
              <Button id="settings-save" variant="contained" disableElevation
                disabled={busy} onClick={save}>Save</Button>
            </div>}
          </>}
        </div>
      </main>
      <Toast msg={toastMsg} />
    </div>
  )
}
