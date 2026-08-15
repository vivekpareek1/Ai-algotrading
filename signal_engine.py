#!/usr/bin/env python3
"""
signal_engine.py — entry decision from market structure, not price alone.

WHAT WAS WRONG WITH THE OLD ENTRY LOGIC
---------------------------------------
The previous confluence used EMA stack + VWAP + RSI + MACD + ADX. All five are
derived from the SAME index candles. They agree with each other most of the
time, so "5 of 5 confluence" is really closer to 2 independent opinions wearing
five hats. That is why the backtest sat at ~48% win rate: the vote was never as
independent as the count implied.

This engine adds inputs that are genuinely independent of the price series:

  futures basis   -> what positioned money is paying to hold (direction)
  OI buildup      -> whether a move has fresh money behind it (conviction)
  max-OI strikes  -> where the option writers have drawn support/resistance
  PCR extremes    -> crowding, used only as a veto at the tails

DESIGN: VETOES, NOT VOTES
-------------------------
Adding more indicators to an additive score makes it *easier* to reach the
threshold, not harder — the weak inputs get carried by the strong ones. That is
backwards for a filter. So structure inputs act as VETOES: price indicators
propose a direction, and structure can refuse it. A trade needs price to say go
AND no structural reason to say don't.

Expect far fewer trades than the old logic. That is the point. Fewer, cleaner.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
* No FII/DII input. NSE publishes those figures end-of-day only — there is no
  intraday feed, from Angel One or anyone else. Anything sold as "live FII data"
  is a derived guess. It can be added later as a NEXT-DAY bias flag, but it
  cannot inform an intraday entry, so it is honestly absent rather than faked.
* No backtest. Angel One does not retain expired-contract OI, which is the whole
  reason oi_collector.py exists. This logic can only be forward-tested. Any
  backtest of it would be fabricated data validating itself.

INPUTS
------
Reads what oi_collector.py already writes:
  data/NIFTY/summary_YYYY-MM-DD.csv   (spot, basis, PCR, max-OI strikes)
  data/NIFTY/chain_YYYY-MM-DD.csv     (per-strike OI, for buildup deltas)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd


# ----------------------------------------------------------------------------
# TYPES
# ----------------------------------------------------------------------------

class Direction(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NONE = "NONE"


class Buildup(str, Enum):
    """Price and OI moving together tells you who is initiating."""
    LONG_BUILDUP = "LONG_BUILDUP"        # price up,   OI up   -> strong bullish
    SHORT_BUILDUP = "SHORT_BUILDUP"      # price down, OI up   -> strong bearish
    SHORT_COVERING = "SHORT_COVERING"    # price up,   OI down -> weak bullish
    LONG_UNWINDING = "LONG_UNWINDING"    # price down, OI down -> weak bearish
    FLAT = "FLAT"


@dataclass
class SignalConfig:
    # --- futures basis ------------------------------------------------------
    # Basis is noisy in absolute points; normalise by spot. Nifty fair-value
    # carry is small, so these are deliberately tight.
    basis_bullish_pct: float = 0.03      # fut premium above this = bullish tilt
    basis_bearish_pct: float = -0.03     # discount below this   = bearish tilt

    # --- OI buildup ---------------------------------------------------------
    oi_change_min_pct: float = 1.0       # ignore OI noise below this % change
    price_change_min_pct: float = 0.05   # ignore price noise below this

    # --- support / resistance from max-OI walls -----------------------------
    # Do not buy a CE into a call wall that is too close to be worth the trade.
    wall_min_distance_atr: float = 1.0   # need this much room to the wall

    # --- PCR crowding veto --------------------------------------------------
    pcr_extreme_high: float = 1.6        # too many puts -> avoid fresh PE
    pcr_extreme_low: float = 0.55        # too many calls -> avoid fresh CE

    # --- lookback -----------------------------------------------------------
    oi_lookback_snapshots: int = 6       # 6 x 5min = 30 min of buildup

    def validate(self) -> None:
        if self.basis_bearish_pct >= self.basis_bullish_pct:
            raise ValueError("basis bearish threshold must be below bullish")
        if self.pcr_extreme_low >= self.pcr_extreme_high:
            raise ValueError("pcr_extreme_low must be below pcr_extreme_high")
        if self.oi_lookback_snapshots < 2:
            raise ValueError("need at least 2 snapshots to measure OI change")


@dataclass
class StructureRead:
    """Everything the engine concluded from market structure, for logging.
    Every field is here so a rejected trade can be explained after the fact."""
    spot: float
    basis: Optional[float]
    basis_pct: Optional[float]
    basis_bias: Direction
    buildup: Buildup
    fut_oi_change_pct: Optional[float]
    price_change_pct: Optional[float]
    pcr_oi: Optional[float]
    support: Optional[float]
    resistance: Optional[float]
    room_to_resistance: Optional[float]
    room_to_support: Optional[float]


@dataclass
class SignalDecision:
    take_trade: bool
    direction: Direction
    option_type: Optional[str]           # "CE" / "PE"
    reason: str
    vetoes: List[str] = field(default_factory=list)
    structure: Optional[StructureRead] = None

    def __str__(self) -> str:
        head = (f"{self.option_type or 'NO TRADE'}: {self.reason}"
                if self.take_trade else f"NO TRADE: {self.reason}")
        if self.vetoes:
            head += "\n  vetoes: " + "; ".join(self.vetoes)
        return head


# ----------------------------------------------------------------------------
# STRUCTURE READING
# ----------------------------------------------------------------------------

class StructureAnalyzer:
    """Turns collector CSVs into a structural read. No trading decisions here —
    this only describes what the market looks like."""

    def __init__(self, config: Optional[SignalConfig] = None):
        self.cfg = config or SignalConfig()
        self.cfg.validate()

    def read_summary(self, summary_df: pd.DataFrame) -> StructureRead:
        """summary_df: the collector's summary_*.csv, oldest row first."""
        if summary_df.empty:
            raise ValueError("summary data is empty — collector has not run yet")

        df = summary_df.sort_values("timestamp")
        latest = df.iloc[-1]

        spot = float(latest["spot"])
        basis = self._opt_float(latest.get("basis"))
        basis_pct = self._opt_float(latest.get("basis_pct"))
        pcr_oi = self._opt_float(latest.get("pcr_oi"))

        # --- basis bias -----------------------------------------------------
        basis_bias = Direction.NONE
        if basis_pct is not None:
            if basis_pct >= self.cfg.basis_bullish_pct:
                basis_bias = Direction.BULLISH
            elif basis_pct <= self.cfg.basis_bearish_pct:
                basis_bias = Direction.BEARISH

        # --- futures OI buildup over the lookback window ---------------------
        lookback = min(self.cfg.oi_lookback_snapshots, len(df))
        past = df.iloc[-lookback]
        buildup, oi_chg, px_chg = self._classify_buildup(past, latest)

        # --- support / resistance from max-OI walls -------------------------
        resistance = self._opt_float(latest.get("max_ce_oi_strike"))
        support = self._opt_float(latest.get("max_pe_oi_strike"))
        room_up = (resistance - spot) if resistance is not None else None
        room_dn = (spot - support) if support is not None else None

        return StructureRead(
            spot=spot, basis=basis, basis_pct=basis_pct, basis_bias=basis_bias,
            buildup=buildup, fut_oi_change_pct=oi_chg, price_change_pct=px_chg,
            pcr_oi=pcr_oi, support=support, resistance=resistance,
            room_to_resistance=room_up, room_to_support=room_dn,
        )

    def _classify_buildup(self, past: pd.Series, now: pd.Series):
        """Price vs futures-OI over the window. This is the conviction read:
        it separates a move backed by fresh positions from one that is just
        old positions closing."""
        past_oi = self._opt_float(past.get("fut_oi"))
        now_oi = self._opt_float(now.get("fut_oi"))
        past_px = self._opt_float(past.get("spot"))
        now_px = self._opt_float(now.get("spot"))

        if None in (past_oi, now_oi, past_px, now_px) or past_oi <= 0 or past_px <= 0:
            return Buildup.FLAT, None, None

        oi_chg = (now_oi - past_oi) / past_oi * 100.0
        px_chg = (now_px - past_px) / past_px * 100.0

        if (abs(oi_chg) < self.cfg.oi_change_min_pct
                or abs(px_chg) < self.cfg.price_change_min_pct):
            return Buildup.FLAT, round(oi_chg, 3), round(px_chg, 3)

        if px_chg > 0 and oi_chg > 0:
            b = Buildup.LONG_BUILDUP
        elif px_chg < 0 and oi_chg > 0:
            b = Buildup.SHORT_BUILDUP
        elif px_chg > 0 and oi_chg < 0:
            b = Buildup.SHORT_COVERING
        else:
            b = Buildup.LONG_UNWINDING

        return b, round(oi_chg, 3), round(px_chg, 3)

    @staticmethod
    def _opt_float(v: Any) -> Optional[float]:
        try:
            if v is None or v == "" or pd.isna(v):
                return None
            return float(v)
        except (TypeError, ValueError):
            return None


