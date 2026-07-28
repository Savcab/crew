// The settings page's second tab: environments — the named setup routines an
// agent's workspace gets before its runtime starts. What an operator would
// notice being wrong is what is pinned here: that the built-ins cannot be
// edited or deleted (the server owns them), that a custom one can be added and
// removed, that the crew-wide default is stored the moment it is picked rather
// than waiting for a Save the tab does not have, and that a refused write says
// why and leaves the operator's typing on screen to fix.
//
// The commands control is a free-text TEXTAREA, which is the OPPOSITE of the
// Harnesses tab's select-only rule — and deliberately so: a harness launch
// command must be one of the choices the server publishes, while an
// environment IS whatever setup the operator writes. A test below pins that,
// so "fixing" it into a select fails loudly.
//
// This vitest project has no globals and no setup file, so testing-library's
// automatic cleanup never runs — without the afterEach below, an unmounted
// page's controls stay in the document and answer getElementById for the tests
// that follow.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent, waitFor, cleanup } from '@testing-library/react'
import SettingsPage from '../src/components/SettingsPage.jsx'

afterEach(cleanup)

// Two built-ins (one with a prereq) and one operator-defined environment —
// the two kinds of card the tab has to tell apart.
const environments = () => [
  {
    name: 'worktree', builtin: true, prereq: '',
    description: 'A fresh git worktree branched off main',
    commands: ['git worktree add -b $CREW_AGENT ../$CREW_AGENT main'],
  },
  {
    name: 'graphite-stack', builtin: true, prereq: 'gt --version',
    description: 'A Graphite branch stacked off main',
    commands: ['gt repo init --trunk main', 'gt create $CREW_AGENT'],
  },
  {
    name: 'node-deps', builtin: false, prereq: '', description: 'npm install',
    commands: ['npm ci'],
  },
]

const withoutNodeDeps = () => environments().filter(e => e.name !== 'node-deps')

const settingRows = () => [{
  key: 'claude_launch_cmd', label: 'Claude launch command',
  default: 'claude', override: null, effective: 'claude', source: 'default',
  choices: [{ label: 'Default', command: 'claude' }],
}]

const envApi = (extra, snapshot) => ({
  settings: vi.fn().mockResolvedValue({ ok: true, settings: settingRows() }),
  settingsUpdate: vi.fn().mockResolvedValue({ ok: true, settings: settingRows() }),
  environments: vi.fn().mockResolvedValue(
    snapshot || { ok: true, default: 'worktree', environments: environments() }),
  environmentsUpdate: vi.fn().mockResolvedValue(
    { ok: true, default: 'worktree', environments: environments() }),
  ...extra,
})

const id = x => document.getElementById(x)
const card = name => id(`env-card-${name}`)
const cardNames = () => [...document.querySelectorAll('.env-card')]
  .map(el => el.id.replace('env-card-', ''))
const defaultOptions = () => [...id('env-default').options].map(o => o.textContent)
const toastText = () => id('toast').textContent
const toastFailed = () => id('toast').className.includes('err')

// The tab's controls only exist after api.environments() settles.
const openTab = async api => {
  const view = render(<SettingsPage api={api} />)
  fireEvent.click(id('settings-tab-environments'))
  await waitFor(() => expect(id('env-default'),
    'the environments tab never rendered its controls').toBeTruthy())
  return view
}

const fillAdd = ({ name, prereq, commands, description }) => {
  fireEvent.change(id('env-new-name'), { target: { value: name } })
  fireEvent.change(id('env-new-prereq'), { target: { value: prereq || '' } })
  fireEvent.change(id('env-new-commands'), { target: { value: commands } })
  fireEvent.change(id('env-new-description'),
    { target: { value: description || '' } })
}

