# Shared Risk & Position-Sizing Module (v2)

One gatekeeper in front of all your strategies (Nifty options indicator,
arbitrage scanner, MCX toolkit), sharing a single capital pool.

## Bugs found and fixed in v2

v1 was reviewed and tested properly. Eleven real issues were found. The
first one would have caused actual money loss:

1. **RACE CONDITION (critical).** Two engines opening trades at the same
   moment silently lost one of the positions. Reproduced in testing: both
   engines reported success, only one was recorded. The lost position was
   invisible to the risk limits and the kill switch. **Fixed** with an
   exclusive cross-process file lock; state is re-read inside the lock.
   Retested with 5 concurrent engines — all 5 recorded, total risk stayed
   under the cap.
2. **Rejected instead of sizing down.** An MCX trade whose risk-based size
   exceeded the capital cap was rejected outright rather than reduced.
   **Fixed** — now sizes down to the largest position that fits.
3. **Gap between sizing and recording.** Two engines could both pass the
   check in the seconds before either recorded its fill, jointly breaching
   the limit. **Fixed** with a reservation system.
4. **Failed orders leaked risk.** No way to undo an approval if the broker
   rejected the order. **Fixed** with `cancel_reservation()` plus a
   3-minute auto-expiry so a crashed engine can't lock up risk forever.
5. **Unvalidated quantity.** `record_open()` accepted any size, even one
   never approved. **Fixed** — validated against the reservation.
6. **State corruption on crash.** Wrote in place; a crash mid-write left
   unreadable JSON. **Fixed** with atomic temp-file + rename.
7. **Capital counter drift.** A separate counter drifted out of sync if a
   close was missed. **Fixed** — now derived from open positions.
8. **Dead config key.** `trading_day_reset_time` existed but was never
   used; the day rolled at midnight instead of market open. **Fixed.**
9. **Kill switch blind to open losses.** Only saw realized P&L — you could
   be deep in the red on open positions and still get new trades approved.
   **Fixed** via optional `update_mark()`.
10. **Crashed without tzdata.** Now falls back to fixed +05:30 with a warning.
11. **Wrong-side stop-loss accepted.** A BUY with the stop above entry was
    sized as if valid. Now rejected as a caller bug.

All 20 test cases pass, including the concurrency test that failed in v1.

## The API changed — v1 calling code will break

Three calls now, not two:

```python
d = rm.size_trade(strategy_name=..., entry_price=..., stop_loss_price=...,
                  lot_size=..., symbol=..., direction=...)
if d["approved"]:
    # place the order at your broker
    if filled:
        rm.record_open(..., quantity=actual_filled_qty,
                       reservation_id=d["reservation_id"])
    else:
        rm.cancel_reservation(d["reservation_id"])
```

**Always** call `record_open` OR `cancel_reservation`. If you skip both,
the risk stays reserved for 3 minutes and other strategies see less
headroom than they actually have. `integration_example.py` shows the
try/except pattern that guarantees this.

## Setup

1. Put `risk_manager.py` + `config.json` in one folder, e.g.
   `/opt/trading/risk/`. All engines import from here.
2. Edit `config.json` — `total_capital` is the big one.
3. Every engine must construct `RiskManager()` with the **same absolute
   path** to that config. Different paths = separate risk pools = the
   whole point defeated.
4. Feed live prices via `update_mark()` (see `integration_example.py`
   section 4) or the kill switch only reacts to closed trades.
5. Dry-run for a few days before real money. Watch `trade_journal.csv`.

## Still worth knowing

- **Futures notional vs margin.** The capital cap measures full notional,
  not margin. For MCX this is conservative and will size you smaller than
  your margin allows. Raise `max_share_of_capital_pct` for MCX, or ask and
  I'll switch that check to margin-based.
- **Gap risk.** `risk_amount` assumes the stop-loss fills at exactly the
  stop price. On a gap open — expiry days especially — the real loss can
  be larger. No software can fix this; size with it in mind.
- **Arbitrage legs.** Both legs are sized independently and may come back
  unequal. `integration_example.py` cancels both in that case rather than
  placing an unbalanced hedge. Review that logic against how your scanner
  actually constructs pairs.
- **The kill switch is realized + unrealized**, and only unblocks on the
  next trading day. `force_unblock()` exists but overriding your own limit
  after a bad day is the exact habit this module is meant to stop.
- **Back up `risk_state.json`** — it is the only record of what's open
  across all strategies. Cron a daily copy.
