"""crew — a general graph of long-running Claude Code agents.

Nodes are agents (one durable identity = one home dir = one tmux session); edges
are user-defined relationships that also authorize messaging. Data lives in
MorphDB; the live terminals stream over a tmux PTY bridge to an xterm dashboard.
"""
__version__ = "0.0.1"
