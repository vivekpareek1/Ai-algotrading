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
from SmartApi.smartExceptions import TokenException

logzero.loglevel(logging.CRITICAL)  # same credential-leak protection as fetch_historical_data.py

sys.path.insert(0, str(Path(__file__).parent))
from backtest_real_data import compute_indicators, raw_regime, RegimeConfirmer, LOT_SIZE
from risk_manager import RiskManager
from signal_engine import SignalEngine, StructureAnalyzer, Direction
from exit_manager import ExitManager, ExitConfig, Position, MarketState, Side, Action
from orb_strategy import compute_todays_range, classify_breakout
from entry_sizing import plan_stops, SizingConfig

CREDS_PATH = Path(__file__).parent / "angel_credentials.json"
CONFIG_PATH = Path(__file__).parent / "config.json"
DATA_DIR = Path(__file__).parent / "data"   # where oi_collector writes
INSTRUMENT_MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
INSTRUMENT_CACHE = Path(__file__).parent / "instrument_master_cache.json"
POLL_SECONDS = 30  # how often to check price while a trade is open
CANDLE_LOOKBACK_DAYS = 10  # enough history for indicator warmup (EMA50 etc.)

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 40)   # NSE extended F&O close from 15:30 to 15:40 on
                                # 3 Aug 2026 (Closing Auction Session rollout).
                                # Index F&O (what we trade) is not itself
                                # subject to CAS, only the extended close time.

# From 15:15 onward, F&O stocks (most Nifty constituents) stop continuous
# trading and enter their own closing auction — so the Nifty INDEX value from
# that point is increasingly built from stale pre-15:15 constituent prices,
# not fresh trading, even though our option contracts keep trading normally
# until 15:40. A signal generated from index candles in this window is
# reading an index that is no longer being freshly priced underneath it.
# Existing EOD square-off (~15:15-15:20) already exits before this gets bad;
# this cutoff stops NEW entries from firing into the same stale window.
NEW_ENTRY_CUTOFF = dtime(15, 15)


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


REQUIRE_STRUCTURE = True   # refuse to trade on price alone — see load_structure()


def load_structure(underlying="NIFTY"):
    """
    Read the latest market-structure snapshot written by oi_collector.
    Returns (StructureRead, None) or (None, reason).

    WHY ITS ABSENCE BLOCKS TRADING: the price-only confluence measured ~48% on
    a year of real data — barely a coin flip, and that figure assumed no theta
    decay, so live it is worse. Structure data is what the entry filter relies
    on. Falling back to price-only when the collector is down would silently
    resume trading the strategy we already know does not work.
    """
    day = datetime.now(IST).strftime("%Y-%m-%d")
    path = DATA_DIR / underlying / f"summary_{day}.csv"
    if not path.exists():
        return None, f"no collector data at {path} — is oi-collector running?"
    try:
        df = pd.read_csv(path)
    except Exception as e:
        return None, f"could not read {path}: {e}"
    if df.empty:
        return None, f"{path} empty — collector has not written a snapshot yet"
    try:
        return StructureAnalyzer().read_summary(df), None
    except Exception as e:
        return None, f"could not parse structure data: {e}"


