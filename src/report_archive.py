"""
Parses the legacy market_summary_<date>_<time>.html archive already
sitting in the reports S3 bucket into the same {date: {symbol: {...}}}
shape signal_history.load_all_snapshots() returns, so
track_record.compute_track_record() can measure it too.

This is a *different* signal from the literal TradingView tv_class
(signal_history.py/track_record.py's usual subject): analysis_engine.py's
own bullish/bearish `Overall` technical call, computed locally from
RSI/MA-crossover/MACD/trend -- not TradingView's live Recommend.All
scanner. It's mined here only because ~7 weeks of it already exist in S3
(the market_summary reports were being generated daily before the
tv_class history-write existed), giving an immediate, real (not
reconstructed) sample instead of waiting weeks for tv_class history to
accumulate.

Takes an already-constructed boto3 S3 client rather than creating one,
matching signal_history.py's style -- stays testable without AWS
credentials.
"""

import re
from datetime import datetime

from logger import get_logger

logger = get_logger(__name__)

ARCHIVE_PREFIX = "market_summary_"
_KEY_RE = re.compile(r"^market_summary_(\d{8})_(\d{6})\.html$")
_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _select_one_key_per_date(keys):
    """
    Group market_summary_<YYYYMMDD>_<HHMMSS>.html keys by date, keeping
    only the earliest-timestamped key per date -- that's the scheduled
    run; later same-day keys are ad hoc reruns (seen on the archive's
    first day, 2026-07-30, which has a dozen dev-testing runs).
    """
    best = {}
    for key in keys:
        m = _KEY_RE.match(key)
        if not m:
            continue
        date_str, time_str = m.groups()
        date = f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}"
        if date not in best or time_str < best[date][0]:
            best[date] = (time_str, key)
    return {date: key for date, (_, key) in best.items()}


def _parse_technical_table(html):
    """
    Extract the {Symbol, Price, Change, RSI, MA Signal, MACD, Trend,
    Overall} table market_summary reports render -- rows with exactly 8
    <td> cells, header anchored on the "Overall" column so this doesn't
    accidentally match an unrelated table on the page.
    """
    idx = html.find("<th>Overall</th>")
    if idx == -1:
        return {}
    section = html[max(0, idx - 3000):]
    symbols = {}
    for row in _ROW_RE.findall(section):
        cells = [_TAG_RE.sub("", c).strip() for c in _CELL_RE.findall(row)]
        if len(cells) != 8:
            continue
        symbol, price_str = cells[0], cells[1]
        overall = cells[7].strip().lower()
        try:
            price = float(price_str)
        except ValueError:
            continue
        if overall not in ("bullish", "bearish"):
            continue
        symbols[symbol] = {"price": price, "overall": overall}
    return symbols


def fetch_market_summary_snapshots(s3, bucket):
    """
    Returns {date: {symbol: {price, overall}}} mined from every
    market_summary_*.html object in the bucket (one per calendar date --
    see _select_one_key_per_date). Tolerant of a single bad object (skip
    + log, never raises), matching signal_history.load_all_snapshots.
    """
    snapshots = {}
    try:
        paginator = s3.get_paginator("list_objects_v2")
        keys = []
        for page in paginator.paginate(Bucket=bucket, Prefix=ARCHIVE_PREFIX):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))

        for date, key in _select_one_key_per_date(keys).items():
            try:
                resp = s3.get_object(Bucket=bucket, Key=key)
                html = resp["Body"].read().decode("utf-8", errors="replace")
                symbols = _parse_technical_table(html)
                if symbols:
                    snapshots[date] = symbols
            except Exception as e:
                logger.debug(f"Report archive: skipping unreadable {key}: {e}")
    except Exception as e:
        logger.warning(f"Report archive load failed: {e}")
    return snapshots


# ---- Test ----
if __name__ == "__main__":
    from logger import setup_logging
    setup_logging()

    _FAKE_TABLE_HTML = """
    <table>
        <tr><th>Symbol</th><th>Price</th><th>Change</th><th>RSI</th>
        <th>MA Signal</th><th>MACD</th><th>Trend</th><th>Overall</th></tr>
        <tr><td>SCOM</td><td>36.40</td><td>+0.83%</td><td>71.2</td>
        <td>Bullish</td><td>Bearish</td><td>Bullish</td><td>Bullish</td></tr>
        <tr><td>KCB</td><td>86.00</td><td>+2.08%</td><td>78.0</td>
        <td>Bullish</td><td>Bullish Cross</td><td>Bullish</td><td>Bullish</td></tr>
        <tr><td>NCBA</td><td>90.00</td><td>+0.28%</td><td>51.9</td>
        <td>Bearish</td><td>Bullish</td><td>Bullish</td><td>Bearish</td></tr>
    </table>
    """

    class _FakeS3:
        def __init__(self, objects):
            self.objects = objects

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
            return {"Body": _Body(self.objects[Key].encode())}

    s3 = _FakeS3({
        # 3 runs on the same day -- only the earliest should be kept.
        "market_summary_20260730_195603.html": _FAKE_TABLE_HTML,
        "market_summary_20260730_213724.html": _FAKE_TABLE_HTML,
        "market_summary_20260731_060029.html": _FAKE_TABLE_HTML,
        "index.html": "<html>not a report</html>",
    })

    snapshots = fetch_market_summary_snapshots(s3, "fake-bucket")
    print(snapshots)
    assert sorted(snapshots.keys()) == ["2026-07-30", "2026-07-31"]
    assert snapshots["2026-07-30"]["KCB"] == {"price": 86.0, "overall": "bullish"}
    assert snapshots["2026-07-30"]["NCBA"] == {"price": 90.0, "overall": "bearish"}
    assert len(snapshots["2026-07-30"]) == 3
    print("OK")
