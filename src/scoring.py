"""
Transparent factor-scoring & screening module (strategy `screen-v3`).

Combines the metrics the pipeline already gathers (valuation, quality, growth,
momentum, dividend, liquidity) into a transparent 0-100 score PER FACTOR and an
overall blend. Every input and every point is exposed in `reasons`, so the score
is a screen you can inspect and tune -- never a black box.

Scores are CROSS-SECTIONAL. Each metric is converted to its percentile rank
against the same day's universe rather than through a hand-picked linear
mapping. That removes the invented constants, restores discrimination where the
universe actually clusters (most NSE banks trade under 1x book, which an
absolute P/B anchor pins at 100 for all of them), and self-calibrates when the
whole market rerates. Absolute anchors survive only as the fallback for a
universe too small to rank -- a single-stock run, or a metric almost nobody
reports.

This is a mechanical screen of public metrics, NOT investment advice.

Also produces per-stock alerts (oversold, near 52-week low, strong signal,
high sustainable yield, illiquid, price-source mismatch) for the dashboard.
"""

import math
import statistics
from collections import namedtuple

from logger import get_logger
from data_quality import finite_number, latest_completed_session, parse_date

logger = get_logger(__name__)

# Baseline policy weights, not fitted return forecasts or probabilities.
DEFAULT_WEIGHTS = {
    "value": 0.20,
    "quality": 0.20,
    "growth": 0.15,
    "momentum": 0.20,
    "dividend": 0.15,
    "liquidity": 0.10,
}

# Ranking a handful of names says nothing about where a stock sits in the
# market, so below this many valid observations the absolute anchor is used.
MIN_CROSS_SECTION = 8
# A sector median needs this many observations before it beats the universe.
MIN_PEER_COUNT = 3


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


# ---------------------------------------------------------------------------
# Metric registry
# ---------------------------------------------------------------------------
# `anchor` is the pre-v3 fixed mapping, kept only as the small-universe
# fallback. `sector_relative` metrics are ranked as a ratio to their sector
# median (or the universe median) so a bank and a manufacturer are not judged
# against the same raw P/E.

Metric = namedtuple(
    "Metric", "key factor lower_is_better sector_relative valid anchor label")


def _positive(value, row):
    return value > 0


def _nonnegative(value, row):
    return value >= 0


def _any_value(value, row):
    return True


def _dividend_payer(value, row):
    payout = row.get("dividend_payout_ratio")
    return value > 0 and payout is not None and 0 < payout <= 100


METRICS = (
    Metric("pe_ratio", "value", True, True, _positive,
           lambda v: _clamp(100 - (v - 8) * 4), lambda v: f"P/E {v:.1f}"),
    Metric("price_to_book", "value", True, True, _positive,
           lambda v: _clamp(100 - (v - 1) * 30), lambda v: f"P/B {v:.2f}"),
    Metric("peg_ratio", "value", True, False, _positive,
           lambda v: _clamp(100 - (v - 0.5) * 50), lambda v: f"PEG {v:.2f}"),

    Metric("roe", "quality", False, False, _any_value,
           lambda v: _clamp(v * 4), lambda v: f"ROE {v:.1f}%"),
    Metric("roa", "quality", False, False, _any_value,
           lambda v: _clamp(v * 50), lambda v: f"ROA {v:.1f}%"),
    Metric("net_margin", "quality", False, False, _any_value,
           lambda v: _clamp(v * 3.3), lambda v: f"net margin {v:.1f}%"),
    Metric("debt_to_equity", "quality", True, False, _nonnegative,
           lambda v: _clamp(100 - v * 40), lambda v: f"D/E {v:.2f}"),
    Metric("current_ratio", "quality", False, False, _positive,
           lambda v: _clamp(v * 50), lambda v: f"current ratio {v:.2f}"),

    Metric("eps_growth_yoy", "growth", False, False, _any_value,
           lambda v: _clamp(50 + v * 2), lambda v: f"EPS growth {v:+.1f}%"),
    Metric("revenue_growth_yoy", "growth", False, False, _any_value,
           lambda v: _clamp(50 + v * 2.5), lambda v: f"revenue growth {v:+.1f}%"),

    Metric("momentum_12_1", "momentum", False, False, _any_value,
           lambda v: _clamp(50 + v), lambda v: f"12-1 momentum {v:+.1f}%"),

    Metric("dividend_yield", "dividend", False, False, _dividend_payer,
           lambda v: _clamp(v * 12.5), lambda v: f"annual yield {v:.1f}%"),

    Metric("median_value_traded_20d", "liquidity", False, False, _positive,
           lambda v: _clamp((math.log10(v) - 6) * 33),
           lambda v: f"20-session median traded KES {v/1e6:.1f}M"),
)

