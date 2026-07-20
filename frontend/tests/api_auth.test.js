// Ported from tests/js/api_auth.mjs — the capability-fragment auth contract:
// a cap-free load never bootstraps; a later #cap fragment (CLI handoff into an
// already-open tab) bootstraps BEFORE the next API call, with the CSRF header,
// and is erased from the address bar.
import { describe, it, expect, vi } from 'vitest'

describe('api auth contract', () => {
  it('exchanges a late capability fragment before the next API call', async () => {
    history.replaceState(null, '', '/?view=graph')
    const calls = []
    vi.stubGlobal('fetch', async (url, options = {}) => {
      calls.push(['fetch', url, options])
      if (url === '/api/auth/bootstrap') {
        return { ok: true, status: 200, json: async () => ({ ok: true }) }
      }
      if (url === '/api/graph/snapshot') {
        return {
          ok: true, status: 200,
          json: async () => ({ ok: true, agents: [], edges: [] }),
        }
      }
      throw new Error(`unexpected fetch ${url}`)
    })
    const replaceSpy = vi.spyOn(history, 'replaceState')

    const { api } = await import('../src/api.js')
    expect(calls.length, 'a capability-free initial load must not bootstrap').toBe(0)

    window.location.hash = '#cap=fresh-local-capability'
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    await api.graphSnapshot()

    const fetches = calls.filter(call => call[0] === 'fetch')
    expect(fetches.map(call => call[1])).toEqual([
      '/api/auth/bootstrap', '/api/graph/snapshot',
    ])
    expect(JSON.parse(fetches[0][2].body).capability).toBe('fresh-local-capability')
    expect(fetches[0][2].headers).toEqual({
      'Content-Type': 'application/json', 'X-Crew-CSRF': '1',
    })
    expect(replaceSpy).toHaveBeenCalledWith(null, '', '/?view=graph')
    expect(window.location.hash).toBe('')
  })
})
