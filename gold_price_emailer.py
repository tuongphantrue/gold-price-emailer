#!/usr/bin/env python3
"""
Vietnam Gold Prices (multi-seller, summary + full detail) -> Email
(runs on GitHub Actions, no local computer needed)

Same shape as the 9gag-meme-emailer this is modeled on: fetches gold price
data, then emails an HTML digest via Gmail SMTP. Runs in two phases so the
workflow can persist dedup state *between* them (see the accompanying
GitHub Actions workflow):

    python gold_price_emailer.py generate
        -> scrapes the price tables, writes the composed email
           (subject/html/text) under ./email/, and updates the
           "last sent price" state file

    python gold_price_emailer.py send
        -> reads ./email/* and sends it via Gmail SMTP

SOURCE
------
Pulls from https://giavang.org/ — a Vietnamese gold-price aggregator whose
pages are server-rendered (unlike most individual sellers' own sites, e.g.
SJC/DOJI/PNJ/Mi Hong, which load their price tables via JavaScript and
can't be read by a plain HTTP scraper).

The email has five sections: gold summary, gold full detail per seller,
silver summary, silver full detail per seller, and price changes over
time (7/30/365 days, computed from a self-recorded daily snapshot since
no robots-compliant source publishes clean historical data for all these
sellers - silver additionally gets an immediate "today" fallback from
giahanghoa.net's own reported 24h change, available from day one before
the self-tracked history has accumulated enough days).

Unlike the meme bot (which dedups by post ID so it never re-sends the same
meme), there's no natural "ID" for a price snapshot. Instead this dedups by
*content*: if SEND_ONLY_ON_CHANGE=true and the scraped prices are
byte-for-byte identical to the last run's, `generate` skips writing an
email at all. Defaults to "false" (send every run).

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
       export SOURCE_URL="https://giavang.org/"    # optional, gold summary page
       export SILVER_URL="https://giahanghoa.net/gia-bac"  # optional, silver summary page
       export STATE_FILE="state/last_price.json"   # optional, dedup state file
       export PRICE_HISTORY_FILE="state/price_history.json"  # optional, daily snapshot history
       export ALLOW_INSECURE_SSL_FALLBACK="false"  # optional, last-resort TLS bypass

SCHEDULING
----------
See README.md / GitHub Actions workflow in this repo for running this on a
schedule in the cloud without needing your own computer on.

NOTE ON SCRAPING
-----------------
Always worth checking the current robots.txt / terms of whatever sites
this is pointed at before running it unattended long-term, e.g.:
    https://giavang.org/robots.txt , https://giahanghoa.net/robots.txt
The page markup can also change at any time — if `generate` reports 0
parsed rows for a section, open the relevant page, inspect the price
table, and update the parsing functions below.
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
DETAIL_BASE_URL = "https://giavang.org/trong-nuoc/"
SILVER_URL = os.environ.get("SILVER_URL", "https://giahanghoa.net/gia-bac")

# Some silver brands have their own dedicated, server-rendered price page
# with a much fuller product breakdown than the giahanghoa.net comparison
# table gives. Where we have one, fetch_silver_details prefers it; brands
# not listed here (or whose page fails/returns nothing) fall back to that
# brand's own row(s) already present in the summary table instead of being
# dropped. (DOJI has a giabac.doji.vn page too, but it loads prices via
# JavaScript - "Đang tải..." with no static data - so it's not usable here.)
SILVER_DETAIL_PAGES = {
    "Phú Quý": "https://giabac.phuquygroup.vn/",
    "ANCARAT": "https://giabac.ancarat.com/",
}

# Used to split giahanghoa.net's combined "Brand ProductName" cell back into
# its two parts (see _split_brand_product). Longest names first, so e.g.
# "Bảo Tín Minh Châu" matches before a shorter unrelated prefix could.
SILVER_BRANDS = sorted(
    [
        "Phú Quý", "Bảo Tín Minh Châu", "BTMC", "Bảo Tín Mạnh Hải", "BTMH",
        "DOJI", "PNJ", "ANCARAT", "Ancarat", "Kim Ngân Phúc", "Mi Hồng",
        "Ngọc Thẩm", "SJC",
    ],
    key=len,
    reverse=True,
)

# (display name, URL slug) for each seller's own detail page on giavang.org
SELLERS = [
    ("SJC", "sjc"),
    ("DOJI", "doji"),
    ("PNJ", "pnj"),
    ("Bảo Tín Minh Châu", "bao-tin-minh-chau"),
    ("Bảo Tín Mạnh Hải", "bao-tin-manh-hai"),
    ("Phú Quý", "phu-quy"),
    ("Mi Hồng", "mi-hong"),
    ("Ngọc Thẩm", "ngoc-tham"),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

EMAIL_DIR = "email"

# Dedup state: a JSON file holding a hash of the last-emailed price data,
# so re-running periodically can optionally only email when prices actually
# moved, instead of sending the same numbers repeatedly. The workflow is
# responsible for fetching this file from the state branch before
# `generate` runs, and for committing the updated version back afterward —
# this script only reads/writes the local path.
STATE_FILE = os.environ.get("STATE_FILE", "state/last_price.json")
SEND_ONLY_ON_CHANGE = os.environ.get("SEND_ONLY_ON_CHANGE", "false").lower() == "true"

# A second state file (also persisted on the gold-price-state branch,
# alongside STATE_FILE) holding one price snapshot per calendar day. Used
# to compute the "Biến động giá" (price changes) section - how today's
# sell price compares to ~7/30/365 days ago. Re-running within the same
# day overwrites that day's entry rather than adding a new one, so this
# grows by about 1 entry/day regardless of how often the workflow runs.
# Entries older than HISTORY_MAX_DAYS are pruned on save to bound growth.
PRICE_HISTORY_FILE = os.environ.get("PRICE_HISTORY_FILE", "state/price_history.json")
HISTORY_MAX_DAYS = 400
# (display label, days ago) - the periods shown in the changes section.
HISTORY_PERIODS = [("7 ngày", 7), ("30 ngày", 30), ("1 năm", 365)]
# A historical snapshot is treated as "the Nth-day-ago price" if it falls
# within this many days of the exact target date - since the workflow
# only takes one snapshot per calendar day, there's rarely an exact match.
HISTORY_MATCH_TOLERANCE_DAYS = 3

# Labels for each table on the summary page, in the order they appear.
SUMMARY_TABLE_LABELS = ["Vàng Miếng (gold bars)", "Vàng Nhẫn 1 Chỉ (gold rings)"]

ALLOW_INSECURE_SSL_FALLBACK = os.environ.get("ALLOW_INSECURE_SSL_FALLBACK", "false").lower() == "true"


def load_last_hash(path=STATE_FILE):
    """Return the previous run's price-data hash, or None if there isn't
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


