# Massive API Reference (formerly Polygon.io)

_Research notes and integration guide for the Simulated Trading project. Scope: retrieving **realtime** and **end-of-day** prices for **multiple tickers** from Massive's Stocks REST API._

> **Rebrand note.** Polygon.io rebranded to **Massive** on 2025-10-30. The primary host is now `https://api.massive.com`; the legacy host `https://api.polygon.io` still resolves and serves the same responses with the same keys. Docs moved from `polygon.io/docs` to `https://massive.com/docs`. Existing API keys are unchanged. This project targets `api.massive.com` and treats `api.polygon.io` as a fallback base URL.

---

## 1. Authentication

The API key is passed one of two ways (either is accepted on every endpoint):

| Method | Example |
|---|---|
| Query parameter | `GET /v2/...?apiKey=YOUR_KEY` |
| Bearer header | `Authorization: Bearer YOUR_KEY` |

**This project uses the `Authorization: Bearer` header** so the key never lands in logs or URLs. The key comes from the `MASSIVE_API_KEY` environment variable (see `planning/PLAN.md` §5). Keys are created/managed at `https://massive.com/dashboard/keys`.

```python
import httpx

BASE_URL = "https://api.massive.com"
client = httpx.AsyncClient(
    base_url=BASE_URL,
    headers={"Authorization": f"Bearer {MASSIVE_API_KEY}"},
    timeout=httpx.Timeout(10.0, connect=5.0),
)
```

---

## 2. Rate limits

| Plan | REST request limit | Data recency | History |
|---|---|---|---|
| Basic (free) | **5 requests / minute** | End-of-day only | 2 years |
| Starter / Developer | Unbounded (soft) | **15-minute delayed** | 5 years |
| Advanced | Unbounded (soft) | **Real-time** | 10 years |
| Business | Unbounded (soft) | Real-time + FMV | All |

- "Unbounded (soft)" = no hard per-minute cap, but Massive throttles sustained bursts. Keep total request rate **under ~100 req/s**.
- The free tier's 5 req/min is the binding constraint for this project. The whole design goal is **one request per poll for the entire watchlist** (see §3), which at a 15 s interval = 4 req/min, inside the free budget.
- On a `429`, back off (Massive returns `Retry-After` on some tiers; if absent, use exponential backoff starting at the poll interval).

---

## 3. Realtime prices for multiple tickers — **Full Market Snapshot**

**This is the primary endpoint for the project's price feed.** One request returns the current market state for a comma-separated list of tickers (or the entire market if omitted).

```
GET /v2/snapshot/locale/us/markets/stocks/tickers
```

### Query parameters

| Param | Type | Notes |
|---|---|---|
| `tickers` | string | Case-sensitive, comma-separated, e.g. `AAPL,TSLA,GOOGL`. Omit to get all ~10,000 tickers. |
| `include_otc` | boolean | Include OTC securities. Default `false`. |

### Response shape

```json
{
  "status": "OK",
  "count": 3,
  "tickers": [
    {
      "ticker": "AAPL",
      "todaysChange": 1.23,
      "todaysChangePerc": 0.65,
      "updated": 1739210400000000000,
      "day":     { "o": 189.5, "h": 191.2, "l": 188.9, "c": 190.7, "v": 41234567, "vw": 190.1 },
      "min":     { "t": 1739210400000, "o": 190.6, "h": 190.8, "l": 190.5, "c": 190.72, "v": 12345, "vw": 190.7, "n": 88, "av": 41246912 },
      "prevDay": { "o": 187.1, "h": 189.9, "l": 186.8, "c": 189.47, "v": 55555555, "vw": 188.3 },
      "lastTrade": { "p": 190.72, "s": 100, "t": 1739210399900000000, "x": 11, "i": "12345", "c": [12, 37] },
      "lastQuote": { "P": 190.74, "S": 2, "p": 190.71, "s": 3, "t": 1739210399950000000 }
    }
  ]
}
```

| Object | Field | Meaning |
|---|---|---|
| `day` | `o/h/l/c/v/vw` | Today's cumulative daily bar (open, high, low, **last close-so-far**, volume, VWAP). `c` is the most recent price of the day. |
| `min` | `o/h/l/c/v/n` | The current (or most recent) 1-minute bar. `c` is the minute close. |
| `prevDay` | `o/h/l/c/v/vw` | The **previous trading day's** completed daily bar. `prevDay.c` is the official previous close. |
| `lastTrade` | `p`, `s`, `t`, `x` | Last trade price, size, timestamp (**nanoseconds**), exchange id. Real-time plans only. |
| `lastQuote` | `P/S`, `p/s`, `t` | NBBO ask price/size, bid price/size, timestamp (**nanoseconds**). Real-time plans only. |
| top level | `todaysChange`, `todaysChangePerc` | Change vs `prevDay.c`. |
| top level | `updated` | Last-update timestamp, **nanoseconds** since epoch. |

