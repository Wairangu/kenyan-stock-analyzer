"""
Market Pulse — context that sits around the NSE trading day.

Sources (every one probed and verified before shipping):
  - Google News RSS       — NSE-related headlines, no auth
  - Central Bank of Kenya — CBR & inflation snippet scraped from cbk homepage
  - yfinance              — Brent + WTI oil (BZ=F, CL=F)
  - tvkit                 — JSE Top 40 / NGX 30 / EGX 30 for African peer comparison
  - open.er-api.com       — USD/EUR/GBP → KES

Every function fails safe (returns None or an empty container on error) so a
network hiccup or a source outage cannot break the dashboard.

We deliberately do NOT auto-classify headlines as positive/negative — free
sentiment analysis on financial short text is unreliable enough to be
dangerous when money is involved. Headlines are shown neutral with date +
publisher and the user reads them.
"""

import asyncio
import datetime as dt
import re
from html import unescape

import requests

from logger import get_logger
from utils import http_get

logger = get_logger(__name__)

_UA = {"User-Agent": (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)}


# --------------------------------------------------------------------------
# News (Google News RSS — no auth, verified: ~100 items/query)
# --------------------------------------------------------------------------

NEWS_QUERIES = [
    ("NSE / Kenyan Stocks", "Nairobi Securities Exchange OR NSE Kenya stocks"),
    ("Kenyan Banking",      "Kenya banking sector OR KCB OR Equity Bank OR NCBA"),
    ("Central Bank of Kenya", "Central Bank of Kenya CBR OR monetary policy Kenya"),
    ("Kenyan Economy",      "Kenya economy OR inflation Kenya OR treasury bill Kenya"),
    ("Oil / Global",        "Brent oil price OR WTI oil"),
]


def fetch_news(max_items_per_topic: int = 8) -> list[dict]:
    """
    Fetch NSE-relevant news from Google News RSS. Returns a list of dicts:
        {topic, title, url, source, published_utc}
    """
    out: list[dict] = []
    for topic, query in NEWS_QUERIES:
        try:
            url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=en-KE&gl=KE&ceid=KE:en"
            r = http_get(url, headers=_UA)
            if r is None or r.status_code != 200:
                continue
            items = re.findall(r"<item>(.*?)</item>", r.text, re.S)
            for raw in items[:max_items_per_topic]:
                title_m = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", raw, re.S)
                link_m = re.search(r"<link>(.*?)</link>", raw, re.S)
                date_m = re.search(r"<pubDate>(.*?)</pubDate>", raw, re.S)
                src_m = re.search(r"<source[^>]*>(.*?)</source>", raw, re.S)
                if not (title_m and link_m and date_m):
                    continue
                title = unescape((title_m.group(1) or title_m.group(2) or "").strip())
                # Google prefixes titles with " - Source"; strip it
                title = re.sub(r"\s*-\s*[^-]+$", "", title).strip()
                out.append({
                    "topic": topic,
                    "title": title,
                    "url": link_m.group(1).strip(),
                    "source": unescape(src_m.group(1).strip()) if src_m else "",
                    "published_utc": date_m.group(1).strip(),
                })
        except Exception as e:
            logger.debug(f"News fetch failed for '{topic}': {e}")
    # Sort newest first (best-effort — Google's dates are RFC-822)
    def _parse(x):
        try:
            return dt.datetime.strptime(x["published_utc"], "%a, %d %b %Y %H:%M:%S %Z")
        except Exception:
            try:
                return dt.datetime.strptime(x["published_utc"][:25], "%a, %d %b %Y %H:%M:%S")
            except Exception:
                return dt.datetime.min
    out.sort(key=_parse, reverse=True)
    logger.info(f"Market Pulse — news: {len(out)} headlines across {len(NEWS_QUERIES)} topics")
    return out


# --------------------------------------------------------------------------
# Central Bank of Kenya — CBR + inflation snippet (verified: parseable)
# --------------------------------------------------------------------------

