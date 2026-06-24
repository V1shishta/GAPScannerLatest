# -*- coding: utf-8 -*-
"""
deep_scan.py — Runs once a day (before market open)
======================================================
1. Fetches all NSE EQ stocks
2. Filters by market cap (cached 7 days)
3. Fetches 2 years of history per stock
4. Finds all open downside gaps
5. Saves everything to gap_store.json (committed back to repo by the workflow)

Run manually:  python deep_scan.py
"""

import time
from datetime import datetime
import gap_lib as lib

log = lib.log


def main():
    log.info("=" * 60)
    log.info("DEEP SCAN starting...")
    t0 = time.time()

    today_str = datetime.now().strftime("%Y-%m-%d")

    # Step 1: NSE universe
    all_symbols = lib.get_nse_symbols()
    if not all_symbols:
        log.error("No symbols fetched. Aborting.")
        return

    # Step 2: Market cap filter
    all_symbols = lib.filter_by_market_cap(all_symbols)
    if not all_symbols:
        log.error("No stocks passed market cap filter. Aborting.")
        return

    log.info(f"Final filtered universe: {len(all_symbols)} stocks. Fetching history...")

    # Step 3: Fetch historical candles, find open gaps
    stocks = {}
    stocks_with_gaps = 0

    for i, sym in enumerate(all_symbols):
        try:
            df = lib.fetch_historical_candles(sym)
            if df is None or len(df) < lib.VOLUME_AVG_DAYS + 5:
                continue

            open_gaps = lib.find_downside_gaps(df)
            if not open_gaps:
                continue

            avg_vol = float(df["Volume"].iloc[-(lib.VOLUME_AVG_DAYS + 1):-1].mean())

            stocks[sym] = {
                "avg_volume":    int(avg_vol),
                "gaps":          open_gaps,
                "latest_candle": None,
            }
            stocks_with_gaps += 1

        except Exception as e:
            log.warning(f"Deep scan error for {sym}: {e}")
            continue

        if (i + 1) % 50 == 0:
            log.info(f"History fetch: {i + 1}/{len(all_symbols)} done, "
                     f"{stocks_with_gaps} with gaps...")

    # Step 4: Save to gap_store.json
    # Carry over today's alerted list if this is a same-day re-run,
    # otherwise start fresh for a new trading day.
    old_store = lib.load_gap_store()
    alerted   = old_store.get("alerted", []) if old_store.get("scan_date") == today_str else []

    store = {
        "scan_date": today_str,
        "alerted":   alerted,
        "stocks":    stocks,
    }
    lib.save_gap_store(store)

    elapsed = round((time.time() - t0) / 60, 1)
    log.info(f"DEEP SCAN complete in {elapsed} mins. "
             f"{stocks_with_gaps} stocks have open gaps.")


if __name__ == "__main__":
    main()
