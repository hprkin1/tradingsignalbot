"""
MEXC USDT-perpetual breakout scanner with Telegram alerts.

Runs on a 15m schedule (GitHub Actions). Reads only public endpoints —
no MEXC API key required. Posts matches to Telegram with a direct link
to the futures pair on MEXC.

Filter (all must pass on the most recently CLOSED 15m bar):
  1. 24h price change >= +5%
  2. Last 15m bar quote volume >= $500k USDT
  3. Last bar volume > previous bar volume
  4. Last bar volume > 2 * 20-bar avg volume
  5. Close > rolling 96-bar high (24h breakout)
  6. 4-bar ROC > 0 AND accelerating (ROC_now > ROC_prev)
  7. Dedupe: previous bar was NOT already above the 24h high
            (so we alert on the breakout candle, not every candle after)
"""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import ccxt
import numpy as np
import pandas as pd
import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

MIN_24H_PCT = 5.0
MIN_BAR_QUOTE_VOL_USD = 500_000
VOL_VS_20BAR_MULT = 2.0
BREAKOUT_LOOKBACK = 96            # 24h of 15m bars
MOMENTUM_LOOKBACK = 4
MIN_24H_QV_PREFILTER = 500_000    # liquidity floor before deep-fetching OHLCV
WORKERS = 4                       # conservative — MEXC rate-limits aggressively


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch_15m(exchange, symbol):
    try:
        bars = exchange.fetch_ohlcv(symbol, "15m", limit=120)
        if not bars or len(bars) < 100:
            return symbol, None
        df = pd.DataFrame(bars, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return symbol, df
    except Exception as e:
        log(f"  ohlcv error {symbol}: {type(e).__name__}")
        return symbol, None


def check_signal(df: pd.DataFrame) -> dict | None:
    """Return signal details if all conditions pass on the last CLOSED bar, else None.

    We use iloc[-2] as the "last closed" bar to avoid the still-forming candle.
    """
    if df is None or len(df) < BREAKOUT_LOOKBACK + 4:
        return None

    last = df.iloc[-2]
    prev = df.iloc[-3]

    # 96-bar prior high (highs of the 96 bars immediately before `last`)
    prior_window = df.iloc[-(BREAKOUT_LOOKBACK + 2):-2]
    prior_high = prior_window["high"].max()

    # Cond 5 + dedupe (cond 7): breakout candle, not continuation
    if last["close"] <= prior_high:
        return None
    if prev["close"] > prior_high:
        return None

    # Cond 2: quote volume on last bar
    last_qv = last["volume"] * last["close"]
    if last_qv < MIN_BAR_QUOTE_VOL_USD:
        return None

    # Cond 3: last vol > prev vol
    if last["volume"] <= prev["volume"]:
        return None

    # Cond 4: last vol > 2 * 20-bar avg (excluding the breakout bar itself)
    vol20_avg = df["volume"].iloc[-22:-2].mean()
    if last["volume"] < VOL_VS_20BAR_MULT * vol20_avg:
        return None

    # Cond 6: 4-bar ROC > 0 and accelerating
    closes = df["close"].iloc[-(MOMENTUM_LOOKBACK + 3):-1].values  # ends at `last`
    if len(closes) < MOMENTUM_LOOKBACK + 2:
        return None
    roc_now = closes[-1] / closes[-MOMENTUM_LOOKBACK - 1] - 1
    roc_prev = closes[-2] / closes[-MOMENTUM_LOOKBACK - 2] - 1
    if not (roc_now > 0 and roc_now > roc_prev):
        return None

    return {
        "close": float(last["close"]),
        "last_qv_usd": float(last_qv),
        "vol_ratio_20bar": float(last["volume"] / vol20_avg) if vol20_avg else None,
        "roc_4bar_pct": float(roc_now * 100),
        "roc_prev_pct": float(roc_prev * 100),
        "breakout_over_24h_high_pct": float(last["close"] / prior_high - 1) * 100,
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
    log(f"=== scan starting {pd.Timestamp.now(tz='UTC')} ===")

    mx = ccxt.mexc({
        "options": {"defaultType": "swap"},
        "enableRateLimit": True,
        "timeout": 10000,
    })

    markets = mx.load_markets()
    perps = [s for s, m in markets.items()
             if m.get("swap") and m.get("settle") == "USDT" and m.get("active")]
    log(f"active USDT perps: {len(perps)}")

    tickers = mx.fetch_tickers(perps)

    # Pre-filter on 24h pct change AND liquidity
    candidates = []
    for sym, t in tickers.items():
        pct = t.get("percentage")
        qv = t.get("quoteVolume")
        if pct is None or qv is None:
            continue
        if pct >= MIN_24H_PCT and qv >= MIN_24H_QV_PREFILTER:
            candidates.append(sym)
    log(f"candidates after >={MIN_24H_PCT}% 24h + ${MIN_24H_QV_PREFILTER:.0e} QV: {len(candidates)}")

    if not candidates:
        log("no candidates passed pre-filter; done.")
        return

    signals = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(fetch_15m, mx, s): s for s in candidates}
        for fut in as_completed(futures):
            sym, df = fut.result()
            sig = check_signal(df)
            if sig:
                sig["symbol"] = sym
                sig["pct_24h"] = tickers[sym].get("percentage")
                sig["qv_24h"] = tickers[sym].get("quoteVolume")
                signals.append(sig)

    log(f"signals fired: {len(signals)}")

    if not signals:
        return

    for s in signals:
        base = s["symbol"].split("/")[0]
        link = mexc_futures_link(s["symbol"])
        msg = (
            f"*{base}/USDT* breakout\n"
            f"Price: `{s['close']:.6g}`\n"
            f"24h: *+{s['pct_24h']:.1f}%*\n"
            f"15m bar vol: `${s['last_qv_usd']:,.0f}` ({s['vol_ratio_20bar']:.1f}x 20-bar avg)\n"
            f"4-bar ROC: `+{s['roc_4bar_pct']:.2f}%` (prev `+{s['roc_prev_pct']:.2f}%`)\n"
            f"Above 24h high by: `+{s['breakout_over_24h_high_pct']:.2f}%`\n"
            f"Bar close: `{s['bar_time_utc']}`\n"
            f"[Open in MEXC futures]({link})"
        )
        ok = send_telegram(msg)
        log(f"  {'sent' if ok else 'FAILED'}: {s['symbol']}")


if __name__ == "__main__":
    main()
