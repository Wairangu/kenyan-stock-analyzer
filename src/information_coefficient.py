"""Cross-sectional information coefficient: does a higher score actually
predict a higher forward return?

`track_record.py` collapses each period into one portfolio return. That throws
away the breadth of the universe, and for a paired test against a benchmark the
time to statistical significance then depends only on the information ratio --
the holding horizon cancels out, leaving decades at any realistic edge.

This module asks the cheaper question instead. On each decision date it ranks
every eligible name by score, ranks the same names by their realised forward
return, and correlates the two. One observation uses the whole cross-section
rather than five picks, so the same evidence accumulates far faster. Measuring
IC at several horizons also shows where the signal lives: flat at 5 sessions and
rising at 60 means a slow factor, and the reverse means the trading horizon is
wrong.

Nothing here is a return. An IC is a rank correlation in [-1, 1], and a positive
one says the ordering carried information, not that the strategy made money.
"""

import math

from data_quality import finite_number, parse_date, is_session, next_session
from logger import get_logger

logger = get_logger(__name__)

# A rank correlation over a handful of names is noise. Its standard error is
# roughly 1/sqrt(N-1), so small cross-sections are excluded outright rather
# than averaged in as if they carried the same weight.
MIN_NAMES = 10
# Dropping names that lost their exit quote biases the result upward, because
# suspensions and delistings are not random. Beyond this fraction the date is
# discarded instead.
MAX_DROPPED_FRACTION = 0.2
DEFAULT_HORIZONS = (5, 10, 20, 60)
# Two-sided 5%, 80% power.
_POWER_Z = (1.96 + 0.84) ** 2


