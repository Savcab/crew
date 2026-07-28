# frontend — context

## What this area does
The React dashboard an operator actually looks at: a force-directed graph of
the crew's agents and webhooks, a bottom dock holding one live tmux terminal,
and the modals that create agents, draw edges, and resolve pending approvals.
It talks to the python server only through `src/api.js`. `npm run build`
emits into `../static`, which that server serves — there is no node process at
crew runtime.

## Key files
- `src/main.jsx` — boot: installs the xterm globals, then picks one of three
  top-level surfaces from the ?view= param.
- `src/App.jsx` — the dashboard shell: owns the snapshot poll, the modal slot,
  the toast, and the graph/dock wiring.
- `src/api.js` — every HTTP call, one wrapper per endpoint. Pure transport;
  also owns capability exchange and the CSRF header.
- `src/graphEngine.js` — the imperative graph canvas (physics, drag, pinning,
  localStorage positions). Owns everything inside the #cgraph element.
- `src/dockCore.js` / `src/term.js` — dock controller and the xterm pane bound
  to a real `tmux attach` PTY over SSE.
- `src/termLink.js` — BroadcastChannel handoff between the graph window and
  the second-monitor terminal window.
- `src/keys.js` — the single keydown dispatcher for the whole dashboard.
- `src/modalShared.js` — DOM-free modal logic (cap text, edge normalization),
  extracted so vitest can pin it without rendering.
- `src/components/modals/formUtils.jsx` — the shared form contract: Field,
  CondList, useSubmit, useAlive.
- `src/components/GraphsGallery.jsx` / `src/components/TermWindow.jsx` — the
  ?view=graphs and ?view=term surfaces.
- `src/app.css` / `src/theme.js` / `src/status.js` — the shared palette, the
  MUI theme tuned to match it, and the one status vocabulary.
- `vite.config.js` — build output, base path, and the vitest config.
- `tests/` — vitest suites (transport, layout, modal contract, auth).

## Invariants and gotchas
- No router. `?view=` is read ONCE at boot in `src/main.jsx` (`term` →
  TermWindow, `graphs` → GraphsGallery, anything else → App). Changing views
  is a full navigation, not a state update.
- `npm run build` IS the deploy. It writes into `../static` with
  `emptyOutDir: false` on purpose: feature dossiers pin the hashed bundle
  filenames they shipped and the docs validator requires those paths to still
  exist, so a rebuild must never delete a predecessor's bundle. Superseded
  assets accumulate by design — do not "clean up" `../static/assets`.
- Text inputs are deliberately UNCONTROLLED and read back from the DOM by id
  at submit time (`val`/`checked`/`setVal` in `formUtils.jsx`). Browser test
  scripts and the `/api/expand` prefill write straight into those inputs; a
  controlled React input would silently discard both. Checkboxes are the
  opposite — MUI renders their state from React, so they stay controlled.
- The legacy DOM ids are a contract, not leftovers: `#cmodal`, `#modalTitle`,
  `#modalBody`, `#cgraph`, `#rate`, the `a-*`/`e-*`/`w-*` field ids, and the
  `.cl-rows`/`.cl-input` condition-list classes are what tests and browser
  scripts target. Renaming one silently breaks callers outside this tree.
- `GraphView`, `Dock`, and the graph canvas are imperative islands mounted
  inside static, memoized React skeletons. React must never reconcile their
  children — that is why there is no `StrictMode` in `src/main.jsx`.
- Every POST carries `X-Crew-CSRF: 1`. It is non-simple on purpose: it forces
  a preflight the dashboard never grants, so another localhost port cannot
  ride the cookie.
- The capability arrives as a `#cap=` fragment (fragments never reach the
  server or its logs), is exchanged once for an HttpOnly cookie, and is then
  scrubbed from the address bar. All requests await that exchange, so an
  early call cannot race ahead unauthenticated.
- Modals remount per open (a bumped `key`), which scopes React state but not
  raw DOM writes or the global `onClose` — re-check `alive.current` after
  every await inside a modal.
- vitest runs on jsdom WITHOUT `globals: true`, so testing-library's
  automatic `afterEach(cleanup)` never registers. Rendered trees persist
  across tests in a file and `document.getElementById` will find a stale node
  from the previous one; existing tests call `view.unmount()` explicitly at
  the end of each case. Keep doing that or add your own cleanup.

## When to update this file
- A new top-level surface, a new imperative island, or a change to how
  `?view=`, the build output, or the auth/CSRF handshake works.
- Any change to the uncontrolled-input contract, the legacy id set, or the
  test-cleanup convention above.
