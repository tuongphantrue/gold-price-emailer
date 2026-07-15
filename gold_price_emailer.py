#!/usr/bin/env python3
"""
Vietnam Gold Prices (multi-seller) -> Email (runs on GitHub Actions, no local computer needed)

Same shape as the 9gag-meme-emailer this is modeled on: fetches the current
gold price comparison table, then emails an HTML digest via Gmail SMTP.
Runs in two phases so the workflow can persist dedup state *between* them
(see the accompanying GitHub Actions workflow):

    python gold_price_emailer.py generate
        -> scrapes the price tables, writes the composed email
           (subject/html/text) under ./email/, and updates the
           "last sent price" state file

    python gold_price_emailer.py send
        -> reads ./email/* and sends it via Gmail SMTP

SOURCE
------
Pulls from https://giavang.org/ — a Vietnamese gold-price aggregator whose
homepage is server-rendered (unlike most individual sellers' own sites,
e.g. SJC/DOJI/PNJ/Mi Hong, which load their price tables via JavaScript and
can't be read by a plain HTTP scraper) and already combines prices from
SJC, DOJI, PNJ, Bao Tin Minh Chau, Bao Tin Manh Hai, Phu Quy, Mi Hong, and
Ngoc Tham into one comparison table, covering the bulk of the top Vietnamese
gold sellers in a single fetch. Bao Tin Minh Chau also publishes its own
public price API, and baotinmanhhai.vn has its own price page directly, if
you'd rather add either of those back in as an additional source later.

Unlike the meme bot (which dedups by post ID so it never re-sends the same
meme), there's no natural "ID" for a price snapshot. Instead this dedups by
*content*: if SEND_ONLY_ON_CHANGE=true and the scraped prices are
byte-for-byte identical to the last run's, `generate` skips writing an
email at all. Defaults to "false" here (send every run) to match the
current setup - flip to "true" if you want change-only emails again.

SETUP
-----
1. Install dependencies:
       pip install requests beautifulsoup4 certifi

2. Create a Gmail "App Password" (regular Gmail passwords won't work with SMTP):
       - Go to https://myaccount.google.com/apppasswords
       - You need 2-Step Verification turned on first.
       - Create an app password for "Mail" and copy the 16-character code.

3. Set these as environment variables (see README.md for GitHub Actions
   secrets instead, if running in the cloud):
       export GMAIL_ADDRESS="youraddress@gmail.com"
       export GMAIL_APP_PASSWORD="16-char-app-password"
       export GOLD_RECIPIENT="where-to-send@example.com"
       export SEND_ONLY_ON_CHANGE="false"          # optional, default false
       export TIMEZONE="Asia/Ho_Chi_Minh"          # optional, for the subject line
       export SOURCE_URL="https://giavang.org/"    # optional
       export STATE_FILE="state/last_price.json"   # optional, dedup state file
       export ALLOW_INSECURE_SSL_FALLBACK="false"  # optional, last-resort TLS bypass

SCHEDULING
----------
See README.md / GitHub Actions workflow in this repo for running this on a
schedule in the cloud without needing your own computer on.

NOTE ON SCRAPING
-----------------
Always worth checking the current robots.txt / terms of whatever SOURCE_URL
you point this at before running it unattended long-term, e.g.:
    https://giavang.org/robots.txt
The page markup can also change at any time — if `generate` reports 0
parsed rows, open the page, inspect the price tables, and update the
parsing functions below.
"""

import hashlib
import json
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

import certifi
import requests
import urllib3
from bs4 import BeautifulSoup

# Only silences the warning when ALLOW_INSECURE_SSL_FALLBACK is actually
# used (see fetch_page) - the fallback path itself already prints its own
# explicit warning to stderr, so this just avoids a duplicate/confusing
# urllib3 warning on top of it.
if os.environ.get("ALLOW_INSECURE_SSL_FALLBACK", "false").lower() == "true":
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SOURCE_URL = os.environ.get("SOURCE_URL", "https://giavang.org/")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

