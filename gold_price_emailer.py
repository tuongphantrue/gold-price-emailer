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

    m = re.search(r"([\d,]+\.\d+)\s*USD\s*([+-]?[\d,]+\.\d+)USD\(([+-]?[\d,]+\.\d+)%\)", text)
    if not m:
        return None
    result = {
        "xau_usd": float(m.group(1).replace(",", "")),
        "change_usd": float(m.group(2).replace(",", "")),
        "change_pct": float(m.group(3).replace(",", "")),
    }

    m2 = re.search(r"1 Ounce\s*=\s*([\d.,]+)\s*VNĐ", text)
    if m2:
        result["vnd_per_ounce"] = _parse_vnd_number(m2.group(1))

    m3 = re.search(r"quy đổi sang tiền Việt Nam Đồng có giá là\s*([\d.,]+)\s*VNĐ", text)
    if m3:
        result["vnd_per_luong"] = _parse_vnd_number(m3.group(1))

    if result.get("vnd_per_ounce") and result["xau_usd"]:
        result["implied_usdvnd_rate"] = result["vnd_per_ounce"] / result["xau_usd"]

    return result


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


def compute_big_moves(price_changes, threshold_pct=ALERT_THRESHOLD_PCT):
    """
    Scan price_changes (gold + silver) for any item whose absolute
    percent move - on any available period, including silver's same-day
    source figure - meets or exceeds threshold_pct. Purely derived from
    data already computed in compute_price_changes, no extra requests.
    Returns a list of {label, period, diff, pct} - one entry per
    (item, period) combination that crossed the threshold, largest |pct|
    first.
    """

    def _pct_from_source_today(s):
        # silver's "source_today" field is a raw string like "+4.000" with
        # no percent - can't compute a % move from it alone, so it's
        # excluded from threshold scanning (still shown in the changes
        # section itself, just not eligible for this alert).
        return None

    flagged = []
    for rows in price_changes["gold"]:
        for r in rows:
            for period_label, change in r["changes"].items():
                if change and change["pct"] is not None and abs(change["pct"]) >= threshold_pct:
                    flagged.append({
                        "label": r["label"], "period": period_label,
                        "diff": change["diff"], "pct": change["pct"],
                    })
    for r in price_changes["silver"]:
        for period_label, change in r["changes"].items():
            if change and change["pct"] is not None and abs(change["pct"]) >= threshold_pct:
                flagged.append({
                    "label": r["label"], "period": period_label,
                    "diff": change["diff"], "pct": change["pct"],
                })

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

CARD_STYLE = (
    f"background:#ffffff;border:1px solid {COLOR_BORDER};border-radius:14px;"
    "padding:22px 24px;margin-bottom:18px;box-shadow:0 1px 3px rgba(15,23,42,0.06);"
)


def _card(icon, title, accent, body_html):
    return f"""
    <div style="{CARD_STYLE}">
      <p style="margin:0;font-size:16.5px;font-weight:700;color:#111827;font-family:{FONT_STACK};">
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


def _table_html(rows, label_header, tint=COLOR_GOLD_TINT):
    body = "".join(
        f"<tr style='background:{_tr_bg(i)}'>"
        f"<td style='{_TD}'><strong>{escape(r['label'])}</strong>{_region_span(r['region'])}</td>"
        f"<td style='{_TD}text-align:right;'>{escape(r['buy'])}</td>"
        f"<td style='{_TD}text-align:right;'>{escape(r['sell'])}</td>"
        f"</tr>"
        for i, r in enumerate(rows)
    )
    return _table_open([label_header, "Mua vào", "Bán ra"], ["left", "right", "right"], tint) + body + _TABLE_CLOSE


def _silver_table_html(rows):
    body = "".join(
        f"<tr style='background:{_tr_bg(i)}'>"
        f"<td style='{_TD}'><strong>{escape(r['brand'])}</strong></td>"
        f"<td style='{_TD}'>{escape(r['product'])}</td>"
        f"<td style='{_TD}text-align:right;'>{escape(r['buy'])}</td>"
        f"<td style='{_TD}text-align:right;'>{escape(r['sell'])}</td>"
        f"</tr>"
        for i, r in enumerate(rows)
    )
    return (
        _table_open(["Thương hiệu", "Sản phẩm", "Mua vào", "Bán ra"], ["left", "left", "right", "right"], COLOR_SILVER_TINT)
        + body + _TABLE_CLOSE
    )


def _silver_detail_table_html(products):
    body = "".join(
        f"<tr style='background:{_tr_bg(i)}'>"
        f"<td style='{_TD}'>{escape(p['product'])}</td>"
        f"<td style='{_TD}text-align:right;'>{escape(p['buy'])}</td>"
        f"<td style='{_TD}text-align:right;'>{escape(p['sell'])}</td>"
        f"</tr>"
        for i, p in enumerate(products)
    )
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


def build_html(summary_tables, details, silver, silver_details, price_changes, world_gold, gap_rows,
                spreads, big_moves, has_chart, source_url, timestamp):
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
            parts.append(_table_html(rows, "Đơn vị bán"))
        summary_html = "\n".join(parts)

    # --- Section 2: full detail per seller ---
    detail_parts = []
    for name, info in details.items():
        detail_parts.append(f"<p style='font-size:13px;font-weight:700;color:{COLOR_GOLD};margin:16px 0 8px;'>{escape(name)}</p>")
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

    # --- Section 6: world gold price + domestic-world gap ---
    world_html = _world_gold_html(world_gold, gap_rows)

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

    return f"""\