def _midranks(values):
    """Ranks with ties averaged, so equal scores cannot create a fake ordering."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        rank = (i + j) / 2 + 1
        for k in order[i:j + 1]:
            ranks[k] = rank
        i = j + 1
    return ranks


def spearman(xs, ys):
    """Rank correlation. None when either side has no variation to correlate."""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rx, ry = _midranks(xs), _midranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def _quote(record, expected_date, verified_only=True):
    """A usable price: verified, from the session it claims, finite, positive.

    `verified_only=False` accepts a record that carries no verification
    metadata, for history mined from archived reports that predate those
    fields. It never accepts a record that claims a different date, and it is
    opt-in at the call site so a weaker guarantee can never apply by accident.
    """
    if not isinstance(record, dict):
        return None
    if record.get("price_date") not in (expected_date, None):
        return None
    if verified_only:
        if record.get("price_verified") is not True or record.get("price_date") != expected_date:
            return None
    price = finite_number(record.get("price"))
    return price if price is not None and price > 0 else None


def _summary(values, horizon_days, independent):
    n = len(values)
    if not n:
        return {"n": 0, "mean_ic": None, "sd": None, "t_stat": None,
                "positive_rate": None, "years_to_significance": None}
    mean = sum(values) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1)) if n > 1 else None
    t_stat = round(mean / (sd / math.sqrt(n)), 2) if sd and sd > 0 else None
    # Indicative only, and only once enough observations exist for the estimate
    # to mean anything. Early samples make this number swing wildly.
    years = None
    if independent and n >= 8 and sd and sd > 0 and mean > 0:
        years = round(_POWER_Z * (sd / mean) ** 2 * horizon_days / 252, 1)
    return {"n": n,
            "mean_ic": round(mean, 4),
            "sd": round(sd, 4) if sd else None,
            "t_stat": t_stat if independent else None,
            "positive_rate": round(100 * sum(1 for v in values if v > 0) / n, 1),
            "years_to_significance": years}


def compute_ic(snapshots_by_date, horizon_days=10, *, min_names=MIN_NAMES,
               max_dropped_fraction=MAX_DROPPED_FRACTION, verified_only=True,
               require_investable=True):
    """Rank correlation between score and forward return, per decision date.

    Entry is the next session's close and exit is `horizon_days` NSE sessions
    later, matching `track_record.compute_track_record` so the two are directly
    comparable. The universe is every investable, scored name -- not the five
    that were bought -- because breadth is the entire point.

    Returns both an overlapping series, sampled every date for the trend, and a
    non-overlapping subset. Only the non-overlapping observations are
    independent, so the t-statistic is reported for those alone: consecutive
    overlapping windows share most of their return path and would otherwise
    inflate significance several-fold.
    """
    if not isinstance(horizon_days, int) or horizon_days < 1:
        raise ValueError("horizon_days must be a positive integer")
    dates = sorted(d for d in snapshots_by_date
                   if parse_date(d) and is_session(parse_date(d)))
    observations = []
    skipped = {"too_few_names": 0, "too_many_dropped": 0, "no_variation": 0}
    for signal_date in dates:
        records = snapshots_by_date[signal_date] or {}
        entry_date = next_session(parse_date(signal_date)).isoformat()
        exit_date = next_session(parse_date(entry_date), horizon_days).isoformat()
        if not dates or exit_date > dates[-1]:
            continue  # still outstanding, not a failure
        entries = snapshots_by_date.get(entry_date, {}) or {}
        exits = snapshots_by_date.get(exit_date, {}) or {}

        scored = {s: finite_number(r.get("score"))
                  for s, r in records.items()
                  if isinstance(r, dict)
                  and (r.get("investable") if require_investable else True)
                  and finite_number(r.get("score")) is not None}
        if len(scored) < min_names:
            skipped["too_few_names"] += 1
            continue

        scores, returns, dropped = [], [], 0
        for symbol, score in sorted(scored.items()):
            entry = _quote(entries.get(symbol), entry_date, verified_only)
            exit_price = _quote(exits.get(symbol), exit_date, verified_only)
            if entry is None or exit_price is None:
                dropped += 1
                continue
            scores.append(score)
            returns.append((exit_price / entry - 1) * 100)
        if dropped > max_dropped_fraction * len(scored):
            skipped["too_many_dropped"] += 1
            continue
        if len(scores) < min_names:
            skipped["too_few_names"] += 1
            continue

        ic = spearman(scores, returns)
        if ic is None:
            skipped["no_variation"] += 1
            continue
        observations.append({"signal_date": signal_date, "entry_date": entry_date,
                             "exit_date": exit_date, "n_names": len(scores),
                             "dropped": dropped, "ic": round(ic, 4)})

    independent, reserved = [], None
    for obs in observations:
        if reserved and obs["signal_date"] < reserved:
            continue
        independent.append(obs)
        reserved = obs["exit_date"]

    note = (
        f"Spearman rank correlation of score against the next {horizon_days} NSE "
        "sessions' price change, entered at the following session's close. "
        "An IC is not a return and not a profit: it says the ordering carried "
        "information. Price changes only, before costs, excluding dividends and "
        "corporate actions. Overlapping observations share return paths and are "
        "shown for the trend; inference uses the non-overlapping subset only. "
        f"Dates with fewer than {min_names} usable names, or with more than "
        f"{max_dropped_fraction:.0%} of names missing a verified quote, are "
        "excluded rather than filled in, because suspensions and delistings are "
        "not random. Sample counts alone do not establish confidence."
    )
    if not verified_only:
        note += (" PROVENANCE: prices were accepted without the independent "
                 "cross-check the live screen requires, so this is a diagnostic "
                 "on mined history, not a measurement of the current strategy.")
    return {"horizon_days": horizon_days,
            "as_of": dates[-1] if dates else None,
            "observations": observations,
            "overlapping": _summary([o["ic"] for o in observations], horizon_days, False),
            "independent": _summary([o["ic"] for o in independent], horizon_days, True),
            "skipped": skipped,
            "note": note}


def compute_ic_decay(snapshots_by_date, horizons=DEFAULT_HORIZONS, **kwargs):
    """IC at several horizons. The shape across them says where the signal
    lives, which no single horizon can show on its own."""
    return {h: compute_ic(snapshots_by_date, horizon_days=h, **kwargs)
            for h in horizons}


# Bullish/bearish is an ordering with two levels. Midranks handle the ties, so
# the correlation is a legitimate rank statistic, just a very coarse one.
ARCHIVE_SCORES = {"bullish": 1.0, "bearish": 0.0}


def snapshots_from_archive(archive):
    """Turn `report_archive.fetch_market_summary_snapshots` output into IC input.

    The archived market summaries carry a price and the locally computed
    bullish/bearish `overall` call, but no factor score -- that table predates
    scoring. So this measures whether THAT signal ordered forward returns, on
    real NSE prices. It says nothing about the current screen's score, and a
    score cannot be reconstructed for those dates: it would need point-in-time
    fundamentals, and rebuilding them from today's feed is look-ahead.

    The records deliberately omit `price_verified`, so callers must pass
    `verified_only=False` and accept the weaker guarantee explicitly.
    """
    snapshots = {}
    for day, symbols in (archive or {}).items():
        rows = {}
        for symbol, record in (symbols or {}).items():
            score = ARCHIVE_SCORES.get(str(record.get("overall", "")).lower())
            price = finite_number(record.get("price"))
            if score is None or price is None or price <= 0:
                continue
            rows[symbol] = {"score": score, "price": price, "price_date": day,
                            "investable": True, "price_provenance": "archive"}
        if rows:
            snapshots[day] = rows
    return snapshots
