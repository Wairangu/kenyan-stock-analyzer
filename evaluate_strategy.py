#!/usr/bin/env python3
"""Read saved point-in-time decisions and print prospective results as JSON.

Two independent views of the same snapshots:

  track_record            what the five-name model portfolio actually returned
  information_coefficient whether a higher score predicted a higher forward
                          return across the whole eligible cross-section

The second accumulates evidence far faster, because each observation uses every
name rather than five. See docs/strategy.md.
"""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from signal_history import load_local_snapshots
from track_record import compute_track_record
from information_coefficient import (compute_ic_decay, snapshots_from_archive,
                                     DEFAULT_HORIZONS)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--history', default='data/history')
    parser.add_argument('--horizons', type=int, nargs='+', default=[20, 60, 120],
                        help='Portfolio holding horizons in NSE sessions. '
                             'Short horizons rarely clear the round trip.')
    parser.add_argument('--ic-horizons', type=int, nargs='+', default=list(DEFAULT_HORIZONS),
                        help='Horizons for the information coefficient. Short ones '
                             'give independent observations sooner, so the decay '
                             'profile across all of them is the informative part.')
    parser.add_argument('--slippage', type=float, default=.001,
                        help='Assumed price slippage per side (decimal fraction)')
    parser.add_argument('--observations', action='store_true',
                        help='Include the per-date IC series, not just the summaries')
    parser.add_argument('--archive-bucket', metavar='S3_BUCKET',
                        help='Also measure the archived market_summary reports in this '
                             'bucket. Those carry the local bullish/bearish call, not a '
                             'factor score, and their prices were never independently '
                             'cross-checked -- a diagnostic on mined history, not a '
                             'measurement of the current strategy.')
    args = parser.parse_args()

    snapshots = load_local_snapshots(args.history)
    ic = compute_ic_decay(snapshots, horizons=args.ic_horizons)
    archive_ic = None
    if args.archive_bucket:
        import boto3
        from report_archive import fetch_market_summary_snapshots
        mined = snapshots_from_archive(
            fetch_market_summary_snapshots(boto3.client('s3'), args.archive_bucket))
        archive_ic = compute_ic_decay(mined, horizons=args.ic_horizons,
                                      verified_only=False)
    if not args.observations:
        for record in list(ic.values()) + list((archive_ic or {}).values()):
            record.pop('observations', None)
    payload = {
        'track_record': {h: compute_track_record(snapshots, horizon_days=h,
                                                 slippage_pct=args.slippage)
                         for h in args.horizons},
        'information_coefficient': ic,
    }
    if archive_ic is not None:
        payload['archived_signal_information_coefficient'] = archive_ic
    print(json.dumps(payload, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
