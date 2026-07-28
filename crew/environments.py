"""crew.environments — named setup routines that prepare an agent's workspace.

An ENVIRONMENT is one operator-defined answer to "what should exist in this
agent's directory before its harness starts?": an optional prereq check plus an
ordered list of shell commands, run in the freshly materialized home BEFORE the
tmux session or the runtime exist (see crew.spawn). A failure fails the spawn —
an agent never boots into a half-prepared workspace.

Two environments ship in code, not in the store (``BUILTINS``):

  * ``worktree`` — NATIVE. It has no commands: it selects Crew's own worktree
    machinery (the ``--repo`` path in crew.spawn._plan_home), which already
    creates the checkout, the branch, and the durable ``worktree`` row field.
    Shelling ``git worktree add`` here instead would duplicate that machinery
    and lose the bookkeeping that `crew up` needs to revive the agent.
  * ``graphite-stack`` — a real command environment: it checks ``gt`` exists,
    then stacks a Graphite branch off main inside an existing git checkout.

``{agent}`` in a prereq or command is replaced with the agent's name at run
time. It is a plain textual substitution (never str.format), so a command may
contain ordinary shell or awk braces without being mangled.

Custom environments live in var/environments.json:

    {"default": null | "<name>", "environments": [{name, description,
                                                   prereq, commands}, ...]}

written atomically under the same owner-only lock discipline as the project
registry and the settings store, with every read LIVE — the dashboard is a
long-running process and this is how it gets reconfigured without a restart.

WRITES ARE HUMAN-ONLY (guard op ``environments_write``). An environment's
commands run as the operator, with the operator's environment, in every spawn
that uses it — this store is closer to a credential than to a preference file.
Like crew.config's registry and crew.settings, this module never checks actors
itself; the CLI gates on the live-pane actor identity and the dashboard on the
operator capability.

Corrupt or unrecognized durable state fails closed with EnvironmentsError.
Silently dropping an unparsable entry would let a truncated file turn "every
new agent gets a prepared workspace" into "every new agent gets a bare one"
without anyone noticing.
"""
import json
import os
import re
import subprocess
import threading

from . import config


class EnvironmentsError(ValueError):
    """The durable environments store cannot be read or written safely."""


# An environment name becomes a CLI argument, a stored row value, and a key in
# the dashboard's select, so it is the same safe slug a project name is —
# letters/digits/'_'/'-', starting alphanumeric — capped shorter than an agent
# name because it is only ever an identifier for a routine.
MAX_NAME_LEN = 32
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")

# Longest single line (a command, a prereq, a description). One shell line;
# anything close to this is a mistake, and the limit matches crew.settings'
# rule for the launch command it sits next to.
MAX_LINE_LEN = 1000

# How much command output a failure detail carries back to the operator. The
# tail, not the head: the error that stopped the spawn is at the end.
MAX_DETAIL_LEN = 400

PREREQ_TIMEOUT = 60
COMMAND_TIMEOUT = 300

# The only native marker today: "this environment IS crew's --repo worktree
# machinery", handled in crew.spawn rather than by running commands.
NATIVE_REPO = "repo"

# Built-in environments are code, never store rows: they are the vetted answers
# every install gets, so they cannot be edited, shadowed, or deleted, and a
# custom store that names one is corrupt.
BUILTINS = (
    {
        "name": "worktree",
        "description": ("run the agent in a fresh git worktree of the "
                        "repository (crew's own worktree machinery)"),
        "prereq": "",
        "commands": (),
        "native": NATIVE_REPO,
    },
    {
        "name": "graphite-stack",
        "description": ("expects the workspace to be a git checkout; stacks a "
                        "Graphite branch crew/<agent> off main"),
        "prereq": "gt --version",
        "commands": ("git checkout main",
                     "gt create crew/{agent} --no-interactive"),
    },
)

BUILTIN_NAMES = frozenset(entry["name"] for entry in BUILTINS)

_STORE_KEYS = frozenset({"default", "environments"})
_ENTRY_KEYS = ("commands", "description", "name", "prereq")

_ENVIRONMENTS_THREAD_LOCK = threading.RLock()


def _environments_file():
    return os.path.join(config.VAR, "environments.json")


def _lock():
    return config.var_file_lock(
        _ENVIRONMENTS_THREAD_LOCK, "environments-locks",
        "environments.json.lock", EnvironmentsError, "environments store")


# --------------------------------------------------------------------------- #
# validation — the same rules on the write path and on the read path, so a
# hand-edited store can never hold something `crew env add` would refuse
# --------------------------------------------------------------------------- #
def is_builtin(name):
    return name in BUILTIN_NAMES


def _validated_name(name):
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise EnvironmentsError(
            f"invalid environment name {name!r}: letters, digits, '_', '-' "
            f"only (no dots/slashes/spaces), max {MAX_NAME_LEN} chars, must "
            "start alphanumeric")
    return name


