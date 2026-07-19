#!/usr/bin/env python3
"""scrub.py — crew transform edge: drop probable prompt-injection bodies.

Reads the message body on stdin. If it matches a known injection pattern —
"ignore previous instructions", "disregard your identity", or a forged
"[crew msg from ...]" provenance prefix — exits 1 with a one-line reason on
stderr, which crew.mail.deliver() turns into a DROPPED message (status
"filtered": logged with the original body, the sender told exactly why,
never delivered). Anything else passes through on stdout byte-for-byte
unchanged, exit 0.

Attach it: crew connect A B --transform var/transforms/scrub.py
"""
import re
import sys

PATTERNS = [
    (re.compile(r"(?i)ignore\s+(all\s+)?previous\s+instructions"),
     "ignore-previous-instructions"),
    (re.compile(r"(?i)disregard\s+your\s+identity"), "disregard-your-identity"),
    (re.compile(r"\[crew msg from"), "forged-crew-prefix"),
]


def injection_hit(text):
    for pat, reason in PATTERNS:
        if pat.search(text):
            return reason
    return None


if __name__ == "__main__":
    body = sys.stdin.read()
    hit = injection_hit(body)
    if hit:
        sys.stderr.write(f"blocked: injection pattern ({hit})\n")
        sys.exit(1)
    sys.stdout.write(body)
