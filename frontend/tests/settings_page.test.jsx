// SettingsPage — the crew-wide launch commands, now a full page whose controls
// are SELECTs of server-published choices. The contracts worth pinning are the
// ones an operator would notice being wrong: that no row can be typed into,
// that a row shows which of the published choices is actually in force (and
// says so when an environment variable is quietly outranking the stored one),
// that Save writes ONLY the rows that changed — sending blank for the default
// choice, which is what clears an override — and that a refused write leaves
// the page (and the operator's unsaved pick) standing.
//
// This vitest project has no globals and no setup file, so testing-library's
// automatic cleanup never runs — without the afterEach below, an unmounted
// page's controls stay in the document and answer getElementById for the tests
// that follow.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent, waitFor, cleanup } from '@testing-library/react'
import SettingsPage from '../src/components/SettingsPage.jsx'
import Header from '../src/components/Header.jsx'

afterEach(cleanup)

// One row whose stored override is one of the published choices, one overridden
// by the environment, one sitting on its default — the three sources the page
// has to tell apart.
const settingRows = () => [
  {
    key: 'claude_launch_cmd', label: 'Claude launch command',
    default: 'claude --dangerously-skip-permissions',
    override: 'claude', effective: 'claude', source: 'settings',
    choices: [
      { label: 'Unattended default', command: 'claude --dangerously-skip-permissions' },
      { label: 'Ask for permissions', command: 'claude' },
      { label: 'Unattended, continue last session', command: 'claude --dangerously-skip-permissions --continue' },
    ],
  },
  {
    key: 'codex_launch_cmd', label: 'Codex launch command',
    default: 'codex', override: null, effective: 'codex exec --full-auto',
    source: 'env',
    choices: [
      { label: 'Unattended default', command: 'codex' },
      { label: 'Sandboxed with approvals', command: 'codex exec --sandbox' },
    ],
  },
  {
    key: 'hermes_launch_cmd', label: 'Hermes launch command',
    default: 'hermes', override: null, effective: 'hermes', source: 'default',
    choices: [
      { label: 'Default', command: 'hermes' },
      { label: 'Auto-approve (yolo)', command: 'hermes --yolo' },
      { label: 'Continue last session', command: 'hermes --continue' },
    ],
  },
]

// A value saved before this key's choices existed (or by an older build): the
// server still reports it as the stored override, but it is in no choice list.
const legacyRows = () => settingRows().map(row => row.key === 'hermes_launch_cmd'
  ? { ...row, override: 'hermes --tui', effective: 'hermes --tui', source: 'settings' }
  : row)

const noop = () => {}
const select = key => document.getElementById(`setting-${key}`)
const options = key => [...select(key).options].map(o => o.textContent)
const note = key => select(key).parentElement.querySelector('.set-note').textContent
const toastText = () => document.getElementById('toast').textContent
const toastFailed = () => document.getElementById('toast').className.includes('err')

const settingsApi = extra => ({
  settings: vi.fn().mockResolvedValue({ ok: true, settings: settingRows() }),
  settingsUpdate: vi.fn().mockResolvedValue({ ok: true, settings: settingRows() }),
  ...extra,
})

// Rows only exist after the api.settings() promise settles.
const open = async api => {
  const view = render(<SettingsPage api={api} />)
  await waitFor(() => expect(document.getElementById('settings-save'),
    'settings rows never rendered').toBeTruthy())
  return view
}

describe('settings page shell', () => {
  it('is a page with a tab sidebar and a way back to the graph', async () => {
    const view = await open(settingsApi())

    const nav = document.getElementById('settings-nav')
    expect(nav, 'the page rendered no sidebar').toBeTruthy()
    const tab = document.getElementById('settings-tab-harnesses')
    expect(tab, 'the Harnesses tab is missing from the sidebar').toBeTruthy()
    expect(tab.className, 'the only tab should be the active one')
      .toContain('active')
    expect(nav.contains(tab),
      'the tab must live in the sidebar, not the main pane').toBe(true)
    // Leaving is the page's replacement for a modal's close button; without it
    // the operator is stranded on a view with no chrome around it.
    const back = document.getElementById('settings-back')
    expect(back, 'no way back to the graph').toBeTruthy()
    expect(back.getAttribute('href')).toBe('/')
    expect(document.getElementById('settings-bar').textContent,
      'the main pane should name the group being edited')
      .toContain('Settings · Harnesses')
    view.unmount()
  })

  it('offers no typable field anywhere — every control is a select', async () => {
    const view = await open(settingsApi())

    for (const row of settingRows()) {
      expect(select(row.key), `no control rendered for ${row.key}`).toBeTruthy()
      expect(select(row.key).tagName,
        `${row.key} must be a select — the server refuses commands that are `
        + 'not one of its choices, so a text box would only invent failures')
        .toBe('SELECT')
    }
    expect(document.querySelectorAll('#settings-body input, #settings-body textarea').length,
      'a free-text control leaked back onto the settings page').toBe(0)
    view.unmount()
  })
})

