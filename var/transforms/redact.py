#!/usr/bin/env python3
"""redact.py — crew transform edge: strip common secret shapes from a message.

Reads the message body on stdin, writes the redacted body to stdout, exit 0
always (this transform never drops a message, only cleans it — pair it with
scrub.py on the same edge if you also want injection bodies dropped).

Matches (each occurrence replaced with the literal '[redacted]'):
  * OpenAI-style API keys      sk-...............
  * AWS access key IDs         AKIA................
  * GitHub personal access tok ghp_....................
  * Bearer tokens               Bearer <token>  (Authorization headers)

Everything else passes through byte-for-byte unchanged.

Attach it: crew connect A B --transform var/transforms/redact.py
"""
import re
import sys

PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-_.=]+"),
]


def redact(text):
    for pat in PATTERNS:
        text = pat.sub("[redacted]", text)
    return text


if __name__ == "__main__":
    sys.stdout.write(redact(sys.stdin.read()))