def _validated_custom_name(name):
    """A name a custom environment may occupy: valid, and not a built-in."""
    name = _validated_name(name)
    if is_builtin(name):
        raise EnvironmentsError(
            f"{name!r} is a built-in environment — it cannot be redefined, "
            "shadowed, or removed; pick another name")
    return name


def _validated_line(value, what, required):
    """One stripped, printable, single line — `isprintable()` is what rejects a
    newline, so a command can never smuggle a second line past the review the
    operator gave the first."""
    if not isinstance(value, str):
        raise EnvironmentsError(f"{what} must be a string")
    line = value.strip()
    if not line:
        if required:
            raise EnvironmentsError(f"{what} must not be empty")
        return ""
    if not line.isprintable() or len(line) > MAX_LINE_LEN:
        raise EnvironmentsError(
            f"{what} must be one printable line of at most {MAX_LINE_LEN} "
            f"characters: {line[:60]!r}")
    return line


def _validated_commands(commands):
    # A bare string is the likely mistake (`--command` once, unsplit); name it
    # rather than iterating its characters into 40 one-letter commands.
    if isinstance(commands, str) or not isinstance(commands, (list, tuple)):
        raise EnvironmentsError(
            "an environment's commands must be a list of shell command lines")
    validated = [_validated_line(command, f"command {index}", True)
                 for index, command in enumerate(commands, start=1)]
    if not validated:
        raise EnvironmentsError(
            "an environment needs at least one command — an environment that "
            "runs nothing is the same as no environment at all")
    return validated


def _row(entry, builtin):
    """One environment as every caller sees it: plain JSON-able types, with
    private copies so a caller cannot mutate BUILTINS or a cached entry."""
    row = {
        "name": entry["name"],
        "description": entry.get("description") or "",
        "prereq": entry.get("prereq") or "",
        "commands": list(entry.get("commands") or ()),
        "builtin": bool(builtin),
    }
    if entry.get("native"):
        row["native"] = entry["native"]
    return row


# --------------------------------------------------------------------------- #
# the durable store
# --------------------------------------------------------------------------- #
def _corrupt(detail):
    return EnvironmentsError(
        f"environments store {_environments_file()!r} is corrupt: {detail}")


def _normalize_entry(item):
    if not isinstance(item, dict):
        raise _corrupt(f"invalid entry {item!r}")
    if sorted(item) != list(_ENTRY_KEYS):
        raise _corrupt(
            f"every environment needs exactly {', '.join(_ENTRY_KEYS)}, got "
            f"{sorted(item)}")
    try:
        name = _validated_custom_name(item["name"])
        return {
            "name": name,
            "description": _validated_line(
                item["description"], "description", False),
            "prereq": _validated_line(item["prereq"], "prereq", False),
            "commands": _validated_commands(item["commands"]),
        }
    except EnvironmentsError as error:
        raise _corrupt(str(error)) from error


def _read_state_unlocked():
    """The durable state, fully validated; caller must hold the store lock."""
    path = _environments_file()
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"default": None, "environments": []}
    except (OSError, ValueError) as error:
        raise EnvironmentsError(
            f"environments store {path!r} is corrupt or unreadable: {error}") \
            from error
    if not isinstance(data, dict):
        raise _corrupt("expected a JSON object")
    unknown = sorted(set(data) - _STORE_KEYS)
    if unknown:
        raise _corrupt(f"unknown key {unknown[0]!r}")
    default = data.get("default")
    if default is not None:
        # Only the SHAPE is checked here. A default naming an environment that
        # no longer exists stays readable on purpose: `crew env set-default
        # none` has to be able to repair it, and a spawn that tries to use it
        # fails with a clean "unknown environment" instead.
        try:
            default = _validated_name(default)
        except EnvironmentsError as error:
            raise _corrupt(f"invalid default: {error}") from error
    raw = data.get("environments", [])
    if not isinstance(raw, list):
        raise _corrupt("'environments' must be a JSON list")
    entries, seen = [], set()
    for item in raw:
        entry = _normalize_entry(item)
        if entry["name"] in seen:
            raise _corrupt(f"duplicate environment {entry['name']!r}")
        seen.add(entry["name"])
        entries.append(entry)
    return {"default": default, "environments": entries}


def _write_state_unlocked(state):
    config.atomic_var_json_write(
        _environments_file(),
        {"default": state["default"],
         "environments": [{key: entry[key] for key in _ENTRY_KEYS}
                          for entry in state["environments"]]},
        EnvironmentsError, "environments store")


# --------------------------------------------------------------------------- #
# read API
# --------------------------------------------------------------------------- #
def list_all():
    """Every environment, built-ins first (they are the vetted defaults an
    operator should see before their own), each row carrying `builtin`."""
    with _lock():
        state = _read_state_unlocked()
    return ([_row(entry, True) for entry in BUILTINS]
            + [_row(entry, False) for entry in state["environments"]])


def get(name):
    """One environment row by name, or EnvironmentsError naming the known set."""
    rows = list_all()
    for row in rows:
        if row["name"] == name:
            return row
    raise EnvironmentsError(
        f"unknown environment {name!r} — known environments: "
        + ", ".join(row["name"] for row in rows))


