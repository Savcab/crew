# Runtime selection — browser test

Preconditions: the isolated QA MorphDB and dashboard built from the current
checkout are running on `http://127.0.0.1:18788`, and the browser was opened
with that process's capability fragment. Do not submit the form, so this flow cannot
start Claude, Codex, or a custom process.

1. Open the dashboard and click **+ Agent**.
   Expected: the Create agent modal opens.
2. Click **fill manually instead**.
   Expected: the advanced/manual fields are visible.
3. Find the **Runtime** selector.
   Expected: Claude Code is selected by default and the available choices are
   Claude Code, Codex CLI, and Custom command.
4. Select **Codex CLI**.
   Expected: the launch-command placeholder changes to a Codex command and the
   help text says the native identity file is `AGENTS.md`.
5. Select **Custom command**.
   Expected: the launch command becomes required and the help text explains
   that only portable `identity.md` is written automatically.
6. Select **Claude Code** again and close the modal with **×**.
   Expected: the modal closes, no agent was created, and no error toast appears.

Cleanup: none; the flow is read-only.