METRICS_BY_KEY = {m.key: m for m in METRICS}

# Which quality metrics apply to which kind of issuer. Deposits and regulatory
# liquidity are not industrial working capital, so banks and insurers are
# judged on returns rather than on debt and current ratios.
FINANCIAL_SECTORS = ("Finance", "Banking", "Insurance")
QUALITY_METRICS_FINANCIAL = ("roe", "roa")
QUALITY_METRICS_INDUSTRIAL = ("roe", "net_margin", "debt_to_equity", "current_ratio")

NUMERIC_FIELDS = (
    "pe_ratio", "price_to_book", "peg_ratio", "roe", "roa", "net_margin",
    "debt_to_equity", "current_ratio", "eps_growth_yoy", "revenue_growth_yoy",
    "momentum_12_1", "perf_3m", "dividend_yield", "dividend_payout_ratio",
    "median_value_traded_20d",
)


def _metric_row(analysis_result, fund):
    """Merge the fundamental and price-derived inputs one score needs."""
    result = analysis_result or {}
    row = dict(fund or {})
    row["momentum_12_1"] = (result.get("momentum_12_1")
                            if row.get("momentum_12_1") is None
                            else row.get("momentum_12_1"))
    row["median_value_traded_20d"] = result.get("median_value_traded_20d")
    for key in NUMERIC_FIELDS:
        row[key] = finite_number(row.get(key))
    return row


# ---------------------------------------------------------------------------
# Cross-section
# ---------------------------------------------------------------------------

def _percentile_ranks(values, lower_is_better):
    """Midrank percentiles in (0, 100). Ties share a rank; no value is pinned.

    Percentiles are used rather than z-scores because NSE metrics are heavily
    skewed and a single outlier would otherwise compress everyone else.
    """
    ordered = sorted(values.items(), key=lambda item: item[1])
    n = len(ordered)
    ranks = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        midrank = (i + j) / 2 + 1  # 1-based, averaged over the tied block
        pct = 100 * (midrank - 0.5) / n
        for symbol, _ in ordered[i:j + 1]:
            ranks[symbol] = round(100 - pct, 1) if lower_is_better else round(pct, 1)
        i = j + 1
    return ranks


class CrossSection:
    """Percentile rank of every metric across one day's universe."""

    def __init__(self, ranks=None, sizes=None):
        self._ranks = ranks or {}
        self._sizes = sizes or {}

    def rank(self, key, symbol):
        return self._ranks.get(key, {}).get(symbol)

    def size(self, key):
        return self._sizes.get(key, 0)


def build_cross_section(rows, sector_medians=None):
    """Rank every registered metric across `rows` ({symbol: metric row}).

    A metric with fewer than MIN_CROSS_SECTION valid observations is left
    unranked, and scoring falls back to that metric's absolute anchor.
    """
    ranks, sizes = {}, {}
    for metric in METRICS:
        raw = {}
        for symbol, row in rows.items():
            value = finite_number(row.get(metric.key))
            if value is None or not metric.valid(value, row):
                continue
            raw[symbol] = value
        sizes[metric.key] = len(raw)
        if len(raw) < MIN_CROSS_SECTION:
            continue
        if metric.sector_relative:
            universe_median = statistics.median(raw.values())
            normalized = {}
            for symbol, value in raw.items():
                sector = rows[symbol].get("sector")
                med = (sector_medians or {}).get(sector, {}) if sector else {}
                peer = med.get(metric.key) if med.get(f"{metric.key}_count", 0) >= MIN_PEER_COUNT else None
                base = peer if peer and peer > 0 else universe_median
                normalized[symbol] = value / base if base and base > 0 else value
            raw = normalized
        ranks[metric.key] = _percentile_ranks(raw, metric.lower_is_better)
    return CrossSection(ranks, sizes)


