#!/usr/bin/env python3
"""
Test suite for the Kenyan Stock Analyzer.

Tests data acquisition, analysis engine, report generation,
sector analysis, email notifier, and config loading.
"""

import sys
import os
import unittest
import tempfile
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))

from config import Config
from logger import setup_logging, get_logger
from utils import retry, safe_float, detect_support_resistance
from analysis_engine import AnalysisEngine
from sector_analysis import SectorAnalyzer

# Quiet logging during tests
import logging
logging.disable(logging.CRITICAL)


def make_sample_data(periods=200, seed=42):
    """Generate reproducible sample OHLCV data."""
    np.random.seed(seed)
    dates = pd.date_range('2025-01-01', periods=periods, freq='B')
    close = np.random.randn(periods).cumsum() + 100
    return pd.DataFrame({
        'open': close + np.random.randn(periods) * 0.5,
        'high': close + abs(np.random.randn(periods)) * 2,
        'low': close - abs(np.random.randn(periods)) * 2,
        'close': close,
        'volume': np.random.randint(50000, 500000, periods),
    }, index=dates)


class TestConfig(unittest.TestCase):
    """Test configuration loading."""

    def test_config_loads(self):
        config = Config()
        self.assertIsNotNone(config.stock_symbols)
        self.assertGreater(len(config.stock_symbols), 0)
        self.assertIn('SCOM', config.stock_symbols)
        self.assertIsNotNone(config.data_sources)
        self.assertIn('nse_pdf', config.data_sources)

    def test_analysis_params(self):
        config = Config()
        self.assertEqual(config.rsi_period, 14)
        self.assertEqual(config.macd_fast, 12)
        self.assertEqual(config.macd_slow, 26)


class TestUtils(unittest.TestCase):
    """Test utility functions."""

    def test_safe_float(self):
        self.assertEqual(safe_float('123.45'), 123.45)
        self.assertEqual(safe_float('abc'), 0.0)
        self.assertEqual(safe_float(None), 0.0)

    def test_support_resistance(self):
        prices = np.array([10, 11, 12, 11, 10, 9, 10, 11, 12, 13, 12, 11, 10, 11, 12])
        supports, resistances = detect_support_resistance(prices, window=3)
        self.assertIsInstance(supports, list)
        self.assertIsInstance(resistances, list)

    def test_retry_decorator(self):
        call_count = [0]

        @retry(max_attempts=3, backoff=0.01, exceptions=(ValueError,))
        def flaky():
            call_count[0] += 1
            if call_count[0] < 3:
                raise ValueError("fail")
            return "success"

        result = flaky()
        self.assertEqual(result, "success")
        self.assertEqual(call_count[0], 3)


class TestAnalysisEngine(unittest.TestCase):
    """Test technical analysis calculations."""

    @classmethod
    def setUpClass(cls):
        cls.engine = AnalysisEngine()
        cls.data = make_sample_data(200)
        cls.result = cls.engine.analyze_stock(cls.data)

    def test_indicators_present(self):
        df = self.result['data']
        expected = ['sma_20', 'sma_50', 'ema_12', 'ema_26', 'rsi',
                     'macd', 'macd_signal', 'macd_hist',
                     'bb_upper', 'bb_middle', 'bb_lower',
                     'atr', 'obv', 'stoch_k', 'stoch_d', 'volume_sma_20']
        for col in expected:
            self.assertIn(col, df.columns, f"Missing column: {col}")

    def test_signals_present(self):
        signals = self.result['signals']
        expected = ['ma_crossover', 'rsi', 'macd', 'bollinger', 'trend',
                     'stochastic', 'volume', 'overall']
        for sig in expected:
            self.assertIn(sig, signals, f"Missing signal: {sig}")

    def test_signal_values(self):
        signals = self.result['signals']
        valid = {'bullish', 'bearish', 'overbought', 'oversold', 'neutral',
                 'above_upper', 'below_lower', 'within_bands', 'undefined',
                 'golden_cross', 'death_cross', 'bullish_cross', 'bearish_cross',
                 'high_volume', 'low_volume', 'normal'}
        for name, value in signals.items():
            self.assertIn(value, valid, f"Invalid signal '{value}' for '{name}'")

    def test_support_resistance(self):
        supports = self.result.get('support', [])
        resistances = self.result.get('resistance', [])
        self.assertIsInstance(supports, list)
        self.assertIsInstance(resistances, list)

    def test_daily_change(self):
        chg = self.result.get('daily_change_pct')
        self.assertIsNotNone(chg)
        self.assertIsInstance(chg, float)

    def test_multi_stock_analysis(self):
        data_dict = {
            'SCOM': make_sample_data(100, seed=1),
            'EQTY': make_sample_data(100, seed=2),
        }
        results = self.engine.analyze_multiple_stocks(data_dict)
        self.assertIn('SCOM', results)
        self.assertIn('EQTY', results)

    def test_market_breadth(self):
        data_dict = {
            'SCOM': make_sample_data(100, seed=1),
            'EQTY': make_sample_data(100, seed=2),
            'KCB': make_sample_data(100, seed=3),
        }
        results = self.engine.analyze_multiple_stocks(data_dict)
        breadth = self.engine.calculate_market_breadth(results)
        self.assertIn('total_stocks', breadth)
        self.assertEqual(breadth['total_stocks'], 3)
        self.assertIn('pct_above_sma50', breadth)

    def test_rsi_range(self):
        df = self.result['data']
        rsi = df['rsi'].dropna()
        if len(rsi) > 0:
            self.assertTrue((rsi >= 0).all(), "RSI should be >= 0")
            self.assertTrue((rsi <= 100).all(), "RSI should be <= 100")

    def test_empty_data(self):
        result = self.engine.analyze_stock(pd.DataFrame())
        self.assertEqual(result, {})


