#!/usr/bin/env python3
"""
Kenyan Stock Analyzer - Main Orchestrator

Single entry point for the NSE daily reporting pipeline.
Run: ./run.sh  or  python main.py

Fetches ALL stocks from the NSE daily price list, analyzes them,
and generates a single index.html dashboard as the entry point.

Usage:
    python main.py                          # Full pipeline
    python main.py --period 1mo             # Shorter period
    python main.py --report-type both       # HTML + PDF
    python main.py --export-excel           # Also export Excel
"""

import os
import sys
import argparse
from datetime import datetime
from dotenv import load_dotenv

# Fix WeasyPrint on macOS: ensure Homebrew libraries are findable
if sys.platform == 'darwin':
    homebrew_lib = '/opt/homebrew/lib'
    if os.path.isdir(homebrew_lib):
        existing = os.environ.get('DYLD_LIBRARY_PATH', '')
        os.environ['DYLD_LIBRARY_PATH'] = (
            f"{homebrew_lib}:{existing}" if existing else homebrew_lib
        )

# Add src directory to path (main.py is at project root)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))

load_dotenv()

from config import Config
from logger import get_logger, setup_logging
from data_acquisition import DataAcquisition
from analysis_engine import AnalysisEngine
from report_generator import ReportGenerator
from sector_analysis import SectorAnalyzer
from fundamental_analysis import FundamentalAnalysis

