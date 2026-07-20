#!/usr/bin/env python3
"""crew dashboard server — ThreadingHTTPServer + router.

Serves the static SPA (static/) and a JSON/SSE API. The terminal transport
(tmux PTY-attach → xterm) and the tmux/shell endpoints are ported verbatim from
the tuned `ng` stack (ptyio/tmuxio) — that part was hand-tuned and we keep it.

What's NEW vs the old crew dashboard: the data API is the AGENT GRAPH, not a task
board. It talks straight to crew.graphstore / crew.spawn (same process, in-Python
— no CLI shell-out), so the surfaces are:

  GET  /api/graph/snapshot         crew agents + edges + live tmux status
  GET  /api/pty/stream             SSE terminal attach — CREW SESSIONS ONLY
  POST /api/pty/input|resize       keystrokes / grid size for an attached terminal
  POST /api/agent/create           spawn a new agent (home-uniqueness enforced)
  POST /api/agent/remove           delete an agent
  POST /api/edge/create|update|delete   connect / edit / disconnect two agents
  POST /api/agent/bless|/api/edge/bless   mark an agent/edge row reviewed (human-only)
  POST /api/agent/foreman          grant/revoke the foreman flag (human-only, singleton)
  GET  /api/pending                pending graph_edit rows (WAVE 4), newest first
  POST /api/pending/approve|reject   resolve a pending row (human-only)
  POST /api/expand                 one-blob LLM expansion (UI wave B, human-only)

This dashboard manages ONLY crew-spawned agents. It deliberately does not list,
attach to, or resize any other runtime session on the box — so an independent
Claude Code, Codex, or shell session you started yourself is never touched (no
surprise window resizes).

Binds 127.0.0.1 ONLY — this is remote control of your terminals. Port 8788 by
default (MorphDB owns 8787), overridable via $CREW_PORT.

  Run:  python3 -m crew.server.app   then open http://127.0.0.1:8788
"""
import base64
import binascii
import hmac
import json
import mimetypes
import os
import re
import select
import socket
import subprocess
import threading
import time

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import tmuxio, ptyio
from .. import config, graphstore as gs, guard, spawn, mail, runtime as runtimes
from ..notify import notify

HOST = config.DASHBOARD_HOST
PORT = config.DASHBOARD_PORT

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.realpath(os.path.join(HERE, "..", "..", "static"))
INDEX_HTML = os.path.join(STATIC_DIR, "index.html")

MAX_BODY = 1 << 20
# One terminal input request may carry a paste, but should not become an
# unbounded memory/write primitive. 256 KiB decoded is ample for interactive
# use and remains well below MAX_BODY after base64 expansion.
MAX_PTY_INPUT = 256 * 1024

# JSON booleans are a distinct wire type.  Python's bool("false") is True and
# bool(0) is False, so coercing API input can silently invert launch, deletion,
# topology, or governance intent.  Validate every boolean-bearing mutation at
# the routing boundary; omitted fields retain their handler defaults.
_BOOLEAN_FIELDS_BY_PATH = {
    "/api/agent/create": ("launch",),
    "/api/agent/remove": ("kill_session",),
    "/api/edge/create": ("reply_expected", "back_reply", "directed"),
    "/api/edge/update": ("reply_expected", "back_reply", "directed"),
    "/api/agent/foreman": ("revoke",),
}

# Text-bearing control fields must remain JSON strings all the way to the
# graph/runtime boundary. Coercing numbers to text can turn ``123`` into a real
# agent name, path, edge label, or approval reason; treating objects/lists as
# missing can silently drop an operator's requested condition.
_TEXT_FIELDS_BY_PATH = {
    "/api/auth/bootstrap": ("capability",),
    "/api/agent/create": (
        "name", "role", "identity", "home", "repo", "launch_cmd", "runtime"),
    "/api/agent/start": ("name",),
    "/api/agent/remove": ("name",),
    "/api/edge/create": (
        "source", "target", "label", "description", "condition",
        "target_action", "back_action"),
    "/api/edge/update": (
        "guid", "label", "description", "target_action", "back_action"),
    "/api/edge/delete": ("guid",),
    "/api/agent/bless": ("name",),
    "/api/edge/bless": ("guid",),
    "/api/agent/foreman": ("name",),
    "/api/pending/approve": ("guid",),
    "/api/pending/reject": ("guid", "reason"),
    "/api/expand": ("kind", "text", "source", "target"),
}

_TEXT_LIST_FIELDS_BY_PATH = {
    "/api/edge/create": ("conditions", "back_conditions"),
    "/api/edge/update": ("conditions", "back_conditions"),
}

_GET_API_PATHS = frozenset({
    "/api/graph/snapshot", "/api/health", "/api/pending",
    "/api/pty/stream", "/api/pty/windows",
})
_POST_API_PATHS = frozenset({
    "/api/auth/bootstrap", "/api/pty/input", "/api/pty/resize",
    "/api/pty/window/create", "/api/pty/window/select",
    "/api/agent/create", "/api/agent/start", "/api/agent/remove",
    "/api/edge/create", "/api/edge/update", "/api/edge/delete",
    "/api/agent/bless", "/api/edge/bless", "/api/agent/foreman",
    "/api/pending/approve", "/api/pending/reject", "/api/expand",
})

# A dashboard process is an operator control plane, not merely a read-only
# localhost page.  The CLI gives each process a fresh capability and the UI
# exchanges it (from a URL fragment, which never reaches HTTP logs) for this
# HttpOnly cookie.  An empty capability fails closed: direct server launches
# expose only the UI shell, static assets, and health metadata; graph data,
# pending edits, mutations, and terminal access remain unavailable.
OPERATOR_CAPABILITY = os.environ.pop("CREW_DASHBOARD_CAPABILITY", "").strip()
DASHBOARD_INSTANCE_ID = os.environ.pop("CREW_DASHBOARD_INSTANCE_ID", "").strip()


def _operator_cookie_name(port):
    """A host cookie name unique to one dashboard port.

    Browser cookie scope has no port component. Reusing one name would make
    opening a second local Crew dashboard overwrite the first one's capability.
    """
    return f"crew_operator_{int(port)}"


