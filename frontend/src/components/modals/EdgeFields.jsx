// EdgeFields — the shared body of the connect + edit-edge forms: per-direction
// trigger lists/actions/reply flags, the two-way toggle, and the rate/budget
// caps. The parent owns the checkbox states (submit validation needs them);
// text fields stay uncontrolled and are read back by id at submit.
import Checkbox from '@mui/material/Checkbox'
import FormControlLabel from '@mui/material/FormControlLabel'
import { Field, CondList, numericText } from './formUtils.jsx'

export const readEdgeCaps = () => ({
  max_turns: numericText('e-max'),
  token_cap: numericText('e-token-cap'),
  cost_cap: numericText('e-cost-cap'),
})

export function EdgePair({ S, T, twoWay }) {
  return (
    <div className="f-pair">
      <b>{S}</b> <span className="arrow" id="e-arrow">{twoWay ? '↔' : '→'}</span> <b>{T}</b>
    </div>
  )
}

export default function EdgeFields({
  S, T, defaults = {},
  twoWay, setTwoWay, reply, setReply, backReply, setBackReply,
}) {
  return (
    <>
      <Field id="e-label" label="Label" ph="qualified lead" value={defaults.label} />
      <div className="edge-dir">
        <div className="edge-dir-h">{S} <span className="arrow">→</span> {T}</div>
        <CondList id="e-when" label={`When should ${S} message ${T}?`}
          initial={defaults.conds} ph="e.g. when a lead is qualified" />
        <Field id="e-does" label={`What should ${T} do on receipt?`} rows={3}
          ph="e.g. build a one-page demo and reply with the URL" value={defaults.action} />
        <FormControlLabel className="f-check" label={`${T} should reply back`}
          control={<Checkbox checked={reply} onChange={e => setReply(e.target.checked)}
            slotProps={{ input: { id: 'e-reply' } }} />} />
        <div className="f-note">requires a Two-way relationship so the reply is authorized</div>
      </div>
      <FormControlLabel className="f-check" label="Two-way — both can message each other"
        control={<Checkbox checked={twoWay} onChange={e => setTwoWay(e.target.checked)}
          slotProps={{ input: { id: 'e-undirected' } }} />} />
      <div className={'edge-dir edge-back' + (twoWay ? '' : ' disabled')} id="e-back-wrap">
        <div className="edge-dir-h">{T} <span className="arrow">→</span> {S}{' '}
          <span className="dim">(two-way only)</span></div>
        <CondList id="e-when-back" label={`When should ${T} message ${S}?`}
          initial={defaults.backConds} ph="e.g. when the demo needs changes"
          disabled={!twoWay} />
        <Field id="e-does-back" label={`What should ${S} do on receipt?`} rows={3}
          value={defaults.backAction} />
        <FormControlLabel className="f-check" label={`${S} should reply back`}
          control={<Checkbox checked={backReply} disabled={!twoWay}
            onChange={e => setBackReply(e.target.checked)}
            slotProps={{ input: { id: 'e-reply-back' } }} />} />
      </div>
      <Field id="e-max" label="Limit messages per hour (0 = no limit)" ph="0"
        value={defaults.max != null ? String(defaults.max) : '0'}
        note="rate-limits this link so a tight back-and-forth loop never runs away" />
      <Field id="e-token-cap" label="Token budget/hr (0 = uncapped)" ph="0"
        value={defaults.tokenCap != null ? String(defaults.tokenCap) : '0'}
        note="refuses sends once the target's hourly token spend hits this" />
      <Field id="e-cost-cap" label="$ budget/hr (0 = uncapped)" ph="0"
        value={defaults.costCap != null ? String(defaults.costCap) : '0'}
        note="refuses sends once the target's hourly $ spend hits this" />
    </>
  )
}