def default_name():
    """The crew-wide default environment name, or None. Read LIVE."""
    with _lock():
        return _read_state_unlocked()["default"]


def resolve(explicit=None):
    """The environment that prepares a new agent's workspace, or None.

    Most explicit wins: an explicit pick, else the crew-wide default, else no
    environment at all (a plain home, Crew's behavior before this feature)."""
    name = explicit.strip() if isinstance(explicit, str) else explicit
    if not name:
        name = default_name()
    if not name:
        return None
    return get(name)


# --------------------------------------------------------------------------- #
# write API (human-only at every caller — see crew.guard's environments_write)
# --------------------------------------------------------------------------- #
def add_environment(name, commands, prereq="", description=""):
    """Define one custom environment, replacing a same-named one in place.

    Replacing rather than refusing is deliberate: editing an environment is the
    common operation (the settings page has no separate update verb), and a
    replace keeps the definition's position in the operator's list."""
    entry = {
        "name": _validated_custom_name(name),
        "description": _validated_line(description, "description", False),
        "prereq": _validated_line(prereq, "prereq", False),
        "commands": _validated_commands(commands),
    }
    with _lock():
        state = _read_state_unlocked()
        entries = state["environments"]
        for index, existing in enumerate(entries):
            if existing["name"] == entry["name"]:
                entries[index] = entry
                break
        else:
            entries.append(entry)
        _write_state_unlocked(state)
    return _row(entry, False)


def remove_environment(name):
    """Delete one custom environment; returns whether it existed. Clears the
    crew-wide default when it pointed here — a default naming a deleted
    environment would fail every later spawn."""
    name = _validated_custom_name(name)
    with _lock():
        state = _read_state_unlocked()
        remaining = [entry for entry in state["environments"]
                     if entry["name"] != name]
        if len(remaining) == len(state["environments"]):
            return False
        state["environments"] = remaining
        if state["default"] == name:
            state["default"] = None
        _write_state_unlocked(state)
    return True


def set_default(name):
    """Set (or, with None/"", clear) the crew-wide default environment. The
    name must exist WHEN IT IS STORED — the existence check and the write share
    one lock, so a concurrent `remove` cannot leave a dangling default."""
    target = name.strip() if isinstance(name, str) else name
    if not target:
        target = None
    else:
        target = _validated_name(target)
    with _lock():
        state = _read_state_unlocked()
        if target is not None and not (
                is_builtin(target)
                or any(entry["name"] == target
                       for entry in state["environments"])):
            known = list(BUILTIN_NAMES) + [
                entry["name"] for entry in state["environments"]]
            raise EnvironmentsError(
                f"unknown environment {target!r} — known environments: "
                + ", ".join(sorted(known)))
        state["default"] = target
        _write_state_unlocked(state)
    return target


# --------------------------------------------------------------------------- #
# running one environment's setup routine
# --------------------------------------------------------------------------- #
def _substitute(line, agent_name):
    """`{agent}` → the agent's name. A plain replace, never str.format: a setup
    command may legitimately contain `${VAR}` or awk's `{print $1}`."""
    return line.replace("{agent}", str(agent_name or ""))


def _tail(*chunks):
    text = "\n".join(chunk.strip() for chunk in chunks
                     if isinstance(chunk, str) and chunk.strip()).strip()
    return text[-MAX_DETAIL_LEN:]


def _run_line(command, home, environment, timeout):
    """Run one shell line in the agent's home. Returns (ok, detail)."""
    try:
        finished = subprocess.run(
            command, shell=True, cwd=home, env=environment,
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except (OSError, subprocess.SubprocessError) as error:
        return False, _tail(str(error))
    if finished.returncode != 0:
        return False, (_tail(finished.stderr, finished.stdout)
                       or f"exit status {finished.returncode}")
    return True, ""


def run_setup(entry, home, agent_name, environment=None):
    """Prepare `home` with one environment's routine. Returns (ok, detail).

    The prereq (when the environment has one) runs first and is the cheap
    "is this machine set up for this at all?" check — a missing `gt` should
    report itself as a missing tool, not as a confusing failure three commands
    in. Then every command runs in order, stopping at the first failure.

    NEVER RAISES: a spawn calls this between materializing a home and opening a
    session, and the caller turns (False, detail) into one clean GraphError on
    its existing cleanup path. An exception escaping from here would bypass
    that and could leave the half-prepared home behind.
    """
    try:
        prereq = _substitute((entry or {}).get("prereq") or "", agent_name)
        if prereq:
            ok, detail = _run_line(prereq, home, environment, PREREQ_TIMEOUT)
            if not ok:
                return False, f"prereq failed: {prereq}: {detail}"
        for command in (entry or {}).get("commands") or ():
            command = _substitute(command, agent_name)
            ok, detail = _run_line(
                command, home, environment, COMMAND_TIMEOUT)
            if not ok:
                return False, f"command failed: {command}: {detail}"
    except Exception as error:  # never leave the caller's cleanup unreached
        return False, f"setup could not run: {error}"
    return True, ""
