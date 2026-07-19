"""crew.usage — honest availability-aware runtime usage metering.

Reads the agent's Claude Code transcripts (~/.claude/projects/<slug>/*.jsonl,
where <slug> is the home dir's realpath with every non-alphanumeric turned into
'-') and sums the usage block of every type=="assistant" line inside the
window. This is the metering half of per-edge token/cost budgets (crew.mail):
no daemon, no counters to keep consistent — the transcripts ARE the meter.

``hourly_usage`` is the policy-facing API. Each metric carries ``available``,
``value``, and ``reason`` so a missing/broken meter can never masquerade as a
measured zero. A readable empty source is a real zero; a missing or unreadable
source and a source whose every record is malformed are unavailable. Codex and
custom runtime meters are deliberately unavailable until Crew has a trustworthy
reader for their native usage logs.

An unknown Claude model still yields an available token total, but cost is
unavailable rather than a misleading lower bound. ``hourly_spend`` remains as a
legacy numeric wrapper for display/older callers; budget policy must use the
availability-aware API.
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
    "claude-sonnet-5":  (3.0, 15.0),
    "claude-sonnet-4":  (3.0, 15.0),   # covers 4.6 / 4.5 / 4
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-3-5-haiku": (0.8, 4.0),   # 3.x ids are version-first, unlike 4.x+
}

# Sonnet 5 launched with a temporary $2/$10 input/output rate through the last
# second of August 2026.  Usage is priced by the request record's UTC timestamp,
# not by the wall clock when Crew happens to read the transcript.
_SONNET_5_FAMILY = "claude-sonnet-5"
_SONNET_5_INTRO_PRICE = (2.0, 10.0)
_SONNET_5_STANDARD_START = calendar.timegm((2026, 9, 1, 0, 0, 0, 0, 0, 0))

# Where Claude Code keeps per-project transcripts (module-level so a test can
# point it at a fake tree).
PROJECTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")

_USAGE_TOKEN_FIELDS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)


def _slug(home):
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(os.path.expanduser(home)))


def _price(model, event_ts):
    for key, p in PRICES.items():
        # Model families are either the bare family id or a hyphen-delimited
        # variant (typically a dated release).  A raw prefix match would price a
        # future/lookalike family such as ``claude-sonnet-50`` as Sonnet 5 and
        # silently make a configured cost cap too permissive.
        if model == key or model.startswith(key + "-"):
            if key == _SONNET_5_FAMILY and event_ts < _SONNET_5_STANDARD_START:
                return _SONNET_5_INTRO_PRICE
            return p
    return None


def _ts(s):
    """Epoch seconds for a transcript ISO-8601 'Z' timestamp, or 0 when malformed."""
    try:
        return calendar.timegm(time.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0


def _metric(available, value=None, reason=""):
    return {"available": bool(available),
            "value": value if available else None,
            "reason": "" if available else str(reason or "usage unavailable")}


def _unavailable(runtime_key, reason):
    return {
        "runtime": runtime_key,
        "tokens": _metric(False, reason=reason),
        "cost": _metric(False, reason=reason),
    }


def _parse_usage_block(raw):
    """Return the four token counters, or a reason the record is unmeterable.

    A present all-zero block is a measured zero. Missing/empty/partial fields are
    not zero: accepting them would turn transcript schema drift into a budget
    bypass by silently undercounting a request.
    """
    if not isinstance(raw, dict):
        return None, "assistant usage block is missing or invalid"
    missing = [
        field for field in _USAGE_TOKEN_FIELDS
        if field not in raw or raw[field] is None or raw[field] == ""
    ]
    if missing:
        return None, (
            "assistant usage block is missing required field(s): "
            + ", ".join(missing))
    # JSON booleans are Python ints, and int() also truncates floats and accepts
    # numeric strings.  Usage counters must be genuine JSON integers so schema
    # drift cannot become a quiet undercount.
    if any(type(raw[field]) is not int for field in _USAGE_TOKEN_FIELDS):
        return None, "assistant usage block contains a non-integer token value"
    values = tuple(raw[field] for field in _USAGE_TOKEN_FIELDS)
    if any(value < 0 for value in values):
        return None, "assistant usage block contains a negative token value"
    return values, ""


def hourly_usage(home, since_ts, runtime_key="claude"):
    """Return an availability-aware usage reading for the trailing window.

    Shape::

        {"runtime": "claude",
         "tokens": {"available": True, "value": 123, "reason": ""},
         "cost": {"available": False, "value": None,
                  "reason": "unknown Claude model: ..."}}

    A policy caller must inspect the configured cap's metric before comparing
    its value. Unavailability is intentional information, not a numeric zero.
    """
    runtime_key = str(runtime_key or "claude").strip().lower()
    if runtime_key != "claude":
        return _unavailable(
            runtime_key,
            f"{runtime_key} usage metering is unavailable",
        )
    if not home:
        # realpath("") is the CWD; never accidentally meter the operator shell.
        return _unavailable("claude", "Claude transcript home is missing")

    try:
        pdir = os.path.join(PROJECTS_DIR, _slug(home))
        names = os.listdir(pdir)
    except OSError as e:
        return _unavailable(
            "claude", f"Claude transcript directory is missing or unreadable: {e}")

    tokens, cost = 0, 0.0
    seen = set()   # requestId dedup — one API call spans several transcript lines
    eligible = []
    for name in names:
        path = os.path.join(pdir, name)
        if not name.endswith(".jsonl"):
            continue
        try:
            # mtime stat-gate: a file untouched since the window began can't hold
            # an in-window line — skip it unread (cold files are most files).
            if os.stat(path).st_mtime < since_ts:
                continue
            eligible.append(path)
        except OSError as e:
            return _unavailable(
                "claude", f"Claude transcript source is unreadable: {e}")

    # A readable directory with no warm transcript data is a measured zero.
    if not eligible:
        return {
            "runtime": "claude",
            "tokens": _metric(True, 0),
            "cost": _metric(True, 0.0),
        }

    valid_records = 0
    saw_nonempty_record = False
    unknown_models = set()
    usage_errors = set()
    for path in eligible:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    saw_nonempty_record = True
                    try:
                        o = json.loads(line)
                    except (ValueError, TypeError):
                        usage_errors.add(
                            "transcript contains malformed JSON and is not fully valid")
                        continue
                    if not isinstance(o, dict):
                        usage_errors.add("transcript record is not a JSON object")
                        continue
                    if o.get("type") != "assistant":
                        valid_records += 1
                        continue
                    event_ts = _ts(o.get("timestamp"))
                    if not event_ts:
                        usage_errors.add(
                            "assistant timestamp is missing or invalid")
                        continue
                    if event_ts < since_ts:
                        valid_records += 1
                        continue
                    rid = o.get("requestId")
                    if rid and rid in seen:
                        valid_records += 1
                        continue
                    msg = o.get("message")
                    if not isinstance(msg, dict):
                        usage_errors.add(
                            "assistant message/usage block is missing or invalid")
                        continue
                    values, usage_error = _parse_usage_block(msg.get("usage"))
                    if usage_error:
                        usage_errors.add(usage_error)
                        continue
                    inp, cw, cr, out = values
                    valid_records += 1
                    if rid:
                        seen.add(rid)
                    tokens += inp + cw + cr + out
                    model = str(msg.get("model") or "")
                    p = _price(model, event_ts)
                    if p:
                        cost += (p[0] * (inp + 1.25 * cw + 0.1 * cr) + p[1] * out) / 1e6
                    elif inp or cw or cr or out:
                        unknown_models.add(model or "<missing>")
        except OSError as e:
            return _unavailable(
                "claude", f"Claude transcript source is unreadable: {e}")

    if usage_errors:
        return _unavailable(
            "claude", "Claude transcript has unmeterable usage: "
            + "; ".join(sorted(usage_errors)))

    if saw_nonempty_record and not valid_records:
        return _unavailable(
            "claude", "Claude transcript source contained no valid records")

    if unknown_models:
        models = ", ".join(sorted(unknown_models))
        cost_metric = _metric(
            False, reason=f"unknown Claude model pricing: {models}")
    else:
        cost_metric = _metric(True, cost)
    return {
        "runtime": "claude",
        "tokens": _metric(True, tokens),
        "cost": cost_metric,
    }


def hourly_spend(home, since_ts):
    """Legacy numeric view of :func:`hourly_usage`.

    Unavailable metrics retain the historical zero fallback so existing display
    callers do not break. Enforcement code must use ``hourly_usage`` and fail
    closed when a configured dimension is unavailable.
    """
    reading = hourly_usage(home, since_ts, runtime_key="claude")
    return {
        "tokens": (reading["tokens"]["value"]
                   if reading["tokens"]["available"] else 0),
        "cost": (reading["cost"]["value"]
                 if reading["cost"]["available"] else 0.0),
    }
