"""Static accessibility + resilience contracts for the React dashboard sources.

The dashboard frontend lives in frontend/src (React + MUI, built into
static/). These are fast source-level guards for contracts the browser suite
verifies live: dialog semantics, toast announcements, in-flight submit
guards, poll-scheduler discipline, and the graph engine's keyboard paths.
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "frontend" / "src"


def src(*parts):
    return Path(SRC, *parts).read_text(encoding="utf-8")


class ModalAccessibilityTests(unittest.TestCase):
    def test_manual_fallback_link_has_explicit_dark_theme_color(self):
        css = src("app.css")
        rule = re.search(
            r"\.f-hint\s+a(?:\s*,\s*\.f-hint\s+a:visited)?\s*\{([^}]*)\}",
            css,
        )
        self.assertIsNotNone(
            rule,
            "manual fallback links must not inherit the browser's low-contrast "
            "default link/visited colors on the dark modal",
        )
        self.assertRegex(rule.group(1), r"color\s*:\s*var\(--accent\)")

    def test_modal_exposes_dialog_semantics_and_named_close_control(self):
        # MUI's Dialog supplies role="dialog"/aria-modal at runtime; the shell
        # must still label the dialog and name the unicode close control.
        shell = src("components", "modals", "ModalShell.jsx")
        self.assertIn("from '@mui/material/Dialog'", shell)
        self.assertIn('aria-labelledby="modalTitle"', shell)
        self.assertRegex(
            shell, r'id="modalClose"[^>]*aria-label="Close dialog"')

    def test_toasts_announce_success_and_errors_to_assistive_technology(self):
        toast = src("components", "Toast.jsx")
        self.assertIn('id="toast"', toast)
        self.assertIn("'alert' : 'status'", toast)
        self.assertIn('aria-live="polite"', toast)
        self.assertIn('aria-atomic="true"', toast)

    def test_modal_focus_management_is_delegated_to_mui_dialog(self):
        # The old hand-rolled trap (focusableControls/previousFocus/inert) is
        # MUI Dialog's job now; every modal must render through the one shell.
        shell = src("components", "modals", "ModalShell.jsx")
        self.assertIn("<Dialog open onClose={onClose}", shell)
        for modal in ("CreateAgentModal", "ConnectEdgeModal", "EditEdgeModal",
                      "IdentityModal", "PendingModal"):
            with self.subTest(modal=modal):
                self.assertIn("<ModalShell",
                              src("components", "modals", f"{modal}.jsx"))

    def test_modal_mutations_have_a_single_in_flight_guard(self):
        utils = src("components", "modals", "formUtils.jsx")
        self.assertRegex(utils, r"if\s*\(busyRef\.current\)\s*return")
        # Action buttons disable while a submit is in flight.
        for modal in ("CreateAgentModal", "ConnectEdgeModal", "EditEdgeModal"):
            with self.subTest(modal=modal):
                self.assertIn("disabled={busy}",
                              src("components", "modals", f"{modal}.jsx"))

    def test_stale_async_responses_cannot_touch_a_replacement_modal(self):
        """A slow Generate/POST must not mutate or unlock a replacement form.

        The old code used an epoch counter; the React port remounts a fresh
        modal instance per open (key bump), so an in-flight callback's state
        belongs to the unmounted instance and can never leak forward.
        """
        app = src("App.jsx")
        self.assertIn("modalKeyRef.current += 1", app)
        self.assertGreaterEqual(
            app.count("key={mkey}"), 5,
            "every modal must remount per open so stale callbacks stay scoped")

    def test_edge_cap_inputs_are_not_lossily_coerced_in_the_browser(self):
        """Malformed caps must reach strict backend validation, not become zero.

        JavaScript's ``parseInt('1x')`` returns 1 and ``NaN || 0`` turns a typo
        into an accepted unlimited cap. Preserve the trimmed field text so the
        graphstore boundary can reject anything that is not a whole valid value.
        """
        fields = src("components", "modals", "EdgeFields.jsx")
        self.assertIn("readEdgeCaps", fields)
        self.assertIn("numericText", fields)
        combined = (src("components", "modals", "ConnectEdgeModal.jsx")
                    + src("components", "modals", "EditEdgeModal.jsx"))
        self.assertNotRegex(combined, r"parseInt\(val\('e-(?:max|token-cap)'\)")
        self.assertNotIn("parseFloat(val('e-cost-cap')", combined)
        self.assertGreaterEqual(combined.count("...readEdgeCaps()"), 2)

    def test_codex_launch_placeholder_matches_the_backend_default(self):
        modal = src("components", "modals", "CreateAgentModal.jsx")
        command = "codex --dangerously-bypass-approvals-and-sandbox --disable hooks"
        self.assertIn(
            command,
            modal,
            "the Codex placeholder must show the command Crew will actually "
            "launch when the field is blank",
        )


class DashboardPollingTests(unittest.TestCase):
    def test_snapshot_polling_never_overlaps_a_slow_request(self):
        """A slow backend must not accumulate concurrent snapshot requests.

        The scheduler owns an explicit in-flight latch and must not use
        ``setInterval`` (which invokes an async callback without awaiting it).
        """
        app = src("App.jsx")
        self.assertIn("p.inFlight", app)
        self.assertNotIn("setInterval(", app)
        self.assertIn("setTimeout(runPoll", app)

    def test_forced_refresh_is_not_lost_behind_an_in_flight_snapshot(self):
        """Mutation refreshes must work even when automatic polling is off."""
        app = src("App.jsx")
        self.assertRegex(
            app,
            r"if\s*\(p\.loadPromise\)\s*\{\s*if\s*\(force\)\s*"
            r"p\.reloadQueued\s*=\s*true",
        )
        self.assertIsNotNone(
            re.search(
                r"if\s*\(p\.reloadQueued\)\s*\{.*?await loadGraph\(true\)",
                app,
                re.DOTALL,
            ),
        )

    def test_graph_has_accessible_loading_and_transport_error_states(self):
        view = src("components", "GraphView.jsx")
        app = src("App.jsx")
        self.assertRegex(view, r'id="cgraph"\s+aria-busy="true"')
        self.assertRegex(
            view,
            r'id="graphStatus"[^>]*role="status"[^>]*aria-live="polite"',
        )
        self.assertIn("Loading crew", view)
        self.assertIn("setGraphUnavailable", app)
        self.assertRegex(app, r"catch\s*\(e\)\s*\{\s*setGraphUnavailable")
        self.assertIn("setAttribute('aria-busy', 'false')", app)

    def test_transport_error_invalidates_the_render_signature_for_recovery(self):
        """An unchanged snapshot must repaint after an outage replaced the DOM.

        Otherwise the signature cache suppresses the recovery render and leaves
        the user looking at ``backend unavailable`` until some graph data changes
        or the entire page is reloaded.
        """
        app = src("App.jsx")
        unavailable = re.search(
            r"const setGraphUnavailable = useCallback\((?P<body>.*?)\n  \}",
            app,
            re.DOTALL,
        )
        self.assertIsNotNone(unavailable)
        self.assertRegex(
            unavailable.group("body"),
            r"lastSig\s*=\s*['\"]['\"]",
            "transport failure must invalidate the cached render signature so "
            "the next successful snapshot repaints even when its data is unchanged",
        )

    def test_transport_and_mutations_fail_closed_on_unsuccessful_responses(self):
        """A 4xx/5xx or malformed success body must never produce a success toast."""
        api_js = src("api.js")
        utils = src("components", "modals", "formUtils.jsx")
        self.assertIn("async function _jsonResponse", api_js)
        self.assertIn("if (!response.ok)", api_js)
        self.assertRegex(utils, r"if\s*\(!r\s*\|\|\s*r\.ok\s*!==\s*true\)")


class GraphAccessibilityTests(unittest.TestCase):
    def test_runtime_crash_is_not_labeled_as_a_missing_session(self):
        for name in ("graphEngine.js", "dockCore.js"):
            with self.subTest(source=name):
                js = src(name)
                self.assertIn("runtime down", js)
                self.assertRegex(
                    js,
                    r"status\s*===\s*'down'\s*&&\s*\w+\s*&&\s*"
                    r"\w+\.session_alive",
                )

    def test_pointer_open_focuses_agent_before_docking_for_restoration(self):
        """A mouse-opened dock must return focus to the originating card.

        ``startDrag`` prevents the browser's normal mousedown focus, so the
        click path has to focus the card explicitly before the dock captures
        ``document.activeElement`` as its return target.
        """
        js = src("graphEngine.js")
        click_path = re.search(
            r"if\s*\(!drag\.moved\)\s*\{(?P<body>.*?)\}\s*"
            r"else\s+if\s*\(node\)",
            js,
            re.DOTALL,
        )
        self.assertIsNotNone(click_path)
        body = click_path.group("body")
        self.assertIn("node.el.focus()", body)
        self.assertIn("H.onDockAgent(clicked.data)", body)
        self.assertLess(body.index("node.el.focus()"),
                        body.index("H.onDockAgent(clicked.data)"))

    def test_nodes_and_edges_have_keyboard_activation_paths(self):
        view = src("components", "GraphView.jsx")
        js = src("graphEngine.js")
        self.assertIn('id="graphKeyboardStatus"', view)
        self.assertIn("el.tabIndex = 0", js)
        self.assertIn("el.setAttribute('role', 'button')", js)
        self.assertIn("startKeyboardConnect", js)
        self.assertIn("Connecting from", js)
        self.assertIn("line.setAttribute('tabindex', '0')", js)
        self.assertIn("line.setAttribute('role', 'button')", js)

    def test_canvas_storage_is_scoped_to_the_snapshot_workspace(self):
        js = src("graphEngine.js")
        self.assertIn("workspace_key", js)
        self.assertIn("selectWorkspace", js)
        self.assertIn("positionStorageKey", js)
        self.assertIn("viewStorageKey", js)


if __name__ == "__main__":
    unittest.main()
