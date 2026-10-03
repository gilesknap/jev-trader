# 0014. Public code repository, private data repository per owner

- **Date:** 2026-10-03
- **Status:** accepted
- **Origin:** #169 in the private development repository

## Context

Everything lived in one private repository: code, configuration, and the strategist's journals,
positions and P&L. Sharing the code meant hand-made snapshots. A split was first considered on
2026-09-28 and dropped, over prompt injection through public issues, leaks through proposal
pull requests, and personal details spread through the tree.

## Decision

- **Public `jev-trader`:** code, tests, docs, prompts, the charter, the systemd units, the setup
  scripts, `trading-deploy`, and a data template for new owners.
- **Private data repository per owner**, with two branches: `main` for the human-owned deployment
  configuration (deployed with the code) and `strategist` for the strategist's own data.
- The strategist never touches the public repository. It can't open code PRs; it writes
  **proposals** (a `git format-patch` series and a rationale) into its data branch, with a
  `needs-human` issue. The human reviews them and opens the public PR.

## Consequences

Anyone can run their own copy from the public code. Strategy, results and personal details stay
private. The cost is a three-checkout deployment, and a deploy that reviews two repositories.
