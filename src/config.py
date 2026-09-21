"""
Centralized configuration module.
Reads all settings from .env once at import time.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Config:
    """Centralized configuration for the Kenyan Stock Analyzer."""

    def __init__(self):
        # ---- Data source ----
        self.nse_data_source = os.getenv('NSE_DATA_SOURCE', 'nse_scraper')
        self.data_sources = [
            s.strip() for s in
            os.getenv('DATA_SOURCES', 'tradingview,nse_pdf,yahoo_finance').split(',')
        ]

        # ---- Cache ----
        self.cache_dir = str(
            PROJECT_ROOT / os.getenv('CACHE_DIR', 'data')
        )

        # ---- Analysis parameters ----
        self.rsi_period = int(os.getenv('RSI_PERIOD', '14'))
        self.rsi_overbought = int(os.getenv('RSI_OVERBOUGHT', '70'))
        self.rsi_oversold = int(os.getenv('RSI_OVERSOLD', '30'))
        self.macd_fast = int(os.getenv('MACD_FAST', '12'))
        self.macd_slow = int(os.getenv('MACD_SLOW', '26'))
        self.macd_signal = int(os.getenv('MACD_SIGNAL', '9'))
        self.bbands_period = int(os.getenv('BBANDS_PERIOD', '20'))
        self.bbands_std = float(os.getenv('BBANDS_STD', '2.0'))
        self.sma_short = int(os.getenv('SMA_SHORT', '20'))
        self.sma_long = int(os.getenv('SMA_LONG', '50'))
        self.ema_short = int(os.getenv('EMA_SHORT', '12'))
        self.ema_long = int(os.getenv('EMA_LONG', '26'))
        self.atr_period = int(os.getenv('ATR_PERIOD', '14'))
        self.stoch_k = int(os.getenv('STOCH_K', '14'))
        self.stoch_d = int(os.getenv('STOCH_D', '3'))
        self.volume_sma_period = int(os.getenv('VOLUME_SMA_PERIOD', '20'))

        # ---- Accuracy & enrichment ----
        self.enable_price_validation = (
            os.getenv('ENABLE_PRICE_VALIDATION', 'true').lower() == 'true'
        )
        self.price_disagree_threshold_pct = float(
            os.getenv('PRICE_DISAGREE_THRESHOLD_PCT', '1.0')
        )
        # Attach an independent quote without replacing the price series.
        self.enable_official_close = (
            os.getenv('ENABLE_OFFICIAL_CLOSE', 'true').lower() == 'true'
        )
        self.enable_scoring = (
            os.getenv('ENABLE_SCORING', 'true').lower() == 'true'
        )
        self.enable_history = (
            os.getenv('ENABLE_HISTORY', 'true').lower() == 'true'
        )
        self.enable_fx = (
            os.getenv('ENABLE_FX', 'true').lower() == 'true'
        )

        # ---- Report settings ----
        self.report_format = os.getenv('REPORT_FORMAT', 'html')
        self.report_directory = str(
            PROJECT_ROOT / os.getenv('REPORT_DIRECTORY', 'reports')
        )
        self.template_dir = str(
            PROJECT_ROOT / os.getenv('TEMPLATE_DIR', 'templates')
        )

        # ---- Logging ----
        self.log_level = os.getenv('LOG_LEVEL', 'INFO')
        self.log_file = os.getenv('LOG_FILE', './logs/analyzer.log')

        # ---- Market hours ----
        self.market_open = os.getenv('MARKET_OPEN', '09:00')
        self.market_close = os.getenv('MARKET_CLOSE', '15:00')

        # ---- Email ----
        self.enable_email_notifications = (
            os.getenv('ENABLE_EMAIL_NOTIFICATIONS', 'false').lower() == 'true'
        )
        self.email_user = os.getenv('EMAIL_USER', '')
        self.email_password = os.getenv('EMAIL_PASSWORD', '')
        self.email_recipients = [
            r.strip() for r in
            os.getenv('EMAIL_RECIPIENTS', '').split(',')
            if r.strip()
        ]
        self.smtp_host = os.getenv('SMTP_HOST', 'smtp.gmail.com')
        self.smtp_port = int(os.getenv('SMTP_PORT', '587'))

        # ---- Stock symbols ----
        self.stock_symbols = self._load_symbols()

        # ---- Ensure directories exist ----
        for d in [self.report_directory, self.cache_dir,
                  os.path.dirname(self.log_file)]:
            os.makedirs(d, exist_ok=True)

    def _load_symbols(self):
        """Load stock symbols from env or default list."""
        env_symbols = os.getenv('STOCK_SYMBOLS')
        if env_symbols:
            return [s.strip() for s in env_symbols.split(',')]

        # Default NSE watchlist
        return [
            'SCOM',   # Safaricom
            'EQTY',   # Equity Group
            'KCB',    # KCB Group
            'EABL',   # East African Breweries
            'COOP',   # Co-operative Bank
            'ABSA',   # Absa Bank Kenya
            'NCBA',   # NCBA Group
            'SCBK',   # Standard Chartered Bank Kenya
            'IMH',    # I&M Group
            'KPLC',   # Kenya Power
        ]
