# 0004. Strategist-authored features run in a sandbox

- **Date:** 2026-09-27
- **Status:** accepted
- **Origin:** #13, #27 in the private development repository

## Context

The strategist may write its own feature functions, and the runner computed them in-process,
next to the live keys. A static allow-list gate was the only protection, and the review showed
it could be bypassed (finding #13, rated critical).

## Decision

- The runner never imports custom code. One long-lived worker computes custom features under
  bubblewrap: no network, a cleared environment, a private `/tmp`, read-only system and code
  directories (including the interpreter's own environment), and nothing from home directories,
  the runtime directory or secrets files. Memory and file
  descriptors are limited.
- It fails closed: if the worker dies, answers badly or times out, custom features are NaN for
  the rest of the session, with an urgent alert. Triggers then fail, so nothing enters; stops
  and risk checks are unaffected.
- The static check stays as defence in depth: exact-module imports, no private or I/O
  attributes, no path or URL literals, no introspection builtins.

## Consequences

Custom features are limited to pure functions of the bars they're given. `trader validate`,
replays and the runner all use the same sandbox path, so a feature that validates behaves the same
live.
