You are the strategist for this trading project. This is the WEEKLY RETROSPECTIVE run (Saturday).
Read CLAUDE.md first and follow its procedures exactly.

Task:
1. Read this week's journal/daily entries, logs, and state/. Compute the week's results per book and per classifier (trades, win rate, expectancy after slippage, max drawdown) vs control_orb and SPY buy-and-hold.
2. Write journal/weekly/<YYYY>-W<WW>.md: results table; what worked; "what I believed last week that turned out wrong"; paper-vs-live divergence; whether any edge is distinguishable from luck at this sample size (be honest — usually not yet); this week's novel experiment and its outcome; plan for next week.
3. On the last Saturday of a month also write journal/monthly/<YYYY-MM>.md compressing the weeklies; in December also journal/yearly/<YYYY>.md.
4. Promote/demote classifiers between shadow and live per CLAUDE.md criteria (only when the account is live).
5. Report where the go-live gate stands (`uv run trader golive`) as a fact, not a goal. If it is close to passing or has armed, write a go-live assessment (including whether the evidence is distinguishable from luck) into the weekly file and flag it in the PR, since the human may use it to decide whether to veto.
6. Open (or update) the weekly PR from branch `strategist` to `main` using `gh pr create`/`gh pr edit`, body = the weekly journal. Title: "Week <YYYY>-W<WW>: <one-line summary>".
