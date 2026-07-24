import { useMemo, useState } from 'react'
import Button from '@mui/material/Button'
import TextField from '@mui/material/TextField'
import ModalShell from './ModalShell.jsx'
import { Field, useAlive, useSubmit, val } from './formUtils.jsx'

function absoluteUrl(value) {
  try { return new URL(value || '', window.location.origin).href }
  catch (error) { return value || '' }
}

export default function WebhookModal({
  api, toast, refresh, onClose, webhook,
}) {
  const { busy, submit } = useSubmit({ toast, refresh, onClose })
  const alive = useAlive()
  const [current, setCurrent] = useState(webhook)
  const [rotating, setRotating] = useState(false)
  const url = useMemo(
    () => absoluteUrl((current || {}).public_url),
    [current && current.public_url])

  if (!current) return null

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(url)
      if (alive.current) toast('hook URL copied')
    } catch (error) {
      if (alive.current) toast('could not copy hook URL', true)
    }
  }

  const rotate = async () => {
    if (rotating || !window.confirm(
      'Rotate this hook URL? The current URL will stop working immediately.')) return
    setRotating(true)
    let result
    try { result = await api.webhookRotate(current._guid) }
    catch (error) { result = { ok: false, error: 'request failed' } }
    if (!alive.current) return
    setRotating(false)
    if (!result || !result.ok) {
      toast((result && result.error) || 'failed', true)
      return
    }
    setCurrent(result.webhook)
    toast('hook URL rotated')
    refresh(true)
  }

  const save = () => submit(() => api.webhookUpdate({
    guid: current._guid,
    description: val('w-description'),
    template: val('w-template'),
  }), 'hook updated')

  const remove = () => {
    if (!window.confirm(
      `Delete hook ${current.name}? Its URL and graph routes will stop working.`)) return
    submit(() => api.webhookDelete(current._guid), `deleted hook ${current.name}`)
  }

  return (
    <ModalShell title={`${current.name} — hook`} onClose={onClose}>
      <div className="hook-url-row">
        <TextField id="w-url" label="Public POST URL" value={url}
          fullWidth margin="dense"
          slotProps={{ input: { readOnly: true }, inputLabel: { shrink: true } }} />
        <Button id="w-copy" size="small" onClick={copy}>Copy</Button>
      </div>
      <div className="f-actions hook-url-actions">
        <Button id="w-rotate" size="small" color="warning"
          disabled={busy || rotating} onClick={rotate}>Rotate URL</Button>
      </div>
      <Field id="w-description" label="What sends events here?" rows={2}
        value={current.role} />
      <Field id="w-template" label="Message template (optional)" rows={5}
        value={current.webhook_template}
        ph={'New issue: {{ payload.issue.title }}'}
        note={'Dot paths can read payload objects/arrays and lower-case headers. '
          + 'Missing fields reject the request without sending.'} />
      <div className="id-card hook-status">
        <div className="id-row"><span className="id-k">last call</span>
          <span>{current.webhook_last_called_at
            ? new Date(current.webhook_last_called_at * 1000).toLocaleString()
            : <span className="dim">never</span>}</span></div>
        <div className="id-row"><span className="id-k">result</span>
          <span>{current.webhook_last_status || <span className="dim">—</span>}</span></div>
      </div>
      <div className="f-actions">
        <Button id="w-del" color="error" disabled={busy || rotating}
          onClick={remove}>Delete</Button>
        <Button id="w-save" variant="contained" disableElevation
          disabled={busy || rotating} onClick={save}>Save</Button>
      </div>
      <div className="f-hint">The URL is a secret capability. For internet
        traffic, expose only <span className="mono">/hooks/*</span> through a
        TLS proxy or tunnel; dashboard controls stay local and authenticated.</div>
    </ModalShell>
  )
}
