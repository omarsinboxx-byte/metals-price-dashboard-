#!/usr/bin/env python3
"""
Scrape live metal spot prices from four bullion dealers and write data/prices.json.

Design notes
------------
- Runs server-side (GitHub Action), so there is no CORS limit — unlike the browser.
- Dealers inject spot via their page, so we parse HTML text. There is no public API.
- Each dealer is a small adapter. If a dealer changes its markup, only that adapter
  needs a fix; the others keep working. A failed dealer keeps its last-known values
  (read back from the existing prices.json) and is flagged ok=false.
- Default fetch is plain HTTP (fast, no browser). If a dealer renders prices with
  JavaScript and the static HTML has no numbers, set USE_PLAYWRIGHT=1 (see README).

Exit code is always 0 so the Action still commits partial updates.
"""

import json
import os
import re
import sys
import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

METALS = ["gold", "silver", "platinum", "palladium"]
OUT = Path(__file__).parent / "data" / "prices.json"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

DEALERS = {
    "jmbullion":   {"name": "JM Bullion",   "url": "https://www.jmbullion.com"},
    "apmex":       {"name": "APMEX",         "url": "https://www.apmex.com"},
    "moneymetals": {"name": "Money Metals",  "url": "https://www.moneymetals.com/spot-prices"},
    "sdbullion":   {"name": "SD Bullion",    "url": "https://sdbullion.com"},
}
# The URL shown to users in the dashboard (homepage) can differ from the scrape URL.
HOMEPAGE = {
    "jmbullion": "https://www.jmbullion.com",
    "apmex": "https://www.apmex.com",
    "moneymetals": "https://www.moneymetals.com",
    "sdbullion": "https://sdbullion.com",
}

USE_PLAYWRIGHT = os.environ.get("USE_PLAYWRIGHT") == "1"


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_num(s):
    """'$4,362.28' -> 4362.28 ; returns None if no number found."""
    if s is None:
        return None
    m = re.search(r"-?\d[\d,]*\.?\d*", str(s))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def fetch_html(url: str, render: bool = False) -> str:
    """Fetch page HTML. render=True (or USE_PLAYWRIGHT=1) uses a real browser."""
    if render or USE_PLAYWRIGHT:
        return _fetch_rendered(url)
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.text


def _fetch_rendered(url: str) -> str:
    """JS-rendered fetch via a real headless Chromium (defeats JS rendering / basic blocks)."""
    from playwright.sync_api import sync_playwright  # imported lazily
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="en-US",
            viewport={"width": 1366, "height": 900},
        )
        page = ctx.new_page()
        page.goto(url, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(2500)  # let any late-loading price JS settle
        html = page.content()
        browser.close()
        return html


def generic_parse(html: str) -> dict:
    """
    Best-effort extraction that works when spot numbers sit in the page text near their
    metal label, e.g. 'Gold Ask $4,362.28 ▲$37.70'. For each metal we grab the first
    dollar amount after the label, plus a nearby change value and its direction.

    This is the default adapter. Tune per dealer if the layout differs (see README).
    """
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    out = {}
    for metal in METALS:
        # label -> optional Ask/Bid/Spot word -> price -> optional change
        pat = re.compile(
            metal + r"\b[^$\d]{0,40}?\$?\s*([\d,]+\.\d{2})"
            r"(?:[^$\d]{0,25}?([▲▼+\-]?)\s*\$?\s*([\d,]+\.\d{2}))?",
            re.IGNORECASE,
        )
        m = pat.search(text)
        if not m:
            continue
        ask = to_num(m.group(1))
        change = to_num(m.group(3)) if m.group(3) else None
        if change is not None and m.group(2) in ("▼", "-"):
            change = -change
        if ask is not None:
            out[metal] = {"ask": ask, "change": change}
    return out


# --- Per-dealer adapters -----------------------------------------------------
# Each returns {metal: {ask, change}}. They start on the generic parser; if a
# dealer needs bespoke handling (e.g. reading an embedded JSON blob), edit here.

def scrape_jmbullion():
    return generic_parse(fetch_html(DEALERS["jmbullion"]["url"]))


def scrape_apmex():
    # APMEX is a Next.js app; if the generic text parse comes up empty, its spot data
    # usually lives in a __NEXT_DATA__ JSON blob — parse that here as a fallback.
    html = fetch_html(DEALERS["apmex"]["url"])
    data = generic_parse(html)
    if not data:
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if m:
            blob = m.group(1)
            for metal in METALS:
                mm = re.search(metal + r'"[^}]{0,120}?"(?:ask|price|spot)"\s*:\s*([\d.]+)',
                               blob, re.IGNORECASE)
                if mm:
                    data[metal] = {"ask": to_num(mm.group(1)), "change": None}
    return data


def scrape_moneymetals():
    # Money Metals blocks plain requests / renders prices with JS -> use a real browser.
    return generic_parse(fetch_html(DEALERS["moneymetals"]["url"], render=True))


def scrape_sdbullion():
    return generic_parse(fetch_html(DEALERS["sdbullion"]["url"]))


SCRAPERS = {
    "jmbullion": scrape_jmbullion,
    "apmex": scrape_apmex,
    "moneymetals": scrape_moneymetals,
    "sdbullion": scrape_sdbullion,
}


def load_previous() -> dict:
    if OUT.exists():
        try:
            return json.loads(OUT.read_text())
        except Exception:
            pass
    return {"dealers": {}}


def main():
    prev = load_previous()
    result = {
        "updated_utc": now_iso(),
        "sample": False,
        "metals": METALS,
        "dealers": {},
    }

    for dealer_id, scraper in SCRAPERS.items():
        meta = DEALERS[dealer_id]
        entry = {
            "name": meta["name"],
            "url": HOMEPAGE[dealer_id],
            "ok": True,
            "updated_utc": now_iso(),
            "prices": {},
        }
        try:
            prices = scraper()
            got = [m for m in METALS if m in prices and prices[m].get("ask")]
            if not got:
                raise ValueError("no prices parsed from page")
            entry["prices"] = prices
            print(f"[ok]   {dealer_id:12} " +
                  " ".join(f"{m}={prices[m]['ask']}" for m in got))
        except Exception as e:  # keep last-known values, mark stale
            print(f"[FAIL] {dealer_id:12} {e}", file=sys.stderr)
            prev_entry = prev.get("dealers", {}).get(dealer_id, {})
            entry["ok"] = False
            entry["prices"] = prev_entry.get("prices", {})
            entry["updated_utc"] = prev_entry.get("updated_utc", now_iso())
        result["dealers"][dealer_id] = entry

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2) + "\n")
    ok = sum(1 for d in result["dealers"].values() if d["ok"])
    print(f"\nwrote {OUT}  ({ok}/{len(SCRAPERS)} dealers live)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