OPERATOR_COOKIE = _operator_cookie_name(PORT)
CSRF_HEADER = "X-Crew-CSRF"
CSRF_VALUE = "1"

_CTYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon",
    ".map": "application/json; charset=utf-8",
}


def _dashboard_health():
    """Process identity used by the CLI to make PID-file shutdown safe."""
    return {
        "ok": True,
        "service": "crew-dashboard",
        "pid": os.getpid(),
        "port": PORT,
        "app": config.current_app(),
        "instance_id": DASHBOARD_INSTANCE_ID,
    }


def _invalid_boolean_field(path, data):
    """First present API boolean whose JSON type is not exactly boolean."""
    for field in _BOOLEAN_FIELDS_BY_PATH.get(path, ()):
        if field in data and type(data[field]) is not bool:
            return field
    return None


def _invalid_text_field(path, data):
    """First present API text field whose JSON type is not exactly string."""
    for field in _TEXT_FIELDS_BY_PATH.get(path, ()):
        if field in data and not isinstance(data[field], str):
            return field
    return None


def _invalid_text_list_field(path, data):
    """First present condition field that is not an array of strings."""
    for field in _TEXT_LIST_FIELDS_BY_PATH.get(path, ()):
        if field not in data:
            continue
        value = data[field]
        if (not isinstance(value, list)
                or any(not isinstance(item, str) for item in value)):
            return field
    return None


def _decode_pty_input(data):
    """Strictly decode one non-empty, size-bounded PTY input payload."""
    encoded = data.get("b64")
    if not isinstance(encoded, str):
        raise ValueError("b64 must be a non-empty JSON string")
    if not encoded:
        raise ValueError("b64 must be a non-empty JSON string")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("b64 must be valid base64") from error
    if not decoded:
        raise ValueError("b64 must decode to non-empty input")
    if len(decoded) > MAX_PTY_INPUT:
        raise ValueError("PTY input exceeds 256 KiB decoded limit")
    return decoded


def _required_string(data, field):
    """Return one required, non-blank JSON string without coercing its type."""
    value = data.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a non-empty JSON string")
    if not value.strip():
        raise ValueError(f"{field} must be a non-empty JSON string")
    return value


def _required_int(data, field, minimum, maximum):
    """Return one bounded JSON integer, rejecting bools and lossy coercion."""
    value = data.get(field)
    if type(value) is not int:
        raise ValueError(
            f"{field} must be a JSON integer between {minimum} and {maximum}")
    if value < minimum or value > maximum:
        raise ValueError(
            f"{field} must be between {minimum} and {maximum}")
    return value


def _decode_json_object(raw):
    """Decode one standards-compliant JSON object or raise a client error."""
    def reject_constant(_value):
        # Python's json module accepts NaN/Infinity extensions by default even
        # though JSON does not. API numbers must use the same finite wire
        # contract in every client and language.
        raise ValueError("non-standard numeric constant")

    try:
        data = json.loads(raw or b"{}", parse_constant=reject_constant)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("request body must contain valid JSON") from error
    if not isinstance(data, dict):
        raise ValueError("request body must be a JSON object")
    return data


# --------------------------------------------------------------------------- #
# status-transition notifications — the "silent overnight death" fix
# --------------------------------------------------------------------------- #
# Previous live_status per stable agent identity lets both the browser snapshot and the
# dashboard-owned background monitor fire crew.notify on TRANSITIONS only —
# never for a steady state. First sight of an agent SEEDS the dict without
# firing, so a dashboard restart doesn't re-announce every already-down agent.
# Agents gone from the graph are pruned so churn cannot grow this forever.
_prev_status = {}
_notify_lock = threading.Lock()


def _status_transitions(agents):
    """Notify the operator when an agent transitions to down or needs_input.

    Transition
    detection runs under the lock; the webhook POSTs fire from a daemon thread
    AFTER it's released, so a slow webhook never delays the snapshot response
    (nor the background monitor or other pollers queued on the lock). A real
    recovery followed by a second failure is a second transition and must not
    be hidden behind a time-based suppression window.
    """
    pending = []
    with _notify_lock:
        live = {a.get("_guid") or a.get("name") for a in agents}
        for gone in [identity for identity in _prev_status if identity not in live]:
            del _prev_status[gone]
        for a in agents:
            name, status = a.get("name"), a.get("live_status")
            if not name:
                continue
            # Names are intentionally reusable after removal. A replacement
            # must seed its own state even when no monitor cycle observed the
            # old name disappear, otherwise it inherits false transitions.
            identity = a.get("_guid") or name
            prev = _prev_status.get(identity)
            _prev_status[identity] = status
            if prev is None or status == prev:
                continue        # seeding, or steady state
            if status == "down":
                event = "agent_down"
                detail = f"session '{a.get('session') or name}' died (was {prev})"
            elif status == "needs_input":
                event = "needs_input"
                detail = f"waiting for input (was {prev})"
            else:
                continue
            pending.append((event, name, detail))
    if pending:
        threading.Thread(target=lambda: [notify(*p) for p in pending],
                         daemon=True).start()


def _enrich_live_status(agents):
    """Add live tmux/runtime fields to agent rows in-place."""
    inventory = tmuxio.live_agent_inventory(agents)
    for agent in agents:
        agent.update(tmuxio.agent_snapshot_fields(
            agent, live=inventory.get(tmuxio.agent_inventory_key(agent), {})))
    return agents


def _status_monitor_once():
    """Run one best-effort status cycle without requiring a browser request."""
    try:
        agents, _malformed = gs.partition_operational_agents(gs.list_agents())
        _enrich_live_status(agents)
        _status_transitions(agents)
        return True
    except Exception:
        # MorphDB/tmux may be restarting. The daemon loop retries; status
        # monitoring must never take down the operator control plane.
        return False


def _status_monitor_loop():
    """Watch agent transitions for the lifetime of the dashboard process."""
    while True:
        _status_monitor_once()
        time.sleep(4.0)


