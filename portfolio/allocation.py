"""Deterministic whole-share allocations, including fees and existing exposure."""

from decimal import Decimal, ROUND_DOWN, ROUND_UP
import math


def _number(value):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def allocate_budget(candidates, budget_kes, top_n=5, *, fee_pct=0.015,
                    holdings=None, max_stock_weight=0.20, max_sector_weight=0.40):
    """Target equal new allocations, constrained by post-contribution exposure.

    Caps are conservative policy defaults, not an optimized portfolio. Unused
    capacity stays in cash. holdings maps symbols to market_value and sector;
    unpriced open positions block allocation because exposure is unknown.
    """
    budget = _number(budget_kes)
    fee = _number(fee_pct)
    if budget is None or budget <= 0 or fee is None or not 0 <= fee < 1:
        raise ValueError("Budget and fee must be finite, with positive budget and fee in [0, 1).")
    if not 0 < max_stock_weight <= 1 or not 0 < max_sector_weight <= 1 or top_n < 1:
        raise ValueError("Invalid allocation limits")
    cent = Decimal('0.01')
    remaining = Decimal(str(budget)).quantize(cent, rounding=ROUND_DOWN)
    initial = remaining
    fee_rate = Decimal(str(fee))
    values, sectors = {}, {}
    for symbol, holding in (holdings or {}).items():
        if holding.get('qty', 1) <= 0:
            continue
        value = _number(holding.get('market_value'))
        if value is None or value < 0:
            raise ValueError(f"Current value missing for holding {symbol}")
        values[symbol] = Decimal(str(value))
        sector = holding.get('sector') or 'Unknown'
        sectors[sector] = sectors.get(sector, Decimal(0)) + values[symbol]
    total = initial + sum(values.values(), Decimal(0))
    stock_limit = total * Decimal(str(max_stock_weight))
    sector_limit = total * Decimal(str(max_sector_weight))
    selected, seen = [], set()
    for candidate in candidates:
        price = _number(candidate.get('price'))
        symbol = candidate.get('symbol')
        if not symbol or symbol in seen or price is None or price <= 0:
            continue
        seen.add(symbol)
        sector = candidate.get('sector') or 'Unknown'
        p = Decimal(str(price))
        room = min(stock_limit - values.get(symbol, Decimal(0)),
                   sector_limit - sectors.get(sector, Decimal(0)), remaining)
        if p * (1 + fee_rate) <= room:
            selected.append((candidate, p, sector))
        if len(selected) == top_n:
            break
    if not selected:
        return [], float(initial)

    allocations = []
    # Never relax concentration caps just to exhaust a budget.
    target = initial / len(selected)
    for candidate, price, sector in selected:
        symbol = candidate['symbol']
        room = max(Decimal(0), min(target, remaining,
                    stock_limit - values.get(symbol, Decimal(0)),
                    sector_limit - sectors.get(sector, Decimal(0))))
        shares = int(room / (price * (1 + fee_rate)))
        # If the equal slice cannot buy a share, use available capacity. This
        # avoids discarding every individually affordable candidate at once.
        if shares == 0:
            capacity = min(remaining, stock_limit - values.get(symbol, Decimal(0)),
                           sector_limit - sectors.get(sector, Decimal(0)))
            shares = 1 if price * (1 + fee_rate) <= capacity else 0
        amount = (shares * price).quantize(cent, rounding=ROUND_UP)
        fees = (amount * fee_rate).quantize(cent, rounding=ROUND_UP)
        capacity = min(remaining, stock_limit - values.get(symbol, Decimal(0)),
                       sector_limit - sectors.get(sector, Decimal(0)))
        if amount + fees > capacity:
            shares = max(0, shares - 1)
            amount = (shares * price).quantize(cent, rounding=ROUND_UP)
            fees = (amount * fee_rate).quantize(cent, rounding=ROUND_UP)
        if shares <= 0:
            continue
        cash = amount + fees
        remaining -= cash
        values[symbol] = values.get(symbol, Decimal(0)) + amount
        sectors[sector] = sectors.get(sector, Decimal(0)) + amount
        allocations.append({**candidate, 'shares': shares, 'price': float(price),
                            'allocated_kes': float(amount), 'fees_kes': float(fees),
                            'cash_required_kes': float(cash)})
    return allocations, float(remaining)
