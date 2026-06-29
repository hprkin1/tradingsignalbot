"""
MEXC USDT-perpetual ARMED setup scanner.

Looks for coins that are about to move, not coins that already moved:
volatility-compressed + leverage building on the short side + signs of
absorption (high volume on a flat price).

Fires when ALL of the following hold on the most recently CLOSED 15m bar
AND the previous bar did NOT also satisfy them (dedupe via state transition):

  1. Bollinger Band Width at the lowest level in the prior 96 bars (24h squeeze)
  2. Funding rate < 0 (shorts paying longs — leverage fuel for upside)
  3. Volume absorption: last 8 bars total volume > 1.5x average prior 8-bar
     window volume, AND the 8-bar price range is under 2% of price
  4. 24h price change not catastrophically negative (-10% floor)
  5. Liquidity floor: 24h quote volume >= $1M
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import ccxt
import numpy as np
import pandas as pd
import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

BB_PERIOD = 20
BBW_LOOKBACK = 96                    # 24h on 15m
ABSORPTION_LOOKBACK = 8              # last N bars for absorption check
ABSORPTION_VOL_MULT = 1.5            # last 8-bar vol > X * avg prior 8-bar window
ABSORPTION_RANGE_MAX_PCT = 0.02      # 2% max range over last 8 bars
MIN_24H_PCT = -10.0
MIN_24H_QV_USD = 1_000_000
WORKERS = 4
PER_REQ_TIMEOUT_MS = 10_000


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch_15m(exchange, symbol):
    try:
        bars = exchange.fetch_ohlcv(symbol, "15m", limit=200)
        if not bars or len(bars) < BBW_LOOKBACK + BB_PERIOD + 4:
            return symbol, None
        df = pd.DataFrame(bars, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return symbol, df
    except Exception as e:
        log(f"  ohlcv error {symbol}: {type(e).__name__}")
        return symbol, None


def compute_bbw(df: pd.DataFrame) -> pd.Series:
    sma = df["close"].rolling(BB_PERIOD).mean()
    std = df["close"].rolling(BB_PERIOD).std()
    sma_safe = sma.replace(0, np.nan)
    return (4 * std) / sma_safe   # (upper - lower) / sma  where bands = sma ± 2σ


def armed_at(df: pd.DataFrame, bbw: pd.Series, idx: int,
             funding_rate: float, pct_24h: float) -> bool:
    """Check whether the bar at `idx` (negative index from end) meets all ARMED conditions."""
    # 1. BBW at min of PRIOR 96 bars (excluding current)
    prior_bbw = bbw.iloc[idx - BBW_LOOKBACK: idx].dropna()
    if len(prior_bbw) < int(BBW_LOOKBACK * 0.8):
        return False
    cur_bbw = bbw.iloc[idx]
    if pd.isna(cur_bbw) or cur_bbw > prior_bbw.min():
        return False

    # 2. Funding < 0
    if funding_rate is None or funding_rate >= 0:
        return False

    # 3. Volume absorption
    recent = df.iloc[idx - ABSORPTION_LOOKBACK + 1: idx + 1]
    recent_vol = recent["volume"].sum()
    prior = df.iloc[idx - BBW_LOOKBACK: idx - ABSORPTION_LOOKBACK + 1]
    prior_8sum = prior["volume"].rolling(ABSORPTION_LOOKBACK).sum()
    avg_8sum = prior_8sum.mean()
    if pd.isna(avg_8sum) or avg_8sum <= 0:
        return False
    if recent_vol < ABSORPTION_VOL_MULT * avg_8sum:
        return False
    mid = df["close"].iloc[idx]
    if mid <= 0:
        return False
    rng = recent["high"].max() - recent["low"].min()
    if rng / mid > ABSORPTION_RANGE_MAX_PCT:
        return False

    # 4. Not in deep drawdown
    if pct_24h is None or pct_24h < MIN_24H_PCT:
        return False

    return True


def check_armed(df, funding_rate, pct_24h):
    if df is None or len(df) < BBW_LOOKBACK + BB_PERIOD + 4:
        return None
    bbw = compute_bbw(df)

    current = armed_at(df, bbw, -2, funding_rate, pct_24h)
    if not current:
        return None
    previous = armed_at(df, bbw, -3, funding_rate, pct_24h)
    if previous:
        return None  # dedupe — already armed last bar

    last = df.iloc[-2]
    recent = df.iloc[-ABSORPTION_LOOKBACK - 1: -1]
    recent_vol_q = (recent["volume"] * recent["close"]).sum()
    rng_pct = (recent["high"].max() - recent["low"].min()) / last["close"] * 100
    return {
        "close": float(last["close"]),
        "bbw_current": float(bbw.iloc[-2]),
        "funding_rate_pct": float(funding_rate * 100),
        "absorption_vol_quote_usd": float(recent_vol_q),
        "absorption_range_pct": float(rng_pct),
        "pct_24h": float(pct_24h),
        "bar_time_utc": str(last["ts"]),
    }


def mexc_futures_link(symbol: str) -> str:
    base = symbol.split("/")[0]
    return f"https://www.mexc.com/futures/{base}_USDT?type=linear_swap"


def send_telegram(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }, timeout=10)
        if not r.ok:
            log(f"  telegram error {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        log(f"  telegram exception: {e}")
        return False


def main():
    log(f"=== precursor scan starting {pd.Timestamp.now(tz='UTC')} ===")

    mx = ccxt.mexc({
        "options": {"defaultType": "swap"},
        "enableRateLimit": True,
        "timeout": PER_REQ_TIMEOUT_MS,
    })
    markets = mx.load_markets()
    perps = [s for s, m in markets.items()
             if m.get("swap") and m.get("settle") == "USDT" and m.get("active")]
    log(f"active USDT perps: {len(perps)}")

    tickers = mx.fetch_tickers(perps)

    candidates = []
    for sym, t in tickers.items():
        qv = t.get("quoteVolume")
        pct = t.get("percentage")
        funding = t.get("fundingRate")
        if funding is None:
            funding = (t.get("info") or {}).get("fundingRate")
        try:
            funding = float(funding) if funding is not None else None
        except (TypeError, ValueError):
            funding = None
        if qv is None or qv < MIN_24H_QV_USD:
            continue
        if pct is None or pct < MIN_24H_PCT:
            continue
        if funding is None or funding >= 0:
            continue
        candidates.append((sym, funding, pct))
    log(f"candidates after liquidity + funding<0 + 24h>{MIN_24H_PCT}%: {len(candidates)}")

    if not candidates:
        log("no candidates; done.")
        return

    cand_map = {c[0]: (c[1], c[2]) for c in candidates}
    signals = []
    t0 = time.time()
    done_n = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_15m, mx, s): s for s, _, _ in candidates}
        for f in as_completed(futs):
            done_n += 1
            if done_n % 25 == 0:
                log(f"  {done_n}/{len(candidates)} (elapsed {time.time()-t0:.0f}s)")
            sym, df = f.result()
            if df is None:
                continue
            funding, pct = cand_map[sym]
            sig = check_armed(df, funding, pct)
            if sig:
                sig["symbol"] = sym
                signals.append(sig)

    log(f"ARMED signals fired: {len(signals)}")
    if not signals:
        return

    for s in signals:
        base = s["symbol"].split("/")[0]
        link = mexc_futures_link(s["symbol"])
        msg = (
            f"⚡ *{base}/USDT* ARMED setup\n"
            f"Price: `{s['close']:.6g}`\n"
            f"24h: `{s['pct_24h']:+.1f}%`\n"
            f"Funding (8h): `{s['funding_rate_pct']:+.4f}%`  _shorts paying_\n"
            f"BBW: `{s['bbw_current']:.4f}` at 96-bar low\n"
            f"Absorption: 8-bar range `{s['absorption_range_pct']:.2f}%` on `${s['absorption_vol_quote_usd']:,.0f}`\n"
            f"Bar close: `{s['bar_time_utc']}`\n"
            f"[Open in MEXC futures]({link})"
        )
        ok = send_telegram(msg)
        log(f"  {'sent' if ok else 'FAILED'}: {s['symbol']}")


if __name__ == "__main__":
    main()
