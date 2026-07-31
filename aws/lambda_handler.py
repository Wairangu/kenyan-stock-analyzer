"""
AWS Lambda entry point for the Kenyan Stock Analyzer.

Mirrors main.py's pipeline call sequence for the lean, watchlist-only,
HTML-only path (no PDF, no Excel, no per-stock detailed reports, no
NSE-PDF-OCR fallback source), then uploads the generated dashboard to S3
and emails an HTML summary via SES.

Triggered on a schedule by EventBridge (see ../terraform). See ../aws for
the Dockerfile and build script.
"""

import os
import sys
import json
import mimetypes
from datetime import datetime

# Point the app at Lambda's writable /tmp. Terraform sets these explicitly
# on the function too; the setdefault calls here just make `docker run`
# work locally without having to pass every one of them by hand.
os.environ.setdefault('CACHE_DIR', '/tmp/data')
os.environ.setdefault('REPORT_DIRECTORY', '/tmp/reports')
os.environ.setdefault('LOG_FILE', '/tmp/logs/analyzer.log')
os.environ.setdefault('DATA_SOURCES', 'tradingview,yahoo_finance')
os.environ.setdefault(
    'STOCK_SYMBOLS',
    'SCOM,EQTY,KCB,EABL,COOP,ABSA,NCBA,SCBK,IMH,KPLC',
)
# main.py's own SMTP send path is unused here — we send via SES instead.
os.environ.setdefault('ENABLE_EMAIL_NOTIFICATIONS', 'false')

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))

import boto3

from config import Config
from logger import get_logger, setup_logging
from data_acquisition import DataAcquisition
from analysis_engine import AnalysisEngine
from report_generator import ReportGenerator
from sector_analysis import SectorAnalyzer
from fundamental_analysis import FundamentalAnalysis
from email_notifier import EmailNotifier

CONTENT_TYPES = {
    '.html': 'text/html',
    '.ics': 'text/calendar',
}


def _market_closed_today():
    """
    Same weekday + Kenyan-holiday check as send_summary.py's
    market_closed_today(), duplicated here (rather than imported) so this
    module has no dependency on send_summary.py's own module-level setup.
    """
    try:
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("Africa/Nairobi")).date()
    except Exception:
        today = datetime.now().date()

    if today.weekday() >= 5:  # Saturday, Sunday
        return True, "weekend"

    try:
        import holidays
        ke = holidays.Kenya(years=today.year)
        if today in ke:
            return True, ke.get(today)
    except Exception:
        pass  # fail open — don't block the report on a holiday-check error

    return False, None


def _upload_reports(report_directory, bucket, logger):
    s3 = boto3.client('s3')
    uploaded = []
    for name in sorted(os.listdir(report_directory)):
        path = os.path.join(report_directory, name)
        if not os.path.isfile(path):
            continue
        ext = os.path.splitext(name)[1].lower()
        content_type = (
            CONTENT_TYPES.get(ext)
            or mimetypes.guess_type(name)[0]
            or 'application/octet-stream'
        )
        with open(path, 'rb') as f:
            s3.put_object(Bucket=bucket, Key=name, Body=f.read(), ContentType=content_type)
        uploaded.append(name)
        logger.info(f"  Uploaded s3://{bucket}/{name} ({content_type})")
    return uploaded


def _upload_prices(fundamentals_data, bucket, logger):
    """
    Publish a small {symbol: {price, change_pct}} snapshot alongside the
    HTML dashboard, covering every NSE stock fetch_all_fundamentals()
    returned (not just the watchlist). Public data already shown on the
    dashboard itself, so no new sensitivity -- lets other lightweight
    services (e.g. the portfolio tracker) read current prices without
    needing their own TradingView/tvkit dependency.
    """
    prices = {
        sym: {"price": d.get("close"), "change_pct": d.get("change_pct")}
        for sym, d in (fundamentals_data or {}).items()
        if d.get("close") is not None
    }
    boto3.client('s3').put_object(
        Bucket=bucket, Key="prices.json",
        Body=json.dumps(prices).encode(), ContentType="application/json",
    )
    logger.info(f"  Uploaded s3://{bucket}/prices.json ({len(prices)} symbols)")
    return len(prices)


def _send_email(subject, html_body, logger):
    ses = boto3.client('ses')
    resp = ses.send_email(
        Source=os.environ['SES_SENDER'],
        Destination={'ToAddresses': [os.environ['SES_RECIPIENT']]},
        Message={
            'Subject': {'Data': subject, 'Charset': 'UTF-8'},
            'Body': {'Html': {'Data': html_body, 'Charset': 'UTF-8'}},
        },
    )
    message_id = resp.get('MessageId')
    logger.info(f"  SES message sent: {message_id}")
    return message_id


def _invalidate_cache(distribution_id, logger):
    """
    Bust CloudFront's cache after each day's upload so the dashboard is
    never stale for up to the cache's 24h default TTL.
    """
    cf = boto3.client('cloudfront')
    resp = cf.create_invalidation(
        DistributionId=distribution_id,
        InvalidationBatch={
            'CallerReference': str(datetime.now().timestamp()),
            'Paths': {'Quantity': 1, 'Items': ['/*']},
        },
    )
    invalidation_id = resp['Invalidation']['Id']
    logger.info(f"  CloudFront invalidation created: {invalidation_id}")
    return invalidation_id


