"""
Fundamental analysis module — fetches valuation, profitability, growth,
and financial health metrics from TradingView for all NSE-listed stocks.

Data source: TradingView scanner (tvkit) — Market.KENYA, COMPREHENSIVE_FULL columns.
All values come directly from TradingView; nothing is guessed or estimated.

Metrics fetched (100% from TradingView, no hypotheses):
  - Market Cap, P/E (TTM), PEG, EPS (TTM), EPS Growth (YoY)
  - Revenue Growth (YoY), Revenue (TTM), Net Income (TTM)
  - Gross Margin, Operating Margin, Net Margin
  - ROE, ROA, ROIC
  - Debt/Equity, Current Ratio, Quick Ratio
  - Free Cash Flow (TTM), FCF Margin
  - Dividend Yield, Payout Ratio
  - Enterprise Value, EV/Revenue, EV/EBITDA
  - Price/Book, Price/Sales
  - Sector classification
  - Analyst recommendation

Also provides plain-English explanations of every metric for reports.
"""

import asyncio
import json
import os
import hashlib
from datetime import datetime, timedelta
from typing import Optional

from logger import get_logger

logger = get_logger(__name__)

# Metric explanations in plain English
METRIC_EXPLANATIONS = {
    "market_cap": (
        "Market Capitalisation — the total value of all the company's shares "
        "added together. It tells you how big the company is. Think of it as "
        "the price tag to buy the entire company."
    ),
    "pe_ratio": (
        "Price-to-Earnings (P/E) — how many shillings you pay for every "
        "1 shilling of profit the company makes. A high P/E (e.g. 30+) means "
        "investors expect fast growth. A low P/E (e.g. <10) may mean the "
        "stock is cheap or the company has problems. Always compare P/E to "
        "other companies in the same sector."
    ),
    "peg_ratio": (
        "Price/Earnings to Growth (PEG) — P/E divided by the earnings growth "
        "rate. This adjusts the P/E to account for how fast the company is "
        "growing. PEG < 1.0 suggests the stock may be undervalued relative "
        "to its growth. PEG > 1.0 suggests it may be overvalued."
    ),
    "eps": (
        "Earnings Per Share (EPS) — the company's profit divided by the "
        "number of shares. Higher EPS means the company is making more "
        "money per share you own. This is the 'E' in P/E ratio."
    ),
    "eps_growth": (
        "EPS Growth (Year-over-Year) — how much earnings per share have "
        "grown compared to the same period last year. Positive growth means "
        "the company is becoming more profitable. 10%+ is solid; 20%+ is "
        "excellent. Negative growth is a warning sign."
    ),
    "revenue_growth": (
        "Revenue Growth (Year-over-Year) — how much total sales have grown "
        "compared to last year. Revenue is the 'top line' — money coming in "
        "before any costs. Growing revenue means the business is expanding. "
        "Falling revenue may mean losing market share."
    ),
    "gross_margin": (
        "Gross Margin — the percentage of revenue left after paying for the "
        "direct costs of making the product or service. Higher is better. "
        "A 50% gross margin means for every 100 KES in sales, 50 KES is "
        "left after direct costs to cover other expenses."
    ),
    "operating_margin": (
        "Operating Margin — the percentage of revenue left after paying both "
        "direct costs AND operating expenses (salaries, rent, marketing, "
        "etc.). This shows how profitable the core business is. Higher = "
        "more efficient operations."
    ),
    "net_margin": (
        "Net Profit Margin — the percentage of revenue that becomes actual "
        "profit after ALL costs, taxes, and interest. This is the 'bottom "
        "line.' A 20% net margin means 20 KES of every 100 KES in sales "
        "becomes profit."
    ),
    "roe": (
        "Return on Equity (ROE) — how much profit the company generates "
        "with the money shareholders have invested. ROE of 15% means the "
        "company makes 15 KES in profit for every 100 KES of shareholder "
        "equity. 15-20% is generally good. Above 20% is excellent but "
        "check if it is sustainable."
    ),
    "roic": (
        "Return on Invested Capital (ROIC) — how efficiently the company "
        "uses ALL the money invested in it (both from shareholders and "
        "lenders) to generate profit. Higher than the company's cost of "
        "capital means it is creating value. ROIC > 15% is strong."
    ),
    "debt_to_equity": (
        "Debt-to-Equity (D/E) — compares what the company owes (debt) to "
        "what shareholders own (equity). A D/E of 1.0 means debt equals "
        "equity. Below 0.5 is conservative (low risk). Above 2.0 is "
        "aggressive (high risk). Banks naturally have higher ratios."
    ),
    "current_ratio": (
        "Current Ratio — measures if the company can pay its short-term "
        "bills (due within 1 year) using its short-term assets. Above 1.0 "
        "means they can cover their bills. Below 1.0 is a red flag. "
        "Too high (>3) may mean idle cash not being invested."
    ),
    "free_cash_flow": (
        "Free Cash Flow (FCF) — the actual cash the business generates "
        "after paying for buildings, equipment, and other capital needs. "
        "This is the money available to pay dividends, buy back shares, "
        "or invest in growth. Positive FCF is healthy."
    ),
    "dividend_yield": (
        "Dividend Yield — the annual dividend payment as a percentage of "
        "the current share price. A 5% yield means you get 5 KES per year "
        "for every 100 KES invested. High yields are attractive but check "
        "if the dividend is sustainable."
    ),
    "rsi": (
        "Relative Strength Index (RSI) — a momentum indicator from 0-100 "
        "that measures how fast and how much the price is moving. Above 70 "
        "= 'overbought' (price may have risen too fast and could pull "
        "back). Below 30 = 'oversold' (price may have fallen too fast and "
        "could bounce). Between 30-70 is neutral territory."
    ),
    "recommendation": (
        "Analyst Recommendation — the average rating from analysts covering "
        "the stock, on a scale of 1 (Strong Buy) to 5 (Strong Sell). "
        "1.0-1.5 = Strong Buy, 1.5-2.5 = Buy, 2.5-3.5 = Hold, "
        "3.5-4.5 = Sell, 4.5-5.0 = Strong Sell."
    ),
}

