# -*- coding: utf-8 -*-
"""
gap_lib.py — Shared functions for the NSE Gap Scanner (GitHub Actions edition)
================================================================================
Used by both deep_scan.py (daily) and hourly_scan.py (hourly).

Data sources (all free, no login):
  - nsearchives.nseindia.com/content/equities/EQUITY_L.csv -> stock universe
  - yfinance -> 2yr historical OHLCV + market cap + live quote

State is persisted to JSON files (gap_store.json, mcap_cache.json) which
GitHub Actions commits back to the repo after each run, so data survives
between runs even though each run happens on a fresh, temporary machine.
"""

import os
import sys
import json
import time
import logging
import requests
import pandas as pd
import yfinance as yf
from io import StringIO
from datetime import datetime, timedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

GAP_STORE_FILE  = "gap_store.json"
MCAP_CACHE_FILE = "mcap_cache.json"

# ── Settings (can be overridden via environment variables) ────────────
MIN_MARKET_CAP_CR = float(os.environ.get("MIN_MARKET_CAP_CR") or 1000)
MIN_GAP_PERCENT    = float(os.environ.get("MIN_GAP_PERCENT") or 0.01)
VOLUME_MULTIPLIER  = float(os.environ.get("VOLUME_MULTIPLIER") or 1.5)
VOLUME_AVG_DAYS    = int(os.environ.get("VOLUME_AVG_DAYS") or 20)
LOOKBACK_YEARS     = int(os.environ.get("LOOKBACK_YEARS") or 2)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or ""
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID") or ""


# ============================================================
# JSON STATE HELPERS
# ============================================================
def load_gap_store():
    """Load gap_store.json. Returns dict with 'scan_date', 'alerted', 'stocks'."""
    if not os.path.exists(GAP_STORE_FILE):
        return {"scan_date": None, "alerted": [], "stocks": {}}
    try:
        with open(GAP_STORE_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"Could not read {GAP_STORE_FILE}: {e}")
        return {"scan_date": None, "alerted": [], "stocks": {}}


def save_gap_store(store):
    """Save gap store dict to gap_store.json."""
    try:
        with open(GAP_STORE_FILE, "w") as f:
            json.dump(store, f, indent=2)
        log.info(f"Saved {GAP_STORE_FILE} ({len(store.get('stocks', {}))} stocks).")
    except Exception as e:
        log.error(f"Could not save {GAP_STORE_FILE}: {e}")


# ============================================================
# FETCH ALL NSE EQ SYMBOLS
# ============================================================
def get_nse_symbols():
    """Returns list of NSE EQ series stock symbols e.g. ['RELIANCE', 'TCS', ...]."""
    try:
        url  = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        resp.raise_for_status()

        df = pd.read_csv(StringIO(resp.text))
        df.columns = [c.strip() for c in df.columns]
        for col in df.columns:
            if df[col].dtype == object:
                df[col] = df[col].str.strip()

        eq = df[df["SERIES"] == "EQ"]["SYMBOL"].tolist()
        log.info(f"NSE EQ symbols fetched: {len(eq)}")
        return eq
    except Exception as e:
        log.error(f"Failed to fetch NSE symbols: {e}")
        return []


# ============================================================
# FILTER: MARKET CAP > MIN_MARKET_CAP_CR
# Cached to mcap_cache.json for 7 days (committed back to repo)
# ============================================================
def filter_by_market_cap(symbols):
    min_cap    = MIN_MARKET_CAP_CR * 1e7
    symbol_set = set(symbols)

    if os.path.exists(MCAP_CACHE_FILE):
        try:
            with open(MCAP_CACHE_FILE, "r") as f:
                cache = json.load(f)
            cached_date = datetime.fromisoformat(cache["date"])
            age_days    = (datetime.now() - cached_date).days

            if age_days < 7:
                passing = [s for s in cache["symbols"] if s in symbol_set]
                log.info(f"Market cap filter: loaded {len(passing)} stocks from cache "
                         f"(cached {age_days} day(s) ago).")
                return passing
            else:
                log.info(f"Market cap cache is {age_days} days old. Refreshing...")
        except Exception as e:
            log.warning(f"Cache read failed: {e}. Fetching fresh...")

    log.info(f"Market cap filter: fetching {len(symbols)} stocks from yfinance...")
    passing = []

    for i, sym in enumerate(symbols):
        try:
            ticker = yf.Ticker(f"{sym}.NS")
            mcap   = ticker.fast_info.market_cap or 0
            if mcap >= min_cap:
                passing.append(sym)
            time.sleep(0.15)
        except Exception:
            continue
        if (i + 1) % 100 == 0:
            log.info(f"Market cap filter: {i + 1}/{len(symbols)} done, {len(passing)} passing...")

    try:
        with open(MCAP_CACHE_FILE, "w") as f:
            json.dump({"date": datetime.now().isoformat(), "symbols": passing}, f)
        log.info(f"Market cap cache saved. Valid for 7 days.")
    except Exception as e:
        log.warning(f"Could not save market cap cache: {e}")

    log.info(f"Market cap filter done: {len(passing)} stocks pass.")
    return passing


