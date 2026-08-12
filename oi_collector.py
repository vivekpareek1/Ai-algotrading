#!/usr/bin/env python3
"""
oi_collector.py — Nifty/BankNifty intraday market-structure data collector.

WHY THIS EXISTS
---------------
Angel One (and every Indian broker) does NOT retain historical data for expired
F&O contracts. That means option OI, option premiums and futures basis for past
weeks are gone forever — you cannot backtest any OI-based logic on history.

The only fix is to start recording NOW. Every trading day this script runs, you
gain one day of a dataset that cannot be bought or re-created later.

WHAT IT RECORDS (every SNAPSHOT_INTERVAL_SEC, 09:15–15:30 IST, Mon–Fri)
-----------------------------------------------------------------------
  1. Index spot   : LTP, OHLC
  2. Index future : LTP, OI, volume  -> basis (fut - spot) = big-player bias
  3. Option chain : ATM +/- N strikes, CE & PE -> LTP, OI, volume, bid/ask

OUTPUT (per instrument, per day)
--------------------------------
  data/NIFTY/chain_2026-08-12.csv    long format, one row per instrument leg
  data/NIFTY/summary_2026-08-12.csv  one row per snapshot (PCR, max-OI, basis...)

DESIGN NOTES
------------
* Collector is deliberately "dumb": it records raw facts only. All derived
  analytics (OI change, buildup classification, signals) are computed later from
  these CSVs. Never bake strategy logic into a recorder — you cannot re-record.
* ATM is recomputed on every snapshot, so the strike window follows spot.
* Session token is refreshed automatically on expiry.
* Safe to restart mid-day: CSVs are appended, headers written once.

SETUP
-----
  pip install smartapi-python pyotp requests pytz

  angel_credentials.json (chmod 600, gitignored):
  {
    "api_key":    "xxxxxxxx",
    "client_code":"YOUR_CLIENT_CODE",
    "mpin":       "YOUR_MPIN",
    "totp_secret":"BASE32SECRETFROMANGELONE"
  }

  Run:  python3 oi_collector.py
  Test: python3 oi_collector.py --dry-run     (one snapshot, prints, no CSV)
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import pytz
    import pyotp
    import requests
    from SmartApi import SmartConnect
except ImportError as exc:  # pragma: no cover
    sys.exit(f"Missing dependency: {exc}\n"
             f"Run: pip install smartapi-python pyotp requests pytz")


# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CRED_FILE = BASE_DIR / "angel_credentials.json"
DATA_DIR = BASE_DIR / "data"
CACHE_DIR = BASE_DIR / "cache"
LOG_DIR = BASE_DIR / "logs"
HOLIDAY_FILE = BASE_DIR / "nse_holidays.txt"   # optional, one YYYY-MM-DD per line

IST = pytz.timezone("Asia/Kolkata")

SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/"
    "OpenAPIScripMaster.json"
)

# Instruments to record. strike_step must match the exchange's strike spacing.
INSTRUMENTS: List[Dict[str, Any]] = [
    {
        "name": "NIFTY",
        "spot_exchange": "NSE",
        "spot_token": "99926000",      # NIFTY 50 index
        "strike_step": 50,
        "strikes_each_side": 10,       # ATM +/- 10 -> 21 strikes -> 42 option legs
    },
    {
        "name": "BANKNIFTY",
        "spot_exchange": "NSE",
        "spot_token": "99926009",      # NIFTY BANK index
        "strike_step": 100,
        "strikes_each_side": 8,
    },
]

SNAPSHOT_INTERVAL_SEC = 300            # 5 minutes
MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)
PRE_OPEN_START = (9, 10)               # start loop slightly early
QUOTE_BATCH_SIZE = 50                  # Angel One getMarketData limit per call
API_PAUSE_SEC = 1.1                    # stay under ~1 req/sec quote rate limit
MAX_API_RETRIES = 3
STALE_SNAPSHOTS_BEFORE_ABORT = 4       # likely a trading holiday -> stop

_shutdown = False


# ----------------------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = LOG_DIR / f"collector_{now_ist().strftime('%Y-%m-%d')}.log"
    fmt = "%(asctime)s %(levelname)-7s %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[logging.FileHandler(logfile), logging.StreamHandler(sys.stdout)],
    )
    return logging.getLogger("collector")


def now_ist() -> datetime:
    """Always IST, regardless of the server's own timezone."""
    return datetime.now(IST)


