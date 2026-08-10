"""
fetch_historical_data.py
=========================
Downloads real historical candle data for Nifty 50 (or Bank Nifty) from
YOUR Angel One account via SmartAPI, for the past 1 year, and saves it
as a clean CSV for backtesting.

This is REAL market data — actual traded prices, not synthetic.

SETUP (do this once):
    pip3 install smartapi-python pyotp pandas --break-system-packages

CREDENTIALS — create a separate file so they never touch git:
    Create angel_credentials.json in this same folder (NOT committed to
    GitHub — it's in .gitignore) with:
    {
        "api_key": "your_api_key",
        "client_code": "your_client_code",
        "password": "your_password_or_mpin",
        "totp_secret": "your_totp_secret_key"
    }

    The totp_secret is the key you got when you enabled TOTP at
    smartapi.angelbroking.com/enable-totp (NOT the 6-digit code itself —
    the secret that generates those codes). If you only have the app-based
    authenticator set up, you'll need to re-enable TOTP on that page to
    get the raw secret key for this script to use.

RUN:
    python3 fetch_historical_data.py

Angel One limits how much data you can request per single API call
(shorter timeframes = shorter allowed date range per request), so this
script automatically fetches in chunks and stitches them together.
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pyotp
from SmartApi import SmartConnect

CREDS_PATH = Path(__file__).parent / "angel_credentials.json"
OUTPUT_CSV = Path(__file__).parent / "nifty_historical_1year.csv"

# Well-known NSE index tokens (verify against Angel One's OpenAPIScripMaster
# if this ever returns no data — token mappings can change).
SYMBOL_TOKENS = {
    "NIFTY": {"token": "99926000", "exchange": "NSE"},
    "BANKNIFTY": {"token": "99926009", "exchange": "NSE"},
}

# Angel One's documented max days per single request, by interval.
# Being conservative (a few days under the documented max) to avoid
# edge-case rejections.
MAX_DAYS_PER_REQUEST = {
    "ONE_MINUTE": 25,
    "THREE_MINUTE": 55,
    "FIVE_MINUTE": 95,
    "FIFTEEN_MINUTE": 180,
    "THIRTY_MINUTE": 180,
    "ONE_HOUR": 180,
    "ONE_DAY": 1800,
}


def login():
    if not CREDS_PATH.exists():
        raise FileNotFoundError(
            f"{CREDS_PATH} not found. Create it first — see the instructions "
            f"at the top of this script."
        )
    creds = json.loads(CREDS_PATH.read_text())

    obj = SmartConnect(api_key=creds["api_key"])
    totp = pyotp.TOTP(creds["totp_secret"]).now()
    data = obj.generateSession(creds["client_code"], creds["password"], totp)

    if not data.get("status"):
        raise RuntimeError(f"Login failed: {data}")

    print("Logged in successfully.")
    return obj


def fetch_range(obj, symbol_info, interval, from_dt, to_dt):
    params = {
        "exchange": symbol_info["exchange"],
        "symboltoken": symbol_info["token"],
        "interval": interval,
        "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"),
        "todate": to_dt.strftime("%Y-%m-%d %H:%M"),
    }
    resp = obj.getCandleData(params)
    if not resp.get("status"):
        print(f"  Warning: chunk {from_dt.date()} to {to_dt.date()} failed: "
              f"{resp.get('message')}")
        return []
    return resp.get("data", [])


def fetch_full_year(obj, symbol="NIFTY", interval="FIFTEEN_MINUTE", days=365):
    if symbol not in SYMBOL_TOKENS:
        raise ValueError(f"Unknown symbol '{symbol}'. Add it to SYMBOL_TOKENS.")
    symbol_info = SYMBOL_TOKENS[symbol]

    chunk_days = MAX_DAYS_PER_REQUEST.get(interval, 25)
    end = datetime.now()
    start = end - timedelta(days=days)

    all_rows = []
    cursor = start
    chunk_num = 0
    total_chunks = (days // chunk_days) + 1

    print(f"Fetching {days} days of {symbol} {interval} data "
          f"in ~{total_chunks} chunks...")

    while cursor < end:
        chunk_end = min(cursor + timedelta(days=chunk_days), end)
        chunk_num += 1
        print(f"  Chunk {chunk_num}/{total_chunks}: "
              f"{cursor.date()} to {chunk_end.date()}...", end=" ")

        rows = fetch_range(obj, symbol_info, interval, cursor, chunk_end)
        all_rows.extend(rows)
        print(f"got {len(rows)} candles")

        cursor = chunk_end
        time.sleep(0.4)  # stay well under Angel One's rate limit

    return all_rows


def rows_to_csv(rows, output_path):
    if not rows:
        print("No data returned — nothing to save. Check your token/symbol "
              "and that the market was open in this date range.")
        return

    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low",
                                      "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp")
    df.to_csv(output_path, index=False)
    print(f"\nSaved {len(df)} candles to {output_path}")
    print(f"Date range: {df['timestamp'].min()} to {df['timestamp'].max()}")


if __name__ == "__main__":
    obj = login()
    rows = fetch_full_year(obj, symbol="NIFTY", interval="FIFTEEN_MINUTE", days=365)
    rows_to_csv(rows, OUTPUT_CSV)