# Sector mapping for display
SECTOR_DISPLAY = {
    "Finance": "🏦 Finance & Banking",
    "Communications": "📡 Communications",
    "Utilities": "⚡ Utilities & Energy",
    "Consumer Non-Durables": "🛒 Consumer Goods",
    "Consumer Durables": "🏠 Consumer Durables",
    "Consumer Services": "🍽️ Consumer Services",
    "Process Industries": "🏭 Manufacturing & Processing",
    "Producer Manufacturing": "🔧 Industrial Manufacturing",
    "Distribution Services": "🚚 Distribution & Logistics",
    "Retail Trade": "🛍️ Retail",
    "Transportation": "✈️ Transportation",
    "Commercial Services": "💼 Commercial Services",
    "Industrial Services": "🏗️ Industrial Services",
    "Non-Energy Minerals": "⛏️ Mining & Minerals",
    "Health Services": "🏥 Healthcare",
    "Technology Services": "💻 Technology",
    "Electronic Technology": "🔌 Electronic Technology",
    "Health Technology": "🧬 Health Technology",
    "Energy Minerals": "🛢️ Energy",
}


class FundamentalAnalysis:
    """Fetches and structures fundamental data from TradingView."""

    def __init__(self, cache_dir: str = "data"):
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)
        self._cache: dict = {}
        self._cache_date: Optional[str] = None
        # Clean old fundamental cache files from previous days
        self._clean_old_cache()

    def _clean_old_cache(self):
        """Remove fundamental cache files from previous days."""
        today = datetime.now().strftime('%Y%m%d')
        removed = 0
        for fname in os.listdir(self.cache_dir):
            if fname.startswith('fundamentals_') and today not in fname:
                try:
                    os.remove(os.path.join(self.cache_dir, fname))
                    removed += 1
                except OSError:
                    pass
        if removed > 0:
            logger.info(f"Cleaned {removed} old fundamental cache files")

    def fetch_all_fundamentals(self, force_refresh: bool = False) -> dict:
        """
        Fetch fundamental data for ALL NSE stocks from TradingView.

        Args:
            force_refresh: Skip cache and fetch fresh data.

        Returns:
            dict: {symbol: {metric: value, ...}} for all 57+ NSE stocks.
        """
        today = datetime.now().strftime("%Y%m%d")

        # Check cache
        if not force_refresh:
            cached = self._load_from_cache(today)
            if cached:
                logger.info(f"Using cached fundamental data ({len(cached)} stocks)")
                return cached

        # Fetch fresh data from TradingView
        logger.info("Fetching fundamental data from TradingView for all NSE stocks...")
        try:
            data = asyncio.run(self._scan_kenya_market())
        except RuntimeError as e:
            logger.error(f"Async error: {e}")
            # Try fallback
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                data = loop.run_until_complete(self._scan_kenya_market())
                loop.close()
            except Exception as e2:
                logger.error(f"Fundamental fetch failed: {e2}")
                return {}

        if data:
            self._save_to_cache(today, data)
            logger.info(f"Fetched fundamental data for {len(data)} NSE stocks")
        else:
            logger.warning("No fundamental data retrieved")

        return data

    @staticmethod
    def _ts_to_date(ts):
        """Convert a Unix timestamp (seconds) to a YYYY-MM-DD string, or None."""
        if ts is None:
            return None
        try:
            return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
        except (ValueError, TypeError, OSError):
            return None

    async def _scan_kenya_market(self) -> dict:
        """Scan Kenya market on TradingView for all stocks with full fundamentals."""
        try:
            from tvkit import ScannerRequest, ScannerService, Market, ColumnSets
            from tvkit.api.scanner.models.scanner import ScannerOptions, SortConfig
        except ImportError:
            logger.error("tvkit not installed. Run: pip install tvkit")
            return {}

        # Extra columns not in COMPREHENSIVE_FULL that we surface in reports.
        extra_columns = [
            'industry',
            'dividend_ex_date_upcoming', 'dividend_ex_date_recent',
            'earnings_release_next_date', 'earnings_release_date',
            'price_52_week_high', 'price_52_week_low', 'book_value_per_share_fq',
            'average_volume_10d_calc', 'average_volume_30d_calc', 'Value.Traded',
        ]
        columns = list(ColumnSets.COMPREHENSIVE_FULL) + [
            c for c in extra_columns if c not in ColumnSets.COMPREHENSIVE_FULL
        ]

        async with ScannerService() as scanner:
            request = ScannerRequest(
                columns=columns,
                options=ScannerOptions(filter_lang='pinescript_v5'),
                range=(0, 100),
                sort=SortConfig(sortBy='name', sortOrder='asc'),
                preset='all_stocks',
            )
            result = await scanner.scan_market(Market.KENYA, request)
            logger.info(f"TradingView returned {result.total_count} Kenya stocks")

            data = {}
            for stock in result.data:
                try:
                    symbol = stock.name
                    if not symbol:
                        continue

                    # Extract all fundamental metrics
                    d = stock.model_extra  # extra fields not in base model
                    fundamentals = {
                        # === Valuation ===
                        "market_cap": getattr(stock, 'market_cap_basic', None),
                        "pe_ratio": getattr(stock, 'price_earnings_ttm', None),
                        "peg_ratio": d.get('price_earnings_growth_ttm'),
                        "price_to_book": d.get('price_book_fq'),
                        "price_to_sales": d.get('price_sales_current'),
                        "enterprise_value": d.get('enterprise_value_current'),
                        "ev_to_revenue": d.get('enterprise_value_to_revenue_ttm'),
                        "ev_to_ebitda": d.get('enterprise_value_ebitda_ttm'),

                        # === Earnings ===
                        "eps_ttm": getattr(stock, 'earnings_per_share_diluted_ttm', None),
                        "eps_growth_yoy": getattr(stock, 'earnings_per_share_diluted_yoy_growth_ttm', None),

                        # === Revenue & Income ===
                        "revenue_ttm": d.get('total_revenue_ttm'),
                        "revenue_growth_yoy": d.get('total_revenue_yoy_growth_ttm'),
                        "net_income_ttm": d.get('net_income_ttm'),
                        "operating_income_ttm": d.get('oper_income_ttm'),
                        "ebitda_ttm": d.get('ebitda_ttm'),
                        "gross_profit_ttm": d.get('gross_profit_ttm'),

                        # === Margins (as percentages) ===
                        "gross_margin": d.get('gross_margin_ttm'),
                        "operating_margin": d.get('operating_margin_ttm'),
                        "net_margin": d.get('net_margin_ttm'),
                        "fcf_margin": d.get('free_cash_flow_margin_ttm'),

                        # === Returns ===
                        "roe": d.get('return_on_equity_fq'),
                        "roa": d.get('return_on_assets_fq'),
                        "roic": d.get('return_on_invested_capital_fq'),

                        # === Financial Health ===
                        "debt_to_equity": d.get('debt_to_equity_fq'),
                        "current_ratio": d.get('current_ratio_fq'),
                        "quick_ratio": d.get('quick_ratio_fq'),
                        "total_assets": d.get('total_assets_fq'),
                        "total_debt": d.get('total_debt_fq'),
                        "net_debt": d.get('net_debt_fq'),
                        "total_equity": d.get('total_equity_fq'),

                        # === Cash Flow ===
                        "free_cash_flow_ttm": d.get('free_cash_flow_ttm'),
                        "operating_cash_flow_ttm": d.get('cash_f_operating_activities_ttm'),
                        "capital_expenditures_ttm": d.get('capital_expenditures_ttm'),

                        # === Dividends ===
                        "dividend_yield": next((v for v in (
                            getattr(stock, 'dividends_yield_current', None),
                            d.get('dividends_yield_current'), d.get('dividends_yield'))
                            if v is not None), None),
                        "dividend_payout_ratio": d.get('dividend_payout_ratio_ttm'),
                        "dps_fy": d.get('dps_common_stock_prim_issue_fy'),
                        "dividend_ex_date": self._ts_to_date(
                            d.get('dividend_ex_date_upcoming') or d.get('dividend_ex_date_recent')
                        ),
                        "dividend_ex_date_is_upcoming": d.get('dividend_ex_date_upcoming') is not None,

                        # === Earnings calendar ===
                        "earnings_next_date": self._ts_to_date(d.get('earnings_release_next_date')),
                        "earnings_last_date": self._ts_to_date(d.get('earnings_release_date')),

                        # === 52-week range & book value ===
                        "price_52w_high": d.get('price_52_week_high'),
                        "price_52w_low": d.get('price_52_week_low'),
                        "book_value_per_share": d.get('book_value_per_share_fq'),

                        # === Liquidity ===
                        "avg_volume_10d": d.get('average_volume_10d_calc'),
                        "avg_volume_30d": d.get('average_volume_30d_calc'),
                        "value_traded": d.get('Value.Traded'),

                        # === Classification ===
                        "sector": getattr(stock, 'sector', None) or d.get('sector', 'Unknown'),
                        "industry": getattr(stock, 'industry', None) or d.get('industry'),
                        "market": d.get('market', 'kenya'),
                        "currency": getattr(stock, 'currency', 'KES'),

                        # === Price & Technical ===
                        "close": getattr(stock, 'close', None),
                        "change_pct": d.get('change'),
                        "rsi": d.get('RSI'),
                        "recommendation": getattr(stock, 'recommendation_mark', None),
                        # TradingView technical-rating gauge (Recommend.All),
                        # a value in [-1, 1]. Available for ALL stocks, unlike
                        # the analyst mark which only covers a handful.
                        "tech_rating": d.get('Recommend.All'),

                        # === Performance ===
                        "perf_1w": d.get('Perf.W'),
                        "perf_1m": d.get('Perf.1M'),
                        "perf_3m": d.get('Perf.3M'),
                        "perf_6m": d.get('Perf.6M'),
                        "perf_ytd": d.get('Perf.YTD'),
                        "perf_1y": d.get('Perf.Y'),
                        "perf_5y": d.get('Perf.5Y'),

                        # === Volume ===
                        "volume": getattr(stock, 'volume', None),
                        "relative_volume": d.get('relative_volume_10d_calc'),

                        # === Raw data for reference ===
                        "_data_date": datetime.now().strftime("%Y-%m-%d"),
                        "_data_source": "TradingView",
                    }

                    data[symbol] = fundamentals

                except Exception as e:
                    logger.warning(f"Error parsing fundamental data for {getattr(stock, 'name', '?')}: {e}")
                    continue

            return data

    def get_fundamentals_for_symbol(self, symbol: str, all_data: dict) -> dict:
        """
        Get fundamentals for a specific symbol.

        Args:
            symbol: Stock symbol (e.g., 'SCOM').
            all_data: Full fundamental data dict from fetch_all_fundamentals().

        Returns:
            dict with fundamental metrics, or empty dict if not found.
        """
        return all_data.get(symbol.upper(), {})

    def find_similar_stocks(self, symbol: str, all_data: dict, top_n: int = 5) -> list:
        """
        Find similar stocks based on sector and market cap proximity.

        If no same-sector peers exist, finds stocks with similar valuation
        (P/E, market cap, margins) across all sectors.

        Args:
            symbol: Reference stock symbol.
            all_data: Full fundamental data dict.
            top_n: Number of similar stocks to return.

        Returns:
            list of dicts with symbol, similarity reason, and key metrics.
        """
        target = all_data.get(symbol.upper(), {})
        if not target:
            return []

        target_sector = target.get('sector', '')
        target_mcap = target.get('market_cap', 0) or 0
        target_pe = target.get('pe_ratio')
        target_roe = target.get('roe')
        target_net_margin = target.get('net_margin')

        candidates = []
        has_sector_peers = False

        for sym, data in all_data.items():
            if sym == symbol.upper():
                continue
            if not data:
                continue

            score = 0
            reasons = []

            # Same sector = high similarity
            if data.get('sector') == target_sector:
                score += 5
                reasons.append("Same sector")
                has_sector_peers = True

            # Similar market cap (within 3x range)
            mcap = data.get('market_cap', 0) or 0
            if target_mcap > 0 and mcap > 0:
                ratio = max(target_mcap, mcap) / max(min(target_mcap, mcap), 1)
                if ratio < 1.5:
                    score += 3
                    reasons.append("Very similar market cap")
                elif ratio < 3:
                    score += 2
                    reasons.append("Similar market cap")
                elif ratio < 5:
                    score += 1
                    reasons.append("Comparable market cap")

            # Similar P/E range
            pe = data.get('pe_ratio')
            if target_pe and pe and target_pe > 0 and pe > 0:
                pe_ratio = max(target_pe, pe) / max(min(target_pe, pe), 0.01)
                if pe_ratio < 1.3:
                    score += 2
                    reasons.append("Similar P/E valuation")
                elif pe_ratio < 1.5:
                    score += 1
                    reasons.append("Comparable P/E")

            # Similar ROE
            roe = data.get('roe')
            if target_roe and roe and target_roe > 0 and roe > 0:
                roe_ratio = max(target_roe, roe) / max(min(target_roe, roe), 0.01)
                if roe_ratio < 1.5:
                    score += 1
                    reasons.append("Similar profitability")

            if score > 0:
                candidates.append({
                    'symbol': sym,
                    'score': score,
                    'reasons': reasons,
                    'sector': data.get('sector', ''),
                    'market_cap': mcap,
                    'pe_ratio': pe,
                    'roe': data.get('roe'),
                    'net_margin': data.get('net_margin'),
                    'revenue_growth': data.get('revenue_growth_yoy'),
                    'close': data.get('close'),
                })

        # Sort by score descending, return top N
        candidates.sort(key=lambda x: x['score'], reverse=True)
        return candidates[:top_n]

    def get_sector_peers(self, symbol: str, all_data: dict) -> list:
        """
        Get all stocks in the same sector for comparison.

        Returns:
            list of dicts with symbol and key metrics, sorted by market cap.
        """
        target = all_data.get(symbol.upper(), {})
        target_sector = target.get('sector', '')

        if not target_sector:
            return []

        peers = []
        for sym, data in all_data.items():
            if sym == symbol.upper():
                continue
            if data.get('sector') == target_sector:
                peers.append({
                    'symbol': sym,
                    'market_cap': data.get('market_cap'),
                    'pe_ratio': data.get('pe_ratio'),
                    'roe': data.get('roe'),
                    'net_margin': data.get('net_margin'),
                    'revenue_growth_yoy': data.get('revenue_growth_yoy'),
                    'close': data.get('close'),
                })

        peers.sort(key=lambda x: x['market_cap'] or 0, reverse=True)
        return peers

    # ---- Caching ----

    def _cache_path(self, date_str: str) -> str:
        return os.path.join(self.cache_dir, f"fundamentals_{date_str}.json")

    def _load_from_cache(self, date_str: str) -> dict:
        """Load cached fundamental data if fresh."""
        path = self._cache_path(date_str)
        if os.path.exists(path):
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(path))
                if mtime.date() == datetime.now().date():
                    with open(path, 'r') as f:
                        return json.load(f)
            except Exception as e:
                logger.debug(f"Cache read error: {e}")
        return {}

    def _save_to_cache(self, date_str: str, data: dict):
        """Save fundamental data to JSON cache."""
        path = self._cache_path(date_str)
        try:
            with open(path, 'w') as f:
                json.dump(data, f, indent=2, default=str)
            logger.debug(f"Cached fundamentals to {path}")
        except Exception as e:
            logger.debug(f"Cache write error: {e}")

    # ---- Formatting helpers for reports ----

    @staticmethod
    def fmt_value(value, fmt_type: str = "auto") -> str:
        """Format a metric value for display in reports."""
        if value is None:
            return "N/A"

        try:
            if fmt_type == "pct":
                # Already a percentage value (e.g., 22.5 for 22.5%)
                return f"{value:.2f}%"
            elif fmt_type == "ratio":
                return f"{value:.2f}"
            elif fmt_type == "currency_large":
                # Large KES values
                if abs(value) >= 1e12:
                    return f"KES {value / 1e12:.2f}T"
                elif abs(value) >= 1e9:
                    return f"KES {value / 1e9:.2f}B"
                elif abs(value) >= 1e6:
                    return f"KES {value / 1e6:.2f}M"
                else:
                    return f"KES {value:,.0f}"
            elif fmt_type == "currency":
                return f"KES {value:,.2f}"
            elif fmt_type == "mcap":
                if value >= 1e12:
                    return f"KES {value / 1e12:.2f} Trillion"
                elif value >= 1e9:
                    return f"KES {value / 1e9:.2f} Billion"
                elif value >= 1e6:
                    return f"KES {value / 1e6:.2f} Million"
                else:
                    return f"KES {value:,.0f}"
            else:
                # Auto-detect
                if isinstance(value, float):
                    return f"{value:.2f}"
                return str(value)
        except (ValueError, TypeError):
            return str(value) if value is not None else "N/A"

    @staticmethod
    def interpret_pe(pe: float) -> str:
        """Plain-English interpretation of P/E ratio."""
        if pe is None:
            return "No data available"
        if pe < 0:
            return "The company is currently unprofitable (negative earnings)"
        if pe < 10:
            return "Low valuation — the market is pricing this stock cheaply relative to its earnings. This could be a value opportunity OR a sign of problems ahead."
        if pe < 15:
            return "Fairly valued — reasonable price for the earnings the company generates"
        if pe < 20:
            return "Moderately valued — slightly above average, typical for growing companies"
        if pe < 30:
            return "Above-average valuation — investors expect good future growth"
        return "High valuation — the market expects very strong future growth. The stock may be expensive."

    @staticmethod
    def interpret_peg(peg: float) -> str:
        if peg is None:
            return "No data available"
        if peg < 0:
            return "Negative PEG — earnings are declining, which is a warning sign"
        if peg < 0.5:
            return "Very undervalued relative to growth — potentially a bargain"
        if peg < 1.0:
            return "Undervalued — the stock's P/E is lower than its growth rate"
        if peg < 1.5:
            return "Fairly valued — P/E is roughly in line with growth rate"
        if peg < 2.5:
            return "Slightly overvalued — P/E exceeds growth rate"
        return "Overvalued — the stock price is high relative to its earnings growth rate"

    @staticmethod
    def interpret_de(ratio: float) -> str:
        if ratio is None:
            return "No data available"
        if ratio < 0:
            return "Negative equity — the company has more liabilities than assets. HIGH RISK."
        if ratio < 0.3:
            return "Very conservative — the company uses very little debt. Low financial risk."
        if ratio < 0.7:
            return "Conservative — manageable debt levels. Low to moderate risk."
        if ratio < 1.5:
            return "Moderate leverage — the company uses a reasonable amount of debt"
        if ratio < 3.0:
            return "High leverage — significant debt relative to equity. Higher risk."
        return "Very high leverage — the company is heavily indebted. Proceed with caution."

    @staticmethod
    def interpret_rsi(rsi: float) -> str:
        if rsi is None:
            return "No data available"
        if rsi > 80:
            return "Strongly overbought — price has risen very fast, high risk of a pullback"
        if rsi > 70:
            return "Overbought — the stock may have risen too quickly and could correct"
        if rsi > 50:
            return "Bullish momentum — price is trending upward with moderate strength"
        if rsi > 30:
            return "Bearish momentum — price is trending downward with moderate weakness"
        if rsi > 20:
            return "Oversold — the stock may have fallen too far and could bounce back"
        return "Strongly oversold — extreme selling pressure, potential for a sharp reversal"

    @staticmethod
    def interpret_roe(roe: float) -> str:
        if roe is None:
            return "No data available"
        if roe < 0:
            return "Negative ROE — the company is destroying shareholder value"
        if roe < 5:
            return "Weak — the company generates very little profit from shareholder money"
        if roe < 10:
            return "Below average — acceptable but not impressive"
        if roe < 15:
            return "Average — decent returns on shareholder capital"
        if roe < 20:
            return "Good — the company efficiently turns shareholder money into profit"
        if roe < 30:
            return "Excellent — very efficient use of shareholder capital"
        return "Outstanding — extremely efficient. Verify this is sustainable."

    @staticmethod
    def interpret_recommendation(rec: float) -> str:
        if rec is None:
            return "No analyst coverage"
        if rec <= 1.5:
            return "Strong Buy — analysts are very bullish on this stock"
        if rec <= 2.5:
            return "Buy — analysts recommend purchasing this stock"
        if rec <= 3.5:
            return "Hold — analysts suggest neither buying nor selling"
        if rec <= 4.5:
            return "Sell — analysts recommend selling this stock"
        return "Strong Sell — analysts are very bearish on this stock"

    @staticmethod
    def signal_from_tech_rating(rating):
        """
        Map TradingView's technical-rating gauge (Recommend.All, a value in
        [-1, 1]) to a Buy/Sell label and a CSS class token.

        Uses TradingView's own standard thresholds:
          >   0.5  Strong Buy
          >   0.1  Buy
          >= -0.1  Neutral
          >= -0.5  Sell
          else     Strong Sell

        Returns a (label, css_class) tuple. css_class is one of
        strong_buy / buy / neutral / sell / strong_sell, or 'undefined'
        when no rating is available.
        """
        if rating is None:
            return ("N/A", "undefined")
        try:
            r = float(rating)
        except (ValueError, TypeError):
            return ("N/A", "undefined")
        import math
        if not math.isfinite(r) or not -1 <= r <= 1:
            return ("N/A", "undefined")
        if r > 0.5:
            return ("Strong Buy", "strong_buy")
        if r > 0.1:
            return ("Buy", "buy")
        if r >= -0.1:
            return ("Neutral", "neutral")
        if r >= -0.5:
            return ("Sell", "sell")
        return ("Strong Sell", "strong_sell")


