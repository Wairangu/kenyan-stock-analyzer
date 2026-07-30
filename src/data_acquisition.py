"""
Data acquisition module for NSE (Nairobi Securities Exchange) stocks.

Supports multiple data sources with automatic fallback:
  1. TradingView (tvkit) — reliable historical OHLCV via NSEKE:SYMBOL
  2. NSE official PDF — daily price list from nse.co.ke (OCR)
  3. Yahoo Finance — historical data (bare ticker)

Data is cached as Parquet files in the cache directory.
"""

import os
import re
import io
import asyncio
import pandas as pd
import requests
import hashlib
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from logger import get_logger
from utils import retry, safe_float

load_dotenv()
logger = get_logger(__name__)

# ---- Constants ----
NSE_MARKET_STATS_URL = 'https://www.nse.co.ke/market-statistics/'
NSE_UPLOADS_BASE = 'https://www.nse.co.ke/wp-content/uploads/'

# Fix SSL on macOS
if os.environ.get('SSL_CERT_FILE') is None:
    try:
        import certifi
        os.environ['SSL_CERT_FILE'] = certifi.where()
    except ImportError:
        pass


class DataAcquisition:
    """Fetch stock data from NSE sources with caching and fallback."""

    def __init__(self, data_sources=None, cache_dir='data'):
        """
        Args:
            data_sources: Ordered list of source names to try.
                          Options: 'nse_pdf', 'yahoo_finance'.
            cache_dir: Directory for Parquet cache files.
        """
        self.data_sources = data_sources or ['nse_pdf', 'yahoo_finance']
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)
        # Clean old cache files from previous days
        self._clean_old_cache()
        # Cache parsed PDF data per run (avoid re-downloading/OCR for each stock)
        self._pdf_cache = None
        self._pdf_cache_date = None
        logger.info(
            f"DataAcquisition: sources={self.data_sources}, "
            f"cache={self.cache_dir}"
        )

    def _clean_old_cache(self):
        """Remove cache files from previous days."""
        today = datetime.now().strftime('%Y%m%d')
        removed = 0
        for fname in os.listdir(self.cache_dir):
            if fname.endswith('.parquet') and today not in fname:
                try:
                    os.remove(os.path.join(self.cache_dir, fname))
                    removed += 1
                except OSError:
                    pass
        if removed > 0:
            logger.info(f"Cleaned {removed} old cache files")

    # ---- Public API ----

    def fetch_stock_data(self, symbol, period='1y', interval='1d',
                         force_refresh=False):
        """
        Fetch historical stock data for a symbol.

        Args:
            symbol: Stock symbol (e.g., 'SCOM').
            period: Data period ('1d', '5d', '1mo', '3mo', '6mo', '1y', etc.).
            interval: Data interval ('1d', '1wk', '1mo').
            force_refresh: Skip cache and force fresh fetch.

        Returns:
            pandas.DataFrame with columns [open, high, low, close, volume],
            or None on failure.
        """
        # Check cache first (for daily data)
        if not force_refresh and interval == '1d':
            cached = self._load_from_cache(symbol)
            if cached is not None:
                logger.debug(f"Cache hit for {symbol}")
                return cached

        # Try each source in order
        for source in self.data_sources:
            logger.debug(f"Trying {source} for {symbol}")
            data = self._fetch_from_source(source, symbol, period, interval)
            if data is not None and not data.empty:
                # Cache the result
                if interval == '1d':
                    self._save_to_cache(symbol, data)
                return data
            logger.warning(f"  {source} failed for {symbol}")

        logger.error(f"All sources failed for {symbol}")
        return None

    def fetch_multiple_stocks(self, symbols, period='1y', interval='1d',
                              force_refresh=False):
        """
        Fetch data for multiple stocks. Failures are isolated per stock.

        Returns:
            dict: {symbol: DataFrame} for successfully fetched stocks.
        """
        data_dict = {}
        for symbol in symbols:
            try:
                data = self.fetch_stock_data(
                    symbol, period=period, interval=interval,
                    force_refresh=force_refresh,
                )
                if data is not None and not data.empty:
                    data_dict[symbol] = data
                    logger.info(f"  {symbol}: {len(data)} rows")
                else:
                    logger.warning(f"  {symbol}: NO DATA")
            except Exception as e:
                logger.error(f"  {symbol}: ERROR — {e}")
        return data_dict

    def get_all_stocks_from_pdf(self):
        """
        Download and parse the NSE PDF once, returning ALL stock symbols found.
        Useful for discovering the full market list.

        Returns:
            list: All stock symbols found in the PDF.
        """
        try:
            pdf_url = self._get_nse_pdf_url()
            if not pdf_url:
                return []
            all_stocks = self._parse_nse_pdf(pdf_url)
            if all_stocks:
                self._pdf_cache = all_stocks
                self._pdf_cache_date = datetime.now().date()
                return sorted(all_stocks.keys())
            return []
        except Exception as e:
            logger.warning(f"Could not get all stocks from PDF: {e}")
            return []

    def get_all_stocks_from_tradingview(self):
        """
        Get ALL stock symbols from TradingView's Kenya market scanner.
        This is the most reliable source — typically 55-60 stocks.

        Returns:
            list: All stock symbols found on TradingView for Kenya market.
        """
        try:
            import asyncio
            from tvkit import ScannerRequest, ScannerService, Market, ColumnSets
            from tvkit.api.scanner.models.scanner import ScannerOptions, SortConfig

            async def _scan():
                async with ScannerService() as scanner:
                    request = ScannerRequest(
                        columns=['name'],
                        options=ScannerOptions(filter_lang='pinescript_v5'),
                        range=(0, 100),
                        sort=SortConfig(sortBy='name', sortOrder='asc'),
                        preset='all_stocks',
                    )
                    result = await scanner.scan_market(Market.KENYA, request)
                    return [s.name for s in result.data if s.name]

            symbols = asyncio.run(_scan())
            logger.info(f"TradingView scanner found {len(symbols)} Kenya stocks")
            return symbols
        except Exception as e:
            logger.warning(f"TradingView scanner failed: {e}")
            return []

    def fetch_all_stocks(self, period='6mo', interval='1d', force_refresh=False):
        """
        Fetch ALL stocks from TradingView and NSE, enriching with historical data.

        Primary source: TradingView scanner (55-60 stocks)
        Fallback: NSE PDF (daily price list)

        Returns:
            dict: {symbol: DataFrame} for all successfully fetched stocks.
        """
        # First, try TradingView scanner for ALL stocks (most reliable)
        all_symbols = self.get_all_stocks_from_tradingview()

        # Fallback to NSE PDF if TradingView scanner fails
        if not all_symbols:
            all_symbols = self.get_all_stocks_from_pdf()

        # Last resort: configured watchlist
        if not all_symbols:
            logger.error("No stocks found from any source, falling back to configured list")
            from config import Config
            all_symbols = Config().stock_symbols

        logger.info(f"Fetching ALL {len(all_symbols)} stocks with historical data...")
        return self.fetch_multiple_stocks(
            all_symbols, period=period, interval=interval,
            force_refresh=force_refresh,
        )

    # ---- Source-specific fetchers ----

    def _fetch_from_source(self, source, symbol, period, interval):
        """Dispatch to the correct fetcher method."""
        if source == 'tradingview':
            return self._fetch_from_tradingview(symbol, period, interval)
        elif source == 'nse_pdf':
            return self._fetch_from_nse_pdf(symbol, period, interval)
        elif source == 'yahoo_finance':
            return self._fetch_from_yahoo(symbol, period, interval)
        else:
            logger.warning(f"Unknown source: {source}")
            return None

    def _fetch_from_tradingview(self, symbol, period='6mo', interval='1d'):
        """
        Fetch historical OHLCV data from TradingView via tvkit.

        Uses NSEKE:SYMBOL format (e.g., NSEKE:SCOM for Safaricom).
        Returns a DataFrame with actual multi-day historical data.
        """
        try:
            from tvkit import get_historical_data
        except ImportError:
            logger.warning("tvkit not installed. Run: pip install tvkit")
            return None

        tv_symbol = f"NSEKE:{symbol}"
        days = self._period_to_days(period)

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're in an async context, can't use asyncio.run()
                logger.debug(f"  Cannot run async in existing loop, skipping TV for {symbol}")
                return None
            data = asyncio.run(get_historical_data(tv_symbol, days=days))
        except RuntimeError:
            data = asyncio.run(get_historical_data(tv_symbol, days=days))

        if not data:
            logger.warning(f"  No TradingView data for {tv_symbol}")
            return None

        # Convert to DataFrame
        df = pd.DataFrame(
            [{
                'open': float(b.open),
                'high': float(b.high),
                'low': float(b.low),
                'close': float(b.close),
                'volume': int(b.volume) if b.volume else 0,
            } for b in data],
            index=pd.DatetimeIndex([
                pd.Timestamp(b.timestamp, unit='s') for b in data
            ])
        )
        df.index.name = 'Date'

        logger.info(
            f"  {symbol} from TradingView: {len(df)} days, "
            f"close={df['close'].iloc[-1]:.2f}"
        )
        return df

    def _period_to_days(self, period):
        """Convert a yfinance-style period string to number of days."""
        mapping = {
            '1d': 1, '5d': 5, '1mo': 30, '3mo': 90,
            '6mo': 180, '1y': 365, '2y': 730, '5y': 1825,
            '10y': 3650, 'ytd': 365, 'max': 5000,
        }
        # Handle numeric suffixes
        if period in mapping:
            return mapping[period]
        match = re.match(r'(\d+)([dmy])', period)
        if match:
            num = int(match.group(1))
            unit = match.group(2)
            if unit == 'd':
                return num
            elif unit == 'm':
                return num * 30
            elif unit == 'y':
                return num * 365
        return 180  # default 6 months

    def _fetch_from_nse_pdf(self, symbol, period='1y', interval='1d'):
        """
        Fetch current-day price data from the official NSE daily price list PDF.

        Downloads the latest PDF from nse.co.ke, OCRs it, and extracts
        the OHLCV row for the given symbol. Caches parsed PDF data in memory.
        """
        try:
            # Use cached PDF data if available (avoid re-downloading/OCR per stock)
            today = datetime.now().date()
            if self._pdf_cache is not None and self._pdf_cache_date == today:
                all_stocks = self._pdf_cache
                logger.debug(f"  Using cached PDF data ({len(all_stocks)} stocks)")
            else:
                # Get the PDF URL from the market statistics page
                pdf_url = self._get_nse_pdf_url()
                if not pdf_url:
                    return None

                # Download and OCR the PDF once
                all_stocks = self._parse_nse_pdf(pdf_url)
                # Always cache (even if empty) to avoid re-downloading
                self._pdf_cache = all_stocks or {}
                self._pdf_cache_date = today

            if not all_stocks:
                return None

            # Find the requested symbol
            symbol_upper = symbol.upper()
            if symbol_upper in all_stocks:
                row = all_stocks[symbol_upper]
                today_ts = pd.Timestamp.today().normalize()
                df = pd.DataFrame({
                    'open': [row['open']],
                    'high': [row['high']],
                    'low': [row['low']],
                    'close': [row['close']],
                    'volume': [row['volume']],
                }, index=[today_ts])
                df.index.name = 'Date'
                logger.info(
                    f"  {symbol} from NSE PDF: close={row['close']}, "
                    f"volume={row['volume']}"
                )
                return df
            else:
                logger.debug(f"  {symbol} not found in NSE PDF")

        except Exception as e:
            logger.warning(f"  NSE PDF error for {symbol}: {e}")

        return None

    @retry(max_attempts=2, backoff=2, exceptions=(requests.RequestException,))
    def _get_nse_pdf_url(self):
        """
        Scrape the NSE market statistics page to find the equity PDF URL.
        Returns the full PDF URL or None.
        """
        headers = {
            'User-Agent': (
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            ),
        }
        resp = requests.get(NSE_MARKET_STATS_URL, headers=headers, timeout=15)
        resp.raise_for_status()

        # Find the equity-specific PDF download link
        soup = BeautifulSoup(resp.text, 'html.parser')
        for a in soup.find_all('a', href=True):
            text = a.get_text(strip=True)
            href = a['href']
            if 'Equity Price List' in text and href.endswith('.pdf'):
                if href.startswith('/'):
                    return f"https://www.nse.co.ke{href}"
                return href

        # Fallback 1: Try any equity-related PDF
        for a in soup.find_all('a', href=True):
            href = a['href']
            if href.endswith('.pdf') and 'equity' in href.lower():
                if href.startswith('/'):
                    return f"https://www.nse.co.ke{href}"
                return href

        # Fallback 2: Try today's date pattern
        today = datetime.now()
        for fmt in ['%d-%b-%y', '%d-%B-%Y', '%d-%b-%Y']:
            fname = today.strftime(fmt).upper() + '.pdf'
            url = NSE_UPLOADS_BASE + fname
            try:
                r = requests.head(url, timeout=5)
                if r.status_code == 200:
                    return url
            except Exception:
                continue

        logger.warning("Could not find NSE PDF URL")
        return None

    def _parse_nse_pdf(self, pdf_url):
        """
        Download and OCR the NSE daily price list PDF.

        Returns:
            dict: {symbol: {open, high, low, close, volume}} or None.
        """
        try:
            from pdf2image import convert_from_bytes
            import pytesseract
        except ImportError as e:
            logger.warning(f"OCR dependencies missing: {e}")
            return None

        # Download PDF
        headers = {
            'User-Agent': (
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                'AppleWebKit/537.36'
            ),
        }
        resp = requests.get(pdf_url, headers=headers, timeout=30)
        resp.raise_for_status()

        logger.info(f"Downloaded NSE PDF: {len(resp.content)} bytes")

        # Convert PDF to images
        images = convert_from_bytes(resp.content, dpi=200)
        logger.info(f"PDF has {len(images)} pages")

        # OCR all pages
        all_text = ""
        for i, img in enumerate(images):
            text = pytesseract.image_to_string(img)
            all_text += text + "\n"

        # Parse the OCR'd text
        all_stocks = self._parse_nse_text(all_text)
        logger.info(f"Extracted {len(all_stocks)} stocks from NSE PDF")
        return all_stocks

    def _parse_nse_text(self, text):
        """
        Parse OCR'd text from the NSE price list PDF.

        The NSE price list lists stocks by company name, followed by
        par value, ISIN, and price data. We match known company names
        to ticker symbols.

        Returns:
            dict: {symbol: {open, high, low, close, volume}}
        """
        # Company name → ticker mapping
        name_to_ticker = {
            'Safaricom': 'SCOM',
            'Equity Group': 'EQTY',
            'Equity': 'EQTY',
            'KCB Group': 'KCB',
            'KCB': 'KCB',
            'East African Breweries': 'EABL',
            'EABL': 'EABL',
            'Co-operative Bank': 'COOP',
            'Coop': 'COOP',
            'ABSA Bank': 'ABSA',
            'ABSA': 'ABSA',
            'NCBA Group': 'NCBA',
            'NCBA': 'NCBA',
            'Standard Chartered': 'SCBK',
            'StanChart': 'SCBK',
            'Stanbic': 'SBIC',
            'I&M Group': 'IMH',
            'I&M': 'IMH',
            'IMH': 'IMH',
            'I and M': 'IMH',
            'Kenya Power': 'KPLC',
            'KPCL': 'KPLC',
            'KPLC': 'KPLC',
            'Kenya Power & Lighting': 'KPLC',
            'Diamond Trust': 'DTB',
            'DTB': 'DTB',
            'British American Tobacco': 'BAT',
            'BAT': 'BAT',
            'Kenya Airways': 'KQ',
            'KQ': 'KQ',
            'Jubilee': 'JUB',
            'Britam': 'BRIT',
            'Limuru Tea': 'LIMT',
            'Sasini': 'SASN',
            'Kakuzi': 'KUKZ',
            'Unga Group': 'UNGA',
            'BOC Kenya': 'BOC',
            'Carbacid': 'CARG',
            'Car & General': 'CARG',
            'C&G': 'CARG',
            'Nation Media': 'NMG',
            'NMG': 'NMG',
            'Williamson Tea': 'WTK',
            'Kapchorua Tea': 'KAPC',
            'Kenya Re': 'KNRE',
            'KEGN': 'KEGN',
            'KenGen': 'KEGN',
            'Umeme': 'UMME',
            'Centum': 'CTUM',
            'Home Afrika': 'HOME',
            'Eaagads': 'EGAD',
            'TPS': 'TPS',
            'Sameer': 'SCAN',
            'NIC': 'NIC',
            'National Bank': 'NBV',
            'Mumias': 'MSC',
            'Flame Tree': 'FLME',
            'Longhorn': 'LKL',
            'Express': 'XPR',
        }

        stocks = {}
        lines = text.strip().split('\n')

        for line in lines:
            line = line.strip()
            if len(line) < 30:
                continue
            if any(h in line.upper() for h in [
                'SECURITIES', 'TRADING', 'NSE 20', 'NSE 25',
                'NSE SHARE', 'MARKET', 'STATISTICS', 'EXCHANGE',
                'PAGE', 'JULY', 'JUL', 'Tel:', 'Fax:',
                'PREVIOUS', 'STATUS', 'AGRICULTURAL', 'BANKING',
                'TELECOMMUNICATION', 'MANUFACTURING', 'REAL ESTATE',
                'EXCHANGE TRADED', 'ENERGY', 'INSURANCE',
                'AUTOMOBILES', 'COMMERCIAL', 'INVESTMENT',
            ]):
                continue

            # Try to match a company name
            matched_ticker = None
            for name, ticker in name_to_ticker.items():
                if name.upper() in line.upper():
                    matched_ticker = ticker
                    break

            if not matched_ticker:
                continue

            # Extract all numeric values from the line
            words = line.split()
            nums = []
            for w in words:
                w_clean = w.strip('.,;:()[]{}\'"').replace(',', '')
                # Fix common OCR errors
                w_clean = re.sub(r'[oO]', '0', w_clean)
                w_clean = re.sub(r'[lI]', '1', w_clean)
                try:
                    nums.append(float(w_clean))
                except ValueError:
                    pass

            if len(nums) >= 4:
                # Filter: volume is typically the largest number
                # Prices range from ~1 to ~10000
                prices = [n for n in nums if 0.5 < n < 20000]
                if len(prices) >= 4:
                    # Use last value as volume (if it's large)
                    raw_volume = int(nums[-1])
                    volume = raw_volume if raw_volume > 100 else 0

                    # Take the 4 price values closest to the end
                    price_vals = [n for n in nums[:-1] if 0.5 < n < 20000]
                    if len(price_vals) >= 4:
                        last_four = price_vals[-4:]
                        stocks[matched_ticker] = {
                            'open': round(last_four[0], 2),
                            'high': round(max(last_four), 2),
                            'low': round(min(last_four), 2),
                            'close': round(last_four[-2], 2),
                            'volume': volume,
                        }

        return stocks

    def _fetch_from_yahoo(self, symbol, period='1y', interval='1d'):
        """
        Fetch data from Yahoo Finance using .NR suffix for NSE stocks.
        Handles pandas 3.x MultiIndex columns.
        """
        try:
            import yfinance as yf
        except ImportError:
            logger.warning("yfinance not installed")
            return None

        yahoo_symbol = f"{symbol}.NR"
        logger.debug(f"Fetching {yahoo_symbol} from Yahoo Finance")

        try:
            data = yf.download(
                yahoo_symbol,
                period=period,
                interval=interval,
                progress=False,
                timeout=30,
            )
            if data.empty:
                # Try without .NR suffix
                logger.debug(f"  {yahoo_symbol} empty, trying {symbol}")
                data = yf.download(
                    symbol, period=period, interval=interval,
                    progress=False, timeout=30,
                )

            if data.empty:
                return None

            # Handle MultiIndex columns (pandas 3.x / yfinance)
            if isinstance(data.columns, pd.MultiIndex):
                # Flatten: take the first level (Price), drop Ticker level
                data.columns = data.columns.get_level_values(0)

            # Normalize column names to lowercase
            col_map = {
                'Open': 'open', 'High': 'high', 'Low': 'low',
                'Close': 'close', 'Volume': 'volume',
            }
            data = data.rename(columns=col_map)
            required = ['open', 'high', 'low', 'close', 'volume']
            available = [c for c in required if c in data.columns]
            if len(available) < 4:
                logger.warning(f"  Missing columns for {symbol}: {data.columns.tolist()}")
                return None
            data = data[available]
            logger.info(f"  {symbol} from Yahoo: {len(data)} rows")
            return data

        except Exception as e:
            logger.warning(f"  Yahoo Finance error for {symbol}: {e}")
            return None

    # ---- Caching ----

    def _cache_key(self, symbol):
        """Generate a cache key for today's date."""
        today = datetime.now().strftime('%Y%m%d')
        h = hashlib.md5(symbol.encode()).hexdigest()[:8]
        return f"{symbol}_{today}_{h}"

    def _save_to_cache(self, symbol, data):
        """Save DataFrame to Parquet cache."""
        try:
            path = os.path.join(
                self.cache_dir, f"{self._cache_key(symbol)}.parquet"
            )
            data.to_parquet(path)
            logger.debug(f"  Cached {symbol} → {path}")
        except Exception as e:
            logger.debug(f"  Cache write error: {e}")

    def _load_from_cache(self, symbol):
        """Load DataFrame from Parquet cache if fresh (today's data)."""
        try:
            prefix = f"{symbol}_{datetime.now().strftime('%Y%m%d')}_"
            for fname in os.listdir(self.cache_dir):
                if fname.startswith(prefix) and fname.endswith('.parquet'):
                    path = os.path.join(self.cache_dir, fname)
                    # Check file freshness (must be from today)
                    mtime = datetime.fromtimestamp(os.path.getmtime(path))
                    if mtime.date() == datetime.now().date():
                        return pd.read_parquet(path)
        except Exception as e:
            logger.debug(f"  Cache read error: {e}")
        return None


# ---- Test ----
if __name__ == "__main__":
    from logger import setup_logging
    setup_logging()
    logger = get_logger(__name__)

    da = DataAcquisition(data_sources=['nse_scraper', 'yahoo_finance'])
    test_symbols = ['SCOM', 'EQTY', 'KCB', 'EABL']

    print("\nFetching test data...")
    data = da.fetch_multiple_stocks(test_symbols, period='5d')

    for sym, df in data.items():
        print(f"\n{sym}: {df.shape[0]} rows")
        print(df.tail(2))