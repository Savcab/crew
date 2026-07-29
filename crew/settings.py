"""crew.settings — durable crew-wide operator settings (var/settings.json).

One flat JSON object of known keys, written atomically under the same
owner-only locking discipline as the project registry (crew.config). Every
read is LIVE — the dashboard is a long-running process and this store is how
it gets reconfigured without a restart (the `expand_cmd()` precedent).

Per-key precedence, most explicit wins:

  1. process env override (e.g. $CREW_CLAUDE_LAUNCH_CMD) — a deliberate
     per-process choice, so it beats the durable store;
  2. stored value in var/settings.json;
  3. the built-in default from crew.config.

Every setting is an ENUM, not a text box: each key carries a curated list of
``choices`` (label + command) and a write must equal one of those commands.
Harnesses have a handful of meaningful launch modes, all of them verified
against the real CLI, so offering a free-text field only invited typos and
unvetted flags into a string that gets typed straight into a shell.

Membership is a WRITE rule only. A value stored before a choice set changed
still reads back verbatim — a read that failed or reverted to the default
because policy tightened would swap a running crew's launch command out from
under it — and an env override is a per-process escape hatch that skips the
menu entirely.

WRITES ARE HUMAN-ONLY (guard op ``settings_write``). A stored launch command
is typed verbatim into every future agent pane, so an agent that could write
this store could run arbitrary commands as the operator. The CLI gates on the
live-pane actor identity and the dashboard on the operator capability; this
module never checks actors itself, exactly like crew.config's registry.

Corrupt or unrecognized durable state fails closed with SettingsError —
silently falling back to defaults would let a truncated file revert a
deliberately configured launch command without anyone noticing.
"""
import json
import os
import shlex
import threading

from . import config


class SettingsError(ValueError):
    """The durable settings store cannot be read or written safely."""


# Longest command a choice may carry. Launch commands are one shell line;
# anything close to this is a mistake (and Codex's generated form must stay
# under the 1023-byte tty input limit downstream anyway).
MAX_VALUE_LEN = 1000

# The registry of every crew-wide setting. `env` names are checked in order,
# live, on every read; a set-but-empty env var counts as unset. `choices` is
# the whole menu for the key, first entry being the built-in default —
# _check_curated_choices() enforces that at import.
KEYS = {
    "claude_launch_cmd": {
        "label": "Claude Code launch command",
        "default": config.CLAUDE_LAUNCH_CMD_DEFAULT,
        "env": ("CREW_CLAUDE_LAUNCH_CMD", "CREW_LAUNCH_CMD"),
        "choices": [
            {"label": "Unattended (default)",
             "command": "claude --dangerously-skip-permissions"},
            {"label": "Ask for permissions", "command": "claude"},
            {"label": "Unattended, continue last session",
             "command": "claude --dangerously-skip-permissions --continue"},
        ],
    },
    "codex_launch_cmd": {
        "label": "Codex CLI launch command (base; per-home config is appended)",
        "default": config.CODEX_LAUNCH_CMD_DEFAULT,
        "env": ("CREW_CODEX_LAUNCH_CMD",),
        "choices": [
            {"label": "Unattended (default)",
             "command": ("codex --dangerously-bypass-approvals-and-sandbox "
                         "--disable hooks")},
            {"label": "Sandboxed with approvals", "command": "codex"},
        ],
    },
    "hermes_launch_cmd": {
        "label": "Hermes launch command",
        "default": config.HERMES_LAUNCH_CMD_DEFAULT,
        "env": ("CREW_HERMES_LAUNCH_CMD",),
        "choices": [
            {"label": "Default", "command": "hermes"},
            {"label": "Auto-approve (yolo)", "command": "hermes --yolo"},
            {"label": "Continue last session", "command": "hermes --continue"},
        ],
    },
}

_RUNTIME_KEYS = {
    "claude": "claude_launch_cmd",
    "codex": "codex_launch_cmd",
    "hermes": "hermes_launch_cmd",
}


def _check_curated_choices():
    """Fail the import rather than serve a menu that cannot be launched.

    Every constraint free-text validation used to impose on operator input now
    lives here, on the curated list: this is the only place a launch command
    enters the system, so it is the only place left to check it.
    """
    for key, spec in KEYS.items():
        choices = spec["choices"]
        if not choices:
            raise SettingsError(f"setting {key!r} has no choices")
        if choices[0]["command"] != spec["default"]:
            raise SettingsError(
                f"setting {key!r}: the first choice must be the built-in "
                f"default {spec['default']!r}, not "
                f"{choices[0]['command']!r}")
        seen = set()
        for choice in choices:
            if sorted(choice) != ["command", "label"]:
                raise SettingsError(
                    f"setting {key!r}: every choice needs exactly a label and "
                    f"a command, got {sorted(choice)}")
            command, label = choice["command"], choice["label"]
            if not label.strip():
                raise SettingsError(f"setting {key!r}: choice {command!r} "
                                    "has no label")
            if command in seen:
                raise SettingsError(
                    f"setting {key!r}: duplicate choice {command!r}")
            seen.add(command)
            if (command != command.strip() or not command.isprintable()
                    or len(command) > MAX_VALUE_LEN):
                raise SettingsError(
                    f"setting {key!r}: choice {command!r} is not one "
                    f"printable line of at most {MAX_VALUE_LEN} characters")
            try:
                if not shlex.split(command):
                    raise ValueError("empty command")
            except ValueError as error:
                raise SettingsError(
                    f"setting {key!r}: choice {command!r} is not a valid "
                    f"shell command: {error}") from error


