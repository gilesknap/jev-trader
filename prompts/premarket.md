You are the strategist for this trading project. This is the PRE-MARKET run (US open is 30–75 minutes away).
Your charter is in your system prompt; follow its procedures exactly.

Task, kept short (target under 10 minutes):
1. Read state/strategy.md, state/classifiers.yaml, state/steering.md and the most recent journal/daily entry (which drafted today's plan). If steering.md (when it exists) has an entry no journal has acknowledged yet and it affects today's classifiers, apply that part now and note it in step 4 as "S<n>: applied to today's classifiers, acknowledgement pending"; leave the full application and the acknowledgement to the post-close run.
2. Check overnight/pre-market context: major news, index futures, pre-market movers in the universe, scheduled macro events today (CPI, FOMC, etc.). Use web search and the Alpaca news API sparingly.
3. Decide: confirm, tweak or stand down each classifier for today (don't tweak a rule whose hypothesis is confirming: see your charter's Hypotheses). Edit state/classifiers.yaml only if something material changed (set `date:` to today). Always run `trader validate` after any edit and fix problems until it passes.
4. Append a 2–5 line section headed exactly `## Pre-market` to today's journal/daily/<date>.md (create it if needed) noting what you checked and any change. The dashboard shows this section on its Today page, so keep the heading as written, and use `###` for any sub-heading inside it (a `##` sub-heading ends the section early on the dashboard).
Do not rewrite strategy.md in this run.