describe('settings page rows', () => {
  it('lists every published choice and selects the default when nothing is stored', async () => {
    const view = await open(settingsApi())

    expect(options('hermes_launch_cmd'),
      'each option should name the choice and the command it runs').toEqual([
      'Default — hermes',
      'Auto-approve (yolo) — hermes --yolo',
      'Continue last session — hermes --continue',
    ])
    expect(select('hermes_launch_cmd').value,
      'an unset row should sit on its default choice').toBe('hermes')
    expect(select('codex_launch_cmd').value,
      'an env-overridden row still edits the STORED value, which is unset')
      .toBe('codex')
    view.unmount()
  })

  it('selects the stored override when it is one of the choices', async () => {
    const view = await open(settingsApi())

    expect(select('claude_launch_cmd').value,
      'the stored override should be the selected choice').toBe('claude')
    expect(options('claude_launch_cmd').length,
      'a stored choice must not be duplicated as a custom option').toBe(3)
    view.unmount()
  })

  it('keeps a legacy override that matches no choice, and selects it', async () => {
    const api = settingsApi({
      settings: vi.fn().mockResolvedValue({ ok: true, settings: legacyRows() }),
    })
    const view = await open(api)

    // Dropping it silently would rewrite the operator's setting the next time
    // they saved any other row.
    expect(options('hermes_launch_cmd'),
      'the stored custom command was dropped from the select').toEqual([
      'Default — hermes',
      'Auto-approve (yolo) — hermes --yolo',
      'Continue last session — hermes --continue',
      'Stored custom command — hermes --tui',
    ])
    expect(select('hermes_launch_cmd').value,
      'the legacy value is what is in force, so it must be what is selected')
      .toBe('hermes --tui')
    view.unmount()
  })

  it('names the source each effective value actually comes from', async () => {
    const view = await open(settingsApi())

    // The env row is the dangerous one: a saved value that looks applied but
    // is being outranked has to say so, and name the value that really wins.
    const env = note('codex_launch_cmd')
    expect(env, 'env-sourced row must flag the environment override')
      .toMatch(/environment override/i)
    expect(env, 'env-sourced row must say the environment wins')
      .toMatch(/wins over anything saved/i)
    expect(env, 'env-sourced row must show the effective command')
      .toContain('codex exec --full-auto')

    expect(note('claude_launch_cmd'),
      'settings-sourced row should read as a stored override')
      .toContain('stored override')
    expect(note('hermes_launch_cmd'),
      'unset row should read as the default').toContain('default')
    view.unmount()
  })
})

