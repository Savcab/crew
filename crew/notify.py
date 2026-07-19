"""crew.notify — fire-and-forget operator notifications (one webhook POST).

A crew runs unattended for hours, and its worst failures are SILENT: an agent's
session dies overnight, a handoff expires undelivered, a pane sits on a
permission prompt nobody sees. This module turns those events into a single
POST to a webhook the operator configures — phone push via ntfy.sh, or any
JSON-accepting endpoint (Slack, Discord, your own).

Config: $CREW_WEBHOOK_URL (read LIVE, like config.current_app) wins, else
config.WEBHOOK_URL. No URL → notify() is an instant no-op. Never raises — a
monitoring hiccup must never break mail delivery or the dashboard — and never
blocks the caller beyond the ~3s socket timeout.

Body shape is picked automatically: ntfy.sh URLs (or $CREW_WEBHOOK_FORMAT=ntfy)
get a plain-text body + Title header (what ntfy's phone push expects); every
other URL gets JSON {"event", "agent", "detail", "ts"}.

  Self-check:  python3 -m crew.notify
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

from . import config

TIMEOUT = 3.0


def _url():
    return (os.environ.get("CREW_WEBHOOK_URL") or config.WEBHOOK_URL or "").strip()


def _is_ntfy(url):
    return (os.environ.get("CREW_WEBHOOK_FORMAT", "").strip().lower() == "ntfy"
            or "ntfy.sh" in url)


def notify(event, agent, detail):
    """POST one operator notification; fire-and-forget. No URL configured → no-op
    without touching urllib; any failure is swallowed (one warn print at most)."""
    url = _url()
    if not url:
        return
    try:
        if _is_ntfy(url):
            req = urllib.request.Request(url, data=f"{agent}: {detail}".encode(),
                                         headers={"Title": f"crew: {event}"},
                                         method="POST")
        else:
            payload = {"event": event, "agent": agent, "detail": detail,
                       "ts": int(time.time())}
            req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
        urllib.request.urlopen(req, timeout=TIMEOUT).close()
    except urllib.error.HTTPError as e:
        e.close()
        print(f"[crew.notify] webhook POST failed (ignored): {e}", file=sys.stderr)
    except Exception as e:
        print(f"[crew.notify] webhook POST failed (ignored): {e}", file=sys.stderr)


if __name__ == "__main__":
    calls = []

    class _Resp:
        def close(self):
            pass

    urllib.request.urlopen = lambda req, timeout=None: (calls.append(req), _Resp())[1]

    os.environ.pop("CREW_WEBHOOK_URL", None)
    os.environ.pop("CREW_WEBHOOK_FORMAT", None)
    config.WEBHOOK_URL = ""
    notify("agent_down", "builder", "x")
    assert calls == [], "no-config notify must not touch urllib"

    os.environ["CREW_WEBHOOK_URL"] = "https://ntfy.sh/demo"
    notify("agent_down", "builder", "session died")
    assert len(calls) == 1
    assert calls[0].data == b"builder: session died"
    assert calls[0].get_header("Title") == "crew: agent_down"

    os.environ["CREW_WEBHOOK_URL"] = "https://hooks.example.com/w"
    notify("needs_input", "leads", "permission prompt")
    body = json.loads(calls[1].data.decode())
    assert body["event"] == "needs_input" and body["agent"] == "leads"
    assert body["detail"] == "permission prompt" and isinstance(body["ts"], int)
    assert calls[1].get_header("Content-type") == "application/json"
    print("crew.notify self-check OK")
