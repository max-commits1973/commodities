#!/usr/bin/env python3
"""
1939 Dashboard collector.

Pulls daily history + the latest quote for each market from Yahoo Finance,
resamples to weekly bars (with the in-progress week kept "live" so the weekly
indicator moves intra-week), computes the 1939 oscillator faithfully, and
writes a single market_data.json the dashboard reads.

Design notes
------------
* The cumulative summation (ta.cum) makes the indicator's absolute level
  history-dependent, so we anchor every run to a FIXED start date. The
  baseline is therefore stable run-to-run instead of drifting as a rolling
  window slides.
* Each symbol is isolated: one failure never sinks the whole run. A failed
  symbol is marked status="error" and the dashboard greys it out.
"""

import argparse, json, os, sys, time, math
from datetime import datetime, timezone
import requests

# --------------------------------------------------------------------------
# Universe (15 markets, all on free Yahoo data).
# LME nickel / lead / zinc are intentionally excluded -- no free feed exists.
# Aluminium below is the CME (COMEX) contract, a close proxy for LME aluminium.
# --------------------------------------------------------------------------
UNIVERSE = {
    "Energy": [
        ("Brent Crude", "BZ=F"),
        ("Natural Gas", "NG=F"),
    ],
    "Softs": [
        ("Soybeans", "ZS=F"),
        ("Sugar",    "SB=F"),
        ("Cocoa",    "CC=F"),
        ("Coffee",   "KC=F"),
        ("Cotton",   "CT=F"),
        ("Corn",     "ZC=F"),
        ("Wheat",    "ZW=F"),
    ],
    "Metals": [
        ("Gold",      "GC=F"),
        ("Silver",    "SI=F"),
        ("Copper",    "HG=F"),
        ("Platinum",  "PL=F"),
        ("Palladium", "PA=F"),
        ("Aluminium", "ALI=F"),
    ],
}

# Indicator parameters (match the PineScript inputs).
FAST, SLOW, ATR_LEN, SCALE, SIG_LEN = 19, 39, 14, 100.0, 9

# Fixed history anchor -> stable cumulative baseline. 2016-01-01 UTC.
ANCHOR = 1451606400
KEEP_WEEKS = 104          # weekly points kept for charts (~2y)

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}
BASES = ["https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"]


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------
def fetch_chart(ticker, retries=3):
    """Return (daily_bars, meta). daily_bars = [(ts,o,h,l,c), ...]."""
    last_err = None
    for attempt in range(retries):
        base = BASES[attempt % len(BASES)]
        url = f"{base}/v8/finance/chart/{ticker}"
        params = {"period1": ANCHOR, "period2": int(time.time()),
                  "interval": "1d", "includePrePost": "false"}
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
            ts = res.get("timestamp", []) or []
            q = res["indicators"]["quote"][0]
            bars = []
            for i, t in enumerate(ts):
                o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
                if None in (o, h, l, c):
                    continue
                bars.append((t, o, h, l, c))
            return bars, res.get("meta", {})
        except Exception as e:                       # noqa: BLE001
            last_err = e
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"fetch failed for {ticker}: {last_err}")


# --------------------------------------------------------------------------
# Weekly resample with a LIVE in-progress week
# --------------------------------------------------------------------------
def to_weekly(daily, meta):
    buckets, order = {}, []
    for t, o, h, l, c in daily:
        iso = datetime.fromtimestamp(t, tz=timezone.utc).isocalendar()
        key = (iso[0], iso[1])
        if key not in buckets:
            buckets[key] = {"o": o, "h": h, "l": l, "c": c, "t": t}
            order.append(key)
        else:
            b = buckets[key]
            b["h"] = max(b["h"], h); b["l"] = min(b["l"], l)
            b["c"] = c; b["t"] = t

    bars = [buckets[k] for k in order]

    # Overlay the latest quote onto the forming (current) week so the weekly
    # bar -- and therefore the indicator -- updates intra-week on each run.
    price = meta.get("regularMarketPrice")
    if price is not None and bars:
        now_iso = datetime.now(timezone.utc).isocalendar()
        last_iso = datetime.fromtimestamp(bars[-1]["t"], tz=timezone.utc).isocalendar()
        if (now_iso[0], now_iso[1]) != (last_iso[0], last_iso[1]):
            # New week with no daily bar yet -> seed a fresh forming bar.
            bars.append({"o": bars[-1]["c"], "h": price, "l": price,
                         "c": price, "t": int(time.time())})
        else:
            b = bars[-1]
            b["c"] = price
            b["h"] = max(b["h"], price); b["l"] = min(b["l"], price)
    return bars


