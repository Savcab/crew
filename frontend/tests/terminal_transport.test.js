// Ported from tests/js/terminal_transport.mjs — the PTY transport + dock
// controller contracts: stale-stream isolation, ordered/chunked input, and the
// dock's GUID-bound start/sync/focus semantics, against real jsdom DOM.
import { describe, it, expect, vi, beforeAll } from 'vitest'

class FakeTerminal {
  constructor() {
    this.cols = 80
    this.rows = 24
    this.buffer = { active: { viewportY: 0, baseY: 0 } }
    this.writes = []
  }
  loadAddon(addon) { this.addon = addon }
  onData(fn) { this.dataHandler = fn }
  onBinary(fn) { this.binaryHandler = fn }
  attachCustomKeyEventHandler(fn) { this.keyHandler = fn }
  open(host) {
    this.host = host
    this.textarea = document.createElement('textarea')
    host.appendChild(this.textarea)
  }
  reset() { this.resetCount = (this.resetCount || 0) + 1 }
  write(bytes, callback) { this.writes.push(Uint8Array.from(bytes)); if (callback) callback() }
  scrollToBottom() { this.scrolled = true }
  focus() { this.focused = true; if (this.textarea) this.textarea.focus() }
  blur() { this.focused = false; if (this.textarea) this.textarea.blur() }
  dispose() { this.disposed = true }
}
class FakeFitAddon { fit() {} }

class FakeEventSource {
  static instances = []
  constructor(url) {
    this.url = url
    this.listeners = new Map()
    FakeEventSource.instances.push(this)
  }
  addEventListener(type, fn) { this.listeners.set(type, fn) }
  emit(type, data) {
    const fn = this.listeners.get(type)
    if (fn) fn({ data })
  }
  close() { this.closed = true }
}

const b64 = s => btoa(s)
const fromB64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0))
const text = u8 => new TextDecoder().decode(Uint8Array.from(u8))

let api, TerminalPane, createDock

beforeAll(async () => {
  document.body.innerHTML = `
    <button id="addAgentBtn"></button>
    <div id="crew">
      <div id="dock">
        <div id="dockResize"></div>
        <div id="dock-head">
          <span id="dockDot"></span><span id="dockName"></span>
          <span id="dockMeta"></span>
          <button id="dockIdentity"></button>
          <button id="dockStart" style="display:none"></button>
          <button id="dockPrev"></button><button id="dockNext"></button>
          <button id="dockMax"></button><button id="dockClose"></button>
        </div>
        <div id="dockPanes"><div id="dockTerm"></div></div>
      </div>
    </div>
    <button id="graph-agent-a"></button>`
  document.getElementById('crew').getBoundingClientRect =
    () => ({ top: 0, bottom: 600, height: 600, width: 800 })
  document.getElementById('dock').getBoundingClientRect =
    () => ({ top: 300, bottom: 600, height: 300, width: 800 })
  window.Terminal = FakeTerminal
  window.FitAddon = { FitAddon: FakeFitAddon }
  vi.stubGlobal('EventSource', FakeEventSource)
  ;({ api } = await import('../src/api.js'))
  ;({ TerminalPane } = await import('../src/term.js'))
  ;({ createDock } = await import('../src/dockCore.js'))
})