### Which field is "the price"?

Prefer the freshest non-zero value, in this order:

1. `lastTrade.p` — real-time plans, populated during market hours
2. `min.c` — falls back to the minute bar when there is no fresh trade
3. `day.c` — daily cumulative close-so-far
4. `prevDay.c` — market closed, or ticker hasn't traded today

```python
def extract_price(t: dict) -> float | None:
    for path in (("lastTrade", "p"), ("min", "c"), ("day", "c"), ("prevDay", "c")):
        v = t.get(path[0], {}).get(path[1])
        if v:                       # skips both None and 0.0
            return float(v)
    return None
```

> **Snapshot reset window.** Massive clears snapshot data daily at **03:30 EST** and it repopulates from **~04:00 EST** as exchanges report. Between those times `day`/`min`/`lastTrade` may be `0`; `prevDay.c` still carries the last close, so the fallback chain above keeps returning a usable price.

> **Plan availability.** The Full Market Snapshot is available on **Starter → Advanced** and **Business** plans. On the free **Basic** plan it may return `403 NOT_AUTHORIZED`. For Basic keys, use the EOD path (§5) or, more practically for this project, fall back to the simulator.

### Example — poll the whole watchlist in one call

```python
async def fetch_snapshot(client: httpx.AsyncClient, tickers: list[str]) -> dict[str, float]:
    resp = await client.get(
        "/v2/snapshot/locale/us/markets/stocks/tickers",
        params={"tickers": ",".join(tickers)},
    )
    resp.raise_for_status()
    body = resp.json()
    out: dict[str, float] = {}
    for t in body.get("tickers", []):
        price = extract_price(t)
        if price is not None:
            out[t["ticker"]] = price
    return out
```

---

## 4. Single ticker snapshot (used for ticker validation)

```
GET /v2/snapshot/locale/us/markets/stocks/tickers/{stocksTicker}
```

Same per-ticker object as §3, wrapped as `{ "status": "OK", "ticker": { ... } }`.

Used by `POST /api/watchlist` to validate a new symbol: if the response is `404`, or `status` is `NOT_FOUND`, or no price can be extracted, the ticker is rejected with `400 unknown_ticker`.

```python
async def ticker_exists(client: httpx.AsyncClient, ticker: str) -> bool:
    resp = await client.get(
        f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker.upper()}"
    )
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    body = resp.json()
    return extract_price(body.get("ticker", {})) is not None
```

> A cheaper alternative that works on **all** plans (including Basic) is the Previous Day Bar (§5.1): a `resultsCount` of `0` means the symbol is unknown.

---

## 5. End-of-day prices for multiple tickers

### 5.1 Previous Day Bar — one ticker

```
GET /v2/aggs/ticker/{stocksTicker}/prev?adjusted=true
```

```json
{
  "ticker": "AAPL", "adjusted": true, "status": "OK", "queryCount": 1, "resultsCount": 1,
  "results": [
    { "T": "AAPL", "o": 115.55, "h": 117.59, "l": 114.13, "c": 115.97,
      "v": 131704427, "vw": 116.3058, "t": 1605042000000, "n": 1 }
  ]
}
```

| Field | Meaning |
|---|---|
| `results[0].c` | Previous trading day's close (the EOD price) |
| `results[0].o/h/l` | Prev day open / high / low |
| `results[0].v` | Prev day volume |
| `results[0].t` | Bar timestamp, **milliseconds** since epoch |
| `results[0].n` | Trade count |

Available on **all** plans. One request per ticker — for a 10-to-30 name watchlist on the free tier (5 req/min) this needs batching/pacing; prefer §5.2.

### 5.2 Grouped Daily Bars — the whole market for one day (**preferred EOD path**)

```
GET /v2/aggs/grouped/locale/us/market/stocks/{date}?adjusted=true
```

| Param | Type | Notes |
|---|---|---|
| `date` (path) | string | `YYYY-MM-DD` trading day |
| `adjusted` | boolean | Split-adjusted. Default `true`. |
| `include_otc` | boolean | Default `false`. |

