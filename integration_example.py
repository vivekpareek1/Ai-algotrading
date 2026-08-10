"""
integration_example.py  (v2)
=============================
How each strategy engine plugs into RiskManager.

The v2 API adds a RESERVATION step. The pattern is always three calls:

    1. size_trade()          -> reserves risk, returns reservation_id
    2. place order at broker
    3a. record_open(reservation_id=...)   if filled
    3b. cancel_reservation(reservation_id) if NOT filled

Step 3 is not optional. If you skip it the risk stays reserved until it
expires (3 min), and meanwhile other strategies see less headroom than
they really have.
"""

from risk_manager import RiskManager

# Point every engine at the SAME config.json path so they share one pool.
rm = RiskManager("/opt/trading/risk/config.json")


# --------------------------------------------------------------------- #
# 1) Nifty / Bank Nifty options indicator (Angel One SmartAPI)
# --------------------------------------------------------------------- #
def nifty_place_trade(signal):
    """signal = {symbol, direction, entry_price, stop_loss, lot_size}"""
    d = rm.size_trade(
        strategy_name="nifty_options_indicator",
        entry_price=signal["entry_price"],
        stop_loss_price=signal["stop_loss"],
        lot_size=signal["lot_size"],
        symbol=signal["symbol"],
        direction=signal["direction"],
    )

    if not d["approved"]:
        print(f"[RISK] blocked: {d['reason']}")
        return None

    if d["sized_down"]:
        print(f"[RISK] note: {d['reason']}")

    try:
        # --- place order via Angel One SmartAPI ---
        # resp = smartapi.placeOrder(tradingsymbol=signal["symbol"],
        #                            quantity=d["quantity"], ...)
        # filled_qty = int(resp["filledshares"])
        filled_qty = d["quantity"]   # replace with the broker's real fill qty

        if filled_qty <= 0:
            rm.cancel_reservation(d["reservation_id"], "order not filled")
            return None

        trade_id = rm.record_open(
            strategy_name="nifty_options_indicator",
            symbol=signal["symbol"], direction=signal["direction"],
            entry_price=signal["entry_price"], stop_loss_price=signal["stop_loss"],
            quantity=filled_qty,
            reservation_id=d["reservation_id"],
        )
        return trade_id

    except Exception as e:
        # CRITICAL: always release the reservation if anything goes wrong,
        # otherwise the risk stays locked up for 3 minutes.
        rm.cancel_reservation(d["reservation_id"], f"error: {e}")
        raise


def nifty_close_trade(trade_id, exit_price, quantity=None):
    """quantity=None closes the whole position; pass a number for partial exit."""
    return rm.record_close(trade_id, exit_price, quantity=quantity)


# --------------------------------------------------------------------- #
# 2) Arbitrage scanner — BOTH legs must be approved before either is placed
# --------------------------------------------------------------------- #
def arbitrage_place_trade(leg1, leg2):
    d1 = rm.size_trade("nifty_arbitrage_scanner", leg1["entry_price"],
                       leg1["stop_loss"], leg1["lot_size"],
                       leg1["symbol"], leg1["direction"])
    d2 = rm.size_trade("nifty_arbitrage_scanner", leg2["entry_price"],
                       leg2["stop_loss"], leg2["lot_size"],
                       leg2["symbol"], leg2["direction"])

    if not (d1["approved"] and d2["approved"]):
        # release whichever leg WAS approved — never leave one reserved
        rm.cancel_reservation(d1.get("reservation_id"), "paired leg rejected")
        rm.cancel_reservation(d2.get("reservation_id"), "paired leg rejected")
        print("[RISK] arbitrage pair blocked — never place a single leg")
        return None, None

    # if the two legs were sized differently, scale both to the smaller
    # ratio so the hedge stays balanced (arbitrage only works matched)
    if d1["lots"] != d2["lots"]:
        print(f"[RISK] legs sized unequally ({d1['lots']} vs {d2['lots']} lots). "
              f"Cancelling — rebuild with matched sizing before placing.")
        rm.cancel_reservation(d1["reservation_id"], "unequal leg sizing")
        rm.cancel_reservation(d2["reservation_id"], "unequal leg sizing")
        return None, None

    t1 = rm.record_open("nifty_arbitrage_scanner", leg1["symbol"], leg1["direction"],
                        leg1["entry_price"], leg1["stop_loss"], d1["quantity"],
                        reservation_id=d1["reservation_id"])
    t2 = rm.record_open("nifty_arbitrage_scanner", leg2["symbol"], leg2["direction"],
                        leg2["entry_price"], leg2["stop_loss"], d2["quantity"],
                        reservation_id=d2["reservation_id"])
    return t1, t2


# --------------------------------------------------------------------- #
# 3) MCX Gold/Silver
# --------------------------------------------------------------------- #
def mcx_place_trade(signal):
    d = rm.size_trade("mcx_gold_silver", signal["entry_price"], signal["stop_loss"],
                      signal["lot_size"], signal["symbol"], signal["direction"])
    if not d["approved"]:
        print(f"[RISK] blocked: {d['reason']}")
        return None
    try:
        filled_qty = d["quantity"]   # replace with broker's real fill
        return rm.record_open("mcx_gold_silver", signal["symbol"], signal["direction"],
                              signal["entry_price"], signal["stop_loss"], filled_qty,
                              reservation_id=d["reservation_id"])
    except Exception as e:
        rm.cancel_reservation(d["reservation_id"], f"error: {e}")
        raise


# --------------------------------------------------------------------- #
# 4) Feed live prices so the kill switch also sees UNREALIZED loss.
#    Without this it only reacts to closed trades — you could be deep in
#    the red on open positions and still get new trades approved.
#    Call this from whatever loop already receives price ticks.
# --------------------------------------------------------------------- #
def on_price_tick(price_map):
    """price_map = {trade_id: current_price}"""
    for trade_id, price in price_map.items():
        rm.update_mark(trade_id, price)


def refresh_all_marks(get_price_fn):
    """Convenience: re-mark every open position. Run once a minute."""
    for trade_id, pos in rm.list_open_positions().items():
        try:
            rm.update_mark(trade_id, get_price_fn(pos["symbol"]))
        except Exception as e:
            print(f"[RISK] could not mark {pos['symbol']}: {e}")


# --------------------------------------------------------------------- #
# Daily status check (wire this to cron + Telegram/email)
# --------------------------------------------------------------------- #
if __name__ == "__main__":
    import json
    print(json.dumps(rm.get_status(), indent=2))
