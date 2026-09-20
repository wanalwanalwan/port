"""
Sync live crypto prices into the Notion "Assets" database.

For every row where Class = Crypto and Status != Exited:
  - If "CoinGecko ID" is empty but "Ticker" is set, the ID is looked up
    automatically (top-500 coins, unambiguous matches only) and written back
  - If "CoinGecko ID" is set      -> price from CoinGecko (batched, one request)
  - Else if "DexScreener Pair" set -> price from DexScreener (format: chain/pairAddress)
  - Else                          -> skipped (logged)
Writes "Current Price" and "Price Updated".

Market metrics (see sync_metrics):
  - "Volume 24h" / "Volume WoW %" from CoinGecko for rows with a CoinGecko ID
  - "Holders" / "Holders WoW %" / "Top 10 %" from the chain explorer for rows
    with "Contract" set (format: chain/0xaddress, e.g. robinhood/0x2e8c...)
  Holder history is kept in holder_history.json (one snapshot per day) so
  WoW can be computed; the workflow commits that file back to the repo.

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
from datetime import datetime, timedelta, timezone

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
                "contract": plain_text(p.get("Contract")).strip("/").lower(),
                "old_price": (p.get("Current Price") or {}).get("number"),
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


def dexscreener_price(pair, ticker=""):
    """pair = 'chainId/pairAddress', as in https://dexscreener.com/<chainId>/<pairAddress>.
    Returns the USD price of the token matching ticker, whichever side of the pair it is on."""
    res = http("GET", f"https://api.dexscreener.com/latest/dex/pairs/{pair}")
    pairs = res.get("pairs") or ([res["pair"]] if res.get("pair") else [])
    if not pairs or not pairs[0].get("priceUsd"):
        return None
    p = pairs[0]
    base = (p.get("baseToken") or {}).get("symbol", "").lower()
    quote = (p.get("quoteToken") or {}).get("symbol", "").lower()
    base_usd = float(p["priceUsd"])
    if not ticker or base == ticker:
        return base_usd
    if quote == ticker and float(p.get("priceNative") or 0) > 0:
        # priceNative = base price in quote units, so quote USD = base USD / priceNative
        return base_usd / float(p["priceNative"])
    raise RuntimeError(f"ticker '{ticker}' not in pair ({base}/{quote})")


# ---------------------------------------------------------------------------
# Market metrics: volume WoW (CoinGecko) and holders / top-10 share (explorer)
# ---------------------------------------------------------------------------

HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "holder_history.json")

# Blockscout explorers by chain name used in the "Contract" property
EXPLORERS = {
    "robinhood": "https://robinhoodchain.blockscout.com",
}

# Never counted as holders in the top-10 share
BURN_ADDRESSES = {
    "0x" + "0" * 40,
    "0x" + "0" * 36 + "dead",
}


def coingecko_volume(cid):
    """(latest 24h volume, WoW change as a fraction).
    WoW = average rolling-24h volume over the last 7 days vs the 7 days before."""
    headers = {"accept": "application/json"}
    if COINGECKO_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_KEY
    qs = urllib.parse.urlencode({"vs_currency": "usd", "days": 14})
    res = http("GET", f"https://api.coingecko.com/api/v3/coins/{cid}/market_chart?{qs}", headers)
    vols = res.get("total_volumes") or []
    if not vols:
        return None, None
    latest_ts, latest = vols[-1]
    week_ms = 7 * 24 * 3600 * 1000
    this_week = [v for t, v in vols if t > latest_ts - week_ms]
    last_week = [v for t, v in vols if latest_ts - 2 * week_ms < t <= latest_ts - week_ms]
    wow = None
    if this_week and last_week and sum(last_week) > 0:
        wow = (sum(this_week) / len(this_week)) / (sum(last_week) / len(last_week)) - 1
    return latest, wow


def explorer_holders(contract):
    """contract = 'chain/0xaddress'. Returns (holder count, top-10 share as a fraction, excluded list).
    Top 10 skips burn addresses and verified/named contracts (LP pools, vaults),
    since those aren't people who can sell."""
    chain, _, address = contract.partition("/")
    base = EXPLORERS.get(chain)
    if not base or not address.startswith("0x"):
        raise RuntimeError(f"bad Contract '{contract}' (use chain/0xaddress; known chains: {', '.join(EXPLORERS)})")

    token = http("GET", f"{base}/api/v2/tokens/{address}")
    count = token.get("holders_count") or token.get("holders")
    count = int(count) if count is not None else None
    decimals = int(token.get("decimals") or 18)
    supply = int(token.get("total_supply") or 0) / 10 ** decimals

    top, excluded = [], []
    res = http("GET", f"{base}/api/v2/tokens/{address}/holders")
    for item in res.get("items", []):
        addr = item.get("address") or {}
        h = (addr.get("hash") or "").lower()
        amount = int(item.get("value") or 0) / 10 ** decimals
        label = "burn" if h in BURN_ADDRESSES else (addr.get("name") or h[:10])
        if h in BURN_ADDRESSES or (addr.get("is_contract") and (addr.get("is_verified") or addr.get("name"))):
            excluded.append(f"{label} ({amount / supply:.1%})" if supply else label)
            continue
        top.append(amount)
        if len(top) == 10:
            break
    share = sum(top) / supply if supply else None
    return count, share, excluded


