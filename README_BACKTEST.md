# Fetching real historical data for backtesting

## One-time setup

1. Install dependencies:
   ```
   pip3 install smartapi-python pyotp pandas --break-system-packages
   ```

2. Copy `angel_credentials.json.example` to `angel_credentials.json` and
   fill in your real Angel One API key, client code, password/MPIN, and
   TOTP secret. **Never commit this file** — it's already in .gitignore.

   The `totp_secret` is NOT the 6-digit code — it's the underlying secret
   key from when you enabled TOTP at smartapi.angelbroking.com/enable-totp.
   If you don't have that saved, you'll need to re-enable TOTP there to
   get it again.

## Run it

```
python3 fetch_historical_data.py
```

This logs in, fetches 1 year of 15-minute Nifty candles in chunks
(Angel One limits how much you can request per call), and saves everything
to `nifty_historical_1year.csv`.

To fetch Bank Nifty instead, or a different interval, edit the last two
lines of the script:
```python
rows = fetch_full_year(obj, symbol="BANKNIFTY", interval="FIFTEEN_MINUTE", days=365)
```
Valid intervals: ONE_MINUTE, THREE_MINUTE, FIVE_MINUTE, FIFTEEN_MINUTE,
THIRTY_MINUTE, ONE_HOUR, ONE_DAY.

## What's next

Once you have `nifty_historical_1year.csv`, that's real historical data
we can run your actual indicator logic against, then feed those simulated
trades through the risk module — showing exactly how your capital and
risk limits would have behaved over the last year.
