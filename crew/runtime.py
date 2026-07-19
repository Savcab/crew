"""Runtime adapters for the interactive coding agents Crew manages.

The registry intentionally owns only runtime-specific facts.  Session creation,
identity rendering, and delivery remain in their existing modules, but they ask
this module which executable/window/native instruction file applies instead of
assuming every agent is Claude Code.
"""
from dataclasses import dataclass
import json
import os
import shlex

from . import config


@dataclass(frozen=True)
class RuntimeAdapter:
    key: str
    label: str
    native_file: str | None
    process_names: tuple[str, ...]
    window_name: str


ADAPTERS = {
    # Keep Claude's historical window name so existing sessions and scripts
    # remain attachable. New runtimes use the neutral window name.
    "claude": RuntimeAdapter(
        "claude", "Claude Code", "CLAUDE.md", ("claude",), "claude"),
    "codex": RuntimeAdapter(
        "codex", "Codex CLI", "AGENTS.md", ("codex",), "agent"),
    "custom": RuntimeAdapter(
        "custom", "Custom command", None, (), "agent"),
}
RUNTIME_KEYS = tuple(ADAPTERS)

# Codex may merge values from user/global configuration after inheriting the
# pane environment. Pin Crew's small identity/backend boundary explicitly so a
# stale user setting cannot redirect tool calls to another tenant or server.
# PATH is intentionally absent: it can be several kilobytes and is inherited
# from the already-pinned managed pane instead.
CODEX_CRITICAL_ENV = (
    "MORPHDB_HOST",
    "CREW_APP",
    "CREW_PROJECT",
    "CREW_AGENT",
    "AGENT_MAIL_NAME",
    "CREW_ROOT",
    "CREW_RUNTIME",
)
_CODEX_LAUNCH_MAX_BYTES = 1023
_CODEX_INHERIT_CONFIG = 'shell_environment_policy.inherit="all"'


def adapter(key):
    try:
        return ADAPTERS[key]
    except KeyError as e:
        raise ValueError(
            f"invalid runtime {key!r}: choose {', '.join(RUNTIME_KEYS)}") from e


def _command_tokens(command):
    try:
        tokens = shlex.split(command or "")
    except ValueError:
        return []
    while tokens and (tokens[0] == "exec" or "=" in tokens[0] and not tokens[0].startswith("/")):
        tokens.pop(0)
    if tokens and tokens[0] == "env":
        tokens.pop(0)
        while tokens and "=" in tokens[0]:
            tokens.pop(0)
    return tokens


def command_executable(command):
    program = command_program(command)
    return os.path.basename(program) if program else ""


def command_program(command):
    """Configured executable token, preserving an explicit absolute path."""
    tokens = _command_tokens(command)
    return tokens[0] if tokens else ""


def infer_runtime(launch_cmd):
    exe = command_executable(launch_cmd)
    if exe == "claude":
        return "claude"
    if exe == "codex":
        return "codex"
    return "custom"


def resolve_runtime(requested=None, launch_cmd=None):
    """Resolve a new agent's runtime, preserving legacy defaults.

    An explicit runtime wins. Otherwise a supplied launch command is inferred;
    with neither, CREW_RUNTIME (Claude by default) is used.
    """
    key = (requested or "").strip().lower() if isinstance(requested, str) else requested
    if not key:
        key = infer_runtime(launch_cmd) if launch_cmd else config.DEFAULT_RUNTIME
    adapter(key)
    return key


def resolve_agent_runtime(agent):
    """Runtime for stored rows, including pre-runtime legacy records."""
    saved = agent.get("runtime") if isinstance(agent, dict) else None
    if saved in ADAPTERS:
        return saved
    launch = (agent or {}).get("launch_cmd") or ""
    return infer_runtime(launch) if launch else "claude"


def agent_path():
    """PATH placed in every managed session, with Crew's CLI first."""
    crew_bin = os.path.join(config.ROOT, "bin")
    parts = [crew_bin]
    for part in os.environ.get("PATH", "").split(os.pathsep):
        if part and part not in parts:
            parts.append(part)
    return os.pathsep.join(parts)


def _codex_trust_config(home):
    # Spawn already canonicalizes managed homes. Avoid resolving platform
    # aliases again here (/tmp -> /private/tmp on macOS), which would make an
    # explicitly configured project key differ from the path the user sees.
    path = os.path.abspath(os.path.expanduser(str(home or os.getcwd())))
    escaped_home = path.replace("\\", "\\\\").replace('"', '\\"')
    return f'projects."{escaped_home}".trust_level="trusted"'