def open_new_trade(obj, instruments, rm, signal_eng, exit_mgr, last_row, spot,
                   regime, now_str, candle_df=None):
    """Price proposes a direction, structure can veto it, ORB can veto it too,
    then stop levels are reconciled so the reserved risk matches the stop
    that will actually fire. Returns an open_trade dict, or None.

    candle_df: full multi-day candle history (same df the caller already
    fetched for indicators) — used to compute today's opening range. None
    is accepted so this function stays testable without ORB wired in."""
    price_direction = Direction.BULLISH if regime == "UP" else Direction.BEARISH
    atr = float(last_row["atr14"])

    orb_direction = Direction.NONE
    if candle_df is not None:
        try:
            orb_range = compute_todays_range(candle_df)
            orb_direction = classify_breakout(spot, orb_range)
        except Exception as e:
            print(f"[{now_str}] ORB computation failed (treating as no "
                  f"confirmation, not blocking the whole engine): {e}")

    structure, err = load_structure("NIFTY")
    if structure is None:
        if REQUIRE_STRUCTURE:
            print(f"[{now_str}] {price_direction.value} price signal IGNORED — {err}")
            return None
        print(f"[{now_str}] WARNING trading without structure data: {err}")
    else:
        sig = signal_eng.decide(price_direction, structure, atr, orb_direction)
        print(f"[{now_str}] {sig.reason}")
        for v in sig.vetoes:
            print(f"[{now_str}]    veto: {v}")
        if not sig.take_trade:
            return None

    direction = "CE" if price_direction is Direction.BULLISH else "PE"
    try:
        symbol, token = find_option_contract(instruments, "NIFTY",
                                             round_to_strike(spot), direction)
        entry_premium = get_ltp(obj, "NFO", symbol, token)
    except Exception as e:
        print(f"[{now_str}] Could not get option contract/price: {e}")
        return None

    # exit_manager stops on the underlying, risk_manager sizes on premium.
    # plan_stops() decides which stop binds first and returns the premium
    # distance to size on, so risk_amount is not fiction.
    try:
        plan = plan_stops(entry_premium, spot, atr, is_call=(direction == "CE"))
    except ValueError as e:
        print(f"[{now_str}] Could not plan stops: {e}")
        return None

    d = rm.size_trade(strategy_name="nifty_options_indicator",
                      entry_price=entry_premium,
                      stop_loss_price=plan.premium_stop_for_sizing,
                      lot_size=LOT_SIZE, symbol=symbol, direction="BUY")
    if not d["approved"]:
        print(f"[{now_str}] Passed structure but risk layer blocked: {d['reason']}")
        return None

    trade_id = rm.record_open(
        strategy_name="nifty_options_indicator", symbol=symbol, direction="BUY",
        entry_price=entry_premium, stop_loss_price=plan.premium_stop_for_sizing,
        quantity=d["quantity"], reservation_id=d["reservation_id"])

    pos = Position(position_id=trade_id, symbol=symbol,
                   side=Side.LONG_CE if direction == "CE" else Side.LONG_PE,
                   entry_time=datetime.now(IST), entry_underlying=spot,
                   entry_premium=entry_premium, quantity=d["quantity"],
                   atr_at_entry=atr)

    # MUST run before the first evaluate(): it freezes initial_sl_underlying,
    # which is what defines 1R for the whole life of the trade. Without it
    # exit_manager raises on risk_points and the position cannot be managed.
    exit_mgr.set_initial_stop(pos)

    print(f"[{now_str}] PAPER ENTRY: {direction} {symbol} @ Rs.{entry_premium:.2f} "
          f"qty={d['quantity']} risk=Rs.{d['risk_amount']:.0f} | stop binds on "
          f"{plan.binding_constraint} (underlying {plan.underlying_stop:.0f} / "
          f"premium {plan.premium_stop_for_sizing:.2f})")

    return {"trade_id": trade_id, "symbol": symbol, "token": token,
            "direction": direction, "entry_premium": entry_premium,
            "sl_premium": plan.premium_stop_for_sizing, "position": pos,
            "opened_at": pos.entry_time.isoformat()}


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


def _futures_volume(structure):
    """Current and average futures volume for the momentum score.

    The index itself has no traded volume — only its constituent stocks do —
    so the collector's futures volume is the correct input here. Passing index
    volume (always 0) pins the score's volume component at a neutral 0.5 and
    compresses the usable range, which is what disabled the momentum exit."""
    day = datetime.now(IST).strftime("%Y-%m-%d")
    path = DATA_DIR / "NIFTY" / f"summary_{day}.csv"
    try:
        df = pd.read_csv(path)
        vols = pd.to_numeric(df["fut_volume"], errors="coerce").dropna()
        if vols.empty:
            return 0.0, 0.0
        return float(vols.iloc[-1]), float(vols.mean())
    except Exception:
        return 0.0, 0.0


