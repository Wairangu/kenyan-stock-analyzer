"""Conservative, score-first screen. Thresholds are policy, not return forecasts."""

from fundamental_analysis import FundamentalAnalysis
from data_quality import finite_number, latest_completed_session, parse_date

BUYABLE_TIERS = ("strong_buy", "buy")
ILLIQUID_THRESHOLD_KES = 1_000_000
DEFAULT_MIN_COVERAGE = 80
DEFAULT_MIN_SCORE = 60
# Hysteresis: a name already held is only dropped once its score falls well
# below the entry bar. Rotating on every rank change pays the round trip below
# for a difference that is mostly noise, so entry and exit are separate
# decisions with a deliberate dead band between them.
DEFAULT_HOLD_SCORE = 45
DEFAULT_FEE_PCT = 0.015
DEFAULT_SLIPPAGE_PCT = 0.001
STRATEGY_VERSION = "screen-v3"


def round_trip_breakeven_pct(fee_pct=DEFAULT_FEE_PCT, slippage_pct=DEFAULT_SLIPPAGE_PCT):
    """Percent the quoted price must rise before a full round trip breaks even.

    Both sides pay commission and slippage, so at the 1.5% default a position
    starts roughly 3.2% under water. Any candidate is a bet that it clears this
    hurdle within the holding period; the screen produces no expected return,
    so the number is published beside every candidate rather than assumed away.
    """
    fee = finite_number(fee_pct)
    slip = finite_number(slippage_pct)
    if fee is None or not 0 <= fee < 1 or slip is None or not 0 <= slip < 1:
        raise ValueError("Fee and slippage must be finite fractions in [0, 1).")
    buy = (1 + fee) * (1 + slip)
    sell = (1 - fee) * (1 - slip)
    return round((buy / sell - 1) * 100, 4)


def build_candidate_list(analysis_results, fundamentals_data, scores,
                         min_coverage=DEFAULT_MIN_COVERAGE, *, validations=None,
                         min_score=DEFAULT_MIN_SCORE, as_of=None, exclusions=None,
                         holdings=None, hold_score=DEFAULT_HOLD_SCORE,
                         fee_pct=DEFAULT_FEE_PCT, slippage_pct=DEFAULT_SLIPPAGE_PCT):
    """Reject unknown inputs, then rank by factor score.

    as_of is the expected completed NSE session, for reproducible evaluation.
    exclusions optionally receives reasons for each rejection.
    Omitting validation results intentionally produces no candidates.

    holdings names the symbols already owned. Those keep their place while
    their score stays at or above hold_score, and are not dropped merely
    because the technical gauge turned neutral. Every data-integrity rule still
    applies to them: without a trustworthy current price there is no decision
    to make either way.
    """
    if not hold_score <= min_score:
        raise ValueError("hold_score must not exceed min_score")
    breakeven = round_trip_breakeven_pct(fee_pct, slippage_pct)
    expected = parse_date(as_of) if as_of is not None else latest_completed_session()
    if expected is None:
        raise ValueError("as_of must be an ISO date")
    held_symbols = set(holdings or ())
    candidates = []
    for symbol, result in (analysis_results or {}).items():
        result = result or {}
        fund = (fundamentals_data or {}).get(symbol) or {}
        score = (scores or {}).get(symbol) or {}
        validation = (validations or {}).get(symbol) or {}
        price = finite_number(result.get("latest", {}).get("close"))
        overall = finite_number(score.get("overall"))
        coverage = finite_number(score.get("coverage"))
        liquidity = finite_number(result.get("median_value_traded_20d"))
        label, tier = FundamentalAnalysis.signal_from_tech_rating(fund.get("tech_rating"))
        held = symbol in held_symbols
        threshold = hold_score if held else min_score
        reasons = []
        if price is None or price <= 0:
            reasons.append("invalid price")
        if not result.get("identity_verified"):
            reasons.append("security identity unverified")
        if not result.get("history_complete"):
            reasons.append("insufficient price history")
        if parse_date(result.get("history_date")) != expected:
            reasons.append("price history is not from the latest completed session")
        if validation.get("status") != "ok" or validation.get("is_stale") is not False:
            reasons.append("price is stale, disputed or unverified")
        if liquidity is None or liquidity < ILLIQUID_THRESHOLD_KES:
            reasons.append("insufficient sustained liquidity")
        if overall is None or not threshold <= overall <= 100:
            reasons.append(f"score below {'hold' if held else 'entry'} minimum or missing")
        if coverage is None or not min_coverage <= coverage <= 100:
            reasons.append("insufficient score coverage")
        for factor in ("value", "quality", "growth"):
            if finite_number(score.get(factor)) is None:
                reasons.append(f"missing essential {factor} factor")
        if tier not in BUYABLE_TIERS and not held:
            reasons.append("no positive technical rating")
        if reasons:
            if exclusions is not None:
                exclusions[symbol] = reasons
            continue
        candidates.append({
            "symbol": symbol, "price": price, "tv_label": label, "tv_class": tier,
            "score": overall, "score_coverage": coverage,
            "sector": fund.get("sector") or "Unknown", "price_date": str(expected),
            "median_value_traded_20d": liquidity, "strategy_version": STRATEGY_VERSION,
            "held": held, "action": "hold" if held else "buy",
            "min_score_applied": threshold, "breakeven_pct": breakeven,
        })
    candidates.sort(key=lambda c: (-c["score"], -c["score_coverage"], c["symbol"]))
    return candidates


def screen_with_alerts(analysis_results, fundamentals_data, scores, validations, alerts,
                       holdings=None):
    """Keep purchase exclusions visible in the ordinary report alerts."""
    exclusions = {}
    candidates = build_candidate_list(analysis_results, fundamentals_data, scores,
                                      validations=validations, exclusions=exclusions,
                                      holdings=holdings)
    for symbol, reasons in exclusions.items():
        alerts.setdefault(symbol, []).append("Not eligible for buy list: " + "; ".join(reasons))
    return candidates
