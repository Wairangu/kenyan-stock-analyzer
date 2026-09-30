# Screening and evaluation methodology

The current strategy is `screen-v3`. It is a transparent baseline awaiting
prospective evaluation, not a proven NSE trading system. No weights were tuned
against the one-day history available locally.

`screen-v3` changes three things from `screen-v2`. Metrics are scored by
percentile rank against the same day's universe instead of hand-picked linear
mappings. Momentum is the 12-month return excluding the latest month, and no
longer counts RSI, 3-month return, or the same moving average three times.
Entry and exit are separate decisions with a dead band between them, and the
round-trip cost every position must clear is published beside it.

## Candidate rules

Candidates must have a finite positive price, verified security identity,
enough history for the configured indicators (at least 50 bars by default),
and the latest completed NSE session's quote. Both the historical feed and
independent reference must have matching session dates and agree within the
configured tolerance. Missing validation, unavailable reference dates, stale
bars and OCR-only security identity exclude a candidate. Holidays and weekends
are accounted for; a session is treated as complete after 15:30 Nairobi time.

Liquidity requires median daily value traded of at least KES 1 million across
20 consecutive NSE sessions. Missing bars do not count as evidence of liquidity.
Zero-volume sessions are included. A single day's volume spike cannot satisfy
this requirement.

The baseline requires score >= 60, coverage >= 80%, available value/quality/growth
factors and a positive TradingView technical rating. It ranks by score, then
coverage, then symbol for reproducible ties. These cutoffs are conservative
policy choices, not statistically fitted thresholds. Function arguments allow
offline comparisons without silently changing the live policy.

A name already held is screened at a lower hold threshold of 45 and is not
dropped merely because the technical gauge turned neutral. Every data-integrity
rule still applies to it: without a trustworthy current price there is no
decision to make in either direction. This dead band exists because a round
trip costs about 3.25% at the default 1.5% commission and 0.1% slippage per
side, so rotating on a rank change that is mostly noise is a guaranteed loss.
The analyzer never sees anyone's holdings, so it publishes a retention list of
every name evaluated as though it were held; the portfolio app intersects that
with its user's open positions and shows a holdings review. Only the entry-grade
list reaches the allocator, so hysteresis never directs new money into a name
that clears the hold bar alone. A flagged holding is a prompt to review, never
an instruction to sell.

Weights remain value 20%, quality 20%, growth 15%, momentum 20%, dividend 15%,
liquidity 10%. Missing factors are omitted and their weights renormalized; the
coverage metric and essential-factor requirements prevent sparse scores from
qualifying as complete analyses.

Every metric is converted to its percentile rank against the same day's
universe, using midranks so ties share a score. Ranks replace the fixed linear
mappings of `screen-v2`, which carried invented constants and clamped away the
differences that matter: an absolute price-to-book anchor scored every NSE bank
trading under 1x book at 100, which is no discrimination at all in the part of
the distribution the universe occupies. Ranks are also self-calibrating when the
whole market rerates, and percentiles rather than z-scores because these metrics
are skewed enough that one outlier would compress everyone else. Price-to-earnings
and price-to-book are ranked as a ratio to the stock's own sector median, which
needs at least three valid observations, otherwise the universe median. A metric
with fewer than eight valid observations across the universe cannot be ranked
meaningfully and falls back to the `screen-v2` absolute anchor; a single-stock
run is therefore entirely anchor-based and is not comparable to a ranked score.
Each score reports `ranked` and a per-factor `metric_counts`, because coverage
can read 100% while every factor rests on one metric. That count is diagnostic
and is not yet a screening rule.

Financial firms use ROE/ROA rather than industrial debt/current-ratio formulas.
This remains incomplete bank underwriting: the feed does not consistently supply
loan quality, provisioning or regulatory capital. The quality explanation flags
this limitation; review issuer disclosures before investing. No absent bank
metrics are fabricated. ROE/ROA thresholds and the other factor mappings remain
heuristics to evaluate, not established return predictors.

RSI uses Wilder's initial arithmetic mean and subsequent smoothing. Flat prices
are neutral, a series with gains but no losses has RSI 100, and indicators cannot
emit crossovers before enough history is available.