def run_trading_session(obj, instruments, rm):
    """Runs the intraday trading loop for one session (one market day),
    until the market closes or a fatal error occurs. Returns normally when
    the day ends — the caller loops back and waits for the next day."""
    NIFTY_TOKEN = "99926000"
    confirmer = RegimeConfirmer()
    prev_regime = "FLAT"
    signal_eng = SignalEngine()
    exit_mgr = ExitManager()

    # Guard against the two configs drifting apart. If they disagree, the
    # reserved risk stops matching the stop that actually fires — the exact
    # failure entry_sizing exists to prevent, so fail loudly rather than trade.
    SizingConfig().verify_against_exit_config(exit_mgr.cfg)

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

        # --- manage open paper trade via ExitManager ---
        if open_trade is not None:
            try:
                current_premium = get_ltp(obj, "NFO", open_trade["symbol"],
                                          open_trade["token"])
            except Exception as e:
                print(f"[{now_str}] Could not fetch LTP for open position: {e}")
                time.sleep(POLL_SECONDS)
                continue

            # Feed FUTURES volume, not index volume. An index has no traded
            # volume of its own (only its constituents do), so passing index
            # volume pins the momentum score's volume component at a neutral
            # 0.5 and compresses the whole score into roughly [0.10, 0.80] —
            # which makes the momentum-decay exit essentially unreachable.
            structure, _ = load_structure("NIFTY")
            fut_vol = avg_fut_vol = 0.0
            if structure is not None:
                fut_vol, avg_fut_vol = _futures_volume(structure)

            m = MarketState(
                timestamp=datetime.now(IST), underlying=spot,
                premium=current_premium, atr=float(last_row["atr14"]),
                ema_fast=float(last_row["ema9"]), ema_slow=float(last_row["ema21"]),
                rsi=float(last_row["rsi14"]), adx=float(last_row["adx14"]),
                prev_adx=float(df.iloc[-2]["adx14"]) if len(df) > 1 else float(last_row["adx14"]),
                volume=fut_vol, avg_volume=avg_fut_vol,
                ema_fast_prev=float(df.iloc[-2]["ema9"]) if len(df) > 1 else float(last_row["ema9"]),
            )

            dec = exit_mgr.evaluate(open_trade["position"], m)
            print(f"[{now_str}] {open_trade['direction']} {open_trade['symbol']}: "
                  f"premium={current_premium:.2f} R={dec.r_multiple:+.2f} "
                  f"mom={dec.momentum.value}({dec.momentum_score:.2f}) "
                  f"-> {dec.action.value}: {dec.reason}")

            if dec.action is Action.EXIT_FULL:
                pnl = rm.record_close(open_trade["trade_id"], current_premium)
                print(f"[{now_str}] CLOSED: P&L = Rs.{pnl:,.2f} ({dec.reason})")
                open_trade = None

            elif dec.action is Action.EXIT_PARTIAL:
                # exit_manager has ALREADY updated remaining_quantity,
                # partial_booked and sl_underlying on the Position before
                # returning. Do NOT repeat those mutations here — doing so
                # decrements the size twice and the position tracking collapses
                # after the first partial book. Only mirror the fill into the
                # risk module.
                qty = dec.exit_quantity
                if qty and qty > 0:
                    pnl = rm.record_close(open_trade["trade_id"], current_premium,
                                          quantity=qty)
                    print(f"[{now_str}] PARTIAL BOOK {qty} units: Rs.{pnl:,.2f} "
                          f"(remaining {open_trade['position'].remaining_quantity})")

            elif dec.action is Action.UPDATE_SL:
                # sl_underlying is likewise already set by exit_manager;
                # nothing to mirror into the risk module for a stop move.
                pass

        # --- look for new entry ---
        elif regime != "FLAT" and regime != prev_regime:
            if datetime.now(IST).time() >= NEW_ENTRY_CUTOFF:
                print(f"[{now_str}] {regime} signal ignored — past {NEW_ENTRY_CUTOFF} "
                      f"cutoff (index constituents entering their closing "
                      f"auction, spot data no longer fresh for a new entry)")
            else:
                open_trade = open_new_trade(obj, instruments, rm, signal_eng,
                                            exit_mgr, last_row, spot, regime, now_str,
                                            candle_df=df)

        prev_regime = regime
        time.sleep(POLL_SECONDS)

    # session ended (market closed) — if a trade is still open, that
    # shouldn't normally happen (near_close should have closed it), but
    # as a safety net, warn loudly rather than silently carrying it over
    if open_trade is not None:
        print(f"WARNING: session ended with an open trade ({open_trade['symbol']}) "
              f"that wasn't closed by the near_close check. It will be re-evaluated "
              f"tomorrow — check the dashboard.")


