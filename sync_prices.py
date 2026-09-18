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
    or (None, reason)
