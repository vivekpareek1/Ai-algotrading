#!/usr/bin/env python3
"""
exit_manager.py — stop loss, momentum-adaptive trailing, and profit booking.

WHY THIS IS A SEPARATE MODULE
-----------------------------
Entry logic decides *if* you trade. This decides *how much you keep*. For
intraday option buying the second one matters more: a 48%-win-rate entry can be
profitable or ruinous purely depending on exits.

THE FOUR EXIT PATHS (checked in this priority order)
----------------------------------------------------
  1. HARD PREMIUM STOP  — premium collapsed past a fixed %, regardless of the
     underlying. Protects against IV crush / gap / spread blowout, where the
     underlying looks fine but your option is dead.
  2. STRUCTURE STOP     — the underlying hit the trailing stop level.
  3. MOMENTUM EXIT      — in profit, but momentum has decayed. Book it.
  4. TIME STOP / EOD    — theta is bleeding a position that isn't working, or
     the square-off clock is up.

WHY THE STOP IS TRACKED ON THE UNDERLYING, NOT THE PREMIUM
-----------------------------------------------------------
Option premium is noisy: IV shifts and spreads move it several percent without
the index moving at all. Trailing on premium gets you stopped out by noise.
So the trail is computed on the index/future, and converted to a premium-side
emergency backstop only (path 1 above).

MOMENTUM-ADAPTIVE TRAILING
--------------------------
Trail distance is NOT constant. It is `atr * multiplier`, and the multiplier
shrinks as momentum decays:

    strong momentum  -> wide trail  (3.0x ATR) -> let the move breathe
    neutral          -> medium      (2.0x ATR)
    weak             -> tight       (1.0x ATR) -> lock in the gain

This is the behaviour you asked for: hold while the move is strong, tighten
and book where it starts weakening.

MOMENTUM SCORE (0.0 - 1.0), five equally weighted components
-------------------------------------------------------------
  - EMA alignment and slope in the trade's direction
  - ADX rising and above threshold (trend strength)
  - RSI in the trending band, not exhausted or reversing
  - Volume participation vs its own average
  - Structure: still making favourable extremes (no stalling)

INTEGRATION
-----------
Standalone and stateless per call. `evaluate()` takes a Position + MarketState
and returns an ExitDecision. Caller applies it and persists. Plugs in alongside
risk_manager.py — this sizes nothing, it only manages open trades.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, time as dtime
from enum import Enum
from typing import Any, Dict, List, Optional


# ----------------------------------------------------------------------------
# ENUMS
# ----------------------------------------------------------------------------

class Side(str, Enum):
    """Both are long-option trades; the sign is the directional view."""
    LONG_CE = "LONG_CE"      # bullish — favourable = underlying up
    LONG_PE = "LONG_PE"      # bearish — favourable = underlying down

    @property
    def sign(self) -> int:
        return 1 if self is Side.LONG_CE else -1


class Action(str, Enum):
    HOLD = "HOLD"
    UPDATE_SL = "UPDATE_SL"
    EXIT_PARTIAL = "EXIT_PARTIAL"
    EXIT_FULL = "EXIT_FULL"


class Momentum(str, Enum):
    STRONG = "STRONG"
    NEUTRAL = "NEUTRAL"
    WEAK = "WEAK"


# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

@dataclass
class ExitConfig:
    # --- initial stop -------------------------------------------------------
    initial_sl_atr_mult: float = 1.5      # SL distance = 1.5 x ATR on underlying

    # --- trailing -----------------------------------------------------------
    breakeven_at_r: float = 1.0           # move SL to entry once +1R is reached
    trail_start_r: float = 1.2            # begin trailing after this
    trail_atr_strong: float = 3.0         # wide  — let winners run
    trail_atr_neutral: float = 2.0
    trail_atr_weak: float = 1.0           # tight — protect the gain
    breakeven_buffer_atr: float = 0.1     # park BE slightly in profit, covers costs

    # --- partial booking ----------------------------------------------------
    partial_book_r: float = 1.5           # book part of the position here
    partial_book_pct: float = 0.50        # 50% off the table

    # --- momentum exit ------------------------------------------------------
    momentum_weak_below: float = 0.35     # score under this = WEAK
    momentum_strong_above: float = 0.65   # score over this  = STRONG
    momentum_exit_min_r: float = 0.8      # only book on weakness if this far up
    momentum_exit_score: float = 0.25     # decisive weakness -> exit now

    # --- premium backstop ---------------------------------------------------
    premium_hard_sl_pct: float = 0.35     # exit if premium down 35% from entry
    premium_trail_giveback_pct: float = 0.50  # min peak-profit giveback to consider
    iv_crush_ratio: float = 1.6           # ...and premium must bleed this many
                                          # times faster than the index retraced

    # --- time -------------------------------------------------------------
    time_stop_minutes: int = 45           # not working after 45 min -> theta bleed
    time_stop_min_r: float = 0.4          # "not working" = below this R
    eod_square_off: dtime = dtime(15, 15)

    def validate(self) -> None:
        if not 0 < self.partial_book_pct < 1:
            raise ValueError("partial_book_pct must be between 0 and 1")
        if self.trail_atr_weak > self.trail_atr_neutral > self.trail_atr_strong:
            raise ValueError("trail multipliers must widen with strength")
        if self.momentum_weak_below >= self.momentum_strong_above:
            raise ValueError("weak threshold must be below strong threshold")
        if self.trail_start_r < self.breakeven_at_r:
            raise ValueError("cannot trail before breakeven")


# ----------------------------------------------------------------------------
# STATE
# ----------------------------------------------------------------------------

@dataclass
class MarketState:
    """One bar of live/backtest data. All fields on the UNDERLYING except premium."""
    timestamp: datetime
    underlying: float
    premium: float
    atr: float
    ema_fast: float
    ema_slow: float
    rsi: float
    adx: float
    prev_adx: float
    volume: float
    avg_volume: float
    ema_fast_prev: Optional[float] = None   # for slope


@dataclass
class Position:
    position_id: str
    symbol: str
    side: Side
    entry_time: datetime
    entry_underlying: float
    entry_premium: float
    quantity: int
    atr_at_entry: float

    # mutable state — caller must persist these between calls
    sl_underlying: Optional[float] = None
    initial_sl_underlying: Optional[float] = None   # frozen: defines 1R forever
    peak_underlying: Optional[float] = None    # best favourable excursion
    peak_premium: Optional[float] = None
    partial_booked: bool = False
    remaining_quantity: Optional[int] = None
    breakeven_moved: bool = False
    history: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.entry_premium <= 0:
            raise ValueError("entry_premium must be positive")
        if self.atr_at_entry <= 0:
            raise ValueError("atr_at_entry must be positive")
        if self.remaining_quantity is None:
            self.remaining_quantity = self.quantity
        if self.peak_underlying is None:
            self.peak_underlying = self.entry_underlying
        if self.peak_premium is None:
            self.peak_premium = self.entry_premium

    @property
    def risk_points(self) -> float:
        """
        1R in underlying points = the ORIGINAL stop distance.

        Deliberately NOT derived from sl_underlying: once the stop trails, that
        distance shrinks, and an R computed from it would inflate without limit
        (a 60-point risk trailed to 5 points would report a 2R move as 24R).
        R must stay anchored to the risk actually taken at entry.
        """
        if self.initial_sl_underlying is None:
            raise ValueError("initial stop not set")
        return abs(self.entry_underlying - self.initial_sl_underlying)


@dataclass
class ExitDecision:
    action: Action
    reason: str
    momentum: Momentum
    momentum_score: float
    r_multiple: float
    new_sl: Optional[float] = None
    exit_quantity: int = 0
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["action"] = self.action.value
        d["momentum"] = self.momentum.value
        return d


# ----------------------------------------------------------------------------
# CORE
# ----------------------------------------------------------------------------

class ExitManager:

    def __init__(self, config: Optional[ExitConfig] = None):
        self.cfg = config or ExitConfig()
        self.cfg.validate()

    # -- setup ---------------------------------------------------------------

    def set_initial_stop(self, pos: Position) -> float:
        """
        Initial SL on the underlying, ATR-based. Called once at entry.
        Wider ATR = wider stop, so volatility does not stop you out by itself.
        """
        distance = self.cfg.initial_sl_atr_mult * pos.atr_at_entry
        pos.sl_underlying = pos.entry_underlying - pos.side.sign * distance
        pos.initial_sl_underlying = pos.sl_underlying
        pos.history.append(f"initial SL {pos.sl_underlying:.2f} ({distance:.2f} pts)")
        return pos.sl_underlying

    # -- momentum ------------------------------------------------------------

    def momentum_score(self, pos: Position, m: MarketState) -> Dict[str, Any]:
        """
        Five components, each 0..1, averaged. Direction-aware: for a PE trade
        'favourable' means falling price, falling EMAs, RSI in the low band.
        """
        sign = pos.side.sign
        parts: Dict[str, float] = {}

        # 1. EMA alignment + slope in the trade's direction
        spread = (m.ema_fast - m.ema_slow) * sign
        spread_norm = spread / m.atr if m.atr > 0 else 0.0
        ema_pts = _clamp(0.5 + spread_norm, 0.0, 1.0)
        if m.ema_fast_prev is not None:
            slope = (m.ema_fast - m.ema_fast_prev) * sign
            slope_norm = slope / m.atr if m.atr > 0 else 0.0
            ema_pts = _clamp((ema_pts + _clamp(0.5 + slope_norm * 3, 0.0, 1.0)) / 2,
                             0.0, 1.0)
        parts["ema"] = ema_pts

        # 2. ADX level + direction. Rising ADX = trend gaining strength.
        adx_level = _clamp((m.adx - 15.0) / 20.0, 0.0, 1.0)   # 15->0, 35->1
        adx_rising = 1.0 if m.adx > m.prev_adx else 0.0
        parts["adx"] = _clamp(adx_level * 0.7 + adx_rising * 0.3, 0.0, 1.0)

        # 3. RSI in the trending band. Directional: mirror it for PE trades.
        rsi_dir = m.rsi if sign > 0 else 100.0 - m.rsi
        if rsi_dir >= 80:
            parts["rsi"] = 0.45          # overextended, reversal risk
        elif rsi_dir >= 60:
            parts["rsi"] = 1.0           # healthy trend zone
        elif rsi_dir >= 50:
            parts["rsi"] = 0.6
        elif rsi_dir >= 40:
            parts["rsi"] = 0.3
        else:
            parts["rsi"] = 0.0           # momentum has flipped against us

        # 4. Volume participation
        if m.avg_volume > 0:
            parts["volume"] = _clamp(m.volume / m.avg_volume / 1.5, 0.0, 1.0)
        else:
            parts["volume"] = 0.5        # index spot has no volume — stay neutral

        # 5. Structure: are we still making favourable extremes?
        excursion = (m.underlying - pos.entry_underlying) * sign
        peak_excursion = (pos.peak_underlying - pos.entry_underlying) * sign
        if peak_excursion <= 0:
            parts["structure"] = 0.5     # never went our way yet
        else:
            giveback = (peak_excursion - excursion) / peak_excursion
            parts["structure"] = _clamp(1.0 - giveback * 2.0, 0.0, 1.0)

        score = sum(parts.values()) / len(parts)

        if score >= self.cfg.momentum_strong_above:
            state = Momentum.STRONG
        elif score <= self.cfg.momentum_weak_below:
            state = Momentum.WEAK
        else:
            state = Momentum.NEUTRAL

        return {"score": round(score, 4), "state": state,
                "parts": {k: round(v, 3) for k, v in parts.items()}}

    # -- trailing ------------------------------------------------------------

    def _trail_multiplier(self, state: Momentum) -> float:
        return {
            Momentum.STRONG: self.cfg.trail_atr_strong,
            Momentum.NEUTRAL: self.cfg.trail_atr_neutral,
            Momentum.WEAK: self.cfg.trail_atr_weak,
        }[state]

    def _compute_trail_sl(self, pos: Position, m: MarketState,
                          state: Momentum, r: float) -> Optional[float]:
        """
        Chandelier-style trail from the favourable extreme, with a
        momentum-scaled distance. Returns a candidate SL, or None.
        """
        sign = pos.side.sign
        atr = m.atr if m.atr > 0 else pos.atr_at_entry

        candidate: Optional[float] = None

        # Step 1: breakeven once +1R is reached.
        if r >= self.cfg.breakeven_at_r:
            buffer = self.cfg.breakeven_buffer_atr * atr
            candidate = pos.entry_underlying + sign * buffer

        # Step 2: proper trail from the peak once past trail_start_r.
        if r >= self.cfg.trail_start_r:
            mult = self._trail_multiplier(state)
            trail = pos.peak_underlying - sign * mult * atr
            if candidate is None:
                candidate = trail
            else:
                # take whichever is tighter (further in our favour)
                candidate = max(candidate, trail) if sign > 0 else min(candidate, trail)

        return candidate

    @staticmethod
    def _is_tighter(new_sl: float, old_sl: float, sign: int) -> bool:
        """A stop may only move in the favourable direction. Never loosen."""
        return new_sl > old_sl if sign > 0 else new_sl < old_sl

    # -- main ----------------------------------------------------------------

    def evaluate(self, pos: Position, m: MarketState) -> ExitDecision:
        """
        Evaluate one open position against one bar. Mutates the position's
        peak tracking and stop level; returns the action to take.
        """
        if pos.sl_underlying is None:
            self.set_initial_stop(pos)
        elif pos.initial_sl_underlying is None:
            # caller supplied their own stop, or the position was restored from
            # disk before this field existed — anchor 1R to it now.
            pos.initial_sl_underlying = pos.sl_underlying
        if pos.remaining_quantity <= 0:
            return ExitDecision(Action.HOLD, "position already closed",
                                Momentum.NEUTRAL, 0.0, 0.0)

        sign = pos.side.sign

        # --- update favourable extremes (before any decision) ---------------
        if (m.underlying - pos.peak_underlying) * sign > 0:
            pos.peak_underlying = m.underlying
        if m.premium > pos.peak_premium:
            pos.peak_premium = m.premium

        excursion = (m.underlying - pos.entry_underlying) * sign
        risk = pos.risk_points
        r = excursion / risk if risk > 0 else 0.0

        mom = self.momentum_score(pos, m)
        state: Momentum = mom["state"]
        score: float = mom["score"]

        base = dict(momentum=state, momentum_score=score, r_multiple=round(r, 3))

        # === PATH 1: hard premium stop (highest priority) ====================
        prem_drop = (pos.entry_premium - m.premium) / pos.entry_premium
        if prem_drop >= self.cfg.premium_hard_sl_pct:
            return ExitDecision(
                Action.EXIT_FULL,
                f"premium hard stop: down {prem_drop*100:.1f}% from entry "
                f"({pos.entry_premium:.2f} -> {m.premium:.2f})",
                exit_quantity=pos.remaining_quantity, **base)

        # === PATH 2: structure stop on the underlying ========================
        hit = (m.underlying <= pos.sl_underlying if sign > 0
               else m.underlying >= pos.sl_underlying)
        if hit:
            kind = "trailing stop" if pos.breakeven_moved else "initial stop loss"
            return ExitDecision(
                Action.EXIT_FULL,
                f"{kind} hit at {pos.sl_underlying:.2f} "
                f"(underlying {m.underlying:.2f})",
                exit_quantity=pos.remaining_quantity, **base)

        # === PATH 3: momentum weakness while in profit =======================
        if r >= self.cfg.momentum_exit_min_r and score <= self.cfg.momentum_exit_score:
            weak = [k for k, v in mom["parts"].items() if v < 0.4]
            return ExitDecision(
                Action.EXIT_FULL,
                f"momentum decayed (score {score:.2f}) at +{r:.2f}R — "
                f"weak: {', '.join(weak) or 'broad'}",
                exit_quantity=pos.remaining_quantity,
                detail=mom["parts"], **base)

        # === PATH 3b: IV crush — premium bleeding faster than the index ======
        #
        # This is NOT a general "gave back some profit" rule. A trending move
        # routinely gives back 30-40% of peak profit on a pullback, and cutting
        # there would defeat the whole point of a momentum-scaled trail — the
        # structure stop above already owns that job.
        #
        # What this catches is the case the structure stop is blind to: the
        # index has barely retraced, but the option has lost far more than that
        # retracement justifies. That gap is IV collapse, spread widening, or
        # theta on a stalled move — the underlying looks fine while the position
        # quietly dies. So it fires only on DISPROPORTIONATE decay.
        peak_profit = pos.peak_premium - pos.entry_premium
        if peak_profit > 0 and m.premium > pos.entry_premium:
            prem_giveback = (pos.peak_premium - m.premium) / peak_profit
            peak_excursion = (pos.peak_underlying - pos.entry_underlying) * sign
            under_giveback = (
                ((pos.peak_underlying - m.underlying) * sign) / peak_excursion
                if peak_excursion > 0 else 0.0
            )
            disproportionate = prem_giveback >= under_giveback * self.cfg.iv_crush_ratio
            if prem_giveback >= self.cfg.premium_trail_giveback_pct and disproportionate:
                return ExitDecision(
                    Action.EXIT_FULL,
                    f"IV crush / decay: premium gave back {prem_giveback*100:.0f}% "
                    f"of peak profit while index gave back only "
                    f"{under_giveback*100:.0f}% "
                    f"(peak {pos.peak_premium:.2f} -> {m.premium:.2f})",
                    exit_quantity=pos.remaining_quantity, **base)

        # === PATH 4: time stop and EOD ======================================
        if m.timestamp.time() >= self.cfg.eod_square_off:
            return ExitDecision(
                Action.EXIT_FULL,
                f"EOD square-off at {self.cfg.eod_square_off.strftime('%H:%M')}",
                exit_quantity=pos.remaining_quantity, **base)

        held_min = (m.timestamp - pos.entry_time).total_seconds() / 60.0
        if held_min >= self.cfg.time_stop_minutes and r < self.cfg.time_stop_min_r:
            return ExitDecision(
                Action.EXIT_FULL,
                f"time stop: {held_min:.0f} min held, only +{r:.2f}R — "
                f"theta bleed",
                exit_quantity=pos.remaining_quantity, **base)

        # === partial booking =================================================
        if not pos.partial_booked and r >= self.cfg.partial_book_r:
            qty = int(pos.remaining_quantity * self.cfg.partial_book_pct)
            if qty > 0:
                pos.partial_booked = True
                pos.remaining_quantity -= qty
                # tighten to breakeven on the rest — the trade is now free
                new_sl = self._compute_trail_sl(pos, m, state, r)
                if new_sl is not None and self._is_tighter(new_sl, pos.sl_underlying, sign):
                    pos.sl_underlying = new_sl
                    pos.breakeven_moved = True
                pos.history.append(f"booked {qty} at +{r:.2f}R")
                return ExitDecision(
                    Action.EXIT_PARTIAL,
                    f"partial book {self.cfg.partial_book_pct*100:.0f}% at "
                    f"+{r:.2f}R; rest runs with SL {pos.sl_underlying:.2f}",
                    exit_quantity=qty, new_sl=pos.sl_underlying,
                    detail=mom["parts"], **base)

        # === trailing stop update ============================================
        new_sl = self._compute_trail_sl(pos, m, state, r)
        if new_sl is not None and self._is_tighter(new_sl, pos.sl_underlying, sign):
            old = pos.sl_underlying
            pos.sl_underlying = new_sl
            pos.breakeven_moved = True
            pos.history.append(f"SL {old:.2f} -> {new_sl:.2f} ({state.value})")
            return ExitDecision(
                Action.UPDATE_SL,
                f"trail SL {old:.2f} -> {new_sl:.2f} "
                f"({state.value} momentum, {self._trail_multiplier(state)}x ATR)",
                new_sl=new_sl, detail=mom["parts"], **base)

        return ExitDecision(Action.HOLD,
                            f"hold at +{r:.2f}R, {state.value} momentum "
                            f"({score:.2f}), SL {pos.sl_underlying:.2f}",
                            detail=mom["parts"], **base)


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


# ----------------------------------------------------------------------------
# DEMO
# ----------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import timedelta

    mgr = ExitManager()
    t0 = datetime(2026, 8, 12, 10, 0)
    pos = Position("P1", "NIFTY14AUG2624500CE", Side.LONG_CE, t0,
                   entry_underlying=24500, entry_premium=120,
                   quantity=150, atr_at_entry=40)
    mgr.set_initial_stop(pos)
    print(f"Entry 24500, initial SL {pos.sl_underlying:.0f}, 1R = {pos.risk_points:.0f} pts\n")

    # a trend that runs, then rolls over
    path = [
        (24510, 128, 62, 22, 21, 1.2), (24545, 152, 66, 25, 22, 1.4),
        (24580, 178, 70, 28, 25, 1.5), (24620, 210, 73, 31, 28, 1.6),
        (24660, 245, 75, 33, 31, 1.5), (24690, 268, 74, 34, 33, 1.2),
        (24675, 250, 66, 33, 34, 0.9), (24650, 228, 58, 30, 33, 0.7),
        (24630, 210, 50, 27, 30, 0.5),
    ]
    prev_ema = 24500.0
    for i, (u, p, rsi, adx, padx, volr) in enumerate(path):
        ema_f = u - 8
        m = MarketState(t0 + timedelta(minutes=5 * (i + 1)), u, p, atr=40,
                        ema_fast=ema_f, ema_slow=u - 25, rsi=rsi, adx=adx,
                        prev_adx=padx, volume=volr * 100000, avg_volume=100000,
                        ema_fast_prev=prev_ema)
        prev_ema = ema_f
        d = mgr.evaluate(pos, m)
        print(f"{m.timestamp:%H:%M} u={u} prem={p:>3} | {d.action.value:<12} "
              f"R={d.r_multiple:+.2f} mom={d.momentum.value:<7} "
              f"{d.momentum_score:.2f} | {d.reason}")
        if d.action is Action.EXIT_FULL:
            break
