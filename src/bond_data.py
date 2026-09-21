"""
Government bond prices from the NSE's official daily bond-trading PDF.

The Nairobi Securities Exchange publishes a "BondPrices_<date>.pdf" on its
bonds-statistics page every trading day, listing every bond on the Fixed
Income Securities Market Segment (Treasury FXD, Infrastructure IFB, and
corporate MTNs) with coupon, traded yield, dirty/clean price and value
traded. The PDF is a scanned table with no text layer.

Table extraction uses AWS Textract's AnalyzeDocument (TABLES feature),
not raw OCR (pytesseract) -- tried first and empirically much worse for
this dense a table: plain tesseract, even at its best tuned settings,
correctly extracted only ~1 of ~25 bonds that traded on a given day.
Textract's table-aware extraction reconstructs actual columns instead of
a single noisy text blob and got ~39 clean rows on the same document.
Textract's sync API only accepts single-page input, so each PDF page is
rendered to an image (via pdf2image/poppler, no OCR binary needed) and
analyzed separately.

Only actively-traded GOVERNMENT bonds (Treasury + Infrastructure series --
the "GOVERNMENT OF KENYA ... TREASURY BONDS" and "INFRASTRUCTURE BONDS"
sections) are returned. Corporate bonds and the bonds sell/buy-back (repo)
section are excluded. A bond counts as "traded" when it has a real value
traded for the day, not just an outstanding listing -- most of the ~150
listed bonds don't trade on any given day.

Sorted by issue date, most recently issued first (then by yield, highest
first, as a tiebreaker within the same issue year). Value traded is shown
but is not the sort key.

`recommend_bonds()` additionally picks one highlighted bond per maturity
bucket (short/medium/long): the highest-yielding actively-traded bond in
that bucket. Every bond here shares the same issuer (Government of Kenya),
so credit risk is constant within a bucket -- among bonds of similar
duration, higher yield is a straightforwardly better buy, no judgment call
needed. It deliberately does NOT pick one single "best" bond overall,
since the right maturity depends on the reader's own time horizon, which
this tool has no way to know.

`bond_market_verdict()` answers a different question -- not which bond,
but whether bonds are worth buying at all right now -- by comparing the
day's median actively-traded yield to the Central Bank Rate (the standard
risk-free benchmark). A wide spread over the CBR is a genuine premium for
locking money up; a thin or negative spread means a T-bill or fixed
deposit may pay similarly without the duration risk.

Extraction reliability: even with Textract, individual digits in the
ISIN/numeric columns are still sometimes misread. Two defenses:
  1. ISIN check-digit validation (ISO 6166) -- a row whose ISIN fails its
     own checksum is almost certainly misread and is dropped entirely.
  2. Plausibility bounds on each numeric field (coupon/yield ~1-30%,
     prices ~50-160) -- a field outside that range is set to None (shown
     as "n/a") rather than displaying a wrong number with false confidence.
This still isn't guaranteed-correct -- the email discloses it's
machine-extracted and approximate (see email_notifier.py's bonds section).

Fails safe: any fetch/extraction/parse failure returns [] and the
pipeline is unaffected -- this is a nice-to-have addition, not a
required source.
"""

import re
import statistics
from datetime import datetime

import requests

from logger import get_logger

logger = get_logger(__name__)

BOND_STATS_URL = "https://www.nse.co.ke/bonds-statistics/"
TEXTRACT_REGION = "eu-west-1"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    ),
}

_ISIN_RE = re.compile(r'KE\d{10}')
_SECTION_GOVT = re.compile(r'GOVERNMENT OF KENYA.{0,25}TREASURY BOND', re.IGNORECASE)
_SECTION_INFRA = re.compile(r'INFRASTRUCTURE BONDS?\s*[:.]?\s*$', re.IGNORECASE)
_SECTION_EXCLUDE = re.compile(r'SELL.{0,6}BUY.{0,6}BACK|CORPORATE BOND', re.IGNORECASE)
_ISSUE_NO_RE = re.compile(r'\b([A-Z]{2,5}\d?)\s*[/\\]?\s*(\d{4})\s*[/\\]?\s*(\d{1,2}\.?\d*)\s*Y', re.IGNORECASE)

_PLAUSIBLE = {
    'coupon': (1, 30), 'yield': (1, 30),
    'dirty': (50, 160), 'clean': (50, 160), 'previous': (50, 160),
}