class TestSectorAnalysis(unittest.TestCase):
    """Test sector analysis."""

    @classmethod
    def setUpClass(cls):
        cls.analyzer = SectorAnalyzer()
        cls.engine = AnalysisEngine()

    def test_sector_mapping(self):
        self.assertEqual(self.analyzer.get_sector('SCOM'), 'Telecommunication')
        self.assertEqual(self.analyzer.get_sector('EQTY'), 'Banking')
        self.assertEqual(self.analyzer.get_sector('KCB'), 'Banking')
        self.assertEqual(self.analyzer.get_sector('EABL'), 'Manufacturing')
        self.assertEqual(self.analyzer.get_sector('UNKNOWN'), 'Other')

    def test_analyze_sectors(self):
        data_dict = {
            'SCOM': make_sample_data(100, seed=1),
            'EQTY': make_sample_data(100, seed=2),
            'KCB': make_sample_data(100, seed=3),
        }
        results = self.engine.analyze_multiple_stocks(data_dict)
        sectors = self.analyzer.analyze_sectors(data_dict, results)

        self.assertIn('Telecommunication', sectors)
        self.assertIn('Banking', sectors)

        banking = sectors['Banking']
        self.assertIn('EQTY', banking['symbols'])
        self.assertIn('KCB', banking['symbols'])
        self.assertEqual(banking['count'], 2)
        self.assertIn('avg_change_pct', banking)
        self.assertIn('avg_rsi', banking)
        self.assertIn('bullish_ratio', banking)


class TestReportGenerator(unittest.TestCase):
    """Test report generation."""

    @classmethod
    def setUpClass(cls):
        from report_generator import ReportGenerator
        cls.engine = AnalysisEngine()
        cls.tempdir = tempfile.mkdtemp()
        cls.rg = ReportGenerator(output_dir=cls.tempdir)

    def test_stock_report_html(self):
        data = make_sample_data(60)
        result = self.engine.analyze_stock(data)
        path = self.rg.generate_stock_report('TEST', result, report_type='html')
        self.assertIsNotNone(path)
        self.assertTrue(os.path.exists(path))
        self.assertGreater(os.path.getsize(path), 500)

    def test_market_summary_html(self):
        data_dict = {
            'SCOM': make_sample_data(60, seed=1),
            'EQTY': make_sample_data(60, seed=2),
        }
        results = self.engine.analyze_multiple_stocks(data_dict)
        path = self.rg.generate_market_summary(results, report_type='html')
        self.assertIsNotNone(path)
        self.assertTrue(os.path.exists(path))
        self.assertGreater(os.path.getsize(path), 500)

    def test_excel_export(self):
        data_dict = {
            'SCOM': make_sample_data(60, seed=1),
            'EQTY': make_sample_data(60, seed=2),
        }
        results = self.engine.analyze_multiple_stocks(data_dict)
        path = self.rg.export_to_excel(results)
        self.assertIsNotNone(path)
        self.assertTrue(os.path.exists(path))
        self.assertGreater(os.path.getsize(path), 1000)


class TestEmailNotifier(unittest.TestCase):
    """Test email notification module."""

    def test_email_body_generation(self):
        from email_notifier import EmailNotifier
        config = Config()
        notifier = EmailNotifier(config)

        # No fundamentals -> no tv_class -> build_candidate_list correctly
        # excludes both (see src/recommender.py); this exercises the
        # "no signals today" branch of the write-up.
        candidates = []
        track_record = {"horizon_days": 10, "as_of": None, "tiers": {}, "note": "No signal history yet."}

        body = notifier.generate_email_body(candidates=candidates, track_record=track_record)
        self.assertIsInstance(body, str)
        self.assertIn('What to Buy Today', body)
        self.assertIn('No Buy or Strong Buy signals today', body)

    def test_email_body_with_candidates(self):
        from email_notifier import EmailNotifier
        config = Config()
        notifier = EmailNotifier(config)

        candidates = [
            {'symbol': 'SCOM', 'price': 36.4, 'tv_label': 'Strong Buy',
             'tv_class': 'strong_buy', 'score': 72, 'score_coverage': 83},
        ]
        track_record = {"horizon_days": 10, "as_of": "2026-09-21",
                        "tiers": {"strong_buy": {"n": 5, "hit_rate": 80.0, "avg_return_pct": 3.2},
                                  "buy": {"n": 0, "hit_rate": None, "avg_return_pct": None}},
                        "note": "Only 5 scored call(s) so far."}

        body = notifier.generate_email_body(candidates=candidates, track_record=track_record)
        self.assertIn('SCOM', body)
        self.assertIn('Strong Buy', body)


def run_tests():
    """Run all tests and print results."""
    print("=" * 60)
    print("KENYAN STOCK ANALYZER — TEST SUITE")
    print("=" * 60)
    print()

    # Create test suite
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestConfig))
    suite.addTests(loader.loadTestsFromTestCase(TestUtils))
    suite.addTests(loader.loadTestsFromTestCase(TestAnalysisEngine))
    suite.addTests(loader.loadTestsFromTestCase(TestSectorAnalysis))
    suite.addTests(loader.loadTestsFromTestCase(TestReportGenerator))
    suite.addTests(loader.loadTestsFromTestCase(TestEmailNotifier))

    # Run
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    print()
    print("=" * 60)
    if result.wasSuccessful():
        print("ALL TESTS PASSED!")
    else:
        print(f"FAILURES: {len(result.failures)}, ERRORS: {len(result.errors)}")
    print("=" * 60)

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(run_tests())