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

Keep the dossier current as the implementation changes:

- `README.md` explains the feature and links the rest of the record.
- `spec.md` records goals, user flow, public surface, architecture, security,
  failure behavior, and rollout. Include at least one Mermaid diagram.
- `evidence.md` records the exact tested commit, commands, decisive outputs,
  and links to real screenshots or video.
- `explainer.html` is a network-free visual explanation with an inline diagram
  and repository-local media from `evidence/` when the feature is verified.
  Published copies inline that media so the shared artifact is self-contained.
- `feature.json` is the machine-readable status, dependency, code, test,
  delivery, and artifact index.

After the evidence-only amend, run
`python3 scripts/validate_feature_docs.py` before claiming a feature is done.
Never use a mockup as proof of live behavior. Redact secrets and customer data
from every committed or linked artifact.

Keep each feature pull request to one feature commit. Capture tests and media
from a candidate commit, compute the declared code/test digest with
`python3 scripts/validate_feature_docs.py --print-content-digest <feature-id>`,
then amend the dossier and evidence into that same commit. Use `"commit":
"self"` in its delivery entry. Stack dependent feature pull requests instead
of adding unrelated feature commits. Declare every non-dossier path changed by
the feature commit in `code_paths` or `test_paths`.