describe('settings page save', () => {
  it('writes only the changed rows, and clears an override by choosing the default', async () => {
    const api = settingsApi()
    const view = await open(api)

    fireEvent.change(select('hermes_launch_cmd'),
      { target: { value: 'hermes --yolo' } })
    fireEvent.change(select('claude_launch_cmd'),
      { target: { value: 'claude --dangerously-skip-permissions' } })
    fireEvent.click(document.getElementById('settings-save'))

    await waitFor(() => expect(toastText(),
      'a clean save should say so').toBe('settings saved'))
    expect(toastFailed(), 'a clean save is not an error').toBe(false)
    // The default choice is stored as the EMPTY value — that is what drops the
    // key back to its default rather than pinning today's default forever.
    expect(api.settingsUpdate.mock.calls,
      'untouched rows must not cost a request, and the default choice clears')
      .toEqual([
        ['claude_launch_cmd', ''],
        ['hermes_launch_cmd', 'hermes --yolo'],
      ])
    expect(document.getElementById('settings-save'),
      'a successful save must leave the operator on the page').toBeTruthy()
    view.unmount()
  })

  it('leaves an untouched page alone', async () => {
    const api = settingsApi()
    const view = await open(api)

    fireEvent.click(document.getElementById('settings-save'))

    await waitFor(() => expect(toastText(),
      'Save on an unchanged page should say nothing changed')
      .toBe('no changes to save'))
    expect(api.settingsUpdate,
      'an unchanged page must not write anything').not.toHaveBeenCalled()
    view.unmount()
  })

  it('re-selecting the value already stored is not a change', async () => {
    const api = settingsApi()
    const view = await open(api)

    fireEvent.change(select('claude_launch_cmd'), { target: { value: 'claude' } })
    fireEvent.click(document.getElementById('settings-save'))

    await waitFor(() => expect(toastText()).toBe('no changes to save'))
    expect(api.settingsUpdate,
      're-picking the stored choice must not cost a request')
      .not.toHaveBeenCalled()
    view.unmount()
  })

  it('repaints the rows from the server answer, not from what was picked', async () => {
    // hermes starts on its default; after the write the server reports it as
    // a stored override, and the row's note has to follow the server.
    const saved = settingRows().map(row => row.key === 'hermes_launch_cmd'
      ? { ...row, override: 'hermes --yolo',
          effective: 'hermes --yolo', source: 'settings' }
      : row)
    const api = settingsApi({
      settingsUpdate: vi.fn().mockResolvedValue({ ok: true, settings: saved }),
    })
    const view = await open(api)

    expect(note('hermes_launch_cmd')).toContain('default')
    fireEvent.change(select('hermes_launch_cmd'),
      { target: { value: 'hermes --yolo' } })
    fireEvent.click(document.getElementById('settings-save'))

    await waitFor(() => expect(toastText()).toBe('settings saved'))
    expect(note('hermes_launch_cmd'),
      'the saved row should now read as a stored override')
      .toContain('stored override')
    view.unmount()
  })

  it('reports a rejected write and keeps the page', async () => {
    const api = settingsApi({
      settingsUpdate: vi.fn().mockResolvedValue({ ok: false, error: 'nope' }),
    })
    const view = await open(api)

    fireEvent.change(select('hermes_launch_cmd'),
      { target: { value: 'hermes --yolo' } })
    fireEvent.click(document.getElementById('settings-save'))

    await waitFor(() => expect(toastText(),
      "a rejected write must surface the server's reason").toBe('nope'))
    expect(toastFailed(), 'a refusal must be reported as an error').toBe(true)
    expect(document.getElementById('settings-save').disabled,
      'Save must be usable again after a failure').toBe(false)
    expect(select('hermes_launch_cmd').value,
      'the refused pick must stay on screen to be fixed or retried')
      .toBe('hermes --yolo')
    view.unmount()
  })

  it('writes several changed rows one at a time', async () => {
    const calls = []
    let releaseFirst
    const api = settingsApi({
      settingsUpdate: (key, value) => {
        calls.push([key, value])
        return calls.length === 1
          ? new Promise(res => {
            releaseFirst = () => res({ ok: true, settings: settingRows() })
          })
          : Promise.resolve({ ok: true, settings: settingRows() })
      },
    })
    const view = await open(api)

    fireEvent.change(select('claude_launch_cmd'),
      { target: { value: 'claude --dangerously-skip-permissions --continue' } })
    fireEvent.change(select('hermes_launch_cmd'),
      { target: { value: 'hermes --continue' } })
    fireEvent.click(document.getElementById('settings-save'))

    await waitFor(() => expect(calls.length).toBe(1))
    // The server takes one key per call and answers with the whole list, so
    // the next write must wait for the previous answer rather than racing it.
    await Promise.resolve()
    expect(calls.length,
      'the second write started before the first came back').toBe(1)
    expect(document.getElementById('settings-save').disabled,
      'Save should be held while writes are in flight').toBe(true)

    releaseFirst()
    await waitFor(() => expect(calls.length).toBe(2))
    expect(calls).toEqual([
      ['claude_launch_cmd', 'claude --dangerously-skip-permissions --continue'],
      ['hermes_launch_cmd', 'hermes --continue'],
    ])
    view.unmount()
  })
})