The momentum factor is the 12-month return excluding the most recent month,
measured over 252 bars ending 21 bars ago, and weighted twice the trend state
that confirms it. A partial window returns nothing rather than silently becoming
a shorter-horizon factor, so the default data period is two years. RSI no longer
contributes: it is a mean-reversion oscillator, and scoring it linearly rewarded
exactly the overbought names the alerts warn about, so the screen contradicted
its own dashboard. Three-month return no longer contributes either, because
short-horizon returns reverse. Both remain visible, RSI as an alert and `perf_3m`
as a reported statistic. The summary technical signal gives the moving-average
crossover and the trend one vote between them, since both read price against a
moving average and counting them separately let a single input outvote MACD two
to one in every case.

## Prices and dividends

Independent quotes are attached as reference fields, never substituted into
already-computed technical results. The legacy `ENABLE_OFFICIAL_CLOSE` option
now controls attaching these references. Historical prices retain their source.
Yahoo uses explicitly unadjusted OHLC and verifies exchange/currency; it never
tries a bare ticker. TradingView's adjustment convention is provider-controlled
and is not currently reconciled across corporate actions.

A dividend calendar is not a full-year dividend ledger. Its latest declaration,
book closure and payment date are stored separately from provider annual DPS,
annual yield and ex-date. Calendar book closure is never treated as the ex-date.
Annual metrics remain labelled as unverified provider figures. Positive yield
with unknown payout receives no dividend factor; nonpositive or >100% payout
receives zero. A known zero yield is distinct from missing yield.

## Budgeting

The same pure allocator is used by the portfolio app and reference snapshots.
It targets equal allocations in up to five ranked names, accounts for the
configured transaction fee (default 1.5% each side), and rounds to whole shares.
Purchases respect 20% stock and 40% sector limits against existing holdings plus
the new contribution. These caps are policy defaults, not an optimized risk model.
Unallocated money stays in cash. Unpriced holdings block a new allocation.
Fees are rounded up to cents so suggested costs cannot exceed the budget.

Published suggestions expire at the next trading session's 15:30 Nairobi time.
The app requires current holding valuations before suggesting additional buys.
Quoted prices are indicative; spread, market impact, broker-specific charges and
execution availability must be checked before placing an order.

## Prospective evaluation

Daily version-2 JSON snapshots freeze the selected shares for a KES 100,000
reference account with no pre-existing holdings, along with the strategy
version, ranks, fees and dated price validation. The first snapshot of the day
is immutable. S3 version-2 keys and legacy signal keys are separate. Local runs
save the same schema under `data/history/`, which routine cache cleanup preserves.

Read local snapshots without fetching data, modifying files or sending email:

```bash
python evaluate_strategy.py --history data/history --horizons 20 60 120
python evaluate_strategy.py --history data/history --slippage 0.003
python evaluate_strategy.py --history data/history --ic-horizons 5 10 20 60
python evaluate_strategy.py --history data/history --observations
```

The default horizon is 60 NSE sessions. At 1.5% commission plus 0.1% slippage
per side, a round trip costs about 3.25%, so a 5- or 10-session hold asks the
signal for roughly 100% a year in friction alone. The screen produces no expected
return to compare against that hurdle, so the hurdle is published instead: beside
every candidate, on the track-record panel, and in the evaluation note.

The reference simulation enters at the next session's close, with assumed
slippage (default 0.1% per side), then liquidates after the stated number of NSE
sessions. If next-day prices make the frozen order unaffordable, shares are
reduced in recorded rank order. Periods do not overlap. Each model period starts
with the same reference budget; this is not a compounded personal-account return.
The comparison is an equal-weight investable universe on the same dates, with
the same assumed costs, not a published NSE index. Portfolio cash allocation
means the portfolio and comparison may have different market exposure.

An absent execution/exit quote invalidates the entire selected period. Counts
of these incomplete periods are displayed: suspensions/delistings must not be
silently treated as zero losses or selectively removed constituents. Missing
snapshots do not lengthen a requested holding horizon. Old tier labels are
displayed only as separate, overlapping signal diagnostics.

## Cross-sectional information coefficient

Portfolio returns collapse each period into one number. For a paired test
against a benchmark the holding horizon then cancels out of the power
calculation entirely, leaving time-to-significance a function of the
information ratio alone: roughly 31 years at an IR of 0.5, which would be a
good professional record. No choice of horizon improves that, so the portfolio
tables cannot settle whether the screen works.

