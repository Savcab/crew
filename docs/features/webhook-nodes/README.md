# Webhook graph nodes

Crew operators can add a source-only hook to the graph, format an incoming HTTP
payload as text, and fan that message out durably to every connected agent.

## What it does

Each hook owns a 256-bit URL capability and accepts JSON, form-encoded, or UTF-8
text POST bodies. Crew applies an optional deterministic template, freezes that
rendered message and the hook's exact outgoing route identities, and creates
one durable message per target through the existing mail gate. Edge
authorization, transforms, rate and budget caps, idempotency, provenance, and
FIFO delivery remain authoritative.

Hooks are sources only: they cannot receive edges, connect to another hook, or
receive replies.

## User experience

Select **+ Hook** in the dashboard, give the node a name and optional template,
then connect it to one or more agents. Clicking the hook provides authenticated
controls to edit it, rotate its URL, or delete it.

A valid POST returns `202` with accepted and rejected target counts. Connected
agents receive ordinary Crew messages carrying immutable hook and edge
provenance. Rotating the URL invalidates the old capability immediately.

This slice serves `/hooks/*` on Crew's loopback dashboard. It does not create a
public tunnel. An operator bringing a proxy must publish only the hook path;
the stacked public-ingress feature adds managed Internet exposure through a
dedicated hook-only gateway.

## Delivery slices

- This feature commit adds the node model, dashboard controls, safe payload
  templates, durable idempotent fan-out, and loopback HTTP ingress.
- `foreman-webhook-control` adds bounded agent and Foreman configuration.
- `public-webhook-ingress` adds managed Cloudflare exposure and isolates the
  hook path from the dashboard control plane.

## Read next

- [Technical specification](spec.md)
- [Verification evidence](evidence.md)
- [Visual explainer](explainer.html)
- [Umbrella webhook specification](../../webhook-nodes.md)
