"""
paper_trade.py
===============
Paper trading engine. Uses the EXACT SAME signal logic already validated in
backtest_real_data.py (same functions, imported directly — not rewritten),
but instead of approximated premiums, fetches REAL live option premiums
from Angel One for every entry and every price check.

NO REAL ORDERS ARE EVER PLACED. This only reads market data and simulates
trades through the risk module, so you can see how the strategy performs
against real prices before risking real money.

WHAT THIS ANSWERS that the backtest couldn't:
  - Real bid/ask spread and premium behavior (not delta-0.5 approximation)
  - Real theta decay, since we're tracking actual live premiums over time
  - Whether the edge shown in the backtest survives contact with reality

WHAT THIS STILL DOESN'T ANSWER:
  - Real execution slippage (paper fills use the LTP at signal time, a
    real order might fill worse during fast moves)
  - Real order rejection scenarios (circuit limits, margin issues, etc.)
  These only show up in live trading with tiny real size — this script is
  the step before that, not a replacement for it.

RUN (only works during market hours, 9:15 AM - 3:30 PM IST, Mon-Fri):
    python3 paper_trade.py

Stop anytime with Ctrl+C — it exits cleanly, no cleanup needed since
nothing real was ever placed.
"""

import json
import sys
import time
import logging
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

import pandas as pd
import pyotp
import logzero
from SmartApi import SmartConnect

logzero.loglevel(logging.CRITICAL)  # same credential-leak protection as fetch_historical_data.py

sys.path.insert(0, str(Path(__file__).parent))
from backtest_real_data import compute_indicators, raw_regime, RegimeConfirmer, LOT_SIZE
from risk_manager import RiskManager

CREDS_PATH = Path(__file__).parent / "angel_credentials.json"
CONFIG_PATH = Path(__file__).parent / "config.json"
INSTRUMENT_MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
INSTRUMENT_CACHE = Path(__file__).parent / "instrument_master_cache.json"
POLL_SECONDS = 30  # how often to check price while a trade is open
CANDLE_LOOKBACK_DAYS = 10  # enough history for indicator warmup (EMA50 etc.)

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)


def login():
    if not CREDS_PATH.exists():
        raise FileNotFoundError(f"{CREDS_PATH} not found — run setup_credentials.py first.")
    creds = json.loads(CREDS_PATH.read_text())
    obj = SmartConnect(api_key=creds["api_key"])
    totp = pyotp.TOTP(creds["totp_secret"]).now()
    data = obj.generateSession(creds["client_code"], creds["password"], totp)
    if not data.get("status"):
        safe_info = {k: data.get(k) for k in ("status", "message", "errorcode")}
        raise RuntimeError(f"Login failed: {safe_info}")
    print("Logged in to Angel One.")
    return obj


def load_instrument_master(force_refresh=False):
    """Downloads Angel One's full instrument list (all NSE/NFO contracts)
    and caches it locally — this is a large file (tens of MB), so we don't
    re-download it every run. Refresh weekly or when expiry changes."""
    if INSTRUMENT_CACHE.exists() and not force_refresh:
        age_hours = (time.time() - INSTRUMENT_CACHE.stat().st_mtime) / 3600
        if age_hours < 24:
            print(f"Using cached instrument master ({age_hours:.1f}h old)")
            return json.loads(INSTRUMENT_CACHE.read_text())

    print("Downloading instrument master (this may take a minute)...")
    import urllib.request
    with urllib.request.urlopen(INSTRUMENT_MASTER_URL, timeout=60) as resp:
        data = json.loads(resp.read())
    INSTRUMENT_CACHE.write_text(json.dumps(data))
    print(f"Downloaded and cached {len(data)} instruments.")
    return data


