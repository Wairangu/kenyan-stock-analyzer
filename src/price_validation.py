"""
Price validation module.

Cross-checks the prices shown on the dashboard (sourced from TradingView)
against an INDEPENDENT second source — afx.kwayisi.org, which publishes the
Nairobi Securities Exchange board — and assesses data freshness.

The goal is accuracy: rather than trusting a single feed, every stock's price
is compared against a second source and flagged when they disagree, and each
price is tagged with when the stock last actually traded (thinly-traded NSE
counters may not trade for days, making a "last price" misleading).

Everything here fails safe: if the second source is unreachable, validation
degrades to "unverified" and the rest of the pipeline is unaffected.
"""

import os
import re
import json
import requests
from datetime import datetime

import pandas as pd

from logger import get_logger

logger = get_logger(__name__)

AFX_URL = "https://afx.kwayisi.org/nse/"
REFERENCE_SOURCE = "afx.kwayisi.org"


class PriceValidator:
    """Validate dashboard prices against an independent source + freshness."""

    def __init__(self, cache_dir="data", disagree_threshold_pct=1.0):
        """
        Args:
            cache_dir: Directory for the daily reference-price cache.
            disagree_threshold_pct: Percent difference above which the two
                sources are considered to disagree.
        """
        self.cache_dir = cache_dir
        self.disagree_threshold_pct = disagree_threshold_pct
        os.makedirs(self.cache_dir, exist_ok=True)
        self._reference = None  # {ticker: {'price': float, 'volume': int}}
        self._clean_old_cache()

    def _clean_old_cache(self):
        today = datetime.now().strftime("%Y%m%d")
        for fname in os.listdir(self.cache_dir):
            if fname.startswith("reference_prices_") and today not in fname:
                try:
                    os.remove(os.path.join(self.cache_dir, fname))
                except OSError:
                    pass

    # ---- Reference price acquisition ----

    def _cache_path(self):
        today = datetime.now().strftime("%Y%m%d")
        return os.path.join(self.cache_dir, f"reference_prices_{today}.json")

    def fetch_reference_prices(self, force_refresh=False):
        """
        Fetch the independent NSE price board.

        Always fetched fresh once per run (a single cheap HTTP request), then
        held in memory for the rest of the run. We deliberately do NOT reuse a
        same-day disk cache: NSE prices settle after the close, so an earlier
        intraday snapshot would be stale — every run must reflect the current
        official price.

        Returns:
            dict: {ticker: {'price': float, 'volume': int, 'change': float}},
            or {} on failure.
        """
        if self._reference is not None:
            return self._reference

        path = self._cache_path()

        # Fetch fresh via the shared fast-fail HTTP helper (tight timeout +
        # one retry; returns None instead of hanging when the source is down).
        from utils import http_get
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = http_get(AFX_URL, headers=headers)
        if resp is None or resp.status_code != 200:
            logger.warning(
                f"Reference price source unavailable — skipping validation "
                f"and price anchoring. TradingView values will be used as-is."
            )
            self._reference = {}
            return self._reference
        try:
            self._reference = self._parse_afx(resp.text)
            logger.info(
                f"Reference prices from {REFERENCE_SOURCE}: "
                f"{len(self._reference)} stocks"
            )
            if self._reference:
                try:
                    with open(path, "w") as f:
                        json.dump(self._reference, f)
                except Exception as e:
                    logger.debug(f"Reference cache write error: {e}")
        except Exception as e:
            logger.warning(f"Reference price parse failed: {e}")
            self._reference = {}

        return self._reference

    @staticmethod
    def _parse_afx(html):
        """
        Parse the afx.kwayisi.org NSE board. Its rows use unclosed tags:
            <tr><td><a>TICKER</a><td><a>Name</a><td>VOLUME<td>PRICE<td ...>CHG
        """
        strip = lambda s: re.sub(r"<[^>]+>", "", s).replace("&amp;", "&").strip()
        out = {}
        for row in html.split("<tr>"):
            if "/nse/" not in row:
                continue
            segs = row.split("<td")
            if len(segs) < 5:
                continue
            cells = [
                strip(s.split(">", 1)[1]) if ">" in s else "" for s in segs[1:]
            ]
            ticker = cells[0]
            if not re.fullmatch(r"[A-Z0-9]{2,6}", ticker):
                continue
            try:
                volume = int(cells[2].replace(",", ""))
                price = float(cells[3].replace(",", ""))
            except (ValueError, IndexError):
                continue
            # The last cell is the day's absolute price change (may be +/-).
            change = None
            if len(cells) > 4:
                try:
                    change = float(cells[4].replace(",", "").replace("+", ""))
                except ValueError:
                    change = None
            out[ticker] = {"price": price, "volume": volume, "change": change}
        return out

    # ---- Validation ----

    def validate(self, symbol, dashboard_price, history_df=None):
        """
        Validate one stock's price and assess freshness.

        Args:
            symbol: Ticker symbol.
            dashboard_price: The price the dashboard shows (TradingView).
            history_df: Optional OHLCV DataFrame to derive last-traded date.

        Returns:
            dict with keys:
                reference_price, reference_source, pct_diff, agree,
                last_traded_date, days_stale, is_stale, status, note
        """
        ref = self.fetch_reference_prices()
        ref_row = ref.get(symbol.upper()) if ref else None
        reference_price = ref_row["price"] if ref_row else None

        pct_diff = None
        agree = None
        if reference_price and dashboard_price and reference_price > 0:
            pct_diff = (dashboard_price - reference_price) / reference_price * 100
            agree = abs(pct_diff) <= self.disagree_threshold_pct

        # Freshness from history (last bar with real volume)
        last_traded_date = None
        days_stale = None
        if history_df is not None and not history_df.empty:
            try:
                traded = history_df
                if "volume" in history_df.columns:
                    traded = history_df[history_df["volume"] > 0]
                if not traded.empty:
                    last_idx = traded.index[-1]
                    last_traded_date = (
                        last_idx.strftime("%Y-%m-%d")
                        if hasattr(last_idx, "strftime")
                        else str(last_idx)
                    )
                    if hasattr(last_idx, "date"):
                        days_stale = (datetime.now().date() - last_idx.date()).days
            except Exception as e:
                logger.debug(f"Freshness calc error for {symbol}: {e}")

        is_stale = bool(days_stale and days_stale > 1)

        # Status precedence: unverified -> mismatch -> stale -> ok
        if reference_price is None:
            status = "unverified"
            note = "No independent source to compare against"
        elif agree is False:
            status = "mismatch"
            note = (
                f"TradingView ({dashboard_price:g}) differs from the NSE "
                f"official close ({reference_price:g}) by {pct_diff:+.1f}% "
                f"— price uncertain"
            )
        elif is_stale:
            status = "stale"
            note = f"Last traded {days_stale} days ago ({last_traded_date})"
        else:
            status = "ok"
            note = f"Matches {REFERENCE_SOURCE} within {self.disagree_threshold_pct:g}%"

        return {
            "reference_price": reference_price,
            "reference_source": REFERENCE_SOURCE,
            "pct_diff": round(pct_diff, 2) if pct_diff is not None else None,
            "agree": agree,
            "last_traded_date": last_traded_date,
            "days_stale": days_stale,
            "is_stale": is_stale,
            "status": status,
            "note": note,
        }


