"""
Portfolio tracker Lambda: password-protected, multi-user personal trade
journal. Registration is open -- anyone who reaches the app can create an
account (see the Context/security notes in terraform/portfolio.tf's plan
history for that tradeoff). Each user's credentials and trade history are
fully isolated from every other user's.

Zero third-party dependencies (stdlib + boto3 only -- boto3 is preinstalled
in the Lambda Python runtime). Current prices come from the existing
dashboard Lambda's public prices.json (https://stocks.getkitters.com/prices.json)
rather than a direct TradingView fetch, so this Lambda needs none of the
heavy tvkit/pandas/pyarrow stack the main analyzer requires -- a plain zip
deploy, no container image.

Routes (Lambda Function URL, API-Gateway-v2-style event payload):
  GET  /login              -- login form
  POST /login               -- verify credentials, set session cookie, redirect
  GET  /register            -- account-creation form
  POST /register             -- create an account, log straight in, redirect
  GET  /                    -- portfolio (holdings + trade history), requires session
  POST /trades               -- add a buy/sell/dividend entry, requires session
  POST /trades/{id}/delete   -- remove a trade, requires session
  GET  /change-password      -- change-password form, requires session
  POST /change-password       -- set a new password, requires session
  GET|POST /logout          -- clear session, redirect to /login

Credentials and account metadata live in users.json in the private S3
bucket (not SSM -- SSM Parameter Store's fixed-name-per-parameter model
doesn't extend to an arbitrary number of self-registered users); each
user's trades live in their own trades/<username>.json. A freshly
self-registered account never needs a forced password change (the user
picked their own password at signup) -- must_change_password only applies
to the original Terraform-bootstrapped credential, migrated into
users.json the same way.

Access control is two-layered: the Function URL uses authorization_type
NONE, but CloudFront injects a shared secret header (X-Origin-Verify) on
every request that only it knows, and _origin_verified() rejects anything
missing/mismatching it before any routing happens -- hitting the Function
URL directly, bypassing CloudFront, is rejected immediately (see
terraform/portfolio.tf for why OAC/AWS_IAM signing isn't used here: it's
incompatible with plain HTML form POSTs). Within the app, a signed session
cookie (HMAC-SHA256) gates every route except /login and /register.
"""

import base64
import hashlib
import hmac
import html
import json
import os
import re
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs
from urllib.request import urlopen

import boto3

SESSION_MAX_AGE_SECONDS = 12 * 3600  # 12 hours
COOKIE_NAME = "session"
PBKDF2_ITERATIONS = 200_000
# Approximate broker commission + statutory NSE/CDSC/CMA levies, applied to
# every buy/sell (not dividends) so cost basis and realized gain reflect
# what actually lands in the account, not just the raw quoted price.
TRANSACTION_FEE_PCT = float(os.environ.get('TRANSACTION_FEE_PCT', '0.015'))

_esc = html.escape
_ssm = boto3.client('ssm')
_s3 = boto3.client('s3')
_secrets_cache = {}


def _get_secret(name):
    """Fetch and cache an SSM SecureString for the lifetime of this warm Lambda."""
    if name not in _secrets_cache:
        prefix = os.environ['SSM_PREFIX']
        resp = _ssm.get_parameter(Name=f"{prefix}/{name}", WithDecryption=True)
        _secrets_cache[name] = resp['Parameter']['Value']
    return _secrets_cache[name]


# ---- Session cookie (HMAC-signed, stdlib only) ----

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def _make_session_cookie(username):
    secret = _get_secret('session_secret').encode()
    expiry = int((datetime.now(timezone.utc) + timedelta(seconds=SESSION_MAX_AGE_SECONDS)).timestamp())
    token = _b64url_encode(f"{username}|{expiry}".encode())
    sig = hmac.new(secret, token.encode(), hashlib.sha256).hexdigest()
    return (f"{COOKIE_NAME}={token}.{sig}; HttpOnly; Secure; SameSite=Strict; "
            f"Path=/; Max-Age={SESSION_MAX_AGE_SECONDS}")


def _clear_session_cookie():
    return f"{COOKIE_NAME}=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0"