def _score_metric(metric, row, symbol, ctx):
    """One metric as 0-100, by percentile rank when ranked, else by anchor.

    Returns (score, reason) or (None, None) when the metric is unusable.
    """
    value = finite_number(row.get(metric.key))
    if value is None or not metric.valid(value, row):
        return None, None
    rank = ctx.rank(metric.key, symbol) if ctx else None
    if rank is not None:
        return rank, f"{metric.label(value)} (rank {rank:.0f}/100 of {ctx.size(metric.key)})"
    return metric.anchor(value), metric.label(value)


def _score_from_keys(keys, row, symbol, ctx, empty_reason):
    parts, reasons = [], []
    for key in keys:
        score, reason = _score_metric(METRICS_BY_KEY[key], row, symbol, ctx)
        if score is None:
            continue
        parts.append(score)
        reasons.append(reason)
    if not parts:
        return None, [empty_reason], 0
    return round(sum(parts) / len(parts)), reasons, len(parts)


# ---------------------------------------------------------------------------
# Factors
# ---------------------------------------------------------------------------

def _score_value(fund, sector_medians=None, symbol=None, ctx=None):
    """Cheaper P/E, P/B and PEG score higher, ranked against the universe
    (P/E and P/B relative to the stock's own sector first)."""
    score, reasons, _ = _score_from_keys(
        ("pe_ratio", "price_to_book", "peg_ratio"), fund, symbol, ctx,
        "no valuation data")
    return score, reasons


def _score_quality(fund, symbol=None, ctx=None):
    """Higher returns and margins, lower leverage score higher."""
    if fund.get("sector") in FINANCIAL_SECTORS:
        score, reasons, _ = _score_from_keys(
            QUALITY_METRICS_FINANCIAL, fund, symbol, ctx, "no quality data")
        reasons = list(reasons) + [
            "financial firm: leverage/current ratio excluded; "
            "loan quality and regulatory capital need review"]
        return score, reasons
    score, reasons, _ = _score_from_keys(
        QUALITY_METRICS_INDUSTRIAL, fund, symbol, ctx, "no quality data")
    return score, reasons


def _score_growth(fund, symbol=None, ctx=None):
    """Higher YoY EPS/revenue growth scores higher -- an expanding business
    rather than just a cheap or a trending one."""
    score, reasons, _ = _score_from_keys(
        ("eps_growth_yoy", "revenue_growth_yoy"), fund, symbol, ctx,
        "no growth data")
    return score, reasons


# The 12-1 return is the momentum factor; the trend state only confirms it.
MOMENTUM_PRIMARY_WEIGHT = 2
MOMENTUM_TREND_WEIGHT = 1


def _score_momentum(analysis_result, fund, symbol=None, ctx=None):
    """12-month return excluding the latest month, confirmed by trend state.

    RSI is deliberately absent. It is a mean-reversion oscillator, so scoring
    it linearly rewarded exactly the overbought names `generate_alerts` warns
    about. 3-month return is absent for the same reason -- short-horizon
    returns reverse, while the documented momentum effect lives at 12-1.
    RSI survives as an alert, and `perf_3m` as a reported statistic.
    """
    signals = (analysis_result or {}).get("signals", {})
    row = dict(fund or {})
    if row.get("momentum_12_1") is None:
        row["momentum_12_1"] = (analysis_result or {}).get("momentum_12_1")
    row["momentum_12_1"] = finite_number(row.get("momentum_12_1"))

    weighted, total, reasons = 0.0, 0.0, []

    score, reason = _score_metric(METRICS_BY_KEY["momentum_12_1"], row, symbol, ctx)
    if score is not None:
        weighted += score * MOMENTUM_PRIMARY_WEIGHT
        total += MOMENTUM_PRIMARY_WEIGHT
        reasons.append(reason)
    else:
        reasons.append("no 12-1 momentum: needs ~12 months of history")

    overall = signals.get("overall")
    trend = {"bullish": 75, "neutral": 50, "bearish": 25}.get(overall)
    if trend is not None:
        weighted += trend * MOMENTUM_TREND_WEIGHT
        total += MOMENTUM_TREND_WEIGHT
        reasons.append(f"technical: {overall}")

    if total == 0:
        return None, ["no momentum data"]
    return round(weighted / total), reasons


def _score_dividend(fund, symbol=None, ctx=None):
    """Reward yield, but only if the payout looks sustainable."""
    dy = finite_number(fund.get("dividend_yield"))
    payout = finite_number(fund.get("dividend_payout_ratio"))
    if dy is None or dy < 0:
        return None, ["no annual dividend yield"]
    if dy == 0:
        return 0, ["no annual dividend"]
    if payout is None:
        return None, ["dividend sustainability unknown: payout missing"]
    if payout <= 0 or payout > 100:
        return 0, [f"yield {dy:.1f}%; payout {payout:.0f}% is not covered by earnings"]
    score, reason = _score_metric(METRICS_BY_KEY["dividend_yield"], fund, symbol, ctx)
    return round(score), [f"{reason}; payout {payout:.0f}%"]