# --------------------------------------------------------------------------
# Pine-faithful indicator
# --------------------------------------------------------------------------
def rma(values, length):
    out = [None] * len(values)
    if len(values) < length:
        return out
    out[length - 1] = sum(values[:length]) / length
    a = 1.0 / length
    for i in range(length, len(values)):
        out[i] = a * values[i] + (1 - a) * out[i - 1]
    return out


def ema(values, length):
    out, prev, a = [None] * len(values), None, 2.0 / (length + 1)
    for i, v in enumerate(values):
        if v is None:
            out[i] = None; continue
        prev = v if prev is None else a * v + (1 - a) * prev
        out[i] = prev
    return out


def compute_1939(bars):
    n = len(bars)
    H = [b["h"] for b in bars]; L = [b["l"] for b in bars]; C = [b["c"] for b in bars]
    tr = [H[0] - L[0]] + [
        max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1])) for i in range(1, n)
    ]
    atr = rma(tr, ATR_LEN)
    nchg = [0.0] * n
    for i in range(1, n):
        if atr[i]:
            nchg[i] = (C[i] - C[i - 1]) / atr[i] * SCALE
    ef, es = ema(nchg, FAST), ema(nchg, SLOW)
    spread = [(ef[i] - es[i]) if (ef[i] is not None and es[i] is not None) else 0.0
              for i in range(n)]
    summation, run = [], 0.0
    for s in spread:
        run += s; summation.append(run)
    signal = ema(summation, SIG_LEN)
    return summation, signal


# --------------------------------------------------------------------------
# Build one market record
# --------------------------------------------------------------------------
def build_item(name, ticker):
    daily, meta = fetch_chart(ticker)
    if len(daily) < 60:
        raise RuntimeError(f"insufficient history ({len(daily)} bars)")
    weekly = to_weekly(daily, meta)
    summ, sig = compute_1939(weekly)

    wk = weekly[-KEEP_WEEKS:]
    su = summ[-KEEP_WEEKS:]
    si = sig[-KEEP_WEEKS:]
    price = meta.get("regularMarketPrice") or daily[-1][4]
    # True 1-day change: latest price vs the prior completed daily settle.
    prev = daily[-2][4] if len(daily) > 1 else price
    return {
        "name": name, "ticker": ticker,
        "price": round(price, 4),
        "prevClose": round(prev, 4),
        "currency": meta.get("currency", ""),
        "marketTime": meta.get("regularMarketTime"),
        "ohlc": [[round(b["o"], 4), round(b["h"], 4), round(b["l"], 4), round(b["c"], 4)] for b in wk],
        "summation": [round(x, 3) for x in su],
        "signal": [round(x, 3) if x is not None else None for x in si],
        "weeks": len(weekly),
        "status": "ok",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/market_data.json")
    args = ap.parse_args()

    dataset = {"generated": datetime.now(timezone.utc).isoformat(),
               "source": "yahoo", "groups": []}
    ok, fail = 0, 0
    for group, items in UNIVERSE.items():
        g = {"name": group, "items": []}
        for name, ticker in items:
            try:
                g["items"].append(build_item(name, ticker))
                ok += 1
                it = g["items"][-1]
                print(f"OK   {name:11s} {ticker:7s} px={it['price']:<10} "
                      f"msi={it['summation'][-1]:.1f}")
            except Exception as e:                    # noqa: BLE001
                fail += 1
                g["items"].append({"name": name, "ticker": ticker,
                                   "status": "error", "error": str(e)})
                print(f"FAIL {name:11s} {ticker:7s} {e}", file=sys.stderr)
            time.sleep(0.35)
        dataset["groups"].append(g)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(dataset, f, separators=(",", ":"))
    print(f"\nWrote {args.out}  ({ok} ok, {fail} failed)")
    # Fail the CI run only if *everything* broke (keeps last good deploy live).
    if ok == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
