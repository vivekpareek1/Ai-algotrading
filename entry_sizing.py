#!/usr/bin/env python3
"""
entry_sizing.py — reconcile the underlying-based stop with premium-based sizing.

THE PROBLEM THIS SOLVES
-----------------------
Two modules disagree about where the stop lives:

  exit_manager  stops on the UNDERLYING (ATR-based). Correct choice: option
                premium is noisy, and trailing on it gets you stopped by IV
                wobble rather than by the trade actually failing.

  risk_manager  sizes on the PREMIUM stop distance, because that is what you
                actually pay and actually lose.

Wire them together naively and the number on your dashboard becomes fiction:
it says "risk Rs.14,000 on this trade" while the real loss at stop-out is
something else entirely. Every limit built on top of it — per-trade risk, the
portfolio open-risk cap, the daily loss kill switch — is then computed from a
wrong input. That is worse than having no limit, because it looks fine.

HOW THE LOSS IS ACTUALLY BOUNDED
--------------------------------
An open position has TWO stops running at once:

  1. structure stop  — underlying moves `initial_sl_atr_mult * ATR` against you
  2. premium hard stop — premium falls `premium_hard_sl_pct` from entry

Whichever is reached FIRST ends the trade. So the realised loss is bounded by
the TIGHTER of the two, expressed in premium terms. Sizing on the tighter one
is what makes `risk_amount` mean what it says.

Sizing on the wider one would be "safer" only in the sense of trading smaller —
it would systematically under-deploy and quietly reserve risk that can never be
lost, which distorts the portfolio cap in the other direction.

THE DELTA ASSUMPTION, AND WHY IT LEANS HIGH
-------------------------------------------
Converting an underlying move into a premium move needs delta. ATM options run
near 0.5, but delta is not fixed: it drifts up as the option goes into the
money, and that drift happens precisely on the trades that move.

The asymmetry matters. If real delta is 0.6 and we assume 0.5, we UNDERSTATE
the premium loss from a given underlying move, and therefore OVERSIZE the
position — the one error that breaches the risk budget. Assuming slightly high
costs a little size; assuming low costs money. So the default leans high, and
that is a deliberate bias, not a guess at the true value.

This remains an approximation. The only way to remove it is to read live option
Greeks (Angel One exposes optionGreek) — a worthwhile upgrade, but it should
not silently block sizing when that call fails.

NOT A BACKTESTED CONSTANT
-------------------------
Every number here is a reasoned default, not one fitted to your data. They are
in one dataclass so they can be calibrated against real collected snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class SizingConfig:
    # Delta used to convert an underlying move into a premium move.
    # Deliberately above the ~0.50 ATM value — see the module docstring for
    # why the error is asymmetric and which way it should lean.
    assumed_delta: float = 0.58

    # Must mirror ExitConfig. Passed in rather than imported so this module
    # stays usable standalone, but they have to agree — verify_against_exit_config()
    # exists to catch drift between the two.
    initial_sl_atr_mult: float = 1.5
    premium_hard_sl_pct: float = 0.35

    # An option premium cannot go below zero, and a stop priced at a few paise
    # is not executable. Floor the stop so sizing never divides by ~0 and
    # produces an absurd quantity.
    min_premium_stop: float = 2.0

    def validate(self) -> None:
        if not 0 < self.assumed_delta <= 1:
            raise ValueError("assumed_delta must be in (0, 1]")
        if self.initial_sl_atr_mult <= 0:
            raise ValueError("initial_sl_atr_mult must be positive")
        if not 0 < self.premium_hard_sl_pct < 1:
            raise ValueError("premium_hard_sl_pct must be in (0, 1)")
        if self.min_premium_stop <= 0:
            raise ValueError("min_premium_stop must be positive")

    def verify_against_exit_config(self, exit_cfg: Any) -> None:
        """Fail loudly if exit_manager's numbers have drifted from these.
        A silent mismatch here is exactly the bug this module exists to prevent."""
        mismatches = []
        for field in ("initial_sl_atr_mult", "premium_hard_sl_pct"):
            mine = getattr(self, field)
            theirs = getattr(exit_cfg, field, None)
            if theirs is not None and abs(mine - theirs) > 1e-9:
                mismatches.append(f"{field}: sizing={mine} exit_manager={theirs}")
        if mismatches:
            raise ValueError(
                "SizingConfig and ExitConfig disagree, so the reserved risk would "
                "not match the real stop:\n  " + "\n  ".join(mismatches))


@dataclass
class StopPlan:
    """The stop levels a trade will actually run with, plus the premium stop
    that position sizing should use."""
    entry_premium: float
    entry_underlying: float
    underlying_stop: float
    premium_stop_for_sizing: float
    premium_stop_distance: float
    binding_constraint: str          # which stop is expected to trigger first
    structure_stop_premium: float
    hard_stop_premium: float

    def as_dict(self) -> Dict[str, Any]:
        return {
            "entry_premium": round(self.entry_premium, 2),
            "entry_underlying": round(self.entry_underlying, 2),
            "underlying_stop": round(self.underlying_stop, 2),
            "premium_stop_for_sizing": round(self.premium_stop_for_sizing, 2),
            "premium_stop_distance": round(self.premium_stop_distance, 2),
            "binding_constraint": self.binding_constraint,
        }


def plan_stops(entry_premium: float, entry_underlying: float, atr: float,
               is_call: bool, config: Optional[SizingConfig] = None) -> StopPlan:
    """
    Work out both stops for a new trade and return the premium stop that
    position sizing must use.

    is_call: True for a CE (bullish), False for a PE (bearish). Determines
             which way the underlying stop sits relative to entry.
    """
    cfg = config or SizingConfig()
    cfg.validate()

    if entry_premium <= 0:
        raise ValueError("entry_premium must be positive")
    if entry_underlying <= 0:
        raise ValueError("entry_underlying must be positive")
    if atr <= 0:
        raise ValueError("atr must be positive — cannot place an ATR stop without it")

    sign = 1 if is_call else -1

    # 1. Structure stop: where exit_manager will actually put the underlying stop.
    underlying_stop_distance = cfg.initial_sl_atr_mult * atr
    underlying_stop = entry_underlying - sign * underlying_stop_distance

    # 2. Same distance expressed in premium terms.
    structure_stop_premium_distance = underlying_stop_distance * cfg.assumed_delta

    # 3. The premium hard stop, which runs in parallel.
    hard_stop_premium_distance = entry_premium * cfg.premium_hard_sl_pct

    # 4. Whichever is reached first is the one that actually ends the trade,
    #    so that is the distance sizing must be based on.
    if structure_stop_premium_distance <= hard_stop_premium_distance:
        binding = "structure stop (underlying ATR)"
        premium_stop_distance = structure_stop_premium_distance
    else:
        binding = "premium hard stop"
        premium_stop_distance = hard_stop_premium_distance

    premium_stop_distance = max(premium_stop_distance, cfg.min_premium_stop)
    # ...and it can never imply a stop at or below zero premium.
    premium_stop_distance = min(premium_stop_distance, entry_premium * 0.99)

    return StopPlan(
        entry_premium=entry_premium,
        entry_underlying=entry_underlying,
        underlying_stop=underlying_stop,
        premium_stop_for_sizing=entry_premium - premium_stop_distance,
        premium_stop_distance=premium_stop_distance,
        binding_constraint=binding,
        structure_stop_premium=entry_premium - structure_stop_premium_distance,
        hard_stop_premium=entry_premium - hard_stop_premium_distance,
    )
