# Vietnam Gold & Silver Prices (multi-seller, summary + full detail) -> Email (runs on GitHub Actions, no local computer needed)

This repo emails you Vietnamese gold and silver prices for the major
sellers - SJC, DOJI, PNJ, Bao Tin Minh Chau, Bao Tin Manh Hai, Phu Quy,
Mi Hong, Ngoc Tham (gold), plus Phu Quy, DOJI, ANCARAT, BTMC, BTMH, and
Kim Ngan Phuc (silver) - automatically, using GitHub's free
scheduled-workflow runners. Nothing needs to run on your own machine.

Each email has eleven sections:

1. **Source health banner** - a one-line "X/Y sources OK" status at the top, so a partial silent failure is visible in the email itself.
2. **Gold summary** - a comparison table (one row per seller) for gold
   bars and for gold rings, so you can see at a glance who's
   cheapest/most expensive right now.
3. **Gold full detail per seller** - each seller's complete product
   breakdown (different bar weights, rings, various jewelry purities,
   etc.) as its own table, the same level of detail baotinmanhhai.vn's own
   page used to give for just that one seller - now for all 8.
4. **Silver summary** - a comparison table across the major silver
   sellers/products (bars in different weights, per brand).
5. **Silver full detail per seller** - a fuller product breakdown per
   silver brand, for the brands that have their own dedicated price page
   (currently Phu Quy and ANCARAT, both large product catalogs). Brands
   without one (or whose page fails to fetch) fall back to their own
   row(s) from the summary table instead of being dropped - see "Silver
   detail coverage" below.
6. **Price changes** - 7-day/30-day/1-year change per item, from a
   self-recorded daily snapshot (silver also gets an immediate same-day
   figure straight from the source).
7. **30/90-day extremes** - flags any item currently at its highest or
   lowest point in the last 30/90 days of recorded history.
8. **World gold price & domestic/world gap** - live XAU/USD, converted
   to VND, plus the real Vietcombank USD/VND rate and how far domestic
   gold-bar prices sit above the world price.
9. **Buy/sell spread** - the Mua vào/Bán ra gap per item.
10. **Big-move alerts** - items whose change crosses a threshold (see
    "Watchlist & alert thresholds" below), highlighted separately.
11. **Your portfolio** - if you've configured `HOLDINGS_JSON` (see
    below), your holdings' current value and gain/loss.
12. **Price trend chart** - an embedded chart image once enough history
    has accumulated.

There's also a separate **weekly/monthly recap email** (its own workflow,
`send-gold-recap.yml`) with high/low/net-change over the period, plus the
full price history attached as a CSV - see "Weekly/monthly recap" below.

## Where the data comes from

- **Gold** comes from https://giavang.org/ - a homepage comparison table
  plus a dedicated detail page per seller (e.g. `giavang.org/trong-nuoc/sjc/`).
- **Silver summary** comes from https://giahanghoa.net/gia-bac - a
  comparison table across silver sellers.
- **Silver detail** comes from each brand's own dedicated page, where one
  exists and is scrapeable (see `SILVER_DETAIL_PAGES` in the script).
- **World gold price** comes from https://giavang.org/the-gioi/.
- **Real Vietcombank USD/VND rate** comes from
  https://tygiausd.org/nganhang/vietcombank.

All of these are server-rendered. Most individual sellers' own price
pages (SJC, PNJ, DOJI, Mi Hong, BTMC's page/API, and DOJI's silver page)
load their numbers via JavaScript or block automated fetching outright,
so a plain Python scraper can't read them directly - these aggregator/
dedicated pages sidestep that. That means **14 requests per run**: 1 gold
summary + 8 gold seller detail pages + 1 silver summary + 2 silver detail
pages + 1 world gold price + 1 Vietcombank rate. You can change
`SOURCE_URL` / `SILVER_URL` / `WORLD_GOLD_URL` / `VCB_RATE_URL` / the
`SELLERS` list / `SILVER_DETAIL_PAGES` in the script if you'd rather
point it somewhere else, or add/remove sellers/brands, later.

