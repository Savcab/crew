# Webhook nodes

## Goal

Add source-only nodes to the Crew graph that turn an external HTTP request into
one durable Crew message for every connected agent.

An operator can:

1. create a webhook node on the graph;
2. copy its capability URL;
3. optionally configure a payload-to-message template;
4. connect the node to one or more agents; and
5. observe each accepted message on the existing edges and in Crew's message
   log.

This feature is an ingress adapter, not a second messaging system. Once the
request has been converted to text, the existing edge authorization, rate and
budget limits, optional edge transform, durable queue, delivery worker, and
message audit trail remain authoritative.

## Product behavior

### Graph model

- A webhook is a distinct, source-only node with a name and description.
- A webhook can have any number of directed outgoing edges to agent nodes.
- A webhook cannot receive an edge, connect to another webhook, create a
  two-way edge, or request a reply.
- Dragging between a webhook and an agent always normalizes to
  `webhook -> agent`, regardless of drag direction.
- Clicking an agent opens its terminal. Clicking a webhook opens its
  configuration, URL, rotation, and deletion controls.

### Public URL

Each webhook owns a random 256-bit URL-safe capability:

```text
POST /hooks/<capability>
```

The route intentionally does not use the dashboard's operator cookie or CSRF
header. Possession of the unguessable URL is the authorization.

The dashboard continues to bind to loopback. On a local installation, the UI
shows a loopback URL. To receive internet traffic, the operator exposes
`/hooks/*` through a TLS reverse proxy or tunnel and sets
`CREW_WEBHOOK_PUBLIC_BASE_URL` to that public origin. This preserves the
existing rule that terminal and graph control APIs are never directly bound to
the network.

Rotating a URL replaces the capability immediately. Deleting a webhook removes
the node and its incident edges; it does not delete historical message or
delivery audit rows.

### Accepted payloads

The endpoint accepts bodies up to the dashboard's existing 1 MiB request limit:

- `application/json` and `application/*+json`: any valid JSON value;
- `application/x-www-form-urlencoded`: parsed into a key/value object; and
- every other content type: UTF-8 text.

Malformed JSON and invalid UTF-8 are rejected before any message is accepted.

### Message template

Templates are plain text with deterministic placeholders:

```text
GitHub issue: {{ payload.issue.title }}
Repository: {{ payload.repository.full_name }}
Event: {{ headers.x-github-event }}
```

Available roots are:

- `payload`: the parsed JSON/form/text value;
- `headers`: lower-case request headers; and
- `raw`: the original UTF-8 body.

Dot segments walk object keys and numeric array indexes. Missing paths,
unmatched template braces, an empty result, or an oversized result reject the
invocation without sending a partial message.

When the template is blank, Crew uses `payload.message`, then `payload.text`
when either is a non-empty string, and otherwise serializes the complete parsed
payload. This makes common webhook shapes useful without configuration.

The first version deliberately does not execute Python or JavaScript stored in
the database. A text/template transform is enough to parse and format webhook
payloads without turning an unauthenticated request body into input for
host-level arbitrary code. Existing human-attached per-edge transform scripts
still run after the node template if a deployment needs code-level processing.

## Persistence

Webhook nodes share the existing `agent` object type so current `edge.source`
and `edge.target` relations remain valid. The `kind` discriminator separates
runtime agents from webhooks:

```text
agent.kind = "webhook"
agent.webhook_token = <random capability>
agent.webhook_template = <template text>
agent.webhook_last_called_at = <unix timestamp>
agent.webhook_last_status = <summary>
```

Agent-list, quota, tmux, lifecycle, grant, and workspace operations exclude
`kind="webhook"`. Name uniqueness is shared across both node kinds so edge
resolution and canvas positions are unambiguous.

`webhook_delivery` is an append-only invocation record:

```text
hook_guid, request_id, idempotency_key_hash, payload_hash,
edge_guids, status, results, received_at, completed_at
```