EMAIL_DIR = "email"

# Dedup state: a JSON file holding a hash of the last-emailed price tables,
# so re-running periodically can optionally only email when prices actually
# moved, instead of sending the same numbers repeatedly. The workflow is
# responsible for fetching this file from the state branch before
# `generate` runs, and for committing the updated version back afterward —
# this script only reads/writes the local path.
STATE_FILE = os.environ.get("STATE_FILE", "state/last_price.json")
SEND_ONLY_ON_CHANGE = os.environ.get("SEND_ONLY_ON_CHANGE", "false").lower() == "true"

# Labels for each price table on the page, in the order they appear.
# giavang.org's homepage currently has two: gold bars, then gold rings.
# If the site adds/removes a table, extra ones fall back to "Bang N" and
# missing ones just don't show up - no crash either way.
TABLE_LABELS = ["Vàng Miếng (gold bars)", "Vàng Nhẫn 1 Chỉ (gold rings)"]


def load_last_hash(path=STATE_FILE):
    """Return the previous run's price-table hash, or None if there isn't
    one (missing/corrupt state is treated as "first run", not fatal).
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f).get("hash")
    except (json.JSONDecodeError, OSError) as e:
        print(f"  could not read {path} ({e}) — starting with empty dedup state", file=sys.stderr)
        return None


def save_last_hash(price_hash, path=STATE_FILE):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump({"hash": price_hash, "updated": datetime.utcnow().isoformat() + "Z"}, f)


def hash_tables(tables):
    canonical = json.dumps(tables, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


ALLOW_INSECURE_SSL_FALLBACK = os.environ.get("ALLOW_INSECURE_SSL_FALLBACK", "false").lower() == "true"


def fetch_page(url=SOURCE_URL):
    """
    GET the page, verifying TLS against certifi's CA bundle explicitly.

    requests normally already uses certifi, but pip can end up with a
    stale certifi wheel cached in a CI runner, which shows up as
    'unable to get local issuer certificate' even though the site's
    certificate is fine. Pointing verify= at certifi.where() explicitly
    (rather than requests' default resolution) sidesteps that, and the
    workflow also upgrades certifi on every run to keep it fresh.

    If that still fails, ALLOW_INSECURE_SSL_FALLBACK=true retries once
    with TLS verification disabled — an explicit opt-in last resort, since
    it means the connection could be tampered with undetected. Leave it
    "false" unless you've confirmed via README's troubleshooting section
    that the failure really is a broken certificate chain on the site's
    end, not a MITM.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, verify=certifi.where())
        resp.raise_for_status()
        return resp.text
    except requests.exceptions.SSLError as e:
        print(f"  TLS verification failed with certifi's CA bundle: {e}", file=sys.stderr)
        if not ALLOW_INSECURE_SSL_FALLBACK:
            print(
                "  Set ALLOW_INSECURE_SSL_FALLBACK=true to retry without verification "
                "as a last resort (see README troubleshooting section first).",
                file=sys.stderr,
            )
            raise
        print("  ALLOW_INSECURE_SSL_FALLBACK=true - retrying with TLS verification disabled.", file=sys.stderr)
        resp = requests.get(url, headers=HEADERS, timeout=15, verify=False)
        resp.raise_for_status()
        return resp.text


def parse_gold_prices(html):
    """
    Parse every price-comparison table on the page into a list of tables,
    each a list of {region, seller, buy, sell} rows - one row per unique
    seller, keeping the first (top) occurrence if a seller appears in more
    than one region.

    giavang.org's tables use an HTML rowspan on the "region" column, so
    only the first row of each region block actually has a region cell -
    subsequent rows for the same region omit it. _iter_table_rows tracks
    the "current region" across rows to handle that.
    """
    soup = BeautifulSoup(html, "html.parser")
    tables = []
    for table in soup.find_all("table"):
        rows = []
        seen_sellers = set()
        for region, seller, buy, sell in _iter_table_rows(table):
            if not seller or seller in seen_sellers or not _looks_like_price(buy):
                continue
            seen_sellers.add(seller)
            rows.append({"region": region, "seller": seller, "buy": buy, "sell": sell})
        if len(rows) >= 2:  # ignore stray unrelated tables (nav, footer, etc.)
            tables.append(rows)
    return tables