# ----------------------------------------------------------------------------
# DECISION
# ----------------------------------------------------------------------------

class SignalEngine:
    """Price proposes, structure vetoes."""

    def __init__(self, config: Optional[SignalConfig] = None):
        self.cfg = config or SignalConfig()
        self.cfg.validate()
        self.analyzer = StructureAnalyzer(self.cfg)

    def decide(self, price_direction: Direction, structure: StructureRead,
               atr: float, orb_direction: Direction = Direction.NONE) -> SignalDecision:
        """
        price_direction: what the existing EMA/RSI/MACD/ADX confluence concluded.
        structure:       output of StructureAnalyzer.
        atr:             current ATR on the underlying, for wall-distance sizing.
        orb_direction:    output of orb_strategy.classify_breakout(), or
                          Direction.NONE if the opening range isn't complete yet,
                          or if orb_strategy isn't wired in by the caller at all.
                          Passed in rather than computed here to avoid a circular
                          import (orb_strategy imports Direction from this module).
        """
        if price_direction is Direction.NONE:
            return SignalDecision(False, Direction.NONE, None,
                                  "price indicators show no clear direction",
                                  structure=structure)

        vetoes: List[str] = []
        bullish = price_direction is Direction.BULLISH

        # --- VETO 1: futures basis contradicts the price signal --------------
        # The strongest single filter. Positioned money paying a premium while
        # price says sell (or vice versa) means one of them is wrong, and it is
        # usually not the money.
        if structure.basis_bias is not Direction.NONE:
            if structure.basis_bias is not price_direction:
                vetoes.append(
                    f"futures basis is {structure.basis_bias.value.lower()} "
                    f"({structure.basis_pct:+.3f}%) against a "
                    f"{price_direction.value.lower()} price signal")

        # --- VETO 2: buildup shows no fresh money ---------------------------
        # Short covering and long unwinding are exits, not initiations. They
        # move price without conviction and mean-revert quickly.
        weak_bull = {Buildup.SHORT_COVERING, Buildup.LONG_UNWINDING,
                     Buildup.SHORT_BUILDUP}
        weak_bear = {Buildup.SHORT_COVERING, Buildup.LONG_UNWINDING,
                     Buildup.LONG_BUILDUP}
        if bullish and structure.buildup in weak_bull:
            vetoes.append(f"OI shows {structure.buildup.value.lower()}, "
                          f"not fresh long buildup")
        if not bullish and structure.buildup in weak_bear:
            vetoes.append(f"OI shows {structure.buildup.value.lower()}, "
                          f"not fresh short buildup")
        if structure.buildup is Buildup.FLAT:
            vetoes.append("OI flat — move has no positioning behind it")

        # --- VETO 3: an OI wall sits in the way ------------------------------
        # Writers defend max-OI strikes. Buying a CE just under a heavy call
        # wall is buying into the exact level where the move is expected to die.
        need = self.cfg.wall_min_distance_atr * atr
        if bullish and structure.room_to_resistance is not None:
            if structure.room_to_resistance < need:
                vetoes.append(
                    f"call wall at {structure.resistance:.0f} is only "
                    f"{structure.room_to_resistance:.0f} pts away "
                    f"(need {need:.0f})")
        if not bullish and structure.room_to_support is not None:
            if structure.room_to_support < need:
                vetoes.append(
                    f"put wall at {structure.support:.0f} is only "
                    f"{structure.room_to_support:.0f} pts away "
                    f"(need {need:.0f})")

        # --- VETO 4: crowded at an extreme -----------------------------------
        # Only meaningful at the tails; ignored in the normal band.
        if structure.pcr_oi is not None:
            if bullish and structure.pcr_oi <= self.cfg.pcr_extreme_low:
                vetoes.append(f"PCR {structure.pcr_oi:.2f} is call-crowded — "
                              f"poor risk/reward for a fresh CE")
            if not bullish and structure.pcr_oi >= self.cfg.pcr_extreme_high:
                vetoes.append(f"PCR {structure.pcr_oi:.2f} is put-crowded — "
                              f"poor risk/reward for a fresh PE")

        # --- VETO 5: opening range not broken yet -----------------------------
        # ORB is a genuinely price-only signal (no OI/basis involved), so it
        # slots in as its own veto rather than being folded into the price
        # confluence itself — keeps the two strategies independently
        # inspectable in the decision trail instead of blended into one score.
        if orb_direction is Direction.NONE:
            vetoes.append("opening range not yet broken (or still forming) — "
                          "no ORB confirmation for this direction")
        elif orb_direction is not price_direction:
            vetoes.append(f"ORB shows {orb_direction.value.lower()}, "
                          f"contradicting the {price_direction.value.lower()} "
                          f"price signal")

        if vetoes:
            return SignalDecision(False, Direction.NONE, None,
                                  f"{price_direction.value.lower()} price signal "
                                  f"rejected by market structure",
                                  vetoes=vetoes, structure=structure)

        opt = "CE" if bullish else "PE"
        target = (structure.resistance if bullish else structure.support)
        target_txt = f", first obstacle {target:.0f}" if target else ""
        return SignalDecision(
            True, price_direction, opt,
            f"{price_direction.value.lower()} price signal confirmed by "
            f"{structure.buildup.value.lower()} and "
            f"{structure.basis_bias.value.lower() if structure.basis_bias is not Direction.NONE else 'neutral'} "
            f"basis{target_txt}",
            structure=structure)

    # -- convenience ---------------------------------------------------------

    def decide_from_files(self, data_dir: Path, underlying: str, day: str,
                          price_direction: Direction, atr: float) -> SignalDecision:
        path = Path(data_dir) / underlying / f"summary_{day}.csv"
        if not path.exists():
            return SignalDecision(False, Direction.NONE, None,
                                  f"no collector data at {path} — "
                                  f"is oi_collector running?")
        df = pd.read_csv(path)
        structure = self.analyzer.read_summary(df)
        return self.decide(price_direction, structure, atr)
