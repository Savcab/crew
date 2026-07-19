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
attach to, or resize any other claude session on the box — so an independent
`claude` you started yourself is never touched (no surprise window resizes).

Binds 127.0.0.1 ONLY — this is remote control of your terminals. Port 8788 by
default (MorphDB owns 8787), overridable via $CREW_PORT.

  Run:  python3 -m crew.server.app   then open http://127.0.0.1:8788
"""
import base64
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
from .. import config, graphstore as gs, guard, spawn, mail
from ..notify import notify

HOST = config.DASHBOARD_HOST
PORT = config.DASHBOARD_PORT

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.realpath(os.path.join(HERE, "..", "..", "static"))
INDEX_HTML = os.path.join(STATIC_DIR, "index.html")

MAX_BODY = 1 << 20

_CTYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon",
    ".map": "application/json; charset=utf-8",
}


# --------------------------------------------------------------------------- #
# status-transition notifications — the "silent overnight death" fix
# --------------------------------------------------------------------------- #
# Previous live_status per agent, so the snapshot builder (the ONE place that
# derives status) can fire crew.notify on TRANSITIONS only — never for a steady
# state. First sight of an agent SEEDS the dict without firing, so a dashboard
# restart doesn't re-announce every already-down agent. A per-agent+event rate
# guard (60s) keeps a flapping pane from repeating the same alert. Agents gone
# from the graph are pruned from both dicts, so churn can't grow them forever.
_prev_status = {}
_last_notify = {}          # (name, event) → monotonic ts of last webhook
_NOTIFY_GAP = 60.0
_notify_lock = threading.Lock()


def _status_transitions(agents):
    """Notify the operator when an agent transitions TO down (its claude/session
    died) or TO needs_input (sitting on a permission prompt). Transition
    detection runs under the lock; the webhook POSTs fire from a daemon thread
    AFTER it's released, so a slow webhook never delays the snapshot response
    (nor other pollers queued on the lock)."""
    pending = []
    with _notify_lock:
        live = {a.get("name") for a in agents}
        for gone in [n for n in _prev_status if n not in live]:
            del _prev_status[gone]
        for k in [k for k in _last_notify if k[0] not in live]:
            del _last_notify[k]
        for a in agents:
            name, status = a.get("name"), a.get("live_status")
            if not name:
                continue
            prev = _prev_status.get(name)
            _prev_status[name] = status
            if prev is None or status == prev:
                continue        # seeding, or steady state
            if status == "down":
                event = "agent_down"
                detail = f"session '{a.get('session') or name}' died (was {prev})"
            elif status == "needs_input":
                event = "needs_input"
                detail = f"waiting on a permission prompt (was {prev})"
            else:
                continue
            now = time.monotonic()
            last = _last_notify.get((name, event))
            if last is not None and now - last < _NOTIFY_GAP:
                continue
            _last_notify[(name, event)] = now
            pending.append((event, name, detail))
    if pending:
        threading.Thread(target=lambda: [notify(*p) for p in pending],
                         daemon=True).start()


# --------------------------------------------------------------------------- #
# graph snapshot — what the dashboard polls
# --------------------------------------------------------------------------- #
def _graph_snapshot():
    """agents (enriched with live tmux status) + edges (names resolved). ONLY
    crew-managed agents — the dashboard deliberately ignores every other claude
    session on the box: it never lists them, never attaches to them, and so never
    resizes a terminal the user is running independently of crew."""
    try:
        agents = gs.list_agents()
        edges = gs.list_edges()
    except gs.GraphError as e:
        return {"ok": False, "error": str(e)}
    by_guid = {a["_guid"]: a for a in agents}
    pane_map = tmuxio._session_pane_map(force=True)
    for a in agents:
        sess = a.get("session") or a.get("name")
        alive = sess in pane_map
        a["alive"] = alive
        a["live_status"] = (
            tmuxio.detect_status(tmuxio.capture_frame(pane_map[sess])) if alive else "down")
    _status_transitions(agents)
    for e in edges:
        e["source_name"] = (by_guid.get(e.get("source")) or {}).get("name")
        e["target_name"] = (by_guid.get(e.get("target")) or {}).get("name")
    # WAVE 4: pending_count lets the UI badge the tray off the SAME poll it
    # already runs (no second endpoint hit just to know whether to show a
    # badge) — the row DATA itself is fetched separately (GET /api/pending),
    # only when the tray is actually opened.
    try:
        pending_count = len(_pending_rows())
    except gs.GraphError:
        pending_count = 0
    return {"ok": True, "agents": agents, "edges": edges, "pending_count": pending_count}


# --------------------------------------------------------------------------- #
# WAVE 4: the pending-approval tray
# --------------------------------------------------------------------------- #
def _pending_rows():
    res = gs.list_objects("graph_edit", result="pending", sort="created_at",
                          order="desc", limit=200)
    return (res or {}).get("objects", [])


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
    return op or "?"


def _pending_snapshot():
    try:
        rows = _pending_rows()
        by_guid = {a["_guid"]: a for a in gs.list_agents()}
    except gs.GraphError as e:
        return {"ok": False, "error": str(e)}
    for r in rows:
        r["summary"] = _pending_summary(r, by_guid)
    return {"ok": True, "pending": rows}


def _crew_sessions():
    """The set of session names crew owns (so the PTY endpoint can refuse to attach
    to anything else). Both the registered session AND the agent name, since a bare
    name is a valid target. Empty set if MorphDB is unreachable → attach refused."""
    try:
        out = set()
        for a in gs.list_agents():
            out.add(a.get("session") or a.get("name"))
            out.add(a.get("name"))
        return {s for s in out if s}
    except gs.GraphError:
        return set()


# --------------------------------------------------------------------------- #
# UI WAVE B: POST /api/expand — one freeform paragraph -> structured fields
# --------------------------------------------------------------------------- #
# Turns the modal's blob textarea into the SAME fields the manual form already
# collects, by shelling out to config.expand_cmd() (a `claude -p
# --output-format json`-shaped command by default). Human-only surface: this
# only exists behind the dashboard's loopback-bound HTTP port, which no agent
# can reach (agents talk to crew over the CLI / crew.mail, never HTTP), so —
# same as the PTY transport above — there is no guard.check()/actor to gate;
# it's human-only by construction, not by a runtime check.
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
        "may message each other), "
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
    """After an edge changes, refresh identity.md (and nudge the live session) for
    each endpoint so every agent's "who I may message" list stays truthful."""
    for g in set(filter(None, agent_guids)):
        try:
            spawn.rewrite_identity(gs.get_object(g), notify=True)
        except gs.GraphError:
            pass


