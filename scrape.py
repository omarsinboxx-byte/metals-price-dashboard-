#!/usr/bin/env python3
"""
Writes three JSON files the dashboard reads:

  data/prices.json   dealer spot comparison (JM Bullion, APMEX, Money Metals, SD Bullion)
  data/history.json  daily price history for 4 metals + WTI + Brent (Yahoo Finance)
  data/oil.json      current WTI + Brent price/change (Yahoo Finance)

Dealer prices are scraped from each dealer's page. History and oil come from Yahoo
Finance's public chart endpoint (no API key). The Yahoo part is wrapped so that if it
fails, the dealer table still updates. Every dealer/source is independent: one failure
never blocks the others. Exit code is always 0 so the Action still commits what worked.
"""

import json
import os
import re
import sys
import datetime
from urllib.parse import quote
from pathlib import Path

import requests
from bs4 import BeautifulSoup

METALS = ["gold", "silver", "platinum", "palladium"]
DATA = Path(__file__).parent / "data"

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
HOMEPAGE = {
    "jmbullion": "https://www.jmbullion.com",
    "apmex": "https://www.apmex.com",
    "moneymetals": "https://www.moneymetals.com",
    "sdbullion": "https://sdbullion.com",
}

# Yahoo Finance symbols for history + energy.
YAHOO = {
    "gold":      "GC=F",
    "silver":    "SI=F",
    "platinum":  "PL=F",
    "palladium": "PA=F",
    "wti":       "CL=F",   # WTI crude
    "brent":     "BZ=F",   # Brent crude
    "natgas":    "NG=F",   # Henry Hub natural gas
    "gasoline":  "RB=F",   # RBOB gasoline
    "heating":   "HO=F",   # heating oil
}
ENERGY = ["wti", "brent", "natgas", "gasoline", "heating"]

USE_PLAYWRIGHT = os.environ.get("USE_PLAYWRIGHT") == "1"


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_num(s):
    if s is None:
        return None
    m = re.search(r"-?\d[\d,]*\.?\d*", str(s))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


# ---------- dealer scraping ----------

def fetch_html(url, render=False):
    if render or USE_PLAYWRIGHT:
        return _fetch_rendered(url)
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.text


def _fetch_rendered(url):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(user_agent=HEADERS["User-Agent"], locale="en-US",
                                  viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        page.goto(url, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(2500)
        html = page.content()
        browser.close()
        return html


def generic_parse(html):
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    out = {}
    for metal in METALS:
        pat = re.compile(
            metal + r"\b[^$\d]{0,40}?\$?\s*([\d,]+\.\d{2})"
            r"(?:[^$\d]{0,25}?([\u25b2\u25bc+\-]?)\s*\$?\s*([\d,]+\.\d{2}))?",
            re.IGNORECASE,
        )
        m = pat.search(text)
        if not m:
            continue
        ask = to_num(m.group(1))
        change = to_num(m.group(3)) if m.group(3) else None
        if change is not None and m.group(2) in ("\u25bc", "-"):
            change = -change
        if ask is not None:
            out[metal] = {"ask": ask, "change": change}
    return out


def scrape_jmbullion():
    return generic_parse(fetch_html(DEALERS["jmbullion"]["url"]))


def scrape_apmex():
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
    return generic_parse(fetch_html(DEALERS["moneymetals"]["url"], render=True))


def scrape_sdbullion():
    return generic_parse(fetch_html(DEALERS["sdbullion"]["url"]))


SCRAPERS = {
    "jmbullion": scrape_jmbullion,
    "apmex": scrape_apmex,
    "moneymetals": scrape_moneymetals,
    "sdbullion": scrape_sdbullion,
}


def load_json(name):
    p = DATA / name
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def build_prices():
    prev = load_json("prices.json")
    result = {"updated_utc": now_iso(), "sample": False, "metals": METALS, "dealers": {}}
    for dealer_id, scraper in SCRAPERS.items():
        meta = DEALERS[dealer_id]
        entry = {"name": meta["name"], "url": HOMEPAGE[dealer_id], "ok": True,
                 "updated_utc": now_iso(), "prices": {}}
        try:
            prices = scraper()
            got = [m for m in METALS if m in prices and prices[m].get("ask")]
            if not got:
                raise ValueError("no prices parsed from page")
            entry["prices"] = prices
            print(f"[ok]   {dealer_id:12} " + " ".join(f"{m}={prices[m]['ask']}" for m in got))
        except Exception as e:
            print(f"[FAIL] {dealer_id:12} {e}", file=sys.stderr)
            pe = prev.get("dealers", {}).get(dealer_id, {})
            entry["ok"] = False
            entry["prices"] = pe.get("prices", {})
            entry["updated_utc"] = pe.get("updated_utc", now_iso())
        result["dealers"][dealer_id] = entry
    (DATA / "prices.json").write_text(json.dumps(result, indent=2) + "\n")
    ok = sum(1 for d in result["dealers"].values() if d["ok"])
    print(f"wrote prices.json  ({ok}/{len(SCRAPERS)} dealers live)")


# ---------- Yahoo Finance history + oil ----------

def yahoo_chart(symbol, rng="1y", interval="1d"):
    """Return (points, price, prev_close). points = [{'t': 'YYYY-MM-DD', 'c': float}]."""
    sym = quote(symbol, safe="")
    last_err = None
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval={interval}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
            ts = res.get("timestamp", []) or []
            closes = res["indicators"]["quote"][0].get("close", []) or []
            points = []
            for t, c in zip(ts, closes):
                if c is None:
                    continue
                d = datetime.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d")
                points.append({"t": d, "c": round(float(c), 2)})
            meta = res.get("meta", {})
            price = meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            return points, price, prev
        except Exception as e:
            last_err = e
    raise last_err


def build_markets():
    """History for all 6 symbols + current oil. Wrapped by caller so it can't break prices."""
    history = {"updated_utc": now_iso(), "range": "1y", "series": {}}
    oil = {"updated_utc": now_iso()}
    for key, sym in YAHOO.items():
        try:
            points, price, prev = yahoo_chart(sym, rng="1y")
            if not points:
                raise ValueError("empty history")
            history["series"][key] = {"symbol": sym, "points": points}
            print(f"[ok]   yahoo {key:10} {len(points)} pts  last={points[-1]['c']}")
            if key in ENERGY and price is not None:
                chg = (price - prev) if prev else None
                pct = (chg / prev * 100) if (chg is not None and prev) else None
                oil[key] = {
                    "price": round(float(price), 4),
                    "change": round(chg, 4) if chg is not None else None,
                    "changePct": round(pct, 2) if pct is not None else None,
                }
        except Exception as e:
            print(f"[FAIL] yahoo {key:10} {e}", file=sys.stderr)
    if history["series"]:
        (DATA / "history.json").write_text(json.dumps(history) + "\n")
        print(f"wrote history.json ({len(history['series'])}/{len(YAHOO)} series)")
    if any(k in oil for k in ENERGY):
        (DATA / "oil.json").write_text(json.dumps(oil, indent=2) + "\n")
        print("wrote oil.json")


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    build_prices()
    try:
        build_markets()
    except Exception as e:
        print(f"[FAIL] markets block {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