_check_curated_choices()

_SETTINGS_THREAD_LOCK = threading.RLock()


def _settings_file():
    return os.path.join(config.VAR, "settings.json")


def _lock():
    return config.var_file_lock(
        _SETTINGS_THREAD_LOCK, "settings-locks", "settings.json.lock",
        SettingsError, "settings store")


def _require_known(key):
    if key not in KEYS:
        raise SettingsError(
            f"unknown setting {key!r} — known settings: "
            f"{', '.join(sorted(KEYS))}")
    return key


def choice_commands(key):
    """Every command that may be stored for `key`, in menu order."""
    return [choice["command"] for choice in KEYS[_require_known(key)]["choices"]]


def validated_value(key, value):
    """The normalized value for `key`, or SettingsError. A write must name one
    of the key's curated choices, so the store can only ever hold a command
    somebody vetted against the real CLI."""
    _require_known(key)
    if not isinstance(value, str):
        raise SettingsError(f"setting {key!r} must be a string")
    value = value.strip()
    if not value:
        raise SettingsError(
            f"setting {key!r} needs a non-empty value — to return to the "
            "default, clear it instead")
    commands = choice_commands(key)
    if value not in commands:
        raise SettingsError(
            f"setting {key!r} must be one of its choices: "
            + ", ".join(repr(command) for command in commands))
    return value


def _read_overrides_unlocked():
    path = _settings_file()
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise SettingsError(
            f"settings store {path!r} is corrupt or unreadable: {error}") \
            from error
    if not isinstance(data, dict):
        raise SettingsError(
            f"settings store {path!r} is corrupt: expected a JSON object")
    overrides = {}
    for key, value in data.items():
        if key not in KEYS:
            raise SettingsError(
                f"settings store {path!r} is corrupt: unknown key {key!r}")
        if not isinstance(value, str):
            raise SettingsError(
                f"settings store {path!r} is corrupt: non-string value "
                f"on {key!r}")
        overrides[key] = value
    return overrides


def _write_overrides_unlocked(overrides):
    config.atomic_var_json_write(
        _settings_file(), overrides, SettingsError, "settings store")


def _env_override(spec):
    for name in spec["env"]:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def set_value(key, value):
    """Store one override; returns the normalized value."""
    value = validated_value(key, value)
    with _lock():
        overrides = _read_overrides_unlocked()
        overrides[key] = value
        _write_overrides_unlocked(overrides)
    return value


def clear_value(key):
    """Drop one override (idempotent); returns whether one existed."""
    _require_known(key)
    with _lock():
        overrides = _read_overrides_unlocked()
        removed = overrides.pop(key, None) is not None
        if removed:
            _write_overrides_unlocked(overrides)
    return removed


def effective(key):
    """(value, source) for one key; source is 'env' | 'settings' | 'default'."""
    spec = KEYS[_require_known(key)]
    env = _env_override(spec)
    if env is not None:
        return env, "env"
    with _lock():
        overrides = _read_overrides_unlocked()
    if key in overrides:
        return overrides[key], "settings"
    return spec["default"], "default"


def launch_cmd(runtime_key):
    """The effective launch command for one harness runtime, read live."""
    if runtime_key not in _RUNTIME_KEYS:
        raise SettingsError(f"no launch-command setting for {runtime_key!r}")
    return effective(_RUNTIME_KEYS[runtime_key])[0]


def describe():
    """Every setting for the UI/CLI: key, label, default, the curated choices,
    stored override (None when unset), effective value, and its source.

    The choices are reported as-is even when the effective value is not among
    them (a value stored before the menu tightened, or an env override); the
    caller decides how to show a current value that is off the menu."""
    with _lock():
        overrides = _read_overrides_unlocked()
    out = []
    for key in sorted(KEYS):
        spec = KEYS[key]
        env = _env_override(spec)
        override = overrides.get(key)
        if env is not None:
            value, source = env, "env"
        elif override is not None:
            value, source = override, "settings"
        else:
            value, source = spec["default"], "default"
        out.append({
            "key": key,
            "label": spec["label"],
            "default": spec["default"],
            "choices": [dict(choice) for choice in spec["choices"]],
            "override": override,
            "effective": value,
            "source": source,
        })
    return out
