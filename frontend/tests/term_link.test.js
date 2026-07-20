// termLink contract: the graph window's clicks reach the terminal window; a
// missing terminal window is spawned (named target → no duplicates); a
// freshly loaded terminal window receives the last requested agent.
import { describe, it, expect, vi } from 'vitest'
import { graphTermLink, termWindowLink } from '../src/termLink.js'

const flush = () => new Promise(resolve => setTimeout(resolve, 0))

describe('termLink', () => {
  it('spawns the window when none is alive, then hands off by broadcast', async () => {
    const spawned = []
    const graph = graphTermLink({ openWindow: () => spawned.push(1) })

    graph.open('builder')
    await flush()
    expect(spawned.length, 'no live term window → must window.open').toBe(1)

    // The term window loads: it announces readiness and receives the LAST ask.
    const opened = []
    const termBc = termWindowLink({ onOpen: name => opened.push(name) })
    await flush()
    expect(opened, 'term-ready must replay the last requested agent')
      .toEqual(['builder'])
    expect(graph.isAlive()).toBe(true)

    // Subsequent clicks broadcast without spawning again.
    graph.open('sales')
    await flush()
    expect(opened).toEqual(['builder', 'sales'])
    expect(spawned.length).toBe(1)

    termBc.close()
    graph.close()
  })

  it('falls back to spawning again after the term window says goodbye', async () => {
    const spawned = []
    const graph = graphTermLink({ openWindow: () => spawned.push(1) })
    const opened = []
    const termBc = termWindowLink({ onOpen: name => opened.push(name) })
    await flush()

    graph.open('leads')
    await flush()
    expect(spawned.length, 'alive term window → no spawn').toBe(0)

    // Simulate the term window closing (pagehide broadcast).
    termBc.postMessage({ type: 'term-bye' })
    termBc.close()
    await flush()
    graph.open('leads')
    await flush()
    expect(spawned.length, 'dead term window → spawn again').toBe(1)
    graph.close()
  })
})