def _score_liquidity(fund, symbol=None, ctx=None):
    """Higher traded value = easier to enter/exit. KES value traded per day."""
    vt = finite_number(fund.get("median_value_traded_20d"))
    if vt is None or vt < 0:
        return None, ["no liquidity data"]
    if vt == 0:
        return 0, ["zero median traded value over 20 sessions"]
    score, reason = _score_metric(METRICS_BY_KEY["median_value_traded_20d"], fund, symbol, ctx)
    return round(score), [reason]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_stock(symbol, analysis_result, fund, weights=None, sector_medians=None,
                cross_section=None):
    """
    Produce a transparent factor score for one stock.

    Returns dict:
        {overall, value, quality, growth, momentum, dividend, liquidity, reasons}
    Sub-scores are 0-100 or None when data is missing. `overall` is the
    weighted blend of the available sub-scores (weights renormalised).

    `cross_section` comes from `build_cross_section` (see `score_universe`) and
    is what turns raw metrics into percentile ranks. Without it -- a one-off
    single-stock call -- every metric falls back to its absolute anchor, so the
    result is comparable to the pre-v3 score but not to a ranked universe.
    """
    weights = weights or DEFAULT_WEIGHTS
    row = _metric_row(analysis_result, fund)

    subs = {
        "value": _score_value(row, sector_medians, symbol, cross_section),
        "quality": _score_quality(row, symbol, cross_section),
        "growth": _score_growth(row, symbol, cross_section),
        "momentum": _score_momentum(analysis_result, row, symbol, cross_section),
        "dividend": _score_dividend(row, symbol, cross_section),
        "liquidity": _score_liquidity(row, symbol, cross_section),
    }

    scores = {k: v[0] for k, v in subs.items()}
    reasons = {k: v[1] for k, v in subs.items()}

    # Weighted blend over available sub-scores only
    num = 0.0
    den = 0.0
    for k, s in scores.items():
        if s is not None:
            w = weights.get(k, 0)
            num += s * w
            den += w
    overall = round(num / den) if den > 0 else None

    # Coverage: how much of the intended weight actually had data behind it.
    # A score built from 2 of 6 factors isn't as trustworthy as one built
    # from all 6, even though renormalization makes both look like a clean
    # 0-100 number -- this is what lets callers flag the difference.
    total_weight = sum(weights.values()) or 1.0
    factors_present = sum(1 for s in scores.values() if s is not None)

    # How many raw metrics sat behind each factor. Coverage alone can read 100
    # while every factor rests on a single metric, so this is reported
    # alongside it -- it is diagnostic, not yet a screening rule.
    metric_counts = {}
    for key, metric in METRICS_BY_KEY.items():
        value = finite_number(row.get(key))
        if value is not None and metric.valid(value, row):
            metric_counts[metric.factor] = metric_counts.get(metric.factor, 0) + 1

    return {
        "symbol": symbol,
        "overall": overall,
        **scores,
        "reasons": reasons,
        "coverage": round(100 * den / total_weight) if overall is not None else 0,
        "factors_present": factors_present,
        "factors_total": len(scores),
        "metric_counts": metric_counts,
        "ranked": bool(cross_section and any(
            cross_section.size(m.key) >= MIN_CROSS_SECTION for m in METRICS)),
    }


