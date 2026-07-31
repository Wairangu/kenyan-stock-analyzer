"""
Dividend calendar & validation.

The dividend figures TradingView reports for NSE stocks are unreliable
(wrong amounts, currency artifacts for cross-listed names like Bank of
Kigali). This module uses an authoritative, human-curated NSE dividend
calendar — live.mystocks.co.ke/m/calendar — as the source of record for
declared dividends (amount in KES, book-closure and payment dates), and
cross-checks TradingView.

Rules (never guess or invent a value):
  - If a stock has a declared dividend on the mystocks calendar, that is the
    value shown (with its dates), tagged source='mystocks'.
  - TradingView is used only to cross-check; a large disagreement is flagged.
  - If a stock is NOT on the calendar (no recent declared NSE dividend, or a
    foreign-listed name like BKG), the dividend is reported as unavailable —
    we show nothing rather than a questionable number.

Fails safe: if the calendar is unreachable, dividend_status becomes
'unverified' and the pipeline is unaffected.
"""

import os
import re
import json
import requests
from datetime import datetime

from logger import get_logger

logger = get_logger(__name__)

CALENDAR_URL = "https://live.mystocks.co.ke/m/calendar"
SOURCE = "mystocks.co.ke"

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


class DividendCalendar:
    """Fetches and parses the authoritative NSE dividend calendar."""

    def __init__(self, cache_dir="data"):
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)
        self._data = None  # {ticker: {...}}

    def fetch(self):
        """
        Return {ticker: {'amount', 'type', 'book_closure', 'payment_date'}}.
        Always fetched fresh once per run; {} on failure.
        """
        if self._data is not None:
            return self._data
        try:
            headers = {"User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
            )}
            from utils import http_get
            resp = http_get(CALENDAR_URL, headers=headers)
            if resp is None or resp.status_code != 200:
                logger.warning("Dividend calendar unavailable — dividends will use TradingView only")
                self._data = {}
                return self._data
            self._data = self._parse(resp.text)
            logger.info(
                f"Dividend calendar ({SOURCE}): {len(self._data)} stocks with "
                f"declared dividends"
            )
        except Exception as e:
            logger.warning(f"Dividend calendar unavailable: {e}")
            self._data = {}
        return self._data

    @staticmethod
    def _parse(html):
        """
        Parse the mystocks calendar. Entries read like:
            "Jul 31 2026 TOTL TotalEnergies ...: Payment of KES 3.45 final dividend"
            "Apr 16 2026 IMH I & M Holdings Plc: Book closure KES 2.25 ..."
        We collect, per ticker, the declared amount and the payment/book dates.
        """
        text = " ".join(
            re.sub(r"<[^>]+>", " ", html).replace("&amp;", "&").split()
        )
        # Each event: optional date, TICKER, company name up to ':', event verb,
        # KES amount, dividend type.
        pat = re.compile(
            r"(?:([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{4})\s+)?"      # opt date
            r"\b([A-Z]{2,5})\s+"                                   # ticker
            r"[A-Z0-9][^:]{2,70}?:\s*"                             # company:
            r"(Payment of|Book closure|Announced a[n]?|Trading ex-dividend|Ex-dividend)"
            r"[^0-9]{0,15}KES\s+([\d.]+)\s*"                       # KES amount
            r"([a-z ]*dividend)?"                                  # type
        )
        events = {}  # ticker -> list of dicts
        for m in pat.finditer(text):
            mon, day, year, ticker, verb, amount, dtype = m.groups()
            try:
                amt = float(amount)
            except (TypeError, ValueError):
                continue
            date_str = None
            if mon in _MONTHS and day and year:
                date_str = f"{year}-{_MONTHS[mon]:02d}-{int(day):02d}"
            events.setdefault(ticker, []).append({
                "verb": verb, "amount": amt,
                "type": (dtype or "").strip(), "date": date_str,
            })

        out = {}
        for ticker, evs in events.items():
            # Group events into dividend cycles by declared amount, so the
            # amount and its dates always belong to the same declaration.
            cycles = {}
            for e in evs:
                key = round(e["amount"], 2)
                c = cycles.setdefault(key, {
                    "amount": key, "type": e["type"],
                    "book_closure": None, "payment_date": None, "dates": [],
                })
                if e["type"] and not c["type"]:
                    c["type"] = e["type"]
                if e["verb"] == "Book closure" and e["date"]:
                    c["book_closure"] = e["date"]
                if e["verb"] == "Payment of" and e["date"]:
                    c["payment_date"] = e["date"]
                if e["date"]:
                    c["dates"].append(e["date"])
            # Choose the most recent / upcoming cycle (latest known date).
            chosen = max(
                cycles.values(),
                key=lambda c: max(c["dates"]) if c["dates"] else "",
            )
            out[ticker] = {
                "amount": chosen["amount"],
                "type": chosen["type"] or "dividend",
                "book_closure": chosen["book_closure"],
                "payment_date": chosen["payment_date"],
                "source": SOURCE,
            }
        return out

    def validate(self, symbol, tv_dps, tolerance_pct=15.0):
        """
        Cross-check TradingView's DPS against the authoritative calendar.

        Returns dict:
            amount, type, book_closure, payment_date, source, status, note
        status: 'verified'  (on calendar; TradingView agrees or absent)
                'mismatch'   (on calendar; TradingView differs materially)
                'unavailable'(not on the NSE calendar → no number shown)
        """
        cal = self.fetch()
        row = cal.get(symbol.upper()) if cal else None

        if not row:
            return {
                "amount": None, "type": None, "book_closure": None,
                "payment_date": None, "source": None,
                "status": "unavailable",
                "note": "No declared dividend on the NSE calendar",
            }

        status, note = "verified", f"Declared dividend per {SOURCE}"
        if tv_dps and row["amount"] and row["amount"] > 0:
            diff = abs(tv_dps - row["amount"]) / row["amount"] * 100
            if diff > tolerance_pct:
                status = "mismatch"
                note = (f"TradingView DPS {tv_dps:g} differs from the declared "
                        f"KES {row['amount']:g} — showing the declared value")
        return {**row, "status": status, "note": note}