<html>
  <body style="margin:0;padding:0;background:#eef1f5;font-family:{FONT_STACK};">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef1f5;">
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
              <td style="background:#ffffff;border-left:1px solid {COLOR_BORDER};border-right:1px solid {COLOR_BORDER};padding:14px 16px 4px;">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>{chips}</tr></table>
              </td>
            </tr>
            <tr><td style="background:#ffffff;border-left:1px solid {COLOR_BORDER};border-right:1px solid {COLOR_BORDER};border-bottom:1px solid {COLOR_BORDER};border-radius:0 0 16px 16px;padding:0 0 18px;"></td></tr>

            <tr><td style="height:20px;"></td></tr>

            <tr><td>{_card("🥇", "Vàng - Tổng hợp so sánh giữa các đơn vị", COLOR_GOLD, summary_html)}</td></tr>
            <tr><td>{_card("📋", "Vàng - Chi tiết đầy đủ theo từng đơn vị", COLOR_GOLD, detail_html)}</td></tr>
            <tr><td>{_card("🥈", "Bạc - So sánh giữa các đơn vị", COLOR_SILVER, silver_html)}</td></tr>
            <tr><td>{_card("📋", "Bạc - Chi tiết đầy đủ theo từng đơn vị", COLOR_SILVER, silver_detail_html)}</td></tr>
            <tr><td>{_card("📊", "Biến động giá (7 ngày / 30 ngày / 1 năm)", COLOR_BLUE, changes_html)}</td></tr>
            <tr><td>{_card("🌍", "Giá vàng thế giới &amp; chênh lệch trong nước/thế giới", COLOR_GREEN_ACCENT, world_html)}</td></tr>
            <tr><td>{_card("↔️", "Chênh lệch mua-bán (spread)", COLOR_BLUE, spread_html)}</td></tr>
            <tr><td>{_card("🚨", f"Cảnh báo biến động lớn (&ge; {ALERT_THRESHOLD_PCT:.1f}%)", COLOR_RED_ACCENT, big_moves_html)}</td></tr>
            <tr><td>{_card("📈", "Biểu đồ xu hướng giá", COLOR_TEXT, chart_html)}</td></tr>

            <tr>
              <td style="padding:8px 6px 0;">
                <p style="color:{COLOR_MUTED};font-size:11.5px;line-height:1.6;margin:0;">
                  Nguồn: <a href="{escape(source_url)}" style="color:{COLOR_BLUE};">{escape(source_url)}</a> (vàng),
                  <a href="{escape(SILVER_URL)}" style="color:{COLOR_BLUE};">{escape(SILVER_URL)}</a> (bạc) &middot;
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
                      spreads, big_moves, has_chart, source_url, timestamp):
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

    spreads = compute_spreads(summary_tables, silver.get("rows", []))
    big_moves = compute_big_moves(price_changes)
    print(f"Big moves: {len(big_moves)} item(s) at or above {ALERT_THRESHOLD_PCT:.1f}%.")

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
        world_gold, gap_rows, spreads, big_moves, has_chart, SOURCE_URL, timestamp,
    )
    text_body = build_plain_text(
        summary_tables, details, silver, silver_details, price_changes,
        world_gold, gap_rows, spreads, big_moves, has_chart, SOURCE_URL, timestamp,
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
                "big_moves_count": len(big_moves),
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
        f"{changes_count} change rows, {len(big_moves)} big moves). Saved to ./{EMAIL_DIR}/"
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