def score_universe(analysis_results, fundamentals_data=None, weights=None,
                   sector_medians=None, *, validations=None, as_of=None):
    """Score every stock against the same day's cross-section.

    This is the entry point callers should use: percentile ranks only mean
    something when every name is ranked against the same universe on the same
    date. Returns {symbol: score dict}.
    """
    fundamentals_data = fundamentals_data or {}
    expected = None
    if validations is not None:
        expected = parse_date(as_of) if as_of is not None else latest_completed_session()
        if expected is None:
            raise ValueError("as_of must be an ISO date")
    rows = {}
    for symbol, result in (analysis_results or {}).items():
        if not result:
            continue
        # In production, calculate ranks only from names that could enter the
        # screened universe. Otherwise stale, misidentified, or illiquid names
        # move every other name's percentile despite being ineligible later.
        # `validations=None` preserves the utility's data-only mode for callers
        # that do not have the independent live-price checks available.
        if validations is not None:
            validation = validations.get(symbol) or {}
            latest = result.get("latest") or {}
            close = finite_number(latest.get("close"))
            liquidity = finite_number(result.get("median_value_traded_20d"))
            if (close is None or close <= 0
                    or result.get("identity_verified") is not True
                    or result.get("history_complete") is not True
                    or parse_date(result.get("history_date")) != expected
                    or validation.get("status") != "ok"
                    or validation.get("is_stale") is not False
                    or liquidity is None or liquidity < 1_000_000):
                continue
        rows[symbol] = _metric_row(result, fundamentals_data.get(symbol, {}))
    ctx = build_cross_section(rows, sector_medians)
    return {
        symbol: score_stock(symbol, (analysis_results or {}).get(symbol),
                            fundamentals_data.get(symbol, {}), weights,
                            sector_medians, cross_section=ctx)
        for symbol, result in (analysis_results or {}).items() if result
    }


def generate_alerts(symbol, analysis_result, fund, validation=None):
    """
    Produce a list of short, transparent alert strings for one stock.
    Each alert states the fact that triggered it.
    """
    alerts = []
    latest = (analysis_result or {}).get("latest", {})
    signals = (analysis_result or {}).get("signals", {})
    fund = fund or {}

    rsi = latest.get("rsi")
    if rsi is not None:
        if rsi < 30:
            alerts.append(f"🟢 Oversold (RSI {rsi:.0f})")
        elif rsi > 70:
            alerts.append(f"🔴 Overbought (RSI {rsi:.0f})")

    # 52-week proximity
    close = latest.get("close")
    hi = fund.get("price_52w_high")
    lo = fund.get("price_52w_low")
    if close and hi and hi > 0 and close >= hi * 0.98:
        alerts.append("🔺 Near 52-week high")
    if close and lo and lo > 0 and close <= lo * 1.03:
        alerts.append("🔻 Near 52-week low")

    # Strong technical signal
    tr = fund.get("tech_rating")
    if tr is not None:
        if tr > 0.5:
            alerts.append("⭐ TradingView: Strong Buy signal")
        elif tr < -0.5:
            alerts.append("⚠️ TradingView: Strong Sell signal")

    # Fresh MACD cross
    if signals.get("macd") == "bullish_cross":
        alerts.append("📈 MACD bullish crossover today")
    elif signals.get("macd") == "bearish_cross":
        alerts.append("📉 MACD bearish crossover today")

    # High sustainable dividend yield
    dy = fund.get("dividend_yield")
    payout = fund.get("dividend_payout_ratio")
    if dy and dy >= 8 and payout is not None and 0 < payout <= 100:
        alerts.append(f"💰 High dividend yield ({dy:.1f}%)")

    # Upcoming dividend ex-date
    if fund.get("dividend_ex_date_is_upcoming") and fund.get("dividend_ex_date"):
        alerts.append(f"📅 Ex-dividend {fund['dividend_ex_date']}")

    # Illiquid warning: the same sustained measure the buy list screens on,
    # so an alert can no longer contradict eligibility.
    vt = (analysis_result or {}).get("median_value_traded_20d")
    if vt is not None and vt < 1_000_000:
        alerts.append("💧 Thinly traded (hard to exit)")

    # Price-source disagreement
    if validation and validation.get("status") == "mismatch":
        alerts.append(f"❗ Price unverified — {validation.get('note', '')}")
    elif validation and validation.get("status") == "stale":
        alerts.append(f"🕒 {validation.get('note', '')}")

    return alerts


# ---- Test ----
if __name__ == "__main__":
    import json, glob
    files = glob.glob("../data/fundamentals_*.json")
    if files:
        data = json.load(open(files[0]))
        scores = score_universe({s: {} for s in data}, data)
        for sym in ["SCOM", "KCB", "EQTY", "HAFR"]:
            sc = scores.get(sym, {})
            print(f"\n{sym}: overall={sc.get('overall')}  "
                  f"V={sc.get('value')} Q={sc.get('quality')} M={sc.get('momentum')} "
                  f"D={sc.get('dividend')} L={sc.get('liquidity')}")
            print("   alerts:", generate_alerts(sym, {}, data.get(sym, {})))