# Table columns, 1-indexed as Textract returns them for this document.
_COL_ISSUE_NO = 1
_COL_ISIN = 3
_COL_COUPON = 9
_COL_YIELD = 10
_COL_DIRTY = 11
_COL_CLEAN = 12
_COL_PREVIOUS = 13
_COL_VALUE_TRADED = 14


def _isin_check_digit_ok(isin):
    """ISO 6166 ISIN check digit (Luhn over letters-as-numbers)."""
    body, check = isin[:-1], isin[-1]
    digits = ''.join(
        c if c.isdigit() else str(ord(c.upper()) - ord('A') + 10) for c in body
    )
    total = 0
    for i, d in enumerate(reversed([int(c) for c in digits])):
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10) == check


def _normalize_number(token):
    """
    Textract sometimes reads a decimal point as a comma inside a
    percentage/price cell (e.g. "13,4440" should be "13.4440"). A comma is
    only ever a genuine thousands separator when every group after it is
    exactly 3 digits; anything else is a misread decimal point.
    """
    token = (token or '').strip().strip('"\'').lstrip('$').strip()
    if re.fullmatch(r'\d{1,3}(,\d{3})+', token):
        return token.replace(',', '')
    if re.fullmatch(r'\d{1,3}(,\d{3})*\.\d+', token):
        return token.replace(',', '')
    if ',' in token:
        head, _, tail = token.rpartition(',')
        return head.replace(',', '') + '.' + tail
    return token


def _num(token, bounds=None):
    token = _normalize_number(token)
    if not token:
        return None
    try:
        v = float(token)
    except ValueError:
        return None
    if bounds and not (bounds[0] <= v <= bounds[1]):
        return None  # implausible -- likely a misread digit, don't trust it
    return v


def _get_bond_pdf_url():
    """Scrape the NSE bonds-statistics page for the latest BondPrices PDF link."""
    from bs4 import BeautifulSoup
    resp = requests.get(BOND_STATS_URL, headers=_HEADERS, timeout=(3, 8))
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')
    candidates = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        if 'bondprices' in href.lower() and href.lower().endswith('.pdf'):
            candidates.append(href if href.startswith('http') else f"https://www.nse.co.ke{href}")
    if not candidates:
        logger.warning("Bond data: no BondPrices PDF link found on bonds-statistics page")
        return None
    return sorted(set(candidates))[-1]


def _extract_table_rows(pdf_bytes):
    """
    Render each PDF page to an image and run it through Textract's table
    extraction. Returns a list of {col_index: cell_text} dicts, one per
    table row, across all pages.
    """
    from pdf2image import convert_from_bytes
    import boto3

    images = convert_from_bytes(pdf_bytes, dpi=200)
    textract = boto3.client('textract', region_name=TEXTRACT_REGION)

    all_rows = []
    for img in images:
        import io
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        resp = textract.analyze_document(
            Document={'Bytes': buf.getvalue()}, FeatureTypes=['TABLES'],
        )
        blocks_by_id = {b['Id']: b for b in resp['Blocks']}

        def cell_text(cell):
            text = ''
            for rel in cell.get('Relationships', []):
                if rel['Type'] == 'CHILD':
                    for cid in rel['Ids']:
                        child = blocks_by_id[cid]
                        if child['BlockType'] == 'WORD':
                            text += child['Text'] + ' '
            return text.strip()

        for table in (b for b in resp['Blocks'] if b['BlockType'] == 'TABLE'):
            cells = [
                blocks_by_id[cid]
                for rel in table.get('Relationships', []) if rel['Type'] == 'CHILD'
                for cid in rel['Ids'] if blocks_by_id[cid]['BlockType'] == 'CELL'
            ]
            rows = {}
            for c in cells:
                rows.setdefault(c['RowIndex'], {})[c['ColumnIndex']] = cell_text(c)
            for r in sorted(rows.keys()):
                all_rows.append(rows[r])

    return all_rows


