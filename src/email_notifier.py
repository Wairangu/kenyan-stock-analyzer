"""
Email notification module for the Kenyan Stock Analyzer.

Sends HTML emails with market summaries and optional PDF attachments.
Supports Gmail (app passwords) and generic SMTP servers.
"""

import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime
from logger import get_logger
from fundamental_analysis import FundamentalAnalysis

logger = get_logger(__name__)


class EmailNotifier:
    """Sends email notifications with report attachments."""

    def __init__(self, config):
        """
        Args:
            config: Config object with SMTP settings.
        """
        self.config = config
        self.smtp_host = config.smtp_host
        self.smtp_port = config.smtp_port
        self.user = config.email_user
        self.password = config.email_password
        self.recipients = config.email_recipients

        self._validate()

    def _validate(self):
        """Check that required settings are present."""
        if not self.user:
            logger.warning("EMAIL_USER not configured")
        if not self.password:
            logger.warning("EMAIL_PASSWORD not configured")
        if not self.recipients:
            logger.warning("EMAIL_RECIPIENTS not configured")

    def send_report(self, subject, html_body, attachments=None):
        """
        Send an email with HTML body and optional attachments.

        Args:
            subject: Email subject line.
            html_body: HTML string for the email body.
            attachments: List of file paths to attach.

        Returns:
            bool: True if sent successfully, False otherwise.
        """
        if not self.user or not self.password or not self.recipients:
            logger.error("Email not configured — cannot send")
            return False

        msg = MIMEMultipart('mixed')
        msg['Subject'] = subject
        msg['From'] = self.user
        msg['To'] = ', '.join(self.recipients)
        msg['Date'] = datetime.now().strftime('%a, %d %b %Y %H:%M:%S +0300')

        # Attach HTML body
        html_part = MIMEText(html_body, 'html', 'utf-8')
        msg.attach(html_part)

        # Attach files. .ics files get a proper text/calendar MIME so Gmail
        # renders the "Add to calendar" button on the message.
        if attachments:
            for filepath in attachments:
                try:
                    filename = filepath.split('/')[-1]
                    lower = filename.lower()
                    with open(filepath, 'rb') as f:
                        payload = f.read()
                    if lower.endswith('.ics'):
                        part = MIMEBase('text', 'calendar', method='PUBLISH', name=filename)
                    elif lower.endswith('.pdf'):
                        part = MIMEBase('application', 'pdf', name=filename)
                    else:
                        part = MIMEBase('application', 'octet-stream')
                    part.set_payload(payload)
                    encoders.encode_base64(part)
                    part.add_header(
                        'Content-Disposition',
                        f'attachment; filename="{filename}"'
                    )
                    msg.attach(part)
                except Exception as e:
                    logger.error(f"Failed to attach {filepath}: {e}")

        # Send
        try:
            context = ssl.create_default_context()
            if self.smtp_port == 587:
                with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as server:
                    server.starttls(context=context)
                    server.login(self.user, self.password)
                    server.send_message(msg)
            else:
                with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=30, context=context) as server:
                    server.login(self.user, self.password)
                    server.send_message(msg)

            logger.info(f"Email sent to {len(self.recipients)} recipients")
            return True

        except smtplib.SMTPAuthenticationError:
            logger.error(
                "SMTP authentication failed. If using Gmail, ensure you're "
                "using an App Password (not your regular password). "
                "See: https://myaccount.google.com/apppasswords"
            )
            return False
        except Exception as e:
            logger.error(f"Failed to send email: {e}")
            return False

    def generate_email_body(self, analysis_results, sector_data=None,
                            breadth=None, dashboard_url=None,
                            fundamentals_data=None, scores=None, bonds=None):
        """
        Generate a compact HTML email body with market summary.

        Visually matches the dashboard's look and feel -- same color
        palette (bullish/bearish greens & reds, navy/teal/amber header
        gradient) as templates/base.html -- kept to email-client-safe CSS
        (no CSS custom properties, animations, or backdrop-filter, since
        those aren't reliably supported by mail clients).

        Args:
            analysis_results: dict from AnalysisEngine.
            sector_data: dict from SectorAnalyzer.
            breadth: dict from AnalysisEngine.calculate_market_breadth.
            dashboard_url: optional URL to the full hosted dashboard.
                Shown as a button under the header and linked in the
                footer. Omitted entirely when not provided (e.g. local
                runs with no hosted dashboard).
            fundamentals_data: dict from FundamentalAnalysis.fetch_all_fundamentals(),
                used for the TradingView Buy/Sell signal column. Stocks
                render as "N/A" for that column when omitted.
            scores: dict from scoring.score_stock() per symbol, used for
                the Score column. Stocks render "—" for that column when
                omitted.
            bonds: list of dicts from bond_data.fetch_active_government_bonds(),
                the government bonds that actually traded on the NSE the
                previous session. Section is omitted entirely when empty
                (most days won't have this if bond_data's PDF extraction
                fails -- it fails safe, not required for the email to send).

        Returns:
            HTML string suitable for email clients.
        """
        now = datetime.now().strftime('%Y-%m-%d %H:%M EAT')
        total = len(analysis_results)

        # Count signals
        bullish = sum(
            1 for r in analysis_results.values()
            if r and r.get('signals', {}).get('overall') == 'bullish'
        )
        bearish = sum(
            1 for r in analysis_results.values()
            if r and r.get('signals', {}).get('overall') == 'bearish'
        )

        # All stocks — signal & score (same fields/order as the dashboard's
        # Overview table)
        stocks = []
        for symbol, r in sorted(analysis_results.items()):
            if not r:
                continue
            latest = r.get('latest', {})
            fund = (fundamentals_data or {}).get(symbol, {})
            tv_label, tv_class = FundamentalAnalysis.signal_from_tech_rating(
                fund.get('tech_rating')
            )
            stocks.append({
                'symbol': symbol,
                'price': latest.get('close'),
                'change': r.get('daily_change_pct'),
                'tv_label': tv_label,
                'tv_class': tv_class,
                'score': (scores or {}).get(symbol, {}).get('overall'),
            })

        dashboard_button = ""
        if dashboard_url:
            dashboard_button = f"""
        <a href="{dashboard_url}" style="display:inline-block; margin-top:16px; padding:11px 22px; background-color:#ffffff; color:#0f172a; font-weight:700; font-size:0.85rem; text-decoration:none; border-radius:999px;">
            📊 View Full Dashboard &rarr;
        </a>"""

        # Build HTML
        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background-color: #f5f7fb;
            color: #172033;
            margin: 0;
            padding: 20px;
        }}
        .container {{ max-width: 640px; margin: 0 auto; }}
        h1 {{ margin: 0 0 6px; font-size: 1.5rem; font-weight: 800; }}
        h2 {{
            font-size: 1rem;
            font-weight: 800;
            margin: 0 0 14px;
            padding-bottom: 8px;
            border-bottom: 2px solid #2563eb;
            display: inline-block;
        }}
        .header {{
            text-align: center;
            background-color: #0f172a;
            background-image: linear-gradient(135deg, #0f172a 0%, #115e59 55%, #b45309 100%);
            color: #ffffff;
            padding: 30px 20px;
            border-radius: 18px;
            margin-bottom: 20px;
        }}
        .header h1 {{ color: #ffffff; }}
        .header .meta {{ color: rgba(255,255,255,0.78); font-size: 0.88rem; margin: 0; }}
        .card {{
            background-color: #ffffff;
            border: 1px solid rgba(148,163,184,0.28);
            border-radius: 16px;
            padding: 18px 20px;
            margin-bottom: 16px;
        }}
        .bar {{
            height: 3px;
            background-color: #2563eb;
            background-image: linear-gradient(90deg, #2563eb, #0f766e, #f59e0b);
            border-radius: 3px;
            margin: -18px -20px 16px;
        }}
        .stats {{ display: flex; gap: 10px; flex-wrap: wrap; }}
        .stat {{
            background-color: #f5f7fb;
            padding: 12px 14px;
            border-radius: 12px;
            text-align: center;
            flex: 1;
            min-width: 100px;
            border: 1px solid rgba(148,163,184,0.22);
        }}
        .stat .big {{ font-size: 1.4rem; font-weight: 800; color: #2563eb; }}
        .stat .label {{ font-size: 0.68rem; color: #667085; text-transform: uppercase; font-weight: 700; margin-top: 2px; }}
        .bullish {{ color: #12b981; }}
        .bearish {{ color: #ef4444; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 0.88rem; }}
        th, td {{ padding: 9px 10px; text-align: left; border-bottom: 1px solid rgba(148,163,184,0.28); }}
        th {{ background-color: #f5f7fb; color: #667085; font-size: 0.68rem; text-transform: uppercase; font-weight: 800; }}
        td strong {{ color: #0f172a; }}
        .badge {{ display: inline-block; padding: 3px 9px; border-radius: 999px; font-size: 0.72rem; font-weight: 700; }}
        .badge.strong_buy {{ background-color: #16a34a; color: #ffffff; }}
        .badge.buy {{ background-color: #d1fae5; color: #065f46; }}
        .badge.neutral {{ background-color: #fef3c7; color: #92400e; }}
        .badge.sell {{ background-color: #fee2e2; color: #991b1b; }}
        .badge.strong_sell {{ background-color: #dc2626; color: #ffffff; }}
        .badge.undefined {{ background-color: #f1f5f9; color: #64748b; }}
        .score {{ display: inline-block; padding: 3px 9px; border-radius: 999px; font-size: 0.78rem; font-weight: 800; }}
        .score-high {{ background-color: #d1fae5; color: #065f46; }}
        .score-mid {{ background-color: #fef3c7; color: #92400e; }}
        .score-low {{ background-color: #fee2e2; color: #991b1b; }}
        .footer {{ text-align: center; padding: 16px 8px 4px; font-size: 0.78rem; color: #667085; }}
        .footer a {{ color: #2563eb; font-weight: 700; text-decoration: none; }}
    </style>
</head>
<body>
    <div class="container">
    <div class="header">
        <h1>🇰🇪 NSE Daily Market Report</h1>
        <p class="meta">{now}</p>{dashboard_button}
    </div>

    <div class="card"><div class="bar"></div>
    <div class="stats">
        <div class="stat">
            <div class="big">{total}</div>
            <div class="label">Stocks</div>
        </div>
        <div class="stat">
            <div class="big bullish">{bullish}</div>
            <div class="label">Bullish</div>
        </div>
        <div class="stat">
            <div class="big bearish">{bearish}</div>
            <div class="label">Bearish</div>
        </div>
        <div class="stat">
            <div class="big">{len(sector_data) if sector_data else 0}</div>
            <div class="label">Sectors</div>
        </div>
    </div>
    </div>
"""
        # Market breadth
        if breadth:
            html += """
    <div class="card"><div class="bar"></div>
    <h2>Market Breadth</h2>
    <div class="stats">
"""
            for key, label in [
                ('pct_above_sma50', 'Above SMA50'),
                ('pct_bullish_macd', 'Bullish MACD'),
                ('pct_rsi_above_50', 'RSI > 50'),
            ]:
                if key in breadth:
                    html += f"""
        <div class="stat">
            <div class="big">{breadth[key]}%</div>
            <div class="label">{label}</div>
        </div>"""
            html += "\n    </div>\n    </div>\n"

        # All stocks — signal & score
        if stocks:
            html += """
    <div class="card"><div class="bar"></div>
    <h2>📋 All Stocks — Signal &amp; Score</h2>
    <table>
        <tr><th>Symbol</th><th>TV Signal</th><th>Price</th><th>Change</th><th>Score</th></tr>
"""
            for s in stocks:
                price_str = f"{s['price']:.2f}" if s['price'] is not None else '—'
                chg = s['change']
                chg_cls = 'bullish' if (chg or 0) >= 0 else 'bearish'
                chg_str = f"{chg:+.2f}%" if chg is not None else '—'
                sc = s['score']
                if sc is None:
                    score_html = '—'
                else:
                    sc_cls = 'score-high' if sc >= 70 else 'score-mid' if sc >= 45 else 'score-low'
                    score_html = f'<span class="score {sc_cls}">{sc}</span>'
                html += (
                    f'        <tr><td><strong>{s["symbol"]}</strong></td>'
                    f'<td><span class="badge {s["tv_class"]}">{s["tv_label"]}</span></td>'
                    f'<td>{price_str}</td>'
                    f'<td class="{chg_cls}">{chg_str}</td>'
                    f'<td>{score_html}</td></tr>\n'
                )
            html += "    </table>\n    </div>\n"

        # Sector performance
        if sector_data:
            html += """
    <div class="card"><div class="bar"></div>
    <h2>Sector Performance</h2>
    <table>
        <tr><th>Sector</th><th>Stocks</th><th>Avg Change</th><th>Bullish %</th></tr>
"""
            for name, data in sector_data.items():
                cls = "bullish" if data['avg_change_pct'] >= 0 else "bearish"
                html += (
                    f'        <tr><td><strong>{name}</strong></td>'
                    f'<td>{data["count"]}</td>'
                    f'<td class="{cls}">{data["avg_change_pct"]:+.2f}%</td>'
                    f'<td>{data["bullish_ratio"]}%</td></tr>\n'
                )
            html += "    </table>\n    </div>\n"

        # Government bonds actively traded on the NSE (Treasury + Infrastructure)
        if bonds:
            html += """
    <div class="card"><div class="bar"></div>
    <h2>&#127974; Government Bonds &mdash; Actively Traded</h2>
    <p style="font-size:0.78rem; color:#667085; margin:0 0 12px;">
        Machine-extracted from the NSE's daily bond prices PDF &mdash; treat as
        approximate and verify before acting on any figure. Sorted by maturity
        (soonest first), then by yield &mdash; for a buy-and-hold investor, how
        soon a bond matures matters more than how much traded today.
    </p>
"""
            from bond_data import bond_market_verdict, recommend_bonds
            verdict = bond_market_verdict(bonds)
            if verdict:
                badge_class = {'buy': 'buy', 'hold': 'neutral', 'avoid': 'sell'}[verdict['verdict']]
                html += (
                    '    <p style="font-size:0.85rem; margin:0 0 14px;">'
                    f'<span class="badge {badge_class}">{verdict["label"]}</span> '
                    f'<span style="color:#344054;">{verdict["reason"]}</span></p>\n'
                )
            picks = recommend_bonds(bonds)
            if picks:
                html += """
    <div style="background:#d1fae5; border-radius:8px; padding:12px 16px; margin:0 0 16px;">
        <div style="font-weight:700; font-size:0.85rem; color:#065f46; margin-bottom:6px;">
            &#127942; Highest yield by time horizon
        </div>
"""
                for p in picks:
                    b = p['bond']
                    html += (
                        f'        <div style="font-size:0.8rem; color:#065f46; margin:2px 0;">'
                        f'<strong>{p["label"]}:</strong> {b["issue_no"]} &mdash; '
                        f'{b["yield_pct"]:.2f}% yield (matures {b["maturity_year"]})</div>\n'
                    )
                html += """
        <div style="font-size:0.72rem; color:#065f46; margin-top:8px; opacity:0.85;">
            Same issuer (Government of Kenya) in every bucket, so within a time
            horizon the higher-yielding bond is the straightforward pick. This is
            not a single "best bond overall" &mdash; that depends on when you
            actually need the money back.
        </div>
    </div>
"""
            html += """
    <table>
        <tr><th>Bond</th><th>Maturity</th><th>Yield</th><th>Coupon</th><th>Clean Price</th><th>Value Traded (KES)</th></tr>
"""
            for b in bonds:
                maturity = str(b['maturity_year']) if b.get('maturity_year') is not None else '—'
                coupon = f"{b['coupon_pct']:.2f}%" if b.get('coupon_pct') is not None else '—'
                yld = f"{b['yield_pct']:.2f}%" if b.get('yield_pct') is not None else '—'
                clean = f"{b['clean_price']:.2f}" if b.get('clean_price') is not None else '—'
                traded = f"{b['value_traded']:,.0f}" if b.get('value_traded') is not None else '—'
                html += (
                    f'        <tr><td><strong>{b["issue_no"]}</strong></td>'
                    f'<td>{maturity}</td><td>{yld}</td><td>{coupon}</td>'
                    f'<td>{clean}</td><td>{traded}</td></tr>\n'
                )
            html += "    </table>\n    </div>\n"

        footer_link = (
            f'<a href="{dashboard_url}">View the full dashboard &rarr;</a><br>'
            if dashboard_url else ''
        )
        html += f"""
    <div class="footer">
        {footer_link}
        Generated by Kenyan Stock Analyzer &mdash; {now}
    </div>
    </div>
</body>
</html>"""

        return html