If any single page fails to fetch or its markup changes, only that
section (or that one brand's row, for silver detail) notes the issue —
the rest of the email still generates and sends normally, and the
source-health banner at the top of the email will show it as one of the
failures for that run.

## Watchlist & alert thresholds

By default, the big-move alerts section scans **every** tracked item
against a single global threshold (`ALERT_THRESHOLD_PCT`, default `3.0`).
Two optional repo secrets narrow that down:

- **`WATCHLIST`** - comma-separated list of item labels to scan (everything
  else is ignored for alerting purposes, though still shown normally in
  the summary/detail sections). Labels must match exactly, e.g.:
  ```
  SJC,Phú Quý - BẠC MIẾNG PHÚ QUÝ 999 1 LƯỢNG
  ```
- **`ALERT_THRESHOLDS_JSON`** - per-item threshold overriding the global
  one, as a JSON object:
  ```json
  {"SJC": 2.0, "DOJI": 5.0}
  ```

Both are optional - leave unset to keep the original "everything, 3%"
behavior. Add them the same way as other secrets: Settings -> Secrets
and variables -> Actions -> New repository secret.

## AI-written market commentary

Optional: set the `ANTHROPIC_API_KEY` repo secret and every email opens
with a short (3-4 sentence) Vietnamese paragraph - actually written by
Claude each run, summarizing that run's real numbers (SJC price, world
gold move, domestic/world gap, biggest mover, any 30/90-day extremes).
Not a template - genuinely generated prose grounded strictly in the data
computed that run.

**Setup:**
1. Get an API key: https://console.anthropic.com/settings/keys
2. Add it as a repo secret named `ANTHROPIC_API_KEY` (same process as the
   other secrets: Settings -> Secrets and variables -> Actions -> New
   repository secret)
3. That's it - the section appears automatically once the key is set.

**Cost:** uses Haiku by default (cheapest Claude model) - a few cents to
maybe a dollar a month at the 30-minute schedule, depending on current
pricing. Set the `COMMENTARY_MODEL` secret to `claude-sonnet-5` for
richer/more nuanced writing at higher cost per run, or back to
`claude-haiku-4-5-20251001` (the default) any time.

**If unset:** the section is simply absent - no cost, no failed
requests, no change to anything else. If the API call fails for any
reason (bad key, rate limit, network issue) that run's email still sends
normally, just without the commentary section that time.

## Your portfolio (holdings tracker)

Set the `HOLDINGS_JSON` repo secret to a JSON list describing what you
own, and the email will show current value and gain/loss for each item
plus a total:

```json
[
  {"label": "SJC", "kind": "gold", "amount": 2, "buy_price": 140000000},
  {"label": "Phú Quý - BẠC MIẾNG PHÚ QUÝ 999 1 LƯỢNG", "kind": "silver", "amount": 10, "buy_price": 2000000}
]
```

- `label` must match the item's label exactly as it appears elsewhere in
  the email - the seller name for gold-bars row 1, or `"brand - product"`
  for silver (copy it from a recent email to be sure).
- `kind` is `"gold"` or `"silver"`.
- `amount` is in whatever unit the source quotes that item in (usually
  lượng for gold, lượng/kg depending on the silver product).
- `buy_price` is your cost basis per unit, same currency/unit as the
  source's price.

Leave `HOLDINGS_JSON` unset to skip this section (it'll show a small
"not configured" note instead).

## Weekly/monthly recap

A second, separate workflow (`.github/workflows/send-gold-recap.yml`)
sends a calmer digest - once a week (Monday 00:00 UTC) and once a month
(1st of the month, 00:00 UTC) - summarizing each item's high/low/net
change over that period, with the **full accumulated price history
attached as a CSV** so you can open it in Excel/Sheets. It reads the same
`price_history.json` the main workflow maintains (read-only - it doesn't
need write access), so the main workflow needs to have run at least once
first.

Upload `send-gold-recap.yml` alongside the other files (same
`.github/workflows/` folder) and it uses the same three Gmail secrets -
no extra setup needed. You can also trigger it manually via Actions ->
"Send Gold Recap" -> "Run workflow", picking weekly or monthly.

## Silver detail coverage