describe('the environments tab', () => {
  it('sits beside Harnesses and swaps the pane rather than adding to it', async () => {
    const api = envApi()
    const view = render(<SettingsPage api={api} />)
    await waitFor(() => expect(id('setting-claude_launch_cmd')).toBeTruthy())

    const tab = id('settings-tab-environments')
    expect(tab, 'no Environments tab in the sidebar').toBeTruthy()
    expect(id('settings-nav').contains(tab),
      'the tab must live in the sidebar, not the main pane').toBe(true)

    fireEvent.click(tab)
    await waitFor(() => expect(id('env-default')).toBeTruthy())
    expect(id('settings-bar').textContent,
      'the main pane should name the group being edited')
      .toContain('Settings · Environments')
    expect(tab.className, 'the clicked tab should be the active one')
      .toContain('active')
    // Tabs are alternatives, not sections of one long page: the harness rows
    // (and their Save) must be gone, or Save would appear to cover this tab.
    expect(id('setting-claude_launch_cmd'),
      'the harness rows stayed on screen under the environments tab').toBe(null)
    expect(id('settings-save'),
      "Save belongs to the harness rows and must not imply it saves this tab's")
      .toBe(null)
    view.unmount()
  })

  it('renders every built-in as a card that cannot be edited or deleted', async () => {
    const view = await openTab(envApi())

    for (const env of environments().filter(e => e.builtin)) {
      const el = card(env.name)
      expect(el, `no card rendered for the built-in ${env.name}`).toBeTruthy()
      expect(el.textContent).toContain(env.description)
      expect(el.textContent, 'a built-in must say it is one').toContain('built-in')
      for (const cmd of env.commands) {
        expect(el.textContent,
          `${env.name} did not show the command it runs`).toContain(cmd)
      }
      expect(id(`env-remove-${env.name}`),
        `${env.name} is server-owned — offering delete would only invent a `
        + 'refusal').toBe(null)
      expect(el.querySelectorAll('input, textarea').length,
        `${env.name}'s commands must be shown read-only`).toBe(0)
    }
    // The prereq is why an environment refuses to run at spawn time, so a card
    // that has one has to show it.
    expect(card('graphite-stack').textContent,
      'the prereq check was not shown').toContain('gt --version')
    view.unmount()
  })

  it('offers a delete control on operator-defined environments only', async () => {
    const api = envApi({
      environmentsUpdate: vi.fn().mockResolvedValue(
        { ok: true, default: 'worktree', environments: withoutNodeDeps() }),
    })
    const view = await openTab(api)

    expect(cardNames()).toEqual(['worktree', 'graphite-stack', 'node-deps'])
    const remove = id('env-remove-node-deps')
    expect(remove, 'a custom environment must be removable').toBeTruthy()

    fireEvent.click(remove)

    await waitFor(() => expect(api.environmentsUpdate)
      .toHaveBeenCalledWith({ action: 'remove', name: 'node-deps' }))
    // The list repaints from the server answer, not from a local splice.
    await waitFor(() => expect(card('node-deps'),
      'the removed card is still on screen').toBe(null))
    expect(cardNames(), 'removing a custom one must not touch the built-ins')
      .toEqual(['worktree', 'graphite-stack'])
    view.unmount()
  })
})

describe('the crew-wide default environment', () => {
  it('offers none plus every environment, and shows the stored default', async () => {
    const view = await openTab(envApi())

    expect(defaultOptions(),
      'the default select should list none and every known environment')
      .toEqual(['none', 'worktree', 'graphite-stack', 'node-deps'])
    expect(id('env-default').value,
      "the select must show the default the server actually holds").toBe('worktree')
    view.unmount()
  })

  it('stores the pick immediately — this tab has no Save', async () => {
    const api = envApi()
    const view = await openTab(api)

    fireEvent.change(id('env-default'), { target: { value: 'node-deps' } })

    await waitFor(() => expect(api.environmentsUpdate)
      .toHaveBeenCalledWith({ action: 'set_default', name: 'node-deps' }))
    await waitFor(() => expect(toastText()).toContain('node-deps'))
    expect(toastFailed(), 'a stored default is not an error').toBe(false)
    view.unmount()
  })

  it('clears the default by picking none', async () => {
    const api = envApi({
      environmentsUpdate: vi.fn().mockResolvedValue(
        { ok: true, default: null, environments: environments() }),
    })
    const view = await openTab(api)

    fireEvent.change(id('env-default'), { target: { value: '' } })

    // Blank is what CLEARS it, the same convention the settings endpoint uses
    // for dropping an override.
    await waitFor(() => expect(api.environmentsUpdate)
      .toHaveBeenCalledWith({ action: 'set_default', name: '' }))
    await waitFor(() => expect(id('env-default').value,
      'a cleared default should read as none').toBe(''))
    view.unmount()
  })

  it('keeps showing the stored default when the write is refused', async () => {
    const api = envApi({
      environmentsUpdate: vi.fn().mockResolvedValue(
        { ok: false, error: 'node-deps: no such environment' }),
    })
    const view = await openTab(api)

    fireEvent.change(id('env-default'), { target: { value: 'node-deps' } })

    await waitFor(() => expect(toastText()).toBe('node-deps: no such environment'))
    expect(toastFailed()).toBe(true)
    // Nothing is pending here — the select IS the stored value, so leaving a
    // refused pick selected would lie about what new agents get.
    expect(id('env-default').value,
      'a refused default stayed selected as if it had been stored')
      .toBe('worktree')
    view.unmount()
  })
})