# ============================================================
# FETCH HISTORICAL DAILY CANDLES VIA YFINANCE
# ============================================================
def fetch_historical_candles(symbol):
    try:
        ticker = yf.Ticker(f"{symbol}.NS")
        end    = datetime.now()
        start  = end - timedelta(days=365 * LOOKBACK_YEARS)
        df     = ticker.history(start=start, end=end, interval="1d", auto_adjust=True)
        if df is None or df.empty:
            return None
        return df.sort_index()
    except Exception as e:
        log.warning(f"Historical fetch failed for {symbol}: {e}")
        return None


# ============================================================
# FIND ALL OPEN DOWNSIDE GAPS IN HISTORY
# ============================================================
def find_downside_gaps(df):
    """
    Downside gap: prev_low > curr_high
    Open: no subsequent candle HIGH ever touched gap_bottom (excl. today)
    """
    open_gaps = []
    history   = df.iloc[:-1]  # exclude today's candle

    for i in range(1, len(history)):
        prev = history.iloc[i - 1]
        curr = history.iloc[i]

        if prev["Low"] > curr["High"]:
            gap_top    = prev["Low"]
            gap_bottom = curr["High"]
            gap_size   = (gap_top - gap_bottom) / gap_top

            if gap_size < MIN_GAP_PERCENT:
                continue

            future = history.iloc[i + 1:]
            if not future[future["High"] >= gap_bottom].empty:
                continue

            open_gaps.append({
                "gap_date":        history.index[i].strftime("%Y-%m-%d"),
                "gap_top":         round(float(gap_top), 2),
                "gap_bottom":      round(float(gap_bottom), 2),
                "gap_candle_high": round(float(curr["High"]), 2),
                "gap_size_pct":    round(float(gap_size) * 100, 2),
            })

    return open_gaps


# ============================================================
# FETCH TODAY'S LIVE QUOTE VIA YFINANCE
# ============================================================
def fetch_live_quote(symbol):
    try:
        ticker     = yf.Ticker(f"{symbol}.NS")
        fast       = ticker.fast_info
        today_high = float(fast.day_high or 0)
        today_low  = float(fast.day_low or 0)
        today_ltp  = float(fast.last_price or 0)
        today_vol  = 0

        hist = ticker.history(period="1d", interval="1d")
        if not hist.empty:
            today_vol = int(hist["Volume"].iloc[-1])

        if today_high <= 0:
            return None

        return {"high": today_high, "low": today_low, "ltp": today_ltp, "volume": today_vol}
    except Exception as e:
        log.warning(f"Live quote failed for {symbol}: {e}")
        return None


# ============================================================
# CHECK TODAY'S LIVE DATA AGAINST STORED GAPS
# ============================================================
def check_gap_fill_live(symbol, gaps, avg_volume, alerted_set):
    """
    Returns (triggered_list, today_snapshot_dict).
    alerted_set: set of "SYMBOL|gap_date" strings already alerted today.
    """
    quote = fetch_live_quote(symbol)
    if not quote:
        return [], None

    today_high = quote["high"]
    today_vol  = quote["volume"]
    today_ltp  = quote["ltp"]

    today_snapshot = {
        "date":   datetime.now().strftime("%Y-%m-%d"),
        "high":   today_high,
        "low":    quote["low"],
        "close":  today_ltp,
        "volume": today_vol,
    }

    volume_ok = today_vol >= VOLUME_MULTIPLIER * avg_volume
    triggered = []

    for gap in gaps:
        alert_key = f"{symbol}|{gap['gap_date']}"
        if alert_key in alerted_set:
            continue

        filled     = today_high >= gap["gap_top"]
        broke_high = today_high > gap["gap_candle_high"]

        if filled and broke_high and volume_ok:
            triggered.append({
                **gap,
                "symbol":       symbol,
                "today_high":   round(today_high, 2),
                "today_ltp":    round(today_ltp, 2),
                "today_volume": today_vol,
                "avg_volume":   int(avg_volume),
                "volume_ratio": round(today_vol / avg_volume, 2),
            })

    return triggered, today_snapshot


# ============================================================
# SEND TELEGRAM ALERT
# ============================================================
def send_telegram(alerts):
    if not alerts:
        return
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Telegram not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID secrets.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    for a in alerts:
        msg = (
            f"*GAP FILL ALERT*\n"
            f"*Stock:* {a['symbol']}\n"
            f"*Gap Date:* {a['gap_date']}\n"
            f"*Gap Zone:* Rs.{a['gap_bottom']} to Rs.{a['gap_top']}\n"
            f"*Gap Size:* {a['gap_size_pct']}%\n"
            f"*Gap Candle High:* Rs.{a['gap_candle_high']}\n"
            f"*Today High:* Rs.{a['today_high']}\n"
            f"*LTP:* Rs.{a['today_ltp']}\n"
            f"*Volume:* {a['today_volume']:,}\n"
            f"*20D Avg Vol:* {a['avg_volume']:,}\n"
            f"*Vol Ratio:* {a['volume_ratio']}x avg\n"
            f"*Time:* {datetime.now().strftime('%d %b %Y %H:%M')} IST"
        )
        try:
            resp = requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown",
            }, timeout=10)
            if resp.status_code == 200:
                log.info(f"Telegram alert sent for {a['symbol']}.")
            else:
                log.error(f"Telegram failed for {a['symbol']}: {resp.text}")
        except Exception as e:
            log.error(f"Telegram error: {e}")