`information_coefficient.py` asks the cheaper question. On each decision date it
ranks every investable, scored name by score, ranks the same names by their
realised forward price change, and takes the Spearman correlation. One
observation uses the whole cross-section instead of five picks, which is where
the statistical efficiency comes from. At an IC of 0.05 over roughly 25 eligible
names, five-session observations reach significance in about 2.6 years against
31 years for the equivalent portfolio test. Treat that as a floor: the eligible
universe is bank-heavy and those names move together, so effective breadth is
lower than the name count and the honest figure is four to six years. Nothing
makes a one-month or six-month sample informative about returns.

Entry and exit follow `track_record.py` exactly -- next session's close, then
the stated number of NSE sessions -- so the two views are comparable. Ties are
averaged into midranks, so equal scores cannot invent an ordering, and a date
whose scores are all equal is recorded as having no variation rather than
correlated. Dates with fewer than ten usable names are excluded, because the
standard error of a rank correlation is about 1/sqrt(N-1) and a thin
cross-section is noise. A name missing a verified, correctly dated quote is
dropped and counted; beyond 20% dropped the whole date is discarded, since
suspensions and delistings are not random and quietly removing them biases the
result upward.

Overlapping observations, sampled every session, are reported for the trend.
Only the non-overlapping subset is independent, so the t-statistic is computed
over that subset alone; consecutive overlapping windows share most of their
return path and would inflate significance several-fold. The projected time to
significance is an in-sample extrapolation from the dispersion observed so far,
shown only once eight independent observations exist, and it will swing widely
early. It is indicative, not a promise.

Measuring several horizons at once is the point. The shape of the IC across 5,
10, 20 and 60 sessions says where the signal lives: flat at 5 and rising at 60
means a slow factor, and the reverse means the trading horizon is wrong. This
separates the measurement horizon from the trading horizon, which do not have to
agree -- trade long to control the round trip, measure short to get feedback.

### Mined history

The reports bucket holds archived `market_summary_*.html` back to 2026-07-30.
Those tables carry a price and the locally computed bullish/bearish `overall`
call, but no factor score, because they predate scoring. So they can measure
whether that older signal ordered forward returns on real NSE prices, and
nothing more. The current screen's score cannot be recovered for those dates:
it would need point-in-time fundamentals, and rebuilding them from today's feed
is look-ahead.

Archived prices never passed the independent cross-check the live screen
requires, so `compute_ic` rejects them under its default. Measuring them is
opt-in through `verified_only=False`, and the returned note then carries an
explicit provenance warning. `require_investable=False` likewise waives the
liquidity flag those records predate. Neither default may be relaxed silently.

```bash
python evaluate_strategy.py --history data/history --archive-bucket <reports-bucket>
```

Run over 38 archived sessions (2026-07-30 to 2026-09-21), the mined signal gave
mean ICs of +0.04 at 5 sessions (n=5, t=0.35) and -0.11 at 10 (n=3, t=-0.79):
indistinguishable from zero in both directions, which is the expected result at
that sample size and is reported rather than smoothed over.

An IC is a rank correlation in [-1, 1]. It is not a return, not a profit, and
not evidence of tradable edge after costs. Decide which single number is the
verdict before reading the table: testing several horizons and several summary
statistics offers many chances to find significance by accident.

**Current limit:** reported returns are price changes after assumed costs.
Dividends, splits, rights issues, taxes, market impact and actual fills are not
available in the snapshot data. Therefore these are explicitly not total-return
performance or evidence that the strategy beats the market. There is no sample
count that automatically establishes statistical confidence. Reliable adjusted
history, issuer publication dates and security lifecycle data are prerequisites
for a full historical backtest, drawdown/Sharpe comparison and walk-forward
selection of algorithms. Changing weights requires a new strategy version and
evaluation on later data; never reconstruct past fundamentals from today's feed.

## Verification

```bash
MPLCONFIGDIR=/tmp/nse-test-mpl python -B -m unittest test tests.test_regressions
```

Tests use synthetic prices and mocked feeds/AWS. They exercise actual financial
edge cases, eligibility failures, fees/caps, dividend period separation,
immutability and next-session portfolio evaluation. They do not demonstrate
profitable trading.
