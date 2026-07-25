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
4. Commit the implementation first. Capture proof from that immutable revision,
   then add or update the dossier in a following commit so the tested SHA is not
   self-referential.
5. Set status to `verified` only when the manifest names a tested commit,
   relevant code and test paths, delivery commits, exact commands, and at least
   one repository-owned screenshot or video with its SHA-256 digest.
6. Change the feature index row to match the manifest status and summary.
7. Run `python3 scripts/validate_feature_docs.py`.

The validator checks structure and proof pointers. It cannot decide whether the
feature itself is correct; the repository's unit, live integration, and browser
tests remain authoritative.

For already-built work, create one dossier per reviewable feature slice. Use
that slice's own implementation commit and proof; do not reuse a later
end-to-end recording to claim an earlier branch was independently exercised.
An umbrella specification may remain the canonical deep reference when several
dependent dossiers form one larger capability.

## Feature index

| Feature | Status | Outcome |
|---|---|---|
<!-- feature-index:append-before -->
