"""Static accessibility contracts for the dependency-free dashboard CSS."""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ModalAccessibilityTests(unittest.TestCase):
    def test_manual_fallback_link_has_explicit_dark_theme_color(self):
        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
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
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertRegex(
            html,
            r'<div id="cmodal"[^>]*role="dialog"[^>]*aria-modal="true"'
            r'[^>]*aria-labelledby="modalTitle"',
        )
        self.assertRegex(
            html, r'id="modalClose"[^>]*aria-label="Close dialog"')

    def test_toasts_announce_success_and_errors_to_assistive_technology(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        js = (ROOT / "static" / "js" / "main.js").read_text(
            encoding="utf-8")
        self.assertRegex(
            html,
            r'id="toast"[^>]*role="status"[^>]*aria-live="polite"'
            r'[^>]*aria-atomic="true"',
        )
        self.assertIn("err ? 'alert' : 'status'", js)

    def test_modal_controller_owns_focus_entry_trap_and_restoration(self):
        js = (ROOT / "static" / "js" / "modal.js").read_text(
            encoding="utf-8")
        for contract in (
            "focusableControls", "previousFocus", "e.key !== 'Tab'",
            "target.focus()", "setBackgroundInert",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, js)

    def test_modal_mutations_have_a_single_in_flight_guard(self):
        js = (ROOT / "static" / "js" / "modal.js").read_text(
            encoding="utf-8")
        self.assertIn("submitInFlight", js)
        self.assertRegex(js, r"if\s*\(submitInFlight\)\s*return")
        self.assertIn("button.disabled = true", js)

    def test_async_expansion_responses_are_scoped_to_the_open_modal(self):
        """A slow Generate response must not mutate a replacement form."""
        js = (ROOT / "static" / "js" / "modal.js").read_text(
            encoding="utf-8")
        self.assertIn("modalEpoch", js)
        self.assertGreaterEqual(
            len(re.findall(r"epoch\s*!==\s*modalEpoch", js)),
            2,
            "both agent and edge expansion callbacks must abandon stale results",
        )

    def test_stale_mutation_response_cannot_close_a_replacement_modal(self):
        """Closing/reopening while a POST is pending must isolate its callback."""
        js = (ROOT / "static" / "js" / "modal.js").read_text(
            encoding="utf-8")
        self.assertIn("const ticket = { epoch", js)
        self.assertIn("submitInFlight === ticket", js)
        self.assertRegex(
            js,
            r"const current\s*=\s*epoch\s*===\s*modalEpoch\s*&&\s*isOpen\(\)",
        )

    def test_edge_cap_inputs_are_not_lossily_coerced_in_the_browser(self):
        """Malformed caps must reach strict backend validation, not become zero.

        JavaScript's ``parseInt('1x')`` returns 1 and ``NaN || 0`` turns a typo
        into an accepted unlimited cap. Preserve the trimmed field text so the
        graphstore boundary can reject anything that is not a whole valid value.
        """
        js = (ROOT / "static" / "js" / "modal.js").read_text(
            encoding="utf-8")
        self.assertIn("readEdgeCaps", js)
        self.assertNotRegex(js, r"parseInt\(val\('e-(?:max|token-cap)'\)")
        self.assertNotIn("parseFloat(val('e-cost-cap')", js)
        self.assertGreaterEqual(js.count("...readEdgeCaps()"), 2)

    def test_codex_launch_placeholder_matches_the_backend_default(self):
        js = (ROOT / "static" / "js" / "modal.js").read_text(
            encoding="utf-8")
        command = "codex --dangerously-bypass-approvals-and-sandbox --disable hooks"
        self.assertIn(
            command,
            js,
            "the Codex placeholder must show the command Crew will actually "
            "launch when the field is blank",
        )


class DashboardPollingTests(unittest.TestCase):
    def test_snapshot_polling_never_overlaps_a_slow_request(self):
        """A slow backend must not accumulate concurrent snapshot requests.

        The browser-level regression delays each snapshot longer than the chosen
        interval.  Keep a small source contract here as the fast suite's guard:
        the scheduler owns an explicit in-flight latch and must not use
        ``setInterval`` (which invokes an async callback without awaiting it).
        """
        js = (ROOT / "static" / "js" / "main.js").read_text(
            encoding="utf-8")
        self.assertIn("pollInFlight", js)
        self.assertNotIn("setInterval(loadGraph", js)
        self.assertIn("setTimeout(runPoll", js)

    def test_forced_refresh_is_not_lost_behind_an_in_flight_snapshot(self):
        """Mutation refreshes must work even when automatic polling is off."""
        js = (ROOT / "static" / "js" / "main.js").read_text(
            encoding="utf-8")
        self.assertIn("graphReloadQueued", js)
        self.assertRegex(
            js,
            r"if\s*\(graphLoadPromise\)\s*\{\s*if\s*\(force\)\s*"
            r"graphReloadQueued\s*=\s*true",
        )
        self.assertIsNotNone(
            re.search(
                r"if\s*\(graphReloadQueued\)\s*\{.*?await loadGraph\(true\)",
                js,
                re.DOTALL,
            ),
        )

    def test_graph_has_accessible_loading_and_transport_error_states(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        js = (ROOT / "static" / "js" / "main.js").read_text(
            encoding="utf-8")
        self.assertRegex(
            html,
            r'id="cgraph"[^>]*aria-busy="true"',
        )
        self.assertRegex(
            html,
            r'id="graphStatus"[^>]*role="status"[^>]*aria-live="polite"',
        )
        self.assertIn("Loading crew", html)
        self.assertIn("setGraphUnavailable", js)
        self.assertRegex(js, r"catch\s*\(e\)\s*\{\s*setGraphUnavailable")
        self.assertIn("setAttribute('aria-busy', 'false')", js)

    def test_transport_error_invalidates_the_render_signature_for_recovery(self):
        """An unchanged snapshot must repaint after an outage replaced the DOM.

        Otherwise the signature cache suppresses the recovery render and leaves
        the user looking at ``backend unavailable`` until some graph data changes
        or the entire page is reloaded.
        """
        js = (ROOT / "static" / "js" / "main.js").read_text(
            encoding="utf-8")
        unavailable = re.search(
            r"function setGraphUnavailable\(message\)\s*\{(?P<body>.*?)\n\}",
            js,
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
        api_js = (ROOT / "static" / "js" / "api.js").read_text(
            encoding="utf-8")
        modal_js = (ROOT / "static" / "js" / "modal.js").read_text(
            encoding="utf-8")
        self.assertIn("async function _jsonResponse", api_js)
        self.assertIn("if (!response.ok)", api_js)
        self.assertRegex(modal_js, r"if\s*\(!r\s*\|\|\s*r\.ok\s*!==\s*true\)")


class GraphAccessibilityTests(unittest.TestCase):
    def test_runtime_crash_is_not_labeled_as_a_missing_session(self):
        graph_js = (ROOT / "static" / "js" / "graph.js").read_text(
            encoding="utf-8")
        dock_js = (ROOT / "static" / "js" / "dock.js").read_text(
            encoding="utf-8")
        for js in (graph_js, dock_js):
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
        js = (ROOT / "static" / "js" / "graph.js").read_text(
            encoding="utf-8")
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
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        js = (ROOT / "static" / "js" / "graph.js").read_text(
            encoding="utf-8")
        self.assertIn('id="graphKeyboardStatus"', html)
        self.assertIn("el.tabIndex = 0", js)
        self.assertIn("el.setAttribute('role', 'button')", js)
        self.assertIn("startKeyboardConnect", js)
        self.assertIn("Connecting from", js)
        self.assertIn("line.setAttribute('tabindex', '0')", js)
        self.assertIn("line.setAttribute('role', 'button')", js)

    def test_canvas_storage_is_scoped_to_the_snapshot_workspace(self):
        js = (ROOT / "static" / "js" / "graph.js").read_text(
            encoding="utf-8")
        self.assertIn("workspace_key", js)
        self.assertIn("selectWorkspace", js)
        self.assertIn("positionStorageKey", js)
        self.assertIn("viewStorageKey", js)


if __name__ == "__main__":
    unittest.main()