def _codex_default(home, environment=None):
    trust = _codex_trust_config(home)
    # The managed pane already imports Crew's exact PATH before launch.  Ask
    # Codex tool shells to inherit that environment instead of embedding the
    # potentially multi-kilobyte PATH in a command typed through the terminal;
    # macOS can truncate fresh tty input at 1024 bytes.
    shell_environment = _CODEX_INHERIT_CONFIG
    context = os.environ if environment is None else environment
    configs = [trust, shell_environment]
    for key in CODEX_CRITICAL_ENV:
        if key in context:
            # JSON string syntax is compatible with TOML basic strings and
            # safely represents quotes, backslashes, control characters, and
            # Unicode before shell quoting protects the full -c argument.
            value = json.dumps(str(context[key]), ensure_ascii=False)
            configs.append(f"shell_environment_policy.set.{key}={value}")
    command = config.CODEX_LAUNCH_CMD + "".join(
        f" -c {shlex.quote(value)}" for value in configs)
    if len(command.encode()) > _CODEX_LAUNCH_MAX_BYTES:
        raise ValueError(
            "generated Codex launch command exceeds the managed terminal's "
            f"{_CODEX_LAUNCH_MAX_BYTES}-byte safe input limit")
    return command


def _is_generated_codex_config(configs, home):
    """Whether config arguments exactly match a Crew-generated Codex shape."""
    trust = _codex_trust_config(home)
    if not configs or configs[0] != trust:
        return False
    # Crew's first Codex adapter embedded PATH directly.
    if (len(configs) == 2
            and configs[1].startswith("shell_environment_policy.set.PATH=")):
        return True
    # Its next revision inherited the pane but did not defend critical values
    # from user/global Codex config.
    if configs == [trust, _CODEX_INHERIT_CONFIG]:
        return True
    # Current generated commands have every critical key, in the fixed order,
    # represented as a TOML/JSON string. This exact check lets revive refresh
    # changed app/backend/root defaults without rewriting arbitrary commands.
    if len(configs) != 2 + len(CODEX_CRITICAL_ENV):
        return False
    if configs[1] != _CODEX_INHERIT_CONFIG:
        return False
    for key, value in zip(CODEX_CRITICAL_ENV, configs[2:]):
        prefix = f"shell_environment_policy.set.{key}="
        if not value.startswith(prefix):
            return False
        try:
            decoded = json.loads(value[len(prefix):])
        except (TypeError, ValueError):
            return False
        if not isinstance(decoded, str):
            return False
    return True


def revive_launch_command(runtime_key, home, stored_command, environment=None):
    """Refresh exact Crew-generated commands while preserving custom commands.

    Crew has emitted three Codex defaults: trust+PATH, trust+inherit, and the
    current trust+inherit+critical-context form. The durable row may remain
    historical or stale, but every revive regenerates those exact shapes from
    current context before typing. Any extra argument, reordered/missing key,
    unrelated config, or other custom command remains byte-for-byte unchanged.
    """
    key = resolve_runtime(runtime_key, stored_command)
    if not stored_command:
        return launch_command(key, home, environment=environment)
    if key != "codex":
        return stored_command
    try:
        tokens = shlex.split(stored_command)
        base = shlex.split(config.CODEX_LAUNCH_CMD)
    except (TypeError, ValueError):
        return stored_command
    rest = tokens[len(base):] if tokens[:len(base)] == base else []
    configs = None
    if len(rest) % 2 == 0 and all(
            rest[index] == "-c" for index in range(0, len(rest), 2)):
        configs = rest[1::2]
    if configs is not None and _is_generated_codex_config(configs, home):
        return _codex_default(home, environment=environment)
    return stored_command


def launch_command(runtime_key, home, override=None, environment=None):
    """Concrete shell command stored on an agent and typed into its pane."""
    key = resolve_runtime(runtime_key, override)
    if override:
        return override
    if key == "claude":
        return config.CLAUDE_LAUNCH_CMD
    if key == "codex":
        return _codex_default(home, environment=environment)
    raise ValueError("custom runtime requires an explicit launch command")


def process_matches(runtime_key, comm, command, launch_cmd=None):
    """Whether one ps row is the configured runtime process.

    Matching is against executable basenames, never arbitrary substrings in
    arguments (so `echo codex` and `--note=claude` cannot create false liveness).
    The second token is considered for Node/shell script launchers.
    """
    key = resolve_runtime(runtime_key, launch_cmd)
    expected = set(adapter(key).process_names)
    if key == "custom":
        exe = command_executable(launch_cmd)
        if exe:
            expected.add(exe)
    if not expected:
        return False
    seen = {os.path.basename((comm or "").strip())}
    tokens = _command_tokens(command)
    if tokens:
        first = os.path.basename(tokens[0])
        seen.add(first)
        # Script launchers expose the actual CLI path as argv[1]. Do not inspect
        # a generic command's arguments: `echo codex` must not look alive.
        if first in {"node", "bun", "python", "python3"} and len(tokens) > 1:
            seen.add(os.path.basename(tokens[1]))
    return bool(expected & seen)


def native_filename(runtime_key):
    return adapter(runtime_key).native_file


def window_name(runtime_key):
    return adapter(runtime_key).window_name
