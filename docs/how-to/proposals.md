# Review the strategist's proposals

The strategist can change only its own data. When it wants a change to the code, the universe,
the prompts or its charter, it writes a **proposal** instead: a patch series and a rationale on its
branch of your data repository, and a `needs-human` issue there pointing at it. Its GitHub token
reaches only your data repository, so it can't open a pull request on the public code itself, and
its reasoning (which may mention your strategy or results) stays private until you decide what to
publish.

## What a proposal looks like

```
proposals/<topic>/README.md        the problem, the evidence, what the tests show, the risk
proposals/<topic>/0001-....patch   a git format-patch series against the deployed code
```

The strategist makes the change in a scratch clone of `/srv/trading/main`, so the series applies
to the commit that was deployed when it wrote it. It tests it there with `trader-test`, which runs
the clone's code in `trader`'s environment; a change that needs new or different dependencies
can't be tested that way, and its README should say so. The tests it ran are its own choice:
run the whole suite yourself.

It's on the `strategist` branch of your data repository (and in `/srv/trading/strategist` on the
host), committed and pushed by the wrapper like any other strategist file. The issue links to it.

## Review and apply it

1. Read the README and the patches. Treat them as you would a pull request from a stranger: the
   strategist wrote them, and they will run next to your money if you deploy them.
2. In a clone of **your fork** of the code, on a fresh branch from the commit the patch was made
   against (normally the one you have deployed), apply the series:

   ```bash
   git switch -c <topic> <deployed sha>
   git am /path/to/data-repo/proposals/<topic>/*.patch
   uv run pytest -q
   ```

   If upstream has moved on, rebase the branch onto it and fix any conflicts yourself.
3. Open the pull request on `gilesknap/jev-trader` (or, with option B of
   [Take updates](take-updates.md), first on your own fork) **with your own description**. Leave
   out or summarise anything private from the strategist's README: strategy details, results,
   account sizes. Never paste data repository content into a public issue or pull request.
4. Close the `needs-human` issue with a link to the public pull request, or with your reason for
   declining it.

Merged and deployed code reaches the strategist at its next run: it runs the deployed code.

## Keeping it tidy

Once a proposal is merged or declined, its directory has done its job. You can delete it with a
commit to the `strategist` branch, as `trader` in `/srv/trading/strategist`, between strategist
runs. A proposal built on old code may no longer apply cleanly; for a large one, declining it and
saying why in the issue is usually better than repairing a stale patch by hand.
