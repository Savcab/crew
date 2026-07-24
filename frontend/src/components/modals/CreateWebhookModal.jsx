import Button from '@mui/material/Button'
import ModalShell from './ModalShell.jsx'
import { Field, useSubmit, val } from './formUtils.jsx'

export default function CreateWebhookModal({ api, toast, refresh, onClose }) {
  const { busy, submit } = useSubmit({ toast, refresh, onClose })

  const create = () => {
    const name = (val('w-name') || '').trim()
    if (!name) { toast('name required', true); return }
    submit(() => api.webhookCreate({
      name,
      description: val('w-description'),
      template: val('w-template'),
    }), `created hook ${name}`)
  }

  return (
    <ModalShell title="Create hook" onClose={onClose}>
      <Field id="w-name" label="Name" ph="github-issues" autoFocus />
      <Field id="w-description" label="What sends events here?" rows={2}
        ph="GitHub issue events for the product repository" />
      <Field id="w-template" label="Message template (optional)" rows={5}
        ph={'New issue: {{ payload.issue.title }}\n{{ payload.issue.html_url }}'}
        note={'Blank uses payload.message, payload.text, or the full body. '
          + 'Placeholders can read payload.*, headers.*, or raw.'} />
      <div className="f-actions">
        <Button id="w-go" variant="contained" disableElevation disabled={busy}
          onClick={create}>Create hook</Button>
      </div>
      <div className="f-hint">Crew creates a secret URL for this node. Connect
        the hook to one or more agents; every valid POST is durably queued to
        each connected target.</div>
    </ModalShell>
  )
}
