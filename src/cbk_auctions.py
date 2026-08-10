"""
Treasury/Infrastructure bonds the Central Bank of Kenya currently has open
for primary-market auction -- distinct from bond_data.py, which covers
bonds already trading on the NSE secondary market. CBK auctions new bonds
roughly monthly; each is only open for about two weeks (see PERIOD OF
SALE below), sold directly by the government via the DhowCSD portal, not
through a stockbroker.

CBK publishes one prospectus PDF per auction at
centralbank.go.ke/bills-bonds/treasury-bonds/, under "Treasury Bonds
Prospectuses". Unlike the NSE's scanned bond-prices PDF (which needs AWS
Textract), these prospectuses have a real text layer, so a much lighter
pdfplumber text extraction + regex parse is enough -- no OCR/Textract
required. A single prospectus commonly reopens 2-3 bonds at once, listed
as parallel columns (one issue number, ISIN, coupon and maturity date
per column) -- _parse_prospectus() reads those as parallel arrays and
bails out rather than guessing if any two arrays disagree in length.

"Switch auctions" (exchanging an existing bond for a new one, only
available to current holders of the old bond) are excluded -- they
aren't something a new investor can just buy, so they don't belong in a
"here's what you can buy right now" list.

Only auctions whose PERIOD OF SALE spans today are returned -- what's
actively open right now, not the full prospectus archive.

Fails safe: any fetch/parse failure returns [] and the pipeline is
unaffected, same posture as bond_data.py.
"""

import io
import re
from datetime import datetime

import requests

from logger import get_logger

logger = get_logger(__name__)

PROSPECTUS_LISTING_URL = "https://www.centralbank.go.ke/bills-bonds/treasury-bonds/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    ),
}

_ISIN_RE = re.compile(r'\bKE\d{10}\b')
_ISSUE_NO_RE = re.compile(r'\b([A-Z]{2,5}\d?)[/-](\d{4})[/-](\d{2,3})\b')
_MONTHS = (
    'January|February|March|April|May|June|July|August|September|'
    'October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec'
)
_DATE_RE = re.compile(rf'(\d{{1,2}})[\s-]*({_MONTHS})[\s,-]*(\d{{4}})', re.IGNORECASE)
# Some older prospectuses use DD/MM/YYYY instead of a month name.
_DATE_NUMERIC_RE = re.compile(r'\b(\d{1,2})/(\d{1,2})/(\d{4})\b')


def _parse_dates(text):
    """Find every loosely-formatted CBK date in `text` (e.g. '30-July 2026',
    '12-Aug-2026', 'Wednesday, 12-Aug-2026', or the numeric '01/03/2038'
    form), in order. Returns a list of date objects; unparseable matches
    are silently skipped."""
    out = []
    for day, mon, year in _DATE_RE.findall(text or ''):
        for fmt in ('%d %B %Y', '%d %b %Y'):
            try:
                out.append(datetime.strptime(f"{day} {mon} {year}", fmt).date())
                break
            except ValueError:
                continue
    if out:
        return out
    for day, mon, year in _DATE_NUMERIC_RE.findall(text or ''):
        try:
            out.append(datetime.strptime(f"{day}/{mon}/{year}", '%d/%m/%Y').date())
        except ValueError:
            continue
    return out


def _get_prospectus_links(limit=8):
    """
    Scrape the CBK Treasury Bonds page for prospectus PDF links, in page
    order (most recently uploaded first). Skips switch auctions -- those
    exchange an existing bond for a new one and aren't something a new
    investor can simply buy.
    """
    from bs4 import BeautifulSoup
    resp = requests.get(PROSPECTUS_LISTING_URL, headers=_HEADERS, timeout=(3, 10))
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')
    links = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        if 'treasury_bonds_prospectuses' not in href.lower() or not href.lower().endswith('.pdf'):
            continue
        if 'switch' in href.lower():
            continue
        url = href if href.startswith('http') else f"https://www.centralbank.go.ke{href}"
        links.append(url)
        if len(links) >= limit:
            break
    return links


