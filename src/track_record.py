"""
Track record: did the system's past calls actually work?

Pure function over a {date: {symbol: {...}}} snapshot dict -- no
AWS/network calls, independently testable. Works for any tiered signal
the snapshots carry: the literal TradingView tv_class (Strong Buy/Buy/...,
see signal_history.py) or the system's own legacy bullish/bearish
technical call (see report_archive.py, which mines it from ~7 weeks of
already-archived daily reports). Deliberately conservative: reports an
explicit "not enough data yet" note below a sample-size floor rather than
presenting a thin sample as a confident number, matching the rest of the
codebase's transparent-screen ethos (src/scoring.py's per-factor
`reasons`, the email's partial-coverage flag).

Every tier's average return is only meaningful next to what the same
stocks did on average over the same period -- a "Strong Buy" tier that's
merely tracking a rising market isn't evidence of skill. compute_track_record
always includes that same-period, all-symbols benchmark alongside the tiers.
"""

from datetime import datetime, timedelta

from logger import get_logger

logger = get_logger(__name__)

DEFAULT_TIER_FIELD = "tv_class"
DEFAULT_TIERS = ("strong_buy", "buy")
MIN_SAMPLES_FOR_CONFIDENCE = 20


def _trading_days_after(dates_sorted, start_date, horizon_days):
    """
    Return the first date in `dates_sorted` that is >= horizon_days
    *snapshot* days after start_date (snapshots only exist for trading
    days already, so this counts snapshots, not calendar days). Returns
    None if there aren't enough later snapshots yet.
    """
    try:
        idx = dates_sorted.index(start_date)
    except ValueError:
        return None
    target_idx = idx + horizon_days
    if target_idx >= len(dates_sorted):
        return None
    return dates_sorted[target_idx]


def compute_track_record(snapshots_by_date, tier_field=DEFAULT_TIER_FIELD,
                          tiers=DEFAULT_TIERS, horizon_days=10):
    """
    Args:
        snapshots_by_date: {date: {symbol: {price, <tier_field>, ...}}}.
        tier_field: which field in each symbol's record holds its tier,
            e.g. "tv_class" (strong_buy/buy/neutral/sell/strong_sell) or
            "overall" (bullish/bearish).
        tiers: which values of tier_field to report on.
        horizon_days: number of later trading-day snapshots ahead to
            measure the forward return at.

    Returns:
        {horizon_days, as_of, tiers: {<tier>: {n, hit_rate, avg_return_pct}, ...},
         benchmark: {n, avg_return_pct} (every symbol, regardless of tier,
         over the same date/horizon pairs -- the "just held the market"
         comparison), note}
    """
    dates_sorted = sorted(snapshots_by_date.keys())
    as_of = dates_sorted[-1] if dates_sorted else datetime.now().strftime("%Y-%m-%d")

    returns_by_tier = {tier: [] for tier in tiers}
    benchmark_returns = []

    for date in dates_sorted:
        later_date = _trading_days_after(dates_sorted, date, horizon_days)
        if later_date is None:
            continue
        today_symbols = snapshots_by_date[date]
        later_symbols = snapshots_by_date[later_date]
        for symbol, rec in today_symbols.items():
            entry_price = rec.get("price")
            exit_price = (later_symbols.get(symbol) or {}).get("price")
            if not entry_price or not exit_price:
                continue
            ret = (exit_price / entry_price - 1) * 100
            benchmark_returns.append(ret)
            tier = rec.get(tier_field)
            if tier in returns_by_tier:
                returns_by_tier[tier].append(ret)

    def _summarize(rets):
        n = len(rets)
        if n == 0:
            return {"n": 0, "hit_rate": None, "avg_return_pct": None}
        hits = sum(1 for r in rets if r > 0)
        return {
            "n": n,
            "hit_rate": round(100 * hits / n, 1),
            "avg_return_pct": round(sum(rets) / n, 2),
        }

    tiers_out = {tier: _summarize(rets) for tier, rets in returns_by_tier.items()}
    total_n = sum(t["n"] for t in tiers_out.values())
    benchmark_out = {
        "n": len(benchmark_returns),
        "avg_return_pct": round(sum(benchmark_returns) / len(benchmark_returns), 2)
        if benchmark_returns else None,
    }

    if not dates_sorted:
        note = "No signal history yet — this starts counting from today."
    elif total_n < MIN_SAMPLES_FOR_CONFIDENCE:
        note = (
            f"Only {total_n} scored call(s) so far (need ~{MIN_SAMPLES_FOR_CONFIDENCE}+ "
            "for a meaningful read) — treat hit rate/avg return as provisional."
        )
    else:
        note = f"Based on {total_n} scored calls."

    return {
        "horizon_days": horizon_days,
        "as_of": as_of,
        "tiers": tiers_out,
        "benchmark": benchmark_out,
        "note": note,
    }


# ---- Test ----
if __name__ == "__main__":
    from logger import setup_logging
    setup_logging()

    base = datetime(2026, 7, 31)
    fake_snapshots = {}
    price = 100.0
    for i in range(15):
        date = (base + timedelta(days=i)).strftime("%Y-%m-%d")
        price *= 1.01  # steadily rising, so strong_buy calls should show a positive track record
        fake_snapshots[date] = {
            "AAA": {"price": round(price, 2), "tv_class": "strong_buy"},
            "BBB": {"price": round(price * 0.5, 2), "tv_class": "neutral"},
        }

    result = compute_track_record(fake_snapshots, horizon_days=10)
    print(result)
    assert result["tiers"]["strong_buy"]["n"] >= 1
    assert result["tiers"]["strong_buy"]["avg_return_pct"] > 0
    assert result["benchmark"]["n"] >= 1
    print("OK: default tv_class/strong_buy-buy")

    # Legacy signal: different field name, different tiers, and here the
    # "bearish" tier is deliberately *worse* than "bullish" -- the
    # benchmark/tier split should make that visible rather than hiding it.
    fake_legacy = {}
    aaa_price, bbb_price = 100.0, 100.0
    for i in range(15):
        date = (base + timedelta(days=i)).strftime("%Y-%m-%d")
        aaa_price *= 1.01  # rises
        bbb_price *= 0.995  # falls
        fake_legacy[date] = {
            "AAA": {"price": round(aaa_price, 2), "overall": "bullish"},
            "BBB": {"price": round(bbb_price, 2), "overall": "bearish"},
        }
    legacy_result = compute_track_record(
        fake_legacy, tier_field="overall", tiers=("bullish", "bearish"), horizon_days=10,
    )
    print(legacy_result)
    assert legacy_result["tiers"]["bullish"]["avg_return_pct"] > 0
    assert legacy_result["tiers"]["bearish"]["avg_return_pct"] < 0
    assert legacy_result["tiers"]["bullish"]["avg_return_pct"] > legacy_result["tiers"]["bearish"]["avg_return_pct"]
    print("OK: generalized tier_field/tiers + benchmark")