def find_option_contract(instruments, underlying, strike, option_type, expiry_hint=None):
    """Finds the nearest-expiry NFO option contract for the given underlying,
    strike, and CE/PE. Angel's instrument master uses fields like 'symbol',
    'name', 'expiry', 'strike', 'instrumenttype', 'exch_seg', 'token'.

    Returns (tradingsymbol, token) or raises if not found — better to fail
    loudly than silently trade the wrong contract."""
    candidates = []
    for inst in instruments:
        if inst.get("exch_seg") != "NFO":
            continue
        if inst.get("name") != underlying:
            continue
        if inst.get("instrumenttype") not in ("OPTIDX",):
            continue
        symbol = inst.get("symbol", "")
        if not symbol.endswith(option_type):
            continue
        try:
            inst_strike = float(inst.get("strike", 0)) / 100  # Angel stores strike * 100
        except (ValueError, TypeError):
            continue
        if abs(inst_strike - strike) > 0.01:
            continue
        candidates.append(inst)

    if not candidates:
        raise ValueError(f"No contract found for {underlying} {strike} {option_type}. "
                          f"Instrument master may need refreshing, or strike/expiry "
                          f"convention has changed.")

    # pick nearest expiry (soonest) among matches
    candidates.sort(key=lambda x: x.get("expiry", ""))
    chosen = candidates[0]
    return chosen["symbol"], chosen["token"]


def round_to_strike(spot, step=50):
    return round(spot / step) * step


def get_ltp(obj, exchange, tradingsymbol, token):
    resp = obj.ltpData(exchange, tradingsymbol, token)
    if not resp.get("status"):
        raise RuntimeError(f"LTP fetch failed for {tradingsymbol}: {resp.get('message')}")
    return float(resp["data"]["ltp"])


def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def get_recent_candles(obj, token, exchange="NSE", interval="FIFTEEN_MINUTE", days=CANDLE_LOOKBACK_DAYS):
    end = datetime.now(IST)
    start = end - timedelta(days=days)
    params = {
        "exchange": exchange, "symboltoken": token, "interval": interval,
        "fromdate": start.strftime("%Y-%m-%d %H:%M"),
        "todate": end.strftime("%Y-%m-%d %H:%M"),
    }
    resp = obj.getCandleData(params)
    if not resp.get("status"):
        raise RuntimeError(f"Candle fetch failed: {resp.get('message')}")
    df = pd.DataFrame(resp["data"], columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def find_token_for_symbol(instruments, tradingsymbol):
    """Look a contract's token back up from its trading symbol. Needed when
    recovering a position from state, since the risk module stores the symbol
    but not the token."""
    for inst in instruments:
        if inst.get("symbol") == tradingsymbol:
            return inst.get("token")
    return None


def recover_open_position(rm, instruments, strategy_name="nifty_options_indicator"):
    """
    Rebuild in-memory tracking for a position that is already open in the risk
    module's state.

    WHY THIS EXISTS: open_trade used to be a plain local variable, reset to None
    at the top of every session. Any position open when the process stopped — a
    crash, a restart, Ctrl+C, or simply the end of the day — stayed recorded in
    the state file while the engine forgot about it entirely. Nothing then
    monitored it, nothing closed it, and its reserved risk and deployed capital
    stayed permanently consumed. Recovering here is what makes the daemon
    genuinely restart-safe rather than only appearing to be.

    Returns an open_trade dict, or None if there is nothing to recover.
    """
    positions = rm.list_open_positions()
    mine = {tid: p for tid, p in positions.items()
            if p.get("strategy") == strategy_name}

    if not mine:
        return None

    if len(mine) > 1:
        print(f"WARNING: {len(mine)} open positions found for {strategy_name}, "
              f"but this engine only tracks one at a time. Recovering the most "
              f"recent; the others need closing manually from the dashboard.")

    trade_id = max(mine, key=lambda t: mine[t].get("opened_at", ""))
    pos = mine[trade_id]

    token = find_token_for_symbol(instruments, pos["symbol"])
    if token is None:
        print(f"WARNING: recovered position {trade_id} ({pos['symbol']}) but its "
              f"contract is not in the current instrument master — it has most "
              f"likely expired. It cannot be priced or closed automatically. "
              f"Close it manually from the dashboard to free its reserved risk.")
        return None

    entry_premium = float(pos["entry_price"])
    sl_premium = float(pos["stop_loss_price"])
    # Target is not stored in state; reconstruct it from the same 1.5:1 ratio
    # the entry logic uses, so a recovered trade exits on the same terms.
    target_premium = entry_premium + (entry_premium - sl_premium) * 1.5

    direction = "CE" if pos["symbol"].endswith("CE") else "PE"

    print(f"RECOVERED open position {trade_id}: {pos['symbol']} "
          f"entry Rs.{entry_premium:.2f}, SL Rs.{sl_premium:.2f}, "
          f"opened {pos.get('opened_at', 'unknown')}")

    return {
        "trade_id": trade_id, "symbol": pos["symbol"], "token": token,
        "direction": direction, "entry_premium": entry_premium,
        "sl_premium": sl_premium, "target_premium": target_premium,
        "opened_at": pos.get("opened_at", ""),
    }


def is_stale_position(open_trade):
    """True if the position was opened on an earlier day. These are intraday
    option trades — nothing should ever carry overnight, so a position from a
    previous session means the square-off never ran and it must be closed at
    the first opportunity rather than treated as a live trade."""
    if not open_trade or not open_trade.get("opened_at"):
        return False
    try:
        opened = datetime.fromisoformat(open_trade["opened_at"])
    except (ValueError, TypeError):
        return False
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=IST)
    return opened.astimezone(IST).date() < datetime.now(IST).date()


