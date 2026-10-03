# 0018. Adopt python-copier-template

- **Date:** 2026-10-03
- **Status:** accepted (being adopted in stages)
- **Origin:** an interactive design session in the private development repository

## Context

The project already had most of the shape of
[DiamondLightSource/python-copier-template](https://github.com/DiamondLightSource/python-copier-template):
a `src/` layout, `uv`, pytest, and Sphinx docs in the Diataxis layout. It had no licence, no
linting, formatting or type checking, and no way to pick up improvements to that tooling.

## Decision

Adopt the template, in stages: the licence first, then the template scaffold (copier answers,
pre-commit with gitleaks, Renovate, the repository templates), then a one-off format and lint
pass, then the dependency groups and the deploy, then type checking. The project keeps what the
template doesn't cover: the deploy scripts, the systemd units, the data template and its own
Sphinx extensions.

These records follow the template's convention: they live in `docs/explanations/decisions/`. The
template's own seed records (recording decisions, and switching to the template) are covered by
this index and this record, so they aren't added again.

## Consequences

The tooling stays consistent with other projects built on the template, and `copier update` can
bring improvements in. Some project-specific choices override the template's defaults, for
example a longer line length, which suits the existing code.
