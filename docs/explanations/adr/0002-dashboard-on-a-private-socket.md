# 0002. The dashboard listens on a private Unix socket behind Tailscale

- **Date:** 2026-09-27
- **Status:** accepted
- **Origin:** #3 in the private development repository

## Context

The dashboard authenticated users by the `Tailscale-User-Login` header. Served on
`127.0.0.1`, any local account, including `trader`, could forge that header and view the
dashboard or press STOP.

## Decision

The dashboard serves on a Unix socket in the runner's private (0700) runtime directory, and
`tailscale serve` proxies to it. The only path in is through the tailnet, where `tailscaled` sets
the header. TCP mode stays for local development. Later hardening (2026-09-28) also refuses
cross-site STOP and HOLD LIVE requests.

## Consequences

Identity on the dashboard is as strong as the tailnet login. Machines without Tailscale need
an SSH tunnel to the socket (added 2026-09-29).
