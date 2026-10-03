You are the strategist for this trading project. This is the PRE-MARKET run (US open is 30–75 minutes away).
Your charter is in your system prompt; follow its procedures exactly.

Task, kept short (target under 10 minutes):
1. Read state/strategy.md, state/classifiers.yaml and the most recent journal/daily entry (which drafted today's plan).
2. Check overnight/pre-market context: major news, index futures, pre-market movers in the universe, scheduled macro events today (CPI, FOMC, etc.). Use web search and the Alpaca news API sparingly.
3. Decide: confirm, tweak or stand down each classifier for today. Edit state/classifiers.yaml only if something material changed (set `date:` to today). Always run `trader validate` after any edit and fix problems until it passes.
4. Append a 2–5 line section headed exactly `## Pre-market` to today's journal/daily/<date>.md (create it if needed) noting what you checked and any change. The dashboard shows this section on its Today page, so keep the heading as written.
Do not rewrite strategy.md in this run.
