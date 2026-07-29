// Ported from tests/js/graph_layout.mjs — the graph engine's layout/click
// contracts, now against real jsdom DOM: (1) an immovable pinned/pinned
// collision must not animate forever, (2) collision avoidance still separates
// a movable node, (3) single-click opens the dock only after the double-click
// window, (4+5) double-click unpin never also opens the terminal.
import { describe, it, expect, vi, beforeAll, afterAll } from 'vitest'

const frames = []
let renderGraph

beforeAll(async () => {
  // The engine's bare `requestAnimationFrame` may resolve through either the
  // jsdom window or the vitest global copy depending on transform — replace it
  // in every scope the module could see, BEFORE the module is imported.
  // Fake ONLY setTimeout/clearTimeout: the default useFakeTimers() also fakes
  // requestAnimationFrame, which would silently replace the manual frame queue.
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  let frameId = 0
  const rafStub = cb => { frames.push(cb); return ++frameId }
  vi.stubGlobal('requestAnimationFrame', rafStub)
  window.requestAnimationFrame = rafStub
  globalThis.requestAnimationFrame = rafStub
  document.body.innerHTML = `
    <span id="cgraph-meta"></span>
    <div id="graphKeyboardStatus"></div>
    <div id="cgraph"></div>`
  // Node 25 ships a bare-bones global localStorage that shadows jsdom's; give
  // the engine and the test one full in-memory Storage they both see.
  const backing = new Map()
  vi.stubGlobal('localStorage', {
    getItem: k => (backing.has(k) ? backing.get(k) : null),
    setItem: (k, v) => backing.set(k, String(v)),
    removeItem: k => backing.delete(k),
    clear: () => backing.clear(),
  })
  localStorage.clear()
  localStorage.setItem('crew.pos.v1', JSON.stringify({
    pinned_a: { x: 200, y: 200, pinned: true },
    pinned_b: { x: 200, y: 200, pinned: true },
  }))
  ;({ renderGraph } = await import('../src/graphEngine.js'))
})
afterAll(() => { vi.useRealTimers() })

function agent(name) {
  return {
    name, runtime: 'custom', role: 'layout fixture',
    session_alive: false, runtime_alive: false, live_status: 'not_started',
  }
}
function runFrame() {
  const callback = frames.shift()
  expect(callback, 'graph did not schedule its initial layout frame').toBeTruthy()
  callback()
}
const nodeElement = name =>
  document.querySelector(`.cnode.agent[data-sess="${name}"]`)