def handler(event, context):
    config = Config()
    setup_logging(config)
    logger = get_logger(__name__)

    dry_run = os.environ.get('DRY_RUN', 'false').lower() == 'true'

    closed, reason = _market_closed_today()
    if closed:
        logger.info(f"NSE is closed today ({reason}) — skipping run.")
        return {"status": "skipped", "reason": reason}

    logger.info("=" * 60)
    logger.info(f"KENYAN STOCK ANALYZER (Lambda) — {datetime.now():%Y-%m-%d %H:%M}")
    logger.info("=" * 60)

    from utils import enforce_daily_cache
    # Matters on a warm Lambda container: /tmp can persist across
    # invocations, so this guarantees a new calendar day never reuses
    # yesterday's cached data (same guarantee main.py gives locally).
    enforce_daily_cache(config.cache_dir, config.report_directory)

    data_acq = DataAcquisition(data_sources=config.data_sources, cache_dir=config.cache_dir)
    analysis_engine = AnalysisEngine(config=config)
    report_gen = ReportGenerator(
        template_dir=config.template_dir,
        output_dir=config.report_directory,
        clean_old=False,  # reports already cleared by enforce_daily_cache
    )
    sector_analyzer = SectorAnalyzer()

    logger.info("Fetching stock data...")
    stock_data = data_acq.fetch_multiple_stocks(config.stock_symbols, period='6mo', interval='1d')
    if not stock_data:
        raise RuntimeError("No stock data fetched — aborting.")
    logger.info(f"Fetched {len(stock_data)} stocks")

    analysis_results = analysis_engine.analyze_multiple_stocks(stock_data)

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
                for symbol, result in analysis_results.items():
                    if not result:
                        continue
                    price = result.get('latest', {}).get('close')
                    validations[symbol] = pv.validate(symbol, price, stock_data.get(symbol))
            if config.enable_official_close:
                apply_official_close(analysis_results, reference, logger)
        except Exception as e:
            logger.warning(f"Official-close/validation skipped: {e}")

    sector_data = sector_analyzer.analyze_sectors(stock_data, analysis_results)
    breadth = analysis_engine.calculate_market_breadth(analysis_results)

    logger.info("Fetching fundamental data...")
    fund_analyzer = FundamentalAnalysis(cache_dir=config.cache_dir)
    fundamentals_data = fund_analyzer.fetch_all_fundamentals()
    logger.info(f"Fundamental data loaded for {len(fundamentals_data)} stocks")

    try:
        from dividend_calendar import apply_dividend_calendar
        apply_dividend_calendar(fundamentals_data, cache_dir=config.cache_dir, logger=logger)
    except Exception as e:
        logger.warning(f"Dividend validation skipped: {e}")

    try:
        from earnings_calendar import write_ics
        write_ics(fundamentals_data, os.path.join(config.report_directory, "earnings.ics"))
    except Exception as e:
        logger.warning(f"Earnings ICS export skipped: {e}")

    sector_medians = {}
    usd_kes = None
    try:
        from market_context import compute_sector_medians, fetch_usd_kes
        sector_medians = compute_sector_medians(fundamentals_data)
        if config.enable_fx:
            usd_kes = fetch_usd_kes()
    except Exception as e:
        logger.warning(f"Market context skipped: {e}")

    scores = {}
    alerts = {}
    if config.enable_scoring:
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

    logger.info("Generating market summary...")
    report_gen.generate_market_summary(
        analysis_results, sector_data=sector_data, breadth=breadth, report_type='html',
    )

    logger.info("Generating index dashboard...")
    report_gen.generate_index(
        analysis_results, sector_data=sector_data, breadth=breadth,
        report_files={}, fundamentals_data=fundamentals_data,
        validations=validations, scores=scores, alerts=alerts, usd_kes=usd_kes,
    )

    notifier = EmailNotifier(config)  # only generate_email_body() is used — SMTP fields are unused here
    dashboard_url = os.environ.get('DASHBOARD_URL')  # set by Terraform to the S3 website endpoint
    email_body = notifier.generate_email_body(
        analysis_results, sector_data, breadth, dashboard_url=dashboard_url,
        fundamentals_data=fundamentals_data, scores=scores,
    )
    subject = f"NSE Daily Report — {datetime.now():%Y-%m-%d}"

    result = {
        "status": "ok",
        "stocks_analyzed": len(analysis_results),
        "dry_run": dry_run,
    }

    if dry_run:
        logger.info("DRY_RUN=true — skipping S3 upload and SES send.")
        result["would_upload"] = sorted(os.listdir(config.report_directory))
        result["email_subject"] = subject
    else:
        bucket = os.environ['S3_BUCKET']
        logger.info(f"Uploading dashboard to s3://{bucket} ...")
        result["uploaded"] = _upload_reports(config.report_directory, bucket, logger)
        result["prices_symbols"] = _upload_prices(fundamentals_data, bucket, logger)

        distribution_id = os.environ.get('CLOUDFRONT_DISTRIBUTION_ID')
        if distribution_id:
            logger.info("Invalidating CloudFront cache...")
            result["cloudfront_invalidation_id"] = _invalidate_cache(distribution_id, logger)

        logger.info("Sending summary email via SES...")
        result["ses_message_id"] = _send_email(subject, email_body, logger)

    logger.info("Done.")
    return result
