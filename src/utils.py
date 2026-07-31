"""
Shared utilities: retry decorator, helpers.
"""

import os
import time
import functools
import logging
from datetime import datetime
from typing import Tuple, List

logger = logging.getLogger(__name__)

# ---- Shared HTTP policy ----
# Tuned so a dead source fails within ~10 seconds worst-case (one retry),
# instead of the earlier 20-25s hangs users saw when afx or CBK were down.
#   connect_timeout: how long to wait to establish the TCP/TLS connection.
#     3s is generous for healthy networks; a source not answering by then is
#     unreachable.
#   read_timeout:    how long to wait for the response body after connecting.
#     6s covers slow-but-alive servers.
DEFAULT_HTTP_TIMEOUT: Tuple[int, int] = (3, 6)


def http_get(url, *, headers=None, timeout=DEFAULT_HTTP_TIMEOUT, retries=1,
             backoff=0.5, allow_redirects=True):
    """
    GET a URL with a tight timeout policy and one quick retry.

    - Fails fast (no long hangs when a source is down).
    - On timeout/connection error, waits `backoff` seconds and retries once
      (this covers most transient DNS/network hiccups).
    - Never raises to the caller — returns the requests.Response on success
      or None on any failure. Callers already treat failure as "skip this
      source"; this keeps that behaviour but does it quickly.
    """
    import requests
    from requests.exceptions import RequestException

    attempts = max(1, retries + 1)
    last_err = None
    for i in range(attempts):
        try:
            r = requests.get(url, headers=headers or {}, timeout=timeout,
                             allow_redirects=allow_redirects)
            return r
        except RequestException as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(backoff)
    logger.warning(f"http_get failed for {url}: {last_err}")
    return None

CACHE_MARKER = ".cache_date"


def enforce_daily_cache(data_dir, reports_dir=None):
    """
    Guarantee cache freshness by calendar day.

    - Same day: the data cache is kept and reused (fast repeat runs).
    - New day (or first ever run): ALL cached data files are deleted first, so
      the first run of a new day never reuses yesterday's data — it fetches
      everything fresh.
    - Reports are cleared on every run (they are always regenerated).

    The history/ subdirectory (long-term daily log) and the marker file are
    preserved. Returns True if a new-day wipe happened, else False.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    is_new_day = True

    # Always clear the reports folder (fresh reports every run).
    if reports_dir and os.path.isdir(reports_dir):
        for name in os.listdir(reports_dir):
            path = os.path.join(reports_dir, name)
            if os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    if not data_dir:
        return False
    os.makedirs(data_dir, exist_ok=True)

    marker = os.path.join(data_dir, CACHE_MARKER)
    if os.path.exists(marker):
        try:
            with open(marker) as f:
                is_new_day = f.read().strip() != today
        except OSError:
            is_new_day = True

    if is_new_day:
        removed = 0
        for name in os.listdir(data_dir):
            if name in ("history", CACHE_MARKER):
                continue  # preserve the long-term log and the marker
            path = os.path.join(data_dir, name)
            if os.path.isfile(path):
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    pass
        try:
            with open(marker, "w") as f:
                f.write(today)
        except OSError:
            pass
        logger.info(
            f"New day ({today}) — cleared {removed} stale cache files; "
            f"this run fetches fresh data."
        )
    else:
        logger.info(f"Same day ({today}) — reusing cached data where available.")

    return is_new_day


def retry(max_attempts=3, backoff=2, exceptions=(Exception,)):
    """
    Decorator that retries a function on failure with exponential backoff.

    Args:
        max_attempts: Maximum number of attempts (including the first).
        backoff: Multiplier for sleep between retries (seconds).
        exceptions: Tuple of exception types to catch.

    Usage:
        @retry(max_attempts=3, backoff=2)
        def flaky_network_call():
            ...
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if attempt < max_attempts:
                        wait = backoff ** (attempt - 1)
                        logger.warning(
                            f"{func.__name__} attempt {attempt}/{max_attempts} "
                            f"failed: {e}. Retrying in {wait}s..."
                        )
                        time.sleep(wait)
                    else:
                        logger.error(
                            f"{func.__name__} failed after {max_attempts} "
                            f"attempts: {e}"
                        )
            raise last_exc
        return wrapper
    return decorator


def safe_float(value, default=0.0):
    """Convert a value to float, returning default on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def detect_support_resistance(
    prices, window=20, threshold=0.02
) -> Tuple[List[float], List[float]]:
    """
    Detect support and resistance levels from a price series.

    Args:
        prices: Array-like of closing prices.
        window: Rolling window size for finding local extrema.
        threshold: Percentage threshold for clustering nearby levels.

    Returns:
        Tuple of (support_levels, resistance_levels).
    """
    import numpy as np
    prices = np.asarray(prices)

    supports = []
    resistances = []

    for i in range(window, len(prices) - window):
        # Local minimum (support)
        if all(prices[i] <= prices[i - window:i]) and \
           all(prices[i] <= prices[i + 1:i + window + 1]):
            supports.append(float(prices[i]))

        # Local maximum (resistance)
        if all(prices[i] >= prices[i - window:i]) and \
           all(prices[i] >= prices[i + 1:i + window + 1]):
            resistances.append(float(prices[i]))

    # Cluster nearby levels
    supports = _cluster_levels(supports, threshold)
    resistances = _cluster_levels(resistances, threshold)

    return supports, resistances


def _cluster_levels(levels, threshold):
    """Cluster nearby price levels together."""
    if not levels:
        return []
    levels = sorted(set(levels))
    clusters = []
    current_cluster = [levels[0]]
    for level in levels[1:]:
        if abs(level - current_cluster[-1]) / current_cluster[-1] < threshold:
            current_cluster.append(level)
        else:
            clusters.append(sum(current_cluster) / len(current_cluster))
            current_cluster = [level]
    clusters.append(sum(current_cluster) / len(current_cluster))
    return [round(c, 2) for c in clusters]