def is_rate_limit_error(exc):
    """Angel One's rate limiter returns plain text instead of JSON, which
    surfaces through this library as a generic parse-failure exception with
    no distinct type — string matching on the message is the only way to
    tell it apart from a real connectivity or auth failure."""
    msg = str(exc).lower()
    return "exceeding access rate" in msg or "access denied" in msg


def main():
    """Runs forever as a background service: waits for market hours, trades
    through the session, then waits for the next trading day. Designed to
    run under systemd with Restart=always, so it survives crashes too.

    SESSION REUSE + BACKOFF — fixed after a real incident: the previous
    version called login() at the top of every retry, and retried every
    session-ending exception on a flat 60-second timer. Angel One sessions
    are valid for the whole trading day, so re-logging in on every retry was
    pure waste — and worse, when something failed immediately at the start of
    every session (as happened live), that waste became a login roughly once
    a minute for four straight hours. Angel's rate limiter is well documented
    to fire even under normal usage and to stay tripped for a while once it
    does — hammering it every 60 seconds during that window made the outage
    longer, not shorter.

    Now: log in once per day and reuse the session across retries. On a
    rate-limit error specifically, back off for minutes, not seconds, and
    let the wait grow on repeated hits instead of retrying at a fixed
    interval forever.
    """
    print("=" * 60)
    print("PAPER TRADING ENGINE (24/7 daemon) — no real orders will be placed")
    print("=" * 60)

    rm = RiskManager(str(CONFIG_PATH))
    if not rm.is_engine_active():
        rm.activate_engine(reason="paper_trade.py session")

    obj = None
    instruments = None
    last_login_day = None
    last_instrument_refresh_day = None
    consecutive_rate_limit_hits = 0

    RATE_LIMIT_BACKOFF_BASE = 300     # 5 min — Angel's limiter needs real time, not seconds
    RATE_LIMIT_BACKOFF_CAP = 1800     # never wait more than 30 min before trying again
    NORMAL_ERROR_BACKOFF = 60         # non-rate-limit errors (network blip etc) can retry sooner

    while True:
        if not is_market_open():
            now = datetime.now(IST)
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] Market closed. "
                  f"Waiting... (checks every 5 min)")
            obj = None  # force a fresh login when the market reopens
            time.sleep(300)
            continue

        today = datetime.now(IST).date()
        try:
            if obj is None or last_login_day != today:
                obj = login()
                last_login_day = today

            if instruments is None or last_instrument_refresh_day != today:
                instruments = load_instrument_master()
                last_instrument_refresh_day = today

            print(f"\n[{datetime.now(IST).strftime('%H:%M:%S')}] "
                  f"Market open — starting today's session.\n")
            run_trading_session(obj, instruments, rm)
            print(f"\n[{datetime.now(IST).strftime('%H:%M:%S')}] "
                  f"Session ended (market closed for today).")
            consecutive_rate_limit_hits = 0   # a clean session resets the backoff

        except TokenException as e:
            # the one case that genuinely means "the session is dead" —
            # always force a fresh login for this specific failure
            print(f"ERROR: session/token invalid ({e}). Forcing re-login.")
            obj = None
            time.sleep(NORMAL_ERROR_BACKOFF)

        except Exception as e:
            print(f"ERROR in trading session: {e}")

            if is_rate_limit_error(e):
                consecutive_rate_limit_hits += 1
                wait = min(RATE_LIMIT_BACKOFF_BASE * (2 ** (consecutive_rate_limit_hits - 1)),
                          RATE_LIMIT_BACKOFF_CAP)
                print(f"Angel One rate limit hit (#{consecutive_rate_limit_hits} in a row). "
                      f"Backing off {wait//60} min before retrying — repeatedly "
                      f"retrying quickly is what caused this in the first place.")
                # Deliberately NOT forcing re-login here. The live incident
                # showed login succeeding every time; the failure was the very
                # next call. Re-logging in anyway would add an extra API call
                # during the exact window we are trying to reduce calls in,
                # and could itself be what pushes a combined per-client limit
                # over the edge. The existing session is reused for the retry.
                time.sleep(wait)
            else:
                print(f"Waiting {NORMAL_ERROR_BACKOFF}s before retrying...")
                time.sleep(NORMAL_ERROR_BACKOFF)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nStopped by user (Ctrl+C).")
