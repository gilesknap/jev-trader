# Classifier schema

`state/classifiers.yaml` in the strategist checkout holds the classifiers for the next session.
It is validated by `src/trader/classifier.py`; [How a decision is made](../explanations/decisions.md)
explains what the fields do at run time.

```yaml
date: 2026-10-06            # the session this was written for (informational)
classifiers:
  - id: my_idea
    mode: shadow
    family: novel
    enabled: true
    symbols: [QQQ, NVDA]
    window: ["10:00", "15:00"]
    cadence_min: 2
    trigger:
      - {feature: vwap_dist_pct, op: ">", value: 0.1}
    features: [vwap_dist_pct, rel_volume_15m]
    context: "Why this classifier exists and what it's looking for."
    entry:
      instructions: "Should we open a long position now?"
      criteria:
        ENTER: "..."
        WAIT: "..."
        STAND_DOWN: "..."
      threshold: 0.65
    exit:
      instructions: "We hold a long. Keep holding?"
      criteria:
        HOLD: "..."
        EXIT: "..."
      threshold: 0.65
    size_fraction: 0.2
    max_trades: 2
    after_exit: rearm
    stop_pct: 0.5
    target_pct: 1.0
```

## Validation

- **Keys are strict.** An unknown or misspelt key (say `trail_pc`) fails `trader validate`, with
  a "did you mean" hint. At session start the runner leaves just that classifier out for the day,
  with an urgent alert, and the rest trade as normal. Any other problem rejects the whole file,
  and nothing trades that day.
- Every symbol must be in `config/universe.yaml`, and every feature (in `features` and `trigger`)
  must be a library feature or a custom feature that passed the gate.
- `trader validate` also requires an honest `family` label, and from `experiment.start_date`
  rejects `test_` ids.
- The file must be a real file, not a symlink: the runner doesn't follow symlinks in `state/`.
- Only `enabled: true` classifiers are loaded.

## Fields

