# Vietnam Gold Prices (multi-seller) -> Email (runs on GitHub Actions, no local computer needed)

This repo emails you the current gold price comparison table for the major
Vietnamese gold sellers - SJC, DOJI, PNJ, Bao Tin Minh Chau, Bao Tin Manh
Hai, Phu Quy, Mi Hong, and Ngoc Tham - automatically, using GitHub's free
scheduled-workflow runners. Nothing needs to run on your own machine.

## Where the data comes from

It scrapes https://giavang.org/ rather than each seller's own site. Most
sellers' own price pages (SJC, PNJ, DOJI, Mi Hong, and BTMC's page/API) load
their numbers via JavaScript or block automated fetching outright, so a
plain Python scraper can't read them. giavang.org's homepage is
server-rendered and already aggregates all of the above into one comparison
table, so this repo reads that instead of maintaining 7+ fragile
per-seller scrapers. You can change `SOURCE_URL` (see below) if you'd
rather point it somewhere else later.

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
- Always worth checking the current `robots.txt` / terms of whatever
  `SOURCE_URL` you're pointed at before running this unattended long-term:
  https://giavang.org/robots.txt
- If a run reports "Parsed 0 table(s)", the site's HTML structure probably
  changed. Open the page, inspect the price tables with your browser's
  dev tools, and adjust `_iter_table_rows` / `parse_gold_prices` in
  `gold_price_emailer.py` to match.

## Adding/removing sellers or switching source

- **Add Bao Tin Minh Chau's own API or baotinmanhhai.vn back in as a
  second source**: this would mean fetching a second URL in `cmd_generate`
  and merging its rows into the `tables` list before building the email -
  ask if you'd like this wired up.
- **Point at a different aggregator or single seller**: set the
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
```

Schedule it yourself with cron (`crontab -e`):

```
*/30 * * * * cd /path/to/gold-price-emailer && /usr/bin/python3 gold_price_emailer.py generate && /usr/bin/python3 gold_price_emailer.py send >> gold_emailer.log 2>&1
```