def fetch_cbk() -> dict:
    """
    Return {cbr_pct, cbr_note, inflation_note, source_url}. Values may be None.
    """
    url = "https://www.centralbank.go.ke/"
    out = {"cbr_pct": None, "cbr_note": None, "inflation_note": None, "source_url": url}
    try:
        r = http_get(url, headers=_UA)
        if r is None or r.status_code != 200:
            logger.warning("Market Pulse — CBK unavailable, section will be empty")
            return out
        t = r.text
        # CBR: e.g. "MPC retains the CBR at 8.75 percent"
        m = re.search(
            r"(?:MPC[^<]{0,80}(?:retains|raises|lowers|maintains)|CBR\s+at)\s*(?:the\s+CBR\s+)?"
            r"(?:at\s+)?(\d+(?:\.\d+)?)\s*percent",
            t, re.I,
        )
        if m:
            out["cbr_pct"] = float(m.group(1))
        # Longer narrative snippet
        m2 = re.search(r"MPC[^<]{20,240}percent[^<]{0,80}", t, re.I)
        if m2:
            out["cbr_note"] = m2.group(0).strip()
        # Inflation narrative
        m3 = re.search(r"[Ii]nflation[^<]{20,200}percent[^<]{0,60}", t)
        if m3:
            out["inflation_note"] = m3.group(0).strip()
        logger.info(f"Market Pulse — CBK: CBR={out['cbr_pct']}%")
    except Exception as e:
        logger.warning(f"Market Pulse — CBK fetch failed: {e}")
    return out


# --------------------------------------------------------------------------
# Oil (yfinance — Brent + WTI, verified)
# --------------------------------------------------------------------------

def fetch_oil() -> list[dict]:
    """
    Return [{symbol, name, price_usd, change_1w_pct}] for Brent & WTI.
    """
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        return []
    out: list[dict] = []
    for sym, name in [("BZ=F", "Brent"), ("CL=F", "WTI")]:
        try:
            d = yf.download(sym, period="7d", interval="1d", progress=False,
                            timeout=8, auto_adjust=False)
            if d.empty:
                continue
            if isinstance(d.columns, pd.MultiIndex):
                d.columns = d.columns.get_level_values(0)
            close = float(d["Close"].iloc[-1])
            first = float(d["Close"].iloc[0])
            chg = (close - first) / first * 100.0 if first else None
            out.append({
                "symbol": sym, "name": name,
                "price_usd": round(close, 2),
                "change_1w_pct": round(chg, 2) if chg is not None else None,
            })
        except Exception as e:
            logger.debug(f"Oil {sym} fetch failed: {e}")
    logger.info(f"Market Pulse — oil: {len(out)} prices")
    return out


# --------------------------------------------------------------------------
# African indices comparison (tvkit — verified: JSE J203, NGX30, EGX30)
# --------------------------------------------------------------------------

AFRICAN_INDICES = [
    ("JSE:J203",  "🇿🇦 JSE Top 40",  "South Africa"),
    ("NSENG:NGX30", "🇳🇬 NGX 30",     "Nigeria"),
    ("EGX:EGX30", "🇪🇬 EGX 30",     "Egypt"),
]


def fetch_african_indices() -> list[dict]:
    """
    Return [{symbol, name, country, price, change_1d_pct, change_1w_pct}] for
    each verified African index. Kenya is added by the caller from our own
    stock data (more honest than a broken TradingView symbol).
    """
    try:
        from tvkit import get_historical_data
    except ImportError:
        return []

    async def _one(sym, name, country):
        try:
            data = await get_historical_data(sym, days=7)
            if not data:
                return None
            last = float(data[-1].close)
            first = float(data[0].close)
            chg_1w = (last - first) / first * 100.0 if first else None
            chg_1d = None
            if len(data) >= 2:
                prev = float(data[-2].close)
                if prev:
                    chg_1d = (last - prev) / prev * 100.0
            return {
                "symbol": sym, "name": name, "country": country,
                "price": round(last, 2),
                "change_1d_pct": round(chg_1d, 2) if chg_1d is not None else None,
                "change_1w_pct": round(chg_1w, 2) if chg_1w is not None else None,
            }
        except Exception as e:
            logger.debug(f"African index {sym} failed: {e}")
            return None

    async def _all():
        return await asyncio.gather(*[_one(s, n, c) for s, n, c in AFRICAN_INDICES])

    try:
        results = asyncio.run(_all())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        results = loop.run_until_complete(_all())
        loop.close()
    out = [r for r in results if r]
    logger.info(f"Market Pulse — African indices: {len(out)} of {len(AFRICAN_INDICES)} available")
    return out


