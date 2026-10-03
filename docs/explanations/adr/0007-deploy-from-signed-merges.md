# 0007. Deploys: tested candidates, signed merges, never mid-session

- **Date:** 2026-09-27
- **Status:** accepted
- **Origin:** #37, #41, #111 in the private development repository

## Context

Every deploy showed the full code diff and required a typed "yes". That review duplicated the
pull-request review, and the deploy itself had two hazards: it switched the runner's code before
running the tests, and it could replace the checkout during a trading session.

## Decision

- The reviewed commit is pinned. Its tests run in a temporary worktree with its own environment,
  and the live checkout switches only if they pass.
- Merges made through GitHub's web UI are two-parent merge commits signed with GitHub's key.
  The deploy lists those pull requests without a diff, since they were reviewed in the PR.
  Anything else still shows its diff for review. Configuration changes always do.
- The runner holds a shared session lock for its whole process lifetime. The deploy refuses
  while the runner is active, and takes the lock exclusively only around the switch.

## Consequences

Review happens once, in the pull request. Merges must use **Create a merge commit**, not
squash or rebase. Deploys happen between the close and the next session start.
