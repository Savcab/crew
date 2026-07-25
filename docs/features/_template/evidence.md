# {{FEATURE_TITLE}} verification evidence

## Tested commit

`pending`

Replace `pending` with the exact 40-character implementation commit SHA before
setting the feature status to `verified`.

Content digest: `pending`

After the implementation and tests are stable, compute this value with
`python3 scripts/validate_feature_docs.py --print-content-digest {{FEATURE_ID}}`.
The digest binds this evidence to the declared code and test files even when
the tested candidate commit is amended to add the dossier evidence. Run the
command only after committing every declared path; it refuses dirty candidate
content.

## Commands and results

TODO(feature): Record the exact commands and decisive output. Do not replace
command evidence with a prose claim.

```text
$ command-to-run
expected decisive output
```

## Media evidence

TODO(feature): Link screenshots or videos captured from the real
implementation. Show the tested commit, command or interaction, and result.

- Pending

## Safety and redaction

TODO(feature): State what fixtures were used, what was cleaned up, and which
secrets or customer data were redacted. Never present staged or mocked media as
live proof.
