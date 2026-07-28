# Crew repository instructions

## Feature work

Before changing product behavior, read
`.claude/skills/feature-development/SKILL.md`. Its test-first and live-system
rules apply to every coding agent, not only Claude.

Every user-visible feature must have a repository-owned dossier under
`docs/features/<feature-id>/`. Create it before implementation with:

```bash
python3 scripts/new_feature.py <feature-id> \
  --title "Human-readable title" \
  --summary "One sentence describing the user outcome."
```

Keep the dossier current as the implementation changes. Its complete shape is:

```text
docs/features/<feature-id>/
├── index.html
└── assets/              # optional until primary evidence exists
```

`index.html` is the single canonical record. It contains the product
description, user flow, public interface, architecture diagram, security and
failure boundaries, rollout plan, exact verification commands and results,
and an embedded `application/json` manifest for status, dependency, code,
test, delivery, digest, and evidence metadata. Render every declared
screenshot or video in that page. Store only those binary proof files under
`assets/`; do not add feature Markdown, JSON sidecars, or a separate
explainer. The page must remain useful from a local checkout without network
access.

After the evidence-only amend, run
`python3 scripts/validate_feature_docs.py` before claiming a feature is done.
Never use a mockup as proof of live behavior. Redact secrets and customer data
from every committed artifact. The canonical record must not depend on an
external artifact host.

Keep each feature pull request to one feature commit. Capture tests and media
from a candidate commit, compute the declared code/test digest with
`python3 scripts/validate_feature_docs.py --print-content-digest <feature-id>`,
then amend the dossier and evidence into that same commit. Use `"commit":
"self"` in its delivery entry. Stack dependent feature pull requests instead
of adding unrelated feature commits. Declare every non-dossier path changed by
the feature commit in `code_paths` or `test_paths`.

## Context docs (CONTEXT.md)

Every feature area keeps a `CONTEXT.md` next to its code so a coding agent
can load one small file and know the area's shape before reading source.
The registered areas are listed in `tests/test_context_docs.py`; that test
enforces everything mechanical below.

**Read the area's `CONTEXT.md` before working in it. Update it in the same
commit whenever your change makes one of its statements false, adds or
removes a key file, or teaches you an invariant or gotcha the file should
have told you.** A context doc that lags the code is worse than none — it
confidently misleads the next agent.

Each `CONTEXT.md` must contain exactly these four sections, in order:

```markdown
# <area> — context

## What this area does
Two to six lines. The job of this area in the product, not a file list.

## Key files
- `<path relative to this file>` — one line on its responsibility.
(Every entry must exist on disk. List load-bearing files, not all files.)

## Invariants and gotchas
- Rules that are cheaper to read than to rediscover: ordering constraints,
  fail-closed behaviors, things that look wrong but are deliberate, traps
  that have actually bitten someone.

## When to update this file
One or two bullets naming the kinds of change in THIS area that require
editing this file (new module, changed invariant, renamed surface, ...).
```

Style: plain prose, no marketing, no duplication of docstrings — link to the
file instead. Facts the repo already records elsewhere (README, dossiers,
test docstrings) get a pointer, not a copy. Keep the whole file under ~80
lines; if it wants to be longer, the area probably needs splitting.

Adding a NEW area (a new top-level package or a directory that grows its own
identity): create its `CONTEXT.md` in the same PR and register the directory
in `tests/test_context_docs.py`. Removing an area removes both.
