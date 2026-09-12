# Metals spot — dealer comparison

A zero-cost dashboard that shows live gold / silver / platinum / palladium spot prices
side by side across four bullion dealers: **JM Bullion, APMEX, Money Metals, SD Bullion**.

- **Dashboard:** `index.html`, served free by GitHub Pages.
- **Data:** a GitHub Action scrapes the four dealers every 30 minutes and commits `data/prices.json`.
- The page reads that JSON and highlights the **cheapest ask per metal** and the **spread** between dealers.

## Why it's built this way

A browser on GitHub Pages can't scrape the dealer sites — cross-site `fetch` is blocked by CORS,
and none of the four offers a public price API. So the scraping runs **server-side inside the Action**
(no CORS there), which writes a JSON file, and the static page just reads its own JSON. That's the only
clean way to do this for free on GitHub.

```
GitHub Action (cron)  ->  scrape.py fetches 4 sites  ->  writes data/prices.json  ->  commits
                                                                                       |
                                              GitHub Pages serves index.html  <--------+
                                                        (reads data/prices.json)
```

> Spot prices come off the same commodity feed, so the four dealers land within pennies of each
> other. The point of the dashboard is the **Best buy** row and the **Spread**, not four identical columns.

## Setup (about 5 minutes)

1. **Create the repo** and push these files:
   ```bash
   git init && git add . && git commit -m "init"
   git branch -M main
   git remote add origin https://github.com/<you>/metals-price-dashboard.git
   git push -u origin main
   ```
2. **Enable Pages:** repo **Settings -> Pages -> Source: Deploy from a branch -> `main` / root.**
   Your dashboard goes live at `https://<you>.github.io/metals-price-dashboard/`.
3. **Allow the Action to commit:** **Settings -> Actions -> General -> Workflow permissions ->
   Read and write permissions.** (The workflow also declares `contents: write`.)
4. **First run:** go to the **Actions** tab -> *Update metal prices* -> **Run workflow**.
   After it finishes, `data/prices.json` holds real values and the page drops the "sample data" badge.

Until the first successful scrape, the page shows the seeded sample values so you can see the layout.

## Run it locally

```bash
pip install -r requirements.txt
python scrape.py            # writes data/prices.json
python -m http.server 8000  # then open http://localhost:8000
```

## If a dealer shows "—" or goes stale

`scrape.py` never crashes on a bad dealer — it keeps that dealer's last-known values and greys the row.
When a dealer consistently returns nothing, it's one of two things:

- **The markup changed.** Each dealer is a small adapter at the bottom of `scrape.py`
  (`scrape_jmbullion`, `scrape_apmex`, …). They use a shared `generic_parse` that finds the dollar
  amount next to each metal label. Adjust that dealer's adapter to match the new page.
- **Bot protection or JS-rendered prices.** Some dealers sit behind Cloudflare or inject prices with
  JavaScript, so a plain HTTP request sees no numbers (or a 403). Switch that run to a real browser:

  ```bash
  pip install playwright && playwright install chromium
  USE_PLAYWRIGHT=1 python scrape.py
  ```

  To use Playwright in the Action, add `playwright` to `requirements.txt`, add a
  `run: playwright install --with-deps chromium` step before the scrape, and set
  `env: { USE_PLAYWRIGHT: "1" }` on the scrape step. It's slower, so only enable it for the dealers
  that need it.

## Files

| File | Purpose |
|------|---------|
| `index.html` | The dashboard (self-contained; also works as a standalone preview via embedded sample data). |
| `scrape.py` | Fetches the four dealers and writes `data/prices.json`. One adapter per dealer. |
| `data/prices.json` | The data the dashboard reads. Rewritten by the Action. |
| `.github/workflows/update-prices.yml` | Schedules the scrape and commits the result. |
| `requirements.txt` | `requests`, `beautifulsoup4` (Playwright optional). |

## Tuning

- **Frequency:** edit the `cron` in the workflow (`*/30 * * * *` = every 30 min).
- **Add a dealer:** add an entry to `DEALERS` and a `scrape_*` function, then add it to `SCRAPERS`
  and to `DEALER_ORDER` in `index.html`.