describe('graph layout contracts', () => {
  it('does not animate an immovable pinned collision forever', () => {
    renderGraph({
      workspace_key: 'crew',
      agents: [agent('pinned_a'), agent('pinned_b')],
      edges: [],
    }, {})
    let framesRun = 0
    while (frames.length && framesRun < 12) { runFrame(); framesRun += 1 }
    expect(frames.length,
      'an immovable pinned collision kept scheduling animation frames').toBe(0)
    expect(nodeElement('pinned_a').style.left).toBe('200px')
    expect(nodeElement('pinned_b').style.left).toBe('200px')
  })

  it('still separates a movable node from a pinned one', () => {
    localStorage.setItem('crew.pos.v1.movable', JSON.stringify({
      anchor: { x: 200, y: 200, pinned: true },
      free: { x: 220, y: 200, pinned: false },
    }))
    renderGraph({
      workspace_key: 'movable',
      agents: [agent('anchor'), agent('free')],
      edges: [],
    }, {})
    runFrame()
    expect(nodeElement('anchor').style.left).toBe('200px')
    expect(nodeElement('free').style.left).not.toBe('220px')
  })

  it('lights up an edge that just carried a message and tooltips the latest', () => {
    const now = Date.now() / 1000
    renderGraph({
      workspace_key: 'edges',
      agents: [agent('talk_a'), agent('talk_b'), agent('talk_c')],
      edges: [
        { _guid: 'e1', source_name: 'talk_a', target_name: 'talk_b',
          directed: true, conditions: ['when ready'],
          last_message: { at: now - 1, from: 'talk_a', to: 'talk_b',
            status: 'delivered', preview: 'the plan is ready' } },
        { _guid: 'e2', source_name: 'talk_b', target_name: 'talk_c',
          directed: true, conditions: ['later'],
          last_message: { at: now - 600, from: 'talk_b', to: 'talk_c',
            status: 'queued', preview: 'old news' } },
      ],
    }, {})
    const lines = [...document.querySelectorAll('.cedge')]
    expect(lines.length).toBe(2)
    const tipOf = l => l.querySelector('title').textContent
    const fresh = lines.find(l => tipOf(l).includes('the plan is ready'))
    const stale = lines.find(l => tipOf(l).includes('old news'))
    expect(fresh.classList.contains('talking'),
      'a just-flowed message must light the cable').toBe(true)
    expect(stale.classList.contains('talking'),
      'a 10-minute-old message must not glow').toBe(false)
    expect(tipOf(fresh)).toContain('just now')
    expect(tipOf(fresh)).toContain('talk_a → talk_b')
    expect(tipOf(stale)).toContain('10m ago')
    expect(tipOf(stale)).toContain('(queued)')
  })

  it('opens the dock on single click, only after the double-click window', () => {
    const docked = []
    renderGraph({
      workspace_key: 'clicks',
      agents: [agent('click_agent')],
      edges: [],
    }, { onDockAgent: value => docked.push(value.name) })
    frames.length = 0
    const clickNode = nodeElement('click_agent')
    const down = () => clickNode.dispatchEvent(new MouseEvent('mousedown',
      { button: 0, clientX: 200, clientY: 200, bubbles: true, cancelable: true }))
    const up = () => window.dispatchEvent(new MouseEvent('mouseup',
      { button: 0, clientX: 200, clientY: 200 }))

    down(); up()
    expect(docked, 'single click opened before its double-click window').toEqual([])
    vi.runAllTimers()
    expect(docked, 'single click no longer opens the dock').toEqual(['click_agent'])

    down(); up(); down(); up()
    const canvas = document.querySelector('.gcanvas')
    canvas.ondblclick({ target: clickNode })
    vi.runAllTimers()
    expect(docked,
      'double-click unpin also opened the agent terminal').toEqual(['click_agent'])
  })

  it('badges live subagents, and only on a real reading above zero', () => {
    renderGraph({
      workspace_key: 'subagents',
      agents: [
        { ...agent('sub_many'), subagents: 2 },
        { ...agent('sub_one'), subagents: 1 },
        { ...agent('sub_none'), subagents: 0 },
        agent('sub_unread'),
      ],
      webhooks: [{
        _guid: 'sub-hook-guid', name: 'sub_hook', kind: 'webhook',
        role: 'no harness of its own', subagents: 3,
      }],
      edges: [],
    }, {})
    frames.length = 0
    const badge = name =>
      nodeElement(name).querySelector('.nm .subagent-badge')

    expect(badge('sub_many').textContent).toBe('⑂ 2 sub')
    expect(badge('sub_many').getAttribute('title'))
      .toBe("2 live subagents run by this agent's harness")
    expect(badge('sub_one').getAttribute('title'))
      .toBe("1 live subagent run by this agent's harness")
    expect(badge('sub_none'),
      'an honest zero must not badge').toBe(null)
    expect(badge('sub_unread'),
      'no reading is not a claim of none — it must not badge').toBe(null)
    expect(badge('sub_hook'),
      'a webhook runs no harness, so it can have no subagents').toBe(null)
  })

  it('badges scheduled cron loops, and only on a real reading above zero', () => {
    renderGraph({
      workspace_key: 'cron-loops',
      agents: [
        { ...agent('cron_many'), cron_loops: 2 },
        { ...agent('cron_one'), cron_loops: 1 },
        { ...agent('cron_none'), cron_loops: 0 },
        agent('cron_unread'),
        { ...agent('cron_and_sub'), cron_loops: 1, subagents: 3 },
      ],
      webhooks: [{
        _guid: 'cron-hook-guid', name: 'cron_hook', kind: 'webhook',
        role: 'no harness of its own', cron_loops: 4,
      }],
      edges: [],
    }, {})
    frames.length = 0
    const badge = name =>
      nodeElement(name).querySelector('.nm .cron-badge')

    expect(badge('cron_many').textContent).toBe('⟳ 2 cron')
    expect(badge('cron_many').getAttribute('title'))
      .toBe("2 cron loops scheduled by this agent's harness")
    expect(badge('cron_one').textContent).toBe('⟳ 1 cron')
    expect(badge('cron_one').getAttribute('title'))
      .toBe("1 cron loop scheduled by this agent's harness")
    expect(badge('cron_none'),
      'an honest zero must not badge').toBe(null)
    expect(badge('cron_unread'),
      'no reading is not a claim of none — it must not badge').toBe(null)
    expect(badge('cron_hook'),
      'a webhook runs no harness, so it can schedule no cron loops').toBe(null)

    // the two harness readings are independent — one card can carry both.
    expect(badge('cron_and_sub').textContent).toBe('⟳ 1 cron')
    expect(nodeElement('cron_and_sub').querySelector('.nm .subagent-badge')
      .textContent).toBe('⑂ 3 sub')
  })

  it('renders a webhook as a listening node and opens its configuration', () => {
    const opened = []
    renderGraph({
      workspace_key: 'webhook-node',
      agents: [],
      webhooks: [{
        _guid: 'hook-guid', name: 'github_issues', kind: 'webhook',
        role: 'GitHub issue events', public_url: '/hooks/secret',
      }],
      edges: [],
    }, { onDockAgent: value => opened.push(value) })
    frames.length = 0

    const hook = nodeElement('github_issues')
    expect(hook).toBeTruthy()
    expect(hook.dataset.kind).toBe('webhook')
    expect(hook.classList.contains('webhook')).toBe(true)
    expect(hook.classList.contains('st-listening')).toBe(true)
    expect(hook.textContent).toContain('webhook')
    expect(hook.textContent).toContain('listening')
    expect(document.getElementById('cgraph-meta').textContent).toContain('1 hook')

    hook.dispatchEvent(new MouseEvent('mousedown',
      { button: 0, clientX: 200, clientY: 200, bubbles: true, cancelable: true }))
    window.dispatchEvent(new MouseEvent('mouseup',
      { button: 0, clientX: 200, clientY: 200 }))
    vi.runAllTimers()
    expect(opened).toHaveLength(1)
    expect(opened[0]).toMatchObject({
      _guid: 'hook-guid', name: 'github_issues', kind: 'webhook',
    })
  })
})
