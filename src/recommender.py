"""Conservative, score-first screen. Thresholds are policy, not return forecasts."""

from fundamental_analysis import FundamentalAnalysis
from data_quality import finite_number, latest_completed_session, parse_date

BUYABLE_TIERS = ("strong_buy", "buy")
ILLIQUID_THRESHOLD_KES = 1_000_000
DEFAULT_MIN_COVERAGE = 80
DEFAULT_MIN_SCORE = 60
STRATEGY_VERSION = "screen-v2"


def build_candidate_list(analysis_results, fundamentals_data, scores,
                         min_coverage=DEFAULT_MIN_COVERAGE, *, validations=None,
                         min_score=DEFAULT_MIN_SCORE, as_of=None, exclusions=None):
    """Reject unknown inputs, then rank by factor score.

    as_of is the expected completed NSE session, for reproducible evaluation.
    exclusions optionally receives reasons for each rejection.
    Omitting validation results intentionally produces no candidates.
    """
    expected = parse_date(as_of) if as_of is not None else latest_completed_session()
    if expected is None:
        raise ValueError("as_of must be an ISO date")
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
        if overall is None or not min_score <= overall <= 100:
            reasons.append("score below minimum or missing")
        if coverage is None or not min_coverage <= coverage <= 100:
            reasons.append("insufficient score coverage")
        for factor in ("value", "quality", "growth"):
            if finite_number(score.get(factor)) is None:
                reasons.append(f"missing essential {factor} factor")
        if tier not in BUYABLE_TIERS:
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
        })
    candidates.sort(key=lambda c: (-c["score"], -c["score_coverage"], c["symbol"]))
    return candidates


def screen_with_alerts(analysis_results, fundamentals_data, scores, validations, alerts):
    """Keep purchase exclusions visible in the ordinary report alerts."""
    exclusions = {}
    candidates = build_candidate_list(analysis_results, fundamentals_data, scores,
                                      validations=validations, exclusions=exclusions)
    for symbol, reasons in exclusions.items():
        alerts.setdefault(symbol, []).append("Not eligible for buy list: " + "; ".join(reasons))
    return candidates
