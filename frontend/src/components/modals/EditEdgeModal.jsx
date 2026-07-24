// Edit / delete an existing edge; unblessed (agent-authored) edges also get a
// bless action here.
import { useState } from 'react'
import Button from '@mui/material/Button'
import ModalShell from './ModalShell.jsx'
import EdgeFields, {
  EdgePair, WebhookEdgeFields, readEdgeCaps,
} from './EdgeFields.jsx'
import { useSubmit, val, readCondList } from './formUtils.jsx'
import { edgeConds } from '../../modalShared.js'

export default function EditEdgeModal({ api, toast, refresh, onClose, edge }) {
  const { busy, submit } = useSubmit({ toast, refresh, onClose })
  const [twoWay, setTwoWay] = useState(edge.directed === false)
  const [reply, setReply] = useState(!!edge.reply_expected)
  const [backReply, setBackReply] = useState(!!edge.back_reply)
  const S = edge.source_name, T = edge.target_name
  const webhookEdge = edge.source_kind === 'webhook'

  const save = () => {
    if (webhookEdge) {
      submit(() => api.edgeUpdate({
        guid: edge._guid, label: val('e-label'),
        target_action: val('e-does'),
        ...readEdgeCaps(),
        directed: true,
        reply_expected: false,
        back_reply: false,
      }), 'hook route updated')
      return
    }
    if ((reply || backReply) && !twoWay) {
      toast('Replies require a Two-way relationship', true); return
    }
    submit(() => api.edgeUpdate({
      guid: edge._guid, label: val('e-label'),
      conditions: readCondList('e-when'), target_action: val('e-does'), reply_expected: reply,
      back_conditions: readCondList('e-when-back'), back_action: val('e-does-back'), back_reply: backReply,
      ...readEdgeCaps(),
      directed: !twoWay,
    }), 'edge updated')
  }

  if (webhookEdge) {
    return (
      <ModalShell title="Edit hook route" onClose={onClose}>
        <EdgePair S={S} T={T} twoWay={false} />
        <WebhookEdgeFields S={S} T={T}
          defaults={{
            label: edge.label, action: edge.target_action,
            max: edge.max_turns || 0, tokenCap: edge.token_cap || 0,
            costCap: edge.cost_cap || 0,
          }} />
        <div className="f-actions">
          <Button id="e-del" color="error" disabled={busy}
            onClick={() => submit(
              () => api.edgeDelete({ guid: edge._guid }), 'hook route deleted')}>
            Delete</Button>
          <Button id="e-save" variant="contained" disableElevation disabled={busy}
            onClick={save}>Save</Button>
        </div>
      </ModalShell>
    )
  }

  return (
    <ModalShell title="Edit relationship" onClose={onClose}>
      <EdgePair S={S} T={T} twoWay={twoWay} />
      <EdgeFields S={S} T={T}
        defaults={{
          label: edge.label, conds: edgeConds(edge, false), action: edge.target_action,
          backConds: edgeConds(edge, true), backAction: edge.back_action,
          max: edge.max_turns || 0, tokenCap: edge.token_cap || 0,
          costCap: edge.cost_cap || 0,
        }}
        twoWay={twoWay} setTwoWay={setTwoWay}
        reply={reply} setReply={setReply}
        backReply={backReply} setBackReply={setBackReply} />
      {edge.blessed === false && (
        <div className="f-row">
          <label>Review</label>
          <div>
            <Button id="e-bless" size="small" disabled={busy}
              onClick={() => submit(() => api.edgeBless(edge._guid), 'edge blessed')}>
              bless this edge</Button>
            <span className="f-note">agent-authored change, not yet reviewed</span>
          </div>
        </div>
      )}
      <div className="f-actions">
        <Button id="e-del" color="error" disabled={busy}
          onClick={() => submit(() => api.edgeDelete({ guid: edge._guid }), 'edge deleted')}>
          Delete</Button>
        <Button id="e-save" variant="contained" disableElevation disabled={busy}
          onClick={save}>Save</Button>
      </div>
    </ModalShell>
  )
}