```json
{
  "status": "OK", "adjusted": true, "queryCount": 9432, "resultsCount": 9432,
  "results": [
    { "T": "AAPL", "o": 187.1, "h": 189.9, "l": 186.8, "c": 189.47, "v": 55555555, "vw": 188.3, "t": 1739142000000, "n": 812345 },
    { "T": "MSFT", "o": 402.0, "h": 407.2, "l": 401.1, "c": 406.8,  "v": 21212121, "vw": 404.9, "t": 1739142000000, "n": 512345 }
  ]
}
```

**One request returns every US ticker's completed daily bar.** Filter client-side to the watchlist. This is the efficient EOD path for the free tier: one call covers any number of tickers.

```python
async def fetch_eod(client: httpx.AsyncClient, date: str, wanted: set[str]) -> dict[str, float]:
    resp = await client.get(
        f"/v2/aggs/grouped/locale/us/market/stocks/{date}",
        params={"adjusted": "true"},
    )
    resp.raise_for_status()
    return {
        r["T"]: float(r["c"])
        for r in resp.json().get("results", [])
        if r["T"] in wanted and r.get("c")
    }
```

> Weekends/holidays return `resultsCount: 0`. Walk back day-by-day to the last trading day if needed.

### 5.3 Daily Ticker Summary (open/close, with pre/after-hours) — one ticker

```
GET /v1/open-close/{stocksTicker}/{date}?adjusted=true
```

```json
{
  "status": "OK", "symbol": "AAPL", "from": "2023-01-09",
  "open": 324.66, "high": 326.2, "low": 322.3, "close": 325.12,
  "preMarket": 324.5, "afterHours": 322.1, "volume": 26122646
}
```

Handy when pre/after-hours prints matter. Not used by this project's core feed; documented for completeness.

---

## 6. Historical intraday bars — Custom Bars (Aggregates)

Not required by the project (charts are session-scoped, see PLAN Appendix A/D2), but documented so the option is understood.

```
GET /v2/aggs/ticker/{stocksTicker}/range/{multiplier}/{timespan}/{from}/{to}
```

| Path param | Example | Notes |
|---|---|---|
| `multiplier` | `1`, `5`, `15` | Size of the timespan multiple |
| `timespan` | `minute`, `hour`, `day`, `week` | Bar granularity |
| `from` / `to` | `2023-01-01` or ms timestamp | Inclusive range |

| Query param | Notes |
|---|---|
| `adjusted` | Default `true` |
| `sort` | `asc` (default oldest-first) or `desc` |
| `limit` | Max `50000`, default `5000`. Use `next_url` for pagination. |

```json
{
  "ticker": "AAPL", "adjusted": true, "queryCount": 2, "resultsCount": 2, "status": "OK",
  "results": [
    { "o": 74.06, "h": 75.15, "l": 73.7975, "c": 75.0875, "v": 135647456, "vw": 74.6099, "t": 1577941200000, "n": 1 }
  ],
  "next_url": "https://api.massive.com/v2/aggs/ticker/AAPL/range/1/day/..."
}
```

---

## 7. Other realtime endpoints (reference)

| Purpose | Endpoint |
|---|---|
| Last trade (one ticker) | `GET /v2/last/trade/{stocksTicker}` → `{ "results": { "p", "s", "t", "x" } }` |
| Last quote (one ticker) | `GET /v2/last/nbbo/{stocksTicker}` → `{ "results": { "P","S","p","s","t" } }` |
| Top 20 gainers / losers | `GET /v2/snapshot/locale/us/markets/stocks/{direction}` where `direction ∈ {gainers, losers}` |
| Unified multi-asset snapshot | `GET /v3/snapshot?ticker.any_of=AAPL,TSLA` (stocks/options/fx/crypto in one call) |

The per-ticker last-trade/last-quote endpoints cost one request each and don't scale to a watchlist on the free tier — the Full Market Snapshot (§3) is strictly better for this project.

---

## 8. Timestamps and units

| Source | Unit |
|---|---|
| Snapshot `updated`, `lastTrade.t`, `lastQuote.t`, `min.t` | **nanoseconds** since Unix epoch |
| Aggregates / grouped / prev `results[].t` | **milliseconds** since Unix epoch |
| `open-close` `from` | date string `YYYY-MM-DD` |

Normalize everything to timezone-aware UTC `datetime` at the edge:

