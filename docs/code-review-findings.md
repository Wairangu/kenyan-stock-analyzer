# Code review findings and fixes

These findings were identified in the repository working tree reviewed on
2026-09-22. Fixes have since been applied.

## 1. Unverified stocks could change other stocks’ scores (P2, fixed)

`score_universe` builds cross-sectional percentile ranks from every nonempty
analysis result in [`src/scoring.py`](../src/scoring.py#L425). It does not first
exclude results with an unverified security identity, stale prices, or failed
price validation. Candidate selection applies those checks later in
[`src/recommender.py`](../src/recommender.py#L75), after the ranks have already
been computed.

Because each stock’s score depends on the other values in the ranking
population, an ineligible or stale stock can shift eligible stocks’ scores and
change whether they pass the entry threshold. Production scoring now builds its
ranking population from stocks with a verified identity, complete current
history, a successful fresh price check, and sufficient liquidity. The analyzer
passes those validations into `score_universe`.

## 2. Trade entry accepted non-finite numbers (P2, fixed)

The trade form converts quantity and price to floats and checks only whether
they are less than or equal to zero in
[`portfolio/lambda_handler.py`](../portfolio/lambda_handler.py#L873). Those
comparisons do not reject `NaN` or infinity. A `NaN` quantity can make the
resulting position fail the positive-quantity check in the FIFO calculation;
infinite values can propagate into cost basis and displayed totals.

The form now rejects non-finite quantity and price values before saving a
trade. The FIFO calculation also skips malformed or non-finite stored rows so
older bad data cannot corrupt displayed positions.

## 3. Concurrent S3 read-modify-write operations could lose updates (P2, fixed)

Trade updates load a user’s full trade list, append or remove an entry, then
overwrite the S3 object in
[`portfolio/lambda_handler.py`](../portfolio/lambda_handler.py#L887). Account
registration follows the same pattern for the shared users object in
[`portfolio/lambda_handler.py`](../portfolio/lambda_handler.py#L201). If two
requests read the same old object and then save in succession, the last write
can erase the other request’s change.

Account and trade updates now use S3 conditional writes against the object ETag
and retry after conflicts, including safe conditional creation for a missing
object. Account uniqueness checks are repeated against the latest object after
a conflict.

The changes received a syntax and whitespace check. The test suite was not run.
