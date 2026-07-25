# Foreman-controlled webhook nodes

Crew's Foreman can build an externally triggered workflow without asking the
operator to configure each hook by hand. It can create a source-only webhook,
inspect and update that hook, connect it to agents in its ownership envelope,
rotate the capability URL, and retire the hook.

## What it does

The existing `crew webhook` commands become available to the active Foreman.
Every non-human operation is tied to the Foreman's immutable agent GUID:

- a Foreman can manage only hooks it created;
- an unblessed hook or route is visible to the operator for review;
- a per-Foreman hook limit bounds unsupervised growth; and
- hook URLs, tokens, hashes, and templates are removed recursively from audit
  arguments.

Hook ownership does not depend on a reusable display name. Revoking or deleting
the Foreman leaves its hooks live for the human operator. A later agent with the
same name has a different GUID and cannot inherit those capabilities.

## User experience

From its managed terminal, the Foreman can run:

```bash
crew webhook create github-issues \
  --description "GitHub issue events" \
  --template "Issue {{ payload.issue.title }}"
crew connect github-issues triage \
  --max-turns 10 --token-cap 50000 --cost-cap 1
crew webhook show github-issues
```

`list` is safe for graph discovery and omits secret URLs. `show`, `update`,
`rotate`, and `remove` require either the human operator or the exact active
Foreman that created the hook. Humans can bless the hook or its outgoing route
with the existing graph-aware `crew bless` commands.

## Delivery slices

- `webhook-nodes` provides hook storage, safe request parsing, durable fan-out,
  and the operator dashboard.
- This feature adds bounded, auditable Foreman control through the CLI.
- `public-webhook-ingress` later gives a configured hook an Internet-reachable
  origin without exposing Crew's dashboard control plane.

## Read next

- [Technical specification](spec.md)
- [Verification evidence](evidence.md)
- [Visual explainer](explainer.html)
- [Umbrella webhook specification](../../webhook-nodes.md)