Only some silver brands have their own dedicated, scrapeable price page:

| Brand | Dedicated page? |
|---|---|
| Phú Quý | Yes - https://giabac.phuquygroup.vn/ |
| ANCARAT | Yes - https://giabac.ancarat.com/ |
| DOJI | No - its page (`giabac.doji.vn`) loads prices via JavaScript with no static data, so this falls back to DOJI's row(s) from the summary table |
| Bảo Tín Minh Châu, Bảo Tín Mạnh Hải, Kim Ngân Phúc, others | Not currently mapped - falls back to the summary table's row(s) for that brand |

If you find a working dedicated page for one of the fallback brands, add
it to the `SILVER_DETAIL_PAGES` dict near the top of
`gold_price_emailer.py` (brand name must match exactly how it appears in
the silver summary table) and it'll start using that instead.

## One-time setup (~5 minutes)

1. **Create a GitHub account** if you don't have one: https://github.com/join

2. **Create a new repository**
   - Click "+" (top right) -> "New repository"
   - Name it anything, e.g. `gold-price-emailer`
   - Set it to **Private** (recommended, keeps your workflow config private)
   - Click "Create repository"

3. **Upload these files** to the repo (drag-and-drop works fine via the
   GitHub web UI: "Add file" -> "Upload files"), keeping the folder structure:
   - `gold_price_emailer.py`
   - `requirements.txt`
   - `.github/workflows/send-gold-price.yml`
   - `.github/workflows/send-gold-recap.yml` (optional - weekly/monthly digest, see below)

