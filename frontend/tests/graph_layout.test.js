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
})