Historical messages keep the webhook node's immutable GUID in `sender_guid`
and the authorizing edge GUID in `edge_guid`.

## Request and delivery flow

```text
external POST
  -> resolve URL capability
  -> parse body
  -> render one message
  -> snapshot outgoing edges
  -> create/resume webhook_delivery
  -> enqueue once per snapshotted edge
  -> return 202 with per-target results
  -> existing background flusher delivers queued messages
```

The HTTP request only performs durable acceptance; it does not wait for an
agent runtime to become idle. This keeps provider response times independent of
the number or state of connected runtimes.

For each target, enqueueing reuses the existing mail gate:

- the exact edge and endpoint identities are revalidated under the graph lock;
- the edge's hourly message, token, and cost caps apply;
- an attached edge transform runs exactly once;
- a durable `message` row is created before success is reported; and
- the normal background flusher delivers it in FIFO order.

The endpoint returns `202 Accepted` after processing the invocation, including
`accepted`, `rejected`, and a per-target result list. A valid hook with no
outgoing targets returns `409`. Invalid payload/template input returns `422`;
an unknown or rotated capability returns `404`.

## Idempotency and concurrency

Providers may supply one of:

- `Idempotency-Key`;
- `X-GitHub-Delivery`;
- `X-Webhook-Id`; or
- `Webhook-Id`.

Crew hashes the header name and value before persistence. The same key and body
returns the original completed result without creating more messages. Reusing a
key with a different body returns `409`.

Each invocation snapshots its edge GUIDs and derives a stable per-edge message
request ID. A retry after a process or persistence failure resumes the same
invocation; already-created message rows reconcile by request ID before limits
or transforms run again. An app-wide webhook-delivery lock closes the
read/create race for duplicate provider requests.

Requests without a recognized idempotency header receive a fresh request ID and
are treated as distinct deliveries.

## Security

- The capability has at least 256 bits of entropy and never appears in server
  logs. It is visible only through operator-authenticated graph responses and
  the local MorphDB data controlled by the same OS user.
- Unknown capabilities use a generic `404` response.
- Public requests cannot reach graph mutation or terminal APIs and do not
  inherit operator authority.
- Request framing retains the dashboard's strict Content-Length,
  Transfer-Encoding, and 1 MiB protections.
- Templates are interpreted data, not executable code.
- Edge limits provide target-specific abuse containment. Operators should
  configure limits appropriate to the upstream provider.
- Internet exposure should terminate TLS and apply any provider signature or
  IP policy at the reverse proxy. Provider-specific signature verification can
  be added later without changing the graph or delivery model.

## Dashboard API

Operator-authenticated endpoints:

```text
POST /api/webhook/create  {name, description, template}
POST /api/webhook/update  {guid, description, template}
POST /api/webhook/rotate  {guid}
POST /api/webhook/delete  {guid}
```

`GET /api/graph/snapshot` adds a `webhooks` list. Each webhook includes a
`public_url` but not the standalone token field. Edges include endpoint kinds
so the UI can render and edit webhook routes correctly.

Public endpoint:

```text
POST /hooks/<capability>
```

## Verification plan

1. Pure template tests cover JSON, form, text, headers, missing paths, malformed
   input, empty output, and size limits.
2. Graphstore tests cover shared name uniqueness, agent/hook filtering,
   source-only edge invariants, token rotation, and cascade deletion.
3. Mail tests cover fast durable enqueue and request-ID reconciliation.
4. HTTP security tests prove hook POSTs need no operator cookie while every
   control endpoint still does, and that wrong methods/framing fail cleanly.
5. Frontend tests cover hook rendering, click behavior, source normalization,
   creation/configuration controls, and copy-safe URL display.
6. A live end-to-end test creates one webhook and two stopped agents, connects
   both, POSTs JSON through the unauthenticated public URL, and verifies two
   durable queued messages with the rendered body and exact edge provenance.