class Handler(BaseHTTPRequestHandler):
    timeout = 15

    def log_message(self, *a):
        pass

    # ---- response helpers ---- #
    def _json(self, obj, code=200, close=False):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

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
        if path == "/":
            self._serve_index()
        elif path == "/static" or path.startswith("/static/"):
            self._serve_static(path[len("/static/"):] if path != "/static" else "")
        elif path == "/api/graph/snapshot":
            self._json(_graph_snapshot())
        elif path == "/api/pending":
            self._json(_pending_snapshot())
        elif path == "/api/pty/stream":
            q = parse_qs(u.query)
            self._pty_stream(q.get("t", [""])[0],
                             q.get("cols", ["80"])[0], q.get("rows", ["24"])[0])
        else:
            self._json({"error": "not found"}, 404)

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
        if sess not in _crew_sessions():
            self._json({"ok": False, "error": "not a crew agent session"}, 403); return
        pid_id, fd = ptyio.open_attach(sess, win or "claude")
        if not pid_id:
            self._json({"ok": False, "error": "no such session"}, 404); return
        ptyio.set_size(pid_id, c, r)
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
            self.wfile.write(f"event: id\ndata: {pid_id}\n\n".encode()); self.wfile.flush()
        except Exception:
            ptyio.close(pid_id); return
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
            ptyio.read_loop(pid_id, on_bytes, alive=client_alive, on_idle=on_idle)
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
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        if length < 0 or length > MAX_BODY:
            self._json({"error": "request too large"}, 413, close=True); return
        try:
            raw = self.rfile.read(length)
        except Exception:
            return
        try:
            data = json.loads(raw or b"{}")
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}

        # --- terminal transport (verbatim) --- #
        if path == "/api/pty/input":
            pid_id = self._field(data, "id") or ""
            try:
                buf = base64.b64decode(self._field(data, "b64") or "")
            except Exception:
                buf = b""
            self._json({"ok": ptyio.write_input(pid_id, buf) if pid_id else False})
        elif path == "/api/pty/resize":
            pid_id = self._field(data, "id") or ""
            ok = ptyio.set_size(pid_id, data.get("cols", 80), data.get("rows", 24)) if pid_id else False
            self._json({"ok": ok})
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
                launch_cmd=f("launch_cmd") or None, actor="human")
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
            edge = gs.create_edge(
                src["_guid"], tgt["_guid"], label=f("label") or "",
                description=f("description") or "",
                conditions=data.get("conditions"), condition=f("condition") or "",
                target_action=f("target_action") or "",
                reply_expected=bool(data.get("reply_expected", False)),
                back_conditions=data.get("back_conditions"),
                back_action=f("back_action") or "",
                back_reply=bool(data.get("back_reply", False)),
                max_turns=int(data.get("max_turns") or 0),
                token_cap=int(data.get("token_cap") or 0),
                cost_cap=float(data.get("cost_cap") or 0),
                directed=bool(data.get("directed", True)), actor="human")
            _rewrite_endpoint_identities(src["_guid"], tgt["_guid"])
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
        if "max_turns" in data:
            try:
                body["max_turns"] = int(data.get("max_turns") or 0)
            except (TypeError, ValueError):
                pass
        if "token_cap" in data:
            try:
                body["token_cap"] = int(data.get("token_cap") or 0)
            except (TypeError, ValueError):
                pass
        if "cost_cap" in data:
            try:
                body["cost_cap"] = float(data.get("cost_cap") or 0)
            except (TypeError, ValueError):
                pass
        try:
            gs.get_object(guid)  # MorphDB's PATCH upserts unknown guids instead of
            # 404ing, so confirm the edge exists first rather than let update_edge
            # silently create a phantom source:null/target:null edge.
        except gs.GraphError:
            self._json({"ok": False, "error": f"no such edge: {guid}"}); return
        try:
            edge = gs.update_edge(guid, body, actor="human")
            _rewrite_endpoint_identities(edge.get("source"), edge.get("target"))
            self._json({"ok": True, "edge": edge})
        except gs.GraphError as e:
            self._json({"ok": False, "error": str(e)})

    def _edge_delete(self, data):
        guid = self._field(data, "guid") or ""
        if not guid:
            self._json({"ok": False, "error": "guid required"}); return
        try:
            edge = gs.get_object(guid)
            src, tgt = edge.get("source"), edge.get("target")
            gs.delete_edge(guid, actor="human")
            _rewrite_endpoint_identities(src, tgt)
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
            guard.check("human", "foreman", name=name, revoke=revoke)
            gs.update_agent(ag["_guid"], can_edit_graph=not revoke, actor="human")
            guard.audit("human", "foreman", {"name": name, "revoke": revoke}, "applied")
            spawn.rewrite_identity(gs.get_object(ag["_guid"]), notify=True)
            self._json({"ok": True})
        except gs.GraphError as e:
            self._json({"ok": False, "error": str(e)})

    # ---- WAVE 4: pending-approval tray (approve/reject are human-only server-side) ---- #
    def _pending_approve(self, data):
        guid = (self._field(data, "guid") or "").strip()
        if not guid:
            self._json({"ok": False, "error": "guid required"}); return
        try:
            row = guard.approve_pending(guid, actor="human")
            args = row.get("args") or {}
            # refresh identity.md on both endpoints, same as every other edge
            # mutation (_edge_create/_edge_update/_edge_delete) — the created/
            # updated edge just changed who may message whom.
            if row.get("op") == "connect":
                _rewrite_endpoint_identities(args.get("source"), args.get("target"))
            elif row.get("op") == "update_edge":
                try:
                    edge = gs.get_object(args.get("guid"))
                    _rewrite_endpoint_identities(edge.get("source"), edge.get("target"))
                except gs.GraphError:
                    pass
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
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
