#!/usr/bin/env python3
"""Read saved point-in-time decisions and print prospective results as JSON."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from signal_history import load_local_snapshots
from track_record import compute_track_record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', default='data/history')
    parser.add_argument('--horizons', type=int, nargs='+', default=[5, 10, 20])
    parser.add_argument('--slippage', type=float, default=.001,
                        help='Assumed price slippage per side (decimal fraction)')
    args = parser.parse_args()
    snapshots = load_local_snapshots(args.history)
    results = {h: compute_track_record(snapshots, horizon_days=h, slippage_pct=args.slippage)
               for h in args.horizons}
    print(json.dumps(results, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