def compute_kenya_pulse(analysis_results: dict) -> dict | None:
    """
    Build a Kenya "index-equivalent" from our own analyzed NSE stocks:
    average daily change across stocks (like a naive equal-weight NSE).
    More honest than a broken TradingView index symbol.
    """
    if not analysis_results:
        return None
    changes = [r.get("daily_change_pct") for r in analysis_results.values()
               if r and r.get("daily_change_pct") is not None]
    if not changes:
        return None
    avg = sum(changes) / len(changes)
    return {
        "symbol": "NSE-KE",
        "name": "🇰🇪 NSE (equal-weight avg)",
        "country": "Kenya",
        "price": None,
        "change_1d_pct": round(avg, 2),
        "change_1w_pct": None,
        "note": f"Average daily change across {len(changes)} NSE stocks (from our own data — no reliable free index symbol for NSE 20 today)",
    }


# --------------------------------------------------------------------------
# FX pulse — USD/EUR/GBP → KES (extends the existing FX fetch)
# --------------------------------------------------------------------------

def fetch_fx_pulse() -> list[dict]:
    """
    Return [{code, name, rate_kes, updated}] for the majors vs KES.
    """
    try:
        resp = http_get("https://open.er-api.com/v6/latest/USD", headers=_UA)
        if resp is None or resp.status_code != 200:
            logger.warning("Market Pulse — FX unavailable")
            return []
        r = resp.json()
        kes_per_usd = r.get("rates", {}).get("KES")
        eur_per_usd = r.get("rates", {}).get("EUR")
        gbp_per_usd = r.get("rates", {}).get("GBP")
        updated = r.get("time_last_update_utc", "")
        if not kes_per_usd:
            return []
        rows = [{"code": "USD", "name": "US Dollar", "rate_kes": round(kes_per_usd, 2), "updated": updated}]
        if eur_per_usd:
            rows.append({"code": "EUR", "name": "Euro",
                         "rate_kes": round(kes_per_usd / eur_per_usd, 2), "updated": updated})
        if gbp_per_usd:
            rows.append({"code": "GBP", "name": "British Pound",
                         "rate_kes": round(kes_per_usd / gbp_per_usd, 2), "updated": updated})
        logger.info(f"Market Pulse — FX: {len(rows)} pairs vs KES")
        return rows
    except Exception as e:
        logger.warning(f"Market Pulse — FX fetch failed: {e}")
        return []


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------

def load_all(analysis_results: dict | None = None) -> dict:
    """
    Fetch every component in one call. Every field is optional; the page
    renders whatever came back and shows a note for anything missing.
    """
    kenya = compute_kenya_pulse(analysis_results or {})
    african = fetch_african_indices()
    if kenya:
        african = [kenya] + african
    return {
        "news": fetch_news(),
        "cbk": fetch_cbk(),
        "oil": fetch_oil(),
        "african_indices": african,
        "fx": fetch_fx_pulse(),
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M EAT"),
    }


# ---- Smoke test ----
if __name__ == "__main__":
    from logger import setup_logging
    setup_logging()
    p = load_all()
    print(f"news         : {len(p['news'])} headlines")
    print(f"cbk          : CBR={p['cbk']['cbr_pct']}%")
    print(f"oil          : {p['oil']}")
    print(f"african idx  : {[i['name'] + ' ' + str(i.get('change_1d_pct')) + '%' for i in p['african_indices']]}")
    print(f"fx           : {p['fx']}")