def hash_data(data):
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_vnd_number(s):
    """'148.400.000' / '2,199,000' -> 148400000 - strips whatever
    separator style the source site uses and keeps only the digits.
    Returns None if there are no digits (e.g. a blank/"-" price cell).
    """
    digits = re.sub(r"[^\d]", "", s or "")
    return int(digits) if digits else None


def build_today_snapshot(summary_tables, silver):
    """
    Build today's price snapshot for history tracking: sell ("Bán ra")
    price per gold-summary row and per silver-summary row, keyed by
    label so it can be compared against past snapshots later. Scoped to
    the summary-level data (not the full per-seller detail breakdown) to
    keep the history file small and the changes section readable.
    """
    gold = {}
    for i, rows in enumerate(summary_tables):
        table_key = f"table_{i}"
        gold[table_key] = {}
        for r in rows:
            sell = _parse_vnd_number(r["sell"])
            if sell is not None:
                gold[table_key][r["label"]] = sell

    silver_snap = {}
    if "rows" in silver:
        for r in silver["rows"]:
            sell = _parse_vnd_number(r["sell"])
            if sell is not None:
                silver_snap[f"{r['brand']} - {r['product']}"] = sell

    return {"gold": gold, "silver": silver_snap}


def load_history(path=PRICE_HISTORY_FILE):
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  could not read {path} ({e}) — starting with empty price history", file=sys.stderr)
        return {}


def save_history(history, today_str, today_snapshot, path=PRICE_HISTORY_FILE):
    history = dict(history)
    history[today_str] = today_snapshot
    cutoff = (datetime.strptime(today_str, "%Y-%m-%d") - _timedelta(days=HISTORY_MAX_DAYS)).strftime("%Y-%m-%d")
    history = {d: snap for d, snap in history.items() if d >= cutoff}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(history, f, ensure_ascii=False)
    return history


def _timedelta(days):
    from datetime import timedelta
    return timedelta(days=days)


def _closest_snapshot_for_period(history, today_str, days_ago):
    """Find the history entry closest to `days_ago` days before today,
    within HISTORY_MATCH_TOLERANCE_DAYS. Returns (date_str, snapshot) or
    (None, None) if nothing in range (e.g. not enough history yet).
    """
    today = datetime.strptime(today_str, "%Y-%m-%d")
    target = today - _timedelta(days=days_ago)
    best_date, best_snapshot, best_diff = None, None, None
    for date_str, snapshot in history.items():
        if date_str == today_str:
            continue
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        diff = abs((d - target).days)
        if diff <= HISTORY_MATCH_TOLERANCE_DAYS and (best_diff is None or diff < best_diff):
            best_date, best_snapshot, best_diff = date_str, snapshot, diff
    return best_date, best_snapshot