describe('TerminalPane transport contracts', () => {
  it('isolates streams, orders input, chunks oversized pastes', async () => {
    // A closed EventSource can still have already-queued callbacks. Those must
    // not overwrite the new PTY id or paint stale bytes after switching agents.
    const resizeCalls = []
    api.ptyResize = (...args) => { resizeCalls.push(args); return Promise.resolve({ ok: true }) }
    api.ptyInput = () => Promise.resolve({ ok: true })
    const pane = new TerminalPane()
    pane.attach(document.getElementById('dockTerm'))
    pane.open('first-session')
    const firstSource = FakeEventSource.instances.at(-1)
    pane.open('second-session')
    const secondSource = FakeEventSource.instances.at(-1)
    expect(firstSource.closed).toBe(true)
    firstSource.emit('id', 'old-pty')
    firstSource.emit('data', b64('stale'))
    expect(pane.ptyId, 'a stale id event replaced the new stream state').toBe(null)
    expect(pane.term.writes.length, 'stale output painted after stream switch').toBe(0)
    secondSource.emit('id', 'new-pty')
    secondSource.emit('data', b64('fresh'))
    expect(pane.ptyId).toBe('new-pty')
    expect(text(pane.term.writes[0])).toBe('fresh')

    // Input POSTs are a threaded HTTP boundary: ordered, never jumping streams.
    const inputCalls = []
    let releaseFirstInput
    api.ptyInput = (id, payload) => {
      inputCalls.push([id, text(fromB64(payload))])
      if (inputCalls.length === 1) {
        return new Promise(resolve => { releaseFirstInput = resolve })
      }
      return Promise.resolve({ ok: true })
    }
    pane.term.dataHandler('a')
    pane.term.dataHandler('b')
    await Promise.resolve()
    await Promise.resolve()
    expect(inputCalls, 'input requests were concurrent').toEqual([['new-pty', 'a']])
    releaseFirstInput({ ok: true })
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(inputCalls).toEqual([['new-pty', 'a'], ['new-pty', 'b']])

    // A browser paste may exceed the API's decoded-body ceiling: bounded
    // sequential requests, every byte preserved.
    await pane._inputTail
    const pasteChunks = []
    api.ptyInput = (id, payload) => {
      pasteChunks.push(fromB64(payload))
      return Promise.resolve({ ok: true })
    }
    const largePaste = new Uint8Array(300 * 1024)
    for (let i = 0; i < largePaste.length; i += 1) largePaste[i] = i % 251
    pane._toPty(largePaste)
    await pane._inputTail
    expect(pasteChunks.length, 'oversized paste was sent as one rejected POST').toBeGreaterThan(1)
    expect(pasteChunks.every(chunk => chunk.length <= 64 * 1024)).toBe(true)
    const joined = new Uint8Array(pasteChunks.reduce((n, c) => n + c.length, 0))
    let at = 0
    for (const chunk of pasteChunks) { joined.set(chunk, at); at += chunk.length }
    expect(joined).toEqual(largePaste)

    pane.dispose()
    expect(pane.ptyId, 'dispose retained a writable server-side PTY id').toBe(null)
  })
})

