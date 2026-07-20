# react-migration — React+MUI frontend boots, auths, and scopes stale async work

The dashboard frontend is a React + MUI app (frontend/, built into static/ by
`npm run build`). This script covers what the migration changed structurally:
the built-asset boot path, the capability auth flow through the new bundle,
MUI dialog semantics behind the legacy ids, and the stale-async-response
guards (the two regressions found during the migration — a stale /api/expand
response writing into a replacement form, and a stale mutation success closing
a modal it didn't open).

Target: an isolated dashboard (throwaway MorphDB app, port 18788) so fetch
interception and fixtures can't touch the operator app. The stale-async steps
follow the same interception technique as resilience-accessibility.md.

1. `GET /` with no cookies. EXPECT: HTML whose scripts load from
   `/static/assets/…` (hashed bundle); `/api/graph/snapshot` without a
   capability cookie returns 403.
2. Load `/#cap=<capability>`. EXPECT: fragment is erased from the address bar
   after load; snapshot polling succeeds; the graph area renders.
3. Confirm MUI + legacy contract coexist: open + Agent. EXPECT: exactly one
   `[role="dialog"]` (MUI), containing `#modalTitle` ("Create agent"),
   `#modalClose`, and blob-mode fields `#a-blob` / `#a-generate` /
   `#a-manual-link`; `#cmodal` id present on the dialog root.
4. Stale Generate guard: intercept `/api/expand` and hold its response. Click
   `#a-generate` (with `#a-blob` filled), CLOSE the modal, reopen + Agent, then
   release the held response with fields `{name:"must_not_appear"}`.
   EXPECT: the fresh form's `#a-name` stays empty; no crash.
5. Stale submit guard: create two fixture agents and an edge between them.
   Intercept `/api/edge/update` and hold it. Open the edge's edit modal, click
   `#e-save`, close the modal, open a DIFFERENT modal (an agent's ⓘ identity
   card), then release the held response `{ok:true}`. EXPECT: the identity
   modal STAYS OPEN; the "edge updated" toast and a graph refresh still occur
   (finished work is reported, but a modal is never closed by a request it
   didn't make).
6. Cleanup: remove fixtures, restore fetch, verify the operator dashboard on
   8788 untouched.