def compute_price_changes(history, today_str, today_snapshot, silver_source_changes=None):
    """
    Build the data for the "Biến động giá" section: for each gold-summary
    table and for silver, for each item, look up its sell price at each
    HISTORY_PERIODS point and compute the diff/percent change. Items or
    periods without a close-enough historical snapshot are marked
    unavailable rather than guessed at.

    silver_source_changes (optional): {"brand - product": "+4.000"} - the
    source site's own reported today-vs-yesterday change (see
    parse_silver_table), attached to each silver row as "source_today" so
    it's available immediately even on day one, before price_history.json
    has accumulated enough days for the 7-day/30-day/1-year columns. No
    equivalent exists for gold on any robots-compliant source we use, so
    gold rows don't get this field.
    """
    silver_source_changes = silver_source_changes or {}
    period_snapshots = {
        label: _closest_snapshot_for_period(history, today_str, days)[1] for label, days in HISTORY_PERIODS
    }

    def changes_for(current_sell, hist_key_path):
        """hist_key_path is a function(snapshot) -> value-or-None, so this
        works for both gold (snapshot['gold'][table_key][label]) and
        silver (snapshot['silver'][key]) lookups."""
        out = {}
        for label, _days in HISTORY_PERIODS:
            snap = period_snapshots[label]
            hist_value = hist_key_path(snap) if snap else None
            if hist_value is None or current_sell is None:
                out[label] = None
                continue
            diff = current_sell - hist_value
            pct = (diff / hist_value * 100) if hist_value else None
            out[label] = {"diff": diff, "pct": pct}
        return out

    gold_changes = []
    for table_key, items in today_snapshot["gold"].items():
        rows = []
        for label, current_sell in items.items():
            rows.append({
                "label": label,
                "current_sell": current_sell,
                "changes": changes_for(current_sell, lambda snap, tk=table_key, lb=label: snap.get("gold", {}).get(tk, {}).get(lb)),
            })
        gold_changes.append(rows)

    silver_changes = []
    for key, current_sell in today_snapshot["silver"].items():
        silver_changes.append({
            "label": key,
            "current_sell": current_sell,
            "changes": changes_for(current_sell, lambda snap, k=key: snap.get("silver", {}).get(k)),
            "source_today": silver_source_changes.get(key),
        })

    return {"gold": gold_changes, "silver": silver_changes}


