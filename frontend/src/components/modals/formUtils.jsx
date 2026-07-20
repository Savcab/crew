// Shared form plumbing for the modals.
//
// Text inputs are deliberately UNCONTROLLED and read back through the DOM at
// submit time (same contract as the old modal.js): browser test scripts and
// the /api/expand prefill both write straight into the inputs by id, which a
// controlled React input would silently ignore. Checkboxes are the opposite —
// MUI renders their visual state from React, so they must be controlled.
import { useEffect, useRef, useState } from 'react'
import TextField from '@mui/material/TextField'

// Staleness guard for async modal callbacks. Remount-per-open scopes React
// STATE to each modal instance, but two things escape that scope: raw DOM
// writes by id (a replacement modal renders the same ids) and the global
// onClose. Every await inside a modal must re-check alive.current before
// touching the DOM or closing anything — the React equivalent of the old
// modal.js epoch check.
export function useAlive() {
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])
  return alive
}

export const val = id => (document.getElementById(id) || {}).value
export const checked = id => !!(document.getElementById(id) || {}).checked

// Prefill helper for uncontrolled MUI text fields (the /api/expand writers).
export const setVal = (id, v) => {
  const el = document.getElementById(id)
  if (el) el.value = v == null ? '' : v
}

// Keep numeric text lossless until the strict server boundary validates it —
// see normalizeEdgeCapText in modalShared.js.
import { normalizeEdgeCapText } from '../../modalShared.js'
export const numericText = id => normalizeEdgeCapText(val(id))

export const readCondList = id => {
  const el = document.getElementById(id)
  return el
    ? [...el.querySelectorAll('.cl-input')].map(i => i.value.trim()).filter(Boolean)
    : []
}

// One submit at a time; buttons disable while in flight; success toasts,
// closes, and refreshes the graph (modal.js's submit()). Remount-per-open
// scopes the button/busy state; the alive-ref check covers what remounting
// can't — the global onClose and post-await toasts.
export function useSubmit({ toast, refresh, onClose }) {
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  const alive = useAlive()
  async function submit(fn, okMsg) {
    if (busyRef.current) return
    busyRef.current = true
    setBusy(true)
    let r
    try { r = await fn() }
    catch (e) {
      busyRef.current = false
      if (!alive.current) return   // stale failure: replacement modal isn't ours
      setBusy(false)
      toast('request failed', true)
      return
    }
    if (!r || r.ok !== true) {
      busyRef.current = false
      if (!alive.current) return
      setBusy(false)
      toast((r && r.error) || 'failed', true)
      return
    }
    // Success still toasts + refreshes even if this form is gone (matching the
    // old modal.js), but must never close a modal this instance didn't open.
    toast(okMsg)
    if (alive.current) onClose()
    refresh(true)
  }
  return { busy, submit }
}

// A labelled MUI text field that keeps the legacy input id (scripts + expand
// prefill target it). Always-shrunk label: values arrive via DOM writes too.
export function Field({ id, label, note, ph, value, rows, autoFocus, required }) {
  return (
    <TextField id={id} label={label} placeholder={ph} defaultValue={value || ''}
      helperText={note} margin="dense" autoFocus={autoFocus} required={!!required}
      multiline={!!rows} minRows={rows}
      slotProps={{ inputLabel: { shrink: true } }} />
  )
}

// The condition LIST editor (multiple "when to message" triggers per
// direction) — same .cl-rows/.cl-input/.cl-add/.cl-del contract as before.
// Rows are uncontrolled; remount (key bump) replaces them wholesale (the
// /api/expand prefill path).
export function CondList({ id, label, initial, ph, disabled }) {
  const seed = (initial && initial.length ? initial : ['']).map((v, i) => ({ key: i, val: v }))
  const [rows, setRows] = useState(seed)
  const nextKey = useRef(seed.length)
  const add = () => {
    if (disabled) return
    setRows(r => [...r, { key: nextKey.current++, val: '' }])
  }
  const del = key => {
    setRows(r => {
      const kept = r.filter(row => row.key !== key)
      return kept.length ? kept : [{ key: nextKey.current++, val: '' }]
    })
  }
  return (
    <div className="f-row">
      <label>{label}</label>
      <div className={'cl-rows' + (disabled ? ' off' : '')} id={id} data-ph={ph || ''}>
        {rows.map(row => (
          <div className="cl-row" key={row.key}>
            <input type="text" className="cl-input" defaultValue={row.val}
              placeholder={ph || ''} disabled={disabled} />
            <button type="button" className="cl-del" title="remove"
              disabled={disabled} onClick={() => del(row.key)}>×</button>
          </div>
        ))}
      </div>
      <button type="button" className="cl-add" data-for={id} disabled={disabled}
        onClick={add}>+ add another condition</button>
    </div>
  )
}
