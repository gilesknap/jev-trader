# 0019. Run on the home Kubernetes cluster, with the trust split carried by namespaces

- **Date:** 2026-10-12
- **Status:** proposed
- **Origin:** a design session; the full design is in [#85](https://github.com/gilesknap/jev-trader/issues/85)

## Context

The system runs on a VPS that it now shares with another project which is deliberately given sudo
and a free hand. The runner and the strategist are kept apart by Unix accounts, file modes,
Claude Code deny rules and the wrapper's path check ([0001](0001-separate-runner-and-strategist-accounts.md)),
all verified by `deploy/setup/check.sh`. A neighbour with root on the same host can get round all of
them.

A home k3s cluster is available, with an NFS-backed NAS. Kubernetes can carry the same split as
separate namespaces, Secrets and network policies, on hosts the other project doesn't touch.

The trust model doesn't change. The runner is trusted: it holds the live keys and enforces the
guardrails. The strategist is not: it is an LLM that reads untrusted text. The one place the runner
runs strategist-written code is custom features ([0004](0004-sandbox-strategist-features.md)), so
those must run in a process with no keys, no network and no access to runtime state.

## Decision

- **Three namespaces.** `trading-runner` (trusted: live and paper keys, OpenRouter, alerts),
  `trading-strategist` (untrusted: the Claude token, paper keys, the data-repository token) and
  `trading-features` (untrusted code, no privileges). Namespaces, not just service accounts,
  because anyone who can create a pod in a namespace can mount any Secret in it.
- **The runner is a single-replica StatefulSet.** It won't be recreated until the old pod is
  confirmed gone, so a lost node gives no runner, never two. The pod stays up and sleeps between
  sessions. With `updateStrategy: OnDelete` a new image or deployed configuration takes effect
  only when the pod is deleted. `OnDelete` doesn't stop anyone deleting the pod mid-session, so
  whatever deletes it (by hand or a roll job after the close) first checks that the runner is
  idle, as the deploy's session-lock check does now ([0007](0007-deploy-from-signed-merges.md)).
  Images are built only from signed merge commits on `main` whose tests pass, keeping 0007's
  guarantees.
- **Custom features run in a separate feature-worker pod**, not under bubblewrap in the runner's
  pod. It has no Secrets, no service-account token and no volumes, runs non-root with a read-only
  root filesystem and no capabilities, and its network policy denies all egress and allows
  ingress only from the runner. The worker keeps its own memory and file-descriptor limits
  (`trader.features.worker`), and the pod has a memory limit as well. At session start the
  runner sends it the feature source, so the code that passed the morning gate is the code that
  runs all day. The JSON-lines protocol is
  unchanged, over TCP rather than a pipe. It still fails closed: if the worker is unreachable,
  custom features are NaN and nothing enters. The static import gate stays. `trader validate`
  and replays in the strategist's pod run custom features too: they use a worker of their own
  with the same limits, never the runner's, so the strategist can't change what the runner's
  worker runs during a session.
- **The strategist's runs are CronJobs** with `timeZone: America/New_York` (stable since
  Kubernetes 1.27),
  `concurrencyPolicy: Forbid` and a starting deadline. They replace the systemd user timers
  ([0012](0012-systemd-timers-for-the-strategist.md)). `scripts/strategist.sh` runs in split mode
  as today.
- **The Claude subscription login is a `claude setup-token` token** in a Secret, passed as
  `CLAUDE_CODE_OAUTH_TOKEN`. Nothing is written back, so the pod can be ephemeral. The strategist
  keeps running the Claude Code CLI, which is what the subscription covers. The token's issue
  date is a second key in the same Secret, passed to the pod, and housekeeping's expiry check
  reads that, so the strategist needs no access to the Kubernetes API.
- **Storage is local to one node, backed up to NFS.** The runtime directory and the strategist
  checkout are local-path volumes. The runner and the strategist are pinned to that node, so
  `flock` stays on a local filesystem. A CronJob copies the runtime directory to the NAS after the
  close, while the runner is idle. The strategist checkout is also pushed to GitHub after every
  run. The feature worker can run on any node.
- **Cilium enforces network policy, by domain name.**
  - The strategist reaches Anthropic, GitHub and Alpaca's data and paper APIs.
  - The runner reaches Alpaca, OpenRouter, ntfy and the feature worker.
  - The feature worker reaches nothing.
  - The dashboard accepts traffic only from the Tailscale proxy, so the `Tailscale-User-Login`
    header can't be forged from another pod ([0002](0002-dashboard-on-a-private-socket.md)).
  - Hubble's flow logs are used to build the allowlists before they are enforced. Whether the
    strategist's WebFetch gets general web egress is decided then, explicitly; WebSearch runs on
    Anthropic's side and needs none.
- **The runner keeps reading the strategist's files through `trader.safeio`**
  ([0017](0017-strategist-checkout-is-untrusted-input.md)): mounting them read-only protects the
  files, not the reader.
- **An isolation-check Job replaces `check.sh`** for the cluster. It asserts what each service
  account can and can't read, that the feature worker has no egress, and that the strategist can't
  write the runtime directory.

## Consequences

- No automatic failover. If the pinned node is lost, the runtime directory is restored from the
  NAS by hand, and the VPS stays as the rollback. Before the runner's pod is force-deleted or the
  runner started anywhere else, the lost node is powered off and confirmed down: deleting the pod
  through the API doesn't stop a process on a node the control plane can't reach.
- The cluster's control plane becomes the trust boundary: whoever can deploy to the cluster can
  read the live keys, so the cluster's GitOps repository and admin credentials need the care the
  VPS's admin account gets now. k3s should encrypt Secrets at rest.
- Home power and connectivity now affect trading. Before live trading on the cluster, add an
  off-cluster dead-man's switch that flattens the live account if the runner's heartbeat stops
  near the close.
- Cilium has to replace k3s's built-in networking cluster-wide. That's a prerequisite, done in the
  cluster's own provisioning before this moves.
- Custom features gain a network hop per call, which must stay well inside the 5 ms budget.
- The VPS setup scripts and docs remain the supported route until the cutover is proven on paper.
- If this is accepted, it supersedes [0012](0012-systemd-timers-for-the-strategist.md) on the
  cluster, and replaces the mechanisms, not the aims, of
  [0001](0001-separate-runner-and-strategist-accounts.md) (accounts),
  [0002](0002-dashboard-on-a-private-socket.md) (the socket),
  [0004](0004-sandbox-strategist-features.md) (bubblewrap) and
  [0007](0007-deploy-from-signed-merges.md) (the session lock). Those records point here once the
  cutover is done.
