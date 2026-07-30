#!/usr/bin/env python3
"""
Scheduler for the Kenyan Stock Analyzer.

Runs the analysis pipeline on a schedule (market close on weekdays).
Supports both long-running daemon mode and one-shot execution.

Usage:
    python scheduler.py                    # Run on schedule (Mon-Fri at 15:00 EAT)
    python scheduler.py --run-once         # Run once immediately and exit
    python scheduler.py --run-once --period 1mo  # One-shot with custom period
"""

import os
import sys
import subprocess
import time
import argparse
from datetime import datetime

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))

from logger import setup_logging, get_logger
from config import Config

config = Config()
setup_logging(config)
logger = get_logger(__name__)


def run_analysis(period='6mo', interval='1d', report_type='html',
                 force_refresh=False, export_excel=False, market_hours_only=False):
    """
    Run the full analysis pipeline by calling main.py.

    Args:
        period: Data period for fetching.
        interval: Data interval.
        report_type: 'html', 'pdf', or 'both'.
        force_refresh: Bypass cache.
        export_excel: Also export Excel workbook.
        market_hours_only: Skip the run if the NSE is not currently open.

    Returns:
        bool: True if successful (or skipped because the market is closed).
    """
    if market_hours_only and not is_market_open():
        logger.info("Market is closed — skipping run (--market-hours-only).")
        return True

    main_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'main.py')
    cmd = [
        sys.executable, main_py,
        '--period', period,
        '--interval', interval,
        '--report-type', report_type,
    ]
    if force_refresh:
        cmd.append('--force-refresh')
    if export_excel:
        cmd.append('--export-excel')

    logger.info(f"Running: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=False, text=True, timeout=300)
        if result.returncode == 0:
            logger.info("Analysis completed successfully")
            return True
        else:
            logger.error(f"Analysis failed with code {result.returncode}")
            return False
    except subprocess.TimeoutExpired:
        logger.error("Analysis timed out (5 minutes)")
        return False
    except Exception as e:
        logger.error(f"Analysis error: {e}")
        return False


def is_market_open():
    """Check if the NSE is currently open (Mon-Fri, 09:00-15:00 EAT)."""
    now = datetime.now()
    if now.weekday() >= 5:  # Weekend
        return False
    # EAT = UTC+3. Kenya doesn't observe DST.
    # Simplified: use local time (assumes system time is EAT or close)
    hour = now.hour
    return 9 <= hour < 15


def main():
    parser = argparse.ArgumentParser(description='NSE Stock Analyzer Scheduler')
    parser.add_argument('--run-once', action='store_true',
                        help='Run analysis once and exit')
    parser.add_argument('--period', type=str, default='6mo',
                        help='Data period')
    parser.add_argument('--interval', type=str, default='1d',
                        help='Data interval')
    parser.add_argument('--report-type', type=str, default='html',
                        choices=['html', 'pdf', 'both'])
    parser.add_argument('--force-refresh', action='store_true',
                        help='Bypass cache')
    parser.add_argument('--export-excel', action='store_true',
                        help='Export Excel workbook')
    parser.add_argument('--market-hours-only', action='store_true',
                        help='Only run during market hours')
    args = parser.parse_args()

    if args.run_once:
        logger.info("Running one-shot analysis...")
        success = run_analysis(
            period=args.period,
            interval=args.interval,
            report_type=args.report_type,
            force_refresh=args.force_refresh,
            export_excel=args.export_excel,
            market_hours_only=args.market_hours_only,
        )
        sys.exit(0 if success else 1)

    # ---- Scheduled mode ----
    try:
        import schedule
    except ImportError:
        logger.error("Schedule library not installed. Run: pip install schedule")
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("NSE STOCK ANALYZER — SCHEDULER")
    logger.info(f"Schedule: Mon-Fri at {config.market_close} EAT")
    logger.info("=" * 60)

    # Schedule for each weekday
    schedule.every().monday.at(config.market_close).do(
        run_analysis, period=args.period, interval=args.interval,
        report_type=args.report_type, force_refresh=args.force_refresh,
        export_excel=args.export_excel, market_hours_only=args.market_hours_only
    )
    schedule.every().tuesday.at(config.market_close).do(
        run_analysis, period=args.period, interval=args.interval,
        report_type=args.report_type, force_refresh=args.force_refresh,
        export_excel=args.export_excel, market_hours_only=args.market_hours_only
    )
    schedule.every().wednesday.at(config.market_close).do(
        run_analysis, period=args.period, interval=args.interval,
        report_type=args.report_type, force_refresh=args.force_refresh,
        export_excel=args.export_excel, market_hours_only=args.market_hours_only
    )
    schedule.every().thursday.at(config.market_close).do(
        run_analysis, period=args.period, interval=args.interval,
        report_type=args.report_type, force_refresh=args.force_refresh,
        export_excel=args.export_excel, market_hours_only=args.market_hours_only
    )
    schedule.every().friday.at(config.market_close).do(
        run_analysis, period=args.period, interval=args.interval,
        report_type=args.report_type, force_refresh=args.force_refresh,
        export_excel=args.export_excel, market_hours_only=args.market_hours_only
    )

    logger.info("Scheduler started. Press Ctrl+C to stop.")
    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        logger.info("Scheduler stopped by user")


if __name__ == '__main__':
    main()