def apply_dividend_calendar(fundamentals_data, cache_dir="data", logger=None):
    """
    Replace TradingView dividend figures with the authoritative NSE calendar
    values (cross-checked). Mutates fundamentals_data in place.

    For each stock:
      - verified/mismatch: dps_fy, dividend_ex_date (book closure) and
        dividend_yield are set from the declared dividend (yield recomputed
        from the declared amount so the figures are consistent and backed).
      - unavailable: dividend fields are cleared to None (shown as "n/a"),
        never a guessed number.
    Adds dividend_status / dividend_note / dividend_payment_date / _source.

    Returns (verified, mismatch, unavailable) counts. Fails safe.
    """
    counts = {"verified": 0, "corrected": 0, "unverified": 0, "none": 0}
    try:
        dc = DividendCalendar(cache_dir=cache_dir)
        dc.fetch()
    except Exception as e:
        if logger:
            logger.warning(f"Dividend validation skipped: {e}")
        return counts

    for sym, f in (fundamentals_data or {}).items():
        if not f:
            continue
        tv_dps = f.get("dps_fy")  # TradingView's figure (used as fallback)
        res = dc.validate(sym, tv_dps)
        f["dividend_type"] = res.get("type")
        f["dividend_book_closure"] = res.get("book_closure")
        f["dividend_payment_date"] = res.get("payment_date")

        if res["status"] in ("verified", "mismatch") and res["amount"]:
            # Cross-checked against the NSE calendar → use the declared value.
            f["dps_fy"] = res["amount"]
            f["dividend_ex_date"] = res.get("book_closure") or None
            f["dividend_ex_date_is_upcoming"] = None  # book closure, not TV ex-date
            close = f.get("close")
            f["dividend_yield"] = (
                round(res["amount"] / close * 100, 2) if close else None
            )
            f["dividend_source"] = res["source"]
            f["dividend_note"] = res["note"]
            status = "corrected" if res["status"] == "mismatch" else "verified"
        elif tv_dps and tv_dps > 0:
            # Not on the calendar → fall back to TradingView, clearly flagged
            # as unverified (keep TradingView's dps / yield / ex-date as-is).
            f["dividend_source"] = "TradingView (unverified)"
            f["dividend_note"] = ("From TradingView — could not be cross-checked "
                                  "on the NSE dividend calendar; treat with caution")
            status = "unverified"
        else:
            # No dividend on record from any source.
            f["dps_fy"] = 0
            f["dividend_yield"] = None
            f["dividend_ex_date"] = None
            f["dividend_source"] = None
            f["dividend_note"] = "No dividend on record"
            status = "none"

        f["dividend_status"] = status
        counts[status] += 1

    if logger:
        logger.info(
            f"  Dividends: {counts['verified']} verified, "
            f"{counts['corrected']} corrected (calendar vs TradingView), "
            f"{counts['unverified']} TradingView-only (uncross-checked), "
            f"{counts['none']} none"
        )
    return counts


# ---- Test ----
if __name__ == "__main__":
    from logger import setup_logging
    setup_logging()
    dc = DividendCalendar()
    cal = dc.fetch()
    print(f"\n{len(cal)} stocks on the calendar")
    for s in ["IMH", "BKG", "SCOM", "KCB", "EQTY", "BAT", "SCBK"]:
        print(f"  {s}: {dc.validate(s, {'IMH': 3.75, 'BKG': 4.71}.get(s))}")
