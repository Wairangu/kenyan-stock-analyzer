"""
Daily signal history, persisted to S3.

Unlike history_tracker.py's local CSV (used only by local/dev runs and
never deployed), this is what the Lambda writes so the system's own calls
can be checked against what actually happened later -- one JSON object per
trading day at a stable, date-only key (`history/signals_<date>.json`), so
a same-day re-run overwrites cleanly instead of accumulating duplicates.

Takes an already-constructed boto3 S3 client rather than creating one, so
this stays testable without AWS credentials.
"""

import json
from datetime import datetime

from fundamental_analysis import FundamentalAnalysis
from logger import get_logger

logger = get_logger(__name__)

HISTORY_PREFIX = "history/"


def _snapshot_key(date: str) -> str:
    return f"{HISTORY_PREFIX}signals_{date}.json"


def write_daily_snapshot(s3, bucket, analysis_results, fundamentals_data=None,
                          scores=None, date=None):
    """
    Write one JSON snapshot of every analyzed symbol's signal for `date`
    (default: today) to s3://bucket/history/signals_<date>.json.

    Returns the number of symbols written, or 0 on error (fails safe --
    never raises, matching the rest of the Lambda's optional-section
    error handling).
    """
    try:
        date = date or datetime.now().strftime("%Y-%m-%d")
        fundamentals_data = fundamentals_data or {}
        scores = scores or {}

        symbols = {}
        for symbol, result in (analysis_results or {}).items():
            if not result:
                continue
            latest = result.get("latest", {})
            fund = fundamentals_data.get(symbol, {})
            sc = scores.get(symbol, {})
            tv_label, tv_class = FundamentalAnalysis.signal_from_tech_rating(
                fund.get("tech_rating")
            )
            symbols[symbol] = {
                "price": latest.get("close"),
                "tv_class": tv_class,
                "tv_label": tv_label,
                "score": sc.get("overall"),
                "score_coverage": sc.get("coverage"),
                "value_traded": fund.get("value_traded"),
            }

        if not symbols:
            return 0

        body = json.dumps({"date": date, "symbols": symbols}, indent=2).encode()
        s3.put_object(
            Bucket=bucket, Key=_snapshot_key(date), Body=body,
            ContentType="application/json",
        )
        logger.info(f"Signal history: wrote {len(symbols)} symbols for {date}")
        return len(symbols)
    except Exception as e:
        logger.warning(f"Signal history write failed: {e}")
        return 0


def load_all_snapshots(s3, bucket):
    """
    Load every persisted daily snapshot. Returns {date: {symbol: {...}}},
    or {} if none exist yet / on error. Tolerant of individual bad/partial
    objects -- one corrupt day is skipped, not fatal to the rest.
    """
    snapshots = {}
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=HISTORY_PREFIX):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith(".json"):
                    continue
                try:
                    resp = s3.get_object(Bucket=bucket, Key=key)
                    data = json.loads(resp["Body"].read())
                    date = data.get("date")
                    symbols = data.get("symbols")
                    if date and isinstance(symbols, dict):
                        snapshots[date] = symbols
                except Exception as e:
                    logger.debug(f"Signal history: skipping unreadable {key}: {e}")
    except Exception as e:
        logger.warning(f"Signal history load failed: {e}")
    return snapshots


# ---- Test ----
if __name__ == "__main__":
    from logger import setup_logging
    setup_logging()

    class _FakeS3:
        """Minimal in-memory stand-in so this can be smoke-tested with no AWS creds."""
        def __init__(self):
            self.objects = {}

        def put_object(self, Bucket, Key, Body, ContentType=None):
            self.objects[Key] = Body

        def get_paginator(self, name):
            objs = self.objects

            class _Pager:
                def paginate(self, Bucket, Prefix):
                    yield {"Contents": [{"Key": k} for k in objs if k.startswith(Prefix)]}
            return _Pager()

        def get_object(self, Bucket, Key):
            class _Body:
                def __init__(self, data):
                    self._data = data

                def read(self):
                    return self._data
            return {"Body": _Body(self.objects[Key])}

    s3 = _FakeS3()
    fake_results = {"SCOM": {"latest": {"close": 36.4}}}
    fake_fund = {"SCOM": {"tech_rating": 0.6, "value_traded": 5_000_000}}
    fake_scores = {"SCOM": {"overall": 72, "coverage": 83}}

    n = write_daily_snapshot(s3, "fake-bucket", fake_results, fake_fund, fake_scores, date="2026-07-31")
    print(f"wrote {n} symbols")
    print(load_all_snapshots(s3, "fake-bucket"))
