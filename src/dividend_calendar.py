"""
Dividend declarations from a secondary calendar, separate from annual metrics.

A declaration is an instalment, not a full-year total. Annual DPS and yield
remain provider estimates and are never replaced by a single calendar event.
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
        Return an individual declaration, never compare it to annual DPS.

        Returns dict:
            amount, type, book_closure, payment_date, source, status, note
        status: 'declaration_only' or 'unavailable'. Annual DPS is not validated.
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

        status, note = "declaration_only", f"Single declaration per {SOURCE}; annual DPS is not verified"
        return {**row, "status": status, "note": note}


def apply_dividend_calendar(fundamentals_data, cache_dir="data", logger=None):
    """Attach individual declarations without changing annual DPS/yield.

    A calendar can omit earlier instalments. It cannot verify a full-year
    total, and book closure is not the ex-dividend date.
    """
    counts = {"verified": 0, "corrected": 0, "unverified": 0, "none": 0}
    try:
        dc = DividendCalendar(cache_dir=cache_dir)
        declarations = dc.fetch()
    except Exception as exc:
        declarations = {}
        if logger:
            logger.warning(f"Dividend calendar unavailable: {exc}")

    for symbol, fund in (fundamentals_data or {}).items():
        row = declarations.get(symbol.upper())
        fund["dividend_source"] = "TradingView (annual, unverified)"
        fund["dividend_status"] = "unverified"
        fund["dividend_note"] = "Annual DPS/yield from TradingView; calendar declarations are separate instalments."
        fund["declared_dividend"] = row.get("amount") if row else None
        fund["dividend_type"] = row.get("type") if row else None
        fund["dividend_book_closure"] = row.get("book_closure") if row else None
        fund["dividend_payment_date"] = row.get("payment_date") if row else None
        fund["declared_dividend_source"] = row.get("source") if row else None
        counts["unverified"] += 1
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
