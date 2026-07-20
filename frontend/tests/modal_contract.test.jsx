// Ported from tests/js/modal_contract.mjs — cap-text losslessness and the
// identity card's escaping/caps/status contracts, now rendered through the
// real React IdentityModal (JSX auto-escaping replaces modal.js's esc()).
import { describe, it, expect } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { normalizeEdgeCapText, capsText, identityChannels } from '../src/modalShared.js'
import IdentityModal from '../src/components/modals/IdentityModal.jsx'
import CreateAgentModal from '../src/components/modals/CreateAgentModal.jsx'
import EditEdgeModal from '../src/components/modals/EditEdgeModal.jsx'

describe('edge cap text stays lossless', () => {
  // The form's explicit default "0" means uncapped; deleting that value is
  // incomplete input and must reach strict server validation as blank rather
  // than silently becoming an accepted zero.
  it('preserves exactly what the operator entered', () => {
    expect(normalizeEdgeCapText('0')).toBe('0')
    expect(normalizeEdgeCapText(' 1000 ')).toBe('1000')
    expect(normalizeEdgeCapText('1.5')).toBe('1.5')
    expect(normalizeEdgeCapText('NaN')).toBe('NaN')
    expect(normalizeEdgeCapText('Infinity')).toBe('Infinity')
    expect(normalizeEdgeCapText('-1')).toBe('-1')
    expect(normalizeEdgeCapText('   ')).toBe('')
    expect(normalizeEdgeCapText(undefined)).toBe('')
  })
})

describe('identity card', () => {
  const worker = {
    name: 'agent-safe', role: 'browser fixture',
    identity: 'Mission <script>alert(1)</script>\nsecond line',
    notes: 'Review <b>carefully</b>',
    grants: [{ name: 'shared-docs', path: '/srv/<private>', mode: 'ro' }],
    home: '/tmp/agent-safe', runtime: 'codex',
    session_alive: true, live_status: 'not_started', blessed: true,
  }
  const edges = [{
    source_name: 'agent-safe', target_name: 'peer', directed: true,
    conditions: ['when ready'], max_turns: 5, token_cap: 1200, cost_cap: 2.5,
  }]

  it('derives channels and caps from the snapshot', () => {
    const { out, inc } = identityChannels('agent-safe', edges)
    expect(out).toHaveLength(1)
    expect(out[0].peer).toBe('peer')
    expect(inc).toHaveLength(0)
    expect(capsText(edges[0])).toBe('5 msg/hr · 1,200 tok/hr · $2.5/hr')
  })

  it('renders hostile content inert and uses operator status labels', () => {
    render(<IdentityModal api={{}} toast={() => {}} refresh={() => {}}
      onClose={() => {}} worker={worker} edges={edges} />)
    const body = document.getElementById('modalBody')
    expect(body, 'identity modal did not render its body').toBeTruthy()
    const text = body.textContent
    expect(text).toMatch(/identity \/ mission/i)
    // The hostile markup must appear as TEXT, never as live elements.
    expect(text).toContain('Mission <script>alert(1)</script>')
    expect(body.querySelector('script')).toBe(null)
    expect(text).toContain('Review <b>carefully</b>')
    expect(text).toContain('refs/shared-docs')
    expect(text).toContain('/srv/<private>')
    expect(text).toMatch(/recorded intent/i)
    expect(text).toContain('5 msg/hr')
    expect(text).toContain('1,200 tok/hr')
    expect(text).toContain('$2.5/hr')
    expect(text).toContain('runtime not started')
  })
})

// Regression (found by tests/browser/resilience-accessibility.md on the React
// port): remount-per-open scopes React state to each modal instance, but raw
// DOM writes by id and the global onClose escape that scope. A stale async
// response must not touch a replacement modal.
describe('stale async responses stay scoped to their modal instance', () => {
  const noop = () => {}

  it('a stale Generate cannot write into a replacement form', async () => {
    let resolveExpand
    const api = { expand: () => new Promise(res => { resolveExpand = res }) }
    const first = render(<CreateAgentModal api={api} toast={noop}
      refresh={noop} onClose={noop} />)
    document.getElementById('a-blob').value = 'some description'
    fireEvent.click(document.getElementById('a-generate'))
    first.unmount()
    // A replacement modal renders the same input ids.
    const second = render(<CreateAgentModal api={api} toast={noop}
      refresh={noop} onClose={noop} />)
    resolveExpand({ ok: true, fields: { name: 'must_not_appear', role: 'x' } })
    await Promise.resolve(); await Promise.resolve()
    expect(document.getElementById('a-name').value,
      'stale expand response wrote into the replacement form').toBe('')
    second.unmount()
  })

  it('a stale submit success cannot close a replacement modal', async () => {
    let resolveUpdate
    const closes = []
    const refreshes = []
    const api = { edgeUpdate: () => new Promise(res => { resolveUpdate = res }) }
    const edge = {
      _guid: 'g1', source_name: 'a', target_name: 'b', directed: true,
      conditions: ['c'],
    }
    const first = render(<EditEdgeModal api={api} toast={noop}
      refresh={() => refreshes.push(1)} onClose={() => closes.push('stale')}
      edge={edge} />)
    fireEvent.click(document.getElementById('e-save'))
    first.unmount()
    resolveUpdate({ ok: true })
    await Promise.resolve(); await Promise.resolve()
    expect(closes,
      'stale submit success closed a modal it did not open').toEqual([])
    // The finished mutation still refreshes the graph (old modal.js parity).
    expect(refreshes.length).toBe(1)
  })
})