def apply_official_close(analysis_results, reference, logger=None):
    """
    Anchor each stock's DISPLAYED price and daily change to the NSE official
    closing price (from the reference board), which after market close is a
    stable, settled value — unlike TradingView's ~15-min-delayed feed.

    - Sets latest['close'] to the official close.
    - Recomputes daily_change_pct from the official day's change when available.
    - Keeps the original TradingView close as latest['tv_close'] so the two can
      still be cross-checked, and tags latest['price_source'].

    Fails safe per stock: if there is no official price, the TradingView value
    is left untouched. Returns the number of stocks anchored.
    """
    anchored = 0
    for sym, result in (analysis_results or {}).items():
        if not result:
            continue
        latest = result.get('latest')
        if latest is None:
            continue
        row = (reference or {}).get(sym.upper())
        if not row or not row.get('price'):
            latest['price_source'] = 'TradingView'
            continue
        off = row['price']
        latest['tv_close'] = latest.get('close')
        latest['close'] = off
        latest['price_source'] = 'NSE official (afx)'
        chg = row.get('change')
        if chg is not None and (off - chg) != 0:
            result['daily_change_pct'] = chg / (off - chg) * 100.0
        anchored += 1
    if logger:
        logger.info(f"  Anchored {anchored} prices to the NSE official close")
    return anchored


# ---- Test ----
if __name__ == "__main__":
    from logger import setup_logging
    setup_logging()
    logger = get_logger(__name__)

    pv = PriceValidator()
    ref = pv.fetch_reference_prices()
    print(f"Reference prices: {len(ref)} stocks")
    for sym, price in [("SCOM", 35.0), ("KCB", 81.0), ("HAFR", 1.18)]:
        print(f"\n{sym} @ {price}:")
        print(" ", pv.validate(sym, price))