describe('settings page partial failure', () => {
  // Nothing spans the per-key calls, so a page that rolled its rows back on
  // failure would claim writes were undone that in fact landed.
  const clearedClaude = () => settingRows().map(
    row => row.key === 'claude_launch_cmd'
      ? { ...row, override: null, effective: row.default, source: 'default' }
      : row)

  const twoWrites = async settingsUpdate => {
    const view = await open(settingsApi({ settingsUpdate }))
    fireEvent.change(select('claude_launch_cmd'),
      { target: { value: 'claude --dangerously-skip-permissions' } })
    fireEvent.change(select('hermes_launch_cmd'),
      { target: { value: 'hermes --yolo' } })
    fireEvent.click(document.getElementById('settings-save'))
    return view
  }

  it('keeps the write that already landed when a later one is refused', async () => {
    const view = await twoWrites(vi.fn()
      .mockResolvedValueOnce({ ok: true, settings: clearedClaude() })
      .mockResolvedValueOnce({ ok: false, error: 'hermes: not executable' }))

    await waitFor(() => expect(toastText()).toBe('hermes: not executable'))
    expect(toastFailed()).toBe(true)
    // The claude clear DID land — the row must show the default it fell back
    // to rather than pretending the whole save failed.
    expect(note('claude_launch_cmd'),
      'the successful earlier write was rolled back in the UI')
      .toContain('default')
    // …and the row that never landed must keep the operator's pick, not snap
    // back to a stored value that was never written.
    expect(select('hermes_launch_cmd').value,
      'the unsaved pick was discarded by the repaint').toBe('hermes --yolo')
    view.unmount()
  })

  it('stops at the first failure instead of writing the rest', async () => {
    const settingsUpdate = vi.fn()
      .mockResolvedValueOnce({ ok: false, error: 'claude: not executable' })
      .mockResolvedValue({ ok: true, settings: settingRows() })
    const view = await twoWrites(settingsUpdate)

    await waitFor(() => expect(toastText()).toBe('claude: not executable'))
    expect(settingsUpdate,
      'writes continued past a failure the operator has not seen yet')
      .toHaveBeenCalledTimes(1)
    view.unmount()
  })

  it('treats a thrown write like a refused one', async () => {
    const view = await twoWrites(vi.fn()
      .mockResolvedValueOnce({ ok: true, settings: clearedClaude() })
      .mockRejectedValueOnce(new Error('connection reset')))

    await waitFor(() => expect(toastText()).toBe('connection reset'))
    expect(toastFailed()).toBe(true)
    expect(note('claude_launch_cmd')).toContain('default')
    view.unmount()
  })
})

describe('settings page load failure', () => {
  const failsToLoad = async settings => {
    const view = render(<SettingsPage api={{ settings, settingsUpdate: vi.fn() }} />)
    await waitFor(() => expect(document.getElementById('settings-error'),
      'a failed load rendered no error row').toBeTruthy())
    return view
  }

  it('shows the error instead of an unsavable page', async () => {
    const view = await failsToLoad(
      vi.fn().mockResolvedValue({ ok: false, error: 'boom' }))

    expect(document.getElementById('settings-error').textContent,
      "the error row should quote the server's reason").toContain('boom')
    expect(document.getElementById('settings-save'),
      'Save must not be offered when no settings loaded').toBe(null)
    expect(document.getElementById('setting-claude_launch_cmd'),
      'no controls should render without settings').toBe(null)
    expect(toastText(), 'a failed load should also toast').toBe('boom')
    expect(toastFailed()).toBe(true)
    // The sidebar is page chrome, not row data: losing the settings must not
    // strand the operator without a way back.
    expect(document.getElementById('settings-back'),
      'the way back disappeared with the failed load').toBeTruthy()
    view.unmount()
  })

  it('reports a thrown request the same way as a refused one', async () => {
    const view = await failsToLoad(
      vi.fn().mockRejectedValue(new Error('network down')))

    expect(document.getElementById('settings-error').textContent)
      .toContain('network down')
    expect(document.getElementById('settings-save'),
      'Save must not be offered when the load threw').toBe(null)
    expect(toastText()).toBe('network down')
    view.unmount()
  })
})

describe('the settings entry point in the header', () => {
  it('sits right after the terminal-window toggle and navigates to the page', () => {
    // Settings is a page now, so the button is a navigation like ⌂ graphs —
    // there is no modal for App to open and no onSettings prop to forget.
    const real = window.location
    const stub = { href: '' }
    Object.defineProperty(window, 'location',
      { value: stub, writable: true, configurable: true })
    try {
      const view = render(<Header agents={[]} webhooks={[]} pendingCount={0}
        onOpenPending={noop} onRateChange={noop} termWin={false}
        onTermWinToggle={noop} workspaceKey="crew" projectTitle="" />)

      const button = document.getElementById('settingsBtn')
      expect(button, 'the header rendered no settings button').toBeTruthy()
      expect(button.textContent).toBe('⚙ settings')
      // Position is part of the contract: settings lands next to the other
      // window-level controls, not among the per-agent buttons.
      expect(document.getElementById('termWinToggle').nextElementSibling,
        'the settings button moved away from the terminal-window toggle')
        .toBe(button)

      fireEvent.click(button)
      expect(stub.href, 'the header button did not navigate to the settings page')
        .toBe('/?view=settings')
      view.unmount()
    } finally {
      Object.defineProperty(window, 'location',
        { value: real, writable: true, configurable: true })
    }
  })
})