def load_history():
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def holders_wow(history, key, count, today):
    """Compare today's count to the newest snapshot that is at least 7 days old."""
    snaps = history.get(key, {})
    cutoff = (datetime.fromisoformat(today) - timedelta(days=7)).date().isoformat()
    old_days = sorted(d for d in snaps if d <= cutoff)
    if not old_days or not snaps[old_days[-1]]:
        return None
    return count / snaps[old_days[-1]] - 1


def sync_metrics(rows):
    history = load_history()
    today = datetime.now(timezone.utc).date().isoformat()
    failed = []
    print("\nMarket metrics:")
    for r in rows:
        props = {}
        try:
            if r["coingecko_id"]:
                vol, vwow = coingecko_volume(r["coingecko_id"])
                time.sleep(2)  # CoinGecko free tier rate limit
                if vol is not None:
                    props["Volume 24h"] = {"number": round(vol, 2)}
                    props["Volume WoW %"] = {"number": round(vwow, 4) if vwow is not None else None}
            if r["contract"]:
                count, share, excluded = explorer_holders(r["contract"])
                if count is not None:
                    history.setdefault(r["contract"], {}).setdefault(today, count)  # first snapshot of the day
                    hwow = holders_wow(history, r["contract"], count, today)
                    props["Holders"] = {"number": count}
                    props["Holders WoW %"] = {"number": round(hwow, 4) if hwow is not None else None}
                if share is not None:
                    props["Top 10 %"] = {"number": round(share, 4)}
                if excluded:
                    print(f"    {r['name']}: top 10 excludes {', '.join(excluded)}")
        except Exception as e:
            failed.append(f"{r['name']} metrics ({e})")
        if props:
            shown = {k: v["number"] for k, v in props.items()}
            print(f"  {r['name']:<24} {shown}")
            if not DRY_RUN:
                notion("PATCH", f"/pages/{r['id']}", {"properties": props})
                time.sleep(0.35)

    if not DRY_RUN:
        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2, sort_keys=True)
            f.write("\n")
    return failed


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
            # An explicit DexScreener pair wins, so a row can use CoinGecko for
            # volume stats while keeping its on-chain pool price.
            if r["dex_pair"]:
                price, source = dexscreener_price(r["dex_pair"], r["ticker"]), "dexscreener"
            elif r["coingecko_id"]:
                price, source = cg.get(r["coingecko_id"]), "coingecko"
            else:
                skipped.append(r["name"])
                continue

            if price is None:
                failed.append(f"{r['name']} (no price from {source})")
                continue

            # Sanity check: refuse a price more than 3x away from the last one (bad data)
            old = r["old_price"]
            if old and (price > old * 3 or price < old / 3):
                failed.append(f"{r['name']} (suspicious price ${price:,.8g} vs last ${old:,.8g}, not written)")
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

    try:
        failed += sync_metrics(rows)
    except Exception as e:
        failed.append(f"metrics ({e})")

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
