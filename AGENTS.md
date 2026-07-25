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
