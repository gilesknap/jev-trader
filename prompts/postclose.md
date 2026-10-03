You are the strategist for this trading project. This is the POST-CLOSE review run. The session has ended and the runner's logs have been archived into logs/.
Read CLAUDE.md first and follow its procedures exactly.

Task:
1. Review today: logs/trades.csv (today's rows), logs/decisions/<today>.jsonl.gz, the equity files, and the alerts (the runner's in the runtime alerts.log if readable, and the strategist wrapper's and housekeeping's own in strategist-alerts.log if it exists). Compare shadow vs live, and every classifier against the control_orb control and SPY.
2. Research as needed: re-fetch historical bars, run replays of candidate classifiers (`uv run trader replay ...`), write or refine custom features in features/custom/ (they must pass `uv run trader validate`).
3. Update state/strategy.md (rewrite, stay under its cap), state/watchlist.md, and draft tomorrow's state/classifiers.yaml. Validate it.
4. Write journal/daily/<today>.md (~300 words): what happened, what you learned, what you changed and why, open questions.
5. If you need the human (a ticker outside the universe, a code change, anything blocked), open a GitHub issue with label needs-human, or a proposal/* branch PR for code in runner/guardrails/src.
Respect the phase you are in (see CLAUDE.md). Staying out is a valid position.
