# -*- coding: utf-8 -*-
"""
hourly_scan.py — Runs at :20 past every hour during market hours
====================================================================
1. Loads gap_store.json (built by deep_scan.py)
2. Fetches today's live quote for each stock with open gaps
3. Checks if any gap has been filled + broken + high volume
4. Sends Telegram alert if triggered
5. Saves updated gap_store.json back (committed by the workflow)

Run manually:  python hourly_scan.py
"""

from datetime import datetime
import gap_lib as lib

log = lib.log


def main():
    log.info("=" * 60)
    log.info("HOURLY SCAN starting...")

    store     = lib.load_gap_store()
    stocks    = store.get("stocks", {})
    alerted   = set(store.get("alerted", []))
    today_str = datetime.now().strftime("%Y-%m-%d")

    # If gap_store.json is from a previous day, deep_scan.py hasn't run
    # yet today — nothing to check against.
    if store.get("scan_date") != today_str:
        log.warning(f"gap_store.json is from {store.get('scan_date')}, not today "
                     f"({today_str}). Deep scan may not have run yet. Skipping.")
        return

    if not stocks:
        log.info("No stocks with open gaps to check.")
        return

    log.info(f"Checking {len(stocks)} stocks with open gaps...")
    all_alerts = []

    for sym, data in stocks.items():
        triggered, snapshot = lib.check_gap_fill_live(
            sym, data["gaps"], data["avg_volume"], alerted
        )

        if snapshot:
            data["latest_candle"] = snapshot

        for t in triggered:
            all_alerts.append(t)
            alerted.add(f"{sym}|{t['gap_date']}")
            log.info(f"ALERT: {sym} gap from {t['gap_date']} filled! "
                     f"High={t['today_high']} Vol={t['volume_ratio']}x")

    log.info(f"Hourly scan done. {len(all_alerts)} new alert(s).")

    if all_alerts:
        lib.send_telegram(all_alerts)

    # Save updated state (latest candles + alerted set)
    store["alerted"] = list(alerted)
    store["stocks"]  = stocks
    lib.save_gap_store(store)


if __name__ == "__main__":
    main()
