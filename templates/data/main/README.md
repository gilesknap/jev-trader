# Trading data: deployment config

Deployment config for one owner of [jev-trader](https://github.com/gilesknap/jev-trader) (code is
in that public repo; this repo is private). This `main` branch is human-owned:

- `config.yaml`: owner, dashboard users, experiment start date, schedule, models.
- `config/mode.yaml`: the human's paper/live override (`auto | paper | live`).
- `deploy/systemd*/`: timers and `trader.env` rendered from `config.yaml` by
  `trader config render-deploy --data-root <this checkout>`. Re-render and commit them after every
  edit to `config.yaml`.

Changes take effect only when a human runs `trading-deploy`. The strategist's data (state,
journal, logs, custom features) is on the `strategist` branch, which is never merged into this one.