def _row_to_bond(row):
    """Convert one extracted table row into a bond dict, or None if it's
    not a valid, actively-traded, checksum-clean government bond row."""
    isin_text = (row.get(_COL_ISIN) or '').replace(' ', '')
    m = _ISIN_RE.search(isin_text)
    if not m or not _isin_check_digit_ok(m.group()):
        return None

    issue_m = _ISSUE_NO_RE.search(row.get(_COL_ISSUE_NO) or '')
    if not issue_m:
        return None
    issue_no = f"{issue_m.group(1).upper()}/{issue_m.group(2)}/{issue_m.group(3)}Yr"

    value_traded = _num(row.get(_COL_VALUE_TRADED))
    if not value_traded:
        return None  # didn't trade this session -- out of scope for the email

    # Approximate maturity year from the issue number (e.g. "FXD/2018/15Yr"
    # -> issued 2018, 15-year tenor -> matures ~2033). The PDF doesn't expose
    # an exact redemption date column we extract, so this is a proxy -- good
    # enough to order bonds by how soon they mature, not their exact date.
    issue_year = int(issue_m.group(2))
    tenor_years = float(issue_m.group(3))
    maturity_year = issue_year + round(tenor_years)

    coupon_pct = _num(row.get(_COL_COUPON), _PLAUSIBLE['coupon'])
    # NSE-quoted Treasury/Infrastructure bonds are priced per KES 100 nominal
    # (hence clean/dirty prices sitting in the 50-160 range) and pay
    # semi-annually, so the per-100 coupon payment is just half the annual
    # coupon rate. There's no quarterly-paying series on this market.
    coupon_payment_semi_annual = round(coupon_pct / 2, 2) if coupon_pct is not None else None
    # KES 100 nominal is the pricing/quoting unit, not a realistic holding --
    # nobody buys KES 100 of a bond. CBK's minimum subscription for Treasury
    # and Infrastructure bonds is KES 50,000 (in multiples of 50,000
    # thereafter), so scale the payment to that for a figure that actually
    # matches what a real investor would receive.
    coupon_payment_per_50k = (
        round(coupon_payment_semi_annual * 500, 2) if coupon_payment_semi_annual is not None else None
    )

    return {
        'issue_no': issue_no,
        'isin': m.group(),
        'issue_year': issue_year,
        'tenor_years': tenor_years,
        'maturity_year': maturity_year,
        'coupon_pct': coupon_pct,
        'coupon_payment_semi_annual': coupon_payment_semi_annual,
        'coupon_payment_per_50k': coupon_payment_per_50k,
        'yield_pct': _num(row.get(_COL_YIELD), _PLAUSIBLE['yield']),
        'clean_price': _num(row.get(_COL_CLEAN), _PLAUSIBLE['clean']),
        'previous_price': _num(row.get(_COL_PREVIOUS), _PLAUSIBLE['previous']),
        'value_traded': value_traded,
    }


def _section_of(row):
    """Classify a row by its section header text (section headers land in
    column 1, spanning what would otherwise be data columns)."""
    text = ' '.join(v for v in row.values() if v)
    if _SECTION_EXCLUDE.search(text):
        return 'exclude'
    if _SECTION_GOVT.search(text) or _SECTION_INFRA.search(text):
        return 'govt-start'
    return None


def _parse_rows(rows):
    bonds = []
    in_govt_section = False
    for row in rows:
        section = _section_of(row)
        if section == 'exclude':
            in_govt_section = False
            continue
        if section == 'govt-start':
            in_govt_section = True
            continue
        if not in_govt_section:
            continue
        bond = _row_to_bond(row)
        if bond:
            bonds.append(bond)

    # De-duplicate by ISIN (repeated re-opened-tranche rows, or occasional
    # extraction noise producing a partial duplicate) -- keep the one with
    # the larger traded value.
    by_isin = {}
    for b in bonds:
        existing = by_isin.get(b['isin'])
        if not existing or (b['value_traded'] or 0) > (existing['value_traded'] or 0):
            by_isin[b['isin']] = b

    # Most recently issued first, then highest yield first among bonds
    # issued in the same year. Missing values sort last within their tier
    # rather than crashing the comparison.
    return sorted(
        by_isin.values(),
        key=lambda b: (
            -(b['issue_year'] if b.get('issue_year') is not None else -9999),
            -(b['yield_pct'] if b.get('yield_pct') is not None else -1),
        ),
    )


