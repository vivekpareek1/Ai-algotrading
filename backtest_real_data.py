"""
backtest_real_data.py
======================
Backtests a confluence-based Nifty options strategy against REAL historical
index data (nifty_historical_1year.csv from fetch_historical_data.py), then
routes every simulated trade through your actual risk_manager.py — so the
results reflect real position sizing, real portfolio limits, and the real
daily loss kill switch, not just raw signal accuracy.

=======================================================================
IMPORTANT LIMITATION — READ THIS FIRST
=======================================================================
No retail broker (including Angel One) keeps historical data for EXPIRED
option contracts. This means real historical option premiums cannot be
backtested by anyone, on any platform, with a retail API. This is not a
limitation of this script — it's a limitation of what data exists.

What this script actually does instead:
  - Generates BUY signals (CE or PE) from REAL index price action using
    a confluence of EMA stack, VWAP, RSI, MACD, and ADX — the same kind
    of factors your indicator used
  - Approximates option premium movement using delta ≈ 0.5, meaning a
    100-point favorable index move is treated as ~50 points of premium
    gain (a reasonable approximation for a near-ATM weekly option)
  - Estimates entry premium as roughly 0.4% of spot (typical ATM weekly
    premium ballpark) — this is an APPROXIMATION, not real market data

This means the results are a FLOOR, not a ceiling. Theta decay (time
value loss) is not modeled at all, and real option premium behaves less
smoothly than 0.5x the index move, especially near expiry. If a strategy
doesn't show an edge even here, it's very unlikely to show one live. If
it does show an edge here, that's necessary but not sufficient — it still
needs forward-testing on real premiums before real money.
=======================================================================

Run:
    python3 backtest_real_data.py --csv nifty_historical_1year.csv
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from risk_manager import RiskManager

LOT_SIZE = 75  # current Nifty lot size — update if NSE changes it
CONFIRM_BARS = 3  # regime must hold this many consecutive bars (avoids flicker)


# ----------------------------------------------------------------- #
# Indicators
# ----------------------------------------------------------------- #

def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd_hist(series, fast=12, slow=26, signal=9):
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    return macd_line - signal_line


def atr(df, period=14):
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def adx(df, period=14):
    high, low, close = df["high"], df["low"], df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = atr(df, period)
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / tr.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / tr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False).mean()


def session_vwap(df):
    """VWAP resets every trading day."""
    day = df["timestamp"].dt.date
    typical = (df["high"] + df["low"] + df["close"]) / 3
    pv = typical * df["volume"]
    cum_pv = pv.groupby(day).cumsum()
    cum_vol = df["volume"].groupby(day).cumsum()
    return cum_pv / cum_vol.replace(0, np.nan)


def compute_indicators(df):
    df = df.copy()
    df["ema9"] = ema(df["close"], 9)
    df["ema21"] = ema(df["close"], 21)
    df["ema50"] = ema(df["close"], 50)
    df["rsi14"] = rsi(df["close"], 14)
    df["macd_h"] = macd_hist(df["close"])
    df["adx14"] = adx(df, 14)
    df["atr14"] = atr(df, 14)
    df["vwap"] = session_vwap(df)
    return df


# ----------------------------------------------------------------- #
# Signal generation (confluence scoring)
# ----------------------------------------------------------------- #

def raw_regime(row, adx_threshold=20):
    """Score bullish vs bearish confluence factors. Returns UP, DOWN, or FLAT."""
    if pd.isna(row["adx14"]) or pd.isna(row["vwap"]):
        return "FLAT"

    bull_score = 0
    bear_score = 0

    # EMA stack
    if row["ema9"] > row["ema21"] > row["ema50"]:
        bull_score += 1
    elif row["ema9"] < row["ema21"] < row["ema50"]:
        bear_score += 1

    # VWAP position
    if row["close"] > row["vwap"]:
        bull_score += 1
    elif row["close"] < row["vwap"]:
        bear_score += 1

    # RSI momentum (avoid extreme overbought/oversold as entries)
    if 50 < row["rsi14"] < 70:
        bull_score += 1
    elif 30 < row["rsi14"] < 50:
        bear_score += 1

    # MACD histogram
    if row["macd_h"] > 0:
        bull_score += 1
    elif row["macd_h"] < 0:
        bear_score += 1

    # ADX trend strength gate — no regime without a real trend
    if row["adx14"] < adx_threshold:
        return "FLAT"

    if bull_score >= 3:
        return "UP"
    elif bear_score >= 3:
        return "DOWN"
    return "FLAT"


class RegimeConfirmer:
    """Requires N consecutive bars of agreement before confirming a regime —
    prevents entering on every single flicker."""
    def __init__(self, n=CONFIRM_BARS):
        self.n = n
        self.buf = []
        self.current = "FLAT"

    def update(self, raw):
        self.buf.append(raw)
        if len(self.buf) > self.n:
            self.buf.pop(0)
        if len(self.buf) == self.n and len(set(self.buf)) == 1:
            self.current = raw
        return self.current


# ----------------------------------------------------------------- #
# Premium approximation
# ----------------------------------------------------------------- #

def estimate_atm_premium(spot):
    """Rough ATM weekly premium ballpark: ~0.4% of spot. This is an
    approximation, not real market data (see module docstring)."""
    return max(30.0, spot * 0.004)


DELTA_APPROX = 0.5


# ----------------------------------------------------------------- #
# Backtest loop
# ----------------------------------------------------------------- #

class SimClock:
    """Feeds the risk module simulated 'current time' from the historical
    data being replayed, instead of the real wall clock — critical for the
    daily loss kill switch to reset on each SIMULATED day, not just once
    for the whole backtest run."""
    def __init__(self, tz):
        self.tz = tz
        self.current = None

    def set(self, ts):
        if ts.tzinfo is None:
            ts = ts.tz_localize(self.tz)
        else:
            ts = ts.tz_convert(self.tz)
        self.current = ts.to_pydatetime()

    def __call__(self):
        return self.current


def print_diagnostics(df, adx_threshold):
    """Always printed — shows WHY zero (or few) trades happened, instead of
    leaving you to guess or dig through the journal manually."""
    valid = df.dropna(subset=["adx14", "vwap"])
    print("\n" + "-" * 60)
    print("SIGNAL DIAGNOSTICS")
    print("-" * 60)
    print(f"Total bars: {len(df)}, usable after indicator warmup: {len(valid)}")
    above_adx = (valid["adx14"] >= adx_threshold).sum()
    print(f"Bars with ADX >= {adx_threshold}: {above_adx} "
          f"({100*above_adx/len(valid):.1f}% of usable bars)")
    print(f"ADX distribution — min: {valid['adx14'].min():.1f}, "
          f"median: {valid['adx14'].median():.1f}, "
          f"75th pct: {valid['adx14'].quantile(0.75):.1f}, "
          f"max: {valid['adx14'].max():.1f}")

    regimes = valid.apply(lambda r: raw_regime(r, adx_threshold), axis=1)
    counts = regimes.value_counts()
    print(f"\nRaw regime counts (before the {CONFIRM_BARS}-bar confirmation filter):")
    print(counts.to_string())
    print("-" * 60)


def run_backtest(df, config_path, min_lots=1, adx_threshold=20):
    df = compute_indicators(df)
    print_diagnostics(df, adx_threshold)
    confirmer = RegimeConfirmer()

    # bootstrap: read config to get the configured timezone before the
    # clock exists, since RiskManager needs the clock at construction time
    import json as _json
    tz_name = _json.loads(Path(config_path).read_text()).get("timezone", "Asia/Kolkata")
    from zoneinfo import ZoneInfo
    clock = SimClock(ZoneInfo(tz_name))
    clock.set(df.iloc[0]["timestamp"])  # initialize before first use

    rm = RiskManager(config_path, clock=clock)
    if not rm.is_engine_active():
        rm.activate_engine(reason="backtest_real_data.py")

    open_trade = None  # dict: {reservation_id/trade_id, direction, entry_premium, sl_premium, target_premium, entry_bar_day}
    trades = []
    prev_regime = "FLAT"

    for i, row in df.iterrows():
        clock.set(row["timestamp"])  # advance simulated time to this bar

        if pd.isna(row["atr14"]) or pd.isna(row["adx14"]):
            continue  # not enough warmup data yet

        raw = raw_regime(row, adx_threshold)
        regime = confirmer.update(raw)
        today = row["timestamp"].date()

        # --- manage an open trade first ---
        if open_trade is not None:
            spot_move = row["close"] - open_trade["entry_spot"]
            if open_trade["direction"] == "PE":
                spot_move = -spot_move  # PE profits from downward moves
            current_premium = open_trade["entry_premium"] + DELTA_APPROX * spot_move

            hit_sl = current_premium <= open_trade["sl_premium"]
            hit_target = current_premium >= open_trade["target_premium"]
            end_of_day = today != open_trade["entry_day"]  # square off, no overnight

            if hit_sl or hit_target or end_of_day:
                exit_premium = max(0.0, current_premium)
                pnl = rm.record_close(open_trade["trade_id"], exit_premium)
                trades.append({
                    "entry_time": open_trade["entry_time"], "exit_time": row["timestamp"],
                    "direction": open_trade["direction"], "entry_premium": open_trade["entry_premium"],
                    "exit_premium": exit_premium, "pnl": pnl,
                    "reason": "SL" if hit_sl else ("TARGET" if hit_target else "EOD"),
                })
                open_trade = None

        # --- look for a new entry only if flat and regime just confirmed ---
        if open_trade is None and regime != "FLAT" and regime != prev_regime:
            direction = "CE" if regime == "UP" else "PE"
            entry_spot = row["close"]
            entry_premium = estimate_atm_premium(entry_spot)
            sl_distance_spot = row["atr14"] * 1.0
            target_distance_spot = row["atr14"] * 1.5
            sl_premium = max(1.0, entry_premium - DELTA_APPROX * sl_distance_spot)
            target_premium = entry_premium + DELTA_APPROX * target_distance_spot

            decision = rm.size_trade(
                strategy_name="nifty_options_indicator",
                entry_price=entry_premium, stop_loss_price=sl_premium,
                lot_size=LOT_SIZE, symbol=f"NIFTY-{direction}-BACKTEST",
                direction="BUY", min_lots=min_lots,
            )

            if decision["approved"]:
                trade_id = rm.record_open(
                    strategy_name="nifty_options_indicator",
                    symbol=f"NIFTY-{direction}-BACKTEST", direction="BUY",
                    entry_price=entry_premium, stop_loss_price=sl_premium,
                    quantity=decision["quantity"], reservation_id=decision["reservation_id"],
                )
                open_trade = {
                    "trade_id": trade_id, "direction": direction,
                    "entry_spot": entry_spot, "entry_premium": entry_premium,
                    "sl_premium": sl_premium, "target_premium": target_premium,
                    "entry_time": row["timestamp"], "entry_day": today,
                }
            # if not approved (blocked/kill switch/etc), just skip this signal

        prev_regime = regime

    return trades, rm


def print_report(trades, rm):
    print("\n" + "=" * 60)
    print("BACKTEST RESULTS")
    print("=" * 60)

    if not trades:
        print("No trades were generated. This can happen if the data is too")
        print("short, too choppy (ADX rarely above 20), or the risk limits")
        print("blocked every signal. Check the trade_journal.csv for BLOCKED entries.")
        return

    df = pd.DataFrame(trades)
    total_trades = len(df)
    wins = (df["pnl"] > 0).sum()
    losses = (df["pnl"] <= 0).sum()
    win_rate = 100 * wins / total_trades
    total_pnl = df["pnl"].sum()
    avg_win = df.loc[df["pnl"] > 0, "pnl"].mean() if wins else 0
    avg_loss = df.loc[df["pnl"] <= 0, "pnl"].mean() if losses else 0

    # max drawdown on cumulative P&L
    cum = df["pnl"].cumsum()
    running_max = cum.cummax()
    drawdown = cum - running_max
    max_dd = drawdown.min()

    print(f"Total trades:     {total_trades}")
    print(f"Wins / Losses:    {wins} / {losses}")
    print(f"Win rate:         {win_rate:.1f}%")
    print(f"Total P&L:        Rs.{total_pnl:,.0f}")
    print(f"Avg win:          Rs.{avg_win:,.0f}")
    print(f"Avg loss:         Rs.{avg_loss:,.0f}")
    print(f"Max drawdown:     Rs.{max_dd:,.0f}")
    print(f"By exit reason:\n{df['reason'].value_counts().to_string()}")

    status = rm.get_status()
    print(f"\nFinal capital state:")
    print(f"  Realized P&L today (last session only): Rs.{status['realized_pnl_today']:,.0f}")
    print(f"  Blocked at end: {status['blocked_for_today']}")

    df.to_csv("backtest_trades.csv", index=False)
    print(f"\nFull trade log saved to backtest_trades.csv")
    print("\nRemember: this uses APPROXIMATED option premiums (delta 0.5,")
    print("no theta decay). Treat this as a floor, not a live-performance guarantee.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default="nifty_historical_1year.csv")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--adx-threshold", type=float, default=20,
                         help="Lower this (e.g. 15) if diagnostics show real "
                              "ADX rarely reaches 20 on this timeframe.")
    args = parser.parse_args()

    df = pd.read_csv(args.csv, parse_dates=["timestamp"])
    print(f"Loaded {len(df)} candles from {args.csv}")
    print(f"Date range: {df['timestamp'].min()} to {df['timestamp'].max()}")

    trades, rm = run_backtest(df, args.config, adx_threshold=args.adx_threshold)
    print_report(trades, rm)