# ----------------------------------------------------------------------------
# MARKET CALENDAR
# ----------------------------------------------------------------------------

def load_holidays() -> set:
    if not HOLIDAY_FILE.exists():
        return set()
    out = set()
    for line in HOLIDAY_FILE.read_text().splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        try:
            out.add(date.fromisoformat(line))
        except ValueError:
            pass
    return out


def is_trading_day(d: date, holidays: set) -> bool:
    return d.weekday() < 5 and d not in holidays


def within_market_hours(dt: datetime) -> bool:
    t = (dt.hour, dt.minute)
    return MARKET_OPEN <= t <= MARKET_CLOSE


def seconds_until_open(dt: datetime) -> float:
    target = dt.replace(hour=PRE_OPEN_START[0], minute=PRE_OPEN_START[1],
                        second=0, microsecond=0)
    if dt >= target:
        target += timedelta(days=1)
    return (target - dt).total_seconds()


# ----------------------------------------------------------------------------
# SCRIP MASTER
# ----------------------------------------------------------------------------

def fetch_scrip_master(log: logging.Logger) -> List[Dict[str, Any]]:
    """Download the instrument dump once per day; fall back to cache on failure."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"scrip_{now_ist().strftime('%Y-%m-%d')}.json"

    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("Cached scrip master unreadable, re-downloading")

    try:
        log.info("Downloading scrip master...")
        resp = requests.get(SCRIP_MASTER_URL, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        cache_file.write_text(json.dumps(data))
        # keep only last 5 days of cache
        for old in sorted(CACHE_DIR.glob("scrip_*.json"))[:-5]:
            old.unlink(missing_ok=True)
        log.info("Scrip master: %d instruments", len(data))
        return data
    except Exception as exc:
        log.error("Scrip master download failed: %s", exc)
        stale = sorted(CACHE_DIR.glob("scrip_*.json"))
        if stale:
            log.warning("Falling back to stale cache %s", stale[-1].name)
            return json.loads(stale[-1].read_text())
        raise


def _parse_expiry(raw: str) -> Optional[date]:
    """Angel expiry format is like 28AUG2026."""
    try:
        return datetime.strptime(raw.strip(), "%d%b%Y").date()
    except (ValueError, AttributeError):
        return None


def _parse_strike(raw: Any) -> Optional[float]:
    """Strike in the dump is in paise: '2450000' -> 24500.0"""
    try:
        val = float(raw) / 100.0
        return val if val > 0 else None
    except (TypeError, ValueError):
        return None


def build_universe(master: List[Dict[str, Any]], name: str,
                   log: logging.Logger) -> Dict[str, Any]:
    """
    Pick the nearest non-expired weekly option expiry and the current-month
    future for one underlying, and index every option leg by (strike, CE/PE).
    """
    today = now_ist().date()
    options: Dict[date, Dict[Tuple[float, str], Dict[str, Any]]] = {}
    futures: Dict[date, Dict[str, Any]] = {}

    for row in master:
        if row.get("name") != name or row.get("exch_seg") != "NFO":
            continue
        itype = row.get("instrumenttype", "")
        exp = _parse_expiry(row.get("expiry", ""))
        if exp is None or exp < today:
            continue

        if itype == "OPTIDX":
            strike = _parse_strike(row.get("strike"))
            symbol = row.get("symbol", "")
            if strike is None:
                continue
            if symbol.endswith("CE"):
                opt_type = "CE"
            elif symbol.endswith("PE"):
                opt_type = "PE"
            else:
                continue
            options.setdefault(exp, {})[(strike, opt_type)] = {
                "token": str(row["token"]),
                "symbol": symbol,
                "lotsize": row.get("lotsize"),
            }
        elif itype == "FUTIDX":
            futures[exp] = {
                "token": str(row["token"]),
                "symbol": row.get("symbol", ""),
                "lotsize": row.get("lotsize"),
            }

    if not options:
        raise RuntimeError(f"No live option contracts found for {name}")

    opt_expiry = min(options)
    fut_expiry = min(futures) if futures else None

    log.info("%s -> option expiry %s (%d legs), future expiry %s",
             name, opt_expiry, len(options[opt_expiry]),
             fut_expiry if fut_expiry else "N/A")

    return {
        "opt_expiry": opt_expiry,
        "chain": options[opt_expiry],
        "fut_expiry": fut_expiry,
        "future": futures.get(fut_expiry) if fut_expiry else None,
    }


# ----------------------------------------------------------------------------
# BROKER SESSION
# ----------------------------------------------------------------------------

class AngelSession:
    """Wraps SmartConnect with auto re-login and retry on transient errors."""

    def __init__(self, log: logging.Logger):
        self.log = log
        self.creds = self._load_creds()
        self.api: Optional[SmartConnect] = None
        self.logged_in_at: Optional[datetime] = None

    @staticmethod
    def _load_creds() -> Dict[str, str]:
        if not CRED_FILE.exists():
            sys.exit(f"Credentials file not found: {CRED_FILE}")
        raw = json.loads(CRED_FILE.read_text())
        # accept a few common key spellings so an existing file keeps working
        def pick(*keys: str) -> Optional[str]:
            for k in keys:
                if raw.get(k):
                    return str(raw[k])
            return None
        creds = {
            "api_key": pick("api_key", "apiKey", "API_KEY"),
            "client_code": pick("client_code", "clientCode", "client_id"),
            "mpin": pick("mpin", "MPIN", "pin", "password"),
            "totp_secret": pick("totp_secret", "totp", "totpSecret", "totp_key"),
        }
        missing = [k for k, v in creds.items() if not v]
        if missing:
            sys.exit(f"Missing keys in {CRED_FILE.name}: {', '.join(missing)}")
        return creds

    def login(self) -> None:
        totp = pyotp.TOTP(self.creds["totp_secret"]).now()
        api = SmartConnect(api_key=self.creds["api_key"])
        resp = api.generateSession(self.creds["client_code"],
                                   self.creds["mpin"], totp)
        if not resp or not resp.get("status"):
            msg = resp.get("message") if isinstance(resp, dict) else resp
            raise RuntimeError(f"Login failed: {msg}")
        self.api = api
        self.logged_in_at = now_ist()
        self.log.info("Logged in as %s", self.creds["client_code"])

    def ensure_session(self) -> None:
        if self.api is None:
            self.login()

    def quotes(self, exchange_tokens: Dict[str, List[str]]) -> Dict[str, Any]:
        """
        FULL-mode quote for a token map. Chunks to QUOTE_BATCH_SIZE and merges,
        because Angel One rejects oversized batches outright.
        """
        self.ensure_session()

        flat = [(ex, tok) for ex, toks in exchange_tokens.items() for tok in toks]
        merged: Dict[str, Any] = {}

        for i in range(0, len(flat), QUOTE_BATCH_SIZE):
            chunk = flat[i:i + QUOTE_BATCH_SIZE]
            payload: Dict[str, List[str]] = {}
            for ex, tok in chunk:
                payload.setdefault(ex, []).append(tok)

            data = self._call_with_retry(payload)
            for item in data:
                merged[str(item.get("symbolToken"))] = item

            if i + QUOTE_BATCH_SIZE < len(flat):
                time.sleep(API_PAUSE_SEC)

        return merged

    def _call_with_retry(self, payload: Dict[str, List[str]]) -> List[Dict[str, Any]]:
        last_exc: Optional[Exception] = None
        for attempt in range(1, MAX_API_RETRIES + 1):
            try:
                resp = self.api.getMarketData("FULL", payload)
                if resp and resp.get("status"):
                    return resp.get("data", {}).get("fetched", []) or []
                msg = (resp or {}).get("message", "unknown error")
                raise RuntimeError(msg)
            except Exception as exc:
                last_exc = exc
                text = str(exc).lower()
                self.log.warning("Quote attempt %d/%d failed: %s",
                                 attempt, MAX_API_RETRIES, exc)
                if "token" in text or "session" in text or "unauthor" in text:
                    self.log.info("Re-authenticating...")
                    try:
                        self.login()
                    except Exception as login_exc:
                        self.log.error("Re-login failed: %s", login_exc)
                time.sleep(2 * attempt)
        raise RuntimeError(f"Quote call failed after retries: {last_exc}")


# ----------------------------------------------------------------------------
# CSV WRITERS
# ----------------------------------------------------------------------------

CHAIN_FIELDS = [
    "timestamp", "underlying", "leg_type", "expiry", "strike", "opt_type",
    "symbol", "token", "ltp", "open", "high", "low", "close", "prev_close",
    "volume", "oi", "bid", "ask", "bid_qty", "ask_qty",
]

SUMMARY_FIELDS = [
    "timestamp", "underlying", "spot", "atm_strike",
    "fut_ltp", "fut_oi", "fut_volume", "basis", "basis_pct",
    "total_ce_oi", "total_pe_oi", "pcr_oi",
    "total_ce_vol", "total_pe_vol", "pcr_volume",
    "max_ce_oi_strike", "max_ce_oi", "max_pe_oi_strike", "max_pe_oi",
    "legs_captured",
]


def append_rows(path: Path, fields: List[str], rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(rows)
        fh.flush()
        os.fsync(fh.fileno())      # survive an abrupt EC2 stop


# ----------------------------------------------------------------------------
# SNAPSHOT
# ----------------------------------------------------------------------------

def _num(value: Any) -> Optional[float]:
    try:
        if value in (None, "", "-"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _depth_top(quote: Dict[str, Any], side: str) -> Tuple[Optional[float], Optional[float]]:
    """Best bid/ask price and quantity from the FULL-mode depth block."""
    depth = (quote.get("depth") or {}).get(side) or []
    if not depth:
        return None, None
    top = depth[0] or {}
    return _num(top.get("price")), _num(top.get("quantity"))


def take_snapshot(session: AngelSession, inst: Dict[str, Any],
                  universe: Dict[str, Any], log: logging.Logger
                  ) -> Optional[Tuple[List[Dict[str, Any]], Dict[str, Any]]]:
    """One full capture: spot -> ATM -> strike window -> quotes -> rows."""
    name = inst["name"]
    ts = now_ist().strftime("%Y-%m-%d %H:%M:%S")

    # 1. Spot first — ATM depends on it.
    spot_quotes = session.quotes({inst["spot_exchange"]: [inst["spot_token"]]})
    spot_q = spot_quotes.get(inst["spot_token"])
    spot = _num(spot_q.get("ltp")) if spot_q else None
    if spot is None:
        log.error("%s: no spot LTP, skipping snapshot", name)
        return None

    step = inst["strike_step"]
    atm = round(spot / step) * step
    span = inst["strikes_each_side"]
    strikes = [atm + i * step for i in range(-span, span + 1)]

    # 2. Assemble the token list: options in window + current-month future.
    chain = universe["chain"]
    legs: List[Dict[str, Any]] = []
    for strike in strikes:
        for opt_type in ("CE", "PE"):
            meta = chain.get((float(strike), opt_type))
            if meta:
                legs.append({"leg_type": "OPT", "strike": strike,
                             "opt_type": opt_type, **meta})

    fut = universe.get("future")
    if fut:
        legs.append({"leg_type": "FUT", "strike": None, "opt_type": None, **fut})

    if not legs:
        log.error("%s: no matching contracts near ATM %s", name, atm)
        return None

    time.sleep(API_PAUSE_SEC)
    leg_quotes = session.quotes({"NFO": [leg["token"] for leg in legs]})

    # 3. Flatten into rows.
    rows: List[Dict[str, Any]] = []
    spot_bid, spot_bid_qty = _depth_top(spot_q, "buy")
    spot_ask, spot_ask_qty = _depth_top(spot_q, "sell")
    rows.append({
        "timestamp": ts, "underlying": name, "leg_type": "SPOT",
        "expiry": "", "strike": "", "opt_type": "",
        "symbol": spot_q.get("tradingSymbol", name), "token": inst["spot_token"],
        "ltp": spot, "open": _num(spot_q.get("open")), "high": _num(spot_q.get("high")),
        "low": _num(spot_q.get("low")), "close": _num(spot_q.get("close")),
        "prev_close": _num(spot_q.get("close")),
        "volume": _num(spot_q.get("tradeVolume")), "oi": None,
        "bid": spot_bid, "ask": spot_ask,
        "bid_qty": spot_bid_qty, "ask_qty": spot_ask_qty,
    })

    ce_oi = pe_oi = ce_vol = pe_vol = 0.0
    max_ce = (None, -1.0)
    max_pe = (None, -1.0)
    fut_ltp = fut_oi = fut_vol = None

    for leg in legs:
        q = leg_quotes.get(leg["token"])
        if not q:
            continue
        bid, bid_qty = _depth_top(q, "buy")
        ask, ask_qty = _depth_top(q, "sell")
        oi = _num(q.get("opnInterest"))
        vol = _num(q.get("tradeVolume"))
        expiry = (universe["fut_expiry"] if leg["leg_type"] == "FUT"
                  else universe["opt_expiry"])

        rows.append({
            "timestamp": ts, "underlying": name, "leg_type": leg["leg_type"],
            "expiry": expiry.isoformat() if expiry else "",
            "strike": leg["strike"] if leg["strike"] is not None else "",
            "opt_type": leg["opt_type"] or "", "symbol": leg["symbol"],
            "token": leg["token"], "ltp": _num(q.get("ltp")),
            "open": _num(q.get("open")), "high": _num(q.get("high")),
            "low": _num(q.get("low")), "close": _num(q.get("close")),
            "prev_close": _num(q.get("close")), "volume": vol, "oi": oi,
            "bid": bid, "ask": ask, "bid_qty": bid_qty, "ask_qty": ask_qty,
        })

        if leg["leg_type"] == "FUT":
            fut_ltp, fut_oi, fut_vol = _num(q.get("ltp")), oi, vol
        elif leg["opt_type"] == "CE":
            ce_oi += oi or 0.0
            ce_vol += vol or 0.0
            if (oi or 0) > max_ce[1]:
                max_ce = (leg["strike"], oi or 0.0)
        elif leg["opt_type"] == "PE":
            pe_oi += oi or 0.0
            pe_vol += vol or 0.0
            if (oi or 0) > max_pe[1]:
                max_pe = (leg["strike"], oi or 0.0)

    basis = (fut_ltp - spot) if fut_ltp is not None else None

    summary = {
        "timestamp": ts, "underlying": name, "spot": spot, "atm_strike": atm,
        "fut_ltp": fut_ltp, "fut_oi": fut_oi, "fut_volume": fut_vol,
        "basis": round(basis, 2) if basis is not None else None,
        "basis_pct": round(basis / spot * 100, 4) if basis is not None else None,
        "total_ce_oi": ce_oi, "total_pe_oi": pe_oi,
        "pcr_oi": round(pe_oi / ce_oi, 4) if ce_oi else None,
        "total_ce_vol": ce_vol, "total_pe_vol": pe_vol,
        "pcr_volume": round(pe_vol / ce_vol, 4) if ce_vol else None,
        "max_ce_oi_strike": max_ce[0], "max_ce_oi": max_ce[1] if max_ce[0] else None,
        "max_pe_oi_strike": max_pe[0], "max_pe_oi": max_pe[1] if max_pe[0] else None,
        "legs_captured": len(rows),
    }
    return rows, summary


# ----------------------------------------------------------------------------
# MAIN LOOP
# ----------------------------------------------------------------------------

def _handle_signal(signum, _frame):
    global _shutdown
    _shutdown = True
    logging.getLogger("collector").info("Signal %s received, finishing up...", signum)


def run(dry_run: bool = False) -> None:
    log = setup_logging()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    holidays = load_holidays()
    session = AngelSession(log)
    session.login()

    master_day: Optional[date] = None
    universes: Dict[str, Dict[str, Any]] = {}
    last_spot: Dict[str, float] = {}
    stale_count = 0

    while not _shutdown:
        now = now_ist()
        today = now.date()

        if not is_trading_day(today, holidays):
            if dry_run:
                log.warning("Not a trading day — running one snapshot anyway")
            else:
                wait = seconds_until_open(now)
                log.info("Non-trading day. Sleeping %.1f h", wait / 3600)
                time.sleep(min(wait, 3600))
                continue

        if not within_market_hours(now) and not dry_run:
            if (now.hour, now.minute) > MARKET_CLOSE:
                log.info("Market closed. Day complete.")
                if os.environ.get("COLLECTOR_EXIT_AT_CLOSE") == "1":
                    break
            wait = min(seconds_until_open(now), 900)
            time.sleep(wait)
            continue

        # Refresh contract universe once per day (weekly expiry rolls over).
        if master_day != today:
            try:
                master = fetch_scrip_master(log)
                universes = {i["name"]: build_universe(master, i["name"], log)
                             for i in INSTRUMENTS}
                master_day = today
                stale_count = 0
            except Exception as exc:
                log.error("Universe build failed: %s — retrying in 60s", exc)
                time.sleep(60)
                continue

        cycle_start = time.time()
        any_moved = False

        for inst in INSTRUMENTS:
            if _shutdown:
                break
            name = inst["name"]
            try:
                result = take_snapshot(session, inst, universes[name], log)
                if result is None:
                    continue
                rows, summary = result

                if last_spot.get(name) is not None and summary["spot"] != last_spot[name]:
                    any_moved = True
                last_spot[name] = summary["spot"]

                if dry_run:
                    log.info("[DRY] %s spot=%s atm=%s basis=%s pcr=%s legs=%d",
                             name, summary["spot"], summary["atm_strike"],
                             summary["basis"], summary["pcr_oi"],
                             summary["legs_captured"])
                else:
                    day = today.isoformat()
                    append_rows(DATA_DIR / name / f"chain_{day}.csv",
                                CHAIN_FIELDS, rows)
                    append_rows(DATA_DIR / name / f"summary_{day}.csv",
                                SUMMARY_FIELDS, [summary])
                    log.info("%s spot=%s atm=%s basis=%s pcr_oi=%s legs=%d",
                             name, summary["spot"], summary["atm_strike"],
                             summary["basis"], summary["pcr_oi"], len(rows))
            except Exception as exc:
                log.exception("%s snapshot failed: %s", name, exc)

            time.sleep(API_PAUSE_SEC)

        if dry_run:
            log.info("Dry run complete.")
            return

        # Undeclared holiday guard: prices frozen across several cycles.
        if last_spot and not any_moved:
            stale_count += 1
            if stale_count >= STALE_SNAPSHOTS_BEFORE_ABORT:
                log.warning("Prices unchanged for %d cycles — likely a market "
                            "holiday. Pausing for 1 hour.", stale_count)
                time.sleep(3600)
                stale_count = 0
        else:
            stale_count = 0

        elapsed = time.time() - cycle_start
        sleep_for = max(5.0, SNAPSHOT_INTERVAL_SEC - elapsed)
        for _ in range(int(sleep_for)):
            if _shutdown:
                break
            time.sleep(1)

    log.info("Collector stopped cleanly.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nifty OI/futures data collector")
    parser.add_argument("--dry-run", action="store_true",
                        help="take one snapshot, print it, write nothing")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