# --------------------------------------------------------------------------- #
# graph snapshot — what the dashboard polls
# --------------------------------------------------------------------------- #
def _latest_edge_messages(edges):
    """Newest ACCEPTED message per edge, for the graph's edge glow + hover
    tooltip. Refusal-audit rows (blocked/ratelimited/budget*/filtered) never
    count — they were never authorized to flow. Best-effort: a store error
    degrades to no enrichment, never a failed snapshot."""
    if not edges:
        return {}
    try:
        rows = gs.list_objects(
            "message", limit=200, sort="created_at",
            order="desc")["objects"]
    except gs.GraphError:
        return {}
    refused = set(getattr(gs, "REFUSAL_STATUSES", ()))
    latest = {}
    for row in rows:
        edge_guid = row.get("edge_guid")
        if not edge_guid or edge_guid in latest:
            continue
        if row.get("status") in refused:
            continue
        latest[edge_guid] = {
            "at": row.get("created_at"),
            "from": row.get("sender"),
            "to": row.get("target"),
            "status": row.get("status"),
            "preview": (row.get("body") or "")[:140],
        }
    return latest


def _graph_snapshot():
    """agents (enriched with live tmux status) + edges (names resolved). ONLY
    crew-managed agents — the dashboard deliberately ignores every other claude
    session on the box: it never lists them, never attaches to them, and so never
    resizes a terminal the user is running independently of crew."""
    try:
        agents, _malformed = gs.partition_operational_agents(gs.list_agents())
        edges = gs.list_edges()
    except gs.GraphError as e:
        return {"ok": False, "error": str(e)}
    by_guid = {a["_guid"]: a for a in agents}
    _enrich_live_status(agents)
    _status_transitions(agents)
    last_messages = _latest_edge_messages(edges)
    for e in edges:
        e["source_name"] = (by_guid.get(e.get("source")) or {}).get("name")
        e["target_name"] = (by_guid.get(e.get("target")) or {}).get("name")
        lm = last_messages.get(e.get("_guid"))
        if lm:
            e["last_message"] = lm
    # WAVE 4: pending_count lets the UI badge the tray off the SAME poll it
    # already runs (no second endpoint hit just to know whether to show a
    # badge) — the row DATA itself is fetched separately (GET /api/pending),
    # only when the tray is actually opened.
    try:
        pending_count = len(_pending_rows())
    except gs.GraphError:
        pending_count = 0
    return {
        "ok": True,
        "workspace_key": config.current_app(),
        "agents": agents,
        "edges": edges,
        "pending_count": pending_count,
    }


# --------------------------------------------------------------------------- #
# WAVE 4: the pending-approval tray
# --------------------------------------------------------------------------- #
def _pending_rows():
    rows = []
    for result in guard.PENDING_ATTENTION_RESULTS:
        response = gs.list_objects(
            "graph_edit", result=result, sort="created_order",
            order="desc", limit=200)
        rows.extend((response or {}).get("objects", []))
    return sorted(
        {row.get("_guid"): row for row in rows if row.get("_guid")}.values(),
        key=lambda row: (
            row.get("created_order") or 0, row.get("created_at") or 0),
        reverse=True)[:200]


def _pending_summary(row, by_guid):
    """Human-readable one-liner for a pending row, resolving connect's raw
    source/target guids to names via the SAME agent list the snapshot already
    built (avoids an extra round trip per row)."""
    op = row.get("op")
    args = row.get("args") or {}
    if op == "connect":
        s = (by_guid.get(args.get("source")) or {}).get("name") or args.get("source") or "?"
        t = (by_guid.get(args.get("target")) or {}).get("name") or args.get("target") or "?"
        return f"connect {s} → {t}"
    if op == "update_edge":
        fields = args.get("fields") or {}
        chs = ", ".join(f"{k}→{v}" for k, v in fields.items())
        return f"raise edge cap(s): {chs}" if chs else "edge update"
    if op == "grant":
        agent_guid = args.get("agent_guid")
        target = by_guid.get(agent_guid) or {}
        agent_name = target.get("name") or args.get("agent") or agent_guid or "?"
        mode = args.get("mode") or "?"
        path = args.get("path") or "?"
        return f"grant {mode} access to {path} for {agent_name}"
    return op or "?"


def _pending_snapshot():
    try:
        rows = _pending_rows()
        agents, _malformed = gs.partition_operational_agents(gs.list_agents())
        by_guid = {a["_guid"]: a for a in agents}
    except gs.GraphError as e:
        return {"ok": False, "error": str(e)}
    for r in rows:
        r["summary"] = _pending_summary(r, by_guid)
    return {"ok": True, "pending": rows}


def _agent_session(agent):
    """The one tmux session this current-project agent is allowed to expose."""
    return tmuxio.canonical_agent_session(agent) or ""


def _crew_sessions():
    """Exact current-project session names the PTY endpoint may attach to.

    An agent name is not an alias for a different stored session: authorizing
    both ``demo__foo`` and ``foo`` would let a named project's dashboard attach
    to an unrelated plain-name tmux session.  Sparse legacy rows derive their
    session through the current project's normal naming rule.
    """
    try:
        sessions = tmuxio.session_names()
        agents, _malformed = gs.partition_operational_agents(gs.list_agents())
        return {
            owned for agent in agents
            if (owned := tmuxio.owned_agent_session(
                agent, sessions=sessions))
        }
    except gs.GraphError:
        return set()


