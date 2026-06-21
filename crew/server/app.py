#!/usr/bin/env python3
"""crew dashboard server — ThreadingHTTPServer + router.

Serves the static SPA (static/) and a JSON/SSE API. The terminal transport
(tmux PTY-attach → xterm) and the tmux/shell endpoints are ported verbatim from
the tuned `ng` stack (ptyio/tmuxio) — that part was hand-tuned and we keep it.

What's NEW vs the old crew dashboard: the data API is the AGENT GRAPH, not a task
board. It talks straight to crew.graphstore / crew.spawn (same process, in-Python
— no CLI shell-out), so the surfaces are:

  GET  /api/graph/snapshot         agents + edges + live tmux status + independents
  POST /api/agent/create           spawn a new agent (home-uniqueness enforced)
  POST /api/agent/adopt            adopt an independent claude session as an agent
  POST /api/agent/remove           delete an agent
  POST /api/edge/create|update|delete   connect / edit / disconnect two agents

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

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import tmuxio, gitpr, ptyio
from .. import config, graphstore as gs, spawn

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
    """agents (enriched with live tmux status) + edges (names resolved) + every
    independent claude session on the box (so the graph is a full census)."""
    try:
        agents = gs.list_agents()
        edges = gs.list_edges()
    except gs.GraphError as e:
        return {"ok": False, "error": str(e)}
    by_guid = {a["_guid"]: a for a in agents}
    pane_map = tmuxio._session_pane_map(force=True)
    known = set()
    for a in agents:
        sess = a.get("session") or a.get("name")
        known.add(a.get("name"))
        known.add(sess)
        alive = sess in pane_map
        a["alive"] = alive
        a["live_status"] = (
            tmuxio.detect_status(tmuxio.capture_frame(pane_map[sess])) if alive else "down")
    for e in edges:
        e["source_name"] = (by_guid.get(e.get("source")) or {}).get("name")
        e["target_name"] = (by_guid.get(e.get("target")) or {}).get("name")
    indep = tmuxio.independent_sessions(known)
    return {"ok": True, "agents": agents, "edges": edges, "independent": indep}


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
        elif path == "/api/sessions":
            self._sessions()
        elif path == "/api/graph/snapshot":
            self._json(_graph_snapshot())
        elif path == "/api/pty/stream":
            q = parse_qs(u.query)
            self._pty_stream(q.get("t", [""])[0],
                             q.get("cols", ["80"])[0], q.get("rows", ["24"])[0])
        elif path == "/api/pr":
            url = parse_qs(u.query).get("url", [""])[0]
            if not url:
                self._json({"ok": False, "error": "url required"}); return
            self._json(gitpr.gh_pr(url))
        elif path == "/api/diff":
            self._diff(parse_qs(u.query))
        else:
            self._json({"error": "not found"}, 404)

    def _sessions(self):
        panes = tmuxio.list_claude_panes()
        for p in panes:
            p["status"] = tmuxio.detect_status(tmuxio.capture_frame(p["target"]))
        self._json({"sessions": panes})

    def _diff(self, q):
        t = q.get("t", [""])[0]
        if not t:
            self._json({"ok": False, "error": "t required"}); return
        t = tmuxio.resolve_target(t)
        ok_p, cwd = tmuxio.tmux("display-message", "-t", t, "-p", "#{pane_current_path}")
        if not ok_p or not cwd.strip():
            self._json({"ok": False, "error": "could not resolve worktree path"}); return
        cwd = cwd.strip()
        res = gitpr.git_diff(cwd, base=q.get("base", [""])[0] or None)
        if res.get("ok") and q.get("pr", ["1"])[0] == "1":
            try:
                res["pr"] = gitpr.gh_pr_for_worktree(res.get("root") or cwd)
            except Exception:
                res["pr"] = None
        self._json(res)

    # ---- SSE PTY-attach stream (verbatim from ng/ptyio) ---- #
    def _pty_stream(self, target, cols, rows):
        if not target:
            self._json({"ok": False, "error": "t required"}); return
        try:
            c = max(2, min(500, int(cols))); r = max(2, min(300, int(rows)))
        except (TypeError, ValueError):
            c, r = 80, 24
        sess, _, win = target.partition(":")
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
        elif path == "/api/send":
            t = self._field(data, "t") or ""
            if not t: self._json({"ok": False, "err": "t required"}); return
            t = tmuxio.resolve_target(t)
            ok, err = tmuxio.send(t, self._field(data, "keys") or "", bool(data.get("enter", True)))
            self._json({"ok": ok, "err": err})
        elif path == "/api/key":
            t = self._field(data, "t") or ""
            if not t: self._json({"ok": False, "err": "t required"}); return
            t = tmuxio.resolve_target(t)
            ok, err = tmuxio.send_key(t, self._field(data, "key") or "")
            self._json({"ok": ok, "err": err})
        elif path == "/api/resize":
            self._json({"ok": True})  # deprecated no-op (PTY transport resizes itself)
        elif path == "/api/broadcast":
            keys = data.get("keys", ""); results = []
            for p in tmuxio.list_claude_panes():
                ok, err = tmuxio.send(p["target"], keys, True)
                results.append({"target": p["target"], "ok": ok})
            self._json({"results": results})
        elif path == "/api/shell":
            session = self._field(data, "session") or ""
            if not session: self._json({"ok": False, "target": "", "err": "session required"}); return
            ok, target = tmuxio.ensure_shell_window(session)
            self._json({"ok": ok, "target": target if ok else "",
                        "windows": tmuxio.list_shell_windows(session) if ok else [],
                        "err": "" if ok else target})
        elif path == "/api/shell/new":
            session = self._field(data, "session") or ""
            if not session: self._json({"ok": False, "target": "", "err": "session required"}); return
            ok, target = tmuxio.new_shell_window(session)
            self._json({"ok": ok, "target": target if ok else "",
                        "windows": tmuxio.list_shell_windows(session) if ok else [],
                        "err": "" if ok else target})
        elif path == "/api/shell/kill":
            session = self._field(data, "session") or ""
            window = self._field(data, "window") or ""
            if not session or not window: self._json({"ok": False, "err": "session and window required"}); return
            ok, err = tmuxio.kill_shell_window(session, window)
            self._json({"ok": ok, "windows": tmuxio.list_shell_windows(session), "err": "" if ok else err})
        # --- agent graph mutations --- #
        elif path == "/api/agent/create":
            self._agent_create(data)
        elif path == "/api/agent/adopt":
            self._agent_adopt(data)
        elif path == "/api/agent/remove":
            self._agent_remove(data)
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
                launch=bool(data.get("launch", True)))
            self._json({"ok": True, "agent": agent})
        except gs.GraphError as e:
            self._json({"ok": False, "error": str(e)})
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)

    def _agent_adopt(self, data):
        """Adopt an already-running independent claude session as a crew agent —
        no new tmux session, just a record + identity.md anchored to its cwd."""
        f = lambda k: self._field(data, k)
        session = (f("session") or "").strip()
        if not session:
            self._json({"ok": False, "error": "session required"}); return
        name = (f("name") or session).strip()
        if not config.valid_agent_name(name):
            self._json({"ok": False, "error": f"invalid agent name {name!r}"}); return
        target = tmuxio.resolve_target(session)
        ok_p, cwd = tmuxio.tmux("display-message", "-t", target, "-p", "#{pane_current_path}")
        home = cwd.strip() if ok_p and cwd.strip() else os.getcwd()
        try:
            if gs.get_agent_by_name(name):
                self._json({"ok": False, "error": f"agent '{name}' already exists"}); return
            bad = gs.unsafe_home_reason(home)
            if bad:
                self._json({"ok": False, "error": bad}); return
            conflict = gs.home_conflict(home)
            if conflict:
                self._json({"ok": False, "error": f"home {home} overlaps agent '{conflict['name']}'"}); return
            agent = gs.create_agent(name, role=f("role") or "", home=home,
                                    session=session, status="idle")
            spawn.rewrite_identity(agent)
            self._json({"ok": True, "agent": agent})
        except gs.GraphError as e:
            self._json({"ok": False, "error": str(e)})

    def _agent_remove(self, data):
        name = (self._field(data, "name") or "").strip()
        if not name:
            self._json({"ok": False, "error": "name required"}); return
        try:
            spawn.remove_agent(name, kill_session=bool(data.get("kill_session", True)))
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
                description=f("description") or "", condition=f("condition") or "",
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
        for k in ("label", "description", "condition"):
            v = self._field(data, k)
            if v is not None:
                body[k] = v
        if "directed" in data:
            body["directed"] = bool(data.get("directed"))
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


def main():
    print(f"crew dashboard → http://{HOST}:{PORT}  (Ctrl-C to stop)")
    print(f"data: MorphDB app '{config.current_app()}' at {config.morphdb_base()}")
    try:
        ptyio.reap_stale()
    except Exception:
        pass
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