4. **Create a Gmail App Password** (your normal Gmail password won't work):
   - Turn on 2-Step Verification: https://myaccount.google.com/signinoptions/two-step-verification
   - Then create an app password: https://myaccount.google.com/apppasswords
   - Choose "Mail" as the app, copy the 16-character password it gives you.

5. **Add your secrets to the repo** (this keeps your email/password out of the code):
   - In your repo: Settings -> Secrets and variables -> Actions -> "New repository secret"
   - Add three secrets:
     - `GMAIL_ADDRESS` = your Gmail address
     - `GMAIL_APP_PASSWORD` = the 16-character app password from step 4
     - `GOLD_RECIPIENT` = the email address that should receive the price update

6. **Test it manually**
   - Go to the "Actions" tab in your repo
   - Click "Send Gold Price" on the left
   - Click "Run workflow" -> "Run workflow" (green button)
   - Wait ~15-20 seconds, refresh, click into the run to see logs / confirm success
   - Check the recipient inbox for the email

That's it — from now on it runs automatically on the schedule below, with
no computer of yours needing to be on.

## Changing the schedule

Open `.github/workflows/send-gold-price.yml` and edit this line:

```
- cron: "*/30 * * * *"
```

Cron format is `minute hour day month weekday`, always in **UTC**. Examples:

- `0 * * * *`      -> every hour
- `*/30 * * * *`    -> every 30 minutes (current setting)
- `0 */3 * * *`     -> every 3 hours
- `0 8,12,16,20 * * *` -> 8am/12pm/4pm/8pm UTC (3pm/7pm/11pm/3am Vietnam, UTC+7)

A handy converter: https://crontab.guru (shows what a cron string means, but
you still need to convert your local time to UTC yourself, e.g. via
https://www.timeanddate.com/worldclock/converter.html)

## Only emailing on price changes

Currently the workflow has `SEND_ONLY_ON_CHANGE: "false"`, so **every**
scheduled run sends an email with that moment's prices, whether or not
they've moved since last time. If you'd rather only get emailed when a
price actually changes, open `.github/workflows/send-gold-price.yml`,
find that line under the "Generate email" step, and change it to:

```
SEND_ONLY_ON_CHANGE: "true"
```

With that on, `generate` compares the freshly scraped prices against a hash
saved from the last run — stored in `state/last_price.json` on a dedicated
`gold-price-state` branch the workflow creates/updates automatically — and
skips the email if nothing changed.

## Notes

- GitHub Actions free tier includes 2,000 minutes/month for private repos —
  this job takes a few seconds a run, so it's effectively free even at a
  30-minute cadence.
- You can also trigger it manually anytime via the "Run workflow" button.
- If the run fails, check the Actions tab -> the failed run -> logs. Common
  causes: a secret is missing/misspelled, the Gmail app password was
  revoked, or giavang.org changed its page markup (see below).
- Always worth checking the current `robots.txt` / terms of whatever sites
  you're pointed at before running this unattended long-term, e.g.:
  https://giavang.org/robots.txt , https://giahanghoa.net/robots.txt ,
  https://giabac.phuquygroup.vn/robots.txt , https://giabac.ancarat.com/robots.txt
- This makes 14 requests per run (1 gold summary + 8 gold seller detail
  pages + 1 silver summary + 2 silver detail pages + 1 world gold price +
  1 Vietcombank rate). At the current every-30-minutes schedule that's
  about 670 requests/day combined across all sites. If that feels like
  too much load for sites you don't operate, consider a longer interval
  (e.g. every 1-3 hours) via the cron line above.
- If a run reports "Parsed 0 table(s)", the site's HTML structure probably
  changed. Open the page, inspect the price tables with your browser's
  dev tools, and adjust `_iter_table_rows` / `parse_gold_prices` in
  `gold_price_emailer.py` to match.

## Adding/removing sellers or switching source

- **Add/remove a seller from the detail section**: edit the `SELLERS` list
  near the top of `gold_price_emailer.py` - it's a list of
  `(display name, giavang.org URL slug)` pairs. Find a seller's slug from
  its URL, e.g. `giavang.org/trong-nuoc/sjc/` -> slug is `sjc`.
- **Add Bao Tin Minh Chau's own API or baotinmanhhai.vn back in as an
  additional source** (rather than via giavang.org): this would mean
  fetching that URL separately in `cmd_generate` and merging its rows in -
  ask if you'd like this wired up.
- **Point the summary section at a different aggregator**: set the
  `SOURCE_URL` environment variable (in the workflow's "Generate email"
  step) to the new URL. If its table markup differs from giavang.org's,
  the parsing functions may need adjusting.

## Troubleshooting: SSL certificate errors in Actions

If the "Generate email" step fails with something like:

```
SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED]
certificate verify failed: unable to get local issuer certificate ...'))
```

this means the runner couldn't build a trust chain for the source site's
certificate — usually because the site doesn't send its full intermediate
certificate chain (something browsers paper over via AIA fetching, but
Python's TLS stack doesn't do automatically), occasionally because of a
stale cached `certifi` CA bundle in the runner.

The workflow already upgrades `certifi` fresh on every run, and the script
points `requests` at that bundle explicitly, which fixes the stale-cache
case. If it's still failing after that, it's most likely the site's chain
itself. You can confirm by checking the padlock/certificate details for
the source site in a desktop browser — if the browser also warns or shows
an incomplete chain, that's the site's issue, not this script's.

As a last resort, you can add this secret to opt into skipping TLS
verification for just this one scrape request:

- In your repo: Settings -> Secrets and variables -> Actions -> "New repository secret"
- `ALLOW_INSECURE_SSL_FALLBACK` = `true`

This is scraping a public price page (no login, no credentials in
transit), which is why it's offered as an opt-in rather than refused
outright — but it does mean that particular request's response could be
tampered with in transit without you knowing, so leave it off unless
you've confirmed the failure is really the site's broken chain.

## Running locally instead

If you'd rather run this on your own machine instead of GitHub Actions:

```bash
pip install -r requirements.txt
export GMAIL_ADDRESS="you@gmail.com"
export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"
export GOLD_RECIPIENT="you@gmail.com"
python gold_price_emailer.py generate
python gold_price_emailer.py send

# Weekly/monthly recap (reads the same price_history.json state file):
python gold_price_emailer.py recap-generate weekly    # or: monthly
python gold_price_emailer.py recap-send
```

Schedule it yourself with cron (`crontab -e`):

```
*/30 * * * * cd /path/to/gold-price-emailer && /usr/bin/python3 gold_price_emailer.py generate && /usr/bin/python3 gold_price_emailer.py send >> gold_emailer.log 2>&1
```
