"""
risk_manager.py  (v2 — bug-fixed)
==================================
Shared portfolio-level risk & position-sizing layer for multiple trading
strategies (Nifty options indicator, arbitrage scanner, MCX toolkit).

Designed to be used by SEVERAL SEPARATE PROCESSES at the same time, all
sharing one capital pool via one state file.

WHAT CHANGED FROM v1 (all were real bugs found in testing):
  1. RACE CONDITION (critical) — two engines opening trades at the same
     time silently lost one of the positions. Now every state change
     happens under an exclusive file lock, with state re-read from disk
     inside the lock.
  2. SIZE-DOWN instead of reject — a risk-based size whose notional
     exceeded the capital cap was rejected outright. Now it sizes down to
     the largest position that fits, and only rejects if that is 0 lots.
  3. RESERVATION SYSTEM — v1 sized a trade, then recorded it later, with
     broker latency in between. Two engines could both pass the risk check
     in that gap and jointly breach the limit. size_trade() now RESERVES
     the risk; record_open() converts the reservation into a position;
     cancel_reservation() releases it if the order fails. Reservations
     expire automatically so a crashed engine cannot leak risk forever.
  4. ORDER-FAILURE LEAK — v1 had no way to undo an approval if the broker
     rejected the order. Fixed by cancel_reservation().
  5. QUANTITY MISMATCH — record_open() accepted any quantity, even one
     that was never approved. Now it validates against the reservation.
  6. CORRUPTED STATE ON CRASH — v1 wrote the state file in place. A crash
     mid-write left unreadable JSON. Now writes to a temp file and renames
     (atomic on POSIX).
  7. CAPITAL-USED DRIFT — v1 tracked capital used in a separate counter
     that drifted out of sync if a close was missed. Now derived from the
     open positions themselves, so it cannot drift.
  8. DEAD CONFIG KEY — config had trading_day_reset_time which was never
     used; the day rolled at midnight, not at market open. Now honoured.
  9. UNREALIZED LOSS BLIND SPOT — the kill switch only saw realized P&L,
     so it would happily approve new trades while sitting on large open
     losses. Optional mark-to-market now feeds the kill switch.
 10. MISSING TZDATA — crashed on systems without tzdata. Now falls back
     to a fixed +05:30 offset with a warning.
 11. WRONG-SIDE STOP-LOSS — a BUY with the stop above entry was accepted
     and sized as if valid. Now rejected as a caller bug.

Usage: see integration_example.py
"""

import json
import csv
import os
import tempfile
import uuid
import fcntl
import warnings
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone, time as dtime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    _HAS_ZONEINFO = True
except ImportError:  # pragma: no cover
    _HAS_ZONEINFO = False


def _get_tz(name):
    """Fix #10: don't crash if tzdata is missing on the server."""
    if _HAS_ZONEINFO:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    warnings.warn(
        f"Timezone '{name}' unavailable (tzdata not installed?). "
        f"Falling back to fixed UTC+05:30. Install tzdata for correctness."
    )
    return timezone(timedelta(hours=5, minutes=30))


# How long a reservation stays valid before it is auto-released.
# Covers the gap between sizing a trade and the broker confirming the fill.
RESERVATION_TTL_SECONDS = 180


