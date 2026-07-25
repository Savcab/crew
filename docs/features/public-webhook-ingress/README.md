# Public webhook ingress

Give local Crew webhook nodes temporary public HTTPS endpoints without exposing
the dashboard or installing an application dependency.

## What it does

`crew ingress run` starts one foreground, hook-only local gateway and one
official Cloudflare Quick Tunnel for the selected Crew project. The tunnel
reaches the gateway through a fresh private Unix socket rather than a reusable
TCP port. While that process is healthy, every existing webhook URL uses the
tunnel's HTTPS origin. Stopping the process removes the active origin and makes
the temporary URL offline.

## User experience

1. Install the official `cloudflared` binary once.
2. Run `crew ingress run` and leave it in the foreground.
3. In another shell, run `crew webhook show <name>` to copy the public secret
   POST URL.
4. Stop ingress with Control-C. `crew ingress status` then reports offline.

The dashboard, graph APIs, static assets, terminal controls, and every
non-hook path remain unreachable through the tunnel.

## Delivery slices

- One commit adds the hook-only gateway, foreground tunnel lifecycle,
  app-scoped live state, CLI, tests, and real public verification.
- The webhook-node and Foreman-control dependencies are already merged into
  `main`; this feature commit sits directly on that merged base.
- The sanitized HTML explainer, screenshot, and video live beside the feature
  in this repository. There is no separately hosted artifact.

## Read next

- [Technical specification](spec.md)
- [Verification evidence](evidence.md)
- [Visual explainer](explainer.html)
