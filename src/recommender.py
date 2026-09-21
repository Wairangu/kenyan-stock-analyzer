"""
Candidate ranking for the "budget -> buy list" feature.

Pure functions, no AWS/network -- this is the one place that decides
"what counts as buy-worthy" for the whole system, so the portfolio app
(a separate, independently-deployed Lambda) never has to re-derive it; it
only ever does budget arithmetic against an already-ranked list.

Reuses the same signals the rest of the pipeline already trusts: TradingView's
tech-rating classification (fundamental_analysis.signal_from_tech_rating,
also used by email_notifier.py's "Strong Buy & Strong Sell" table) and the
same illiquidity gate as scoring.generate_alerts's "Thinly traded" alert
(value_traded < 1,000,000 KES/day).
"""

from fundamental_analysis import FundamentalAnalysis
from logger import get_logger

logger = get_logger(__name__)

BUYABLE_TIERS = ("strong_buy", "buy")
TIER_RANK = {"strong_buy": 0, "buy": 1}
ILLIQUID_THRESHOLD_KES = 1_000_000
DEFAULT_MIN_COVERAGE = 60


def build_candidate_list(analysis_results, fundamentals_data, scores,
                          min_coverage=DEFAULT_MIN_COVERAGE):
    """
    Returns a ranked list of {symbol, price, tv_label, tv_class, score,
    score_coverage} for every stock that is (a) currently Buy or Strong
    Buy, (b) has a score built from enough factors to trust
    (score_coverage >= min_coverage), and (c) isn't too thin to actually
    exit a position in. Sorted strong_buy-before-buy, then score
    descending.
    """
    fundamentals_data = fundamentals_data or {}
    scores = scores or {}

    candidates = []
    for symbol, result in (analysis_results or {}).items():
        if not result:
            continue
        latest = result.get("latest", {})
        price = latest.get("close")
        if not price:
            continue

        fund = fundamentals_data.get(symbol, {})
        tv_label, tv_class = FundamentalAnalysis.signal_from_tech_rating(
            fund.get("tech_rating")
        )
        if tv_class not in BUYABLE_TIERS:
            continue

        value_traded = fund.get("value_traded")
        if value_traded is not None and value_traded < ILLIQUID_THRESHOLD_KES:
            continue

        sc = scores.get(symbol, {})
        score = sc.get("overall")
        coverage = sc.get("coverage")
        if coverage is not None and coverage < min_coverage:
            continue

        candidates.append({
            "symbol": symbol,
            "price": price,
            "tv_label": tv_label,
            "tv_class": tv_class,
            "score": score,
            "score_coverage": coverage,
        })

    candidates.sort(key=lambda c: (
        TIER_RANK.get(c["tv_class"], 99),
        -(c["score"] if c["score"] is not None else -1),
    ))
    return candidates


# ---- Test ----
if __name__ == "__main__":
    from logger import setup_logging
    setup_logging()

    fake_results = {
        "AAA": {"latest": {"close": 50.0}},
        "BBB": {"latest": {"close": 20.0}},
        "CCC": {"latest": {"close": 10.0}},
        "DDD": {"latest": {"close": 5.0}},
    }
    fake_fund = {
        "AAA": {"tech_rating": 0.6, "value_traded": 5_000_000},   # strong_buy, liquid
        "BBB": {"tech_rating": 0.2, "value_traded": 2_000_000},   # buy, liquid
        "CCC": {"tech_rating": 0.6, "value_traded": 100_000},     # strong_buy but illiquid -> excluded
        "DDD": {"tech_rating": -0.6, "value_traded": 5_000_000},  # strong_sell -> excluded
    }
    fake_scores = {
        "AAA": {"overall": 60, "coverage": 80},
        "BBB": {"overall": 90, "coverage": 80},
        "CCC": {"overall": 95, "coverage": 80},
        "DDD": {"overall": 10, "coverage": 80},
    }

    out = build_candidate_list(fake_results, fake_fund, fake_scores)
    print(out)
    assert [c["symbol"] for c in out] == ["AAA", "BBB"], out
    print("OK")