```python
from datetime import datetime, timezone

def ns_to_dt(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)

def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1_000, tz=timezone.utc)
```

The project emits ISO-8601 with a `Z` suffix (`dt.isoformat().replace("+00:00", "Z")`).

---

## 9. Error handling

| Status | Meaning | Project handling |
|---|---|---|
| `200` with `status: "OK"` / `"DELAYED"` | Success (`DELAYED` = 15-min tier) | Use results |
| `200` with `resultsCount: 0` | No data for the symbol/date | Treat as unknown ticker (validation) or "no update" (poll) |
| `401` / `403` | Bad key, or endpoint not in plan | Log once, fall back to simulator; surface in `/api/health` |
| `404` | Unknown ticker (snapshot single) | `400 unknown_ticker` on watchlist add |
| `429` | Rate limited | Exponential backoff from the poll interval; skip this cycle |
| `5xx` | Massive outage | Keep serving last cached prices; mark feed stale in `/api/health` |

A ticker that resolved before but returns no data on a later poll is **kept**, and the price cache keeps streaming its last known value (per PLAN §6).

---

## 10. Official Python client (alternative to raw HTTP)

```bash
pip install -U massive         # new package name
# or:  pip install -U polygon-api-client   # legacy alias, identical code
```

```python
from massive import RESTClient

client = RESTClient(api_key=MASSIVE_API_KEY)      # defaults to api.massive.com

# Full market snapshot for specific tickers
snap = client.get_snapshot_all("stocks", tickers=["AAPL", "TSLA", "GOOGL"])
for t in snap:
    print(t.ticker, t.last_trade.price if t.last_trade else t.day.close)

# Previous close
prev = client.get_previous_close_agg("AAPL")

# Grouped daily bars (whole market, one day)
grouped = client.get_grouped_daily_aggs("2026-09-09")

# Intraday history
for bar in client.list_aggs("AAPL", 1, "minute", "2026-09-01", "2026-09-09", limit=50000):
    print(bar.close)
```

**This project uses raw `httpx` instead of the client library** because:

- The uv backend already depends on `httpx`; the SDK adds a transitive dependency tree for two endpoints.
- We need fine control over the async poll loop, timeouts, and backoff shared with the simulator code path.
- The response subset we consume is tiny and stable.

The SDK is the better choice if the project later needs WebSocket streaming, pagination helpers, or the options/indices/crypto surfaces.

---

## 11. What this project actually calls

| Need | Endpoint | Frequency |
|---|---|---|
| Live prices for all watched tickers | `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=…` | Every 2–15 s (tier-dependent), one request |
| Validate a newly added ticker | `GET /v2/snapshot/locale/us/markets/stocks/tickers/{ticker}` (or `/v2/aggs/ticker/{ticker}/prev` on Basic) | On `POST /api/watchlist` |
| EOD backfill (optional / not in core scope) | `GET /v2/aggs/grouped/locale/us/market/stocks/{date}` | On demand, one request |

Polling interval by tier (from PLAN §6):

| Tier | Interval |
|---|---|
| Free / Basic (5 req/min) | 15 s |
| Starter / Developer | 5–15 s |
| Advanced / Business | 2–5 s |

---

## Sources

- [Stocks REST API — Overview](https://massive.com/docs/rest/stocks/overview)
- [Full Market Snapshot](https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot)
- [Single Ticker Snapshot](https://massive.com/docs/rest/stocks/snapshots/single-ticker-snapshot)
- [Previous Day Bar (OHLC)](https://massive.com/docs/rest/stocks/aggregates/previous-day-bar)
- [Custom Bars (OHLC)](https://massive.com/docs/rest/stocks/aggregates/custom-bars)
- [Daily Market Summary / Grouped Daily](https://massive.com/docs/rest/stocks/aggregates/daily-market-summary)
- [Daily Ticker Summary (open/close)](https://massive.com/docs/rest/stocks/aggregates/daily-ticker-summary)
- [KB: latest trades, quotes and aggregates in one request](https://massive.com/knowledge-base/article/does-massive-have-an-endpoint-that-returns-the-latest-trades-quotes-and-aggregates-in-one-request)
- [KB: request limit for Massive's RESTful APIs](https://polygon.io/knowledge-base/article/what-is-the-request-limit-for-polygons-restful-apis)
- [Official Python client (`massive-com/client-python`)](https://github.com/massive-com/client-python)
- [`massive` on PyPI](https://pypi.org/project/massive/)