def _extract_text(pdf_bytes):
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def _row(text, label):
    """First line in `text` whose stripped, upper-cased form starts with
    `label`, with the label itself stripped off. None if not found."""
    for line in text.splitlines():
        if line.strip().upper().startswith(label):
            return line[len(label):].strip()
    return None


def _parse_prospectus(text):
    """
    Parse one prospectus's extracted text into a list of bond dicts (one
    per tranche). Returns [] if the expected labelled rows are missing or
    their token counts disagree -- fails safe rather than guessing which
    value belongs to which issue.
    """
    issue_row = _row(text, 'ISSUE NUMBER')
    isin_row = _row(text, 'ISIN')
    coupon_row = _row(text, 'COUPON RATES')
    maturity_row = _row(text, 'MATURITY DATES')
    sale_row = _row(text, 'PERIOD OF SALE')
    auction_row = _row(text, 'AUCTION DATE')

    if not issue_row or not sale_row:
        return []

    issue_nos = [f"{m.group(1).upper()}/{m.group(2)}/{m.group(3)}"
                 for m in _ISSUE_NO_RE.finditer(issue_row)]
    if not issue_nos:
        return []
    n = len(issue_nos)

    isins = _ISIN_RE.findall(isin_row or '')
    coupons = [float(x) for x in re.findall(r'\d+\.\d+', coupon_row or '')]
    maturities = _parse_dates(maturity_row)

    if len(isins) != n or len(coupons) != n or len(maturities) != n:
        logger.warning(
            f"CBK prospectus: token count mismatch (issues={n}, isins={len(isins)}, "
            f"coupons={len(coupons)}, maturities={len(maturities)}) -- skipping"
        )
        return []

    sale_dates = _parse_dates(sale_row)
    if len(sale_dates) != 2:
        return []
    sale_opens, sale_closes = sale_dates

    auction_dates = _parse_dates(auction_row or '')
    auction_date = auction_dates[0] if auction_dates else None

    return [
        {
            'issue_no': issue_nos[i],
            'isin': isins[i],
            'coupon_pct': coupons[i],
            'coupon_payment_per_50k': round(coupons[i] / 2 * 500, 2),
            'maturity_date': maturities[i].isoformat(),
            'sale_opens': sale_opens.isoformat(),
            'sale_closes': sale_closes.isoformat(),
            'auction_date': auction_date.isoformat() if auction_date else None,
        }
        for i in range(n)
    ]


def fetch_open_treasury_auctions(limit=8):
    """
    Fetch Treasury/Infrastructure bonds CBK currently has open for
    primary-market auction (today falls within PERIOD OF SALE), scraped
    from the latest prospectus PDF(s) on CBK's Treasury Bonds page.
    Regular cash-sale auctions only -- switch auctions are excluded.
    Returns [] on any failure, or if nothing is open right now -- fails
    safe, same as bond_data.py.
    """
    try:
        links = _get_prospectus_links(limit=limit)
        today = datetime.now().date()
        open_bonds = []
        seen_isins = set()
        for url in links:
            try:
                resp = requests.get(url, headers=_HEADERS, timeout=(3, 20))
                resp.raise_for_status()
                text = _extract_text(resp.content)
                for b in _parse_prospectus(text):
                    opens = datetime.strptime(b['sale_opens'], '%Y-%m-%d').date()
                    closes = datetime.strptime(b['sale_closes'], '%Y-%m-%d').date()
                    if not (opens <= today <= closes):
                        continue
                    if b['isin'] in seen_isins:
                        continue  # same tranche re-listed under a different prospectus link
                    seen_isins.add(b['isin'])
                    open_bonds.append(b)
            except Exception as e:
                logger.warning(f"CBK prospectus fetch/parse failed for {url}: {e}")
                continue
        logger.info(f"CBK auctions: {len(open_bonds)} bond(s) currently open for auction")
        return open_bonds
    except Exception as e:
        logger.warning(f"CBK auctions unavailable: {e}")
        return []


# ---- Test ----
if __name__ == "__main__":
    from logger import setup_logging
    setup_logging()
    for b in fetch_open_treasury_auctions():
        print(b)
