"""Immutable daily decisions shared by local reports and the deployed analyzer."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

try:
    from botocore.exceptions import ClientError, ParamValidationError
except ImportError:  # local runs without boto3
    class ClientError(Exception):
        response = {}
    class ParamValidationError(Exception):
        pass

from fundamental_analysis import FundamentalAnalysis
from logger import get_logger
from data_quality import latest_completed_session, finite_number
from portfolio.allocation import allocate_budget
from recommender import STRATEGY_VERSION

logger = get_logger(__name__)
HISTORY_PREFIX = "history/"


def _snapshot_key(date):
    return f"{HISTORY_PREFIX}signals_v2_{date}.json"


def build_daily_snapshot(analysis_results, fundamentals_data=None, scores=None,
                         date=None, candidates=None, validations=None):
    date = date or latest_completed_session().isoformat()
    fundamentals_data, scores, validations = fundamentals_data or {}, scores or {}, validations or {}
    fee = float(os.environ.get('TRANSACTION_FEE_PCT', '0.015'))
    budget = 100_000
    allocations, cash = allocate_budget(candidates or [], budget, fee_pct=fee)
    selected = {a['symbol']: {**a, 'rank': i} for i, a in enumerate(allocations)}
    symbols = {}
    for symbol, result in (analysis_results or {}).items():
        if not result:
            continue
        fund = fundamentals_data.get(symbol, {})
        score = scores.get(symbol, {})
        label, tier = FundamentalAnalysis.signal_from_tech_rating(fund.get('tech_rating'))
        symbols[symbol] = {
            'price': finite_number(result.get('latest', {}).get('close')),
            'tv_class': tier, 'tv_label': label,
            'score': finite_number(score.get('overall')),
            'score_coverage': finite_number(score.get('coverage')),
            'value_traded': finite_number(fund.get('value_traded')),
            'schema_version': 2, 'strategy_version': STRATEGY_VERSION,
            'selected': symbol in selected,
            'allocation_shares': selected.get(symbol, {}).get('shares', 0),
            'allocation_rank': selected.get(symbol, {}).get('rank'),
            'model_budget_kes': budget, 'fee_pct': fee,
            'sector': fund.get('sector') or 'Unknown',
            'price_date': result.get('history_date'),
            'price_verified': (validations.get(symbol, {}).get('status') == 'ok'
                               and validations.get(symbol, {}).get('is_stale') is False
                               and result.get('history_date') == date),
            'investable': (result.get('identity_verified', False)
                           and result.get('history_complete', False)
                           and (result.get('median_value_traded_20d') or 0) >= 1_000_000),
        }
    return {'date': date, 'symbols': symbols, 'strategy_version': STRATEGY_VERSION,
            'schema_version': 2, 'model_cash_kes': cash,
            'generated_at': datetime.now(timezone.utc).isoformat()}


def write_daily_snapshot(s3, bucket, analysis_results, fundamentals_data=None,
                         scores=None, date=None, candidates=None, validations=None):
    """Preserve the first decision of each date; never rewrite history on reruns."""
    try:
        payload = build_daily_snapshot(analysis_results, fundamentals_data, scores,
                                       date, candidates, validations)
        if not payload['symbols']:
            return 0
        key = _snapshot_key(payload['date'])
        body = json.dumps(payload, allow_nan=False).encode()
        try:
            s3.put_object(Bucket=bucket, Key=key, Body=body,
                          ContentType='application/json', IfNoneMatch='*')
        except ParamValidationError:
            # Older bundled boto3 without S3 conditional writes: check first.
            try:
                s3.head_object(Bucket=bucket, Key=key)
                logger.info('Existing model snapshot preserved')
                return 0
            except ClientError as exc:
                if exc.response.get('Error', {}).get('Code') not in ('404', 'NoSuchKey', 'NotFound'):
                    raise
            s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType='application/json')
        return len(payload['symbols'])
    except Exception as exc:
        code = getattr(exc, 'response', {}).get('Error', {}).get('Code')
        if code in ('PreconditionFailed', '412'):
            logger.info('Existing model snapshot preserved')
        else:
            logger.warning(f'Signal history write failed: {exc}')
        return 0


def write_local_snapshot(directory, *args, **kwargs):
    payload = build_daily_snapshot(*args, **kwargs)
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / Path(_snapshot_key(payload['date'])).name
    body = json.dumps(payload, indent=2, allow_nan=False)
    try:
        with path.open('x') as stream:
            stream.write(body)
    except FileExistsError:
        pass
    return path


def _merge_snapshot(snapshots, data):
    date, symbols = data.get('date'), data.get('symbols')
    if not date or not isinstance(symbols, dict):
        return
    def version(rows):
        return max((r.get('schema_version', 1) for r in rows.values()), default=0)
    if date not in snapshots or version(symbols) > version(snapshots[date]):
        snapshots[date] = symbols


def load_local_snapshots(directory):
    snapshots = {}
    for path in sorted(Path(directory).glob('signals_*.json')):
        try:
            _merge_snapshot(snapshots, json.loads(path.read_text()))
        except (ValueError, OSError) as exc:
            logger.warning(f'Ignoring unreadable snapshot {path.name}: {exc}')
    return snapshots


def load_all_snapshots(s3, bucket):
    snapshots = {}
    try:
        for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=HISTORY_PREFIX):
            for obj in page.get('Contents', []):
                key = obj['Key']
                if not key.endswith('.json'):
                    continue
                try:
                    data = json.loads(s3.get_object(Bucket=bucket, Key=key)['Body'].read())
                    _merge_snapshot(snapshots, data)
                except Exception as exc:
                    logger.warning(f'Ignoring unreadable snapshot {key}: {exc}')
    except Exception as exc:
        logger.warning(f'Signal history load failed: {exc}')
    return snapshots
