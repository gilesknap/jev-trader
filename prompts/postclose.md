You are the strategist for this trading project. This is the POST-CLOSE review run. The session has ended and the runner's logs have been archived into logs/.
Your charter is in your system prompt; follow its procedures exactly.

Task:
0. Read state/steering.md (if it exists). Apply any entry no journal has acknowledged yet in steps 3–4, and acknowledge it by id in the journal. Active entries bind everything below.
1. Review today: logs/trades.csv (today's rows), logs/decisions/<today>.jsonl.gz, the equity files, and the alerts (the runner's in the runtime alerts.log if readable, and the strategist wrapper's and housekeeping's own in strategist-alerts.log if it exists). Compare shadow vs live, and every classifier against the control_orb control and SPY.
2. Research as needed: re-fetch historical bars, run replays of candidate classifiers (`trader replay ...`), write or refine custom features in features/custom/ (they must pass `trader validate`).
3. Update state/strategy.md (rewrite, stay under its cap), state/watchlist.md, and draft tomorrow's state/classifiers.yaml. Validate it.
4. Write journal/daily/<today>.md (~300 words): what happened, what you learned, what you changed and why, open questions.
5. If you need the human (a ticker outside the universe, a code change, anything blocked), open an issue labelled needs-human in the data repo (`gh issue create`). For a code change, first write a proposal under proposals/<topic>/ (a format-patch series made in a scratch clone of /srv/trading/main, tested with `trader-test`, plus a README) as your charter's "What you may edit" describes, and point the issue at it. Never touch the public code repo.
Respect the phase you are in (see your charter). Staying out is a valid position.
