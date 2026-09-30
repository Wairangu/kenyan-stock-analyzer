# Upstream review: voleche09/kenyan-stock-analyzer

Reviewed on 2026-09-30 against branch `fix/data-integrity-and-model-portfolio`.

`origin/main` (up to `ed94ee7`) has 18 non-merge commits that this branch
doesn't have, adding about 7,000 lines across 32 files. A test merge conflicts
in 9 files: `main.py`, `send_summary.py`, `src/email_notifier.py`,
`src/report_generator.py` (+2,700 lines upstream), `README.md`, `.gitignore`,
`templates/base.html`, `templates/market_summary.html` and
`templates/stock_report.html`. Don't run a full merge. Take the pieces you
want one at a time.

## Worth pulling (small, easy to take)

| Change | Why |
|---|---|
| `src/fundamental_analysis.py`, part of `d7eb4e7` | **Real bug in our code.** Line 367 reads `d.get('change')`, but TradingView exposes `change` as an attribute, so the value is always `None`. The fix is `getattr(stock, 'change', None)`. It also adds `perf_all` (`d.get('Perf.All')`). Nothing reads this key today, so the effect is small. |
| `src/utils.py` → `market_closed_today()` | Uses the Nairobi timezone plus Kenyan public holidays (via the `holidays` package). Useful if the Lambda or email should skip holidays. |
| `0f490a8` favicon | Adds one line to `templates/base.html`. |
| `05710cb` click-to-sort Overview table | A self-contained UI improvement, but it touches `report_generator.py`. |
| `977f445` news limited to 7 days, filtered before capping | Sound logic, but it lives in upstream's `src/portfolio.py`, which we don't have. |

## Probably skip or adapt, because the designs differ

- **Personal portfolio, bonds and international trackers** (`76858c1`,
  `74a7ea7`, `f0187b6`, `b33fb2e`, `1fb1ce0`, `44cd4b9`): these read holdings
  from gitignored JSON files on local disk. That doesn't suit our setup, where
  `portfolio/` is a multi-user Lambda that stores trades in S3. Their
  `portfolio/` data folder would also sit inside our `portfolio` Python
  package.
- **Docker, nginx and `restart: always`** (`e5110b7`, `c1b0aca`): for
  self-hosting on a home server. We deploy to AWS Lambda.
- **Government bonds tab** (`96d4db9`): we already have our own bond features
  (CBK auctions, coupons, verdicts). Compare the two before porting anything.
- **Dark mode, treemap heatmap and Visuals tab** (`d7eb4e7`, `3299315`,
  `6e95f2a`, `658d285`), plus the TradingView performance panel (`f16a360`):
  good features, but they're heavy edits to `report_generator.py`, which
  conflicts with ours. Pull these only if we want the dashboard UI.

## Not needed

- `d3159c6` fixes a nested `<style>` tag in `stock_report.html` and
  `market_summary.html`. Neither of our versions has that bug, since
  `base.html` is the only template with a `<style>` tag.

## Recommendation

After committing the current work, cherry-pick the `change_pct` fix and
`market_closed_today()` by hand, and add the favicon if wanted. Leave the rest
unless we want upstream's dashboard UI.