def _verify_session(event):
    """Return the username if the request carries a valid, unexpired session cookie."""
    value = None
    for c in event.get("cookies") or []:
        if c.strip().startswith(f"{COOKIE_NAME}="):
            value = c.strip()[len(COOKIE_NAME) + 1:]
            break
    if not value or '.' not in value:
        return None
    token, _, sig = value.rpartition('.')
    secret = _get_secret('session_secret').encode()
    expected_sig = hmac.new(secret, token.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None
    try:
        username, expiry = _b64url_decode(token).decode().rsplit('|', 1)
        if int(expiry) < int(datetime.now(timezone.utc).timestamp()):
            return None
        return username
    except Exception:
        # Any malformed-cookie failure mode just means "not logged in".
        return None


_USERNAME_RE = re.compile(r'^[a-zA-Z0-9_-]{3,32}$')


def _valid_username(username):
    return bool(_USERNAME_RE.match(username or ''))


def _hash_password(plaintext):
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac('sha256', plaintext.encode(), salt, PBKDF2_ITERATIONS).hex()
    return f"{salt.hex()}${digest}"


def _verify_password_hash(submitted, stored):
    salt_hex, _, hash_hex = (stored or '').partition('$')
    if not salt_hex or not hash_hex:
        return False
    computed = hashlib.pbkdf2_hmac(
        'sha256', submitted.encode(), bytes.fromhex(salt_hex), PBKDF2_ITERATIONS
    ).hex()
    return hmac.compare_digest(computed, hash_hex)


# ---- User accounts (S3 JSON, tolerant load -- style matches src/foreign_flows.py) ----

def _load_users():
    bucket = os.environ['TRADES_BUCKET']
    key = os.environ['USERS_KEY']
    try:
        resp = _s3.get_object(Bucket=bucket, Key=key)
        users = json.loads(resp['Body'].read()).get('users')
        return users if isinstance(users, dict) else {}
    except _s3.exceptions.NoSuchKey:
        return {}
    except Exception:
        return {}


def _save_users(users):
    _s3.put_object(
        Bucket=os.environ['TRADES_BUCKET'], Key=os.environ['USERS_KEY'],
        Body=json.dumps({"users": users}, indent=2).encode(),
        ContentType="application/json",
    )


def _check_password(username, submitted):
    user = _load_users().get(username)
    if not user:
        # Deliberately skip the PBKDF2 computation for a nonexistent user --
        # this leaks a small username-enumeration timing signal, an
        # acceptable tradeoff at this app's personal/hobby scale.
        return False
    return _verify_password_hash(submitted, user.get('password_hash', ''))


def _must_change_password(username):
    user = _load_users().get(username) or {}
    return bool(user.get('must_change_password'))


def _set_password(username, plaintext):
    users = _load_users()
    user = users.setdefault(username, {})
    user['password_hash'] = _hash_password(plaintext)
    user['must_change_password'] = False
    _save_users(users)


def _register_user(username, plaintext):
    """Create a new account. Caller must have already validated the username
    format and password rules. Returns False if the username is taken."""
    users = _load_users()
    if username in users:
        return False
    users[username] = {
        'password_hash': _hash_password(plaintext),
        'must_change_password': False,  # they chose this password themselves
        'created_at': datetime.now(timezone.utc).isoformat(),
    }
    _save_users(users)
    return True


# ---- Trade storage (S3 JSON, tolerant load -- style matches src/foreign_flows.py) ----

def _load_trades(username):
    bucket = os.environ['TRADES_BUCKET']
    key = f"{os.environ['TRADES_PREFIX']}{username}.json"
    try:
        resp = _s3.get_object(Bucket=bucket, Key=key)
        trades = json.loads(resp['Body'].read()).get('trades')
        return trades if isinstance(trades, list) else []
    except _s3.exceptions.NoSuchKey:
        return []
    except Exception:
        return []


def _save_trades(username, trades):
    _s3.put_object(
        Bucket=os.environ['TRADES_BUCKET'], Key=f"{os.environ['TRADES_PREFIX']}{username}.json",
        Body=json.dumps({"trades": trades}, indent=2).encode(),
        ContentType="application/json",
    )


def _fetch_prices():
    try:
        with urlopen(os.environ['PRICES_URL'], timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return {}


# ---- FIFO cost-basis accounting ----

def _fifo_positions(trades, prices):
    """
    Per symbol, process trades oldest-first with FIFO lot matching: a buy
    opens a new lot, a sell consumes the oldest open lot(s) first,
    realizing gain/loss on each lot consumed. Remaining open lots are the
    current holdings, valued (and their unrealized gain computed) against
    prices.json's current price.

    Returns (positions: {symbol: {...}}, totals: {...}).
    """
    by_symbol = {}
    for t in trades:
        by_symbol.setdefault(t['symbol'], []).append(t)

    positions = {}
    totals = {'cost_basis': 0.0, 'market_value': 0.0, 'unrealized_gain': 0.0, 'realized_gain': 0.0,
              'dividends': 0.0, 'fees': 0.0}

    for symbol, sym_trades in by_symbol.items():
        lots = deque()  # each: [qty, price]
        realized_gain = 0.0
        dividends = 0.0
        fees = 0.0

        for t in sorted(sym_trades, key=lambda x: x['date']):
            qty, price = float(t['quantity']), float(t['price'])
            if t['side'] == 'buy':
                fees += qty * price * TRANSACTION_FEE_PCT
                lots.append([qty, price * (1 + TRANSACTION_FEE_PCT)])
            elif t['side'] == 'sell':
                fees += qty * price * TRANSACTION_FEE_PCT
                net_price = price * (1 - TRANSACTION_FEE_PCT)
                remaining = qty
                while remaining > 1e-9 and lots:
                    lot_qty, lot_price = lots[0]
                    consumed = min(lot_qty, remaining)
                    realized_gain += consumed * (net_price - lot_price)
                    remaining -= consumed
                    if lot_qty - consumed <= 1e-9:
                        lots.popleft()
                    else:
                        lots[0][0] = lot_qty - consumed
                # A sell exceeding recorded buys (data-entry error) is not
                # modeled as a short position -- the excess is just ignored.
            elif t['side'] == 'dividend':
                # Not part of FIFO lot matching -- qty*price here is shares
                # held x per-share payout, tracked purely as income.
                dividends += qty * price

        open_qty = sum(l[0] for l in lots)
        cost_basis = sum(l[0] * l[1] for l in lots)
        current_price = (prices.get(symbol) or {}).get('price')
        has_position = open_qty > 1e-9
        market_value = current_price * open_qty if (has_position and current_price is not None) else None
        unrealized_gain = (market_value - cost_basis) if market_value is not None else None
        unrealized_pct = (unrealized_gain / cost_basis * 100) if (unrealized_gain is not None and cost_basis > 1e-9) else None

        totals['dividends'] += dividends
        totals['fees'] += fees

        if has_position or abs(realized_gain) > 1e-9 or abs(dividends) > 1e-9:
            positions[symbol] = {
                'symbol': symbol,
                'qty': round(open_qty, 4),
                'avg_cost': round(cost_basis / open_qty, 2) if has_position else None,
                'cost_basis': round(cost_basis, 2),
                'current_price': current_price,
                'market_value': round(market_value, 2) if market_value is not None else None,
                'unrealized_gain': round(unrealized_gain, 2) if unrealized_gain is not None else None,
                'unrealized_pct': round(unrealized_pct, 2) if unrealized_pct is not None else None,
                'realized_gain': round(realized_gain, 2),
                'dividends': round(dividends, 2),
                'fees': round(fees, 2),
            }
            totals['cost_basis'] += cost_basis
            totals['market_value'] += market_value or 0.0
            totals['unrealized_gain'] += unrealized_gain or 0.0
            totals['realized_gain'] += realized_gain

    totals = {k: round(v, 2) for k, v in totals.items()}
    totals['unrealized_pct'] = (
        round(totals['unrealized_gain'] / totals['cost_basis'] * 100, 2)
        if totals['cost_basis'] > 1e-9 else None
    )
    return positions, totals


# ---- HTML (hand-written f-strings, matching src/email_notifier.py's style -- no templating engine) ----

_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color:#f5f7fb; color:#172033; margin:0; padding:20px; }
.container { max-width: 900px; margin: 0 auto; }
h1 { margin:0 0 6px; font-size:1.5rem; font-weight:800; }
h2 { font-size:1rem; font-weight:800; margin:0 0 14px; }
.header { text-align:center; background-color:#0f172a; background-image:linear-gradient(135deg,#0f172a 0%,#115e59 55%,#b45309 100%); color:#fff; padding:26px 20px; border-radius:18px; margin-bottom:20px; position:relative; }
.header h1 { color:#fff; }
.header .logout { position:absolute; top:14px; right:16px; color:#fff; font-size:0.78rem; opacity:0.85; text-decoration:none; }
.card { background-color:#fff; border:1px solid rgba(148,163,184,0.28); border-radius:16px; padding:18px 20px; margin-bottom:16px; }
.stats { display:flex; gap:10px; flex-wrap:wrap; }
.stat { background-color:#f5f7fb; padding:12px 14px; border-radius:12px; text-align:center; flex:1; min-width:130px; border:1px solid rgba(148,163,184,0.22); }
.stat .big { font-size:1.25rem; font-weight:800; }
.stat .label { font-size:0.66rem; color:#667085; text-transform:uppercase; font-weight:700; margin-top:2px; }
.bullish { color:#12b981; } .bearish { color:#ef4444; } .dividend { color:#0ea5e9; } .fee { color:#f59e0b; }
table { width:100%; border-collapse:collapse; font-size:0.85rem; }
th, td { padding:8px 10px; text-align:left; border-bottom:1px solid rgba(148,163,184,0.28); }
th { background-color:#f5f7fb; color:#667085; font-size:0.66rem; text-transform:uppercase; font-weight:800; }
td strong { color:#0f172a; }
label { display:block; font-size:0.8rem; font-weight:700; color:#667085; margin-bottom:4px; }
input, select, button { font-size:0.9rem; padding:8px 10px; border-radius:8px; border:1px solid rgba(148,163,184,0.4); box-sizing:border-box; }
form.grid { display:flex; gap:12px; flex-wrap:wrap; align-items:flex-end; }
form.grid p { margin:0; }
button { background-color:#2563eb; color:#fff; border:none; font-weight:700; cursor:pointer; }
button.danger { background-color:#ef4444; padding:5px 9px; font-size:0.78rem; }
form.inline { display:inline; margin:0; }
.error { color:#ef4444; font-weight:600; }
a { color:#2563eb; }
"""


def _page(title, body):
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        f'<title>{_esc(title)}</title><style>{_CSS}</style></head>'
        f'<body><div class="container">{body}</div></body></html>'
    )


def _render_login(error=None):
    error_html = f'<p class="error">{_esc(error)}</p>' if error else ''
    body = f"""
    <div class="header"><h1>🔒 Portfolio Login</h1></div>
    <div class="card">
      {error_html}
      <form method="POST" action="/login">
        <p><label>Username</label><input name="username" required autofocus></p>
        <p><label>Password</label><input type="password" name="password" required></p>
        <p><button type="submit">Log in</button></p>
      </form>
      <p><a href="/register">Don't have an account? Register</a></p>
    </div>"""
    return _page("Portfolio — Login", body)


def _render_register(error=None):
    error_html = f'<p class="error">{_esc(error)}</p>' if error else ''
    body = f"""
    <div class="header"><h1>📝 Create Account</h1></div>
    <div class="card">
      {error_html}
      <form method="POST" action="/register">
        <p><label>Username</label><input name="username" required autofocus
           pattern="[a-zA-Z0-9_-]{{3,32}}" title="3-32 characters: letters, numbers, underscore, hyphen"></p>
        <p><label>Password</label><input type="password" name="password" required minlength="8"></p>
        <p><label>Confirm password</label><input type="password" name="confirm_password" required minlength="8"></p>
        <p><button type="submit">Create account</button></p>
      </form>
      <p><a href="/login">&larr; Back to login</a></p>
    </div>"""
    return _page("Portfolio — Register", body)


def _render_change_password(forced=False, error=None):
    error_html = f'<p class="error">{_esc(error)}</p>' if error else ''
    intro = (
        '<p>This is a first-time or reset credential — choose a new password before continuing.</p>'
        if forced else ''
    )
    cancel_link = '' if forced else '<p><a href="/">&larr; Cancel</a></p>'
    body = f"""
    <div class="header"><h1>🔑 Change Password</h1></div>
    <div class="card">
      {intro}
      {error_html}
      <form method="POST" action="/change-password">
        <p><label>New password</label><input type="password" name="new_password" required minlength="8"></p>
        <p><label>Confirm new password</label><input type="password" name="confirm_password" required minlength="8"></p>
        <p><button type="submit">Set password</button></p>
      </form>
      {cancel_link}
    </div>"""
    return _page("Portfolio — Change Password", body)


def _render_error(message):
    body = f"""
    <div class="header"><h1>⚠️ Error</h1></div>
    <div class="card"><p class="error">{_esc(message)}</p><p><a href="/">&larr; Back to portfolio</a></p></div>"""
    return _page("Portfolio — Error", body)


def _render_dashboard(positions, totals, trades):
    holdings_rows = ''
    for p in sorted(positions.values(), key=lambda x: x['symbol']):
        avg_cost_str = f"{p['avg_cost']:.2f}" if p['avg_cost'] is not None else '—'
        price_str = f"{p['current_price']:.2f}" if p['current_price'] is not None else '—'
        mv_str = f"{p['market_value']:.2f}" if p['market_value'] is not None else '—'
        if p['unrealized_gain'] is not None:
            u_cls = 'bullish' if p['unrealized_gain'] >= 0 else 'bearish'
            u_str = f"{p['unrealized_gain']:+.2f} ({p['unrealized_pct']:+.1f}%)"
        else:
            u_cls, u_str = '', '—'
        r_cls = 'bullish' if p['realized_gain'] >= 0 else 'bearish'
        holdings_rows += (
            f'<tr><td><strong>{_esc(p["symbol"])}</strong></td><td>{p["qty"]:g}</td>'
            f'<td>{avg_cost_str}</td><td>{price_str}</td><td>{mv_str}</td>'
            f'<td class="{u_cls}">{u_str}</td>'
            f'<td class="{r_cls}">{p["realized_gain"]:+.2f}</td>'
            f'<td class="dividend">{p["dividends"]:.2f}</td></tr>'
        )
    if not holdings_rows:
        holdings_rows = '<tr><td colspan="8">No trades recorded yet.</td></tr>'

    trade_rows = ''
    for t in sorted(trades, key=lambda x: x['date'], reverse=True):
        side_cls = {'buy': 'bullish', 'sell': 'bearish', 'dividend': 'dividend'}.get(t['side'], '')
        trade_rows += (
            f'<tr><td>{_esc(t["date"])}</td><td><strong>{_esc(t["symbol"])}</strong></td>'
            f'<td class="{side_cls}">{t["side"].upper()}</td>'
            f'<td>{float(t["quantity"]):g}</td><td>{float(t["price"]):.2f}</td>'
            f'<td><form class="inline" method="POST" action="/trades/{_esc(t["id"])}/delete" '
            f'onsubmit="return confirm(\'Delete this trade?\')">'
            f'<button type="submit" class="danger">Delete</button></form></td></tr>'
        )
    if not trade_rows:
        trade_rows = '<tr><td colspan="6">No trades yet.</td></tr>'

    u_cls = 'bullish' if (totals['unrealized_gain'] or 0) >= 0 else 'bearish'
    r_cls = 'bullish' if totals['realized_gain'] >= 0 else 'bearish'
    unrealized_pct_str = f" ({totals['unrealized_pct']:+.1f}%)" if totals['unrealized_pct'] is not None else ''

    body = f"""
    <div class="header">
      <a class="logout" href="/logout">Log out</a>
      <a class="logout" href="/change-password" style="right:90px;">Change password</a>
      <h1>📈 My Portfolio</h1>
    </div>

    <div class="card">
      <div class="stats">
        <div class="stat"><div class="big">{totals['cost_basis']:.2f}</div><div class="label">Cost Basis (KES)</div></div>
        <div class="stat"><div class="big">{totals['market_value']:.2f}</div><div class="label">Market Value (KES)</div></div>
        <div class="stat"><div class="big {u_cls}">{totals['unrealized_gain']:+.2f}{unrealized_pct_str}</div><div class="label">Unrealized Gain</div></div>
        <div class="stat"><div class="big {r_cls}">{totals['realized_gain']:+.2f}</div><div class="label">Realized Gain</div></div>
        <div class="stat"><div class="big dividend">{totals['dividends']:.2f}</div><div class="label">Dividends Received</div></div>
        <div class="stat"><div class="big fee">-{totals['fees']:.2f}</div><div class="label">Fees Paid ({TRANSACTION_FEE_PCT * 100:.1f}%)</div></div>
      </div>
    </div>

    <div class="card">
      <h2>Holdings</h2>
      <p style="font-size:0.72rem; color:#667085; margin:-8px 0 12px;">
        Avg Cost, Unrealized and Realized already have the {TRANSACTION_FEE_PCT * 100:.1f}%
        brokerage/statutory fee on buys and sells factored in (see Fees Paid above),
        to match real broker P&amp;L.
      </p>
      <table><thead><tr><th>Symbol</th><th>Qty</th><th>Avg Cost</th><th>Price</th>
      <th>Market Value</th><th>Unrealized</th><th>Realized</th><th>Dividends</th></tr></thead>
      <tbody>{holdings_rows}</tbody></table>
    </div>

    <div class="card">
      <h2>Add Trade</h2>
      <form class="grid" method="POST" action="/trades">
        <p><label>Symbol</label><input name="symbol" required style="text-transform:uppercase; width:100px;"></p>
        <p><label>Side</label><select name="side"><option value="buy">Buy</option><option value="sell">Sell</option><option value="dividend">Dividend</option></select></p>
        <p><label>Quantity</label><input type="number" step="any" min="0" name="quantity" required style="width:100px;"></p>
        <p><label>Price (KES)</label><input type="number" step="any" min="0" name="price" required style="width:100px;"></p>
        <p><label>Date</label><input type="date" name="date" required></p>
        <p><button type="submit">Add</button></p>
      </form>
    </div>

    <div class="card">
      <h2>Trade History</h2>
      <table><thead><tr><th>Date</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th><th></th></tr></thead>
      <tbody>{trade_rows}</tbody></table>
    </div>"""
    return _page("My Portfolio", body)


# ---- Lambda-Function-URL (payload format v2.0) plumbing ----

def _parse_form(event):
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    return parse_qs(body)


def _html_response(body, status=200, set_cookie=None):
    resp = {
        "statusCode": status,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": body,
        "isBase64Encoded": False,
    }
    if set_cookie:
        resp["cookies"] = [set_cookie]
    return resp


def _redirect(location, set_cookie=None):
    resp = {"statusCode": 302, "headers": {"Location": location}, "body": "", "isBase64Encoded": False}
    if set_cookie:
        resp["cookies"] = [set_cookie]
    return resp


def _origin_verified(event):
    """
    True only if the request carries the shared secret CloudFront injects
    as a custom origin header (see terraform/portfolio.tf) -- this Function
    URL uses authorization_type=NONE (OAC's AWS_IAM/SigV4 model can't work
    with plain HTML form POSTs), so this header check is what actually
    restricts real access to CloudFront-routed traffic; anything hitting
    the Function URL directly fails this and is rejected before touching
    sessions, trades, or S3.
    """
    expected = os.environ.get('ORIGIN_VERIFY_SECRET', '')
    provided = (event.get("headers") or {}).get('x-origin-verify', '')
    return bool(expected) and hmac.compare_digest(provided, expected)


def _route(event):
    if not _origin_verified(event):
        return {"statusCode": 403, "headers": {"Content-Type": "text/plain"}, "body": "Forbidden"}

    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath") or "/"

    if path == "/login" and method == "GET":
        return _html_response(_render_login())

    if path == "/login" and method == "POST":
        form = _parse_form(event)
        username = (form.get('username', [''])[0] or '').strip()
        password = form.get('password', [''])[0] or ''
        if username and _check_password(username, password):
            return _redirect("/", set_cookie=_make_session_cookie(username))
        return _html_response(_render_login(error="Invalid username or password"), status=401)

    if path == "/register" and method == "GET":
        return _html_response(_render_register())

    if path == "/register" and method == "POST":
        form = _parse_form(event)
        username = (form.get('username', [''])[0] or '').strip()
        password = form.get('password', [''])[0] or ''
        confirm_password = form.get('confirm_password', [''])[0] or ''
        if not _valid_username(username):
            return _html_response(
                _render_register(error="Username must be 3-32 characters: letters, numbers, underscore, hyphen only."),
                status=400,
            )
        if len(password) < 8:
            return _html_response(_render_register(error="Password must be at least 8 characters."), status=400)
        if password != confirm_password:
            return _html_response(_render_register(error="Passwords do not match."), status=400)
        if not _register_user(username, password):
            return _html_response(_render_register(error="That username is already taken."), status=409)
        return _redirect("/", set_cookie=_make_session_cookie(username))

    if path == "/logout":
        return _redirect("/login", set_cookie=_clear_session_cookie())

    # Every remaining route requires a valid session.
    username = _verify_session(event)
    if not username:
        return _redirect("/login")

    # A freshly bootstrapped (or just-reset) credential forces a password
    # change before anything else is reachable.
    if _must_change_password(username) and path != "/change-password":
        return _redirect("/change-password")

    if path == "/change-password" and method == "GET":
        return _html_response(_render_change_password(forced=_must_change_password(username)))

    if path == "/change-password" and method == "POST":
        form = _parse_form(event)
        new_password = form.get('new_password', [''])[0] or ''
        confirm_password = form.get('confirm_password', [''])[0] or ''
        forced = _must_change_password(username)
        if len(new_password) < 8:
            return _html_response(
                _render_change_password(forced=forced, error="Password must be at least 8 characters."),
                status=400,
            )
        if new_password != confirm_password:
            return _html_response(
                _render_change_password(forced=forced, error="Passwords do not match."), status=400,
            )
        _set_password(username, new_password)
        return _redirect("/")

    if path == "/" and method == "GET":
        trades = _load_trades(username)
        prices = _fetch_prices()
        positions, totals = _fifo_positions(trades, prices)
        return _html_response(_render_dashboard(positions, totals, trades))

    if path == "/trades" and method == "POST":
        form = _parse_form(event)
        try:
            symbol = (form.get('symbol', [''])[0] or '').strip().upper()
            side = form.get('side', [''])[0]
            quantity = float(form.get('quantity', [''])[0])
            price = float(form.get('price', [''])[0])
            date = (form.get('date', [''])[0] or '').strip()
            if not symbol or side not in ('buy', 'sell', 'dividend') or quantity <= 0 or price <= 0 or not date:
                raise ValueError("incomplete or invalid trade")
        except (ValueError, IndexError):
            return _html_response(
                _render_error("Invalid trade — check symbol, side, quantity, price and date."), status=400,
            )
        trades = _load_trades(username)
        trades.append({
            'id': str(uuid.uuid4()), 'symbol': symbol, 'side': side,
            'quantity': quantity, 'price': price, 'date': date,
        })
        _save_trades(username, trades)
        return _redirect("/")

    if path.startswith("/trades/") and path.endswith("/delete") and method == "POST":
        trade_id = path.split('/')[2] if len(path.split('/')) > 2 else None
        trades = [t for t in _load_trades(username) if t.get('id') != trade_id]
        _save_trades(username, trades)
        return _redirect("/")

    return {"statusCode": 404, "headers": {"Content-Type": "text/plain"}, "body": "Not found"}


def handler(event, context):
    try:
        return _route(event)
    except Exception as e:  # personal single-user tool: never leak a raw traceback, show something readable
        return _html_response(_render_error(f"Unexpected error: {e}"), status=500)
