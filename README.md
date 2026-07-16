# Vietnam Gold & Silver Prices (multi-seller, summary + full detail) -> Email (runs on GitHub Actions, no local computer needed)

This repo emails you Vietnamese gold and silver prices for the major
sellers - SJC, DOJI, PNJ, Bao Tin Minh Chau, Bao Tin Manh Hai, Phu Quy,
Mi Hong, Ngoc Tham (gold), plus Phu Quy, DOJI, ANCARAT, BTMC, BTMH, and
Kim Ngan Phuc (silver) - automatically, using GitHub's free
scheduled-workflow runners. Nothing needs to run on your own machine.

Each email has four sections:

1. **Gold summary** - a comparison table (one row per seller) for gold
   bars and for gold rings, so you can see at a glance who's
   cheapest/most expensive right now.
2. **Gold full detail per seller** - each seller's complete product
   breakdown (different bar weights, rings, various jewelry purities,
   etc.) as its own table, the same level of detail baotinmanhhai.vn's own
   page used to give for just that one seller - now for all 8.
3. **Silver summary** - a comparison table across the major silver
   sellers/products (bars in different weights, per brand).
4. **Silver full detail per seller** - a fuller product breakdown per
   silver brand, for the brands that have their own dedicated price page
   (currently Phu Quy and ANCARAT, both large product catalogs). Brands
   without one (or whose page fails to fetch) fall back to their own
   row(s) from the summary table instead of being dropped - see "Silver
   detail coverage" below.

## Where the data comes from

- **Gold** comes from https://giavang.org/ - a homepage comparison table
  plus a dedicated detail page per seller (e.g. `giavang.org/trong-nuoc/sjc/`).
- **Silver summary** comes from https://giahanghoa.net/gia-bac - a
  comparison table across silver sellers.
- **Silver detail** comes from each brand's own dedicated page, where one
  exists and is scrapeable (see `SILVER_DETAIL_PAGES` in the script).

All of these are server-rendered. Most individual sellers' own price
pages (SJC, PNJ, DOJI, Mi Hong, BTMC's page/API, and DOJI's silver page)
load their numbers via JavaScript or block automated fetching outright,
so a plain Python scraper can't read them directly - these aggregator/
dedicated pages sidestep that. That means **12 requests per run**: 1 gold
summary + 8 gold seller detail pages + 1 silver summary + 2 silver detail
pages. You can change `SOURCE_URL` / `SILVER_URL` / the `SELLERS` list /
`SILVER_DETAIL_PAGES` in the script if you'd rather point it somewhere
else, or add/remove sellers/brands, later.

If any single page fails to fetch or its markup changes, only that
section (or that one brand's row, for silver detail) notes the issue —
the rest of the email still generates and sends normally.

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
