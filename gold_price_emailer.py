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

The email has two sections:
  1. Summary - the homepage's comparison table (one row per seller, for
     gold bars and for gold rings), covering SJC, DOJI, PNJ, Bao Tin Minh
     Chau, Bao Tin Manh Hai, Phu Quy, Mi Hong, and Ngoc Tham.
  2. Full detail per seller - each seller also has its own page on
     giavang.org (e.g. giavang.org/trong-nuoc/sjc/) with a full product
     breakdown (gold bars in different weights, rings, various jewelry
     purities, etc). This script fetches all 8 of those pages too and
     includes each seller's full table as its own section, the same shape
     baotinmanhhai.vn's own page used to provide for just that one seller.

That's 1 (summary) + 8 (per-seller detail) = 9 requests to giavang.org per
run. If a single seller's detail page fails to fetch/parse, that one
section notes the failure and the rest of the email still sends normally.

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
       export SOURCE_URL="https://giavang.org/"    # optional, summary page
       export STATE_FILE="state/last_price.json"   # optional, dedup state file
       export ALLOW_INSECURE_SSL_FALLBACK="false"  # optional, last-resort TLS bypass

SCHEDULING
----------
See README.md / GitHub Actions workflow in this repo for running this on a
schedule in the cloud without needing your own computer on.

NOTE ON SCRAPING
-----------------
Always worth checking the current robots.txt / terms of whatever site this
is pointed at before running it unattended long-term, e.g.:
    https://giavang.org/robots.txt
The page markup can also change at any time — if `generate` reports 0
parsed rows for a section, open the relevant page, inspect the price
table, and update the parsing functions below.
"""

import base64
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
from email.mime.image import MIMEImage
from email.mime.application import MIMEApplication
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
WORLD_GOLD_URL = os.environ.get("WORLD_GOLD_URL", "https://giavang.org/the-gioi/")

# Threshold for the "big move" alerts section: an item is flagged if any
# available change (silver's same-day source figure, or any history-based
# period) has an absolute percent move at or above this.
ALERT_THRESHOLD_PCT = float(os.environ.get("ALERT_THRESHOLD_PCT", "3.0"))

VCB_RATE_URL = os.environ.get("VCB_RATE_URL", "https://tygiausd.org/nganhang/vietcombank")

# Optional: restrict big-move alert scanning to specific item labels, e.g.
# "SJC,Phu Quy - BAC MIENG PHU QUY 999 1 LUONG" (comma-separated, must match
# the label text used elsewhere in the email exactly). Empty (default)
# means all tracked items are scanned, same as before this was added.
WATCHLIST = [s.strip() for s in os.environ.get("WATCHLIST", "").split(",") if s.strip()]

# Optional: per-item alert threshold overriding ALERT_THRESHOLD_PCT, as a
# JSON object string, e.g. '{"SJC": 2.0, "DOJI": 5.0}'. Items not listed
# use ALERT_THRESHOLD_PCT.
try:
    ALERT_THRESHOLDS_OVERRIDE = json.loads(os.environ.get("ALERT_THRESHOLDS_JSON", "{}"))
except json.JSONDecodeError:
    print("  ALERT_THRESHOLDS_JSON is not valid JSON - ignoring, using ALERT_THRESHOLD_PCT for everything.", file=sys.stderr)
    ALERT_THRESHOLDS_OVERRIDE = {}

# Optional: your holdings, as a JSON list string, e.g.
#   '[{"label": "SJC", "kind": "gold", "amount": 2, "buy_price": 140000000},
#     {"label": "Phú Quý - BẠC MIẾNG PHÚ QUÝ 999 1 LƯỢNG", "kind": "silver", "amount": 10, "buy_price": 2100000}]'
# "label" must match the item's label exactly as it appears elsewhere in
# the email (seller name for gold-bars row 0, or "brand - product" for
# silver). "amount" is in the same unit the source quotes that item in
# (typically lượng for gold, lượng/kg for silver depending on product).
# "buy_price" is your cost basis per unit, same currency/unit as the
# source's price. Leave HOLDINGS_JSON unset/empty to skip this section.
try:
    HOLDINGS = json.loads(os.environ.get("HOLDINGS_JSON", "[]"))
except json.JSONDecodeError:
    print("  HOLDINGS_JSON is not valid JSON - ignoring, portfolio section will be empty.", file=sys.stderr)
    HOLDINGS = []

# Periods (label, days) for the 30/90-day high/low extremes section -
# reuses the same price_history.json the changes section depends on.
EXTREME_PERIODS = [("30 ngày", 30), ("90 ngày", 90)]

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

# Real seller logos, hotlinked directly from giavang.org (same slugs as
# SELLERS above) rather than downloaded and re-embedded - referencing the
# original keeps this from redistributing someone else's brand assets,
# and giavang.org already displays these same logos publicly to identify
# each seller. Tradeoff: unlike the embedded hand-drawn product icons
# below, hotlinked images may not auto-display in every email client
# (some show a "load images?" prompt for remote images) - if that's not
# acceptable, set GOLD_SELLER_LOGOS = {} to fall back to the hand-drawn
# gold-bar/gold-ring icon for every row instead.
GOLD_SELLER_LOGOS = {
    name: f"https://giavang.org/assets/images/gia-vang/logo-{slug}.png" for name, slug in SELLERS
}
# No equivalent real per-brand image exists for silver: giahanghoa.net's
# price table (SILVER_URL) has no logos or product photos at all, so
# silver rows always use the hand-drawn icons.

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

    If a request times out or the connection drops, one retry is made
    with a longer timeout before giving up - with 12 pages fetched every
    run, an occasional slow response from one of them is expected, and a
    single timeout shouldn't take down that whole section of the email.
    """
    for attempt, timeout in enumerate((15, 30), start=1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout, verify=certifi.where())
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
            resp = requests.get(url, headers=HEADERS, timeout=timeout, verify=False)
            resp.raise_for_status()
            return resp.text
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt == 1:
                print(f"  {url} - {e} - retrying once with a longer timeout ({timeout + 15}s)...", file=sys.stderr)
                continue
            raise


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


def _clean_change_text(s):
    """
    The source page renders an up/down arrow using an icon font (e.g.
    Material Symbols), whose underlying text content is the literal word
    "trending_up"/"trending_down"/"trending_flat" - invisible in a browser
    (it renders as an arrow glyph) but present in the raw HTML text we
    scrape. Strip that out so the value shown is just the number, e.g.
    "-38.000 trending_down" -> "-38.000".
    """
    if not s:
        return None
    cleaned = re.sub(r"trending_(up|down|flat)", "", s, flags=re.IGNORECASE)
    return cleaned.strip() or None


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
            change_24h_raw = cells[change_idx] if change_idx is not None and change_idx < len(cells) else None
            change_24h = _clean_change_text(change_24h_raw)
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


