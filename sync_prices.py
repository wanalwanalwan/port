"""
Sync live crypto prices into the Notion "Assets" database.

For every row where Class = Crypto and Status != Exited:
  - If "CoinGecko ID" is empty but "Ticker" is set, the ID is looked up
    automatically (top-500 coins, unambiguous matches only) and written back
  - If "CoinGecko ID" is set      -> price from CoinGecko (batched, one request)
  - Else if "DexScreener Pair" set -> price from DexScreener (format: chain/pairAddress)
  - Else                          -> skipped (logged)
Writes "Current Price" and "Price Updated".

Stdlib only. Env vars:
  NOTION_TOKEN           (required) Notion internal integration secret
  NOTION_DATA_SOURCE_ID  (required) Assets data source ID
  COINGECKO_API_KEY      (optional) CoinGecko demo API key
  DRY_RUN=1              (optional) print prices, don't write to Notion
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

NOTION_VERSION = "2025-09-03"
NOTION_API = "https://api.notion.com/v1"

NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
DATA_SOURCE_ID = os.environ.get("NOTION_DATA_SOURCE_ID")
COINGECKO_KEY = os.environ.get("COINGECKO_API_KEY", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"


def http(method, url, headers=None, body=None, retries=3):
  headers = {"User-Agent": "Mozilla/5.0 (crypto-price-sync)", **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            # Back off on rate limits / transient server errors
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = int(e.headers.get("Retry-After", 2 ** (attempt + 1)))
                time.sleep(wait)
                continue
            detail = e.read().decode(errors="replace")
            raise RuntimeError(f"{method} {url} -> {e.code}: {detail}") from None


def notion(method, path, body=None):
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    return http(method, f"{NOTION_API}{path}", headers, body)


def plain_text(prop):
    if not prop:
        return ""
    items = prop.get("rich_text") or prop.get("title") or []
    return "".join(i.get("plain_text", "") for i in items).strip()


def fetch_rows():
    """All open crypto rows from the Assets data source (handles pagination)."""
    body = {
        "filter": {
            "and": [
                {"property": "Class", "select": {"equals": "Crypto"}},
                {"property": "Status", "select": {"does_not_equal": "Exited"}},
            ]
        },
        "page_size": 100,
    }
    rows, cursor = [], None
    while True:
        if cursor:
            body["start_cursor"] = cursor
        res = notion("POST", f"/data_sources/{DATA_SOURCE_ID}/query", body)
        for page in res["results"]:
            p = page["properties"]
            rows.append({
                "id": page["id"],
                "name": plain_text(p.get("Asset")),
                "ticker": plain_text(p.get("Ticker")).lstrip("$").lower(),
                "coingecko_id": plain_text(p.get("CoinGecko ID")).lower(),
                "dex_pair": plain_text(p.get("DexScreener Pair")).strip("/"),
            })
        if not res.get("has_more"):
            return rows
        cursor = res["next_cursor"]


def coingecko_prices(ids):
    if not ids:
        return {}
    qs = urllib.parse.urlencode({"ids": ",".join(sorted(ids)), "vs_currencies": "usd"})
    headers = {"accept": "application/json"}
    if COINGECKO_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_KEY
    res = http("GET", f"https://api.coingecko.com/api/v3/simple/price?{qs}", headers)
    return {cid: v["usd"] for cid, v in res.items() if "usd" in v}


AUTO_RESOLVE_MAX_RANK = 500  # only auto-pick coins in CoinGecko's top 500 by market cap


def resolve_coingecko_id(ticker, name):
    """
    Guess a CoinGecko ID from a ticker. Returns (id, None) when confident,
    or (None, reason) when a human should decide.

    Confident = an exact symbol match in the top AUTO_RESOLVE_MAX_RANK by market cap,
    and (if several match) it is by far the largest. Small/new tokens are never
    auto-picked because copycat tokens reuse their tickers.
    """
    headers = {"accept": "application/json"}
    if COINGECKO_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_KEY
    qs = urllib.parse.urlencode({"query": ticker})
    res = http("GET", f"https://api.coingecko.com/api/v3/search?{qs}", headers)

    matches = [c for c in res.get("coins", []) if c.get("symbol", "").lower() == ticker]
    ranked = sorted((c for c in matches if c.get("market_cap_rank")),
                    key=lambda c: c["market_cap_rank"])
    if not ranked:
        return None, f"no ranked coin with ticker '{ticker}' ({len(matches)} unranked)"

    top = ranked[0]
    if top["market_cap_rank"] > AUTO_RESOLVE_MAX_RANK:
        return None, f"best match '{top['id']}' is rank {top['market_cap_rank']}, too small to auto-pick"

    # If the row's name matches a specific candidate, trust that over rank
    for c in ranked:
        if name and c.get("name", "").lower() == name.lower():
            return c["id"], None

    if len(ranked) > 1 and ranked[1]["market_cap_rank"] <= AUTO_RESOLVE_MAX_RANK:
        others = ", ".join(c["id"] for c in ranked[:4])
        return None, f"ambiguous ticker '{ticker}': {others}"
    return top["id"], None


def dexscreener_price(pair):
    """pair = 'chainId/pairAddress', as in https://dexscreener.com/<chainId>/<pairAddress>"""
    res = http("GET", f"https://api.dexscreener.com/latest/dex/pairs/{pair}")
    pairs = res.get("pairs") or ([res["pair"]] if res.get("pair") else [])
    if pairs and pairs[0].get("priceUsd"):
        return float(pairs[0]["priceUsd"])
    return None