describe('adding an environment', () => {
  const added = () => ({
    ok: true,
    default: 'worktree',
    environments: [...environments(), {
      name: 'py-venv', builtin: false, prereq: 'python3 --version',
      description: 'virtualenv + deps', commands: ['python3 -m venv .venv', 'pip install -r requirements.txt'],
    }],
  })

  it('posts one command per non-blank line, trimmed, then clears the form', async () => {
    const api = envApi({
      environmentsUpdate: vi.fn().mockResolvedValue(added()),
    })
    const view = await openTab(api)

    // Free text is CORRECT here — an environment is whatever setup the
    // operator writes, unlike a harness launch command.
    expect(id('env-new-commands').tagName,
      'the command list must stay free-form; it is not a curated choice')
      .toBe('TEXTAREA')

    fillAdd({
      name: '  py-venv  ',
      prereq: ' python3 --version ',
      commands: 'python3 -m venv .venv\n\n   pip install -r requirements.txt   \n\n',
      description: ' virtualenv + deps ',
    })
    fireEvent.click(id('env-add'))

    await waitFor(() => expect(api.environmentsUpdate).toHaveBeenCalledWith({
      action: 'add',
      name: 'py-venv',
      commands: ['python3 -m venv .venv', 'pip install -r requirements.txt'],
      prereq: 'python3 --version',
      description: 'virtualenv + deps',
    }))
    await waitFor(() => expect(card('py-venv'),
      'the new environment never appeared').toBeTruthy())
    // A form still holding the last environment invites adding it twice.
    expect(id('env-new-name').value, 'the add form kept its values').toBe('')
    expect(id('env-new-commands').value).toBe('')
    expect(id('env-new-prereq').value).toBe('')
    expect(id('env-new-description').value).toBe('')
    expect(defaultOptions(), 'a new environment must become selectable as the default')
      .toContain('py-venv')
    view.unmount()
  })

  it('refuses to post an environment with no name or no commands', async () => {
    const api = envApi()
    const view = await openTab(api)

    fillAdd({ name: '  ', commands: 'npm ci' })
    fireEvent.click(id('env-add'))
    await waitFor(() => expect(toastText()).toMatch(/name/i))
    expect(toastFailed()).toBe(true)

    fillAdd({ name: 'empty', commands: '   \n\n  ' })
    fireEvent.click(id('env-add'))
    await waitFor(() => expect(toastText()).toMatch(/command/i))
    // An environment IS its commands; posting either shape only invents a
    // server refusal.
    expect(api.environmentsUpdate,
      'an incomplete add cost a request').not.toHaveBeenCalled()
    view.unmount()
  })

  it("reports a refused add and keeps what was typed", async () => {
    const api = envApi({
      environmentsUpdate: vi.fn().mockResolvedValue(
        { ok: false, error: 'worktree is a built-in name' }),
    })
    const view = await openTab(api)

    fillAdd({ name: 'worktree', commands: 'echo hi' })
    fireEvent.click(id('env-add'))

    await waitFor(() => expect(toastText()).toBe('worktree is a built-in name'))
    expect(toastFailed()).toBe(true)
    expect(id('env-new-name').value,
      'the refused values must stay on screen to be fixed or retried')
      .toBe('worktree')
    expect(id('env-new-commands').value).toBe('echo hi')
    expect(cardNames(), 'a refused add must not add a card')
      .toEqual(['worktree', 'graphite-stack', 'node-deps'])
    view.unmount()
  })

  it('treats a thrown write like a refused one', async () => {
    const api = envApi({
      environmentsUpdate: vi.fn().mockRejectedValue(new Error('connection reset')),
    })
    const view = await openTab(api)

    fillAdd({ name: 'py-venv', commands: 'npm ci' })
    fireEvent.click(id('env-add'))

    await waitFor(() => expect(toastText()).toBe('connection reset'))
    expect(toastFailed()).toBe(true)
    expect(id('env-new-name').value).toBe('py-venv')
    view.unmount()
  })
})

describe('the environments tab when the list will not load', () => {
  const failed = async snapshot => {
    const api = envApi({}, snapshot)
    if (snapshot === null) {
      api.environments = vi.fn().mockRejectedValue(new Error('network down'))
    }
    const view = render(<SettingsPage api={api} />)
    fireEvent.click(id('settings-tab-environments'))
    await waitFor(() => expect(id('env-error'),
      'a failed load rendered no error').toBeTruthy())
    return view
  }

  it('shows the reason instead of an empty tab that looks like no environments', async () => {
    const view = await failed({ ok: false, error: 'environments.json unreadable' })

    expect(id('env-error').textContent).toContain('environments.json unreadable')
    // An empty add form over a failed read would post into the dark.
    expect(id('env-add'), 'the add form was offered over a failed read').toBe(null)
    expect(id('env-default'), 'no default control without a list').toBe(null)
    view.unmount()
  })

  it('reports a thrown request the same way, and leaves the other tab usable', async () => {
    const view = await failed(null)

    expect(id('env-error').textContent).toContain('network down')
    fireEvent.click(id('settings-tab-harnesses'))
    await waitFor(() => expect(id('setting-claude_launch_cmd'),
      'a failed environments read broke the harnesses tab').toBeTruthy())
    view.unmount()
  })
})
