// Pending-approval tray — every result="pending" graph_edit row; approve
// executes the stored request (human-only server-side), reject records a
// reason without executing.
import Button from '@mui/material/Button'
import ModalShell from './ModalShell.jsx'
import { useSubmit } from './formUtils.jsx'
import { ageText } from '../../modalShared.js'

function attentionText(r, state) {
  if (state === 'applying') {
    return 'Reconciliation/manual review required: the mutation may have started. Do not replay it blindly.'
  }
  if (state === 'approval_failed') {
    return `${r.reason || 'Approval failed without a stored reason.'} Manual review is required before recovery or retry.`
  }
  return ''
}

export default function PendingModal({ api, toast, refresh, onClose, rows }) {
  const { busy, submit } = useSubmit({ toast, refresh, onClose })
  const list = rows || []
  return (
    <ModalShell title="Approval attention" onClose={onClose}>
      {list.length ? (
        <div className="pend-list">
          {list.map(r => {
            const state = r.result || 'pending'
            const attention = attentionText(r, state)
            return (
              <div className="pend-row" data-guid={r._guid} key={r._guid}>
                <div className="pend-main">
                  <div><b>{r.actor}</b> <span className="dim">{r.op}</span>{' '}
                    <span className={`pend-state ${state}`}>{state}</span></div>
                  <div className="pend-summary">{r.summary || ''}</div>
                  {attention ? <div className="pend-attention">{attention}</div> : null}
                  <div className="dim" style={{ fontSize: 11 }}>{ageText(r.created_at)} ago</div>
                </div>
                <div className="pend-actions">
                  {state === 'pending' && (
                    <>
                      <Button className="pend-approve" size="small" variant="contained"
                        disableElevation disabled={busy}
                        onClick={() => submit(() => api.pendingApprove(r._guid), 'approved')}>
                        approve</Button>
                      <Button className="pend-reject" size="small" color="error"
                        disabled={busy}
                        onClick={() => {
                          const reason = window.prompt('reason for rejecting (optional):', '') || ''
                          submit(() => api.pendingReject(r._guid, reason), 'rejected')
                        }}>reject</Button>
                    </>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      ) : <div className="empty">no pending requests</div>}
    </ModalShell>
  )
}
