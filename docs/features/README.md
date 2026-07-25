# Feature dossiers

A feature dossier is the durable, human-readable record for one user-visible
Crew capability. It keeps the product description, technical design, diagrams,
delivery history, and verification evidence together so a reviewer does not
need to reconstruct the feature from commits and chat transcripts.

## Create a dossier

Run the repository scaffold before implementation:

```bash
python3 scripts/new_feature.py webhook-trigger-nodes \
  --title "Webhook trigger nodes" \
  --summary "Turn external HTTP payloads into durable messages for connected agents."
```

The command creates `docs/features/webhook-trigger-nodes/` from
[`_template/`](_template/) and adds it to the index below.

## Required record

| File | Reader question |
|---|---|
| `feature.json` | What is its status, dependencies, delivery lineage, tested code, and evidence inventory? |
| `README.md` | What does it do, who is it for, and where should I read next? |
| `spec.md` | How does it work, what can fail, and what are the security boundaries? |
| `evidence.md` | Which exact commit and commands prove it works? |
| `explainer.html` | Can a human understand the architecture and repository-local proof visually? |

The Markdown files follow the
[Diataxis](https://diataxis.fr/) split: the overview orients, the spec provides
reference and explanation, and evidence contains executable verification.

## Lifecycle

1. Scaffold with status `planned` before implementation.
2. Update the spec and delivery entries as the design changes.
3. Record real commands and media captured from the implemented path.
4. Put the implementation, tests, browser script, and planned dossier in one
   candidate commit. Capture proof from that revision, then amend the same
   commit with the evidence and completed dossier.
5. Set a single-commit delivery entry to `"commit": "self"`. Record the
   candidate SHA as `tested_revision`, then run
   `python3 scripts/validate_feature_docs.py --print-content-digest <feature-id>`
   and store the result as `verification.content_sha256`. The digest proves the
   declared code and test files did not change during the evidence-only amend.
   Run it from a clean candidate commit; the command reads `HEAD`, not
   uncommitted files.
6. Set status to `verified` only when the manifest names a tested candidate,
   relevant code and test paths, exact commands, and at least one
   repository-owned screenshot or video with its SHA-256 digest.
7. Change the feature index row to match the manifest status and summary.
8. Run `python3 scripts/validate_feature_docs.py`.

The validator checks structure and proof pointers. It cannot decide whether the
feature itself is correct; the repository's unit, live integration, and browser
tests remain authoritative.

For a verified dossier, `self` resolves to the commit that first added that
dossier. The validator hashes each declared path's category, repository path,
Git executable mode, and blob at that commit. Every non-dossier path changed by
the feature commit must be declared as code or test coverage. Later stacked
commits may evolve the same files without invalidating the earlier feature's
historical proof.

For already-built work, create one dossier per reviewable feature slice. Use
that slice's own implementation commit and proof; do not reuse a later
end-to-end recording to claim an earlier branch was independently exercised.
An umbrella specification may remain the canonical deep reference when several
dependent dossiers form one larger capability. Each feature pull request
should contain one feature commit; dependent features use stacked pull
requests rather than extra commits in the same feature.

## Feature index

| Feature | Status | Outcome |
|---|---|---|
| [Webhook graph nodes](webhook-nodes/) | verified | Turn JSON, form, or text webhook payloads into durable messages for every connected agent. |
| [Foreman-controlled webhook nodes](foreman-webhook-control/) | verified | Let the foreman create, configure, connect, inspect, and retire webhook nodes within explicit ownership and quota boundaries. |
| [Public webhook ingress](public-webhook-ingress/) | verified | Give local Crew webhook nodes temporary public HTTPS endpoints without exposing the dashboard or installing an application dependency. |
<!-- feature-index:append-before -->