class RiskManager:
    def __init__(self, config_path="config.json", clock=None):
        """clock: optional callable returning the current tz-aware datetime.
        Used for backtesting, where 'now' must follow simulated historical
        timestamps instead of the real wall clock — otherwise the daily
        loss kill switch and trading-day rollover would use TODAY's real
        date for every simulated day, which is wrong. Leave as None for
        live/normal use (uses the real clock)."""
        self.config_path = Path(config_path).resolve()
        self._load_config()
        self._clock_fn = clock

        self.tz = _get_tz(self.config.get("timezone", "Asia/Kolkata"))
        base = self.config_path.parent
        self.state_path = base / self.config["state_file"]
        self.journal_path = base / self.config["journal_file"]
        self.lock_path = base / (self.config["state_file"] + ".lock")

        self._ensure_journal_header()
        with self._locked():
            pass  # forces initial load, day-reset and reservation cleanup

    # ------------------------------------------------------------------ #
    # Config
    # ------------------------------------------------------------------ #

    def _load_config(self):
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_path}")
        with open(self.config_path, "r") as f:
            self.config = json.load(f)

        required = ["total_capital", "risk_per_trade_pct", "max_daily_loss_pct",
                    "max_concurrent_open_risk_pct", "state_file", "journal_file"]
        for key in required:
            if key not in self.config:
                raise ValueError(f"config.json missing required key: {key}")
        if self.config["total_capital"] <= 0:
            raise ValueError("total_capital must be > 0")
        for pct_key in ["risk_per_trade_pct", "max_daily_loss_pct",
                        "max_concurrent_open_risk_pct"]:
            if not (0 < self.config[pct_key] <= 100):
                raise ValueError(f"{pct_key} must be between 0 and 100")
        if self.config["risk_per_trade_pct"] > self.config["max_concurrent_open_risk_pct"]:
            raise ValueError(
                "risk_per_trade_pct cannot exceed max_concurrent_open_risk_pct — "
                "no single trade would ever be allowed through"
            )

    # ------------------------------------------------------------------ #
    # Fix #1 + #6: locked, atomic state access
    # ------------------------------------------------------------------ #

    @contextmanager
    def _locked(self):
        """Exclusive lock across processes. State is re-read from disk inside
        the lock and written atomically on exit, so concurrent engines can
        never clobber each other's positions."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "w") as lockfile:
            fcntl.flock(lockfile.fileno(), fcntl.LOCK_EX)
            try:
                self._read_state()
                self._maybe_reset_for_new_day()
                self._expire_reservations()
                yield
                self._write_state_atomic()
            finally:
                fcntl.flock(lockfile.fileno(), fcntl.LOCK_UN)

    def _read_state(self):
        if self.state_path.exists():
            try:
                with open(self.state_path, "r") as f:
                    self.state = json.load(f)
            except json.JSONDecodeError:
                warnings.warn(
                    f"{self.state_path} is corrupted. Starting a fresh state. "
                    f"Check your backup — open positions may be untracked."
                )
                self.state = self._blank_state()
        else:
            self.state = self._blank_state()
        # forward-compat for state files written by v1
        self.state.setdefault("reservations", {})
        self.state.setdefault("marks", {})
        self.state.setdefault("open_positions", {})
        self.state.setdefault("realized_pnl_today", 0.0)
        self.state.setdefault("blocked_for_today", False)
        self.state.setdefault("block_reason", None)
        self.state.setdefault("engine_active", False)
        self.state.pop("strategy_capital_used", None)  # fix #7: now derived

    def _blank_state(self):
        return {
            "trading_day": self._trading_day_str(),
            "realized_pnl_today": 0.0,
            "blocked_for_today": False,
            "block_reason": None,
            "open_positions": {},
            "reservations": {},
            "marks": {},          # trade_id -> last known market price
            "engine_active": False,   # starts OFF — you must manually activate
        }

    def _write_state_atomic(self):
        """Fix #6: temp file + rename, so a crash mid-write can't corrupt state."""
        fd, tmp = tempfile.mkstemp(dir=str(self.state_path.parent),
                                    prefix=".risk_state_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.state, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ------------------------------------------------------------------ #
    # Fix #8: trading day honours reset time, not midnight
    # ------------------------------------------------------------------ #

    def _now(self):
        if self._clock_fn is not None:
            return self._clock_fn()
        return datetime.now(self.tz)

    def _trading_day_str(self):
        """A trading day starts at trading_day_reset_time (default 09:00).
        Anything before that belongs to the previous trading day, so an
        overnight session isn't split in half."""
        now = self._now()
        reset_str = self.config.get("trading_day_reset_time", "09:00")
        try:
            hh, mm = [int(x) for x in reset_str.split(":")]
            reset_t = dtime(hh, mm)
        except Exception:
            reset_t = dtime(9, 0)
        if now.time() < reset_t:
            return (now - timedelta(days=1)).strftime("%Y-%m-%d")
        return now.strftime("%Y-%m-%d")

    def _maybe_reset_for_new_day(self):
        today = self._trading_day_str()
        if self.state.get("trading_day") != today:
            self.state["trading_day"] = today
            self.state["realized_pnl_today"] = 0.0
            self.state["blocked_for_today"] = False
            self.state["block_reason"] = None
            # open positions and their marks carry over — they are not closed

    # ------------------------------------------------------------------ #
    # Fix #3/#4: reservations
    # ------------------------------------------------------------------ #

    def _expire_reservations(self):
        """Release risk reserved by an engine that crashed or never filled."""
        now = self._now()
        expired = []
        for rid, r in self.state["reservations"].items():
            try:
                created = datetime.fromisoformat(r["created_at"])
            except Exception:
                expired.append(rid)
                continue
            if (now - created).total_seconds() > RESERVATION_TTL_SECONDS:
                expired.append(rid)
        for rid in expired:
            r = self.state["reservations"].pop(rid)
            self._journal("RESERVATION_EXPIRED", trade_id=rid,
                          strategy=r.get("strategy", ""), symbol=r.get("symbol", ""),
                          risk_amount=r.get("risk_amount", ""),
                          reason=f"not filled within {RESERVATION_TTL_SECONDS}s — risk released")

    def _committed_risk(self):
        """Open positions PLUS live reservations. Using both is what closes
        the race window between sizing and recording a trade."""
        open_risk = sum(p["risk_amount"] for p in self.state["open_positions"].values())
        reserved = sum(r["risk_amount"] for r in self.state["reservations"].values())
        return open_risk + reserved

    def _committed_capital(self, strategy_name=None):
        """Fix #7: derived from positions + reservations, never a separate
        counter that can drift."""
        total = 0.0
        for p in self.state["open_positions"].values():
            if strategy_name is None or p["strategy"] == strategy_name:
                total += p["entry_price"] * p["quantity"]
        for r in self.state["reservations"].values():
            if strategy_name is None or r["strategy"] == strategy_name:
                total += r["entry_price"] * r["quantity"]
        return total

    # ------------------------------------------------------------------ #
    # Journal
    # ------------------------------------------------------------------ #

    _JOURNAL_COLS = ["timestamp", "event", "trade_id", "strategy", "symbol",
                     "direction", "entry_price", "stop_loss_price", "exit_price",
                     "quantity", "risk_amount", "pnl", "reason"]

    def _ensure_journal_header(self):
        if not self.journal_path.exists():
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.journal_path, "w", newline="") as f:
                csv.writer(f).writerow(self._JOURNAL_COLS)

    def _journal(self, event, **kwargs):
        row = {c: "" for c in self._JOURNAL_COLS}
        row["timestamp"] = self._now().isoformat()
        row["event"] = event
        row.update({k: v for k, v in kwargs.items() if k in row})
        with open(self.journal_path, "a", newline="") as f:
            csv.writer(f).writerow([row[c] for c in self._JOURNAL_COLS])

    # ------------------------------------------------------------------ #
    # Kill switch
    # ------------------------------------------------------------------ #

    def _unrealized_pnl(self):
        """Fix #9: only counts positions you have supplied a mark for."""
        total = 0.0
        for tid, pos in self.state["open_positions"].items():
            mark = self.state["marks"].get(tid)
            if mark is None:
                continue
            mult = 1 if pos["direction"].upper() == "BUY" else -1
            total += (mark - pos["entry_price"]) * pos["quantity"] * mult
        return total

    def _check_kill_switch(self):
        """Fires on realized loss, or realized+unrealized if marks are supplied."""
        if self.state["blocked_for_today"]:
            return
        capital = self.config["total_capital"]
        limit = capital * (self.config["max_daily_loss_pct"] / 100.0)

        realized = self.state["realized_pnl_today"]
        unrealized = self._unrealized_pnl()
        combined = realized + unrealized

        if -realized >= limit:
            basis = f"realized loss Rs.{-realized:,.0f}"
        elif -combined >= limit:
            basis = (f"realized Rs.{realized:,.0f} + open (unrealized) "
                     f"Rs.{unrealized:,.0f} = Rs.{combined:,.0f}")
        else:
            return

        self.state["blocked_for_today"] = True
        self.state["block_reason"] = (
            f"Daily loss limit hit: {basis} vs limit Rs.{limit:,.0f}. "
            f"New trades blocked for the rest of the day. "
            f"Open positions NOT auto-closed."
        )
        self._journal("KILL_SWITCH", reason=self.state["block_reason"])

    # ------------------------------------------------------------------ #
    # Core: sizing (reserves risk)
    # ------------------------------------------------------------------ #

    def size_trade(self, strategy_name, entry_price, stop_loss_price,
                    lot_size=1, symbol="", direction="BUY", min_lots=1,
                    reserve=True):
        """
        Sizes a position so max loss (if stop-loss hits) <= risk_per_trade_pct
        of total capital, then checks portfolio limits.

        On approval it RESERVES that risk and returns a reservation_id.
        You MUST then call either:
            record_open(..., reservation_id=...)   if the order filled
            cancel_reservation(reservation_id)     if it did not
        Unclaimed reservations auto-expire after RESERVATION_TTL_SECONDS.

        Set reserve=False for a what-if check that changes nothing.

        Returns dict: approved, quantity, lots, risk_amount, reservation_id,
                      reason, sized_down (bool)
        """
        def reject(reason):
            return {"approved": False, "quantity": 0, "lots": 0,
                    "risk_amount": 0.0, "reservation_id": None,
                    "reason": reason, "sized_down": False}

        # --- input validation (no lock needed) ---
        if entry_price <= 0 or stop_loss_price <= 0:
            return reject("entry_price and stop_loss_price must be > 0")
        if lot_size <= 0:
            return reject("lot_size must be > 0")
        if min_lots < 1:
            return reject("min_lots must be >= 1")

        per_unit_risk = abs(entry_price - stop_loss_price)
        if per_unit_risk == 0:
            return reject("stop_loss_price equals entry_price — cannot size a "
                          "trade with zero risk distance")

        # Fix #11: stop on the wrong side of entry is almost always a caller bug
        d = direction.upper()
        if d == "BUY" and stop_loss_price >= entry_price:
            return reject(f"BUY trade with stop-loss ({stop_loss_price}) at or above "
                          f"entry ({entry_price}) — check your signal logic")
        if d in ("SELL", "SHORT") and stop_loss_price <= entry_price:
            return reject(f"SELL trade with stop-loss ({stop_loss_price}) at or below "
                          f"entry ({entry_price}) — check your signal logic")

        strat_cfg = self.config.get("strategies", {}).get(strategy_name)
        if strat_cfg is not None and not strat_cfg.get("enabled", True):
            return reject(f"strategy '{strategy_name}' is disabled in config.json")

        capital = self.config["total_capital"]
        per_lot_risk = per_unit_risk * lot_size
        per_lot_value = entry_price * lot_size

        with self._locked():
            if not self.state.get("engine_active", False):
                reason = ("Engine is OFF. No trades are approved until you activate "
                          "it manually (dashboard or activate_engine()).")
                self._journal("BLOCKED", strategy=strategy_name, symbol=symbol,
                              direction=direction, entry_price=entry_price,
                              stop_loss_price=stop_loss_price, reason=reason)
                return reject(reason)

            if self.state["blocked_for_today"]:
                reason = self.state["block_reason"]
                self._journal("BLOCKED", strategy=strategy_name, symbol=symbol,
                              direction=direction, reason=reason)
                return reject(reason)

            # 1) lots allowed by per-trade risk
            max_risk_amount = capital * (self.config["risk_per_trade_pct"] / 100.0)
            lots_by_risk = int(max_risk_amount // per_lot_risk)

            # 2) lots allowed by remaining portfolio open-risk headroom
            max_open_risk = capital * (self.config["max_concurrent_open_risk_pct"] / 100.0)
            headroom = max_open_risk - self._committed_risk()
            lots_by_headroom = int(headroom // per_lot_risk) if headroom > 0 else 0

            # 3) lots allowed by this strategy's capital cap  (Fix #2: size down)
            lots_by_capital = lots_by_risk
            if strat_cfg:
                cap_pct = strat_cfg.get("max_share_of_capital_pct", 100)
                strat_cap = capital * (cap_pct / 100.0)
                cap_headroom = strat_cap - self._committed_capital(strategy_name)
                lots_by_capital = int(cap_headroom // per_lot_value) if cap_headroom > 0 else 0

            lots = min(lots_by_risk, lots_by_headroom, lots_by_capital)
            sized_down = lots < lots_by_risk

            if lots < min_lots:
                if lots_by_risk < min_lots:
                    reason = (f"Stop-loss too wide for your risk budget: even {min_lots} lot "
                              f"risks Rs.{per_lot_risk*min_lots:,.0f}, above the per-trade "
                              f"cap of Rs.{max_risk_amount:,.0f} "
                              f"({self.config['risk_per_trade_pct']}% of capital).")
                elif lots_by_headroom < min_lots:
                    reason = (f"Portfolio open-risk cap reached: only Rs.{max(headroom,0):,.0f} "
                              f"headroom left of Rs.{max_open_risk:,.0f}, need "
                              f"Rs.{per_lot_risk*min_lots:,.0f} for {min_lots} lot.")
                else:
                    reason = (f"'{strategy_name}' capital cap reached — not enough room for "
                              f"{min_lots} lot (needs Rs.{per_lot_value*min_lots:,.0f}).")
                self._journal("BLOCKED", strategy=strategy_name, symbol=symbol,
                              direction=direction, entry_price=entry_price,
                              stop_loss_price=stop_loss_price, reason=reason)
                return reject(reason)

            quantity = lots * lot_size
            risk_amount = lots * per_lot_risk

            reservation_id = None
            if reserve:
                reservation_id = "r" + uuid.uuid4().hex[:9]
                self.state["reservations"][reservation_id] = {
                    "strategy": strategy_name, "symbol": symbol, "direction": direction,
                    "entry_price": entry_price, "stop_loss_price": stop_loss_price,
                    "quantity": quantity, "risk_amount": risk_amount,
                    "created_at": self._now().isoformat(),
                }

            note = "ok"
            if sized_down:
                note = (f"sized down from {lots_by_risk} to {lots} lots to stay within "
                        f"portfolio/capital limits")

            self._journal("APPROVED", trade_id=reservation_id or "", strategy=strategy_name,
                          symbol=symbol, direction=direction, entry_price=entry_price,
                          stop_loss_price=stop_loss_price, quantity=quantity,
                          risk_amount=risk_amount, reason=note)

            return {"approved": True, "quantity": quantity, "lots": lots,
                    "risk_amount": round(risk_amount, 2),
                    "reservation_id": reservation_id,
                    "reason": note, "sized_down": sized_down}

    def cancel_reservation(self, reservation_id, reason="order not filled"):
        """Fix #4: call this if the broker rejects/cancels the order, so the
        reserved risk is released immediately instead of waiting for expiry."""
        if not reservation_id:
            return False
        with self._locked():
            r = self.state["reservations"].pop(reservation_id, None)
            if r is None:
                return False
            self._journal("RESERVATION_CANCELLED", trade_id=reservation_id,
                          strategy=r["strategy"], symbol=r["symbol"],
                          risk_amount=r["risk_amount"], reason=reason)
            return True

    # ------------------------------------------------------------------ #
    # Trade lifecycle
    # ------------------------------------------------------------------ #

    def record_open(self, strategy_name, symbol, direction, entry_price,
                     stop_loss_price, quantity, reservation_id=None,
                     trade_id=None, allow_without_reservation=False):
        """
        Call AFTER the broker confirms the fill.

        Pass the reservation_id from size_trade(). Fix #5: the quantity is
        validated against what was approved — you cannot silently record a
        bigger position than the risk layer allowed.

        Partial fills are fine: pass the actual filled quantity (must be <=
        the reserved quantity) and the leftover reserved risk is released.
        """
        with self._locked():
            res = None
            if reservation_id:
                res = self.state["reservations"].pop(reservation_id, None)
                if res is None:
                    raise ValueError(
                        f"Reservation {reservation_id} not found — it may have expired "
                        f"(TTL {RESERVATION_TTL_SECONDS}s) or already been used. "
                        f"Do NOT record this trade blindly; re-run size_trade() to "
                        f"re-check limits against current exposure."
                    )
                if quantity > res["quantity"]:
                    self.state["reservations"][reservation_id] = res  # put it back
                    raise ValueError(
                        f"Filled quantity {quantity} exceeds approved quantity "
                        f"{res['quantity']} for reservation {reservation_id}. "
                        f"Refusing to record — this would breach the risk limit."
                    )
                if quantity <= 0:
                    self._journal("RESERVATION_CANCELLED", trade_id=reservation_id,
                                  strategy=strategy_name, symbol=symbol,
                                  reason="zero fill")
                    return None
            elif not allow_without_reservation:
                raise ValueError(
                    "record_open() called without a reservation_id. Call size_trade() "
                    "first and pass its reservation_id, or set "
                    "allow_without_reservation=True if you are deliberately importing "
                    "a position opened outside this risk layer."
                )

            trade_id = trade_id or "t" + uuid.uuid4().hex[:9]
            if trade_id in self.state["open_positions"]:
                raise ValueError(f"trade_id {trade_id} is already an open position")

            risk_amount = abs(entry_price - stop_loss_price) * quantity
            self.state["open_positions"][trade_id] = {
                "strategy": strategy_name, "symbol": symbol,
                "direction": direction, "entry_price": entry_price,
                "stop_loss_price": stop_loss_price, "quantity": quantity,
                "risk_amount": risk_amount,
                "opened_at": self._now().isoformat(),
            }
            self._journal("OPENED", trade_id=trade_id, strategy=strategy_name,
                          symbol=symbol, direction=direction, entry_price=entry_price,
                          stop_loss_price=stop_loss_price, quantity=quantity,
                          risk_amount=risk_amount,
                          reason=(f"partial fill {quantity}/{res['quantity']}"
                                  if res and quantity < res["quantity"] else ""))
            return trade_id

    def record_close(self, trade_id, exit_price, quantity=None):
        """
        Call AFTER the broker confirms the exit. Pass `quantity` for a partial
        exit — the remaining position stays open with its risk reduced.
        Returns realized P&L for the closed portion.
        """
        with self._locked():
            pos = self.state["open_positions"].get(trade_id)
            if pos is None:
                raise KeyError(f"No open position with trade_id={trade_id}")

            close_qty = pos["quantity"] if quantity is None else quantity
            if close_qty <= 0 or close_qty > pos["quantity"]:
                raise ValueError(
                    f"Invalid close quantity {close_qty} for position of "
                    f"{pos['quantity']} units"
                )

            mult = 1 if pos["direction"].upper() == "BUY" else -1
            pnl = (exit_price - pos["entry_price"]) * close_qty * mult
            self.state["realized_pnl_today"] += pnl

            if close_qty == pos["quantity"]:
                self.state["open_positions"].pop(trade_id)
                self.state["marks"].pop(trade_id, None)
                partial_note = ""
            else:
                pos["quantity"] -= close_qty
                pos["risk_amount"] = abs(pos["entry_price"] - pos["stop_loss_price"]) * pos["quantity"]
                partial_note = f"partial exit; {pos['quantity']} units still open"

            self._journal("CLOSED", trade_id=trade_id, strategy=pos["strategy"],
                          symbol=pos["symbol"], direction=pos["direction"],
                          entry_price=pos["entry_price"], exit_price=exit_price,
                          quantity=close_qty, pnl=round(pnl, 2), reason=partial_note)

            self._check_kill_switch()
            return round(pnl, 2)

    def update_mark(self, trade_id, current_price):
        """Optional (Fix #9). Feed live prices for open positions and the kill
        switch will also account for unrealized loss, not just realized.
        Call it from your engine's price-tick loop, e.g. once a minute."""
        with self._locked():
            if trade_id not in self.state["open_positions"]:
                return False
            self.state["marks"][trade_id] = current_price
            self._check_kill_switch()
            return True

    def import_existing_position(self, strategy_name, symbol, direction,
                                  entry_price, stop_loss_price, quantity):
        """For positions opened before this layer existed, or manually.
        Counts toward exposure limits from now on."""
        return self.record_open(strategy_name, symbol, direction, entry_price,
                                 stop_loss_price, quantity,
                                 allow_without_reservation=True)

    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #

    def get_status(self):
        with self._locked():
            capital = self.config["total_capital"]
            realized = self.state["realized_pnl_today"]
            unrealized = self._unrealized_pnl()
            marked = sum(1 for t in self.state["open_positions"] if t in self.state["marks"])
            return {
                "trading_day": self.state["trading_day"],
                "engine_active": self.state.get("engine_active", False),
                "total_capital": capital,
                "realized_pnl_today": round(realized, 2),
                "realized_pnl_today_pct": round(100 * realized / capital, 3),
                "unrealized_pnl": round(unrealized, 2),
                "positions_with_live_marks": f"{marked}/{len(self.state['open_positions'])}",
                "blocked_for_today": self.state["blocked_for_today"],
                "block_reason": self.state["block_reason"],
                "open_positions_count": len(self.state["open_positions"]),
                "active_reservations": len(self.state["reservations"]),
                "committed_risk": round(self._committed_risk(), 2),
                "max_open_risk_amount": round(
                    capital * self.config["max_concurrent_open_risk_pct"] / 100.0, 2),
                "max_daily_loss_amount": round(
                    capital * self.config["max_daily_loss_pct"] / 100.0, 2),
                "capital_deployed": round(self._committed_capital(), 2),
                "capital_by_strategy": {
                    s: round(self._committed_capital(s), 2)
                    for s in self.config.get("strategies", {})
                },
            }

    def list_open_positions(self):
        with self._locked():
            return dict(self.state["open_positions"])

    def list_recent_journal(self, limit=50):
        """Every trade decision — approved, blocked, opened, closed, engine
        on/off — most recent first. This is what 'did it take a trade or
        not' actually means: read from here, not just open_positions."""
        if not self.journal_path.exists():
            return []
        with open(self.journal_path, "r", newline="") as f:
            rows = list(csv.DictReader(f))
        return list(reversed(rows))[:limit]

    def activate_engine(self, reason="manual activation"):
        """Turn trading ON. No trade is approved while the engine is off —
        this is the switch you control yourself, separate from the daily
        loss kill switch."""
        with self._locked():
            self.state["engine_active"] = True
            self._journal("ENGINE_ACTIVATED", reason=reason)

    def deactivate_engine(self, reason="manual deactivation"):
        """Turn trading OFF. Does not touch open positions — only blocks
        NEW trades from being approved."""
        with self._locked():
            self.state["engine_active"] = False
            self._journal("ENGINE_DEACTIVATED", reason=reason)

    def is_engine_active(self):
        with self._locked():
            return self.state.get("engine_active", False)

    def force_unblock(self, reason="manual override"):
        """Manual override of today's block. Use with care — the limit exists
        for a reason, and overriding it after a bad day is exactly the habit
        this module is meant to prevent."""
        with self._locked():
            self.state["blocked_for_today"] = False
            self.state["block_reason"] = None
            self._journal("MANUAL_UNBLOCK", reason=reason)