def fetch_page(url):
    """
    GET a page, verifying TLS against certifi's CA bundle explicitly.

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


def parse_comparison_tables(html):
    """
    Parse every price table on a giavang.org page into a list of tables,
    each a list of {region, label, buy, sell} rows - one row per unique
    label (a seller name on the summary page, or a product/gold type on a
    per-seller detail page), keeping the first (top) occurrence if a label
    appears in more than one region.

    giavang.org's tables use an HTML rowspan on the "region" column, so
    only the first row of each region block actually has a region cell -
    subsequent rows for the same region omit it. _iter_table_rows tracks
    the "current region" across rows to handle that.
    """
    soup = BeautifulSoup(html, "html.parser")
    tables = []
    for table in soup.find_all("table"):
        rows = []
        seen_labels = set()
        for region, label, buy, sell in _iter_table_rows(table):
            if not label or label in seen_labels or not _looks_like_price(buy):
                continue
            seen_labels.add(label)
            rows.append({"region": region, "label": label, "buy": buy, "sell": sell})
        if len(rows) >= 1:  # ignore stray unrelated tables (nav, footer, etc.)
            tables.append(rows)
    return tables


def _iter_table_rows(table):
    """Yield (region, label, buy, sell) for each data row in a table,
    carrying the region forward across rowspan-merged cells.
    """
    current_region = None
    header_cells = {"Khu vực", "Hệ thống", "Loại vàng", "Mua vào", "Bán ra"}
    for tr in table.find_all("tr"):
        cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
        if not cells or all(not c for c in cells):
            continue
        if cells[0] in header_cells:
            continue
        if len(cells) >= 4:
            current_region, label, buy, sell = cells[0], cells[1], cells[2], cells[3]
        elif len(cells) == 3:
            label, buy, sell = cells
        else:
            continue
        yield current_region, label, buy, sell


def _looks_like_price(s):
    digits = re.sub(r"[^\d]", "", s)
    return digits.isdigit() and len(digits) >= 5


def parse_silver_table(html):
    """
    Parse giahanghoa.net's silver comparison table into a list of
    {brand, product, buy, sell, change_24h} rows. That table's first
    column combines brand and product name in one cell (e.g. "Phú Quý
    BẠC MIẾNG PHÚ QUÝ 999 1 LƯỢNG"), so _split_brand_product pulls them
    back apart using a known-brand-name match.

    change_24h (if present) is the site's own reported today-vs-yesterday
    change, e.g. "+4.000" - used as an immediate fallback in the changes
    section for the "today" column, since our self-tracked
    price_history.json needs to accumulate at least a day before it can
    report anything, whereas the source already publishes this every run.
    No equivalent same-day (or 7-day/30-day/1-year) column exists on any
    robots-compliant source we use for gold, so gold's changes section
    stays purely history-based.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows_out = []
    header_names = {"Thương hiệu", "Mua vào", "Bán ra", "Biến động 24h"}
    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if not header_row:
            continue
        header_cells_list = [c.get_text(strip=True) for c in header_row.find_all(["td", "th"])]
        header_texts = set(header_cells_list)
        if not header_texts & header_names:
            continue  # not the table we're looking for
        change_idx = header_cells_list.index("Biến động 24h") if "Biến động 24h" in header_cells_list else None
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if not cells or cells[0] in header_names:
                continue
            if len(cells) < 3 or not _looks_like_price(cells[1]):
                continue
            brand, product = _split_brand_product(cells[0])
            change_24h = cells[change_idx] if change_idx is not None and change_idx < len(cells) else None
            rows_out.append({
                "brand": brand, "product": product, "buy": cells[1], "sell": cells[2],
                "change_24h": change_24h,
            })
    return rows_out


def _split_brand_product(combo):
    for brand in SILVER_BRANDS:
        if combo.startswith(brand):
            product = combo[len(brand):].strip()
            return brand, product or combo
    return "Khác", combo  # unrecognized brand prefix - keep the row, just unlabeled


def fetch_silver():
    """Fetch + parse the silver comparison table. Returns {"rows": [...]}
    on success or {"error": "...", "url": SILVER_URL} on failure - a
    silver-fetch problem never aborts the gold sections of the email.
    """
    try:
        html = fetch_page(SILVER_URL)
        rows = parse_silver_table(html)
        if not rows:
            return {"error": "Could not parse any rows from this page.", "url": SILVER_URL}
        return {"rows": rows, "url": SILVER_URL}
    except requests.RequestException as e:
        print(f"  Failed to fetch silver prices: {e}", file=sys.stderr)
        return {"error": str(e), "url": SILVER_URL}


def parse_generic_price_table(html):
    """
    Generic parser for simple "product name + price columns" tables (no
    region rowspan) - used for brands' own dedicated silver pages like
    Phu Quy's and Ancarat's. Detects the buy/sell column indices from the
    header row by matching common header labels instead of assuming a
    fixed position, since column order/count (e.g. an extra unit column,
    or "Bán ra" listed before "Mua vào") differs by site. Rows that are
    really section-header dividers (e.g. "NHÓM BẠC TÍCH TRỮ...") get
    skipped automatically since their price cells are empty.
    """
    soup = BeautifulSoup(html, "html.parser")
    buy_labels = {"mua vào", "giá mua vào"}
    sell_labels = {"bán ra", "giá bán ra"}
    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if not header_row:
            continue
        header_cells = [c.get_text(strip=True).lower() for c in header_row.find_all(["td", "th"])]
        buy_idx = next((i for i, h in enumerate(header_cells) if h in buy_labels), None)
        sell_idx = next((i for i, h in enumerate(header_cells) if h in sell_labels), None)
        if buy_idx is None or sell_idx is None:
            continue
        rows_out = []
        for tr in table.find_all("tr")[1:]:
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            if not cells or all(not c for c in cells):
                continue
            if len(cells) <= max(buy_idx, sell_idx):
                continue
            product, buy, sell = cells[0], cells[buy_idx], cells[sell_idx]
            if not product or not _looks_like_price(buy):
                continue  # section-header row or malformed row
            rows_out.append({"product": product, "buy": buy, "sell": sell})
        if rows_out:
            return rows_out
    return []


def fetch_silver_details(summary_rows):
    """
    Build a full per-brand silver detail section. Brands with a known
    dedicated page (SILVER_DETAIL_PAGES) get the fuller breakdown from
    that page; everyone else falls back to their own row(s) already
    present in the summary comparison table, so no brand is dropped even
    without a dedicated page.
    """
    by_brand = {}
    for r in summary_rows:  # preserves first-seen order from the summary table
        by_brand.setdefault(r["brand"], []).append({"product": r["product"], "buy": r["buy"], "sell": r["sell"]})

    details = {}
    for brand, fallback_products in by_brand.items():
        url = SILVER_DETAIL_PAGES.get(brand)
        if not url:
            details[brand] = {"products": fallback_products, "source": None}
            continue
        try:
            html = fetch_page(url)
            products = parse_generic_price_table(html)
            details[brand] = {"products": products or fallback_products, "source": url}
        except requests.RequestException as e:
            print(f"  Failed to fetch {brand}'s silver detail page: {e}", file=sys.stderr)
            details[brand] = {"products": fallback_products, "source": None}
    return details


def fetch_summary():
    """Fetch + parse the homepage comparison tables (one row per seller)."""
    html = fetch_page(SOURCE_URL)
    return parse_comparison_tables(html)


def fetch_seller_details():
    """
    Fetch + parse each seller's own detail page. Returns an ordered dict
    (plain dict, Python 3.7+ preserves insertion order) mapping seller
    display name -> {"tables": [...]} on success, or
    {"error": "..."} if that one seller's page failed to fetch/parse -
    a single seller's failure doesn't abort the whole run.
    """
    details = {}
    for name, slug in SELLERS:
        url = f"{DETAIL_BASE_URL}{slug}/"
        print(f"Fetching detail page for {name} ({url}) ...")
        try:
            html = fetch_page(url)
            tables = parse_comparison_tables(html)
            if not tables:
                details[name] = {"error": "Could not parse any rows from this page.", "url": url}
            else:
                details[name] = {"tables": tables, "url": url}
        except requests.RequestException as e:
            print(f"  Failed to fetch {name}'s detail page: {e}", file=sys.stderr)
            details[name] = {"error": str(e), "url": url}
    return details


def _region_span(region):
    if not region:
        return ""
    return f" <span style='color:#999;font-size:12px'>({escape(region)})</span>"


def _table_html(rows, label_header):
    row_html = "\n".join(
        f"<tr>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee'><strong>{escape(r['label'])}</strong>"
        f"{_region_span(r['region'])}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{escape(r['buy'])}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{escape(r['sell'])}</td>"
        f"</tr>"
        for r in rows
    )
    return f"""
        <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;max-width:600px;font-family:Arial,Helvetica,sans-serif;font-size:14px;">
          <thead>
            <tr style="background:#f5f5f5;">
              <th style="padding:8px 12px;text-align:left;">{escape(label_header)}</th>
              <th style="padding:8px 12px;text-align:right;">Mua vào</th>
              <th style="padding:8px 12px;text-align:right;">Bán ra</th>
            </tr>
          </thead>
          <tbody>
            {row_html}
          </tbody>
        </table>"""


def _silver_table_html(rows):
    row_html = "\n".join(
        f"<tr>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee'><strong>{escape(r['brand'])}</strong></td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee'>{escape(r['product'])}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{escape(r['buy'])}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{escape(r['sell'])}</td>"
        f"</tr>"
        for r in rows
    )
    return f"""
        <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;max-width:600px;font-family:Arial,Helvetica,sans-serif;font-size:14px;">
          <thead>
            <tr style="background:#f5f5f5;">
              <th style="padding:8px 12px;text-align:left;">Thương hiệu</th>
              <th style="padding:8px 12px;text-align:left;">Sản phẩm</th>
              <th style="padding:8px 12px;text-align:right;">Mua vào</th>
              <th style="padding:8px 12px;text-align:right;">Bán ra</th>
            </tr>
          </thead>
          <tbody>
            {row_html}
          </tbody>
        </table>"""


def _silver_detail_table_html(products):
    row_html = "\n".join(
        f"<tr>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee'>{escape(p['product'])}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{escape(p['buy'])}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{escape(p['sell'])}</td>"
        f"</tr>"
        for p in products
    )
    return f"""
        <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;max-width:600px;font-family:Arial,Helvetica,sans-serif;font-size:14px;">
          <thead>
            <tr style="background:#f5f5f5;">
              <th style="padding:8px 12px;text-align:left;">Sản phẩm</th>
              <th style="padding:8px 12px;text-align:right;">Mua vào</th>
              <th style="padding:8px 12px;text-align:right;">Bán ra</th>
            </tr>
          </thead>
          <tbody>
            {row_html}
          </tbody>
        </table>"""


def _format_vnd(n):
    """148400000 -> '148.400.000' (Vietnamese thousands separator)."""
    return f"{n:,.0f}".replace(",", ".")


def _format_diff(change):
    if not change:
        return "Chưa đủ dữ liệu"
    diff, pct = change["diff"], change["pct"]
    sign = "+" if diff >= 0 else ""
    pct_str = f" ({sign}{pct:.2f}%)" if pct is not None else ""
    color = "#1a7a1a" if diff > 0 else ("#a33" if diff < 0 else "#666")
    return f"<span style='color:{color}'>{sign}{_format_vnd(diff)}{pct_str}</span>"


def _changes_table_html(rows, show_source_today=False):
    period_headers = "".join(
        f"<th style='padding:8px 12px;text-align:right;'>{escape(label)}</th>" for label, _ in HISTORY_PERIODS
    )
    source_header = (
        "<th style='padding:8px 12px;text-align:right;'>Hôm nay (nguồn)</th>" if show_source_today else ""
    )
    row_html = "\n".join(
        "<tr>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee'>{escape(r['label'])}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{_format_vnd(r['current_sell'])}</td>"
        + (
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right;font-size:12px'>"
            f"{escape(r.get('source_today') or 'Không có')}</td>"
            if show_source_today else ""
        )
        + "".join(
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right;font-size:12px'>"
            f"{_format_diff(r['changes'][label])}</td>"
            for label, _ in HISTORY_PERIODS
        )
        + "</tr>"
        for r in rows
    )
    return f"""
        <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;max-width:800px;font-family:Arial,Helvetica,sans-serif;font-size:14px;">
          <thead>
            <tr style="background:#f5f5f5;">
              <th style="padding:8px 12px;text-align:left;">Sản phẩm</th>
              <th style="padding:8px 12px;text-align:right;">Bán ra hiện tại</th>
              {source_header}
              {period_headers}
            </tr>
          </thead>
          <tbody>
            {row_html}
          </tbody>
        </table>"""


def build_html(summary_tables, details, silver, silver_details, price_changes, source_url, timestamp):
    # --- Section 1: summary comparison ---
    if not summary_tables:
        summary_html = (
            "<p>Could not parse the summary comparison table this run. "
            f"Check <a href='{escape(source_url)}'>{escape(source_url)}</a> directly.</p>"
        )
    else:
        parts = []
        for i, rows in enumerate(summary_tables):
            label = SUMMARY_TABLE_LABELS[i] if i < len(SUMMARY_TABLE_LABELS) else f"Bảng {i + 1}"
            parts.append(f'<h3 style="color:#b8860b;font-size:15px;margin:16px 0 6px;">{escape(label)}</h3>')
            parts.append(_table_html(rows, "Đơn vị bán"))
        summary_html = "\n".join(parts)

    # --- Section 2: full detail per seller ---
    detail_parts = []
    for name, info in details.items():
        detail_parts.append(f'<h3 style="color:#b8860b;font-size:15px;margin:20px 0 6px;">{escape(name)}</h3>')
        if "error" in info:
            detail_parts.append(
                f"<p style='color:#a33;font-size:13px;'>Không lấy được dữ liệu chi tiết lần này "
                f"({escape(info['error'])}). Xem trực tiếp tại "
                f"<a href='{escape(info['url'])}'>{escape(info['url'])}</a>.</p>"
            )
            continue
        for rows in info["tables"]:
            detail_parts.append(_table_html(rows, "Loại vàng"))
    detail_html = "\n".join(detail_parts) if detail_parts else "<p>Không có dữ liệu chi tiết.</p>"

    # --- Section 3: silver ---
    if "error" in silver:
        silver_html = (
            f"<p style='color:#a33;font-size:13px;'>Không lấy được giá bạc lần này "
            f"({escape(silver['error'])}). Xem trực tiếp tại "
            f"<a href='{escape(silver['url'])}'>{escape(silver['url'])}</a>.</p>"
        )
    else:
        silver_html = _silver_table_html(silver["rows"])

    silver_detail_parts = []
    for brand, info in silver_details.items():
        silver_detail_parts.append(f'<h3 style="color:#666;font-size:15px;margin:20px 0 6px;">{escape(brand)}</h3>')
        if not info["source"]:
            silver_detail_parts.append(
                "<p style='color:#999;font-size:12px;margin:0 0 6px;'>"
                "(Không có trang chi tiết riêng cho đơn vị này - hiển thị dữ liệu từ bảng tổng hợp.)</p>"
            )
        silver_detail_parts.append(_silver_detail_table_html(info["products"]))
    silver_detail_html = "\n".join(silver_detail_parts) if silver_detail_parts else "<p>Không có dữ liệu chi tiết.</p>"

    # --- Section 5: price changes over time ---
    changes_parts = []
    for i, rows in enumerate(price_changes["gold"]):
        if not rows:
            continue
        label = SUMMARY_TABLE_LABELS[i] if i < len(SUMMARY_TABLE_LABELS) else f"Bảng {i + 1}"
        changes_parts.append(f'<h3 style="color:#b8860b;font-size:15px;margin:16px 0 6px;">{escape(label)}</h3>')
        changes_parts.append(_changes_table_html(rows))
    if price_changes["silver"]:
        changes_parts.append('<h3 style="color:#666;font-size:15px;margin:20px 0 6px;">Bạc</h3>')
        changes_parts.append(_changes_table_html(price_changes["silver"], show_source_today=True))
    changes_html = "\n".join(changes_parts) if changes_parts else "<p>Không có dữ liệu để so sánh.</p>"

    return f"""\
<html>
  <body style="margin:0; padding:20px; background:#f4f4f4; font-family:Arial,Helvetica,sans-serif;">
    <h1 style="color:#b8860b;">Giá vàng &amp; bạc hôm nay - các đơn vị lớn tại Việt Nam</h1>
    <p style="color:#555;">Cập nhật {escape(timestamp)}</p>

    <h2 style="color:#333;font-size:18px;border-bottom:2px solid #b8860b;padding-bottom:4px;">Vàng - Tổng hợp so sánh giữa các đơn vị</h2>
    {summary_html}

    <h2 style="color:#333;font-size:18px;border-bottom:2px solid #b8860b;padding-bottom:4px;margin-top:28px;">Vàng - Chi tiết đầy đủ theo từng đơn vị</h2>
    {detail_html}

    <h2 style="color:#333;font-size:18px;border-bottom:2px solid #888;padding-bottom:4px;margin-top:28px;">Bạc - So sánh giữa các đơn vị</h2>
    {silver_html}

    <h2 style="color:#333;font-size:18px;border-bottom:2px solid #888;padding-bottom:4px;margin-top:28px;">Bạc - Chi tiết đầy đủ theo từng đơn vị</h2>
    {silver_detail_html}

    <h2 style="color:#333;font-size:18px;border-bottom:2px solid #555;padding-bottom:4px;margin-top:28px;">Biến động giá (so với 7 ngày / 30 ngày / 1 năm trước)</h2>
    {changes_html}
    <p style="color:#999;font-size:12px;margin-top:4px;">Biến động dựa trên lịch sử tự ghi nhận từ lần đầu email này chạy - có thể chưa đủ dữ liệu cho mốc 30 ngày/1 năm ngay từ đầu, sẽ đầy đủ dần theo thời gian.</p>

    <p style="color:#999; font-size:12px; margin-top:20px;">
      Nguồn: <a href="{escape(source_url)}">{escape(source_url)}</a> (vàng),
      <a href="{escape(SILVER_URL)}">{escape(SILVER_URL)}</a> (bạc) ·
      Đơn vị: nghìn đồng/lượng trừ khi ghi chú khác trên trang gốc ·
      Email tự động, chỉ mang tính tham khảo, không phải lời khuyên đầu tư.
    </p>
  </body>
</html>"""


def build_plain_text(summary_tables, details, silver, silver_details, price_changes, source_url, timestamp):
    lines = [f"Gia vang & bac hom nay - cap nhat {timestamp}", "", "== VANG - TONG HOP =="]
    if not summary_tables:
        lines.append("Could not parse the summary comparison table this run.")
    else:
        for i, rows in enumerate(summary_tables):
            label = SUMMARY_TABLE_LABELS[i] if i < len(SUMMARY_TABLE_LABELS) else f"Bang {i + 1}"
            lines.append(f"-- {label} --")
            for r in rows:
                region_suffix = f" ({r['region']})" if r["region"] else ""
                lines.append(f"{r['label']}{region_suffix}: mua {r['buy']} / ban {r['sell']}")
            lines.append("")

    lines.append("== VANG - CHI TIET THEO TUNG DON VI ==")
    for name, info in details.items():
        lines.append(f"-- {name} --")
        if "error" in info:
            lines.append(f"  Khong lay duoc du lieu ({info['error']}). Xem tai {info['url']}")
            continue
        for rows in info["tables"]:
            for r in rows:
                region_suffix = f" ({r['region']})" if r["region"] else ""
                lines.append(f"  {r['label']}{region_suffix}: mua {r['buy']} / ban {r['sell']}")
        lines.append("")

    lines.append("== BAC - SO SANH GIUA CAC DON VI ==")
    if "error" in silver:
        lines.append(f"  Khong lay duoc gia bac ({silver['error']}). Xem tai {silver['url']}")
    else:
        for r in silver["rows"]:
            lines.append(f"  {r['brand']} - {r['product']}: mua {r['buy']} / ban {r['sell']}")
    lines.append("")

    lines.append("== BAC - CHI TIET DAY DU THEO TUNG DON VI ==")
    for brand, info in silver_details.items():
        lines.append(f"-- {brand} --")
        if not info["source"]:
            lines.append("  (khong co trang chi tiet rieng - du lieu tu bang tong hop)")
        for p in info["products"]:
            lines.append(f"  {p['product']}: mua {p['buy']} / ban {p['sell']}")
        lines.append("")

    lines.append(f"Nguon vang: {source_url}")
    lines.append(f"Nguon bac: {SILVER_URL}")

    def _fmt_change_line(label, current_sell, changes, source_today=None):
        parts = [f"{label}: ban ra {current_sell:,}".replace(",", ".")]
        if source_today is not None:
            parts.append(f"hom nay (nguon): {source_today or 'khong co'}")
        for period_label, _days in HISTORY_PERIODS:
            c = changes[period_label]
            if not c:
                parts.append(f"{period_label}: chua du du lieu")
            else:
                sign = "+" if c["diff"] >= 0 else ""
                pct = f" ({sign}{c['pct']:.2f}%)" if c["pct"] is not None else ""
                parts.append(f"{period_label}: {sign}{c['diff']:,}".replace(",", ".") + pct)
        return " | ".join(parts)

    lines.append("")
    lines.append("== BIEN DONG GIA (so voi 7 ngay / 30 ngay / 1 nam truoc) ==")
    for i, rows in enumerate(price_changes["gold"]):
        if not rows:
            continue
        label = SUMMARY_TABLE_LABELS[i] if i < len(SUMMARY_TABLE_LABELS) else f"Bang {i + 1}"
        lines.append(f"-- {label} --")
        for r in rows:
            lines.append("  " + _fmt_change_line(r["label"], r["current_sell"], r["changes"]))
        lines.append("")
    if price_changes["silver"]:
        lines.append("-- Bac --")
        for r in price_changes["silver"]:
            lines.append("  " + _fmt_change_line(r["label"], r["current_sell"], r["changes"], r.get("source_today")))

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

    print(f"Fetching summary page {SOURCE_URL} ...")
    try:
        summary_tables = fetch_summary()
    except requests.RequestException as e:
        print(f"Failed to fetch summary page: {e}", file=sys.stderr)
        sys.exit(1)
    summary_rows = sum(len(rows) for rows in summary_tables)
    print(f"Summary: parsed {len(summary_tables)} table(s), {summary_rows} row(s).")

    details = fetch_seller_details()
    detail_rows = sum(len(t) for info in details.values() for t in info.get("tables", []))
    failed = [name for name, info in details.items() if "error" in info]
    print(f"Details: {len(details) - len(failed)}/{len(details)} sellers OK, {detail_rows} total row(s).")
    if failed:
        print(f"  Failed sellers this run: {', '.join(failed)}", file=sys.stderr)

    print(f"Fetching silver prices {SILVER_URL} ...")
    silver = fetch_silver()
    silver_rows = len(silver.get("rows", []))
    print(f"Silver: {silver_rows} row(s)." if "rows" in silver else f"Silver: failed ({silver['error']}).")

    silver_details = fetch_silver_details(silver.get("rows", []))
    silver_detail_count = sum(len(info["products"]) for info in silver_details.values())
    dedicated_ok = [b for b, info in silver_details.items() if info["source"]]
    print(f"Silver detail: {len(dedicated_ok)}/{len(silver_details)} brand(s) via dedicated page, {silver_detail_count} total product row(s).")

    now, timestamp = resolve_timestamp()
    today_str = now.strftime("%Y-%m-%d")

    # Record today's snapshot and compute changes against *prior* history
    # (i.e. compute first, then save - otherwise today would count as its
    # own "history" and every diff would show zero).
    today_snapshot = build_today_snapshot(summary_tables, silver)
    history = load_history()
    silver_source_changes = {}
    if "rows" in silver:
        for r in silver["rows"]:
            silver_source_changes[f"{r['brand']} - {r['product']}"] = r.get("change_24h")
    price_changes = compute_price_changes(history, today_str, today_snapshot, silver_source_changes)
    history = save_history(history, today_str, today_snapshot)
    changes_count = sum(len(rows) for rows in price_changes["gold"]) + len(price_changes["silver"])
    print(f"Price changes: computed for {changes_count} item(s) against {len(history)} day(s) of history.")

    combined = {
        "summary": summary_tables,
        "details": details,
        "silver": silver,
        "silver_details": silver_details,
        "price_changes": price_changes,
    }
    price_hash = hash_data(combined)
    last_hash = load_last_hash()

    if summary_tables and SEND_ONLY_ON_CHANGE and price_hash == last_hash:
        print("Prices unchanged since last run and SEND_ONLY_ON_CHANGE=true - skipping email.")
        with open(os.path.join(EMAIL_DIR, "meta.json"), "w") as f:
            json.dump({"send": False}, f)
        return

    subject = f"Gia vang & bac hom nay - {now.strftime('%d/%m/%Y %H:%M')}"
    html_body = build_html(summary_tables, details, silver, silver_details, price_changes, SOURCE_URL, timestamp)
    text_body = build_plain_text(summary_tables, details, silver, silver_details, price_changes, SOURCE_URL, timestamp)

    with open(os.path.join(EMAIL_DIR, "subject.txt"), "w") as f:
        f.write(subject)
    with open(os.path.join(EMAIL_DIR, "body.html"), "w") as f:
        f.write(html_body)
    with open(os.path.join(EMAIL_DIR, "body.txt"), "w") as f:
        f.write(text_body)
    with open(os.path.join(EMAIL_DIR, "meta.json"), "w") as f:
        json.dump(
            {
                "send": True,
                "summary_rows": summary_rows,
                "detail_rows": detail_rows,
                "failed_sellers": failed,
                "silver_rows": silver_rows,
                "silver_ok": "rows" in silver,
                "silver_detail_rows": silver_detail_count,
                "changes_count": changes_count,
            },
            f,
        )

    # Only persist the new hash once the email has actually been composed,
    # mirroring the meme bot's "mark as sent only after it's queued" logic.
    save_last_hash(price_hash)
    print(
        f"Generated email ({summary_rows} summary rows, {detail_rows} detail rows, "
        f"{silver_rows} silver rows, {silver_detail_count} silver detail rows, "
        f"{changes_count} change rows). Saved to ./{EMAIL_DIR}/"
    )


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
