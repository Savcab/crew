"""crew.usage — how much has an agent's claude actually spent this hour?

Reads the agent's Claude Code transcripts (~/.claude/projects/<slug>/*.jsonl,
where <slug> is the home dir's realpath with every non-alphanumeric turned into
'-') and sums the usage block of every type=="assistant" line inside the
window. This is the metering half of per-edge token/cost budgets (crew.mail):
no daemon, no counters to keep consistent — the transcripts ARE the meter.

Fail-OPEN: a missing/unreadable transcript dir, a malformed line, or an
unparsable timestamp counts as ZERO spend — a broken meter must never wedge the
crew's mail, so a budget degrades to "uncapped", not "blocked".

Cost is a LOWER BOUND on real $: an unknown model's tokens are still counted
but its $ is skipped (PRICES is hand-maintained), and cache writes are priced
at the 5-minute 1.25x rate though 1-hour writes actually bill 2x.
"""
import calendar
import json
import os
import re
import time

# $ per MTok (input, output) — verified 2026-07-17 against
# https://platform.claude.com/docs/en/about-claude/pricing
# Matched by PREFIX so dated ids (claude-sonnet-4-5-20250929) hit their row.
PRICES = {
    "claude-fable-5":   (10.0, 50.0),
    "claude-mythos-5":  (10.0, 50.0),
    "claude-opus-4-8":  (5.0, 25.0),
    "claude-opus-4-7":  (5.0, 25.0),
    "claude-opus-4-6":  (5.0, 25.0),
    "claude-opus-4-5":  (5.0, 25.0),
    "claude-opus-4-1":  (15.0, 75.0),
    "claude-sonnet-5":  (2.0, 10.0),   # intro price through 2026-08-31; 3/15 after
    "claude-sonnet-4":  (3.0, 15.0),   # covers 4.6 / 4.5 / 4
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-3-5-haiku": (0.8, 4.0),   # 3.x ids are version-first, unlike 4.x+
}

# Where Claude Code keeps per-project transcripts (module-level so a test can
# point it at a fake tree).
PROJECTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")


def _slug(home):
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(os.path.expanduser(home)))


def _price(model):
    for key, p in PRICES.items():
        if model.startswith(key):
            return p
    return None


def _ts(s):
    """Epoch seconds for a transcript ISO-8601 'Z' timestamp, or 0 (→ falls out
    of any window; fail-open) when malformed."""
    try:
        return calendar.timegm(time.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0


def hourly_spend(home, since_ts):
    """Total {"tokens": int, "cost": float} the claude living in `home` spent at
    or after `since_ts` (pass time.time()-3600 for the trailing hour). Fail-open
    zeros when nothing is readable; cost is a lower bound (see module docstring)."""
    if not home:
        return {"tokens": 0, "cost": 0.0}   # realpath("") is the CWD — never meter it
    tokens, cost = 0, 0.0
    seen = set()   # requestId dedup — one API call spans several transcript lines
    try:
        pdir = os.path.join(PROJECTS_DIR, _slug(home))
        names = os.listdir(pdir)
    except OSError:
        return {"tokens": 0, "cost": 0.0}
    for name in names:
        path = os.path.join(pdir, name)
        try:
            # mtime stat-gate: a file untouched since the window began can't hold
            # an in-window line — skip it unread (cold files are most files).
            if not name.endswith(".jsonl") or os.stat(path).st_mtime < since_ts:
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"assistant"' not in line:   # cheap prefilter before json.loads
                        continue
                    try:
                        o = json.loads(line)
                        if o.get("type") != "assistant" or _ts(o.get("timestamp")) < since_ts:
                            continue
                        rid = o.get("requestId")
                        if rid and rid in seen:
                            continue
                        seen.add(rid)
                        msg = o.get("message") or {}
                        u = msg.get("usage") or {}
                        inp = int(u.get("input_tokens") or 0)
                        cw = int(u.get("cache_creation_input_tokens") or 0)
                        cr = int(u.get("cache_read_input_tokens") or 0)
                        out = int(u.get("output_tokens") or 0)
                        tokens += inp + cw + cr + out
                        p = _price(str(msg.get("model") or ""))
                        if p:
                            cost += (p[0] * (inp + 1.25 * cw + 0.1 * cr) + p[1] * out) / 1e6
                    except (ValueError, TypeError, AttributeError):
                        continue   # fail-open: a malformed line counts as zero spend
        except OSError:
            continue
    return {"tokens": tokens, "cost": cost}