# ---- Setup ----
config = Config()
setup_logging(config)
logger = get_logger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description='Kenyan Stock Analyzer — NSE Daily Dashboard'
    )
    parser.add_argument('--date', type=str, help='Analysis date (YYYY-MM-DD)')
    parser.add_argument('--period', type=str, default='6mo',
                        help='Data period: 1d, 5d, 1mo, 3mo, 6mo, 1y')
    parser.add_argument('--interval', type=str, default='1d',
                        help='Data interval')
    parser.add_argument('--report-type', type=str, choices=['html', 'pdf', 'both'],
                        default=config.report_format)
    parser.add_argument('--no-email', action='store_true')
    parser.add_argument('--force-refresh', action='store_true')
    parser.add_argument('--export-excel', action='store_true')
    parser.add_argument('--detailed', action='store_true',
                        help='Generate individual stock reports (slower)')
    parser.add_argument('--watchlist-only', action='store_true',
                        help='Only analyze configured watchlist, not all stocks')
    args = parser.parse_args()

    analysis_date = datetime.now()
    if args.date:
        try:
            analysis_date = datetime.strptime(args.date, '%Y-%m-%d')
        except ValueError:
            logger.error("Invalid date format. Use YYYY-MM-DD")
            sys.exit(1)

    logger.info("=" * 60)
    logger.info(f"KENYAN STOCK ANALYZER — {analysis_date.strftime('%Y-%m-%d')}")
    logger.info("=" * 60)

    try:
        # ---- Enforce cache freshness by day ----
        # New day (or --force-refresh) → wipe stale data cache and clear reports
        # so nothing from a previous day is ever reused. Same day → keep cache.
        from utils import enforce_daily_cache
        new_day = enforce_daily_cache(config.cache_dir, config.report_directory)
        if args.force_refresh:
            logger.info("--force-refresh: ignoring any same-day cache.")

        # ---- Initialize ----
        data_acq = DataAcquisition(
            data_sources=config.data_sources,
            cache_dir=config.cache_dir,
        )
        analysis_engine = AnalysisEngine(config=config)
        report_gen = ReportGenerator(
            template_dir=config.template_dir,
            output_dir=config.report_directory,
            clean_old=False,  # reports already cleared by enforce_daily_cache
        )
        sector_analyzer = SectorAnalyzer()

        # ---- Fetch data ----
        logger.info("Fetching stock data from NSE...")
        if args.watchlist_only:
            symbols = config.stock_symbols
            stock_data = data_acq.fetch_multiple_stocks(
                symbols, period=args.period, interval=args.interval,
                force_refresh=args.force_refresh,
            )
        else:
            # Fetch ALL stocks from the NSE PDF
            stock_data = data_acq.fetch_all_stocks(
                period=args.period, interval=args.interval,
                force_refresh=args.force_refresh,
            )

        if not stock_data:
            logger.error("No stock data fetched. Exiting.")
            sys.exit(1)

        logger.info(f"Fetched {len(stock_data)} stocks")

        # ---- Analyze ----
        logger.info("Analyzing...")
        analysis_results = analysis_engine.analyze_multiple_stocks(stock_data)

        # ---- Anchor prices to the NSE official close + cross-check ----
        # Displayed prices/changes use the official settled close (stable after
        # market close); TradingView is kept only as a cross-check. Done before
        # sector/breadth so those use the official figures too.
        validations = {}
        if config.enable_price_validation or config.enable_official_close:
            try:
                from price_validation import PriceValidator, apply_official_close
                pv = PriceValidator(
                    cache_dir=config.cache_dir,
                    disagree_threshold_pct=config.price_disagree_threshold_pct,
                )
                reference = pv.fetch_reference_prices()
                if config.enable_price_validation:
                    logger.info("Cross-checking TradingView vs NSE official close...")
                    for symbol, result in analysis_results.items():
                        if not result:
                            continue
                        price = result.get('latest', {}).get('close')
                        validations[symbol] = pv.validate(symbol, price, stock_data.get(symbol))
                    n_ok = sum(1 for v in validations.values() if v['status'] == 'ok')
                    n_mis = sum(1 for v in validations.values() if v['status'] == 'mismatch')
                    logger.info(f"  Cross-check: {n_ok} confirmed, {n_mis} differ")
                if config.enable_official_close:
                    apply_official_close(analysis_results, reference, logger)
            except Exception as e:
                logger.warning(f"Official-close/validation skipped: {e}")

        sector_data = sector_analyzer.analyze_sectors(stock_data, analysis_results)
        breadth = analysis_engine.calculate_market_breadth(analysis_results)

        # ---- Fundamental Analysis (TradingView) ----
        logger.info("Fetching fundamental data from TradingView...")
        fund_analyzer = FundamentalAnalysis(cache_dir=config.cache_dir)
        fundamentals_data = fund_analyzer.fetch_all_fundamentals(
            force_refresh=args.force_refresh
        )
        logger.info(f"Fundamental data loaded for {len(fundamentals_data)} stocks")

        # ---- Validate dividends against the authoritative NSE calendar ----
        try:
            from dividend_calendar import apply_dividend_calendar
            logger.info("Validating dividends against the NSE dividend calendar...")
            apply_dividend_calendar(fundamentals_data, cache_dir=config.cache_dir, logger=logger)
        except Exception as e:
            logger.warning(f"Dividend validation skipped: {e}")

        # ---- Export upcoming earnings as an iCalendar (.ics) ----
        try:
            from earnings_calendar import write_ics
            ics_path = os.path.join(config.report_directory, "earnings.ics")
            write_ics(fundamentals_data, ics_path)
        except Exception as e:
            logger.warning(f"Earnings ICS export skipped: {e}")

        # ---- Market context: sector medians + USD/KES ----
        sector_medians = {}
        usd_kes = None
        try:
            from market_context import compute_sector_medians, fetch_usd_kes
            sector_medians = compute_sector_medians(fundamentals_data)
            if config.enable_fx:
                usd_kes = fetch_usd_kes()
        except Exception as e:
            logger.warning(f"Market context skipped: {e}")

        # ---- Factor scoring + alerts ----
        scores = {}
        alerts = {}
        if config.enable_scoring:
            logger.info("Scoring stocks (transparent factor screen)...")
            try:
                from scoring import score_stock, generate_alerts
                for symbol, result in analysis_results.items():
                    if not result:
                        continue
                    fund = fundamentals_data.get(symbol, {})
                    scores[symbol] = score_stock(symbol, result, fund)
                    a = generate_alerts(symbol, result, fund, validations.get(symbol))
                    if a:
                        alerts[symbol] = a
            except Exception as e:
                logger.warning(f"Scoring skipped: {e}")

        # ---- Persist daily history snapshot ----
        if config.enable_history:
            try:
                from history_tracker import HistoryTracker
                ht = HistoryTracker(data_dir=config.cache_dir)
                ht.record_snapshot(analysis_results, fundamentals_data, scores, validations)
            except Exception as e:
                logger.warning(f"History snapshot skipped: {e}")

        # ---- Individual reports (only if --detailed) ----
        report_files = {}
        if args.detailed:
            logger.info("Generating individual stock reports...")
            for symbol, result in analysis_results.items():
                if result:
                    # Get fundamentals for this stock
                    fund = fundamentals_data.get(symbol, {})
                    # Find similar stocks
                    similar = fund_analyzer.find_similar_stocks(
                        symbol, fundamentals_data
                    )
                    # Get sector peers
                    sector_peers = fund_analyzer.get_sector_peers(
                        symbol, fundamentals_data
                    )

                    path = report_gen.generate_stock_report(
                        symbol, result, report_type='html',
                        fundamentals=fund,
                        similar_stocks=similar,
                        sector_peers=sector_peers,
                        validation=validations.get(symbol),
                        score=scores.get(symbol),
                        alerts=alerts.get(symbol),
                        sector_medians=sector_medians,
                        usd_kes=usd_kes,
                    )
                    if path:
                        fname = os.path.basename(path) if isinstance(path, str) else os.path.basename(path[0])
                        report_files[symbol] = fname

        # ---- Market summary ----
        logger.info("Generating market summary...")
        report_gen.generate_market_summary(
            analysis_results, sector_data=sector_data, breadth=breadth,
            report_type=args.report_type,
        )

        # ---- Excel ----
        if args.export_excel:
            logger.info("Exporting to Excel...")
            report_gen.export_to_excel(analysis_results, sector_data, breadth)

        # ---- INDEX DASHBOARD (the main entry point) ----
        logger.info("Generating index dashboard...")
        index_path = report_gen.generate_index(
            analysis_results, sector_data=sector_data, breadth=breadth,
            report_files=report_files,
            fundamentals_data=fundamentals_data,
            validations=validations,
            scores=scores,
            alerts=alerts,
            usd_kes=usd_kes,
        )

        # ---- Email ----
        if not args.no_email and config.enable_email_notifications:
            logger.info("Sending email...")
            try:
                from email_notifier import EmailNotifier
                notifier = EmailNotifier(config)
                body = notifier.generate_email_body(
                    analysis_results, sector_data, breadth,
                    fundamentals_data=fundamentals_data, scores=scores,
                )
                notifier.send_report(
                    f"NSE Daily Report — {analysis_date.strftime('%Y-%m-%d')}",
                    body,
                )
            except Exception as e:
                logger.warning(f"Email failed: {e}")

        # ---- Done ----
        logger.info("Done!")
        print("\n" + "=" * 60)
        print("NSE DAILY DASHBOARD READY")
        print("=" * 60)
        print(f"Stocks analyzed: {len(analysis_results)}")
        print(f"Dashboard:       {os.path.abspath(index_path)}")
        if sector_data:
            for name, sd in sector_data.items():
                arrow = "▲" if sd['avg_change_pct'] > 0 else "▼" if sd['avg_change_pct'] < 0 else "─"
                print(f"  {arrow} {name}: {sd['avg_change_pct']:+.2f}%")
        print("=" * 60)

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()