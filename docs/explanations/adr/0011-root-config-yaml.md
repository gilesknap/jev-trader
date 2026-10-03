# 0011. Personal and deployment settings live in one config.yaml

- **Date:** 2026-09-29
- **Status:** accepted
- **Origin:** #144 in the private development repository

## Context

Owner-specific values (dashboard users, ports, schedule, models, start date, the repository
slug) were scattered through code, units and setup scripts. Making a copy of the system someone
else's meant hunting through the tree.

## Decision

A root `config.yaml` holds everything personal or deployment-specific, and the setup scripts
render units from it. The code has no built-in dashboard user: an empty list lets nobody in.
**Safety rules stay in code on purpose**: guardrails, gate thresholds and equity floors aren't
configurable.

## Consequences

A new owner edits one file. After the repository split
([0014](0014-public-code-private-data.md)) that file lives in the owner's private data repository,
and its changes are always shown in full at deploy time.
