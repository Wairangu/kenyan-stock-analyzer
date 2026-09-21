"""
Enhanced technical analysis engine for NSE stocks.

Indicators: SMA, EMA, RSI (Wilder), MACD, Bollinger Bands,
            ATR, OBV, Stochastic Oscillator, Volume SMA,
            Support/Resistance levels.

Signals: Crossover detection (not just state), market breadth.
"""

import pandas as pd
import numpy as np
from datetime import timedelta
from data_quality import is_session
from logger import get_logger
from utils import detect_support_resistance

logger = get_logger(__name__)


class AnalysisEngine:
    """Performs technical analysis on stock data."""

    def __init__(self, config=None):
        self.config = config
        if config:
            self.rsi_period = config.rsi_period
            self.rsi_overbought = config.rsi_overbought
            self.rsi_oversold = config.rsi_oversold
            self.macd_fast = config.macd_fast
            self.macd_slow = config.macd_slow
            self.macd_signal_period = config.macd_signal
            self.bb_period = config.bbands_period
            self.bb_std = config.bbands_std
            self.sma_short = config.sma_short
            self.sma_long = config.sma_long
            self.ema_short = config.ema_short
            self.ema_long = config.ema_long
            self.atr_period = config.atr_period
            self.stoch_k = config.stoch_k
            self.stoch_d = config.stoch_d
            self.vol_sma = config.volume_sma_period
        else:
            self._set_defaults()

        logger.info("AnalysisEngine initialized")

    def _set_defaults(self):
        self.rsi_period = 14
        self.rsi_overbought = 70
        self.rsi_oversold = 30
        self.macd_fast = 12
        self.macd_slow = 26
        self.macd_signal_period = 9
        self.bb_period = 20
        self.bb_std = 2.0
        self.sma_short = 20
        self.sma_long = 50
        self.ema_short = 12
        self.ema_long = 26
        self.atr_period = 14
        self.stoch_k = 14
        self.stoch_d = 3
        self.vol_sma = 20

    # ---- Simple Indicators ----

    @staticmethod
    def calculate_sma(data, window):
        return data.rolling(window=window).mean()

    @staticmethod
    def calculate_ema(data, window):
        return data.ewm(span=window, adjust=False, min_periods=window).mean()

    # ---- RSI with Wilder's Smoothing ----

    def calculate_rsi(self, data, window=None):
        """
        RSI using Wilder's smoothing.

        Args:
            data: Series of closing prices.
            window: RSI period (default self.rsi_period).

        Returns:
            Series of RSI values.
        """
        if window is None:
            window = self.rsi_period

        delta = data.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)

        # Wilder seeds with the arithmetic mean of the first window changes.
        avg_gain = pd.Series(np.nan, index=data.index, dtype=float)
        avg_loss = avg_gain.copy()
        if len(data) <= window:
            return avg_gain
        avg_gain.iloc[window] = gain.iloc[1:window + 1].mean()
        avg_loss.iloc[window] = loss.iloc[1:window + 1].mean()
        for i in range(window + 1, len(data)):
            avg_gain.iloc[i] = (avg_gain.iloc[i - 1] * (window - 1) + gain.iloc[i]) / window
            avg_loss.iloc[i] = (avg_loss.iloc[i - 1] * (window - 1) + loss.iloc[i]) / window

        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        rsi.loc[(avg_loss == 0) & (avg_gain > 0)] = 100.0
        rsi.loc[(avg_loss == 0) & (avg_gain == 0)] = 50.0

        # First `window` values are unreliable, set to NaN
        rsi.iloc[:window] = np.nan
        return rsi

    # ---- MACD ----

    def calculate_macd(self, data, fast=None, slow=None, signal=None):
        if fast is None:
            fast = self.macd_fast
        if slow is None:
            slow = self.macd_slow
        if signal is None:
            signal = self.macd_signal_period

        ema_fast = self.calculate_ema(data, fast)
        ema_slow = self.calculate_ema(data, slow)
        macd = ema_fast - ema_slow
        signal_line = self.calculate_ema(macd, signal)
        histogram = macd - signal_line

        return {'macd': macd, 'signal': signal_line, 'histogram': histogram}

    # ---- Bollinger Bands ----

    def calculate_bollinger_bands(self, data, window=None, num_std=None):
        if window is None:
            window = self.bb_period
        if num_std is None:
            num_std = self.bb_std

        middle = self.calculate_sma(data, window)
        std = data.rolling(window=window).std()
        upper = middle + (std * num_std)
        lower = middle - (std * num_std)

        return {'upper': upper, 'middle': middle, 'lower': lower}

    # ---- ATR (Average True Range) ----

    def calculate_atr(self, data, window=None):
        """
        Average True Range — measures volatility.

        Args:
            data: DataFrame with 'high', 'low', 'close' columns.
            window: ATR period.

        Returns:
            Series of ATR values.
        """
        if window is None:
            window = self.atr_period

        high = data['high']
        low = data['low']
        close = data['close'].shift(1)

        tr1 = high - low
        tr2 = (high - close).abs()
        tr3 = (low - close).abs()
        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        return true_range.rolling(window=window).mean()

    # ---- OBV (On-Balance Volume) ----

    @staticmethod
    def calculate_obv(data):
        """
        On-Balance Volume — cumulative volume flow indicator.

        Args:
            data: DataFrame with 'close' and 'volume' columns.

        Returns:
            Series of OBV values.
        """
        close = data['close']
        volume = data['volume']
        obv = pd.Series(0.0, index=data.index)

        for i in range(1, len(data)):
            if close.iloc[i] > close.iloc[i - 1]:
                obv.iloc[i] = obv.iloc[i - 1] + volume.iloc[i]
            elif close.iloc[i] < close.iloc[i - 1]:
                obv.iloc[i] = obv.iloc[i - 1] - volume.iloc[i]
            else:
                obv.iloc[i] = obv.iloc[i - 1]

        return obv

    # ---- Stochastic Oscillator ----

    def calculate_stochastic(self, data, k_window=None, d_window=None):
        """
        Stochastic Oscillator (%K and %D).

        Args:
            data: DataFrame with 'high', 'low', 'close' columns.
            k_window: %K lookback period.
            d_window: %D smoothing period.

        Returns:
            dict with 'stoch_k' and 'stoch_d' Series.
        """
        if k_window is None:
            k_window = self.stoch_k
        if d_window is None:
            d_window = self.stoch_d

        low_min = data['low'].rolling(window=k_window).min()
        high_max = data['high'].rolling(window=k_window).max()

        stoch_k = 100 * ((data['close'] - low_min) / (high_max - low_min).replace(0, np.nan))
        stoch_d = stoch_k.rolling(window=d_window).mean()

        return {'stoch_k': stoch_k, 'stoch_d': stoch_d}

    # ---- Volume SMA ----

    def calculate_volume_sma(self, data, window=None):
        if window is None:
            window = self.vol_sma
        return data['volume'].rolling(window=window).mean()

    # ---- Main Analysis ----

    def analyze_stock(self, data):
        """
        Perform full technical analysis on a stock.

        Args:
            data: DataFrame with OHLCV columns.

        Returns:
            dict with 'data', 'signals', 'latest', 'support', 'resistance'.
        """
        if data is None or data.empty:
            logger.warning("Empty data provided for analysis")
            return {}

        if 'close' not in data.columns:
            logger.error("Data must contain 'close' column")
            return {}

        df = data.copy()
        if df.index.has_duplicates or not df.index.is_monotonic_increasing:
            logger.warning("Unordered or duplicate price history")
            return {}
        if not np.isfinite(df['close']).all() or (df['close'] <= 0).any():
            logger.warning("Non-finite or non-positive closing prices")
            return {}

        # ---- Calculate all indicators ----
        df['sma_20'] = self.calculate_sma(df['close'], self.sma_short)
        df['sma_50'] = self.calculate_sma(df['close'], self.sma_long)
        df['ema_12'] = self.calculate_ema(df['close'], self.ema_short)
        df['ema_26'] = self.calculate_ema(df['close'], self.ema_long)
        df['rsi'] = self.calculate_rsi(df['close'])

        macd = self.calculate_macd(df['close'])
        df['macd'] = macd['macd']
        df['macd_signal'] = macd['signal']
        df['macd_hist'] = macd['histogram']

        bb = self.calculate_bollinger_bands(df['close'])
        df['bb_upper'] = bb['upper']
        df['bb_middle'] = bb['middle']
        df['bb_lower'] = bb['lower']

        # New indicators
        if all(c in df.columns for c in ['high', 'low']):
            df['atr'] = self.calculate_atr(df)
            stoch = self.calculate_stochastic(df)
            df['stoch_k'] = stoch['stoch_k']
            df['stoch_d'] = stoch['stoch_d']

        if 'volume' in df.columns:
            df['obv'] = self.calculate_obv(df)
            df['volume_sma_20'] = self.calculate_volume_sma(df)

        # ---- Generate signals ----
        signals = self._generate_signals(df)

        # ---- Support/Resistance ----
        supports, resistances = detect_support_resistance(
            df['close'].values
        )

        # ---- Latest values ----
        latest = self._get_latest_values(df)
        latest['price_source'] = data.attrs.get('source', 'TradingView')

        recent = df.tail(20)
        traded_value = None
        if len(recent) == 20 and 'volume' in recent:
            volumes = recent['volume']
            consecutive = False
            if isinstance(df.index, pd.DatetimeIndex):
                day = df.index[-1].date()
                expected = []
                while len(expected) < 20:
                    if is_session(day):
                        expected.append(day)
                    day -= timedelta(days=1)
                consecutive = list(recent.index.date) == list(reversed(expected))
            if consecutive and np.isfinite(volumes).all() and (volumes >= 0).all():
                traded_value = float((recent['close'] * volumes).median())
        history_date = df.index[-1].strftime('%Y-%m-%d') if isinstance(df.index, pd.DatetimeIndex) else None

        # ---- Daily change ----
        daily_change = None
        if len(df) >= 2:
            prev_close = df['close'].iloc[-2]
            curr_close = df['close'].iloc[-1]
            if prev_close and prev_close > 0:
                daily_change = ((curr_close - prev_close) / prev_close) * 100

        return {
            'data': df,
            'signals': signals,
            'latest': latest,
            'support': supports,
            'resistance': resistances,
            'daily_change_pct': daily_change,
            'history_bars': len(df),
            'history_date': history_date,
            'history_complete': len(df) >= max(self.sma_long, self.macd_slow + self.macd_signal_period),
            'median_value_traded_20d': traded_value,
            'identity_verified': bool(data.attrs.get('identity_verified', False)),
        }

    def _generate_signals(self, df):
        """Generate trading signals with crossover detection."""
        signals = {}

        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else latest

        # 1. MA Crossover (actual cross, not just state)
        if not pd.isna(latest['sma_20']) and not pd.isna(latest['sma_50']):
            if latest['sma_20'] > latest['sma_50']:
                signals['ma_crossover'] = 'bullish'
                # Check if this is a new cross
                if not pd.isna(prev['sma_20']) and not pd.isna(prev['sma_50']):
                    if prev['sma_20'] <= prev['sma_50']:
                        signals['ma_crossover'] = 'golden_cross'  # Just crossed!
            elif latest['sma_20'] < latest['sma_50']:
                signals['ma_crossover'] = 'bearish'
                if not pd.isna(prev['sma_20']) and not pd.isna(prev['sma_50']):
                    if prev['sma_20'] >= prev['sma_50']:
                        signals['ma_crossover'] = 'death_cross'  # Just crossed!
            else:
                signals['ma_crossover'] = 'neutral'
        else:
            signals['ma_crossover'] = 'undefined'

        # 2. RSI
        if not pd.isna(latest['rsi']):
            rsi = latest['rsi']
            if rsi > self.rsi_overbought:
                signals['rsi'] = 'overbought'
            elif rsi < self.rsi_oversold:
                signals['rsi'] = 'oversold'
            else:
                signals['rsi'] = 'neutral'
        else:
            signals['rsi'] = 'undefined'

        # 3. MACD Crossover
        if not pd.isna(latest['macd']) and not pd.isna(latest['macd_signal']):
            if latest['macd'] > latest['macd_signal']:
                signals['macd'] = 'bullish'
                if not pd.isna(prev['macd']) and not pd.isna(prev['macd_signal']):
                    if prev['macd'] <= prev['macd_signal']:
                        signals['macd'] = 'bullish_cross'
            elif latest['macd'] < latest['macd_signal']:
                signals['macd'] = 'bearish'
                if not pd.isna(prev['macd']) and not pd.isna(prev['macd_signal']):
                    if prev['macd'] >= prev['macd_signal']:
                        signals['macd'] = 'bearish_cross'
            else:
                signals['macd'] = 'neutral'
        else:
            signals['macd'] = 'undefined'

        # 4. Bollinger Bands
        if not pd.isna(latest['close']) and not pd.isna(latest['bb_upper']):
            if latest['close'] > latest['bb_upper']:
                signals['bollinger'] = 'above_upper'
            elif latest['close'] < latest['bb_lower']:
                signals['bollinger'] = 'below_lower'
            else:
                signals['bollinger'] = 'within_bands'
        else:
            signals['bollinger'] = 'undefined'

        # 5. Trend
        if not pd.isna(latest['close']) and not pd.isna(latest['sma_50']):
            signals['trend'] = ('bullish' if latest['close'] > latest['sma_50'] else
                                'bearish' if latest['close'] < latest['sma_50'] else 'neutral')
        else:
            signals['trend'] = 'undefined'

        # 6. Stochastic
        if 'stoch_k' in latest and not pd.isna(latest['stoch_k']):
            k = latest['stoch_k']
            if k > 80:
                signals['stochastic'] = 'overbought'
            elif k < 20:
                signals['stochastic'] = 'oversold'
            else:
                signals['stochastic'] = 'neutral'
        else:
            signals['stochastic'] = 'undefined'

        # 7. Volume
        if 'volume_sma_20' in latest and not pd.isna(latest['volume_sma_20']):
            if latest['volume'] > latest['volume_sma_20'] * 1.5:
                signals['volume'] = 'high_volume'
            elif latest['volume'] < latest['volume_sma_20'] * 0.5:
                signals['volume'] = 'low_volume'
            else:
                signals['volume'] = 'normal'
        else:
            signals['volume'] = 'undefined'

        # 8. Overall summary signal
        bullish_count = sum(
            1 for s in [
                signals['ma_crossover'], signals['macd'], signals['trend']
            ] if 'bullish' in str(s) or 'golden' in str(s)
        )
        bearish_count = sum(
            1 for s in [
                signals['ma_crossover'], signals['macd'], signals['trend']
            ] if 'bearish' in str(s) or 'death' in str(s)
        )
        if not self._get_latest_values(df).get('sma_50'):
            signals['overall'] = 'undefined'
        elif bullish_count > bearish_count:
            signals['overall'] = 'bullish'
        elif bearish_count > bullish_count:
            signals['overall'] = 'bearish'
        elif signals['rsi'] == 'overbought':
            signals['overall'] = 'bearish'
        elif signals['rsi'] == 'oversold':
            signals['overall'] = 'bullish'
        else:
            signals['overall'] = 'neutral'

        return signals

    def _get_latest_values(self, df):
        """Extract latest values of all indicators."""
        latest = df.iloc[-1]
        fields = [
            'close', 'sma_20', 'sma_50', 'ema_12', 'ema_26',
            'rsi', 'macd', 'macd_signal', 'macd_hist',
            'bb_upper', 'bb_middle', 'bb_lower',
            'atr', 'obv', 'stoch_k', 'stoch_d',
            'volume', 'volume_sma_20',
        ]
        result = {}
        for f in fields:
            if f in latest and not pd.isna(latest[f]):
                val = latest[f]
                result[f] = float(val) if isinstance(val, (np.floating, np.integer)) else val
            else:
                result[f] = None
        return result

    # ---- Multi-stock & Market Breadth ----

    def analyze_multiple_stocks(self, data_dict):
        """Analyze multiple stocks. Failures are isolated per stock."""
        results = {}
        for symbol, data in data_dict.items():
            try:
                results[symbol] = self.analyze_stock(data)
                if results[symbol]:
                    signals = results[symbol].get('signals', {})
                    logger.info(
                        f"  {symbol}: close={results[symbol]['latest'].get('close', 'N/A')}, "
                        f"overall={signals.get('overall', '?')}"
                    )
            except Exception as e:
                logger.error(f"  {symbol}: analysis error — {e}")
                results[symbol] = {}
        return results

    def calculate_market_breadth(self, analysis_results):
        """
        Calculate market breadth indicators across all analyzed stocks.

        Returns:
            dict with breadth metrics.
        """
        total = len(analysis_results)
        if total == 0:
            return {}

        above_sma50 = 0
        bullish_macd = 0
        rsi_above_50 = 0
        positive_change = 0
        with_change = 0

        for symbol, result in analysis_results.items():
            if not result:
                continue
            signals = result.get('signals', {})
            latest = result.get('latest', {})

            if signals.get('trend') == 'bullish':
                above_sma50 += 1
            if 'bullish' in str(signals.get('macd', '')):
                bullish_macd += 1
            if latest.get('rsi') and latest['rsi'] > 50:
                rsi_above_50 += 1

            chg = result.get('daily_change_pct')
            if chg is not None:
                with_change += 1
                if chg > 0:
                    positive_change += 1

        breadth = {
            'total_stocks': total,
            'pct_above_sma50': round(above_sma50 / total * 100, 1),
            'pct_bullish_macd': round(bullish_macd / total * 100, 1),
            'pct_rsi_above_50': round(rsi_above_50 / total * 100, 1),
        }

        if with_change > 0:
            breadth['advance_decline_ratio'] = round(
                positive_change / with_change, 2
            )
            breadth['advancers'] = positive_change
            breadth['decliners'] = with_change - positive_change

        return breadth


# ---- Test ----
if __name__ == "__main__":
    from logger import setup_logging
    setup_logging()
    logger = get_logger(__name__)

    # Generate sample data
    dates = pd.date_range('2025-01-01', periods=200, freq='B')
    np.random.seed(42)
    sample = pd.DataFrame({
        'open': np.random.randn(200).cumsum() + 100,
        'high': np.random.randn(200).cumsum() + 105,
        'low': np.random.randn(200).cumsum() + 95,
        'close': np.random.randn(200).cumsum() + 100,
        'volume': np.random.randint(10000, 100000, 200),
    }, index=dates)

    engine = AnalysisEngine()
    result = engine.analyze_stock(sample)

    print("\nSignals:", result['signals'])
    print("Latest:", {k: round(v, 2) if v else None
                      for k, v in result['latest'].items()})
    print("Support:", result['support'])
    print("Resistance:", result['resistance'])
    print(f"Daily change: {result['daily_change_pct']:.2f}%")
