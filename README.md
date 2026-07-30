# 🇰🇪 Kenyan Stock Analyzer — NSE Daily Dashboard

A fully automated daily stock analysis pipeline for the **Nairobi Securities Exchange (NSE)**. Displays the **NSE official closing price** for each stock (settled after market close), cross-checked against **TradingView**, and uses TradingView for fundamentals and price history. Performs technical and fundamental analysis on all 57+ listed stocks and generates an interactive HTML dashboard with individual stock reports.

> **Accuracy note:** prices shown are the official NSE close from an independent NSE data service, cross-checked against TradingView; where the two disagree (usually thinly-traded stocks) the price is flagged ❗ so you know to confirm it. No free feed is guaranteed accurate to the shilling intraday — for the most reliable numbers, run **after market close (15:00 EAT)**.

## Features

- **All 57 NSE stocks** analyzed daily — not just a watchlist
- **Fundamental analysis** — P/E, PEG, ROE, ROIC, operating/net margins, debt ratios, revenue/EPS growth, market cap, and more
- **Technical analysis** — RSI, MACD, Bollinger Bands, SMA/EMA crossovers, Stochastic, ATR, OBV, support/resistance levels
- **6 charts per stock** — price+SMA+Bollinger, RSI, MACD, volume, stochastic, ATR
- **Plain-English explanations** — every metric explained in simple terms (e.g. "RSI above 70 = overbought, price may pull back")
- **Similar stocks** — peer comparison by sector, market cap, and valuation
- **Sector performance** — sector-by-sector breakdown with average returns
- **Market breadth** — advance/decline, % above SMA50, bullish MACD ratio
- **Excel export** — multi-sheet workbook with all data
- **Clean runs** — old reports and cache files are automatically removed each run

## Quick Start

### Prerequisites

- Python 3.10+
- macOS or Linux (Windows works but PDF generation requires extra setup)

### 1. Set up