def fetch_active_government_bonds(limit=25):
    """
    Fetch actively-traded government bonds (Treasury + Infrastructure) from
    the NSE's most recent daily bond prices PDF, sorted by issue date (most
    recently issued first) then yield (highest first). `limit` is raised
    from the old volume-sorted default (15) since truncating the list at a
    low number would silently drop older issues -- the day's actively-traded
    set is typically well under this anyway.
    Returns [] on any failure -- fails safe.
    """
    try:
        pdf_url = _get_bond_pdf_url()
        if not pdf_url:
            return []
        resp = requests.get(pdf_url, headers=_HEADERS, timeout=(3, 25))
        resp.raise_for_status()
        rows = _extract_table_rows(resp.content)
        bonds = _parse_rows(rows)
        current_year = datetime.now().year
        for b in bonds:
            if b.get('maturity_year') is not None:
                b['years_to_maturity'] = b['maturity_year'] - current_year
        logger.info(f"Bond data: {len(bonds)} actively-traded government bonds from {pdf_url}")
        return bonds[:limit]
    except Exception as e:
        logger.warning(f"Bond data unavailable: {e}")
        return []


_MATURITY_BUCKETS = [
    ("Short-term (up to 5 years)", lambda y: y <= 5),
    ("Medium-term (5-15 years)", lambda y: 5 < y <= 15),
    ("Long-term (over 15 years)", lambda y: y > 15),
]


def recommend_bonds(bonds):
    """
    Pick the single highest-yielding bond in each maturity bucket (short/
    medium/long) -- see the module docstring for why this is a defensible
    "which one to buy" pick within a bucket rather than a false claim of
    knowing the single best bond overall.

    Returns a list of {"label": bucket label, "bond": bond dict}, one entry
    per bucket that has at least one candidate with a usable yield. A
    bucket with no qualifying bond is skipped, not guessed at.
    """
    picks = []
    for label, in_bucket in _MATURITY_BUCKETS:
        candidates = [
            b for b in bonds
            if b.get('years_to_maturity') is not None
            and in_bucket(b['years_to_maturity'])
            and b.get('yield_pct') is not None
        ]
        if candidates:
            picks.append({'label': label, 'bond': max(candidates, key=lambda b: b['yield_pct'])})
    return picks


def bond_market_verdict(bonds):
    """
    A single top-line "is now a decent time to buy government bonds at
    all" verdict, based on the spread between actively-traded yields and
    the Central Bank Rate (CBR) -- the standard risk-free benchmark
    T-bills/T-bonds get priced against, already scraped by
    market_pulse.fetch_cbk(). A wide spread means bonds are paying a real
    premium for locking your money up for years; a thin or negative
    spread means you're barely (or not) being paid more than you'd get
    from a T-bill or fixed deposit without the duration risk.

    Returns {verdict, label, cbr_pct, median_yield_pct, spread_pts, reason}
    or None if there's no CBR figure or no bond yields to compare against
    (fails safe, same as the rest of this module -- CBR is scraped off the
    CBK homepage's front-page text, which can change layout at any time).
    """
    yields = [b['yield_pct'] for b in bonds if b.get('yield_pct') is not None]
    if not yields:
        return None
    try:
        from market_pulse import fetch_cbk
        cbr = fetch_cbk().get('cbr_pct')
    except Exception as e:
        logger.warning(f"Bond verdict: CBR unavailable ({e})")
        return None
    if cbr is None:
        return None

    median_yield = statistics.median(yields)
    spread = median_yield - cbr

    if spread >= 2:
        verdict, label = 'buy', 'Attractive'
        reason = (
            f"Median actively-traded yield ({median_yield:.2f}%) is {spread:.2f} pts "
            f"above the CBR ({cbr:.2f}%) -- a wide premium over the policy rate."
        )
    elif spread >= 0:
        verdict, label = 'hold', 'Fair'
        reason = (
            f"Median actively-traded yield ({median_yield:.2f}%) is only {spread:.2f} pts "
            f"above the CBR ({cbr:.2f}%) -- a modest premium, worth weighing against a "
            f"T-bill or fixed deposit before committing to the longer lock-up."
        )
    else:
        verdict, label = 'avoid', 'Unattractive'
        reason = (
            f"Median actively-traded yield ({median_yield:.2f}%) is actually below the "
            f"CBR ({cbr:.2f}%) -- years of duration risk for less than the risk-free "
            f"policy rate."
        )

    return {
        'verdict': verdict,
        'label': label,
        'cbr_pct': cbr,
        'median_yield_pct': round(median_yield, 2),
        'spread_pts': round(spread, 2),
        'reason': reason,
    }


# ---- Test ----
if __name__ == "__main__":
    from logger import setup_logging
    setup_logging()
    bonds = fetch_active_government_bonds()
    for b in bonds:
        print(b)
    print("\nRecommended:")
    for p in recommend_bonds(bonds):
        print(p)
    print("\nVerdict:", bond_market_verdict(bonds))
