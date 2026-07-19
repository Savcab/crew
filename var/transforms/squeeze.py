#!/usr/bin/env python3
"""squeeze.py — crew transform edge: cap a message body's length.

Reads the message body on stdin, writes it to stdout, exit 0 always. Bodies
at or under 4000 chars pass through byte-for-byte unchanged. Longer bodies
are squeezed to: the first 2000 chars, a '[...squeezed N chars...]' marker
(N = the number of chars dropped from the middle), then the last 1000 chars
— so a huge diff/log dump doesn't blow past the target's context budget
while both ends of it (usually the most useful parts) survive.

Attach it: crew connect A B --transform var/transforms/squeeze.py
"""
import sys

LIMIT = 4000
HEAD = 2000
TAIL = 1000


def squeeze(text):
    if len(text) <= LIMIT:
        return text
    dropped = len(text) - HEAD - TAIL
    return text[:HEAD] + f"\n[...squeezed {dropped} chars...]\n" + text[-TAIL:]


if __name__ == "__main__":
    sys.stdout.write(squeeze(sys.stdin.read()))