# --------------------------------------------------------------------------- #
# UI WAVE B: POST /api/expand — one freeform paragraph -> structured fields
# --------------------------------------------------------------------------- #
# Turns the modal's blob textarea into the SAME fields the manual form already
# collects, by shelling out to config.expand_cmd() (a `claude -p
# --output-format json`-shaped command by default). Operator-only surface:
# every POST is gated by the dashboard's per-process capability cookie.
# Loopback binding alone is not an authority boundary because an unsandboxed
# local agent can reach localhost too.
_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def _expand_prompt(kind, text, source, target):
    """The instruction sent to the expander command's stdin. Starts with an
    all-caps marker line (AGENT-DESCRIBE / EDGE-DESCRIBE) purely so a stub
    command (tests/fixtures/expand_stub.sh) can tell which shape of JSON to
    hand back without any real language understanding — the real `claude -p`
    doesn't need it, but it's harmless context for it either way."""
    if kind == "agent":
        return (
            "AGENT-DESCRIBE\n"
            "You are helping a user describe ONE new autonomous agent from a "
            "plain-language sentence. Output STRICT JSON ONLY — no prose, no "
            "markdown code fences — with EXACTLY these keys: "
            "name (short slug-safe lowercase agent name, no spaces), "
            "role (one-line summary of what it does), "
            "identity (a short paragraph: who this agent is and what it owns).\n"
            f"Description: {text}"
        )
    return (
        "EDGE-DESCRIBE\n"
        "You are helping a user describe a messaging relationship between two "
        f"autonomous agents, '{source}' (the source) and '{target}' (the "
        "target), from a plain-language sentence. Output STRICT JSON ONLY — no "
        "prose, no markdown code fences — with EXACTLY these keys: "
        "label (short name for the relationship), "
        "conditions (array of strings: when the source should message the "
        "target), "
        "target_action (string: what the target does on receipt), "
        "reply_expected (bool), "
        "back_conditions (array of strings; empty if one-way), "
        "back_action (string; empty if one-way), "
        "back_reply (bool), "
        "directed (bool: true if one-way source->target only, false if both "
        "may message each other; MUST be false whenever either reply flag is true), "
        "max_turns (int messages/hour cap, 0 = no limit), "
        "token_cap (int hourly token budget, 0 = uncapped), "
        "cost_cap (number hourly $ budget, 0 = uncapped).\n"
        f"Description: {text}"
    )


def _expand_fallback(kind, text):
    """On ANY failure (expander errored, timed out, or returned something we
    couldn't parse) the raw text is stuffed VERBATIM into the field the user
    would expect to read it back in, so nothing they typed is lost."""
    if kind == "agent":
        return {"name": "", "role": text, "identity": text}
    return {
        "label": "", "conditions": [text], "target_action": "",
        "reply_expected": False, "back_conditions": [], "back_action": "",
        "back_reply": False, "directed": True,
        "max_turns": 0, "token_cap": 0, "cost_cap": 0,
    }


def _run_expand_cmd(prompt):
    """Shell out to config.expand_cmd() with `prompt` on stdin. Returns
    (stdout_text, None) on a clean exit, or (None, error_str) on any failure
    (nonzero exit, timeout, or the command not existing at all)."""
    cmd = config.expand_cmd()
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              timeout=config.EXPAND_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, "expander timed out"
    except Exception as e:
        return None, f"could not run expander: {e}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return None, f"expander exited {proc.returncode}" + (f": {detail}" if detail else "")
    return proc.stdout, None


def _parse_expand_output(raw):
    """`claude -p --output-format json` wraps its answer in an envelope object
    whose "result" field carries the actual text; that text is often itself
    fenced as ```json ... ```. Tolerate: a non-enveloped raw JSON blob (some
    other expander command), a missing/non-string "result", and code fences
    around the inner JSON either way. Raises on genuine garbage — the caller
    turns that into the fallback response."""
    try:
        envelope = json.loads(raw)
    except (TypeError, ValueError):
        envelope = None
    inner = envelope.get("result") if isinstance(envelope, dict) else None
    if not isinstance(inner, str):
        inner = raw if isinstance(raw, str) else str(raw)
    m = _FENCE_RE.match(inner.strip())
    inner = m.group(1) if m else inner.strip()
    fields = json.loads(inner)
    if not isinstance(fields, dict):
        raise ValueError("expander output was not a JSON object")
    return fields


def _rewrite_endpoint_identities(*agent_guids):
    """Compatibility helper for tests/extensions; failures are never hidden."""
    result = gs._rewrite_agent_identities(
        agent_guids, spawn.rewrite_identity, notify=False)
    gs._notify_agent_identity_changes(
        agent_guids, spawn.notify_connection_change)
    return result