describe('dock controller contracts', () => {
  class FakePane {
    constructor() { this.opens = [] }
    attach(host) { this.host = host; return this }
    open(target) { this.opens.push(target); return this }
    setLive(on) { this.live = on; return this }
    fit() { this.fitCount = (this.fitCount || 0) + 1; return this }
  }

  it('keeps start/sync/focus bound to the right worker generation', async () => {
    const workers = []
    const toasts = []
    let resolveStart
    const dockApi = {
      agentStart: () => new Promise(resolve => { resolveStart = resolve }),
    }
    const dockController = createDock({
      TerminalPane: FakePane,
      api: dockApi,
      getWorkers: () => workers,
      onDockChange: () => {},
      onShowIdentity: () => {},
      toast: (...args) => toasts.push(args),
    })
    const dockPane = window.__dock.claudePane
    const meta = () => document.getElementById('dockMeta').textContent

    // Opening a known-down agent must not start EventSource's 404 retry loop.
    const opener = document.getElementById('graph-agent-a')
    opener.focus()
    const agentA = {
      name: 'agent-a', session: 'session-a', runtime: 'claude',
      session_alive: false, runtime_alive: false, live_status: 'down',
    }
    dockController.openDock(agentA)
    expect(dockPane.opens.at(-1), 'down agent opened a retrying PTY stream').toBe(null)
    expect(meta(),
      'dock exposed the raw runtime status "down" instead of the operator label')
      .toBe('claude · session down')

    // A delayed start response belongs to the worker that initiated it.
    const startPromise = document.getElementById('dockStart').onclick()
    const agentB = {
      name: 'agent-b', session: 'session-b', runtime: 'claude',
      session_alive: true, runtime_alive: true, live_status: 'idle',
    }
    dockController.openDock(agentB)
    const bOpensBefore = dockPane.opens.filter(value => value === 'session-b').length
    resolveStart({ ok: true })
    await startPromise
    expect(dockController.dockedWorker().name).toBe('agent-b')
    expect(dockPane.opens.filter(value => value === 'session-b').length,
      'agent A start completion re-opened agent B').toBe(bOpensBefore)
    expect(toasts.at(-1)[0]).toBe('starting agent-a…')

    // An in-flight Start response stays bound to the immutable row GUID and
    // never relabels/reattaches a same-name replacement.
    const oldGeneration = {
      _guid: 'old-generation', name: 'agent-reused', session: 'session-reused',
      runtime: 'claude', session_alive: false, runtime_alive: false,
      live_status: 'down', role: 'old generation',
    }
    dockController.openDock(oldGeneration)
    const oldGenerationStart = document.getElementById('dockStart').onclick()
    const newGeneration = {
      ...oldGeneration,
      _guid: 'new-generation', session_alive: true, runtime_alive: true,
      live_status: 'idle', role: 'replacement generation',
    }
    dockController.openDock(newGeneration)
    const replacementOpensBefore = dockPane.opens.filter(
      value => value === 'session-reused').length
    resolveStart({ ok: true })
    await oldGenerationStart
    expect(dockController.dockedWorker()._guid).toBe('new-generation')
    expect(meta(), 'old Start completion relabeled the same-name replacement')
      .toBe('replacement generation · claude · idle')
    expect(dockPane.opens.filter(value => value === 'session-reused').length,
      'old Start completion reattached the same-name replacement')
      .toBe(replacementOpensBefore)

    // Status labels: operator vocabulary, never raw machine statuses.
    dockController.openDock({
      name: 'agent-crashed', session: 'session-crashed', runtime: 'claude',
      session_alive: true, runtime_alive: false, live_status: 'down',
    })
    expect(meta(), 'dock mislabeled a crashed runtime as a missing tmux session')
      .toBe('claude · runtime down')
    dockController.openDock({
      name: 'agent-not-started', runtime: 'claude', session_alive: true,
      runtime_alive: false, live_status: 'not_started',
    })
    expect(meta()).toBe('claude · runtime not started')
    const customUnknown = {
      name: 'agent-custom', runtime: 'custom', session_alive: true,
      runtime_alive: true, live_status: 'unknown',
    }
    dockController.openDock(customUnknown)
    expect(meta()).toBe('custom · state unknown')
    dockController.openDock({
      name: 'agent-custom-exited', runtime: 'custom', session_alive: true,
      runtime_alive: false, live_status: 'unknown',
    })
    expect(document.getElementById('dockStart').style.display,
      'dock hid Start after the configured custom runtime process exited')
      .not.toBe('none')
    dockController.openDock(customUnknown)

    // Snapshot polling must refresh an already-open dock's header.
    workers.push({
      ...customUnknown,
      role: 'runtime fixture', runtime_alive: true, live_status: 'unknown',
    })
    dockController.syncDockedWorker()
    expect(meta(), 'open dock did not synchronize the latest worker snapshot')
      .toBe('runtime fixture · custom · state unknown')

    // Ctrl+Esc detaches to a real dock control; closing returns focus to the
    // graph control that opened the dock.
    dockController.detach()
    expect(document.activeElement).toBe(document.getElementById('dockClose'))
    dockController.closeDock()
    expect(document.activeElement).toBe(opener)

    // Polling must not silently adopt a same-name replacement row.
    const syncOld = {
      _guid: 'sync-old', name: 'agent-sync-reused', session: 'sync-session',
      runtime: 'claude', session_alive: true, runtime_alive: true,
      live_status: 'idle',
    }
    dockController.openDock(syncOld)
    workers.splice(0, workers.length, { ...syncOld, _guid: 'sync-new' })
    dockController.syncDockedWorker()
    expect(dockController.dockedWorker(),
      'snapshot sync silently adopted a same-name replacement GUID').toBe(null)

    // Resize handle semantics + the keyboard path: ArrowUp grows one step.
    expect(document.getElementById('dock').getAttribute('role')).toBe('region')
    expect(document.getElementById('dockResize').getAttribute('role')).toBe('separator')
    expect(document.getElementById('dockResize').getAttribute('tabindex')).toBe('0')
    const arrowUp = new KeyboardEvent('keydown', { key: 'ArrowUp', cancelable: true })
    document.getElementById('dockResize').dispatchEvent(arrowUp)
    expect(arrowUp.defaultPrevented).toBe(true)
    expect(document.getElementById('dock').style.height).toBe('324px')
    expect(document.getElementById('dockResize').getAttribute('aria-valuenow')).toBe('324')
  })
})
