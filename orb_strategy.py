#!/usr/bin/env python3
"""
orb_strategy.py — Opening Range Breakout.

THE STRATEGY
------------
The first N minutes of the session (default 15, i.e. 09:15-09:30) sets a
range: OR_high = highest high in that window, OR_low = lowest low. After
that window closes, a candle closing above OR_high is a bullish breakout; a
candle CLOSING below OR_low is a bearish breakdown. Nothing before the range
is complete counts as a signal — there's no range yet to break.

WHY CLOSE, NOT WICK: using the close (not just touching the level intrabar)
filters out the single most common ORB failure mode — a brief spike through
the level that immediately reverses. It costs some speed but a meaningfully
higher hit rate on the signals that do fire.

HOW THIS FITS THE EXISTING ARCHITECTURE
----------------------------------------
signal_engine.py already treats market structure (basis, OI, walls, PCR) as
VETOES over a price-direction proposal, not additive votes — see that file's
docstring for the reasoning. ORB slots into the same pattern as ANOTHER
veto: the existing EMA/RSI/MACD/ADX confluence proposes a direction, and ORB
can refuse it if price hasn't actually broken the opening range. This keeps
the "propose then filter" structure consistent rather than bolting on a
second, differently-shaped strategy.

MULTI-DAY DATA: the live candle fetch pulls several days of history each
cycle (needed for EMA/ATR warmup), so this must compute a fresh range PER
DAY, not once across the whole fetch — a candle from day N-1 must never
leak into day N's range.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from datetime import time as dtime

import pandas as pd

from signal_engine import Direction  # reuse the same enum signal_engine uses


@dataclass
class ORBConfig:
    range_minutes: int = 15          # 09:15-09:30 by default
    market_open: dtime = dtime(9, 15)

    def validate(self) -> None:
        if self.range_minutes <= 0:
            raise ValueError("range_minutes must be positive")


@dataclass
class ORBRange:
    day: object          # datetime.date
    high: float
    low: float
    range_complete: bool  # False while still inside the opening window


def compute_todays_range(df: pd.DataFrame, config: Optional[ORBConfig] = None) -> Optional[ORBRange]:
    """
    df: candle data with 'timestamp', 'high', 'low' columns, possibly
    spanning multiple days (only the LATEST day's candles are used).

    Returns None if there's no data for today at all. Returns an ORBRange
    with range_complete=False if today's session has started but the
    opening window (e.g. 09:15-09:30) hasn't finished yet — there's no
    frozen range to break out of yet, which callers must treat as "no
    signal possible," not "range is empty."
    """
    cfg = config or ORBConfig()
    cfg.validate()

    if df.empty:
        return None

    today = df["timestamp"].iloc[-1].date()
    today_df = df[df["timestamp"].dt.date == today]
    if today_df.empty:
        return None

    open_dt = pd.Timestamp.combine(today, cfg.market_open)
    if open_dt.tzinfo is None and today_df["timestamp"].dt.tz is not None:
        open_dt = open_dt.tz_localize(today_df["timestamp"].dt.tz)
    range_end = open_dt + pd.Timedelta(minutes=cfg.range_minutes)

    window = today_df[(today_df["timestamp"] >= open_dt) & (today_df["timestamp"] < range_end)]
    if window.empty:
        # session just started, not even one candle inside the window yet
        return ORBRange(day=today, high=float("nan"), low=float("nan"), range_complete=False)

    latest_ts = today_df["timestamp"].iloc[-1]
    complete = latest_ts >= range_end

    return ORBRange(day=today, high=float(window["high"].max()),
                    low=float(window["low"].min()), range_complete=complete)


def classify_breakout(current_close: float, orb_range: Optional[ORBRange]) -> Direction:
    """
    Returns BULLISH if price has closed above the opening range, BEARISH if
    below, NONE if the range isn't complete yet or price is still inside it.
    """
    if orb_range is None or not orb_range.range_complete:
        return Direction.NONE
    if current_close > orb_range.high:
        return Direction.BULLISH
    if current_close < orb_range.low:
        return Direction.BEARISH
    return Direction.NONE