```bash
cd kenyan_stock_analyzer
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure (optional)

```bash
cp .env.example .env
# Edit .env if you want email notifications or custom settings
```

### 3. Run

```bash
./run.sh
```

This generates:
- `reports/index.html` — **main dashboard** (open this in your browser)
- `reports/SCOM_report_*.html` — individual stock reports (57 files)
- `reports/nse_analysis_*.xlsx` — Excel export
- `reports/market_summary_*.html` — market summary

### 4. Open the dashboard

```bash
open reports/index.html
```

## Usage

### Daily run (recommended)

```bash
./run.sh
```

### Command-line options

```bash
python main.py                    # All stocks, 6-month data, HTML only
python main.py --detailed         # Generate individual stock reports (57 files)
python main.py --export-excel     # Also export Excel workbook
python main.py --report-type both # Generate HTML + PDF (requires WeasyPrint)
python main.py --period 1y        # Use 1 year of historical data
python main.py --force-refresh    # Skip cache, fetch fresh data
python main.py --watchlist-only   # Only analyze configured watchlist (10 stocks)
```

### Daily automated run (cron)

Add to your crontab (`crontab -e`):

```cron
# Run at 3:00 PM EAT (market close) on weekdays
0 12 * * 1-5 cd /path/to/kenyan_stock_analyzer && ./venv/bin/python main.py --detailed --export-excel >> logs/cron.log 2>&1
```

Nairobi is UTC+3, so 15:00 EAT = 12:00 UTC.

## Daily Summary Email (automated, cloud-based)

Get a **1-page PDF summary of key metrics emailed to you every weekday at ~10 pm EAT** — automatically, on GitHub's servers, whether or not your computer is on.

The summary is a subset of the dashboard (the dashboard itself is unchanged) and includes:
- Market pulse — bullish/bearish counts, breadth, USD/KES
- **Data quality** — how many prices were independently verified vs flagged as mismatched/stale
- Your watchlist — TV signal (Buy/Sell), price, change, yield, score
- Top gainers/losers
- **Upcoming ex-dividends** (next 30 days)
- Key alerts (strong Buy/Sell, oversold, high yield, etc.)

### How it works

- `send_summary.py` runs a lean pipeline (data → analysis → fundamentals → price validation → scoring), builds the PDF via `ReportGenerator.generate_summary`, and emails it.
- `.github/workflows/daily-summary.yml` runs `send_summary.py` on **GitHub Actions** at `19:00 UTC` (22:00 EAT), Monday–Friday, plus a manual "Run workflow" button.
- **Skips non-trading days.** Weekends are excluded by the schedule, and `send_summary.py` also checks the **Kenyan public-holiday calendar** (Christmas, Boxing Day, Jamhuri Day, Madaraka Day, Mashujaa Day, Labour Day, New Year, Easter, Eid, etc. via the `holidays` library) — on those days it sends nothing. Manual runs pass `--ignore-calendar`, so you can still test on any day.
- **Files never accumulate on GitHub.** Each run happens on a fresh, throwaway machine and starts by clearing any leftovers. The report is committed nowhere (`reports/`, `data/`, `*.pdf`, `*.xlsx` are git-ignored). The only thing kept is a **single backup artifact** (the latest PDF) — and the workflow **deletes the previous one just before generating the new one**, so at most one ever exists. That backup lets you re-download the report if an email ever fails.

### Non-trading days (no email sent)

The NSE does not trade on weekends or Kenyan public holidays, so there is no new data to report on those days. `send_summary.py` checks the date **in Nairobi time** and sends nothing when the market is closed:

- **Weekends** — Saturday & Sunday (scheduled runs are already limited to Mon–Fri, and the code double-checks).
- **Kenyan public holidays** — resolved automatically via the [`holidays`](https://pypi.org/project/holidays/) library (`holidays.Kenya`):

  | Holiday | Date |
  |---------|------|
  | New Year's Day | 1 January |
  | Good Friday | moves each year (Easter) |
  | Easter Monday | moves each year (Easter) |
  | Labour Day | 1 May |
  | Madaraka Day | 1 June |
  | Idd-ul-Fitr (Eid al-Fitr) | moves each year (Islamic calendar) |
  | Idd-ul-Adha (Eid al-Adha) | moves each year (Islamic calendar) |
  | Utamaduni / Mazingira Day | 10 October |
  | Mashujaa Day | 20 October |
  | Jamhuri Day | 12 December |
  | Christmas Day | 25 December |
  | Boxing Day | 26 December |

**Works for every year, automatically.** The calendar is computed for the current year at run time — nothing is hard-coded to a single year, and the moving holidays (Easter, Eid) are recalculated correctly each year. Occasional one-off public holidays declared by the government are also picked up as the `holidays` library is updated.

**Manual runs always send.** The "Run workflow" button passes `--ignore-calendar`, so you can test on any day (weekend or holiday). Only the automatic scheduled runs respect the calendar.

**Fails open.** If the holiday check is ever unavailable, the report still runs rather than silently going missing.

### Setup (one time)

1. Create a Gmail **App Password** at <https://myaccount.google.com/apppasswords> (requires 2-Step Verification). Do **not** use your normal password.
2. In your GitHub repo, add three **Actions secrets** — *Settings → Secrets and variables → Actions → New repository secret*:

   | Secret name | What it holds |
   |-------------|----------------|
   | `EMAIL_USER` | the Gmail address that sends the report |
   | `EMAIL_PASSWORD` | the 16-character Gmail **app password** |
   | `EMAIL_RECIPIENTS` | comma-separated recipient address(es) |

   > 🔒 **Keep these in GitHub Actions secrets only.** Never commit real credentials to the code, the README, or a tracked `.env`. (`.env` is git-ignored.)

### Run it manually

- **Web:** repo → **Actions** → *Daily NSE Summary Email* → **Run workflow** → branch `main` → **Run**.
- **CLI:** `gh workflow run "Daily NSE Summary Email"` then `gh run watch`.

### Run / test locally

Put `EMAIL_USER`, `EMAIL_PASSWORD`, and `EMAIL_RECIPIENTS` in your **local `.env`** (git-ignored), then:

```bash
ENABLE_EMAIL_NOTIFICATIONS=true python send_summary.py --force-refresh
```

With email disabled it just builds `reports/nse_summary_*.pdf` without sending.

> **Notes:** GitHub's scheduler can start a few minutes late — harmless for a daily digest. Running at 10 pm means the market is closed, so prices reflect the settled daily close. Check your spam folder for the first email and mark it "not spam".

## Project Structure

```
kenyan_stock_analyzer/
├── main.py                     # Entry point — orchestrates the full pipeline
├── send_summary.py             # Builds the 1-page PDF summary and emails it (daily job)
├── run.sh                      # One-command runner (venv + pipeline + open dashboard)
├── scheduler.py                # Optional scheduler for automated daily runs
├── .github/workflows/          # GitHub Actions — daily-summary.yml (scheduled email)
├── requirements.txt            # Python dependencies
├── .env.example                # Example environment configuration
├── crontab.example             # Cron setup reference
├── README.md
│
├── src/
│   ├── data_acquisition.py     # Fetches OHLCV data from TradingView, NSE PDF, Yahoo
│   ├── analysis_engine.py      # Technical analysis (RSI, MACD, Bollinger, etc.)
│   ├── fundamental_analysis.py # Fundamental data from TradingView scanner
│   ├── price_validation.py     # Cross-checks prices vs an independent source + freshness
│   ├── market_context.py       # USD/KES rate + per-sector median valuation
│   ├── scoring.py              # Transparent 0-100 factor score + per-stock alerts
│   ├── history_tracker.py      # Appends a daily snapshot for later accuracy review
│   ├── report_generator.py     # HTML/PDF reports, Excel export, charts, summary PDF
│   ├── sector_analysis.py      # Sector-level aggregation
│   ├── config.py               # Centralized configuration from .env
│   ├── logger.py               # Logging setup
│   ├── utils.py                # Support/resistance detection, retry decorator
│   └── email_notifier.py       # Email report sending
│
├── templates/
│   ├── base.html               # Base HTML template with shared styles
│   ├── stock_report.html       # Individual stock report template
│   └── market_summary.html     # Market summary template
│
├── reports/                    # Generated reports (cleaned each run)
├── data/                       # Cached data files (cleaned each run)
└── logs/                       # Application logs
```

## Data Sources

All fundamental and technical data is sourced from **TradingView** via the `tvkit` library. Nothing is guessed or estimated.

| Data Type | Source | Method |
|-----------|--------|--------|
| OHLCV price history | TradingView | `tvkit` WebSocket API |
| Fundamental metrics | TradingView | Scanner API (Kenya market) |
| Daily price list (fallback) | NSE PDF | OCR via `pdf2image` + `pytesseract` |
| Historical data (fallback) | Yahoo Finance | `yfinance` |

## Metrics Included

### Technical Indicators
- RSI (14) with overbought/oversold signals
- MACD (12, 26, 9) with crossover detection
- Bollinger Bands (20, 2σ)
- SMA 20 & SMA 50 with golden cross/death cross detection
- EMA 12 & EMA 26
- Stochastic Oscillator (%K, %D)
- ATR (Average True Range)
- OBV (On-Balance Volume)
- Support & Resistance levels

### Fundamental Metrics (from TradingView)
- Market Cap, P/E (TTM), PEG, Price/Book, Price/Sales
- Enterprise Value, EV/Revenue, EV/EBITDA
- EPS (TTM), EPS Growth (YoY)
- Revenue (TTM), Revenue Growth (YoY)
- Net Income, Operating Income, EBITDA
- Gross Margin, Operating Margin, Net Margin, FCF Margin
- ROE, ROA, ROIC
- Debt/Equity, Current Ratio, Quick Ratio
- Total Assets, Total Debt, Net Debt, Total Equity
- Free Cash Flow, Operating Cash Flow, CapEx
- Dividend Yield, Payout Ratio, DPS
- Performance: 1W, 1M, 3M, 6M, YTD, 1Y, 5Y
- Analyst Recommendation (1=Strong Buy, 5=Strong Sell)
- Sector classification

## Requirements

See [`requirements.txt`](requirements.txt) for the full, authoritative list of pinned Python dependencies.

### System dependencies (macOS)

```bash
# For PDF generation (WeasyPrint)
brew install pango glib

# For NSE PDF OCR (optical character recognition)
brew install tesseract poppler
```

## Troubleshooting

### WeasyPrint (PDF) not working on macOS
```bash
brew install pango glib
```

### Yahoo Finance timeout
Yahoo Finance can be unreliable for NSE stocks. The pipeline tries multiple sources:
1. TradingView (primary — most reliable)
2. NSE PDF (current-day prices via OCR)
3. Yahoo Finance (historical data, multiple ticker formats)

### Email not sending
- Gmail: Use an [App Password](https://myaccount.google.com/apppasswords), not your regular password
- Ensure `SMTP_HOST`, `SMTP_PORT`, `EMAIL_USER`, and `EMAIL_PASSWORD` are set in `.env`

## License

This project is for personal use. Use at your own risk. Not financial advice.