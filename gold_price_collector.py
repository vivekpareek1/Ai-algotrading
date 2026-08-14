#!/usr/bin/env python3
"""
gold_price_collector.py
========================
Lightweight companion to oi_collector.py — records MCX Gold (GOLDM futures)
price every 5 minutes for the dashboard's Gold chart. Deliberately much
simpler than oi_collector: this is a price series for a chart, not a full
options/OI pipeline. No option chain, no OI, no PCR — just price over time.

REUSES oi_collector's already-tested AngelSession (login, session reuse,
retry-with-backoff) and fetch_scrip_master rather than duplicating that logic
in a second, possibly-diverging implementation.

WHY MCX, NOT XAUUSD: this reads the actual MCX GOLDM contract via your Angel
One account, not an international gold proxy — real Indian market price,
real lot size, real basis to what you'd actually trade.

MCX TRADING HOURS ASSUMPTION: MCX runs a day + evening session, extending
into the evening (the exact evening close shifts with US daylight saving,
typically 23:30 or 23:55 IST). This uses a fixed 09:00-23:30 window as a
reasonable default — if the evening close is at 23:55 on a given date, the
last ~25 minutes won't be captured. Tighten or widen MARKET_CLOSE below if
that matters to you.

RUN:
    python3 gold_price_collector.py --dry-run   # one snapshot, prints, writes nothing
    python3 gold_price_collector.py              # runs forever, snapshots every 5 min
"""

import argparse
import csv
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))
from oi_collector import (
    AngelSession, fetch_scrip_master, _parse_expiry, now_ist,
    LOG_DIR, is_trading_day,
)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SNAPSHOT_INTERVAL_SEC = 300

# See module docstring — MCX evening close shifts with US DST.
MARKET_OPEN = (9, 0)
MARKET_CLOSE = (23, 30)

GOLD_UNDERLYING = "GOLDM"   # Gold Mini, 100g — matches the instrument already
                            # decided on for the MCX strategy work

_log = None


def get_logger() -> logging.Logger:
    global _log
    if _log is not None:
        return _log
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "gold_collector.log"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    _log = logging.getLogger("gold_collector")
    return _log


def find_nearest_gold_future(master: List[Dict[str, Any]], log: logging.Logger):
    """
    Finds the nearest non-expired GOLDM futures contract on MCX.

    Deliberately broad matching (exch_seg == MCX, name == GOLDM, instrument
    type starting with FUT) rather than a single hardcoded instrumenttype
    string — commodity futures type codes aren't confirmed against a live
    instrument master at the time this was written, so a narrow exact-match
    risks silently finding nothing if the real code differs slightly from
    the assumption. Broad matching plus a loud, specific error if nothing
    is found is safer than a guess that fails silently.
    """
    today = now_ist().date()
    candidates = []
    for row in master:
        if row.get("exch_seg") != "MCX":
            continue
        if row.get("name") != GOLD_UNDERLYING:
            continue
        itype = row.get("instrumenttype", "") or ""
        if not itype.upper().startswith("FUT"):
            continue
        exp = _parse_expiry(row.get("expiry", ""))
        if exp is None or exp < today:
            continue
        candidates.append((exp, row))

    if not candidates:
        raise ValueError(
            f"No MCX {GOLD_UNDERLYING} futures contract found in the "
            f"instrument master. Either the instrument type code assumed "
            f"here doesn't match what Angel actually uses for MCX commodity "
            f"futures, or {GOLD_UNDERLYING} isn't the right symbol name. "
            f"Run with --dry-run and inspect the master directly to confirm."
        )

    candidates.sort(key=lambda c: c[0])
    exp, row = candidates[0]
    log.info("%s -> future expiry %s, token %s, symbol %s",
             GOLD_UNDERLYING, exp, row.get("token"), row.get("symbol"))
    return row


SUMMARY_FIELDS = ["timestamp", "underlying", "spot", "token", "symbol"]


def append_row(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS)
        if is_new:
            w.writeheader()
        w.writerow(row)


def take_snapshot(session: AngelSession, contract: Dict[str, Any],
                  log: logging.Logger, dry_run: bool) -> None:
    token = str(contract["token"])
    quotes = session.quotes({"MCX": [token]})
    data = quotes.get(token)
    if not data:
        log.warning("No quote returned for token %s", token)
        return

    ltp = data.get("ltp")
    if ltp is None:
        log.warning("Quote for %s had no ltp field: %s", token, data)
        return

    ts = now_ist().strftime("%Y-%m-%d %H:%M:%S")
    row = {"timestamp": ts, "underlying": "GOLD", "spot": ltp,
           "token": token, "symbol": contract.get("symbol", "")}

    if dry_run:
        log.info("[DRY] GOLD ltp=%s token=%s symbol=%s", ltp, token,
                 contract.get("symbol"))
        return

    day = now_ist().strftime("%Y-%m-%d")
    path = DATA_DIR / "GOLD" / f"summary_{day}.csv"
    append_row(path, row)
    log.info("GOLD ltp=%s -> %s", ltp, path.name)


def in_window(now_hm, open_hm, close_hm) -> bool:
    return open_hm <= now_hm <= close_hm


def run(dry_run: bool = False) -> None:
    log = get_logger()
    log.info("Gold price collector starting (dry_run=%s)", dry_run)

    session = AngelSession(log)
    master = fetch_scrip_master(log)
    contract = find_nearest_gold_future(master, log)

    if dry_run:
        take_snapshot(session, contract, log, dry_run=True)
        log.info("Dry run complete.")
        return

    holidays = set()  # oi_collector's is_trading_day handles missing holiday file

    while True:
        now = now_ist()
        now_hm = (now.hour, now.minute)

        if not is_trading_day(now.date(), holidays):
            log.info("Not a trading day, sleeping 1h")
            time.sleep(3600)
            continue

        if not in_window(now_hm, MARKET_OPEN, MARKET_CLOSE):
            log.info("Outside MCX window (%02d:%02d-%02d:%02d), sleeping 5 min",
                     *MARKET_OPEN, *MARKET_CLOSE)
            time.sleep(300)
            continue

        try:
            take_snapshot(session, contract, log, dry_run=False)
        except Exception as exc:
            log.error("Snapshot failed: %s", exc)
            # matches paper_trade.py's reasoning: don't force a re-login on
            # every failure, only on an actual session error. quotes()
            # already re-authenticates internally on token/session errors.

        elapsed = (now_ist() - now).total_seconds()
        time.sleep(max(5.0, SNAPSHOT_INTERVAL_SEC - elapsed))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MCX Gold price collector")
    parser.add_argument("--dry-run", action="store_true",
                        help="take one snapshot, print it, write nothing")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
