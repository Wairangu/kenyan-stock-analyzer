"""
Market context module.

Provides broad-market context that helps interpret individual stock data:
  - USD/KES exchange rate (relevant for foreign-exposed and dual-listed names)
  - Sector-median valuation metrics, so a stock's P/E, P/B and dividend yield
    can be read RELATIVE to its peers rather than in isolation.

Everything fails safe: if the FX source is unreachable, the rate is simply
None and the rest of the pipeline is unaffected.
"""

import statistics
from datetime import datetime

import requests

from logger import get_logger

logger = get_logger(__name__)

FX_URL = "https://open.er-api.com/v6/latest/USD"


def fetch_usd_kes():
    """
    Fetch the current USD/KES rate. Returns dict {rate, updated} or None.
    """
    try:
        from utils import http_get
        resp = http_get(FX_URL)
        if resp is None or resp.status_code != 200:
            return None
        r = resp.json()
        rate = r.get("rates", {}).get("KES")
        if rate:
            return {
                "rate": round(float(rate), 2),
                "updated": r.get("time_last_update_utc", ""),
                "source": "open.er-api.com",
            }
    except Exception as e:
        logger.warning(f"USD/KES rate unavailable: {e}")
    return None


def compute_sector_medians(fundamentals_data):
    """
    Compute per-sector median valuation metrics across all stocks.

    Args:
        fundamentals_data: {symbol: {metric: value}} from FundamentalAnalysis.

    Returns:
        dict: {sector: {'pe_ratio': median, 'price_to_book': median,
                        'dividend_yield': median, 'roe': median, 'count': n}}
    """
    buckets = {}
    for _, f in (fundamentals_data or {}).items():
        if not f:
            continue
        sector = f.get("sector") or "Unknown"
        buckets.setdefault(sector, []).append(f)

    medians = {}
    for sector, rows in buckets.items():
        def med(key, positive_only=True):
            vals = []
            for r in rows:
                v = r.get(key)
                if v is None:
                    continue
                try:
                    v = float(v)
                except (ValueError, TypeError):
                    continue
                if positive_only and v <= 0:
                    continue
                vals.append(v)
            return round(statistics.median(vals), 2) if vals else None

        medians[sector] = {
            "pe_ratio": med("pe_ratio"),
            "price_to_book": med("price_to_book"),
            "dividend_yield": med("dividend_yield", positive_only=False),
            "roe": med("roe", positive_only=False),
            "count": len(rows),
        }
    return medians


def valuation_vs_sector(fund, sector_medians):
    """
    Compare a single stock's valuation against its sector median.

    Returns:
        dict of {metric: {'value', 'sector_median', 'verdict'}} where verdict
        is a short plain-English tag like 'cheaper than sector'.
    """
    sector = fund.get("sector") or "Unknown"
    med = (sector_medians or {}).get(sector, {})
    result = {}

    def compare(key, value, lower_is_cheaper=True):
        m = med.get(key)
        if value is None or m is None or m == 0:
            return None
        try:
            value = float(value)
        except (ValueError, TypeError):
            return None
        ratio = value / m
        if lower_is_cheaper:
            if ratio < 0.8:
                verdict = "cheaper than sector"
            elif ratio > 1.25:
                verdict = "pricier than sector"
            else:
                verdict = "in line with sector"
        else:  # higher is better (yield, roe)
            if ratio > 1.25:
                verdict = "above sector"
            elif ratio < 0.8:
                verdict = "below sector"
            else:
                verdict = "in line with sector"
        return {"value": round(value, 2), "sector_median": m, "verdict": verdict}

    result["pe_ratio"] = compare("pe_ratio", fund.get("pe_ratio"), True)
    result["price_to_book"] = compare("price_to_book", fund.get("price_to_book"), True)
    result["dividend_yield"] = compare("dividend_yield", fund.get("dividend_yield"), False)
    result["roe"] = compare("roe", fund.get("roe"), False)
    return {k: v for k, v in result.items() if v is not None}


# ---- Test ----
if __name__ == "__main__":
    from logger import setup_logging
    setup_logging()
    logger = get_logger(__name__)

    fx = fetch_usd_kes()
    print("USD/KES:", fx)

    import json, glob
    files = glob.glob("../data/fundamentals_*.json")
    if files:
        data = json.load(open(files[0]))
        medians = compute_sector_medians(data)
        print(f"\nSector medians ({len(medians)} sectors):")
        for s, m in list(medians.items())[:5]:
            print(f"  {s}: {m}")
        scom = data.get("SCOM", {})
        print("\nSCOM vs sector:", valuation_vs_sector(scom, medians))
