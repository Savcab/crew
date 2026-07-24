// Connect — describe a NEW edge between two agents. Blob mode first (Generate
// prefills the manual fields via /api/expand); a Generate result remounts the
// whole field set (key bump) so every field re-seeds from the expansion.
import { useState } from 'react'
import Button from '@mui/material/Button'
import ModalShell from './ModalShell.jsx'
import EdgeFields, {
  EdgePair, WebhookEdgeFields, readEdgeCaps,
} from './EdgeFields.jsx'
import { Field, useAlive, useSubmit, val, readCondList } from './formUtils.jsx'

export default function ConnectEdgeModal({
  api, toast, refresh, onClose, source, target, webhookEdge,
}) {
  const { busy, submit } = useSubmit({ toast, refresh, onClose })
  const alive = useAlive()
  const [blobHidden, setBlobHidden] = useState(false)
  const [foldOpen, setFoldOpen] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [twoWay, setTwoWay] = useState(false)
  const [reply, setReply] = useState(false)
  const [backReply, setBackReply] = useState(false)
  // Generate replaces the seeded defaults and remounts the fields with them.
  const [seed, setSeed] = useState({ v: 0, defaults: {} })

  const generate = async () => {
    const text = (val('e-blob') || '').trim()
    if (!text) { toast('describe the relationship first', true); return }
    setGenerating(true)
    let r
    try { r = await api.expand({ kind: 'edge', text, source, target }) }
    catch (e) { r = { ok: false, fallback: { conditions: [text] } } }
    // Stale response must not touch a replacement modal (see CreateAgentModal).
    if (!alive.current) return
    setGenerating(false)
    const f = (r && r.ok) ? r.fields : ((r && r.fallback) || {})
    setSeed(s => ({
      v: s.v + 1,
      defaults: {
        label: f.label, conds: f.conditions, action: f.target_action,
        backConds: f.back_conditions, backAction: f.back_action,
        max: f.max_turns || 0, tokenCap: f.token_cap || 0, costCap: f.cost_cap || 0,
      },
    }))
    setReply(!!f.reply_expected)
    setTwoWay(f.directed === false)
    setBackReply(!!f.back_reply)
    setFoldOpen(true)
    toast(r && r.ok ? 'generated — review below'
      : 'could not generate — filled in your text, review below', !(r && r.ok))
  }

  const connect = () => {
    if ((reply || backReply) && !twoWay) {
      toast('Replies require a Two-way relationship', true); return
    }
    submit(() => api.edgeCreate({
      source, target, label: val('e-label'),
      conditions: readCondList('e-when'), target_action: val('e-does'), reply_expected: reply,
      back_conditions: readCondList('e-when-back'), back_action: val('e-does-back'), back_reply: backReply,
      ...readEdgeCaps(),
      directed: !twoWay,
    }), `connected ${source} → ${target}`)
  }

  if (webhookEdge) {
    const connectWebhook = () => submit(() => api.edgeCreate({
      source, target, label: val('e-label'),
      conditions: ['when this webhook receives an HTTP request'],
      target_action: val('e-does'),
      ...readEdgeCaps(),
      reply_expected: false,
      back_conditions: [],
      back_action: '',
      back_reply: false,
      directed: true,
    }), `routed ${source} → ${target}`)
    return (
      <ModalShell title="Route hook to agent" onClose={onClose}>
        <EdgePair S={source} T={target} twoWay={false} />
        <WebhookEdgeFields S={source} T={target} />
        <div className="f-actions">
          <Button id="e-go" variant="contained" disableElevation disabled={busy}
            onClick={connectWebhook}>Create route</Button>
        </div>
        <div className="f-hint">Every accepted POST is transformed once by
          the hook, then durably queued to this agent under the limits above.</div>
      </ModalShell>
    )
  }

  return (
    <ModalShell title="Describe the relationship" onClose={onClose}>
      <EdgePair S={source} T={target} twoWay={twoWay} />
      {!blobHidden && (
        <div id="e-blob-mode">
          <Field id="e-blob" label="Describe this relationship in plain words" rows={3}
            ph={`e.g. ${source} sends qualified leads to ${target}, who replies with a demo link`}
            autoFocus />
          <div className="f-actions">
            <Button id="e-generate" variant="contained" disableElevation
              disabled={generating} onClick={generate}>Generate</Button>
            <span id="e-gen-spinner" className="spinner"
              style={{ display: generating ? '' : 'none' }}>generating…</span>
          </div>
          <div className="f-hint">
            <a href="#" id="e-manual-link" onClick={e => {
              e.preventDefault(); setBlobHidden(true); setFoldOpen(true)
            }}>fill manually instead</a>
          </div>
        </div>
      )}
      <details className="f-adv" id="e-form-fold" open={foldOpen}
        onToggle={e => setFoldOpen(e.target.open)}>
        <summary>Advanced / manual fields</summary>
        <EdgeFields key={seed.v} S={source} T={target} defaults={seed.defaults}
          twoWay={twoWay} setTwoWay={setTwoWay}
          reply={reply} setReply={setReply}
          backReply={backReply} setBackReply={setBackReply} />
      </details>
      <div className="f-actions">
        <Button id="e-go" variant="contained" disableElevation disabled={busy}
          onClick={connect}>Connect</Button>
      </div>
      <div className="f-hint">This is the only channel that exists between them.
        Each direction's triggers + what the receiver does are written into both
        agents' identity.</div>
    </ModalShell>
  )
}
