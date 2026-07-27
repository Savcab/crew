// Goal badges on the graph card. `crew.harness` reads what an agent's coding
// harness knows; the snapshot carries it here. The contracts:
//   (1) an open goal gets a badge, with the reading in the title and the
//       accessible name — the graph must answer "who is working toward what?"
//       without opening a terminal;
//   (2) an agent with no open goal gets no badge, so the badge stays a signal;
//   (3) a runtime whose harness Crew cannot read is never silently blank —
//       "no goal" and "no reading" are different claims;
//   (4) badge text is escaped and bounded: goal text is operator content
//       arriving from a file Crew does not control.
import { describe, it, expect, vi, beforeAll, afterAll } from 'vitest'

const frames = []
let renderGraph

beforeAll(async () => {
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
  const backing = new Map()
  vi.stubGlobal('localStorage', {
    getItem: k => (backing.has(k) ? backing.get(k) : null),
    setItem: (k, v) => backing.set(k, String(v)),
    removeItem: k => backing.delete(k),
    clear: () => backing.clear(),
  })
  localStorage.clear()
  ;({ renderGraph } = await import('../src/graphEngine.js'))
})
afterAll(() => { vi.useRealTimers() })

function agent(name, harness) {
  return {
    name, runtime: 'claude', role: 'badge fixture',
    session_alive: true, runtime_alive: true, live_status: 'idle',
    ...(harness === undefined ? {} : { harness }),
  }
}

const nodeElement = name =>
  document.querySelector(`.cnode.agent[data-sess="${name}"]`)
const badge = name => nodeElement(name).querySelector('.harness-badge.goal')

function render(workspace, agents) {
  renderGraph({ workspace_key: workspace, agents, edges: [] }, {})
}

describe('harness goal badges', () => {
  it('badges a goal, carrying the reading into the tooltip', () => {
    render('badges', [agent('has_goal', {
      supported: true, goal: 'rebuild the search index', goal_count: 1,
    })])

    const goal = badge('has_goal')
    expect(goal, 'an agent with a goal must be badged').toBeTruthy()
    expect(goal.getAttribute('title')).toContain('rebuild the search index')
  })

  // Regression: the badge first shipped inside `.nm`, which is a flex row.
  // Flex items shrink, so in a real browser each pill collapsed to its glyph
  // and the reading vanished — invisible to jsdom, which does no layout. Pin
  // the structure instead: badges live on their own row, never in the name row.
  it('puts the badge on its own row, not inside the flex name row', () => {
    render('structure', [agent('rowed', {
      supported: true, goal: 'a goal long enough to be squeezed', goal_count: 1,
    })])

    const node = nodeElement('rowed')
    expect(node.querySelector('.nm .harness-badge'),
      'a badge inside the flex name row collapses in a real browser').toBeNull()
    const row = node.querySelector('.harness-row')
    expect(row).toBeTruthy()
    expect(row.querySelectorAll('.harness-badge').length).toBe(1)
  })

  it('names the goal in the card’s accessible label', () => {
    render('aria', [agent('described', {
      supported: true, goal: 'ship the migration', goal_count: 1,
    })])

    expect(nodeElement('described').getAttribute('aria-label'))
      .toContain('ship the migration')
  })

  it('shows no badge for an agent with no open goal', () => {
    render('empty', [agent('has_none', {
      supported: true, goal: '', goal_count: 0,
    })])

    expect(badge('has_none')).toBeNull()
  })

  it('counts the REMAINING goals rather than listing them', () => {
    render('counts', [agent('busy', {
      supported: true, goal: 'first of several', goal_count: 4,
    })])

    // One reading is shown; the suffix counts what is NOT shown, so a card
    // with 4 open goals reads "first of several +3".
    expect(badge('busy').textContent).toContain('first of several')
    expect(badge('busy').textContent).toContain('+3')
  })

  it('omits the suffix when the shown reading is the only one', () => {
    render('single', [agent('lone', {
      supported: true, goal: 'the only goal', goal_count: 1,
    })])

    expect(badge('lone').textContent).not.toContain('+')
  })

  it('never claims “no goal” for a harness it cannot read', () => {
    render('unsupported', [agent('codex_worker', {
      supported: false, goal: '', goal_count: 0,
    })])

    expect(badge('codex_worker')).toBeNull()
    expect(nodeElement('codex_worker').classList.contains('harness-unknown'))
      .toBe(true)
  })

  it('treats a missing harness reading as unknown, not as absent work', () => {
    render('missing', [agent('no_reading')])

    expect(badge('no_reading')).toBeNull()
    expect(nodeElement('no_reading').classList.contains('harness-unknown'))
      .toBe(true)
  })

  it('escapes goal text rather than rendering it as markup', () => {
    render('escaping', [agent('injected', {
      supported: true, goal: '<img src=x onerror="alert(1)">', goal_count: 1,
    })])

    const node = nodeElement('injected')
    expect(node.querySelector('img'), 'goal text became live markup').toBeNull()
    expect(badge('injected').getAttribute('title')).toContain('<img src=x')
  })

  it('bounds a runaway reading so one card cannot swallow the graph', () => {
    render('bounds', [agent('verbose', {
      supported: true, goal: 'g'.repeat(400), goal_count: 1,
    })])

    expect(badge('verbose').textContent.length).toBeLessThan(80)
  })

  it('drops the badge when the reading goes away on a later poll', () => {
    render('poll', [agent('changer', {
      supported: true, goal: 'transient goal', goal_count: 1,
    })])
    expect(badge('changer')).toBeTruthy()

    render('poll', [agent('changer', {
      supported: true, goal: '', goal_count: 0,
    })])

    expect(badge('changer')).toBeNull()
  })

  it('does not badge a webhook node, which runs no harness', () => {
    render('hooks', [{
      _guid: 'hook-guid', name: 'github_issues', kind: 'webhook',
      public_url: 'https://example.test/hooks/x', live_status: 'listening',
    }])

    const hook = nodeElement('github_issues')
    expect(hook.querySelector('.harness-badge')).toBeNull()
    expect(hook.classList.contains('harness-unknown')).toBe(false)
  })
})
