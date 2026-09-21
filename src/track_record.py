"""Prospective model-portfolio evaluation, separate from legacy signal diagnostics.

Version-2 records freeze the actual screened allocation. Historical tier labels
cannot be retroactively treated as portfolios. Prices alone cannot establish
total returns: these diagnostics explicitly exclude dividends/corporate actions.
"""

from datetime import timedelta
from data_quality import finite_number, parse_date, is_session

DEFAULT_TIER_FIELD = "tv_class"
DEFAULT_TIERS = ("strong_buy", "buy")


def _next_session(day, steps=1):
    for _ in range(steps):
        day += timedelta(days=1)
        while not is_session(day):
            day += timedelta(days=1)
    return day.isoformat()


def _summary(returns):
    return {"n": len(returns),
            "hit_rate": round(100 * sum(r > 0 for r in returns) / len(returns), 1) if returns else None,
            "avg_return_pct": round(sum(returns) / len(returns), 2) if returns else None}


def compute_track_record(snapshots_by_date, tier_field=DEFAULT_TIER_FIELD,
                         tiers=DEFAULT_TIERS, horizon_days=10, *,
                         selected_only=True, slippage_pct=0.001):
    """Evaluate non-overlapping decisions at next-session close, after costs.

    Missing execution/exit quotes invalidate the entire selected period rather
    than selectively dropping losing/delisted constituents. Snapshot gaps do
    not change the requested NSE-session horizon. Legacy mode is explicitly an
    overlapping price-signal diagnostic, never presented as a portfolio.
    """
    if not isinstance(horizon_days, int) or horizon_days < 1:
        raise ValueError("horizon_days must be a positive integer")
    slip = finite_number(slippage_pct)
    if slip is None or not 0 <= slip < 1:
        raise ValueError("Invalid slippage")
    dates = sorted(d for d in snapshots_by_date if parse_date(d) and is_session(parse_date(d)))
    returns = {tier: [] for tier in tiers}
    benchmark = []
    periods = []
    missing_periods = 0
    last_exit = None
    for signal_date in dates:
        if selected_only and last_exit and signal_date < last_exit:
            continue
        records = snapshots_by_date[signal_date]
        chosen = {s: r for s, r in records.items()
                  if (r.get("schema_version") == 2 and r.get("selected") is True)
                  or (not selected_only and r.get(tier_field) in tiers)}
        if not chosen:
            continue
        entry_date = _next_session(parse_date(signal_date))
        exit_date = _next_session(parse_date(entry_date), horizon_days)
        if exit_date > dates[-1]:
            continue  # outstanding period, not a failure
        if selected_only:
            last_exit = exit_date  # reserve the horizon even if marks are missing
        entries = snapshots_by_date.get(entry_date, {})
        exits = snapshots_by_date.get(exit_date, {})

        def quote(symbol, rows):
            rec = rows.get(symbol, {})
            price = finite_number(rec.get("price"))
            expected_date = entry_date if rows is entries else exit_date
            if selected_only and (rec.get("price_verified") is not True
                                  or rec.get("price_date") != expected_date):
                return None
            return price if price is not None and price > 0 else None

        if any(quote(s, entries) is None or quote(s, exits) is None for s in chosen):
            missing_periods += 1
            continue
        first = next(iter(chosen.values()))
        budget = finite_number(first.get("model_budget_kes")) if selected_only else None
        fee = finite_number(first.get("fee_pct", 0.015)) if selected_only else 0.0
        if selected_only and (budget is None or budget <= 0 or fee is None or not 0 <= fee < 1):
            missing_periods += 1
            continue
        cash = budget or 0.0
        proceeds = 0.0
        bought = []
        pending = []
        for symbol, rec in sorted(chosen.items(), key=lambda item: (item[1].get('allocation_rank') or 0, item[0])):
            entry = quote(symbol, entries) * (1 + slip if selected_only else 1)
            exit_price = quote(symbol, exits) * (1 - slip if selected_only else 1)
            ret = (exit_price * (1 - fee) / (entry * (1 + fee)) - 1) * 100
            if selected_only:
                requested = finite_number(rec.get("allocation_shares"))
                if requested is None or requested <= 0:
                    continue
                shares = min(int(requested), int(cash / (entry * (1 + fee))))
                if shares <= 0:
                    continue
                cash -= shares * entry * (1 + fee)
                proceeds += shares * exit_price * (1 - fee)
                bought.append(symbol)
            pending.append((rec.get(tier_field), ret))
        if selected_only and not bought:
            continue
        for tier, ret in pending:
            if tier in returns:
                returns[tier].append(ret)

        universe = [s for s, r in records.items() if r.get("investable")] if selected_only else list(records)
        # Same decision dates, entry convention and costs as the portfolio.
        b_rets = []
        for symbol in universe:
            entry, end = quote(symbol, entries), quote(symbol, exits)
            if entry is None or end is None:
                b_rets = []
                break
            b_rets.append((end * (1 - slip if selected_only else 1) * (1 - fee)
                           / (entry * (1 + slip if selected_only else 1) * (1 + fee)) - 1) * 100)
        b_return = sum(b_rets) / len(b_rets) if b_rets else None
        if b_return is not None:
            benchmark.append(b_return)
        if selected_only:
            periods.append({"signal_date": signal_date, "entry_date": entry_date, "exit_date": exit_date,
                            "return_pct": round((cash + proceeds) / budget * 100 - 100, 4),
                            "benchmark_return_pct": b_return, "symbols": bought})
    model = _summary([p["return_pct"] for p in periods])
    model["periods"] = periods
    model["incomplete_periods"] = missing_periods
    mode = "Screened model portfolio" if selected_only else "Legacy signal diagnostic (overlapping observations)"
    note = (f"{mode}. Next-session close entry; {horizon_days} NSE sessions held. "
            + ("Includes recorded fees and assumed " + str(slip * 100) + "% slippage per side. "
               if selected_only else "Gross price changes, excluding trading costs. ")
            + "Price returns only: dividends and corporate actions are not available. "
            + "Sample counts do not establish statistical confidence. "
            + f"{missing_periods} completed period(s) excluded for missing data.")
    return {"horizon_days": horizon_days, "as_of": dates[-1] if dates else None,
            "tiers": {tier: _summary(values) for tier, values in returns.items()},
            "benchmark": _summary(benchmark), "portfolio": model,
            "selected_only": selected_only, "note": note}
