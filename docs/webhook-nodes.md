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

### Foreman ownership and lifecycle

The active Foreman may create and configure hooks from the CLI. Agent-created
hooks record both the creator's display name and its immutable agent GUID, and
begin unblessed so a human can review them. Ownership checks use only the GUID:
a later agent with the same name cannot inherit the hook's URL or mutation
authority.

The Foreman may manage only hooks carrying its exact creator GUID and may
connect them only as sources to nodes in the same immutable ownership envelope.
`CREW_MAX_WEBHOOKS_PER_FOREMAN` bounds live hook growth independently of the
agent-spawn quota. The hook quota check and row creation share one serialized
admission boundary, so two concurrent creates cannot both claim the last slot.

Revoking or deleting a Foreman does not disable its external integration.
Owned hooks, routes, receipts, and capability URLs remain live, while
agent-side inspection and mutation are denied. The human operator can inspect,
update, rotate, bless, or remove these orphaned hooks. Re-granting the same
surviving agent GUID restores its ownership authority; creating a same-name
replacement does not.

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
- `headers`: lower-case request headers, excluding standard credential and
  cookie headers; and
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
agent.webhook_token_hash = <domain-separated SHA-256 lookup key>
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
receipt_version, rendered_message, routes, edge_guids,
status, results, received_at, completed_at
```

Each route snapshot stores the exact edge, source, and target GUID plus the
target's name at acceptance. `edge_guids` remains as a backward-readable
summary. Historical messages keep the webhook node's immutable GUID in
`sender_guid` and the authorizing edge GUID in `edge_guid`.

## Request and delivery flow

```text
external POST
  -> resolve URL capability
  -> resume a keyed receipt before parsing, when one exists
  -> otherwise parse and render one message
  -> snapshot exact edge + endpoint identities
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
- an attached edge transform runs during acceptance and its durable result is
  reused by later retries (a crash before that row exists can rerun the script,
  so external side effects must be idempotent);
- a durable `message` row is created before success is reported; and
- the normal background flusher delivers it in FIFO order.

The endpoint returns `202 Accepted` after processing the invocation, including
`accepted`, `rejected`, and a per-target result list. That public list contains
only bounded identifiers, accepted state, safe queue status, and a generic
route-failure code; detailed backend diagnostics remain in the operator-owned
delivery record. A valid hook with no outgoing targets returns `409`. Invalid
payload/template input returns `422`; an unknown or rotated capability returns
`404`.

## Idempotency and concurrency

Providers may supply one of:

- `Idempotency-Key`;
- `X-GitHub-Delivery`;
- `X-Webhook-Id`; or
- `Webhook-Id`.

Crew hashes the header name and value before persistence. The same key and body
returns the original completed result without parsing or rendering again.
Reusing a key with a different body returns `409`.

Each invocation snapshots the rendered message and exact route identities, then
derives a stable per-edge message request ID. A retry after a process or
persistence failure resumes those immutable values; a later template/header
change cannot alter the text, and a retargeted or replaced edge cannot redirect
the old payload. Already-created message rows reconcile by request ID before
limits or transforms run again. Infrastructure failures leave the receipt in
`processing` and return a generic `503`; only durable acceptance or a terminal
route-policy/topology rejection can complete it. An app-wide webhook-delivery
lock closes the read/create race for duplicate provider requests.

Requests without a recognized idempotency header receive a fresh request ID and
are treated as distinct deliveries.

## Security

- The capability has at least 256 bits of entropy and never appears in Crew
  request logs or MorphDB query strings. MorphDB lookup uses a
  domain-separated SHA-256 value and constant-time verifies the stored raw
  capability, which is visible only through operator-authenticated graph
  responses and storage controlled by the same OS user.
- Unknown and malformed paths in the reserved `/hooks*` namespace use a
  generic `404` response.
- Capability lookup happens before the handler reads the declared body, then
  repeats before serialized dispatch. Receipt creation shares a linearization
  lock with hook updates, rotation, and deletion. Unknown callers therefore
  cannot hold a dashboard request thread open with a slow large body, while a
  request cannot validate an old capability and create its receipt after
  rotation or deletion has returned.
- Public requests cannot reach graph mutation or terminal APIs and do not
  inherit operator authority.
- Request framing retains the dashboard's strict Content-Length,
  Transfer-Encoding, and 1 MiB protections.
- Standard credential-bearing headers (`Authorization`, cookies, proxy
  credentials, and `Set-Cookie`) are never exposed to message templates.
- Per-route filesystem paths, internal URLs, exceptions, and other durable
  diagnostics never appear in the public response.
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

## Temporary public HTTPS ingress

The lean public path is a foreground operator command:

```text
crew ingress run
crew ingress status
```

`run` acquires one lifetime lock for the canonical MorphDB origin plus app,
starts a hook-only server on a fresh owner-only Unix socket, and supervises one
official `cloudflared` Quick Tunnel through a parent-death watchdog. After a
secret-gated readiness request proves the public route reaches that server,
`crew webhook show <name>` uses the temporary
`https://<label>.trycloudflare.com` origin. Control-C clears the origin and
stops both sides; a hard foreground-process death also causes the watchdog to
stop and reap its exact tunnel child.

The Quick Tunnel never points at the dashboard. The public gateway implements
only exact raw `POST /hooks/<43-character capability>` requests; dashboard,
API, static, terminal, query-bearing, and wrong-method paths return a generic
`404`; malformed HTTP receives a generic parser error. Crew adds no Python/npm
dependency and never downloads, updates, daemonizes, or logs into
`cloudflared`.

## Foreman CLI

```text
crew webhook create <name> [--description TEXT] [--template TEXT]
crew webhook list
crew webhook show <name>
crew webhook update <name> [--description TEXT] [--template TEXT]
crew webhook rotate <name>
crew webhook remove <name>
```

`list` omits URLs and raw token material. `show` is a guarded and audited secret
read. For a non-human caller, every other hook operation requires the live
Foreman flag plus an exact `created_by_guid` match. Audit arguments are
recursively sanitized so nested capability tokens, token hashes, public URLs,
templates, and credential values cannot turn the graph-edit log into a second
secret store.

## Verification plan

1. Pure template tests cover JSON, form, text, headers, missing paths, malformed
   input, empty output, and size limits.
2. Graphstore tests cover shared name uniqueness, agent/hook filtering,
   source-only edge invariants, token rotation, and cascade deletion.
3. Mail tests cover fast durable enqueue and request-ID reconciliation.
4. HTTP security tests prove hook POSTs need no operator cookie while every
   control endpoint still does, unknown capabilities are rejected before body
   read, credential headers are unavailable to templates, public failures
   remain generic, and wrong methods/framing fail cleanly.
5. Frontend tests cover hook rendering, click behavior, source normalization,
   creation/configuration controls, and copy-safe URL display.
6. Retry/race tests prove a processing receipt freezes message text and exact
   route identities, transient storage failures stay retryable, delete cannot
   resurrect a hook, and raw capabilities never enter backend query strings.
7. A live end-to-end test creates one webhook and two stopped agents, connects
   both, POSTs JSON through the unauthenticated public URL, and verifies two
   durable queued messages with the rendered body and exact edge provenance.