def parse_world_gold(html):
    """
    Parse giavang.org's world-gold-price page. Unlike the tabular pages,
    this one is prose text, so it's regex-matched rather than table-parsed.
    Returns a dict with xau_usd, change_usd, change_pct, vnd_per_ounce,
    vnd_per_luong - or None if the page's wording changed and nothing
    matched (caller treats that as a fetch failure for this section).

    The USD/VND rate isn't fetched from a separate source - it's derived
    from vnd_per_ounce / xau_usd, since the page already publishes both
    halves of that conversion (labeled "theo tỷ giá Vietcombank").
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    m = re.search(
        r"([\d,]+\.\d+)\s*USD\s*([+-]?[\d,]+\.\d+)\s*USD\s*\(\s*([+-]?[\d,]+\.\d+)\s*%\s*\)", text
    )
    if not m:
        return None
    result = {
        "xau_usd": float(m.group(1).replace(",", "")),
        "change_usd": float(m.group(2).replace(",", "")),
        "change_pct": float(m.group(3).replace(",", "")),
    }

    m2 = re.search(r"1\s*Ounce\s*=\s*([\d.,]+)\s*VNĐ", text)
    if m2:
        result["vnd_per_ounce"] = _parse_vnd_number(m2.group(1))

    m3 = re.search(r"quy\s*đổi\s*sang\s*tiền\s*Việt\s*Nam\s*Đồng\s*có\s*giá\s*là\s*([\d.,]+)\s*VNĐ", text)
    if m3:
        result["vnd_per_luong"] = _parse_vnd_number(m3.group(1))

    if result.get("vnd_per_ounce") and result["xau_usd"]:
        result["implied_usdvnd_rate"] = result["vnd_per_ounce"] / result["xau_usd"]

    return result


def _leading_grouped_number(s):
    """
    Extract a properly comma-grouped number from the start of a string,
    e.g. '26,080' from '26,08030' - the source site appends a same-day
    change indicator directly after the rate with no separating space or
    tag boundary (confirmed on the live page: cells read like
    '26,08030' where '26,080' is the rate and '30' is the change badge),
    so a blind "strip all non-digits" would merge them into a wrong
    number. Requiring at least one comma-group means the un-grouped
    trailing digits never get included. Returns None if nothing matches.
    """
    m = re.match(r"\s*(\d{1,3}(?:,\d{3})+)", s or "")
    return m.group(1) if m else None


def parse_vcb_rate(html):
    """
    Parse tygiausd.org's Vietcombank rate table for the USD row. Returns
    {"buy": int, "transfer": int, "sell": int} or None if the USD row
    wasn't found (page structure changed).

    Scans every row of each table for one containing "Mã NT" rather than
    assuming it's literally the first <tr> - the site now prepends a
    title/caption row ("Tỷ Giá Vietcombank") above the real header row,
    so table.find("tr") alone would grab the wrong row and miss the
    table entirely.
    """
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        trs = table.find_all("tr")
        header_idx = next(
            (i for i, tr in enumerate(trs) if "Mã NT" in [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]),
            None,
        )
        if header_idx is None:
            continue
        for tr in trs[header_idx + 1:]:
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) >= 5 and cells[0] == "USD":
                buy = _parse_vnd_number(_leading_grouped_number(cells[2]))
                transfer = _parse_vnd_number(_leading_grouped_number(cells[3]))
                sell = _parse_vnd_number(_leading_grouped_number(cells[4]))
                if sell is not None:
                    return {"buy": buy, "transfer": transfer, "sell": sell}
    return None


def fetch_vcb_rate():
    """Fetch + parse the real Vietcombank USD/VND rate. Returns
    {"data": {...}} on success or {"error": "...", "url": VCB_RATE_URL}
    on failure - never aborts the rest of the email.
    """
    try:
        html = fetch_page(VCB_RATE_URL)
        data = parse_vcb_rate(html)
        if not data:
            return {"error": "Could not find the USD row on this page.", "url": VCB_RATE_URL}
        return {"data": data, "url": VCB_RATE_URL}
    except requests.RequestException as e:
        print(f"  Failed to fetch Vietcombank rate: {e}", file=sys.stderr)
        return {"error": str(e), "url": VCB_RATE_URL}


def fetch_world_gold():
    """Fetch + parse the world gold price page. Returns {"data": {...}}
    on success or {"error": "...", "url": WORLD_GOLD_URL} on failure - a
    failure here never aborts the rest of the email.
    """
    try:
        html = fetch_page(WORLD_GOLD_URL)
        data = parse_world_gold(html)
        if not data:
            return {"error": "Could not parse world gold price from this page.", "url": WORLD_GOLD_URL}
        return {"data": data, "url": WORLD_GOLD_URL}
    except requests.RequestException as e:
        print(f"  Failed to fetch world gold price: {e}", file=sys.stderr)
        return {"error": str(e), "url": WORLD_GOLD_URL}


def compute_domestic_world_gap(summary_tables, world_gold):
    """
    For the gold-bars summary table (table index 0), compare each
    seller's domestic sell price against the world price converted to
    VND/lượng - the "chênh lệch giá vàng trong nước và thế giới" figure
    that's widely watched in Vietnamese gold reporting. Returns a list of
    {label, domestic_sell, world_vnd_per_luong, gap} or [] if there's no
    gold-bars table or world price data to compare against.
    """
    if "data" not in world_gold or not world_gold["data"].get("vnd_per_luong"):
        return []
    if not summary_tables:
        return []
    world_vnd = world_gold["data"]["vnd_per_luong"]
    rows = []
    for r in summary_tables[0]:  # table 0 = gold bars, per SUMMARY_TABLE_LABELS
        domestic_sell = _parse_vnd_number(r["sell"])
        if domestic_sell is None:
            continue
        rows.append({
            "label": r["label"],
            "domestic_sell": domestic_sell,
            "world_vnd_per_luong": world_vnd,
            "gap": domestic_sell - world_vnd,
        })
    return rows


def compute_spreads(summary_tables, silver_rows):
    """
    Buy/sell spread (Bán ra - Mua vào) per row, for the gold-summary
    tables and the silver-summary rows - purely derived from data already
    fetched elsewhere, no extra requests. Returns
    {"gold": [[{label, buy, sell, spread, spread_pct}]], "silver": [...]}.
    """
    gold_spreads = []
    for rows in summary_tables:
        table_spreads = []
        for r in rows:
            buy, sell = _parse_vnd_number(r["buy"]), _parse_vnd_number(r["sell"])
            if buy is None or sell is None:
                continue
            spread = sell - buy
            table_spreads.append({
                "label": r["label"], "buy": buy, "sell": sell,
                "spread": spread, "spread_pct": (spread / buy * 100) if buy else None,
            })
        gold_spreads.append(table_spreads)

    silver_spreads = []
    for r in silver_rows:
        buy, sell = _parse_vnd_number(r["buy"]), _parse_vnd_number(r["sell"])
        if buy is None or sell is None:
            continue
        spread = sell - buy
        silver_spreads.append({
            "label": f"{r['brand']} - {r['product']}", "buy": buy, "sell": sell,
            "spread": spread, "spread_pct": (spread / buy * 100) if buy else None,
        })

    return {"gold": gold_spreads, "silver": silver_spreads}


def compute_big_moves(price_changes, threshold_pct=ALERT_THRESHOLD_PCT, watchlist=None, thresholds_override=None):
    """
    Scan price_changes (gold + silver) for any item whose absolute
    percent move - on any available period - meets or exceeds its
    threshold. Purely derived from data already computed in
    compute_price_changes, no extra requests. Returns a list of
    {label, period, diff, pct} - one entry per (item, period) combination
    that crossed the threshold, largest |pct| first.

    watchlist (optional): if non-empty, only labels in this list are
    scanned - everything else is skipped regardless of how much it moved.
    thresholds_override (optional): {label: pct} - use this item's own
    threshold instead of threshold_pct when present.
    """
    watchlist = set(watchlist) if watchlist else None
    thresholds_override = thresholds_override or {}

    def _threshold_for(label):
        return thresholds_override.get(label, threshold_pct)

    def _scan(label, changes):
        if watchlist and label not in watchlist:
            return []
        item_threshold = _threshold_for(label)
        out = []
        for period_label, change in changes.items():
            if change and change["pct"] is not None and abs(change["pct"]) >= item_threshold:
                out.append({"label": label, "period": period_label, "diff": change["diff"], "pct": change["pct"]})
        return out

    flagged = []
    for rows in price_changes["gold"]:
        for r in rows:
            flagged.extend(_scan(r["label"], r["changes"]))
    for r in price_changes["silver"]:
        flagged.extend(_scan(r["label"], r["changes"]))

    flagged.sort(key=lambda x: abs(x["pct"]), reverse=True)
    return flagged


def generate_price_chart(history, today_str, path):
    """
    Render a simple line chart of SJC gold-bars sell price and, if
    present, the first silver item's sell price, over whatever history is
    available (up to HISTORY_MAX_DAYS). Writes a PNG to `path` and
    returns True, or returns False (writing nothing) if there are fewer
    than 2 days of history to plot - a single point isn't a chart.

    Matplotlib runs with the non-interactive "Agg" backend since this is
    a headless CI environment with no display.
    """
    dates = sorted(d for d in history if d <= today_str)
    if len(dates) < 2:
        return False

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    gold_series = []
    silver_label, silver_series = None, []
    for d in dates:
        snap = history[d]
        sjc_sell = snap.get("gold", {}).get("table_0", {}).get("SJC")
        if sjc_sell is not None:
            gold_series.append((d, sjc_sell))
        if silver_label is None and snap.get("silver"):
            silver_label = next(iter(snap["silver"]))
        if silver_label is not None:
            sv = snap.get("silver", {}).get(silver_label)
            if sv is not None:
                silver_series.append((d, sv))

    if not gold_series:
        return False

    fig, ax1 = plt.subplots(figsize=(7, 3.2), dpi=120)
    xs = [datetime.strptime(d, "%Y-%m-%d") for d, _ in gold_series]
    ys = [v / 1_000_000 for _, v in gold_series]  # triệu đồng/lượng
    ax1.plot(xs, ys, color="#b8860b", marker="o", markersize=3, label="SJC (vàng, triệu đ/lượng)")
    ax1.set_ylabel("Vàng SJC (triệu đ/lượng)", color="#b8860b")
    ax1.tick_params(axis="y", labelcolor="#b8860b")
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))

    if silver_series and silver_label:
        ax2 = ax1.twinx()
        sx = [datetime.strptime(d, "%Y-%m-%d") for d, _ in silver_series]
        sy = [v / 1000 for _, v in silver_series]  # nghìn đồng
        ax2.plot(sx, sy, color="#888", marker="o", markersize=3, linestyle="--", label=f"{silver_label} (bạc, nghìn đ)")
        ax2.set_ylabel(f"{silver_label} (nghìn đ)", color="#888")
        ax2.tick_params(axis="y", labelcolor="#888")

    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, format="png")
    plt.close(fig)
    return True


def compute_extremes(history, today_str, today_snapshot):
    """
    For each gold-summary and silver-summary item, check whether today's
    sell price is at or beyond the min/max seen in price_history.json
    over the last N days (EXTREME_PERIODS), i.e. "today is a 30-day low"
    style flags. Only flags items where at least 2 days of history exist
    in that window (a single data point isn't a meaningful high/low).
    Returns a list of {label, period, kind: "low"|"high", value}.
    """

    def _window_values(key_path, days_ago):
        cutoff = (datetime.strptime(today_str, "%Y-%m-%d") - _timedelta(days=days_ago)).strftime("%Y-%m-%d")
        values = []
        for date_str, snap in history.items():
            if cutoff <= date_str <= today_str:
                v = key_path(snap)
                if v is not None:
                    values.append(v)
        return values

    flags = []

    for table_key, items in today_snapshot["gold"].items():
        for label, current in items.items():
            for period_label, days in EXTREME_PERIODS:
                values = _window_values(lambda snap, tk=table_key, lb=label: snap.get("gold", {}).get(tk, {}).get(lb), days)
                values.append(current)
                if len(values) < 3:
                    continue
                if current <= min(values):
                    flags.append({"label": label, "period": period_label, "kind": "low", "value": current})
                elif current >= max(values):
                    flags.append({"label": label, "period": period_label, "kind": "high", "value": current})

    for key, current in today_snapshot["silver"].items():
        for period_label, days in EXTREME_PERIODS:
            values = _window_values(lambda snap, k=key: snap.get("silver", {}).get(k), days)
            values.append(current)
            if len(values) < 3:
                continue
            if current <= min(values):
                flags.append({"label": key, "period": period_label, "kind": "low", "value": current})
            elif current >= max(values):
                flags.append({"label": key, "period": period_label, "kind": "high", "value": current})

    return flags


def compute_holdings(holdings_config, summary_tables, silver_rows):
    """
    Compute current value/gain-loss for each configured holding (see
    HOLDINGS_JSON above). Matches each holding's "label" against the
    gold-bars summary table (table 0) by seller name, or against silver
    summary rows by "brand - product". Holdings whose label doesn't match
    anything currently fetched are skipped (with a note) rather than
    causing an error, since a temporarily-failed fetch shouldn't break
    the whole portfolio section.

    Returns {"items": [...], "total_value": int, "total_cost": int,
    "total_gain": int} or {"items": []} if HOLDINGS_JSON is empty/unset.
    """
    if not holdings_config:
        return {"items": [], "total_value": 0, "total_cost": 0, "total_gain": 0}

    gold_prices = {}
    if summary_tables:
        for r in summary_tables[0]:
            price = _parse_vnd_number(r["sell"])
            if price is not None:
                gold_prices[r["label"]] = price

    silver_prices = {}
    for r in silver_rows:
        price = _parse_vnd_number(r["sell"])
        if price is not None:
            silver_prices[f"{r['brand']} - {r['product']}"] = price

    items = []
    total_value = total_cost = 0
    for h in holdings_config:
        label, kind, amount, buy_price = h.get("label"), h.get("kind"), h.get("amount"), h.get("buy_price")
        if not label or amount is None or buy_price is None:
            continue
        current_price = (gold_prices if kind == "gold" else silver_prices).get(label)
        if current_price is None:
            items.append({"label": label, "matched": False})
            continue
        value = current_price * amount
        cost = buy_price * amount
        gain = value - cost
        gain_pct = (gain / cost * 100) if cost else None
        items.append({
            "label": label, "matched": True, "amount": amount, "current_price": current_price,
            "buy_price": buy_price, "value": value, "cost": cost, "gain": gain, "gain_pct": gain_pct,
        })
        total_value += value
        total_cost += cost

    return {
        "items": items, "total_value": total_value, "total_cost": total_cost,
        "total_gain": total_value - total_cost,
    }


def compute_source_health(details, silver, silver_details, world_gold, vcb_rate):
    """
    One-line-per-source summary of what fetched successfully this run,
    so a partial failure is visible in the email itself rather than only
    in the Actions log. Returns {"ok_count": int, "total": int,
    "failures": [label, ...]}.
    """
    checks = []
    for name, info in details.items():
        checks.append((f"Vàng - {name}", "error" not in info))
    checks.append(("Bạc - tổng hợp", "rows" in silver))
    for brand, info in silver_details.items():
        if brand in SILVER_DETAIL_PAGES:  # only check brands with an actual dedicated page to fetch
            checks.append((f"Bạc - {brand} (chi tiết)", info.get("source") is not None))
    checks.append(("Giá vàng thế giới", "data" in world_gold))
    checks.append(("Tỷ giá Vietcombank", "data" in vcb_rate))

    ok_count = sum(1 for _, ok in checks if ok)
    failures = [name for name, ok in checks if not ok]
    return {"ok_count": ok_count, "total": len(checks), "failures": failures}


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


FONT_STACK = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

# Design tokens. Cards use div+border-radius+box-shadow, which degrade
# gracefully to plain square boxes in older Outlook desktop (which ignores
# those properties) but render as intended in Gmail, Apple Mail, and most
# modern clients - a reasonable tradeoff for a personal digest email.
COLOR_TEXT = "#1f2937"
COLOR_MUTED = "#6b7280"
COLOR_BORDER = "#e6e8eb"
COLOR_GOLD = "#a86a08"
COLOR_GOLD_TINT = "#fdf3e2"
COLOR_SILVER = "#52606d"
COLOR_SILVER_TINT = "#f1f2f4"
COLOR_BLUE = "#2563a8"
COLOR_BLUE_TINT = "#eaf1fb"
COLOR_GREEN_ACCENT = "#1a8a4a"
COLOR_GREEN_TINT = "#e9f8ef"
COLOR_RED_ACCENT = "#c23434"
COLOR_RED_TINT = "#fdecec"
COLOR_UP = "#1a7a1a"
COLOR_DOWN = "#b3261e"
COLOR_FLAT = "#6b7280"

# Small original flat-icon illustrations (gold bar, gold ring, silver bar) -
# hand-drawn SVG, rasterized to PNG, base64-encoded here so the email has zero
# external image dependency (no hotlinking, nothing that can 404 later). Sent
# as inline CID attachments (see cmd_send) and referenced via cid: in the HTML,
# so each is only stored once per email regardless of how many sections use it.
ICON_GOLD_BAR_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAUAAAADwCAYAAABxLb1rAAAABmJLR0QA/wD/AP+gvaeTAAAXEElEQVR4nO3d23Pc1mEG8O9gL7xfxIsoUhfK1sWmZVm2"
    "k9iN3diSJVtJ2k4b/wmdttPnPnX80OFMXzrTyVvTl860f4GdNImd+NY4dRI7iUeVnNhxnNiyRImkeBHJ5d4BnNOH5S6BXexyJQG7AM7306xsHWAPgF3s"
    "x4ODs4cAERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERER"
    "EREREREREVG0Jbq9A0RE7Zqfh/HUaOrMu5/IZT/qS/pRCRFRB4jylZ7vwFC3AFz2pUI/KqF7Nz8Pw7yS+jsFMdLtfSEKGwUIKPyVEHhSCfWWUOKthIEf"
    "/vPL5Y/upV62AMOh8pMN6u+7vSNEYSRqfwFCiQsCWEicLv8rXvahXuoq8dK3ev6d4UfUtsvpM+Uvzc9D3mtFbAF20fw8jPKV9H8A6q+rZcdPHsb45L7K"
    "P1S1VDU+ub5cNSn3Wr9hsWr+L9XGtptuskm97dbZqrx2KH69Ns7FzWq9mzpVk6c1ec6dvDYN9d7p67r39hpPwWBe72a79pvfb0LKhvPo+36EH8AA7Kbq"
    "ZW8t/B45cwJf/4unIYRwnZiuj6PrhHUGmvOEbSyvFbjO07uo1/OD4C73Djrvbamm++qud899dZTv+Ro4yl2B5HVsTd8Hj3q9XoM7rlen99drtyv/JyXw"
    "8o8WGsIPAITYK1XbxwDsjobL3kfOnMDFP/8qAAWllI8fhLrWTCg/CAy6+m3pEnRe60gFvPzadWxmyrViIUTlc+EzBmDnNYTf6TPHcfHP/gQCO8EX2w8C"
    "g65+W/F6f712u/k6XmtICbzy4wVX+PX3CAgDyBW8q7oXDMDOagy/R47h6998EoDa/QnX9ocRIf0gMOjqt8Wga7KGY5tSAq+8fgObW6VaWX+PwPCgge28"
    "L11+DRiAneMZfhe/+QQqZ5JC9D4IDLr6bTHo9g46r/WkBL5bF34DvQZGBgU8ugF9wwDsjMbwO30/Ln7jKxBwtPyAkH4QGHT122LQ3V3QeRVICXz3jQVs"
    "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"
    "RERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERE"
    "RERERERERERERERERERERERERERERERERERERERERERERERERERERFH1/5Y6FqY2NMV/AAAAAElFTkSuQmCC"
)

ICON_GOLD_RING_B64 = (
    "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"
)

ICON_SILVER_BAR_B64 = (
    "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"
    "k0T5AAAAAElFTkSuQmCC"
)


ICON_JEWELRY_B64 = (
    "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"
)


CARD_STYLE = (
    f"background:#ffffff;border:1px solid {COLOR_BORDER};border-radius:14px;"
    "padding:22px 24px;margin-bottom:18px;box-shadow:0 1px 3px rgba(15,23,42,0.06);"
)


def _card(icon, title, accent, body_html):
    return f"""
    <div class="dm-card" style="{CARD_STYLE}">
      <p class="dm-text" style="margin:0;font-size:16.5px;font-weight:700;color:#111827;font-family:{FONT_STACK};">
        <span style="font-size:19px;margin-right:6px;">{icon}</span>{escape(title)}
      </p>
      <div style="height:3px;width:42px;background:{accent};border-radius:2px;margin:8px 0 16px;"></div>
      {body_html}
    </div>"""


def _tr_bg(i):
    return "#ffffff" if i % 2 == 0 else "#f8fafc"


def _table_open(headers, aligns, tint):
    ths = "".join(
        f"<th style='padding:10px 14px;text-align:{a};font-size:12px;font-weight:700;"
        f"color:#374151;text-transform:uppercase;letter-spacing:0.03em;'>{h}</th>"
        for h, a in zip(headers, aligns)
    )
    return (
        f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" "
        f"style=\"border-collapse:collapse;width:100%;font-family:{FONT_STACK};font-size:13.5px;\">"
        f"<thead><tr style='background:{tint};'>{ths}</tr></thead><tbody>"
    )


_TABLE_CLOSE = "</tbody></table>"
_TD = "padding:9px 14px;border-bottom:1px solid #f0f1f3;"


def _region_span(region):
    if not region:
        return ""
    return f" <span style='color:{COLOR_MUTED};font-size:11.5px'>({escape(region)})</span>"


def _classify_product_icon(text, category):
    """
    Guess which of the 4 hand-drawn icons (gold bar, gold ring, silver
    bar, jewelry) best represents a product name, from Vietnamese
    keywords. Used to put a small illustrative icon next to each row so
    it's visually clear at a glance whether that row is a bar, a ring, or
    jewelry - not meant to be a precise product classifier, just a
    helpful visual cue.
    """
    t = (text or "").lower()
    if category == "silver":
        if "nhẫn" in t or "trang sức" in t or "nữ trang" in t or "mỹ nghệ" in t:
            return "jewelry"
        return "silver_bar"
    if "nhẫn" in t:
        return "gold_ring"
    if "nữ trang" in t or "trang sức" in t:
        return "jewelry"
    return "gold_bar"


def _icon_img(kind, size=20):
    return (
        f"<img src='cid:icon_{kind}' width='{size}' height='{size}' "
        f"style='vertical-align:middle;margin-right:7px;' alt=''/>"
    )


def _row_icon_html(label, icon_kind, size=20):
    """
    Real seller logo (hotlinked, see GOLD_SELLER_LOGOS) when this row's
    label is a known gold seller and a fixed icon_kind was requested
    (i.e. this is a seller-per-row table like the summary comparison) -
    otherwise the embedded hand-drawn product-type icon.
    """
    if icon_kind and label in GOLD_SELLER_LOGOS:
        url = GOLD_SELLER_LOGOS[label]
        return (
            f"<img src='{escape(url)}' width='{size}' height='{size}' "
            f"style='vertical-align:middle;margin-right:7px;border-radius:3px;' alt='{escape(label)}'/>"
        )
    kind = icon_kind or _classify_product_icon(label, "gold")
    return _icon_img(kind, size)


def _extremes_badge(kind):
    if kind == "cheapest":
        return f"<span style='background:{COLOR_GREEN_TINT};color:{COLOR_GREEN_ACCENT};font-size:10.5px;font-weight:700;padding:2px 7px;border-radius:10px;margin-left:8px;white-space:nowrap;'>🏆 Rẻ nhất</span>"
    return f"<span style='background:{COLOR_RED_TINT};color:{COLOR_RED_ACCENT};font-size:10.5px;font-weight:700;padding:2px 7px;border-radius:10px;margin-left:8px;white-space:nowrap;'>📈 Đắt nhất</span>"


def _price_spread_note(rows, sell_key="sell"):
    """
    One-line summary above a comparison table: the gap between the
    cheapest and most expensive seller's sell price. Returns "" if fewer
    than 2 rows have a parseable price (nothing to compare).
    """
    prices = [(r["label"], _parse_vnd_number(r[sell_key])) for r in rows]
    prices = [(label, p) for label, p in prices if p is not None]
    if len(prices) < 2:
        return ""
    cheapest = min(prices, key=lambda x: x[1])
    priciest = max(prices, key=lambda x: x[1])
    if cheapest[1] == priciest[1]:
        return f"<p style='font-size:12.5px;color:{COLOR_MUTED};margin:0 0 10px;'>Tất cả các đơn vị đang niêm yết cùng một mức giá.</p>"
    spread = priciest[1] - cheapest[1]
    return (
        f"<p style='font-size:12.5px;color:{COLOR_MUTED};margin:0 0 10px;'>"
        f"Chênh lệch giữa đơn vị rẻ nhất ({escape(cheapest[0])}) và đắt nhất ({escape(priciest[0])}): "
        f"<strong style='color:{COLOR_TEXT}'>{_format_vnd(spread)} đ</strong></p>"
    )


def _table_html(rows, label_header, tint=COLOR_GOLD_TINT, icon_kind=None, highlight_extremes=False):
    """
    icon_kind: pass a fixed icon name ("gold_bar"/"gold_ring") when every
    row in this table represents one seller (e.g. the gold summary
    comparison table) - rows then try a real hotlinked seller logo first
    (see GOLD_SELLER_LOGOS), falling back to the fixed hand-drawn icon
    for any seller without one. Leave icon_kind None to classify each row
    individually from its label text instead (e.g. the per-seller detail
    table, where rows are different gold product types like bars vs
    rings vs jewelry within the same table, not different sellers).

    highlight_extremes: when True, badges the row(s) with the lowest and
    highest sell price ("Rẻ nhất"/"Đắt nhất") and prepends a one-line
    spread summary - meant for seller-comparison tables, not per-product
    detail tables where "cheapest" isn't a meaningful comparison.
    """
    cheapest_label = priciest_label = None
    if highlight_extremes:
        prices = [(r["label"], _parse_vnd_number(r["sell"])) for r in rows]
        prices = [(label, p) for label, p in prices if p is not None]
        if len(prices) >= 2 and min(p for _, p in prices) != max(p for _, p in prices):
            cheapest_label = min(prices, key=lambda x: x[1])[0]
            priciest_label = max(prices, key=lambda x: x[1])[0]

    def _row(i, r):
        badge = ""
        if r["label"] == cheapest_label:
            badge = _extremes_badge("cheapest")
        elif r["label"] == priciest_label:
            badge = _extremes_badge("priciest")
        return (
            f"<tr style='background:{_tr_bg(i)}'>"
            f"<td style='{_TD}'>{_row_icon_html(r['label'], icon_kind)}<strong>{escape(r['label'])}</strong>{_region_span(r['region'])}{badge}</td>"
            f"<td style='{_TD}text-align:right;'>{escape(r['buy'])}</td>"
            f"<td style='{_TD}text-align:right;'>{escape(r['sell'])}</td>"
            f"</tr>"
        )

    spread_note = _price_spread_note(rows) if highlight_extremes else ""
    body = "".join(_row(i, r) for i, r in enumerate(rows))
    return spread_note + _table_open([label_header, "Mua vào", "Bán ra"], ["left", "right", "right"], tint) + body + _TABLE_CLOSE


def _silver_table_html(rows):
    def _row(i, r):
        kind = _classify_product_icon(r["product"], "silver")
        return (
            f"<tr style='background:{_tr_bg(i)}'>"
            f"<td style='{_TD}'><strong>{escape(r['brand'])}</strong></td>"
            f"<td style='{_TD}'>{_icon_img(kind)}{escape(r['product'])}</td>"
            f"<td style='{_TD}text-align:right;'>{escape(r['buy'])}</td>"
            f"<td style='{_TD}text-align:right;'>{escape(r['sell'])}</td>"
            f"</tr>"
        )

    body = "".join(_row(i, r) for i, r in enumerate(rows))
    return (
        _table_open(["Thương hiệu", "Sản phẩm", "Mua vào", "Bán ra"], ["left", "left", "right", "right"], COLOR_SILVER_TINT)
        + body + _TABLE_CLOSE
    )


def _silver_detail_table_html(products):
    def _row(i, p):
        kind = _classify_product_icon(p["product"], "silver")
        return (
            f"<tr style='background:{_tr_bg(i)}'>"
            f"<td style='{_TD}'>{_icon_img(kind)}{escape(p['product'])}</td>"
            f"<td style='{_TD}text-align:right;'>{escape(p['buy'])}</td>"
            f"<td style='{_TD}text-align:right;'>{escape(p['sell'])}</td>"
            f"</tr>"
        )

    body = "".join(_row(i, p) for i, p in enumerate(products))
    return _table_open(["Sản phẩm", "Mua vào", "Bán ra"], ["left", "right", "right"], COLOR_SILVER_TINT) + body + _TABLE_CLOSE


def _format_vnd(n):
    """148400000 -> '148.400.000' (Vietnamese thousands separator)."""
    return f"{n:,.0f}".replace(",", ".")


def _format_diff(change):
    if not change:
        return f"<span style='color:{COLOR_MUTED};font-size:12px;'>Chưa đủ dữ liệu</span>"
    diff, pct = change["diff"], change["pct"]
    sign = "+" if diff >= 0 else ""
    pct_str = f" ({sign}{pct:.2f}%)" if pct is not None else ""
    color = COLOR_UP if diff > 0 else (COLOR_DOWN if diff < 0 else COLOR_FLAT)
    arrow = "▲" if diff > 0 else ("▼" if diff < 0 else "•")
    return f"<span style='color:{color};font-weight:600;'>{arrow} {sign}{_format_vnd(diff)}{pct_str}</span>"


def _changes_table_html(rows, show_source_today=False):
    headers = ["Sản phẩm", "Bán ra hiện tại"]
    aligns = ["left", "right"]
    if show_source_today:
        headers.append("Hôm nay (nguồn)")
        aligns.append("right")
    for label, _ in HISTORY_PERIODS:
        headers.append(label)
        aligns.append("right")

    def _row(i, r):
        cells = [
            f"<td style='{_TD}'>{escape(r['label'])}</td>",
            f"<td style='{_TD}text-align:right;font-weight:600;'>{_format_vnd(r['current_sell'])}</td>",
        ]
        if show_source_today:
            cells.append(f"<td style='{_TD}text-align:right;font-size:12px;'>{escape(r.get('source_today') or 'Không có')}</td>")
        for label, _ in HISTORY_PERIODS:
            cells.append(f"<td style='{_TD}text-align:right;font-size:12px;'>{_format_diff(r['changes'][label])}</td>")
        return f"<tr style='background:{_tr_bg(i)}'>" + "".join(cells) + "</tr>"

    body = "".join(_row(i, r) for i, r in enumerate(rows))
    return _table_open(headers, aligns, COLOR_BLUE_TINT) + body + _TABLE_CLOSE


def _stat_chip(label, value, sub="", accent=COLOR_GOLD):
    return f"""
        <td style="padding:6px;" width="33%">
          <div style="background:#ffffff;border:1px solid {COLOR_BORDER};border-radius:12px;padding:14px 16px;">
            <p style="margin:0;font-size:11px;font-weight:700;color:{COLOR_MUTED};text-transform:uppercase;letter-spacing:0.04em;">{escape(label)}</p>
            <p style="margin:4px 0 0;font-size:19px;font-weight:700;color:{accent};">{value}</p>
            {f"<p style='margin:2px 0 0;font-size:12px;color:{COLOR_MUTED};'>{sub}</p>" if sub else ""}
          </div>
        </td>"""


def _world_gold_html(world_gold, gap_rows):
    if "error" in world_gold:
        return (
            f"<p style='color:{COLOR_DOWN};font-size:13px;'>Không lấy được giá vàng thế giới lần này "
            f"({escape(world_gold['error'])}). Xem trực tiếp tại "
            f"<a href='{escape(world_gold['url'])}'>{escape(world_gold['url'])}</a>.</p>"
        )
    d = world_gold["data"]
    change_color = COLOR_UP if d["change_usd"] > 0 else (COLOR_DOWN if d["change_usd"] < 0 else COLOR_FLAT)
    change_sign = "+" if d["change_usd"] >= 0 else ""
    parts = [f"""
        <p style="font-size:22px;margin:0 0 4px;">
          <strong>{d['xau_usd']:,.2f} USD/oz</strong>
          <span style="color:{change_color};font-size:14px;font-weight:600;margin-left:8px;">{change_sign}{d['change_usd']:,.2f} USD ({change_sign}{d['change_pct']:.2f}%) / 24h</span>
        </p>"""]
    if d.get("vnd_per_luong"):
        parts.append(
            f"<p style='font-size:13px;color:{COLOR_MUTED};margin:0 0 12px;'>Quy đổi tham khảo: "
            f"<strong style='color:{COLOR_TEXT}'>{_format_vnd(d['vnd_per_luong'])} đ/lượng</strong>"
            + (f" &middot; tỷ giá quy đổi ~{d['implied_usdvnd_rate']:,.0f} VNĐ/USD" if d.get("implied_usdvnd_rate") else "")
            + "</p>"
        )
    if gap_rows:
        gap_body = "".join(
            f"<tr style='background:{_tr_bg(i)}'>"
            f"<td style='{_TD}'>{escape(r['label'])}</td>"
            f"<td style='{_TD}text-align:right;font-weight:600;color:{COLOR_GOLD};'>{_format_vnd(r['gap'])} đ</td>"
            f"</tr>"
            for i, r in enumerate(gap_rows)
        )
        parts.append(
            f"<p style='font-size:12.5px;color:{COLOR_MUTED};margin:0 0 8px;font-weight:600;text-transform:uppercase;letter-spacing:0.03em;'>"
            "Chênh lệch vàng miếng trong nước so với thế giới (quy đổi)</p>"
        )
        parts.append(_table_open(["Đơn vị", "Chênh lệch"], ["left", "right"], COLOR_GREEN_TINT) + gap_body + _TABLE_CLOSE)
    return "\n".join(parts)


def _spread_table_html(rows):
    body = "".join(
        f"<tr style='background:{_tr_bg(i)}'>"
        f"<td style='{_TD}'>{escape(r['label'])}</td>"
        f"<td style='{_TD}text-align:right;font-weight:600;'>{_format_vnd(r['spread'])}"
        + (f" <span style='color:{COLOR_MUTED};font-weight:400;'>({r['spread_pct']:.2f}%)</span>" if r["spread_pct"] is not None else "")
        + "</td></tr>"
        for i, r in enumerate(rows)
    )
    return _table_open(["Sản phẩm", "Chênh lệch mua-bán"], ["left", "right"], COLOR_BLUE_TINT) + body + _TABLE_CLOSE


def _big_moves_html(moves):
    if not moves:
        return (
            f"<p style='color:{COLOR_MUTED};font-size:13px;margin:0;'>✅ Không có biến động nào vượt ngưỡng "
            f"{ALERT_THRESHOLD_PCT:.1f}% tính đến hiện tại.</p>"
        )

    def _move_row(i, m):
        color = COLOR_UP if m["diff"] > 0 else COLOR_DOWN
        arrow = "▲" if m["diff"] > 0 else "▼"
        sign = "+" if m["diff"] >= 0 else ""
        pct_sign = "+" if m["pct"] >= 0 else ""
        return (
            f"<tr style='background:{_tr_bg(i)}'>"
            f"<td style='{_TD}'>{escape(m['label'])}</td>"
            f"<td style='{_TD}color:{COLOR_MUTED};'>{escape(m['period'])}</td>"
            f"<td style='{_TD}text-align:right;color:{color};font-weight:700;'>"
            f"{arrow} {sign}{_format_vnd(m['diff'])} ({pct_sign}{m['pct']:.2f}%)</td>"
            "</tr>"
        )

    body = "".join(_move_row(i, m) for i, m in enumerate(moves))
    return _table_open(["Sản phẩm", "Giai đoạn", "Biến động"], ["left", "left", "right"], COLOR_RED_TINT) + body + _TABLE_CLOSE


def _vcb_rate_html(vcb_rate):
    if "error" in vcb_rate:
        return (
            f"<p style='color:{COLOR_MUTED};font-size:12px;margin:8px 0 0;'>Không lấy được tỷ giá VCB thực tế lần này "
            f"({escape(vcb_rate['error'])}). Xem tại <a href='{escape(vcb_rate['url'])}'>{escape(vcb_rate['url'])}</a>.</p>"
        )
    d = vcb_rate["data"]
    return (
        f"<p style='font-size:13px;color:{COLOR_MUTED};margin:8px 0 0;'>Tỷ giá USD/VND thực tế (Vietcombank): "
        f"<strong style='color:{COLOR_TEXT}'>mua {_format_vnd(d['buy'])} / bán {_format_vnd(d['sell'])}</strong> đồng"
        + (f" &middot; chuyển khoản {_format_vnd(d['transfer'])} đồng" if d.get("transfer") else "")
        + "</p>"
    )


def _extremes_html(extremes):
    if not extremes:
        return f"<p style='color:{COLOR_MUTED};font-size:13px;margin:0;'>Chưa có mục nào đang ở mức cao/thấp nhất trong 30/90 ngày qua (hoặc chưa đủ lịch sử để so sánh).</p>"

    def _row(i, e):
        is_high = e["kind"] == "high"
        color = COLOR_UP if is_high else COLOR_DOWN
        icon = "🔺" if is_high else "🔻"
        label_vi = "Cao nhất" if is_high else "Thấp nhất"
        return (
            f"<tr style='background:{_tr_bg(i)}'>"
            f"<td style='{_TD}'>{escape(e['label'])}</td>"
            f"<td style='{_TD}'>{escape(e['period'])}</td>"
            f"<td style='{_TD}text-align:right;color:{color};font-weight:700;'>{icon} {label_vi}: {_format_vnd(e['value'])}</td>"
            "</tr>"
        )

    body = "".join(_row(i, e) for i, e in enumerate(extremes))
    return _table_open(["Sản phẩm", "Giai đoạn", "Mức"], ["left", "left", "right"], COLOR_BLUE_TINT) + body + _TABLE_CLOSE


def _holdings_html(portfolio):
    if not portfolio["items"]:
        return (
            f"<p style='color:{COLOR_MUTED};font-size:13px;margin:0;'>Chưa cấu hình danh mục. Đặt biến môi trường "
            "<code>HOLDINGS_JSON</code> để hiển thị giá trị và lãi/lỗ danh mục của bạn ở đây - xem README.</p>"
        )

    def _row(i, h):
        if not h.get("matched"):
            return (
                f"<tr style='background:{_tr_bg(i)}'>"
                f"<td style='{_TD}'>{escape(h['label'])}</td>"
                f"<td colspan='4' style='{_TD}color:{COLOR_MUTED};font-size:12px;'>Không khớp được với dữ liệu giá hiện tại</td>"
                "</tr>"
            )
        color = COLOR_UP if h["gain"] > 0 else (COLOR_DOWN if h["gain"] < 0 else COLOR_FLAT)
        sign = "+" if h["gain"] >= 0 else ""
        pct_str = f" ({sign}{h['gain_pct']:.2f}%)" if h["gain_pct"] is not None else ""
        return (
            f"<tr style='background:{_tr_bg(i)}'>"
            f"<td style='{_TD}'>{escape(h['label'])}</td>"
            f"<td style='{_TD}text-align:right;'>{h['amount']:g}</td>"
            f"<td style='{_TD}text-align:right;'>{_format_vnd(h['value'])}</td>"
            f"<td style='{_TD}text-align:right;color:{color};font-weight:700;'>{sign}{_format_vnd(h['gain'])}{pct_str}</td>"
            "</tr>"
        )

    body = "".join(_row(i, h) for i, h in enumerate(portfolio["items"]))
    table = _table_open(["Sản phẩm", "Số lượng", "Giá trị hiện tại", "Lãi/Lỗ"], ["left", "right", "right", "right"], COLOR_GOLD_TINT) + body + _TABLE_CLOSE

    total_color = COLOR_UP if portfolio["total_gain"] > 0 else (COLOR_DOWN if portfolio["total_gain"] < 0 else COLOR_FLAT)
    total_sign = "+" if portfolio["total_gain"] >= 0 else ""
    total_pct = (portfolio["total_gain"] / portfolio["total_cost"] * 100) if portfolio["total_cost"] else None
    summary = (
        f"<p style='font-size:15px;margin:14px 0 0;'>Tổng giá trị: <strong>{_format_vnd(portfolio['total_value'])} đ</strong>"
        f" &middot; Lãi/Lỗ: <strong style='color:{total_color}'>{total_sign}{_format_vnd(portfolio['total_gain'])} đ"
        + (f" ({total_sign}{total_pct:.2f}%)" if total_pct is not None else "")
        + "</strong></p>"
    )
    return table + summary


def _source_health_banner(health):
    all_ok = health["ok_count"] == health["total"]
    color = COLOR_GREEN_ACCENT if all_ok else COLOR_RED_ACCENT
    bg = COLOR_GREEN_TINT if all_ok else COLOR_RED_TINT
    icon = "✅" if all_ok else "⚠️"
    text = f"{icon} {health['ok_count']}/{health['total']} nguồn dữ liệu OK"
    if health["failures"]:
        text += " &middot; Lỗi: " + ", ".join(escape(f) for f in health["failures"])
    return (
        f"<div style='background:{bg};border-radius:10px;padding:10px 16px;margin:0 0 18px;'>"
        f"<p style='margin:0;font-size:12.5px;color:{color};font-weight:600;'>{text}</p>"
        "</div>"
    )


def build_html(summary_tables, details, silver, silver_details, price_changes, world_gold, gap_rows,
                spreads, big_moves, has_chart, vcb_rate, extremes, portfolio, source_health, source_url, timestamp):
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
            if i > 0:
                parts.append(f"<p style='font-size:13px;font-weight:700;color:{COLOR_MUTED};margin:16px 0 8px;'>{escape(label)}</p>")
            summary_icon_kind = "gold_bar" if i == 0 else ("gold_ring" if i == 1 else None)
            parts.append(_table_html(rows, "Đơn vị bán", icon_kind=summary_icon_kind, highlight_extremes=True))
        summary_html = "\n".join(parts)

    # --- Section 2: full detail per seller ---
    detail_parts = []
    for name, info in details.items():
        logo = GOLD_SELLER_LOGOS.get(name)
        logo_html = f"<img src='{escape(logo)}' width='18' height='18' style='vertical-align:middle;margin-right:6px;border-radius:3px;' alt=''/>" if logo else ""
        detail_parts.append(f"<p style='font-size:13px;font-weight:700;color:{COLOR_GOLD};margin:16px 0 8px;'>{logo_html}{escape(name)}</p>")
        if "error" in info:
            detail_parts.append(
                f"<p style='color:{COLOR_DOWN};font-size:13px;'>Không lấy được dữ liệu chi tiết lần này "
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
            f"<p style='color:{COLOR_DOWN};font-size:13px;'>Không lấy được giá bạc lần này "
            f"({escape(silver['error'])}). Xem trực tiếp tại "
            f"<a href='{escape(silver['url'])}'>{escape(silver['url'])}</a>.</p>"
        )
    else:
        silver_html = _silver_table_html(silver["rows"])

    silver_detail_parts = []
    for brand, info in silver_details.items():
        silver_detail_parts.append(f"<p style='font-size:13px;font-weight:700;color:{COLOR_SILVER};margin:16px 0 8px;'>{escape(brand)}</p>")
        if not info["source"]:
            silver_detail_parts.append(
                f"<p style='color:{COLOR_MUTED};font-size:12px;margin:0 0 6px;'>"
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
        changes_parts.append(f"<p style='font-size:13px;font-weight:700;color:{COLOR_GOLD};margin:16px 0 8px;'>{escape(label)}</p>")
        changes_parts.append(_changes_table_html(rows))
    if price_changes["silver"]:
        changes_parts.append(f"<p style='font-size:13px;font-weight:700;color:{COLOR_SILVER};margin:16px 0 8px;'>Bạc</p>")
        changes_parts.append(_changes_table_html(price_changes["silver"], show_source_today=True))
    changes_html = "\n".join(changes_parts) if changes_parts else "<p>Không có dữ liệu để so sánh.</p>"
    changes_html += (
        f"<p style='color:{COLOR_MUTED};font-size:11.5px;margin-top:10px;'>Biến động dựa trên lịch sử tự ghi nhận "
        "từ lần đầu email này chạy - có thể chưa đủ dữ liệu cho mốc 30 ngày/1 năm ngay từ đầu, sẽ đầy đủ dần theo thời gian.</p>"
    )

    # --- Section 6: world gold price + domestic-world gap + VCB rate ---
    world_html = _world_gold_html(world_gold, gap_rows) + _vcb_rate_html(vcb_rate)

    # --- Section 7: buy/sell spread ---
    spread_parts = []
    for i, rows in enumerate(spreads["gold"]):
        if not rows:
            continue
        label = SUMMARY_TABLE_LABELS[i] if i < len(SUMMARY_TABLE_LABELS) else f"Bảng {i + 1}"
        spread_parts.append(f"<p style='font-size:13px;font-weight:700;color:{COLOR_GOLD};margin:16px 0 8px;'>{escape(label)}</p>")
        spread_parts.append(_spread_table_html(rows))
    if spreads["silver"]:
        spread_parts.append(f"<p style='font-size:13px;font-weight:700;color:{COLOR_SILVER};margin:16px 0 8px;'>Bạc</p>")
        spread_parts.append(_spread_table_html(spreads["silver"]))
    spread_html = "\n".join(spread_parts) if spread_parts else "<p>Không có dữ liệu.</p>"

    # --- Section 8: big-move alerts ---
    big_moves_html = _big_moves_html(big_moves)

    # --- Section 9: price history chart ---
    chart_html = (
        '<img src="cid:pricechart" alt="Biểu đồ giá vàng/bạc" style="max-width:100%;border-radius:10px;display:block;" />'
        if has_chart else
        f"<p style='color:{COLOR_MUTED};font-size:13px;'>Chưa đủ dữ liệu để vẽ biểu đồ (cần ít nhất 2 ngày lịch sử) - sẽ xuất hiện khi có thêm dữ liệu.</p>"
    )

    # --- Section 10: 30/90-day extremes ---
    extremes_html = _extremes_html(extremes)

    # --- Section 11: your portfolio ---
    holdings_html = _holdings_html(portfolio)

    # --- Hero stat chips (SJC now, world price, alert count) ---
    sjc_sell = next(
        (r["sell"] for r in (summary_tables[0] if summary_tables else []) if r["label"] == "SJC"),
        None,
    )
    world_price_str = (
        f"{world_gold['data']['xau_usd']:,.0f} USD/oz" if "data" in world_gold else "N/A"
    )
    moves_color = COLOR_RED_ACCENT if big_moves else COLOR_GREEN_ACCENT
    chips = (
        _stat_chip("Vàng SJC (bán ra)", sjc_sell or "N/A", "đồng/lượng", COLOR_GOLD)
        + _stat_chip("Vàng thế giới", world_price_str, "XAU/USD", COLOR_BLUE)
        + _stat_chip("Biến động lớn", str(len(big_moves)), f"≥ {ALERT_THRESHOLD_PCT:.0f}% ghi nhận", moves_color)
    )

    health_banner = _source_health_banner(source_health)

    return f"""\
<html>
  <head>
    <meta name="color-scheme" content="light dark">
    <meta name="supported-color-schemes" content="light dark">
    <style>
      @media (prefers-color-scheme: dark) {{
        .dm-bg {{ background:#0f1115 !important; }}
        .dm-card {{ background:#1a1d24 !important; border-color:#2a2e37 !important; }}
        .dm-text {{ color:#f3f4f6 !important; }}
        .dm-muted {{ color:#9aa1ab !important; }}
      }}
    </style>
  </head>
  <body style="margin:0;padding:0;background:#eef1f5;font-family:{FONT_STACK};">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="dm-bg" style="background:#eef1f5;">
      <tr>
        <td align="center" style="padding:24px 12px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:760px;">

            <!-- Header banner -->
            <tr>
              <td style="background:linear-gradient(135deg,#8a5a0f,#b8860b);border-radius:16px 16px 0 0;padding:26px 28px;">
                <p style="margin:0;font-size:22px;font-weight:800;color:#ffffff;">🪙 Giá vàng &amp; bạc hôm nay</p>
                <p style="margin:6px 0 0;font-size:13px;color:#f5e7c8;">Các đơn vị lớn tại Việt Nam &middot; Cập nhật {escape(timestamp)}</p>
              </td>
            </tr>

            <!-- Hero stat chips -->
            <tr>
              <td class="dm-card" style="background:#ffffff;border-left:1px solid {COLOR_BORDER};border-right:1px solid {COLOR_BORDER};padding:14px 16px 4px;">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>{chips}</tr></table>
              </td>
            </tr>
            <tr><td class="dm-card" style="background:#ffffff;border-left:1px solid {COLOR_BORDER};border-right:1px solid {COLOR_BORDER};border-bottom:1px solid {COLOR_BORDER};border-radius:0 0 16px 16px;padding:0 0 18px;"></td></tr>

            <tr><td style="height:20px;"></td></tr>

            <tr><td style="padding:0 6px;">{health_banner}</td></tr>

            <tr><td>{_card("🥇", "Vàng - Tổng hợp so sánh giữa các đơn vị", COLOR_GOLD, summary_html)}</td></tr>
            <tr><td>{_card("📋", "Vàng - Chi tiết đầy đủ theo từng đơn vị", COLOR_GOLD, detail_html)}</td></tr>
            <tr><td>{_card("🥈", "Bạc - So sánh giữa các đơn vị", COLOR_SILVER, silver_html)}</td></tr>
            <tr><td>{_card("📋", "Bạc - Chi tiết đầy đủ theo từng đơn vị", COLOR_SILVER, silver_detail_html)}</td></tr>
            <tr><td>{_card("📊", "Biến động giá (7 ngày / 30 ngày / 1 năm)", COLOR_BLUE, changes_html)}</td></tr>
            <tr><td>{_card("🔺", "Cực trị 30/90 ngày", COLOR_BLUE, extremes_html)}</td></tr>
            <tr><td>{_card("🌍", "Giá vàng thế giới & chênh lệch trong nước/thế giới", COLOR_GREEN_ACCENT, world_html)}</td></tr>
            <tr><td>{_card("↔️", "Chênh lệch mua-bán (spread)", COLOR_BLUE, spread_html)}</td></tr>
            <tr><td>{_card("🚨", f"Cảnh báo biến động lớn (≥ {ALERT_THRESHOLD_PCT:.1f}%)", COLOR_RED_ACCENT, big_moves_html)}</td></tr>
            <tr><td>{_card("💼", "Danh mục của bạn", COLOR_GOLD, holdings_html)}</td></tr>
            <tr><td>{_card("📈", "Biểu đồ xu hướng giá", COLOR_TEXT, chart_html)}</td></tr>

            <tr>
              <td style="padding:8px 6px 0;">
                <p class="dm-muted" style="color:{COLOR_MUTED};font-size:11.5px;line-height:1.6;margin:0;">
                  Nguồn: <a href="{escape(source_url)}" style="color:{COLOR_BLUE};">{escape(source_url)}</a> (vàng),
                  <a href="{escape(SILVER_URL)}" style="color:{COLOR_BLUE};">{escape(SILVER_URL)}</a> (bạc),
                  <a href="{escape(VCB_RATE_URL)}" style="color:{COLOR_BLUE};">{escape(VCB_RATE_URL)}</a> (tỷ giá) &middot;
                  Đơn vị: nghìn đồng/lượng trừ khi ghi chú khác trên trang gốc &middot;
                  Email tự động, chỉ mang tính tham khảo, không phải lời khuyên đầu tư.
                </p>
              </td>
            </tr>

          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""




def build_plain_text(summary_tables, details, silver, silver_details, price_changes, world_gold, gap_rows,
                      spreads, big_moves, has_chart, vcb_rate, extremes, portfolio, source_health,
                      source_url, timestamp):
    lines = [
        f"Gia vang & bac hom nay - cap nhat {timestamp}",
        f"Tinh trang nguon: {source_health['ok_count']}/{source_health['total']} OK"
        + (f" - loi: {', '.join(source_health['failures'])}" if source_health["failures"] else ""),
        "", "== VANG - TONG HOP ==",
    ]
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

    lines.append("")
    lines.append("== GIA VANG THE GIOI ==")
    if "error" in world_gold:
        lines.append(f"  Khong lay duoc ({world_gold['error']}). Xem tai {world_gold['url']}")
    else:
        d = world_gold["data"]
        lines.append(f"  XAU/USD: {d['xau_usd']:,.2f} USD/oz ({'+' if d['change_usd'] >= 0 else ''}{d['change_usd']:,.2f} USD, {'+' if d['change_pct'] >= 0 else ''}{d['change_pct']:.2f}% trong 24h)")
        if d.get("vnd_per_luong"):
            lines.append(f"  Quy doi tham khao: {d['vnd_per_luong']:,}".replace(",", ".") + " d/luong")
        if gap_rows:
            lines.append("  Chenh lech trong nuoc vs the gioi:")
            for r in gap_rows:
                lines.append(f"    {r['label']}: {'+' if r['gap'] >= 0 else ''}{r['gap']:,}".replace(",", ".") + " d")
    if "data" in vcb_rate:
        d = vcb_rate["data"]
        lines.append(f"  Ty gia VCB thuc te: mua {d['buy']:,} / ban {d['sell']:,}".replace(",", ".") + " d")

    lines.append("")
    lines.append("== CUC TRI 30/90 NGAY ==")
    if not extremes:
        lines.append("  Chua co muc cao/thap nhat dang chu y.")
    else:
        for e in extremes:
            lines.append(f"  {e['label']} ({e['period']}): {'CAO NHAT' if e['kind'] == 'high' else 'THAP NHAT'} {e['value']:,}".replace(",", "."))

    lines.append("")
    lines.append("== CHENH LECH MUA-BAN (SPREAD) ==")
    for i, rows in enumerate(spreads["gold"]):
        if not rows:
            continue
        label = SUMMARY_TABLE_LABELS[i] if i < len(SUMMARY_TABLE_LABELS) else f"Bang {i + 1}"
        lines.append(f"-- {label} --")
        for r in rows:
            pct = f" ({r['spread_pct']:.2f}%)" if r["spread_pct"] is not None else ""
            lines.append(f"  {r['label']}: {r['spread']:,}".replace(",", ".") + pct)
    if spreads["silver"]:
        lines.append("-- Bac --")
        for r in spreads["silver"]:
            pct = f" ({r['spread_pct']:.2f}%)" if r["spread_pct"] is not None else ""
            lines.append(f"  {r['label']}: {r['spread']:,}".replace(",", ".") + pct)

    lines.append("")
    lines.append(f"== CANH BAO BIEN DONG LON (>= {ALERT_THRESHOLD_PCT:.1f}%) ==")
    if not big_moves:
        lines.append(f"  Khong co bien dong nao vuot nguong {ALERT_THRESHOLD_PCT:.1f}%.")
    else:
        for m in big_moves:
            sign = "+" if m["diff"] >= 0 else ""
            lines.append(f"  {m['label']} ({m['period']}): {sign}{m['diff']:,}".replace(",", ".") + f" ({sign}{m['pct']:.2f}%)")

    lines.append("")
    lines.append("== BIEU DO XU HUONG GIA ==")
    lines.append("  Xem bieu do trong email HTML." if has_chart else "  Chua du du lieu de ve bieu do.")

    lines.append("")
    lines.append("== DANH MUC CUA BAN ==")
    if not portfolio["items"]:
        lines.append("  Chua cau hinh (dat HOLDINGS_JSON).")
    else:
        for h in portfolio["items"]:
            if not h.get("matched"):
                lines.append(f"  {h['label']}: khong khop duoc voi du lieu gia hien tai")
                continue
            sign = "+" if h["gain"] >= 0 else ""
            pct = f" ({sign}{h['gain_pct']:.2f}%)" if h["gain_pct"] is not None else ""
            lines.append(
                f"  {h['label']} x{h['amount']:g}: gia tri {h['value']:,}".replace(",", ".")
                + f" d, lai/lo {sign}{h['gain']:,}".replace(",", ".") + pct
            )
        lines.append(
            f"  TONG: gia tri {portfolio['total_value']:,}".replace(",", ".")
            + f" d, lai/lo {'+' if portfolio['total_gain'] >= 0 else ''}{portfolio['total_gain']:,}".replace(",", ".") + " d"
        )

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

    print(f"Fetching world gold price {WORLD_GOLD_URL} ...")
    world_gold = fetch_world_gold()
    print("World gold: OK." if "data" in world_gold else f"World gold: failed ({world_gold['error']}).")
    gap_rows = compute_domestic_world_gap(summary_tables, world_gold)

    print(f"Fetching Vietcombank USD rate {VCB_RATE_URL} ...")
    vcb_rate = fetch_vcb_rate()
    print("VCB rate: OK." if "data" in vcb_rate else f"VCB rate: failed ({vcb_rate['error']}).")

    spreads = compute_spreads(summary_tables, silver.get("rows", []))
    big_moves = compute_big_moves(
        price_changes, threshold_pct=ALERT_THRESHOLD_PCT,
        watchlist=WATCHLIST, thresholds_override=ALERT_THRESHOLDS_OVERRIDE,
    )
    print(f"Big moves: {len(big_moves)} item(s) flagged" + (f" (watchlist: {', '.join(WATCHLIST)})" if WATCHLIST else "") + ".")

    extremes = compute_extremes(history, today_str, today_snapshot)
    print(f"Extremes: {len(extremes)} item(s) at a 30/90-day high or low.")

    portfolio = compute_holdings(HOLDINGS, summary_tables, silver.get("rows", []))
    print(f"Portfolio: {len(portfolio['items'])} holding(s) configured.")

    source_health = compute_source_health(details, silver, silver_details, world_gold, vcb_rate)
    print(f"Source health: {source_health['ok_count']}/{source_health['total']} OK.")

    chart_path = os.path.join(EMAIL_DIR, "chart.png")
    try:
        has_chart = generate_price_chart(history, today_str, chart_path)
    except Exception as e:
        print(f"  Chart generation failed: {e}", file=sys.stderr)
        has_chart = False
    print(f"Chart: {'generated' if has_chart else 'not enough history yet'}.")

    combined = {
        "summary": summary_tables,
        "details": details,
        "silver": silver,
        "silver_details": silver_details,
        "price_changes": price_changes,
        "world_gold": world_gold,
        "spreads": spreads,
        "vcb_rate": vcb_rate,
        "portfolio": portfolio,
    }
    price_hash = hash_data(combined)
    last_hash = load_last_hash()

    if summary_tables and SEND_ONLY_ON_CHANGE and price_hash == last_hash:
        print("Prices unchanged since last run and SEND_ONLY_ON_CHANGE=true - skipping email.")
        with open(os.path.join(EMAIL_DIR, "meta.json"), "w") as f:
            json.dump({"send": False}, f)
        return

    subject = f"Gia vang & bac hom nay - {now.strftime('%d/%m/%Y %H:%M')}"
    html_body = build_html(
        summary_tables, details, silver, silver_details, price_changes,
        world_gold, gap_rows, spreads, big_moves, has_chart,
        vcb_rate, extremes, portfolio, source_health, SOURCE_URL, timestamp,
    )
    text_body = build_plain_text(
        summary_tables, details, silver, silver_details, price_changes,
        world_gold, gap_rows, spreads, big_moves, has_chart,
        vcb_rate, extremes, portfolio, source_health, SOURCE_URL, timestamp,
    )

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
                "world_gold_ok": "data" in world_gold,
                "vcb_rate_ok": "data" in vcb_rate,
                "big_moves_count": len(big_moves),
                "extremes_count": len(extremes),
                "portfolio_items": len(portfolio["items"]),
                "source_health": source_health,
                "has_chart": has_chart,
            },
            f,
        )

    # Only persist the new hash once the email has actually been composed,
    # mirroring the meme bot's "mark as sent only after it's queued" logic.
    save_last_hash(price_hash)
    print(
        f"Generated email ({summary_rows} summary rows, {detail_rows} detail rows, "
        f"{silver_rows} silver rows, {silver_detail_count} silver detail rows, "
        f"{changes_count} change rows, {len(big_moves)} big moves, {len(extremes)} extremes). "
        f"Saved to ./{EMAIL_DIR}/"
    )


RECAP_DIR = "email_recap"
RECAP_PERIOD_DAYS = {"weekly": 7, "monthly": 30}


def compute_recap_stats(history, period_days, today_str):
    """
    For each gold-summary and silver-summary item, compute start/end/high/
    low/net-change over the trailing period_days, using whatever daily
    snapshots exist in that window. Items with fewer than 2 data points
    in the window are skipped (nothing meaningful to summarize).
    Returns {"gold": [...], "silver": [...]}.
    """
    cutoff = (datetime.strptime(today_str, "%Y-%m-%d") - _timedelta(days=period_days)).strftime("%Y-%m-%d")
    dates_in_range = sorted(d for d in history if cutoff <= d <= today_str)

    gold_keys, silver_keys = set(), set()
    for d in dates_in_range:
        for tk, items in history[d].get("gold", {}).items():
            gold_keys.update((tk, lb) for lb in items)
        silver_keys.update(history[d].get("silver", {}).keys())

    def _stats_for(values):
        values = [v for v in values if v is not None]
        if len(values) < 2:
            return None
        start, end = values[0], values[-1]
        return {
            "start": start, "end": end, "high": max(values), "low": min(values),
            "change": end - start, "change_pct": (end - start) / start * 100 if start else None,
        }

    gold_stats = []
    for tk, lb in sorted(gold_keys):
        s = _stats_for([history[d].get("gold", {}).get(tk, {}).get(lb) for d in dates_in_range])
        if s:
            gold_stats.append({"label": lb, **s})

    silver_stats = []
    for k in sorted(silver_keys):
        s = _stats_for([history[d].get("silver", {}).get(k) for d in dates_in_range])
        if s:
            silver_stats.append({"label": k, **s})

    return {"gold": gold_stats, "silver": silver_stats}


def history_to_csv(history):
    import csv
    import io
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["date", "category", "table_key", "label", "sell_price_vnd"])
    for date_str in sorted(history):
        snap = history[date_str]
        for table_key, items in snap.get("gold", {}).items():
            for label, price in items.items():
                writer.writerow([date_str, "gold", table_key, label, price])
        for label, price in snap.get("silver", {}).items():
            writer.writerow([date_str, "silver", "", label, price])
    return out.getvalue()


def _recap_table_html(rows, label_header):
    def _row(i, r):
        color = COLOR_UP if r["change"] > 0 else (COLOR_DOWN if r["change"] < 0 else COLOR_FLAT)
        sign = "+" if r["change"] >= 0 else ""
        pct = f" ({sign}{r['change_pct']:.2f}%)" if r["change_pct"] is not None else ""
        return (
            f"<tr style='background:{_tr_bg(i)}'>"
            f"<td style='{_TD}'>{escape(r['label'])}</td>"
            f"<td style='{_TD}text-align:right;'>{_format_vnd(r['low'])}</td>"
            f"<td style='{_TD}text-align:right;'>{_format_vnd(r['high'])}</td>"
            f"<td style='{_TD}text-align:right;color:{color};font-weight:700;'>{sign}{_format_vnd(r['change'])}{pct}</td>"
            "</tr>"
        )

    body = "".join(_row(i, r) for i, r in enumerate(rows))
    return _table_open([label_header, "Thấp nhất", "Cao nhất", "Thay đổi"], ["left", "right", "right", "right"], COLOR_BLUE_TINT) + body + _TABLE_CLOSE


def build_recap_html(stats, period_label_vi, period_days, timestamp):
    gold_html = _recap_table_html(stats["gold"], "Vàng (SJC, PNJ, ...)") if stats["gold"] else \
        f"<p style='color:{COLOR_MUTED};font-size:13px;'>Chưa đủ dữ liệu cho giai đoạn này.</p>"
    silver_html = _recap_table_html(stats["silver"], "Bạc") if stats["silver"] else \
        f"<p style='color:{COLOR_MUTED};font-size:13px;'>Chưa đủ dữ liệu cho giai đoạn này.</p>"

    return f"""\
<html>
  <body style="margin:0;padding:0;background:#eef1f5;font-family:{FONT_STACK};">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef1f5;">
      <tr>
        <td align="center" style="padding:24px 12px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:760px;">
            <tr>
              <td style="background:linear-gradient(135deg,#2f3b52,#4a5b7a);border-radius:16px;padding:26px 28px;">
                <p style="margin:0;font-size:22px;font-weight:800;color:#ffffff;">📅 Tổng kết giá {escape(period_label_vi)}</p>
                <p style="margin:6px 0 0;font-size:13px;color:#dfe4ee;">{period_days} ngày qua &middot; Tính đến {escape(timestamp)}</p>
              </td>
            </tr>
            <tr><td style="height:20px;"></td></tr>
            <tr><td>{_card("🥇", "Vàng", COLOR_GOLD, gold_html)}</td></tr>
            <tr><td>{_card("🥈", "Bạc", COLOR_SILVER, silver_html)}</td></tr>
            <tr>
              <td style="padding:8px 6px 0;">
                <p style="color:{COLOR_MUTED};font-size:11.5px;line-height:1.6;margin:0;">
                  Đính kèm: toàn bộ lịch sử giá đã ghi nhận (CSV) &middot;
                  Email tự động, chỉ mang tính tham khảo, không phải lời khuyên đầu tư.
                </p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""


def build_recap_text(stats, period_label_vi, period_days, timestamp):
    lines = [f"Tong ket gia {period_label_vi} - {period_days} ngay qua - tinh den {timestamp}", "", "== VANG =="]
    if not stats["gold"]:
        lines.append("  Chua du du lieu.")
    else:
        for r in stats["gold"]:
            sign = "+" if r["change"] >= 0 else ""
            pct = f" ({sign}{r['change_pct']:.2f}%)" if r["change_pct"] is not None else ""
            lines.append(f"  {r['label']}: thap {r['low']:,} / cao {r['high']:,} / thay doi {sign}{r['change']:,}{pct}".replace(",", "."))
    lines.append("")
    lines.append("== BAC ==")
    if not stats["silver"]:
        lines.append("  Chua du du lieu.")
    else:
        for r in stats["silver"]:
            sign = "+" if r["change"] >= 0 else ""
            pct = f" ({sign}{r['change_pct']:.2f}%)" if r["change_pct"] is not None else ""
            lines.append(f"  {r['label']}: thap {r['low']:,} / cao {r['high']:,} / thay doi {sign}{r['change']:,}{pct}".replace(",", "."))
    lines.append("")
    lines.append("Dinh kem: toan bo lich su gia da ghi nhan (CSV).")
    return "\n".join(lines)


def cmd_recap_generate(period):
    if period not in RECAP_PERIOD_DAYS:
        print(f"Unknown recap period '{period}' - use 'weekly' or 'monthly'.", file=sys.stderr)
        sys.exit(1)
    period_days = RECAP_PERIOD_DAYS[period]
    period_label_vi = "tuần" if period == "weekly" else "tháng"

    if os.path.exists(RECAP_DIR):
        for f in os.listdir(RECAP_DIR):
            os.remove(os.path.join(RECAP_DIR, f))
    os.makedirs(RECAP_DIR, exist_ok=True)

    history = load_history()
    now, timestamp = resolve_timestamp()
    today_str = now.strftime("%Y-%m-%d")
    stats = compute_recap_stats(history, period_days, today_str)
    print(f"Recap ({period}): {len(stats['gold'])} gold item(s), {len(stats['silver'])} silver item(s), {len(history)} day(s) of history available.")

    subject = f"Tong ket gia vang & bac {period_label_vi} - {now.strftime('%d/%m/%Y')}"
    html_body = build_recap_html(stats, period_label_vi, period_days, timestamp)
    text_body = build_recap_text(stats, period_label_vi, period_days, timestamp)
    csv_data = history_to_csv(history)

    with open(os.path.join(RECAP_DIR, "subject.txt"), "w") as f:
        f.write(subject)
    with open(os.path.join(RECAP_DIR, "body.html"), "w") as f:
        f.write(html_body)
    with open(os.path.join(RECAP_DIR, "body.txt"), "w") as f:
        f.write(text_body)
    with open(os.path.join(RECAP_DIR, "history.csv"), "w") as f:
        f.write(csv_data)
    with open(os.path.join(RECAP_DIR, "meta.json"), "w") as f:
        json.dump({"send": True, "gold_items": len(stats["gold"]), "silver_items": len(stats["silver"])}, f)

    print(f"Generated recap email. Saved to ./{RECAP_DIR}/")


def cmd_recap_send():
    sender = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("GOLD_RECIPIENT")

    missing = [name for name, val in [
        ("GMAIL_ADDRESS", sender), ("GMAIL_APP_PASSWORD", app_password), ("GOLD_RECIPIENT", recipient),
    ] if not val]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    meta_path = os.path.join(RECAP_DIR, "meta.json")
    if not os.path.exists(meta_path):
        print("No meta.json found - run 'recap-generate' first.", file=sys.stderr)
        sys.exit(1)
    with open(meta_path) as f:
        meta = json.load(f)
    if not meta.get("send", False):
        print("Nothing to send.")
        return

    with open(os.path.join(RECAP_DIR, "subject.txt")) as f:
        subject = f.read()
    with open(os.path.join(RECAP_DIR, "body.html")) as f:
        html_body = f.read()
    with open(os.path.join(RECAP_DIR, "body.txt")) as f:
        text_body = f.read()

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text_body, "plain"))
    alt.attach(MIMEText(html_body, "html"))
    msg.attach(alt)

    csv_path = os.path.join(RECAP_DIR, "history.csv")
    if os.path.exists(csv_path):
        with open(csv_path, "rb") as f:
            attachment = MIMEApplication(f.read(), _subtype="csv")
        attachment.add_header("Content-Disposition", "attachment", filename="gold_silver_price_history.csv")
        msg.attach(attachment)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(sender, app_password)
        server.send_message(msg)

    print(f"Sent recap to {recipient}!")


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

    # "related" wraps the text/html alternative part plus the inline chart
    # image (if generate produced one), so <img src="cid:pricechart"> in
    # the HTML resolves to the attached image rather than a broken link.
    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text_body, "plain"))
    alt.attach(MIMEText(html_body, "html"))
    msg.attach(alt)

    chart_path = os.path.join(EMAIL_DIR, "chart.png")
    if meta.get("has_chart") and os.path.exists(chart_path):
        with open(chart_path, "rb") as f:
            img = MIMEImage(f.read())
        img.add_header("Content-ID", "<pricechart>")
        img.add_header("Content-Disposition", "inline", filename="chart.png")
        msg.attach(img)

    # Product-type icons (gold bar/ring, silver bar, jewelry) - baked-in
    # base64 constants, not fetched from anywhere, so this never fails.
    icons = {
        "gold_bar": ICON_GOLD_BAR_B64,
        "gold_ring": ICON_GOLD_RING_B64,
        "silver_bar": ICON_SILVER_BAR_B64,
        "jewelry": ICON_JEWELRY_B64,
    }
    for name, b64 in icons.items():
        img = MIMEImage(base64.b64decode(b64))
        img.add_header("Content-ID", f"<icon_{name}>")
        img.add_header("Content-Disposition", "inline", filename=f"{name}.png")
        msg.attach(img)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(sender, app_password)
        server.send_message(msg)

    print(f"Sent to {recipient}!")


def main():
    usage = "Usage: python gold_price_emailer.py [generate|send|recap-generate weekly|monthly|recap-send]"
    if len(sys.argv) < 2:
        print(usage, file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "generate":
        cmd_generate()
    elif cmd == "send":
        cmd_send()
    elif cmd == "recap-generate":
        if len(sys.argv) != 3:
            print(usage, file=sys.stderr)
            sys.exit(1)
        cmd_recap_generate(sys.argv[2])
    elif cmd == "recap-send":
        cmd_recap_send()
    else:
        print(usage, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
