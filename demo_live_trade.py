"""
demo_live_trade.py
===================
SAFE DEMO — places a FAKE trade through the risk module so you can watch
the dashboard update live. No real broker, no real money, nothing actually
bought or sold. Just proves the whole pipeline works end to end.

Run it, then immediately open your dashboard in the browser and watch the
numbers change in real time.
"""

import time
from risk_manager import RiskManager

rm = RiskManager("config.json")

print("=" * 50)
print("DEMO: simulating a Nifty options trade")
print("=" * 50)

if not rm.is_engine_active():
    print("\nEngine is currently OFF (this is the safety default).")
    print("Activating it now for this demo...")
    rm.activate_engine(reason="demo_live_trade.py")
    print("Engine is now ON. Real engines will need this done too — via")
    print("the dashboard or rm.activate_engine() — before they can trade.")

# Step 1: size the trade (same as a real engine would do)
d = rm.size_trade(
    strategy_name="nifty_options_indicator",
    entry_price=182.5,
    stop_loss_price=165.0,
    lot_size=75,
    symbol="DEMO-NIFTY-CE",
    direction="BUY",
)
print(f"\n1) Sized: approved={d['approved']}, quantity={d['quantity']}, "
      f"risk=Rs.{d['risk_amount']}")

if not d["approved"]:
    print("Blocked:", d["reason"])
    exit()

print("\n>>> Check your dashboard now — Open Positions should still show 0")
print(">>> (risk is only RESERVED, not yet an open position)")
input("\nPress Enter to simulate the broker filling the order...")

# Step 2: "fill" the order (in real life, this happens after your broker confirms)
trade_id = rm.record_open(
    strategy_name="nifty_options_indicator",
    symbol="DEMO-NIFTY-CE", direction="BUY",
    entry_price=182.5, stop_loss_price=165.0,
    quantity=d["quantity"], reservation_id=d["reservation_id"],
)
print(f"\n2) Trade opened: {trade_id}")
print("\n>>> Refresh your dashboard NOW — Open Positions should show 1,")
print(">>> with symbol DEMO-NIFTY-CE, and Capital Deployed should be non-zero")
input("\nPress Enter to simulate the price moving up and closing in profit...")

# Step 3: close the trade at a profit
pnl = rm.record_close(trade_id, exit_price=210.0)
print(f"\n3) Trade closed. P&L: Rs.{pnl}")
print("\n>>> Refresh your dashboard NOW — Open Positions back to 0,")
print(">>> Realized P&L Today should show your profit")

print("\n" + "=" * 50)
print("Demo complete. This is exactly what a real engine would do —")
print("just replace the fake entry/exit prices with real broker calls.")
print("=" * 50)