| Field | Type | Default | Constraints and meaning |
|---|---|---|---|
| `id` | string | required | `[a-z0-9_]{1,40}`, unique in the file. The `control_` prefix is reserved for control classifiers (and they must use it); `test_` is reserved for pre-launch plumbing |
| `mode` | `shadow` \| `live` \| `sim` \| `probe` | `shadow` | `shadow` trades the paper account; `live` trades the live account once it is live and the classifier qualifies (otherwise it runs as shadow); `sim` trades its own simulated account; `probe` asks and logs, never orders |
| `family` | `novel` \| `conventional` | none | Required by `trader validate` for every non-control classifier; control classifiers must have none. A label for the scoreboard, not behaviour: a bad one shows as "unlabelled", and changing it doesn't restart the promotion record |
| `enabled` | bool | `true` | Disabled classifiers are ignored |
| `control` | bool | `false` | Marks the benchmark classifier. Must match the `control_` id prefix. A control can't be a `sim` or a `probe` |
| `symbols` | list of tickers | required | From `config/universe.yaml` |
| `window` | `["HH:MM", "HH:MM"]` | `["09:45", "15:30"]` | US Eastern, within 09:30–16:00, start before end. Entry and exit questions are asked only inside it; stops, targets and time stops run all session |
| `cadence_min` | int | `2` | 1–60. Ask at most this often per symbol |
| `trigger` | list of conditions | `[]` | Each `{feature, op, value}` with `op` one of `>`, `>=`, `<`, `<=`. All must hold before the entry question is asked. A NaN feature never satisfies one |
| `features` | list of names | required | Shown to the decision model (rounded to 4 decimal places) |
| `context` | string | `""` | Sent to the model as `strategy_note` with every question |
| `inputs` | list of `headlines`, `daily_note`, `playbook`, `thesis` | `[]` | Extra inputs sent with every entry and exit question: see [Extra inputs](#extra-inputs-optional). Order and repeats don't matter |
| `entry` | question | required | See below. Criteria keys: `ENTER` plus `WAIT` and/or `STAND_DOWN` |
| `exit` | question | required unless `probe` | Criteria keys: exactly `HOLD` and `EXIT` |
| `size_fraction` | float | `0.2` | Over 0, at most 0.25. Fraction of current equity per position |
| `max_trades` | int | `1` | 1–20. Entries per symbol per day |
| `after_exit` | `rearm` \| `retire` | `retire` | `rearm` arms the symbol again after an exit while it has made fewer than `max_trades` trades today |
| `stop_pct` | float | `0.5` | Over 0, at most 10. Stop distance below the entry, in % |
| `target_pct` | float | `1.0` | Over 0, at most 20. Target distance above the entry, in % |

A **question** (`entry`, `exit`) has:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `instructions` | string | required | The question put to the model |
| `criteria` | map of key to description | required | One crisp, observable description per answer |
| `threshold` | float | `0.6` | 0.5–0.99. The probability the answer needs before the engine acts on it (ENTER, STAND_DOWN or EXIT) |

The model never sees dates or absolute prices, so criteria shouldn't refer to them.

## Extra inputs (optional)

`inputs` adds context to what the model is sent, for questions that need judgement rather than
arithmetic on the features ([ADR 0018](../explanations/adr/)). Each one is opt-in; a classifier
without `inputs` is sent exactly what it was before, and keeps its identity. Turning an input on
or off changes the identity (it changes what the model is asked), so it restarts the promotion
record. The text of the daily note and the playbooks never does: they change every day.

| Input | Sent as | Source | In replays |
|---|---|---|---|
| `headlines` | `headlines`: up to 5 of the symbol's news items from the last 18 hours, newest first, each `{at, minutes_ago, headline, summary}` | Alpaca's news API, refreshed at most every 3 minutes. Only items published at or before the decision time | Yes, from Alpaca's news history, cut at each decision's time |
| `daily_note` | `daily_note`: text | `state/daily_note.md`, written by the pre-market run. Its first line must carry the session's date (`YYYY-MM-DD`), or it's treated as stale. At most 3,000 characters | No: always empty |
| `playbook` | `playbook`: text | This classifier's entry in `state/playbook.yaml` (below), written by the pre-market run | No: always empty |
| `thesis` | `position.thesis` (exit questions only) | Recorded when the position opens: `{opened, entry_question, p_enter, playbook}` (the playbook text if one was in use). Kept with the position, so it survives a restart | Yes |

```yaml
# state/playbook.yaml
date: 2026-10-06            # must be the session's date, or no playbook is used
playbooks:
  my_idea: |                # keyed by classifier id; at most 3,000 characters each
    Scenarios for today, in plain words...
```

- A missing, stale, malformed or empty note or playbook is an empty field (`""`), never an error
  or a pause. The runner sends an info alert and `trader validate` says so.
- A news failure sends `headlines: []` and is counted as `news_errors` in the status; it never
  pauses a decision.
- The decision log records what was sent, not the text: `hl` (headline count), `nh` (the
  note's hash) and `pb` (the playbook's hash). The texts themselves are in the strategist's git
  history.
- The runner reads the note and the playbooks once, at session start (2 minutes before the open).

## Execution toolkit (optional)

Omit any of these for the default behaviour. The engine enforces them every bar, inside the
guardrails, and each position keeps the rules it was opened with. Setting one changes the
classifier's identity, so it restarts its promotion record.

| Field | Type | Constraints | Meaning |
|---|---|---|---|
| `trail_pct` | float | over 0, at most 10 | The stop trails the highest high since entry by this %; it only ever rises |
| `max_hold_min` | int | 1–390 | Time stop: exit after this many minutes |
| `entry_order` | `{type, offset_pct, expire_min}` | `type` `market` (default) or `limit`; `offset_pct` 0–2 (default 0); `expire_min` 1–60 (default 5) | A limit rests `offset_pct`% below the last close and is cancelled if unfilled after `expire_min` minutes. A partial fill cancels the rest |
| `risk_pct` | float | over 0, at most 2 | Size so a stop-out loses about this % of equity, never above `size_fraction` |
| `stop_atr_mult` | float | over 0, at most 20 | Stop distance = this × `atr_14_pct`, capped at `stop_pct` (and at least 0.05%); `stop_pct` until 15 bars exist |
| `scale_out` | `{at_pct, fraction, stop_to_breakeven}` | `at_pct` over 0, at most 20, and below `target_pct`; `fraction` 0.1–0.9; `stop_to_breakeven` default `false` | Sell `fraction` of the position when price first reaches `at_pct`% above entry; optionally raise the stop to the entry price. Skipped if either part would be under $1 |

Per bar the order is: stop first, then target, then scale-out, then the trail ratchets from that
bar's high.

## Promotion identity

A classifier's promotion record is tied to a hash of its spec: every field except `mode`,
`enabled` and `family`, with unset toolkit fields left out, plus a digest of all custom feature
code if it uses any custom feature. Changing anything else restarts its record. See
[Evidence and promotion](../explanations/evidence.md#promotion-to-mode-live).