def _iter_table_rows(table):
    """Yield (region, seller, buy, sell) for each data row in a table,
    carrying the region forward across rowspan-merged cells.
    """
    current_region = None
    header_cells = {"Khu vực", "Hệ thống", "Mua vào", "Bán ra"}
    for tr in table.find_all("tr"):
        cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
        cells = [c for c in cells]
        if not cells or all(not c for c in cells):
            continue
        if cells[0] in header_cells:
            continue
        if len(cells) >= 4:
            current_region, seller, buy, sell = cells[0], cells[1], cells[2], cells[3]
        elif len(cells) == 3:
            seller, buy, sell = cells
        else:
            continue
        yield current_region, seller, buy, sell


def _looks_like_price(s):
    digits = re.sub(r"[^\d]", "", s)
    return digits.isdigit() and len(digits) >= 5


def _region_span(region):
    if not region:
        return ""
    return f" <span style='color:#999;font-size:12px'>({escape(region)})</span>"


def build_html(tables, source_url, timestamp):
    if not tables:
        body = (
            "<p>Could not parse any price rows this run. The page structure "
            f"may have changed — check <a href='{escape(source_url)}'>{escape(source_url)}</a> "
            "directly, and update the parsing functions in gold_price_emailer.py.</p>"
        )
    else:
        sections = []
        for i, rows in enumerate(tables):
            label = TABLE_LABELS[i] if i < len(TABLE_LABELS) else f"Bảng {i + 1}"
            row_html = "\n".join(
                f"<tr>"
                f"<td style='padding:6px 12px;border-bottom:1px solid #eee'><strong>{escape(r['seller'])}</strong>"
                f"{_region_span(r['region'])}</td>"
                f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{escape(r['buy'])}</td>"
                f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{escape(r['sell'])}</td>"
                f"</tr>"
                for r in rows
            )
            sections.append(f"""
        <h2 style="color:#b8860b;font-size:16px;margin:20px 0 8px;">{escape(label)}</h2>
        <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;max-width:600px;font-family:Arial,Helvetica,sans-serif;font-size:14px;">
          <thead>
            <tr style="background:#f5f5f5;">
              <th style="padding:8px 12px;text-align:left;">Đơn vị bán</th>
              <th style="padding:8px 12px;text-align:right;">Mua vào</th>
              <th style="padding:8px 12px;text-align:right;">Bán ra</th>
            </tr>
          </thead>
          <tbody>
            {row_html}
          </tbody>
        </table>""")
        body = "\n".join(sections)

    return f"""\
<html>
  <body style="margin:0; padding:20px; background:#f4f4f4; font-family:Arial,Helvetica,sans-serif;">
    <h1 style="color:#b8860b;">Giá vàng hôm nay - các đơn vị lớn tại Việt Nam</h1>
    <p style="color:#555;">Cập nhật {escape(timestamp)}</p>
    {body}
    <p style="color:#999; font-size:12px; margin-top:20px;">
      Nguồn: <a href="{escape(source_url)}">{escape(source_url)}</a> ·
      Đơn vị: nghìn đồng/lượng trừ khi ghi chú khác trên trang gốc ·
      Email tự động, chỉ mang tính tham khảo, không phải lời khuyên đầu tư.
    </p>
  </body>
</html>"""


def build_plain_text(tables, source_url, timestamp):
    lines = [f"Gia vang hom nay - cap nhat {timestamp}", ""]
    if not tables:
        lines.append("Could not parse any price rows this run.")
    else:
        for i, rows in enumerate(tables):
            label = TABLE_LABELS[i] if i < len(TABLE_LABELS) else f"Bang {i + 1}"
            lines.append(f"== {label} ==")
            for r in rows:
                region_suffix = f" ({r['region']})" if r["region"] else ""
                lines.append(f"{r['seller']}{region_suffix}: mua {r['buy']} / ban {r['sell']}")
            lines.append("")
    lines.append(f"Nguon: {source_url}")
    return "\n".join(lines)