def main():
    if not NOTION_TOKEN or not DATA_SOURCE_ID:
        sys.exit("Set NOTION_TOKEN and NOTION_DATA_SOURCE_ID.")

    rows = fetch_rows()
    print(f"Found {len(rows)} open crypto rows")

    # Auto-fill CoinGecko IDs from tickers where it's safe; write them back so
    # they're visible in Notion (and editable if the guess is ever wrong).
    needs_review = []
    for r in rows:
        if r["coingecko_id"] or r["dex_pair"] or not r["ticker"]:
            continue
        try:
            cid, reason = resolve_coingecko_id(r["ticker"], r["name"])
        except Exception as e:
            cid, reason = None, str(e)
        if not cid:
            needs_review.append(f"{r['name']}: {reason}")
            continue
        print(f"  Resolved {r['name']} ({r['ticker'].upper()}) -> {cid}")
        r["coingecko_id"] = cid
        if not DRY_RUN:
            notion("PATCH", f"/pages/{r['id']}", {
                "properties": {"CoinGecko ID": {"rich_text": [{"text": {"content": cid}}]}}
            })
        time.sleep(2)  # CoinGecko search is rate-limited on the free tier

    cg = coingecko_prices({r["coingecko_id"] for r in rows if r["coingecko_id"]})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    updated, skipped, failed = 0, [], []
    for r in rows:
        price, source = None, None
        try:
            if r["coingecko_id"]:
                price, source = cg.get(r["coingecko_id"]), "coingecko"
            elif r["dex_pair"]:
                price, source = dexscreener_price(r["dex_pair"]), "dexscreener"
            else:
                skipped.append(r["name"])
                continue

            if price is None:
                failed.append(f"{r['name']} (no price from {source})")
                continue

            print(f"  {r['name']:<24} ${price:,.8g}  [{source}]")
            if not DRY_RUN:
                notion("PATCH", f"/pages/{r['id']}", {
                    "properties": {
                        "Current Price": {"number": price},
                        "Price Updated": {"date": {"start": now}},
                    }
                })
                time.sleep(0.35)  # stay under Notion's ~3 req/s limit
            updated += 1
        except Exception as e:  # keep going if one row fails
            failed.append(f"{r['name']} ({e})")

    print(f"\nUpdated: {updated}{' (dry run)' if DRY_RUN else ''}")
    if needs_review:
        print("Needs a CoinGecko ID or DexScreener Pair set by hand:\n  " + "\n  ".join(needs_review))
    if skipped:
        print(f"Skipped (no Ticker, ID, or pair): {', '.join(skipped)}")
    if failed:
        print("Failed:\n  " + "\n  ".join(failed))
        sys.exit(1)  # makes the GitHub Actions run show red so you notice


if __name__ == "__main__":
    main()