def run_trading_session(obj, instruments, rm):
    """Runs the intraday trading loop for one session (one market day),
    until the market closes or a fatal error occurs. Returns normally when
    the day ends — the caller loops back and waits for the next day."""
    NIFTY_TOKEN = "99926000"
    confirmer = RegimeConfirmer()
    prev_regime = "FLAT"

    # Pick up anything left open by a previous run rather than abandoning it.
    open_trade = recover_open_position(rm, instruments)

    if open_trade and is_stale_position(open_trade):
        print(f"Position {open_trade['trade_id']} is from a previous day — "
              f"squaring off now at market price (intraday strategy, nothing "
              f"should carry overnight).")
        try:
            px = get_ltp(obj, "NFO", open_trade["symbol"], open_trade["token"])
            pnl = rm.record_close(open_trade["trade_id"], px)
            print(f"Stale position closed at Rs.{px:.2f}, P&L Rs.{pnl:,.2f}")
        except Exception as e:
            print(f"Could not close stale position automatically: {e}. "
                  f"Close it from the dashboard to free its reserved risk.")
        open_trade = None

    while is_market_open():
        df = get_recent_candles(obj, NIFTY_TOKEN)
        df = compute_indicators(df)
        last_row = df.iloc[-1]

        if pd.isna(last_row["atr14"]) or pd.isna(last_row["adx14"]):
            print("Not enough data yet for indicators, waiting...")
            time.sleep(POLL_SECONDS)
            continue

        raw = raw_regime(last_row)
        regime = confirmer.update(raw)
        spot = last_row["close"]
        now_str = datetime.now(IST).strftime("%H:%M:%S")

        # --- manage open paper trade ---
        if open_trade is not None:
            try:
                current_premium = get_ltp(obj, "NFO", open_trade["symbol"], open_trade["token"])
            except Exception as e:
                print(f"[{now_str}] Could not fetch LTP for open position: {e}")
                time.sleep(POLL_SECONDS)
                continue

            hit_sl = current_premium <= open_trade["sl_premium"]
            hit_target = current_premium >= open_trade["target_premium"]
            near_close = datetime.now(IST).time() >= dtime(15, 20)

            print(f"[{now_str}] Open {open_trade['direction']} {open_trade['symbol']}: "
                  f"premium={current_premium:.2f} (entry={open_trade['entry_premium']:.2f}, "
                  f"SL={open_trade['sl_premium']:.2f}, target={open_trade['target_premium']:.2f})")

            if hit_sl or hit_target or near_close:
                pnl = rm.record_close(open_trade["trade_id"], current_premium)
                reason = "SL" if hit_sl else ("TARGET" if hit_target else "EOD")
                print(f"[{now_str}] CLOSED ({reason}): P&L = Rs.{pnl:,.2f}")
                open_trade = None

        # --- look for new entry ---
        elif regime != "FLAT" and regime != prev_regime:
            direction = "CE" if regime == "UP" else "PE"
            strike = round_to_strike(spot)
            try:
                symbol, token = find_option_contract(instruments, "NIFTY", strike, direction)
                entry_premium = get_ltp(obj, "NFO", symbol, token)
            except Exception as e:
                print(f"[{now_str}] Could not get option contract/price: {e}")
                prev_regime = regime
                time.sleep(POLL_SECONDS)
                continue

            sl_distance = last_row["atr14"] * 1.0 * 0.5  # delta-adjusted for SL only
            target_distance = last_row["atr14"] * 1.5 * 0.5
            sl_premium = max(1.0, entry_premium - sl_distance)
            target_premium = entry_premium + target_distance

            decision = rm.size_trade(
                strategy_name="nifty_options_indicator",
                entry_price=entry_premium, stop_loss_price=sl_premium,
                lot_size=LOT_SIZE, symbol=symbol, direction="BUY",
            )

            if decision["approved"]:
                trade_id = rm.record_open(
                    strategy_name="nifty_options_indicator", symbol=symbol,
                    direction="BUY", entry_price=entry_premium,
                    stop_loss_price=sl_premium, quantity=decision["quantity"],
                    reservation_id=decision["reservation_id"],
                )
                open_trade = {
                    "trade_id": trade_id, "symbol": symbol, "token": token,
                    "direction": direction, "entry_premium": entry_premium,
                    "sl_premium": sl_premium, "target_premium": target_premium,
                }
                print(f"[{now_str}] PAPER ENTRY: {direction} {symbol} @ Rs.{entry_premium:.2f} "
                      f"(qty={decision['quantity']}, risk=Rs.{decision['risk_amount']:.0f})")
            else:
                print(f"[{now_str}] Signal fired but blocked: {decision['reason']}")

        prev_regime = regime
        time.sleep(POLL_SECONDS)

    # session ended (market closed) — if a trade is still open, that
    # shouldn't normally happen (near_close should have closed it), but
    # as a safety net, warn loudly rather than silently carrying it over
    if open_trade is not None:
        print(f"WARNING: session ended with an open trade ({open_trade['symbol']}) "
              f"that wasn't closed by the near_close check. It will be re-evaluated "
              f"tomorrow — check the dashboard.")


