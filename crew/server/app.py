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
import select
import socket
import threading
import time

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import tmuxio, ptyio
from .. import config, graphstore as gs, spawn, mail

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
    for e in edges:
        e["source_name"] = (by_guid.get(e.get("source")) or {}).get("name")
        e["target_name"] = (by_guid.get(e.get("target")) or {}).get("name")
    return {"ok": True, "agents": agents, "edges": edges}


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
        elif path == "/api/agent/remove":
            self._agent_remove(data)
        elif path == "/api/agent/say":
            self._agent_say(data)
        elif path == "/api/edge/create":
            self._edge_create(data)
        elif path == "/api/edge/update":
            self._edge_update(data)
        elif path == "/api/edge/delete":
            self._edge_delete(data)
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
                launch_cmd=f("launch_cmd") or None)
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
            spawn.remove_agent(name, kill_session=bool(data.get("kill_session", True)))
            self._json({"ok": True})
        except gs.GraphError as e:
            self._json({"ok": False, "error": str(e)})

    def _agent_say(self, data):
        """Operator → agent: seed/kick the docked agent with a message. This is the
        USER messaging their own agent (not peer mail), so it bypasses the edge gate
        — but it's still readiness-gated so it never fires Enter into a busy pane."""
        name = (self._field(data, "name") or "").strip()
        text = (self._field(data, "text") or "").strip()
        if not name or not text:
            self._json({"ok": False, "error": "name and text required"}); return
        try:
            ok, msg = mail.say_to_agent(name, text)
            self._json({"ok": ok, "message": msg})
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
                description=f("description") or "", condition=f("condition") or "",
                target_action=f("target_action") or "",
                reply_expected=bool(data.get("reply_expected", False)),
                max_turns=int(data.get("max_turns") or 0),
                directed=bool(data.get("directed", True)))
            _rewrite_endpoint_identities(src["_guid"], tgt["_guid"])
            self._json({"ok": True, "edge": edge})
        except gs.GraphError as e:
            self._json({"ok": False, "error": str(e)})

    def _edge_update(self, data):
        guid = self._field(data, "guid") or ""
        if not guid:
            self._json({"ok": False, "error": "guid required"}); return
        body = {}
        for k in ("label", "description", "condition", "target_action"):
            v = self._field(data, k)
            if v is not None:
                body[k] = v
        if "directed" in data:
            body["directed"] = bool(data.get("directed"))
        if "reply_expected" in data:
            body["reply_expected"] = bool(data.get("reply_expected"))
        if "max_turns" in data:
            try:
                body["max_turns"] = int(data.get("max_turns") or 0)
            except (TypeError, ValueError):
                pass
        try:
            edge = gs.patch_object("edge", guid, body)
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
            gs.delete_edge(guid)
            _rewrite_endpoint_identities(src, tgt)
            self._json({"ok": True})
        except gs.GraphError as e:
            self._json({"ok": False, "error": str(e)})

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