def resolve_timestamp():
    timezone_name = os.environ.get("TIMEZONE", "Asia/Ho_Chi_Minh")
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(timezone_name))
    except Exception:
        now = datetime.now()
    return now, now.strftime("%H:%M %d/%m/%Y")


def cmd_generate():
    if os.path.exists(EMAIL_DIR):
        for f in os.listdir(EMAIL_DIR):
            os.remove(os.path.join(EMAIL_DIR, f))
    os.makedirs(EMAIL_DIR, exist_ok=True)

    print(f"Fetching {SOURCE_URL} ...")
    try:
        html = fetch_page()
    except requests.RequestException as e:
        print(f"Failed to fetch page: {e}", file=sys.stderr)
        sys.exit(1)

    tables = parse_gold_prices(html)
    total_rows = sum(len(rows) for rows in tables)
    print(f"Parsed {len(tables)} table(s), {total_rows} total price row(s).")

    price_hash = hash_tables(tables)
    last_hash = load_last_hash()

    if tables and SEND_ONLY_ON_CHANGE and price_hash == last_hash:
        print("Prices unchanged since last run and SEND_ONLY_ON_CHANGE=true - skipping email.")
        with open(os.path.join(EMAIL_DIR, "meta.json"), "w") as f:
            json.dump({"send": False}, f)
        return

    now, timestamp = resolve_timestamp()
    subject = f"Gia vang hom nay - {now.strftime('%d/%m/%Y %H:%M')}"
    html_body = build_html(tables, SOURCE_URL, timestamp)
    text_body = build_plain_text(tables, SOURCE_URL, timestamp)

    with open(os.path.join(EMAIL_DIR, "subject.txt"), "w") as f:
        f.write(subject)
    with open(os.path.join(EMAIL_DIR, "body.html"), "w") as f:
        f.write(html_body)
    with open(os.path.join(EMAIL_DIR, "body.txt"), "w") as f:
        f.write(text_body)
    with open(os.path.join(EMAIL_DIR, "meta.json"), "w") as f:
        json.dump({"send": True, "table_count": len(tables), "row_count": total_rows}, f)

    # Only persist the new hash once the email has actually been composed,
    # mirroring the meme bot's "mark as sent only after it's queued" logic.
    save_last_hash(price_hash)
    print(f"Generated email with {total_rows} row(s) across {len(tables)} table(s). Saved to ./{EMAIL_DIR}/")


def cmd_send():
    sender = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("GOLD_RECIPIENT")

    missing = [name for name, val in [
        ("GMAIL_ADDRESS", sender),
        ("GMAIL_APP_PASSWORD", app_password),
        ("GOLD_RECIPIENT", recipient),
    ] if not val]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    meta_path = os.path.join(EMAIL_DIR, "meta.json")
    if not os.path.exists(meta_path):
        print("No meta.json found - run 'generate' first.", file=sys.stderr)
        sys.exit(1)

    with open(meta_path) as f:
        meta = json.load(f)
    if not meta.get("send", False):
        print("Nothing to send this run (unchanged prices, or generate found no rows).")
        return

    with open(os.path.join(EMAIL_DIR, "subject.txt")) as f:
        subject = f.read()
    with open(os.path.join(EMAIL_DIR, "body.html")) as f:
        html_body = f.read()
    with open(os.path.join(EMAIL_DIR, "body.txt")) as f:
        text_body = f.read()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(sender, app_password)
        server.send_message(msg)

    print(f"Sent to {recipient}!")


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("generate", "send"):
        print("Usage: python gold_price_emailer.py [generate|send]", file=sys.stderr)
        sys.exit(1)

    if sys.argv[1] == "generate":
        cmd_generate()
    else:
        cmd_send()


if __name__ == "__main__":
    main()