def main():
    """Runs forever as a background service: waits for market hours, trades
    through the session, then waits for the next trading day. Designed to
    run under systemd with Restart=always, so it survives crashes too."""
    print("=" * 60)
    print("PAPER TRADING ENGINE (24/7 daemon) — no real orders will be placed")
    print("=" * 60)

    rm = RiskManager(str(CONFIG_PATH))
    if not rm.is_engine_active():
        rm.activate_engine(reason="paper_trade.py session")

    instruments = None
    last_instrument_refresh_day = None

    while True:
        if not is_market_open():
            now = datetime.now(IST)
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] Market closed. "
                  f"Waiting... (checks every 5 min)")
            time.sleep(300)
            continue

        today = datetime.now(IST).date()
        try:
            obj = login()
            if instruments is None or last_instrument_refresh_day != today:
                instruments = load_instrument_master()
                last_instrument_refresh_day = today

            print(f"\n[{datetime.now(IST).strftime('%H:%M:%S')}] "
                  f"Market open — starting today's session.\n")
            run_trading_session(obj, instruments, rm)
            print(f"\n[{datetime.now(IST).strftime('%H:%M:%S')}] "
                  f"Session ended (market closed for today).")

        except Exception as e:
            # don't let one bad error kill the whole daemon — log it,
            # wait a bit, and let the outer loop retry
            print(f"ERROR in trading session: {e}")
            print("Waiting 60s before retrying...")
            time.sleep(60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nStopped by user (Ctrl+C).")