# ---- Test ----
if __name__ == "__main__":
    from logger import setup_logging
    setup_logging()
    logger = get_logger(__name__)

    fa = FundamentalAnalysis()
    data = fa.fetch_all_fundamentals(force_refresh=True)

    if data:
        print(f"\nFetched fundamentals for {len(data)} stocks")
        # Show SCOM
        scom = data.get('SCOM', {})
        print("\n=== Safaricom (SCOM) Fundamentals ===")
        for k, v in sorted(scom.items()):
            if not k.startswith('_'):
                print(f"  {k}: {v}")

        # Find similar stocks to SCOM
        print("\n=== Stocks Similar to SCOM ===")
        similar = fa.find_similar_stocks('SCOM', data)
        for s in similar:
            print(f"  {s['symbol']}: score={s['score']}, reasons={s['reasons']}")

        # Sector peers
        print("\n=== SCOM Sector Peers ===")
        peers = fa.get_sector_peers('SCOM', data)
        for p in peers:
            print(f"  {p['symbol']}: mcap={p['market_cap']}, P/E={p['pe_ratio']}")

        print("\n=== P/E Interpretation ===")
        print(f"  SCOM P/E: {scom.get('pe_ratio')} → {fa.interpret_pe(scom.get('pe_ratio'))}")
    else:
        print("No data fetched")
