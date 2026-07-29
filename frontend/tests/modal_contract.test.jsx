// Ported from tests/js/modal_contract.mjs — cap-text losslessness and the
// identity card's escaping/caps/status contracts, now rendered through the
// real React IdentityModal (JSX auto-escaping replaces modal.js's esc()).
import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
import {
  normalizeConnectionEndpoints, normalizeEdgeCapText, capsText,
  identityChannels,
} from '../src/modalShared.js'
import IdentityModal from '../src/components/modals/IdentityModal.jsx'
import CreateAgentModal from '../src/components/modals/CreateAgentModal.jsx'
import CreateWebhookModal from '../src/components/modals/CreateWebhookModal.jsx'
import EditEdgeModal from '../src/components/modals/EditEdgeModal.jsx'
import WebhookModal from '../src/components/modals/WebhookModal.jsx'

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

describe('webhook route normalization', () => {
  const nodes = [
    { name: 'triage', kind: 'agent' },
    { name: 'github_issues', kind: 'webhook' },
    { name: 'pager', kind: 'webhook' },
  ]

  it('normalizes either drag direction to webhook → agent', () => {
    expect(normalizeConnectionEndpoints(
      'github_issues', 'triage', nodes)).toEqual({
      source: 'github_issues', target: 'triage', webhookEdge: true,
    })
    expect(normalizeConnectionEndpoints(
      'triage', 'github_issues', nodes)).toEqual({
      source: 'github_issues', target: 'triage', webhookEdge: true,
    })
  })

  it('rejects hook-to-hook routes', () => {
    expect(normalizeConnectionEndpoints(
      'github_issues', 'pager', nodes)).toBe(null)
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

// An environment is the setup that runs in the agent's workspace BEFORE its
// runtime starts, so it is picked at creation like the runtime is. The list is
// server-owned, which makes fetching it a second thing that can fail — and a
// creation form that breaks because a list of optional setup routines did not
// load would be a far worse failure than the missing list.
describe('the create-agent environment select', () => {
  const noop = () => {}
  const snapshot = {
    ok: true,
    default: 'worktree',
    environments: [
      { name: 'worktree', builtin: true, commands: ['git worktree add -b x'] },
      { name: 'node-deps', builtin: false, commands: ['npm ci'] },
    ],
  }
  const createApi = extra => ({
    environments: vi.fn().mockResolvedValue(snapshot),
    agentCreate: vi.fn().mockResolvedValue({ ok: true, agent: { name: 'leads' } }),
    ...extra,
  })
  const openModal = () => document.getElementById('a-environment')
  const submitNamed = name => {
    fireEvent.change(document.getElementById('a-name'), { target: { value: name } })
    fireEvent.click(document.getElementById('a-go'))
  }
  const created = api => api.agentCreate.mock.calls[0][0]

  it('lists none plus every environment and passes the pick through create', async () => {
    const api = createApi()
    const view = render(<CreateAgentModal api={api} toast={noop}
      refresh={noop} onClose={noop} />)

    await waitFor(() => expect(openModal().options.length,
      'the fetched environments never reached the select').toBe(3))
    expect([...openModal().options].map(o => o.textContent),
      'none must be offered — an agent needs no environment').toEqual(
      ['none', 'worktree', 'node-deps'])

    fireEvent.change(openModal(), { target: { value: 'node-deps' } })
    submitNamed('leads')

    await waitFor(() => expect(created(api).environment,
      'the chosen environment never reached the create call').toBe('node-deps'))
    view.unmount()
  })

  it('omits the field entirely when none is chosen', async () => {
    const api = createApi()
    const view = render(<CreateAgentModal api={api} toast={noop}
      refresh={noop} onClose={noop} />)
    await waitFor(() => expect(openModal().options.length).toBe(3))

    submitNamed('leads')

    // Sending '' would ask the server to spawn into a nameless environment;
    // omitting it is what lets the crew-wide default apply.
    await waitFor(() => expect(api.agentCreate).toHaveBeenCalled())
    expect(created(api).environment,
      'an unpicked environment must not be sent at all').toBe(undefined)
    view.unmount()
  })

  it('still creates agents when the environment list cannot be fetched', async () => {
    const api = createApi({
      environments: vi.fn().mockRejectedValue(new Error('network down')),
    })
    const view = render(<CreateAgentModal api={api} toast={noop}
      refresh={noop} onClose={noop} />)

    await waitFor(() => expect(api.environments).toHaveBeenCalled())
    expect(openModal(), 'the select vanished with the failed fetch').toBeTruthy()
    expect(openModal().options.length,
      'a failed fetch should leave none as the only choice').toBe(1)

    submitNamed('leads')

    await waitFor(() => expect(api.agentCreate).toHaveBeenCalled())
    expect(created(api).name, 'creation broke because a list failed to load')
      .toBe('leads')
    expect(created(api).environment).toBe(undefined)
    view.unmount()
  })
})

describe('webhook node controls', () => {
  const noop = () => {}

  it('submits name, description, and template through the webhook API', async () => {
    const api = {
      webhookCreate: vi.fn().mockResolvedValue({
        ok: true, webhook: { _guid: 'hook-guid' },
      }),
    }
    const closes = []
    const refreshes = []
    const view = render(<CreateWebhookModal api={api} toast={noop}
      refresh={() => refreshes.push(1)} onClose={() => closes.push(1)} />)

    fireEvent.change(document.getElementById('w-name'),
      { target: { value: 'github_issues' } })
    fireEvent.change(document.getElementById('w-description'),
      { target: { value: 'GitHub issue events' } })
    fireEvent.change(document.getElementById('w-template'),
      { target: { value: 'Issue {{ payload.issue.title }}' } })
    fireEvent.click(document.getElementById('w-go'))

    await waitFor(() => expect(api.webhookCreate).toHaveBeenCalledWith({
      name: 'github_issues',
      description: 'GitHub issue events',
      template: 'Issue {{ payload.issue.title }}',
    }))
    expect(closes).toEqual([1])
    expect(refreshes).toEqual([1])
    view.unmount()
  })

  it('shows an absolute secret URL and replaces it after rotation', async () => {
    const initial = {
      _guid: 'hook-guid', name: 'github_issues', kind: 'webhook',
      role: 'GitHub issue events',
      webhook_template: 'Issue {{ payload.issue.title }}',
      public_url: '/hooks/old-secret',
      webhook_last_called_at: 0,
      webhook_last_status: '',
    }
    const rotated = {
      ...initial,
      public_url: '/hooks/new-secret',
    }
    const api = {
      webhookRotate: vi.fn().mockResolvedValue({
        ok: true, webhook: rotated,
      }),
    }
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const view = render(<WebhookModal api={api} toast={noop}
      refresh={noop} onClose={noop} webhook={initial} />)

    expect(document.getElementById('w-url').value).toBe(
      new URL('/hooks/old-secret', window.location.origin).href)
    expect(document.getElementById('w-template').value).toBe(
      'Issue {{ payload.issue.title }}')
    fireEvent.click(document.getElementById('w-rotate'))

    await waitFor(() => expect(document.getElementById('w-url').value).toBe(
      new URL('/hooks/new-secret', window.location.origin).href))
    expect(api.webhookRotate).toHaveBeenCalledWith('hook-guid')
    confirm.mockRestore()
    view.unmount()
  })

  it('submits a human-only transform path when editing a hook route', async () => {
    const api = {
      edgeUpdate: vi.fn().mockResolvedValue({
        ok: true, edge: { _guid: 'edge-guid' },
      }),
    }
    const edge = {
      _guid: 'edge-guid',
      source_name: 'github_issues',
      target_name: 'triage',
      source_kind: 'webhook',
      directed: true,
      transform: '',
    }
    const view = render(<EditEdgeModal api={api} toast={noop}
      refresh={noop} onClose={noop} edge={edge} />)

    fireEvent.change(document.getElementById('e-transform'), {
      target: { value: 'var/transforms/normalize.py' },
    })
    fireEvent.click(document.getElementById('e-save'))

    await waitFor(() => expect(api.edgeUpdate).toHaveBeenCalledWith(
      expect.objectContaining({
        guid: 'edge-guid',
        transform: 'var/transforms/normalize.py',
        directed: true,
      }),
    ))
    view.unmount()
  })
})