class Handler(BaseHTTPRequestHandler):
    timeout = 15

    def version_string(self):
        """Do not disclose the stdlib HTTP server or Python runtime version."""
        return "Crew"

    def log_message(self, *a):
        pass

    def _response_has_header(self, name):
        prefix = (name + ":").lower().encode()
        return any(line.lower().startswith(prefix)
                   for line in getattr(self, "_headers_buffer", ()))

    def end_headers(self):
        """Apply one security policy to HTML, static files, JSON, and errors."""
        headers = {
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; connect-src 'self'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "font-src 'self' data:; object-src 'none'; base-uri 'none'; "
                "frame-ancestors 'none'"),
            "Referrer-Policy": "no-referrer",
            "Cache-Control": (
                "no-cache" if urlparse(self.path).path.startswith("/static/")
                else "no-store"),
        }
        for name, value in headers.items():
            if not self._response_has_header(name):
                self.send_header(name, value)
        super().end_headers()

    # ---- response helpers ---- #
    def _json(self, obj, code=200, close=False, headers=None):
        body = json.dumps(obj, allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.close_connection = True
            self.send_header("Connection", "close")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _operator_authorized(self):
        if not OPERATOR_CAPABILITY:
            return False
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            key, sep, value = part.strip().partition("=")
            if sep and key == OPERATOR_COOKIE:
                return hmac.compare_digest(value, OPERATOR_CAPABILITY)
        return False

    def _operator_forbidden(self):
        self._json({"ok": False,
                    "error": "operator capability required; open the dashboard with `crew dashboard open`"},
                   403)

    def _method_not_allowed(self, allowed):
        self._json(
            {"ok": False, "error": "method not allowed"}, 405, close=True,
            headers={"Allow": allowed})

    def _unsupported_method(self):
        path = urlparse(self.path).path
        if path in _GET_API_PATHS:
            self._method_not_allowed("GET")
        elif path in _POST_API_PATHS:
            self._method_not_allowed("POST")
        else:
            self._json({"error": "not found"}, 404, close=True)

    def _json_result(self, factory):
        """Serialize a non-streaming API operation, including clean failures."""
        try:
            result = factory()
            # Validate before sending headers. If a corrupt backend row carries
            # bytes/a set/another non-JSON value, we can still return one valid
            # error response instead of failing halfway through the response.
            json.dumps(result, allow_nan=False)
        except Exception as error:
            self._json({"ok": False, "error": str(error) or "internal server error"},
                       500)
            return
        self._json(result)

    def _require_json(self):
        media_type = self.headers.get("Content-Type", "").split(";", 1)[0]
        if media_type.strip().lower() == "application/json":
            return True
        self._json({"ok": False,
                    "error": "application/json content type required"}, 415)
        return False

    def _csrf_authorized(self):
        """Protect cookie-authenticated mutations from same-site cross-port CSRF.

        A fixed custom header is sufficient here because a foreign origin must
        preflight it and this server grants no CORS access.  When browsers send
        Origin, also bind it exactly to this request's scheme+Host so another
        localhost port is refused even though SameSite cookies ignore ports.
        """
        if self.headers.get(CSRF_HEADER) != CSRF_VALUE:
            self._json({"ok": False, "error": "CSRF header required"}, 403)
            return False
        return self._origin_authorized()

    def _origin_authorized(self):
        """Reject browser requests initiated by a different local origin.

        SameSite cookies are scoped to a site, not a port. In particular, a
        page on another localhost port receives the dashboard cookie on a
        cross-origin EventSource request. The PTY stream is stateful (it opens
        and resizes an attached tmux view), so its GET needs this check just as
        control POSTs do.
        """
        origin = self.headers.get("Origin")
        expected = f"http://{self.headers.get('Host', '')}"
        if origin and origin != expected:
            self._json({"ok": False, "error": "request Origin is not this dashboard"},
                       403)
            return False
        return True

    def _auth_bootstrap(self, data):
        supplied = self._field(data, "capability") or ""
        if (not OPERATOR_CAPABILITY
                or not hmac.compare_digest(str(supplied), OPERATOR_CAPABILITY)):
            self._operator_forbidden(); return
        self._json(
            {"ok": True},
            headers={"Set-Cookie": (
                f"{OPERATOR_COOKIE}={OPERATOR_CAPABILITY}; Path=/; "
                "HttpOnly; SameSite=Strict")})

    def _serve_static(self, rel):
        path = os.path.realpath(os.path.normpath(os.path.join(STATIC_DIR, rel)))
        if not (path == STATIC_DIR or path.startswith(STATIC_DIR + os.sep)):
            self._json({"error": "forbidden"}, 403); return
        if not os.path.isfile(path):
            self._json({"error": "not found"}, 404); return
        ext = os.path.splitext(path)[1].lower()
        ctype = _CTYPES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError as e:
            self._json({"error": str(e)}, 500); return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_index(self):
        try:
            with open(INDEX_HTML, "rb") as fh:
                body = fh.read()
        except OSError:
            self._json({"error": "index.html not found in static/"}, 500); return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- GET ---- #
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        if path in _POST_API_PATHS:
            self._method_not_allowed("POST")
            return
        if path == "/":
            self._serve_index()
        elif path == "/static" or path.startswith("/static/"):
            self._serve_static(path[len("/static/"):] if path != "/static" else "")
        elif path == "/api/graph/snapshot":
            if not self._operator_authorized():
                self._operator_forbidden(); return
            self._json_result(_graph_snapshot)
        elif path == "/api/health":
            self._json_result(_dashboard_health)
        elif path == "/api/pending":
            if not self._operator_authorized():
                self._operator_forbidden(); return
            self._json_result(_pending_snapshot)
        elif path == "/api/pty/windows":
            if not self._operator_authorized():
                self._operator_forbidden(); return
            q = parse_qs(u.query)
            self._pty_windows(q.get("t", [""])[0], q.get("id", [""])[0])
        elif path == "/api/pty/stream":
            if not self._operator_authorized():
                self._operator_forbidden(); return
            if not self._origin_authorized():
                return
            q = parse_qs(u.query)
            try:
                self._pty_stream(
                    q.get("t", [""])[0], q.get("cols", ["80"])[0],
                    q.get("rows", ["24"])[0])
            except Exception as error:
                # _pty_stream catches failures after SSE begins. Anything that
                # escapes here is a setup failure before response headers.
                self._json(
                    {"ok": False,
                     "error": str(error) or "PTY stream setup failed"}, 500)
        else:
            self._json({"error": "not found"}, 404)

    def _crew_live_session(self, target):
        """Resolve a session NAME to the crew-owned live session, or None.
        Same wall as _pty_stream: never operate on a session crew doesn't own."""
        sess, _, _ = (target or "").partition(":")
        try:
            agents, _malformed = gs.partition_operational_agents(gs.list_agents())
        except gs.GraphError:
            agents = []
        owned = next(
            (agent for agent in agents if _agent_session(agent) == sess), None)
        live_session = (
            tmuxio.owned_agent_session(owned) if owned is not None else None)
        if live_session is None or str(live_session) != sess:
            return None
        return live_session

    # ---- dock tabs: windows of a crew session ---- #
    def _pty_windows(self, target, view_id):
        """List the docked session's tmux windows for the tab bar. `id` (the
        live stream's PTY id) marks which window THAT view is showing."""
        if not target:
            self._json({"ok": False, "error": "t required"}); return
        live_session = self._crew_live_session(target)
        if live_session is None:
            self._json({"ok": False, "error": "not a crew agent session"}, 403)
            return
        windows = ptyio.list_windows(str(live_session))
        if windows is None:
            self._json({"ok": False, "error": "no such session"}, 404); return
        active = ptyio.current_window(view_id) if view_id else None
        for window in windows:
            window["active"] = window["id"] == active
        self._json({"ok": True, "windows": windows})

    # ---- SSE PTY-attach stream (verbatim from ng/ptyio) ---- #
    def _pty_stream(self, target, cols, rows):
        if not target:
            self._json({"ok": False, "error": "t required"}); return
        try:
            c = max(2, min(500, int(cols))); r = max(2, min(300, int(rows)))
        except (TypeError, ValueError):
            c, r = 80, 24
        sess, _, win = target.partition(":")
        # HARD SCOPE: only ever attach to a crew-managed session. Attaching runs a
        # grouped `tmux attach` whose resize-window changes the shared window size —
        # so attaching to a stranger's claude would resize THEIR terminal. Refuse any
        # session crew doesn't own (this is the wall behind "only manage crew here").
        try:
            agents, _malformed = gs.partition_operational_agents(gs.list_agents())
        except gs.GraphError:
            agents = []
        owned = next(
            (agent for agent in agents if _agent_session(agent) == sess), None)
        live_session = (
            tmuxio.owned_agent_session(owned) if owned is not None else None)
        if live_session is None or str(live_session) != sess:
            self._json({"ok": False, "error": "not a crew agent session"}, 403); return
        if not win:
            win = runtimes.window_name(
                runtimes.resolve_agent_runtime(owned or {"runtime": "claude"}))
        pid_id, fd = ptyio.open_attach(live_session, win)
        if not pid_id:
            self._json({"ok": False, "error": "no such session"}, 404); return
        try:
            # Close the attach if the session died/reappeared between the first
            # ownership check and tmux attach setup. Never resize or stream the
            # replacement solely because it reused the durable name.
            current_session = tmuxio.owned_agent_session(owned)
            if (current_session is None
                    or str(current_session) != sess
                    or config.tmux_target_endpoint(current_session)
                    != config.tmux_target_endpoint(live_session)):
                self._json({
                    "ok": False, "error": "not a crew agent session"}, 403)
                return
            if not ptyio.set_size(pid_id, c, r):
                self._json({
                    "ok": False, "error": "could not size PTY attach"}, 500)
                return
            try:
                self.connection.settimeout(None)
            except OSError:
                pass
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(
                    f"event: id\ndata: {pid_id}\n\n".encode())
                self.wfile.flush()
            except Exception:
                return
            csock = self.connection

            def client_alive():
                try:
                    rr, _, _ = select.select([csock], [], [], 0)
                    if rr:
                        return bool(csock.recv(1, socket.MSG_PEEK))
                except Exception:
                    return False
                return True

            def on_bytes(chunk):
                self._sse("data", chunk)

            def on_idle():
                self.wfile.write(b": hb\n\n"); self.wfile.flush()

            try:
                ptyio.read_loop(
                    pid_id, on_bytes, alive=client_alive, on_idle=on_idle)
            except Exception:
                pass
        finally:
            ptyio.close(pid_id)

    def _sse(self, event, raw_bytes):
        payload = base64.b64encode(raw_bytes).decode()
        self.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode())
        self.wfile.flush()

    # ---- POST ---- #
    def do_POST(self):
        path = urlparse(self.path).path
        if path in _GET_API_PATHS:
            self._method_not_allowed("GET")
            return
        if self.headers.get_all("Transfer-Encoding", []):
            self._json(
                {"ok": False,
                 "error": "Transfer-Encoding is not supported; use Content-Length"},
                400, close=True)
            return
        content_lengths = self.headers.get_all("Content-Length", [])
        if len(content_lengths) > 1:
            self._json(
                {"ok": False, "error": "exactly one Content-Length is allowed"},
                400, close=True)
            return
        raw_length = content_lengths[0] if content_lengths else "0"
        if not re.fullmatch(r"[0-9]+", raw_length):
            self._json(
                {"ok": False, "error": "Content-Length must be decimal digits"},
                400, close=True)
            return
        normalized_length = raw_length.lstrip("0") or "0"
        max_body_text = str(MAX_BODY)
        if (len(normalized_length) > len(max_body_text)
                or (len(normalized_length) == len(max_body_text)
                    and normalized_length > max_body_text)):
            self._json({"ok": False, "error": "request too large"},
                       413, close=True)
            return
        length = int(normalized_length)
        try:
            raw = self.rfile.read(length)
        except Exception as error:
            try:
                self._json(
                    {"ok": False, "error": f"could not read request body: {error}"},
                    400, close=True)
            except OSError:
                pass
            return
        if len(raw) != length:
            self._json(
                {"ok": False,
                 "error": "request body is shorter than Content-Length"},
                400, close=True)
            return
        json_error = None
        try:
            data = _decode_json_object(raw)
        except ValueError as error:
            data = {}
            json_error = str(error)

        if path == "/api/auth/bootstrap":
            if not self._require_json():
                return
            if json_error:
                self._json({"ok": False, "error": json_error}, 400); return
            invalid_text = _invalid_text_field(path, data)
            if invalid_text:
                self._json(
                    {"ok": False,
                     "error": f"{invalid_text} must be a JSON string"},
                    400)
                return
            self._auth_bootstrap(data); return
        if not self._operator_authorized():
            self._operator_forbidden(); return
        if not self._require_json() or not self._csrf_authorized():
            return
        if json_error:
            self._json({"ok": False, "error": json_error}, 400); return

        invalid_text = _invalid_text_field(path, data)
        if invalid_text:
            self._json(
                {"ok": False,
                 "error": f"{invalid_text} must be a JSON string"},
                400)
            return

        invalid_text_list = _invalid_text_list_field(path, data)
        if invalid_text_list:
            self._json(
                {"ok": False,
                 "error": (f"{invalid_text_list} must be a JSON array "
                           "containing only strings")},
                400)
            return

        invalid_boolean = _invalid_boolean_field(path, data)
        if invalid_boolean:
            self._json(
                {"ok": False,
                 "error": f"{invalid_boolean} must be a JSON boolean"},
                400)
            return

        try:
            self._dispatch_post(path, data)
        except Exception as error:
            # Every non-streaming control request has a JSON response contract.
            # Backends and runtime adapters can fail in ways more general than
            # GraphError; never turn those failures into a dropped socket.
            self._json({"ok": False,
                        "error": str(error) or "internal server error"}, 500)

    def _dispatch_post(self, path, data):
        """Dispatch one authenticated, validated non-streaming control POST."""

        # --- terminal transport (verbatim) --- #
        if path == "/api/pty/input":
            try:
                pid_id = _required_string(data, "id")
                buf = _decode_pty_input(data)
            except ValueError as error:
                self._json({"ok": False, "error": str(error)}, 400)
                return
            self._json({"ok": ptyio.write_input(pid_id, buf)})
        elif path == "/api/pty/resize":
            try:
                pid_id = _required_string(data, "id")
                cols = _required_int(data, "cols", 2, 500)
                rows = _required_int(data, "rows", 2, 300)
            except ValueError as error:
                self._json({"ok": False, "error": str(error)}, 400)
                return
            ok = ptyio.set_size(pid_id, cols, rows)
            self._json({"ok": ok})
        elif path == "/api/pty/window/create":
            try:
                target = _required_string(data, "t")
            except ValueError as error:
                self._json({"ok": False, "error": str(error)}, 400)
                return
            live_session = self._crew_live_session(target)
            if live_session is None:
                self._json(
                    {"ok": False, "error": "not a crew agent session"}, 403)
                return
            window = ptyio.create_window(str(live_session))
            if not window:
                self._json({"ok": False, "error": "could not create window"}, 500)
                return
            self._json({"ok": True, "window": window})
        elif path == "/api/pty/window/select":
            try:
                pid_id = _required_string(data, "id")
                window = _required_string(data, "window")
            except ValueError as error:
                self._json({"ok": False, "error": str(error)}, 400)
                return
            self._json({"ok": ptyio.select_window(pid_id, window)})
        # --- agent graph mutations --- #
        elif path == "/api/agent/create":
            self._agent_create(data)
        elif path == "/api/agent/start":
            self._agent_start(data)
        elif path == "/api/agent/remove":
            self._agent_remove(data)
        elif path == "/api/edge/create":
            self._edge_create(data)
        elif path == "/api/edge/update":
            self._edge_update(data)
        elif path == "/api/edge/delete":
            self._edge_delete(data)
        elif path == "/api/agent/bless":
            self._agent_bless(data)
        elif path == "/api/edge/bless":
            self._edge_bless(data)
        elif path == "/api/agent/foreman":
            self._agent_foreman(data)
        elif path == "/api/pending/approve":
            self._pending_approve(data)
        elif path == "/api/pending/reject":
            self._pending_reject(data)
        elif path == "/api/expand":
            self._expand(data)
        else:
            self._json({"error": "not found"}, 404)

    do_PUT = _unsupported_method
    do_PATCH = _unsupported_method
    do_DELETE = _unsupported_method
    do_OPTIONS = _unsupported_method
    do_HEAD = _unsupported_method

    # ---- agent graph handlers ---- #
    def _agent_create(self, data):
        f = lambda k: self._field(data, k)
        name = (f("name") or "").strip()
        if not name:
            self._json({"ok": False, "error": "name required"}); return
        try:
            agent = spawn.spawn_agent(
                name, role=f("role") or "", agent_identity=f("identity") or "",
                home=f("home") or None, repo=f("repo") or None,
                launch=bool(data.get("launch", True)),
                launch_cmd=f("launch_cmd") or None,
                runtime=f("runtime") or None, actor="human")
            self._json({"ok": True, "agent": agent})
        except gs.GraphError as e:
            self._json({"ok": False, "error": str(e)})
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)

    def _agent_start(self, data):
        """Revive a 'down' agent: (re)create its tmux session + relaunch claude in
        its home. The agent record already exists — only its live session died."""
        name = (self._field(data, "name") or "").strip()
        if not name:
            self._json({"ok": False, "error": "name required"}); return
        try:
            agent = spawn.start_session(name, actor="human")
            self._json({"ok": True, "agent": agent})
        except gs.GraphError as e:
            self._json({"ok": False, "error": str(e)})
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)

    def _agent_remove(self, data):
        name = (self._field(data, "name") or "").strip()
        if not name:
            self._json({"ok": False, "error": "name required"}); return
        try:
            spawn.remove_agent(name, kill_session=bool(data.get("kill_session", True)),
                               actor="human")
            self._json({"ok": True})
        except gs.GraphError as e:
            self._json({"ok": False, "error": str(e)})

    def _resolve_agent_ref(self, ref):
        """A UI edge endpoint may arrive as an agent name OR a guid. Resolve to the
        agent dict (name first, since names are the human-facing handle)."""
        if not ref:
            return None
        a = gs.get_agent_by_name(ref)
        if a:
            return a
        try:
            return gs.get_object(ref)
        except gs.GraphError:
            return None

    def _edge_create(self, data):
        f = lambda k: self._field(data, k)
        src = self._resolve_agent_ref(f("source"))
        tgt = self._resolve_agent_ref(f("target"))
        if not src or not tgt:
            self._json({"ok": False, "error": "source and target must be existing agents"}); return
        try:
            caps = gs.normalize_edge_numeric_fields({
                "max_turns": data.get("max_turns", 0),
                "token_cap": data.get("token_cap", 0),
                "cost_cap": data.get("cost_cap", 0),
            })
            edge = gs.create_edge(
                src["_guid"], tgt["_guid"], label=f("label") or "",
                description=f("description") or "",
                conditions=data.get("conditions"), condition=f("condition") or "",
                target_action=f("target_action") or "",
                reply_expected=bool(data.get("reply_expected", False)),
                back_conditions=data.get("back_conditions"),
                back_action=f("back_action") or "",
                back_reply=bool(data.get("back_reply", False)),
                max_turns=caps["max_turns"],
                token_cap=caps["token_cap"],
                cost_cap=caps["cost_cap"],
                directed=bool(data.get("directed", True)), actor="human",
                _identity_rewriter=spawn.rewrite_identity,
                _identity_notifier=spawn.notify_connection_change)
            self._json({"ok": True, "edge": edge})
        except gs.GraphError as e:
            self._json({"ok": False, "error": str(e)})

    def _edge_update(self, data):
        guid = self._field(data, "guid") or ""
        if not guid:
            self._json({"ok": False, "error": "guid required"}); return
        body = {}
        for k in ("label", "description", "target_action", "back_action"):
            v = self._field(data, k)
            if v is not None:
                body[k] = v
        for k in ("conditions", "back_conditions"):
            if isinstance(data.get(k), list):
                body[k] = data.get(k)
        for k in ("reply_expected", "back_reply", "directed"):
            if k in data:
                body[k] = bool(data.get(k))
        for k in ("max_turns", "token_cap", "cost_cap"):
            if k in data:
                body[k] = data.get(k)
        try:
            body = gs.normalize_edge_numeric_fields(body)
        except gs.GraphError as e:
            self._json({"ok": False, "error": str(e)}); return
        try:
            gs.get_object(guid)  # MorphDB's PATCH upserts unknown guids instead of
            # 404ing, so confirm the edge exists first rather than let update_edge
            # silently create a phantom source:null/target:null edge.
        except gs.GraphError:
            self._json({"ok": False, "error": f"no such edge: {guid}"}); return
        try:
            edge = gs.update_edge(
                guid, body, actor="human",
                _identity_rewriter=spawn.rewrite_identity,
                _identity_notifier=spawn.notify_connection_change)
            self._json({"ok": True, "edge": edge})
        except gs.GraphError as e:
            self._json({"ok": False, "error": str(e)})

    def _edge_delete(self, data):
        guid = self._field(data, "guid") or ""
        if not guid:
            self._json({"ok": False, "error": "guid required"}); return
        try:
            gs.get_object(guid)
            gs.delete_edge(
                guid, actor="human",
                _identity_rewriter=spawn.rewrite_identity,
                _identity_notifier=spawn.notify_connection_change)
            self._json({"ok": True})
        except gs.GraphError as e:
            self._json({"ok": False, "error": str(e)})

    # ---- WAVE 3: bless + foreman handlers (all human-only, actor="human") ---- #
    def _agent_bless(self, data):
        name = (self._field(data, "name") or "").strip()
        if not name:
            self._json({"ok": False, "error": "name required"}); return
        ag = gs.get_agent_by_name(name)
        if not ag:
            self._json({"ok": False, "error": f"no such agent: {name}"}); return
        try:
            gs.bless_agent(ag["_guid"], actor="human")
            self._json({"ok": True})
        except gs.GraphError as e:
            self._json({"ok": False, "error": str(e)})

    def _edge_bless(self, data):
        guid = self._field(data, "guid") or ""
        if not guid:
            self._json({"ok": False, "error": "guid required"}); return
        try:
            gs.bless_edge(guid, actor="human")
            self._json({"ok": True})
        except gs.GraphError as e:
            self._json({"ok": False, "error": str(e)})

    def _agent_foreman(self, data):
        name = (self._field(data, "name") or "").strip()
        if not name:
            self._json({"ok": False, "error": "name required"}); return
        revoke = bool(data.get("revoke", False))
        ag = gs.get_agent_by_name(name)
        if not ag:
            self._json({"ok": False, "error": f"no such agent: {name}"}); return
        try:
            gs.set_foreman(
                ag["_guid"], revoke=revoke, actor="human",
                _identity_rewriter=spawn.rewrite_identity)
            self._json({"ok": True})
        except gs.GraphError as e:
            self._json({"ok": False, "error": str(e)})

    # ---- WAVE 4: pending-approval tray (approve/reject are human-only server-side) ---- #
    def _pending_approve(self, data):
        guid = (self._field(data, "guid") or "").strip()
        if not guid:
            self._json({"ok": False, "error": "guid required"}); return
        try:
            guard.approve_pending(guid, actor="human")
            self._json({"ok": True})
        except gs.GraphError as e:
            self._json({"ok": False, "error": str(e)})
        except Exception as e:
            # A hand-written/corrupt pending row (e.g. args.fields not even a
            # dict) can raise something other than GraphError deep inside the
            # replay (graphstore.update_edge's `dict(fields)`, for one) — that
            # must still come back as a clean JSON error, not drop the
            # connection, same defensive fallback as _agent_create/_agent_start.
            self._json({"ok": False, "error": str(e)}, 500)

    def _pending_reject(self, data):
        guid = (self._field(data, "guid") or "").strip()
        if not guid:
            self._json({"ok": False, "error": "guid required"}); return
        reason = self._field(data, "reason") or ""
        try:
            guard.reject_pending(guid, reason=reason, actor="human")
            self._json({"ok": True})
        except gs.GraphError as e:
            self._json({"ok": False, "error": str(e)})
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)

    # ---- UI WAVE B: one-blob expansion ---- #
    def _expand(self, data):
        f = lambda k: self._field(data, k)
        kind = (f("kind") or "").strip()
        if kind not in ("edge", "agent"):
            self._json({"ok": False, "error": "kind must be 'edge' or 'agent'"}); return
        text = (f("text") or "").strip()
        if not text:
            self._json({"ok": False, "error": "text required"}); return
        source, target = f("source") or "", f("target") or ""
        prompt = _expand_prompt(kind, text, source, target)
        raw, err = _run_expand_cmd(prompt)
        if err is not None:
            self._json({"ok": False, "error": err, "fallback": _expand_fallback(kind, text)}); return
        try:
            fields = _parse_expand_output(raw)
        except Exception as e:
            self._json({"ok": False, "error": f"could not parse expander output: {e}",
                       "fallback": _expand_fallback(kind, text)}); return
        self._json({"ok": True, "fields": fields})

    @staticmethod
    def _field(data, key):
        v = data.get(key)
        if v is None or isinstance(v, (dict, list)):
            return None
        if isinstance(v, bool):
            return None
        return v if isinstance(v, str) else str(v)


def _flusher_loop():
    """Background: deliver queued agent messages whose target has become idle. This
    is what turns 'target was busy' from a dropped message into a retried one."""
    while True:
        time.sleep(4.0)
        try:
            mail.flush_queued()
        except Exception:
            pass


def main():
    print(f"crew dashboard → http://{HOST}:{PORT}  (Ctrl-C to stop)")
    print(f"data: MorphDB app '{config.current_app()}' at {config.morphdb_base()}")
    try:
        ptyio.reap_stale()
    except Exception:
        pass
    threading.Thread(target=_flusher_loop, daemon=True).start()
    threading.Thread(target=_status_monitor_loop, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
