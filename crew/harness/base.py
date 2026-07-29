"""crew.harness.base — the contract every coding-harness reader implements.

Coding harnesses differ wildly (JSON session files, sqlite goal stores,
kanban boards), so the base class deliberately does NOT try to model a
harness.  Its single responsibility is to capture what Crew wants to show
the user — the open goals an agent is working toward — and to normalize
that reading identically no matter which harness produced it.  Anything a
harness knows that the user doesn't need on the graph stays out of this
interface.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import sqlite3

MAX_TEXT = 200      # same cap as an agent's activity line
MAX_GOALS = 50      # a card shows one goal + a count; nobody reads 400


def clean_text(value):
    """Collapse whitespace and bound one user-visible reading."""
    return " ".join(str(value or "").split())[:MAX_TEXT]


def count_reading(read, home):
    """One optional count, bounded, degrading to "no reading" if it breaks.

    A count is always a side reading: a store that moved or a table that was
    dropped must cost that one number and nothing else — never the goals,
    never another count.  Deliberately not a method, so it cannot collide
    with anything a reader subclass keeps on itself.
    """
    try:
        value = read(home)
        return None if value is None else max(0, int(value))
    except Exception:
        return None


def read_only_db(path):
    """A read-only sqlite connection that cannot block a live harness.

    Harness databases are owned by a running program; Crew must neither
    write to them nor hold them long.  ``timeout`` is short because the
    dashboard polls this — better one missed reading than a stalled poll.
    """
    # ponytail: path goes into a URI unescaped; state dirs with '?' or '#'
    # in their names would need urllib.parse.quote here.
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.2)


@dataclass(frozen=True)
class HarnessState:
    """What one agent's harness says it is working toward.

    ``supported`` False means Crew has no reader for the runtime, which is
    different from a supported runtime that currently has no goals — the UI
    must not badge the former as "no goal".  ``reason`` explains an empty or
    missing reading; it is diagnostic text, not part of the graph payload.
    ``subagents`` counts the live subagents the harness itself is running
    under this agent; None means the reading is unavailable, which is a
    different claim from an honest 0.  ``cron_loops`` counts the scheduled
    loops the harness will fire under this agent, with the same semantics —
    a harness with no cron concept at all reads None, never 0.
    """

    runtime: str
    supported: bool
    goals: tuple = field(default=())
    reason: str = ""
    subagents: int = None
    cron_loops: int = None

    @property
    def goal(self):
        return self.goals[0] if self.goals else ""

    @property
    def goal_count(self):
        return len(self.goals)

    def as_dict(self):
        return {"runtime": self.runtime, "supported": self.supported,
                "goal": self.goal, "goal_count": self.goal_count,
                "goals": list(self.goals), "reason": self.reason,
                "subagents": self.subagents, "cron_loops": self.cron_loops}


class Harness(ABC):
    """One coding harness Crew can read (Claude Code, Codex CLI, Hermes...).

    Implementations read the harness's OWN durable state on disk; they never
    drive the TUI and never need the agent's cooperation.  A reader may
    raise freely — ``crew.harness.probe`` shields callers — and returns raw
    strings; bounding and cleaning happen here so every harness is held to
    the same contract.
    """

    key = ""    # the crew.runtime ADAPTERS key this reader serves

    @abstractmethod
    def read_goals(self, home):
        """(goals, reason) for the agent homed at ``home``.

        ``goals`` is the harness's open goals, most current first.
        ``reason`` explains an empty list when absence is a statement about
        readability ("no live session") rather than a real "nothing open".
        """

    def read_subagents(self, home):
        """Live subagents the harness runs under ``home``, or None.

        None means "no reading" — the harness predates the concept or its
        store is absent — never 0, which is a claim there are none.
        """
        return None

    def read_cron_loops(self, home):
        """Cron loops the harness has scheduled under ``home``, or None.

        Same honesty as ``read_subagents``: None means the harness has no
        cron concept, or has one Crew could not read, never 0.
        """
        return None

    def state(self, home):
        """The normalized reading callers actually consume."""
        goals, reason = self.read_goals(home)
        cleaned = [text for text in (clean_text(g) for g in goals or [])
                   if text]
        return HarnessState(self.key, True, tuple(cleaned[:MAX_GOALS]),
                            clean_text(reason),
                            count_reading(self.read_subagents, home),
                            count_reading(self.read_cron_loops, home))
