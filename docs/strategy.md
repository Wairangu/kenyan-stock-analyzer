# Screening and evaluation methodology

The current strategy is `screen-v2`. It is a transparent baseline awaiting
prospective evaluation, not a proven NSE trading system. No weights were tuned
against the one-day history available locally.

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

Weights remain value 20%, quality 20%, growth 15%, momentum 20%, dividend 15%,
liquidity 10%. Missing factors are omitted and their weights renormalized; the
coverage metric and essential-factor requirements prevent sparse scores from
qualifying as complete analyses. Peer-relative valuation needs at least three
valid observations for each metric; otherwise absolute baseline anchors apply.

Financial firms use ROE/ROA rather than industrial debt/current-ratio formulas.
This remains incomplete bank underwriting: the feed does not consistently supply
loan quality, provisioning or regulatory capital. The quality explanation flags
this limitation; review issuer disclosures before investing. No absent bank
metrics are fabricated. ROE/ROA thresholds and the other factor mappings remain
heuristics to evaluate, not established return predictors.

RSI uses Wilder's initial arithmetic mean and subsequent smoothing. Flat prices
are neutral, a series with gains but no losses has RSI 100, and indicators cannot
emit crossovers before enough history is available. Momentum uses a continuous
RSI contribution. A standard 12-month return excluding the latest month is a
future comparison candidate; it requires longer, consistently adjusted history.

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
python evaluate_strategy.py --history data/history --horizons 5 10 20
python evaluate_strategy.py --history data/history --slippage 0.003
```

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
