import json
import os
import time
import threading
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests
import websocket
import dash
from dash import dcc, html
from dash.dependencies import Input, Output
from flask import Flask, request, jsonify
from pymongo import MongoClient



# ==========================
# Config
# ==========================
SYMBOL = "ethusdc" 
INTERVAL = "15m"
CANDLE_LIMIT = 10
PING_URL = os.environ.get("PING_URL", "https://bot-reviver.onrender.com/ping")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://binance-65gz.onrender.com/web")

LENGTH = 1

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = "trading_bot_eth_longonly_6-10"
COLLECTION_STATE = "bot_state"

KOLKATA_TZ = timezone(timedelta(hours=5, minutes=30))
STORE_TZ = timezone.utc

# ==========================
# Globals
# ==========================
candles = pd.DataFrame(columns=["time", "Open", "High", "Low", "Close"])  # 'time' is UTC-aware
live_price = None
last_valid_price = None
alerts = []
unfilled_alerts = []

initial_balance = 10.0
entryprice = None
running_pnl = 0.0

upper_bound = None
lower_bound = None
_bounds_candle_ts = None  # UTC datetime of the candle used to compute bounds
_triggered_window_id = None
_triggered_window_side = None
_last_exit_lock = None

EntryCount = 0
LastSide = None
LastLastSide = "buy"
status = None

# Order filling data
fillcheck = 0
fillcount = 0
totaltradecount = 0
unfilledpnl = 0.0
lastpnl = 0


# ==========================
# MongoDB Setup
# ==========================
mongo_client = MongoClient(MONGO_URI)
db = mongo_client[DB_NAME]
state_col = db[COLLECTION_STATE]


def _serialize_candles(df: pd.DataFrame):
    """Return list[dict] with 'time' as ISO strings for storage."""
    if df is None or df.empty:
        return []
    tmp = df.copy()
    def _iso_safe(x):
        if pd.isna(x):
            return None
        if isinstance(x, str):
            return x
        # ensure timezone-aware
        if getattr(x, "tzinfo", None) is None:
            x = x.replace(tzinfo=STORE_TZ)
        return x.isoformat()
    tmp["time"] = tmp["time"].apply(_iso_safe)
    return tmp.to_dict(orient="records")


def _deserialize_candles(records):
    """Turn stored records (with time iso) into DataFrame with timezone-aware datetimes (UTC)."""
    if not records:
        return pd.DataFrame(columns=["time", "Open", "High", "Low", "Close"])
    df = pd.DataFrame(records)
    if "time" in df.columns:
        # parse ISO strings into timezone-aware UTC datetimes
        df["time"] = pd.to_datetime(df["time"], utc=True)
        # ensure tz is STORE_TZ (UTC)
        df["time"] = df["time"].dt.tz_convert(STORE_TZ)
    return df


def save_state():
    """Save global state + candles + alerts to MongoDB."""
    global candles, live_price, last_valid_price, alerts
    global initial_balance, entryprice, running_pnl
    global upper_bound, lower_bound, _bounds_candle_ts, _triggered_window_id
    global EntryCount, LastSide, LastLastSide, status
    global fillcheck, fillcount, totaltradecount, unfilledpnl

    try:
        state = {
            "_id": "bot_state",
            "candles": _serialize_candles(candles),
            "live_price": float(live_price) if is_valid_price(live_price) else None,
            "last_valid_price": float(last_valid_price) if is_valid_price(last_valid_price) else None,
            "alerts": alerts[-100:],  # keep last 100
            "initial_balance": float(initial_balance),
            "entryprice": float(entryprice) if is_valid_price(entryprice) else None,
            "running_pnl": float(running_pnl),
            "upper_bound": float(upper_bound) if is_valid_price(upper_bound) else None,
            "lower_bound": float(lower_bound) if is_valid_price(lower_bound) else None,
            "_bounds_candle_ts": _bounds_candle_ts.isoformat() if _bounds_candle_ts else None,
            "_triggered_window_id": _triggered_window_id.isoformat() if _triggered_window_id else None,
            "_triggered_window_side": _triggered_window_side,
            "_last_exit_lock":_last_exit_lock,
            "EntryCount": int(EntryCount),
            "LastSide": LastSide,
            "LastLastSide": LastLastSide,
            "status": status,
            "fillcheck": int(fillcheck),
            "fillcount": int(fillcount),
            "totaltradecount": int(totaltradecount),
            "unfilledpnl": float(unfilledpnl)
        }
        state_col.replace_one({"_id": "bot_state"}, state, upsert=True)
    except Exception as e:
        print("⚠️ Error saving state to MongoDB:", e)


def load_state():
    """Restore global state from MongoDB."""
    global candles, live_price, last_valid_price, alerts
    global initial_balance, entryprice, running_pnl
    global upper_bound, lower_bound, _bounds_candle_ts, _triggered_window_id
    global EntryCount, LastSide, LastLastSide, status
    global fillcheck, fillcount, totaltradecount, unfilledpnl

    try:
        doc = state_col.find_one({"_id": "bot_state"})
        if not doc:
            print("ℹ️ No saved state found in DB, fresh start.")
            return

        candles = _deserialize_candles(doc.get("candles", []))
        live_price = doc.get("live_price")
        last_valid_price = doc.get("last_valid_price")
        alerts = doc.get("alerts", []) or []

        initial_balance = doc.get("initial_balance", 10.0)
        entryprice = doc.get("entryprice")
        running_pnl = doc.get("running_pnl", 0.0)

        upper_bound = doc.get("upper_bound")
        lower_bound = doc.get("lower_bound")

        _bounds_candle_ts = (
            pd.to_datetime(doc.get("_bounds_candle_ts")).tz_convert(STORE_TZ)
            if doc.get("_bounds_candle_ts") else None
        )
        _triggered_window_id = (
            pd.to_datetime(doc.get("_triggered_window_id")).tz_convert(STORE_TZ)
            if doc.get("_triggered_window_id") else None
        )
        _triggered_window_side = doc.get("_triggered_window_side","buy")
        _last_exit_lock = doc.get("_last_exit_lock","unlock")

        EntryCount = doc.get("EntryCount", 0)
        LastSide = doc.get("LastSide")
        LastLastSide = doc.get("LastLastSide", "buy")
        status = doc.get("status")

        fillcheck = doc.get("fillcheck", 0)
        fillcount = doc.get("fillcount", 0)
        totaltradecount = doc.get("totaltradecount", 0)
        unfilledpnl = doc.get("unfilledpnl", 0.0)

        print("✅ State restored from MongoDB")
    except Exception as e:
        print("⚠️ Error loading state from MongoDB:", e)


# ==========================
# Utilities
# ==========================

def is_valid_price(x):
    try:
        return float(x) > 0
    except Exception:
        return False


def fmt_price(p):
    if p is None:
        return "--"
    ap = abs(p)
    if ap >= 100:
        return f"{p:.2f}"
    elif ap >= 1:
        return f"{p:.4f}"
    else:
        return f"{p:.8f}"


# ==========================
# Helpers
# ==========================

def fetch_initial_candles():
    """Fetch last CANDLE_LIMIT klines from Binance Futures API and seed the DF.
    Ensures timestamps are stored in UTC (STORE_TZ)."""
    global candles, live_price, last_valid_price
    url = (
        f"https://fapi.binance.com/fapi/v1/klines?symbol={SYMBOL.upper()}&interval={INTERVAL}&limit={CANDLE_LIMIT}"
    )
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()

    rows = []
    for k in data:
        rows.append(
            {
                "time": datetime.fromtimestamp(k[0] / 1000, tz=STORE_TZ),  # UTC
                "Open": float(k[1]),
                "High": float(k[2]),
                "Low": float(k[3]),
                "Close": float(k[4]),
            }
        )
    candles = pd.DataFrame(rows)
    if not candles.empty:
        live_price = float(candles["Close"].iloc[-1])
        last_valid_price = live_price if is_valid_price(live_price) else None
    save_state()


def calculate_pnl(entry_price: float, closing_price: float, side: str) -> float:
    quantity = 0.005  # fixed quantity
    if side == "buy":
        return (closing_price - entry_price) * quantity
    elif side == "sell":
        return (entry_price - closing_price) * quantity
    else:
        return 0.0


# def send_webhook(trigger_time_iso: str, entry_price_in: float, side: str, status_fun: str):
#     global EntryCount, LastSide, status, entryprice, running_pnl, initial_balance, lastpnl
#     global fillcheck, totaltradecount, unfilledpnl, LastLastSide
#     # global unfilled_alerts

#     secret = "gajraj09"
#     quantity = 0.005

#     status = status_fun
#     pnl = 0.0
#     if status == "entry" and fillcheck == 1 and entryprice is not None:
#         # previous entry was not filled
#         alerts.append(
#             f"UNFILLED | Side={LastLastSide} | Price={fmt_price(entryprice)} | Time={trigger_time_iso}"
#         )

#     if status == "exit":
#         if entryprice is None:
#             print("[WEBHOOK] Exit requested but no stored entryprice; ignoring pnl update.")
#         else:
#             pnl = calculate_pnl(entryprice, entry_price_in, LastLastSide)
#             if fillcheck == 0:
#                 initial_balance += pnl
                
#             running_pnl += pnl
#             lastpnl = pnl
#         entryprice = None
#         if fillcheck == 1:
#             unfilledpnl += pnl
#         fillcheck = 0
#     else:  # status == "entry"
#         if entryprice is not None:
#             # Close any previous implicit position first for accounting
#             pnl = calculate_pnl(entryprice, entry_price_in, LastLastSide)
#             if fillcheck == 0:
#                 initial_balance += pnl
#             running_pnl += pnl
#             lastpnl = pnl
#         else:
#             print(f"[ENTRY] Opening first position at {fmt_price(entry_price_in)}")
#         entryprice = entry_price_in
#         totaltradecount += 1
#         fillcheck = 1
#     LastLastSide = LastSide


#     try:
#         payload = {
#             "symbol": SYMBOL.upper(),
#             "side": side,
#             "quantity": quantity,
#             "price": entry_price_in,
#             "status": status,
#             "secret": secret,
#         }
#         requests.post(WEBHOOK_URL, json=payload, timeout=5)
#         print("Sent payload:", payload)
#     except Exception as e:
#         print("Webhook error:", e)
#     save_state()


# def recompute_bounds_on_close():
#     global upper_bound, lower_bound, _bounds_candle_ts, _triggered_window_id
#     if len(candles) < LENGTH:
#         upper_bound = None
#         lower_bound = None
#         _bounds_candle_ts = None
#         _triggered_window_id = None
#         save_state()
#         return

#     window = candles.tail(LENGTH)
#     highs = window["High"][window["High"] > 0]
#     lows = window["Low"][window["Low"] > 0]
#     if highs.empty or lows.empty:
#         return

#     upper_bound = float(highs.max())
#     lower_bound = float(lows.min())
#     _bounds_candle_ts = window["time"].iloc[-1]  # UTC
#     _triggered_window_id = None
#     save_state()

## Upadte on 6/10/2025 -> the actual fill price====================================
def calculate_pnl(entry_price: float, closing_price: float, side: str) -> float:
    """Calculate profit/loss for a fixed quantity position."""
    quantity = 0.005  # fixed quantity
    if side == "buy":
        return (closing_price - entry_price) * quantity
    elif side == "sell":
        return (entry_price - closing_price) * quantity
    else:
        return 0.0


def send_webhook(trigger_time_iso: str, entry_price_in: float, side: str, status_fun: str):
    """Handles webhook sending and state updates for entries/exits."""
    global EntryCount, LastSide, status, entryprice, running_pnl, initial_balance, lastpnl
    global fillcheck, totaltradecount, unfilledpnl, LastLastSide

    secret = "gajraj09"
    quantity = 0.005

    status = status_fun
    pnl = 0.0

    # --- Handle previous unfilled entry ---
    if status == "entry" and fillcheck == 1 and entryprice is not None:
        alerts.append(
            f"UNFILLED | Side={LastLastSide} | Price={fmt_price(entryprice)} | Time={trigger_time_iso}"
        )

    # --- Exit logic ---
    if status == "exit":
        if entryprice is None:
            print("[WEBHOOK] Exit requested but no stored entryprice; ignoring pnl update.")
        else:
            pnl = calculate_pnl(entryprice, entry_price_in, LastLastSide)
            if fillcheck == 0:
                initial_balance += pnl
            running_pnl += pnl
            lastpnl = pnl

        entryprice = None
        if fillcheck == 1:
            unfilledpnl += pnl
        fillcheck = 0

    # --- Entry logic ---
    else:  # status == "entry"
        if entryprice is not None:
            pnl = calculate_pnl(entryprice, entry_price_in, LastLastSide)
            if fillcheck == 0:
                initial_balance += pnl
            running_pnl += pnl
            lastpnl = pnl
        else:
            print(f"[ENTRY] Opening first position at {fmt_price(entry_price_in)}")

        entryprice = entry_price_in
        totaltradecount += 1
        fillcheck = 1

    LastLastSide = LastSide

    # --- Send webhook payload ---
    try:
        payload = {
            "symbol": SYMBOL.upper(),
            "side": side,
            "quantity": quantity,
            "price": entry_price_in,
            "status": status,
            "secret": secret,
        }
        requests.post(WEBHOOK_URL, json=payload, timeout=5)
        print("Sent payload:", payload)
    except Exception as e:
        print("Webhook error:", e)

    save_state()


def recompute_bounds_on_close():
    """Recalculate upper/lower trading bounds from recent candle data."""
    global upper_bound, lower_bound, _bounds_candle_ts, _triggered_window_id
    if len(candles) < LENGTH:
        upper_bound = None
        lower_bound = None
        _bounds_candle_ts = None
        _triggered_window_id = None
        save_state()
        return

    window = candles.tail(LENGTH)
    highs = window["High"][window["High"] > 0]
    lows = window["Low"][window["Low"] > 0]
    if highs.empty or lows.empty:
        return

    upper_bound = float(highs.max())
    lower_bound = float(lows.min())
    _bounds_candle_ts = window["time"].iloc[-1]  # UTC
    _triggered_window_id = None
    save_state()
#=====================================================================================
#=====================================================================================


# ==========================
# Fillcheck update on every tick
# ==========================

#===============================================
# Code for fill check
# --- globals for drop-based fill detection ---

# DROP_TICKS = 10
# TICK_SIZE = 0.01  # adjust to your symbol tick size if different

# _peak_price = None         # highest price seen since fillcheck started
# _trough_price = None       # lowest price seen since fillcheck started
# _drop_filled = False       # whether we already treated a drop as fill for this cycle
# _last_entryprice_seen = None
# # ------------------------------------------------

# def update_fillcheck(trade_price: float):
#     global fillcheck, fillcount, entryprice, LastSide, status, totaltradecount, unfilledpnl
#     global _peak_price, _trough_price, _drop_filled, _last_entryprice_seen

#     # If no active entry/fillcheck, clear internal state and return
#     if entryprice is None or fillcheck == 0:
#         _peak_price = None
#         _trough_price = None
#         _drop_filled = False
#         _last_entryprice_seen = None
#         return

#     # If entryprice changed since last time, reset internal tracking for a fresh cycle
#     if _last_entryprice_seen is None or entryprice != _last_entryprice_seen:
#         _last_entryprice_seen = entryprice
#         _peak_price = trade_price
#         _trough_price = trade_price
#         _drop_filled = False

#     # Update running peak and trough
#     if _peak_price is None or trade_price > _peak_price:
#         _peak_price = trade_price
#     if _trough_price is None or trade_price < _trough_price:
#         _trough_price = trade_price

#     drop_threshold = DROP_TICKS * TICK_SIZE

#     # BUY side: detect drop from peak
#     if LastSide == "buy":
#         drop_amount = _peak_price - trade_price
#         if (not _drop_filled) and drop_amount >= drop_threshold:
#             # mark as filled
#             fillcheck = 0
#             fillcount += 1
#             _drop_filled = True
#             print(f"🚨 Fill: BUY first {DROP_TICKS}-tick drop -> peak={_peak_price}, price={trade_price}, drop={drop_amount:.8f}")
#             save_state()
#             return

#     # SELL side: detect rise from trough (opposite)
#     elif LastSide == "sell":
#         rise_amount = trade_price - _trough_price
#         if (not _drop_filled) and rise_amount >= drop_threshold:
#             fillcheck = 0
#             fillcount += 1
#             _drop_filled = True
#             print(f"🚨 Fill: SELL first {DROP_TICKS}-tick rise -> trough={_trough_price}, price={trade_price}, rise={rise_amount:.8f}")
#             save_state()
#             return

#     # No fill yet — continue tracking
#     return



# --- globals for drop-based fill detection ---
DROP_TICKS = 10
TICK_SIZE = 0.01  # adjust to your symbol tick size if different

_peak_price = None
_trough_price = None
_drop_filled = False
_last_entryprice_seen = None
# ------------------------------------------------


def update_fillcheck(trade_price: float):
    """
    Detects actual fills based on a 10-tick move from peak/trough.
    Updates entryprice to the real fill price, sends alert, and saves state.
    """
    global fillcheck, fillcount, entryprice, LastSide, status, totaltradecount, unfilledpnl
    global _peak_price, _trough_price, _drop_filled, _last_entryprice_seen, alerts

    # Skip if no active trade
    if entryprice is None or fillcheck == 0:
        _peak_price = None
        _trough_price = None
        _drop_filled = False
        _last_entryprice_seen = None
        return

    # Reset when a new entry starts
    if _last_entryprice_seen is None or entryprice != _last_entryprice_seen:
        _last_entryprice_seen = entryprice
        _peak_price = trade_price
        _trough_price = trade_price
        _drop_filled = False

    # Update rolling peak/trough
    if trade_price > _peak_price:
        _peak_price = trade_price
    if trade_price < _trough_price:
        _trough_price = trade_price

    drop_threshold = DROP_TICKS * TICK_SIZE

    # --- BUY side fill detection ---
    if LastSide == "buy":
        drop_amount = _peak_price - trade_price
        if not _drop_filled and drop_amount >= drop_threshold:
            fill_price = trade_price
            entryprice = fill_price  # update to actual fill price

            msg = (
                f"✅ FILLED | BUY | Trigger={fmt_price(_last_entryprice_seen)} | "
                f"Fill={fmt_price(fill_price)} | Drop={DROP_TICKS} ticks | "
                f"Peak={fmt_price(_peak_price)}"
            )
            alerts.append(msg)
            alerts[:] = alerts[-200:]
            print(msg)

            fillcheck = 0
            fillcount += 1
            _drop_filled = True
            save_state()
            return

    # --- SELL side fill detection ---
    elif LastSide == "sell":
        rise_amount = trade_price - _trough_price
        if not _drop_filled and rise_amount >= drop_threshold:
            fill_price = trade_price
            entryprice = fill_price  # update to actual fill price

            msg = (
                f"✅ FILLED | SELL | Trigger={fmt_price(_last_entryprice_seen)} | "
                f"Fill={fmt_price(fill_price)} | Rise={DROP_TICKS} ticks | "
                f"Trough={fmt_price(_trough_price)}"
            )
            alerts.append(msg)
            alerts[:] = alerts[-200:]
            print(msg)

            fillcheck = 0
            fillcount += 1
            _drop_filled = True
            save_state()
            return

    # Continue tracking otherwise
    return




#===============================================
#===============================================




# def update_fillcheck(trade_price: float):
#     global fillcheck, fillcount, entryprice, LastSide, status, totaltradecount, unfilledpnl

#     if entryprice is None or fillcheck == 0:
#         return
#     was_fill = False
#     if status == "entry":
#         if LastSide == "buy" and trade_price <= entryprice:  # <= to allow exact touch
#             fillcheck = 0
#             fillcount += 1
#             was_fill = True
#         elif LastSide == "sell" and trade_price >= entryprice:
#             fillcheck = 0
#             fillcount += 1
#             was_fill = True
#     elif status == "exit":
#         # Exit fill handling can be more sophisticated if you post exit orders
#         fillcheck = 0
#         was_fill = True
#     if was_fill:
#         save_state()



# def try_trigger_on_trade(trade_price: float, trade_ts_ms: int):
#     global alerts, _triggered_window_id, _triggered_window_side
#     global status, EntryCount, LastSide, _last_exit_lock

#     # Preconditions: skip if required values not set
#     if not (
#         is_valid_price(trade_price)
#         and upper_bound is not None
#         and lower_bound is not None
#         and _bounds_candle_ts is not None
#     ):
#         return

#     # Convert timestamps
#     trigger_time = datetime.fromtimestamp(trade_ts_ms / 1000, tz=KOLKATA_TZ)
#     trigger_time_iso = trigger_time.isoformat()
#     ts_dt = datetime.fromtimestamp(trade_ts_ms / 1000, tz=STORE_TZ)

#     # ---- tick size & buffer ----
#     tick_size = 0.01   # for ETHUSDC, adjust if needed
#     buffer_ticks = 100
#     entry_threshold = upper_bound - buffer_ticks * tick_size

#     def process_trigger(side: str, entry_val: float, friendly: str):
#         """Handles sending webhook + logging + updating state"""
#         nonlocal trigger_time_iso, ts_dt
#         global alerts, _triggered_window_id, _triggered_window_side, LastSide

#         LastSide = side
#         _triggered_window_id = _bounds_candle_ts
#         _triggered_window_side = side

#         # Send webhook
#         send_webhook(trigger_time_iso, entry_val, side, "entry")

#         # Prepare alert message
#         msg = (
#             f"{friendly} | {status}: {fmt_price(entry_val)} "
#             f"| Live {fmt_price(trade_price)} "
#             f"| Trigger {ts_dt.astimezone(KOLKATA_TZ).strftime('%H:%M:%S')}"
#         )
#         alerts.append(msg)
#         alerts[:] = alerts[-200:]  # keep only last 50

#         save_state()

#     # === Modified Trigger logic with buffer ===
#     if trade_price >= entry_threshold:  
#         if _triggered_window_id != _bounds_candle_ts:
#             if _last_exit_lock == "unlock":
#                 process_trigger("buy", entry_threshold, "LONG (buffered)")

##======== 6/10/2025 New Actual Entryy ===================
def try_trigger_on_trade(trade_price: float, trade_ts_ms: int):
    """Evaluates incoming trade ticks and triggers entries when thresholds are hit."""
    global alerts, _triggered_window_id, _triggered_window_side
    global status, EntryCount, LastSide, _last_exit_lock

    # --- Preconditions ---
    if not (
        is_valid_price(trade_price)
        and upper_bound is not None
        and lower_bound is not None
        and _bounds_candle_ts is not None
    ):
        return

    # --- Convert timestamp ---
    trigger_time = datetime.fromtimestamp(trade_ts_ms / 1000, tz=KOLKATA_TZ)
    trigger_time_iso = trigger_time.isoformat()
    ts_dt = datetime.fromtimestamp(trade_ts_ms / 1000, tz=STORE_TZ)

    # --- Tick size & buffer logic ---
    tick_size = 0.01   # for ETHUSDT; adjust if needed
    buffer_ticks = 100
    entry_threshold = upper_bound - buffer_ticks * tick_size

    # --- Trigger handling ---
    def process_trigger(side: str, entry_val: float, friendly: str):
        """Handles webhook + logging + state updates for entry trigger."""
        nonlocal trigger_time_iso, ts_dt
        global alerts, _triggered_window_id, _triggered_window_side, LastSide

        LastSide = side
        _triggered_window_id = _bounds_candle_ts
        _triggered_window_side = side

        # Send entry webhook (records trigger, not fill)
        send_webhook(trigger_time_iso, entry_val, side, "entry")

        # Create alert message — mark this clearly as a trigger
        msg = (
            f"📈 TRIGGERED | {friendly} | {status}: {fmt_price(entry_val)} "
            f"| Live {fmt_price(trade_price)} "
            f"| Time {ts_dt.astimezone(KOLKATA_TZ).strftime('%H:%M:%S')}"
        )
        alerts.append(msg)
        alerts[:] = alerts[-200:]  # keep last N alerts
        print(msg)

        save_state()

    # --- Example condition for BUY entry (buffered) ---
    if trade_price >= entry_threshold:
        if _triggered_window_id != _bounds_candle_ts:
            if _last_exit_lock == "unlock":
                process_trigger("buy", entry_threshold, "LONG (buffered)")

    # NOTE:
    # You can add SELL trigger logic similarly below if your strategy supports shorts.



# ==========================
# WebSocket handlers
# ==========================

def on_message(ws, message):
    global candles, live_price, last_valid_price,status,lastpnl,_triggered_window_id,_last_exit_lock
    try:
        data = json.loads(message)
        stream = data.get("stream")
        payload = data.get("data")

        if stream and "kline" in stream:
            kline = payload["k"]
            ts_dt = datetime.fromtimestamp(kline["t"] / 1000, tz=STORE_TZ)  # UTC

            o, h, l, c = (
                float(kline["o"]),
                float(kline["h"]),
                float(kline["l"]),
                float(kline["c"]),
            )

            if is_valid_price(c):
                last_valid_price = c
                live_price = c

            new_candle_detected = candles.empty or candles.iloc[-1]["time"] != ts_dt

            if new_candle_detected:
                open_val = o if is_valid_price(o) else (last_valid_price if last_valid_price is not None else None)
                if open_val is None:
                    return

                high_val = h if is_valid_price(h) else open_val
                low_val = l if is_valid_price(l) else open_val
                close_val = c if is_valid_price(c) else open_val

                new_row = {
                    "time": ts_dt,  # UTC
                    "Open": open_val,
                    "High": max(high_val, open_val),
                    "Low": min(low_val, open_val),
                    "Close": close_val,
                }
                candles = pd.concat([candles, pd.DataFrame([new_row])], ignore_index=True)

                # ---------- OPTIONAL: Send webhook on new candle open (EXIT) ----------
                if status == "exit":
                    _last_exit_lock = "unlock"
                if status != "exit":
                    _last_exit_lock = "lock"
                    try:
                        send_webhook(ts_dt.astimezone(KOLKATA_TZ).strftime("%H:%M:%S"), open_val, "buy", "exit")
                        msg = f"EXIT | Price: {fmt_price(open_val)} | Time: {ts_dt.astimezone(KOLKATA_TZ).strftime('%H:%M:%S')}| PnL: {lastpnl}"
                        alerts.append(msg)
                        alerts[:] = alerts[-50:]
                    except Exception as e:
                        print("New candle webhook error:", e)
                
                save_state()

            else:
                idx = candles.index[-1]
                if is_valid_price(h):
                    candles.at[idx, "High"] = max(candles.at[idx, "High"], h)
                if is_valid_price(l):
                    candles.at[idx, "Low"] = min(candles.at[idx, "Low"], l)
                if is_valid_price(c):
                    candles.at[idx, "Close"] = c
                live_price = c
                last_valid_price = c

            if len(candles) > CANDLE_LIMIT:
                candles = candles.tail(CANDLE_LIMIT).reset_index(drop=True)

            if kline.get("x"):
                # Candle closed -> recompute bounds
                recompute_bounds_on_close()

        elif stream and "trade" in stream:
            trade_price_raw = payload.get("p")
            if not is_valid_price(trade_price_raw):
                return

            trade_price = float(trade_price_raw)
            live_price = trade_price
            last_valid_price = trade_price

            if not candles.empty:
                idx = candles.index[-1]
                # Update last candle tick by tick
                candles.at[idx, "Close"] = trade_price
                candles.at[idx, "High"] = max(candles.at[idx, "High"], trade_price)
                candles.at[idx, "Low"] = min(candles.at[idx, "Low"], trade_price)

            # Update fill status and try triggers
            update_fillcheck(trade_price)
            trade_ts_ms = int(payload.get("T") or payload.get("E") or time.time() * 1000)
            try_trigger_on_trade(trade_price, trade_ts_ms)

    except Exception as e:
        print("on_message error:", e)


def on_error(ws, error):
    print("WebSocket error:", error)


def on_close(ws, code, msg):
    print("WebSocket closed", code, msg)
    # Single delayed restart to avoid tight loops
    def _restart():
        time.sleep(2)
        run_ws()
    threading.Thread(target=_restart, daemon=True).start()


def on_open(ws):
    print("WebSocket connected")
    params = [f"{SYMBOL}@kline_{INTERVAL}", f"{SYMBOL}@trade"]
    msg = {"method": "SUBSCRIBE", "params": params, "id": 1}
    ws.send(json.dumps(msg))


def run_ws():
    url = "wss://fstream.binance.com/stream"
    ws = websocket.WebSocketApp(
        url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )
    # Ping to keep alive
    ws.run_forever(ping_interval=20, ping_timeout=10)


# ==========================
# Dash App
# ==========================
app = dash.Dash(__name__)
server = app.server  # expose Flask server for Render/Heroku
app.layout = html.Div(
    [
        html.H1(f"{SYMBOL.upper()} Live Prices & Last {CANDLE_LIMIT} Candles"),
        html.Div(
            [
                html.H2(id="live-price", style={"color": "black", "marginRight": "20px"}),
                html.H2(id="bal", style={"color": "black", "marginRight": "20px"}),
                html.Div(id="trade-stats", style={"display": "flex", "gap": "20px", "alignItems": "center"}),
            ],
            style={"display": "flex", "flexDirection": "row", "alignItems": "center"},
        ),
        html.Div(id="ohlc-values"),
        html.Div(id="bounds", style={"marginTop": "8px", "color": "#9ad"}),
        html.H3("Strategy Alerts"),
        html.Ul(id="strategy-alerts"),
        dcc.Interval(id="interval", interval=500, n_intervals=0),  # update every 0.5s
    ]
)


@app.callback(
    [
        Output("live-price", "children"),
        Output("bal", "children"),
        Output("trade-stats", "children"),
        Output("ohlc-values", "children"),
        Output("strategy-alerts", "children"),
        Output("bounds", "children"),
    ],
    [Input("interval", "n_intervals")],
)
def update_display(_):
    if len(candles) == 0:
        return (
            "Live Price: --",
            f"Balance: {fmt_price(initial_balance)}",
            [],
            [],
            [],
            "Bounds: waiting for enough closed candles...",
        )

    lp = f"Live Price: {fmt_price(live_price)}"
    bal = f"Balance: {fmt_price(initial_balance)}"

    stats_html = [
        html.Div(f"FillCheck: {fillcheck}"),
        html.Div(f"FillCount: {fillcount}"),
        html.Div(f"TotalTrades: {totaltradecount}"),
        html.Div(f"UnfilledPnL: {fmt_price(unfilledpnl)}"),
    ]

    ohlc_html = []
    for idx, row in candles.iterrows():
        ts_str = row["time"].astimezone(KOLKATA_TZ).strftime("%H:%M:%S")
        text = (
            f"{ts_str} → O:{fmt_price(row['Open'])}, H:{fmt_price(row['High'])}, "
            f"L:{fmt_price(row['Low'])}, C:{fmt_price(row['Close'])}"
        )
        ohlc_html.append(html.Div(text))

    alerts_html = [html.Li(a) for a in alerts[-10:]]

    if upper_bound is not None and lower_bound is not None and _bounds_candle_ts is not None:
        btxt = (
            f"Bounds[{LENGTH}] → Upper {fmt_price(upper_bound)}, Lower {fmt_price(lower_bound)} "
            f"(from candle {_bounds_candle_ts.astimezone(KOLKATA_TZ).strftime('%H:%M:%S')})"
        )
    else:
        btxt = "Bounds: waiting for enough closed candles..."

    return lp, bal, stats_html, ohlc_html, alerts_html, btxt


# Keep-alive ping thread
def keep_alive():
    """Send a ping to the server itself."""
    try:
        print(f"🔄 Pinging {PING_URL}")
        r = requests.get(PING_URL, timeout=10)
        print("✅ Ping response:", r.status_code)
    except Exception as e:
        print("⚠️ Keep-alive ping failed:", str(e))

@server.route('/ping', methods=['GET'])
def ping():
    keep_alive()
    return jsonify({"status": "alive"}), 200

# ==========================
# Main (Render compatible)
# ==========================
if __name__ == "__main__":
        # 1) Attempt to load saved state
    try:
        load_state()
    except Exception as e:
        print("Warning: load_state() failed:", e)
    if candles.empty:
        try:
            fetch_initial_candles()
        except Exception as e:
            print("Initial fetch failed:", e)
    if len(candles) >= LENGTH and (upper_bound is None or lower_bound is None):
        recompute_bounds_on_close()
    t = threading.Thread(target=run_ws, daemon=True)
    t.start()
    def periodic_save_loop(interval_s=30):
        while True:
            try:
                save_state()
            except Exception as e:
                print("Periodic save error:", e)
            time.sleep(interval_s)

    saver_thread = threading.Thread(target=periodic_save_loop, args=(30,), daemon=True)
    saver_thread.start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)




























#===============================================================================================================================
#===============================================================================================================================
#===============================================================================================================================
# import json
# import os
# import time
# import threading
# from datetime import datetime, timezone, timedelta

# import pandas as pd
# import requests
# import websocket
# import dash
# from dash import dcc, html
# from dash.dependencies import Input, Output
# from flask import Flask, request, jsonify
# from pymongo import MongoClient

# # ==========================
# # Config (tweak here)
# # ==========================
# SYMBOL = "ethusdc"       # keep lowercase for websocket streams
# INTERVAL = "5m"
# CANDLE_LIMIT = 10         # needs to be large enough to compute ATR reliably
# PING_URL = os.environ.get("PING_URL", "https://bot-reviver.onrender.com/ping")
# WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://binance-65gz.onrender.com/web")

# # === Strategy params (change to your preference) ===
# ATR_LENGTH = 1           # lookback for ATR (use >=1). 5 is reasonable.
# ATR_MULT = 0.1          # ATR multiplier
# SLIPPAGE_TICKS = 1       # exit slippage in ticks
# TICK_SIZE = 0.01         # ETHUSDC tick size (adjust if needed)

# # ====For channel bounds (keeps earlier behaviour)
# LENGTH = 3  # number of LAST CLOSED candles to compute channel bounds

# MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
# DB_NAME = "trading_bot_eth_atr"
# COLLECTION_STATE = "bot_state"

# # ==========================
# # Timezones
# # ==========================
# KOLKATA_TZ = timezone(timedelta(hours=5, minutes=30))
# STORE_TZ = timezone.utc

# # ==========================
# # Globals
# # ==========================
# candles = pd.DataFrame(columns=["time", "Open", "High", "Low", "Close"])  # 'time' is UTC-aware
# live_price = None
# last_valid_price = None
# alerts = []

# initial_balance = 10.0
# entryprice = None
# running_pnl = 0.0

# upper_bound = None
# lower_bound = None
# _bounds_candle_ts = None  # UTC datetime of the candle used to compute bounds
# _triggered_window_id = None
# _triggered_window_side = None
# _last_exit_lock = None

# # NEW: flags to ensure only one entry and one exit per bounds window
# _triggered_entry_for_window = False
# _triggered_exit_for_window = False

# EntryCount = 0
# LastSide = None
# LastLastSide = "buy"
# status = None

# # Order filling data
# fillcheck = 0
# fillcount = 0
# totaltradecount = 0
# unfilledpnl = 0.0
# lastpnl = 0

# # Strategy levels (computed)
# long_entry_price = None
# long_exit_price = None
# last_atr = None

# # ==========================
# # MongoDB Setup
# # ==========================
# mongo_client = MongoClient(MONGO_URI)
# db = mongo_client[DB_NAME]
# state_col = db[COLLECTION_STATE]


# def _serialize_candles(df: pd.DataFrame):
#     """Return list[dict] with 'time' as ISO strings for storage."""
#     if df is None or df.empty:
#         return []
#     tmp = df.copy()

#     def _iso_safe(x):
#         if pd.isna(x):
#             return None
#         if isinstance(x, str):
#             return x
#         if getattr(x, "tzinfo", None) is None:
#             x = x.replace(tzinfo=STORE_TZ)
#         return x.isoformat()

#     tmp["time"] = tmp["time"].apply(_iso_safe)
#     return tmp.to_dict(orient="records")


# def _deserialize_candles(records):
#     """Turn stored records (with time iso) into DataFrame with timezone-aware datetimes (UTC)."""
#     if not records:
#         return pd.DataFrame(columns=["time", "Open", "High", "Low", "Close"])
#     df = pd.DataFrame(records)
#     if "time" in df.columns:
#         df["time"] = pd.to_datetime(df["time"], utc=True)
#         df["time"] = df["time"].dt.tz_convert(STORE_TZ)
#     return df


# def save_state():
#     """Save global state + candles + alerts to MongoDB."""
#     global candles, live_price, last_valid_price, alerts
#     global initial_balance, entryprice, running_pnl
#     global upper_bound, lower_bound, _bounds_candle_ts, _triggered_window_id
#     global EntryCount, LastSide, LastLastSide, status
#     global fillcheck, fillcount, totaltradecount, unfilledpnl
#     global long_entry_price, long_exit_price, last_atr
#     global _triggered_entry_for_window, _triggered_exit_for_window

#     try:
#         state = {
#             "_id": "bot_state",
#             "candles": _serialize_candles(candles),
#             "live_price": float(live_price) if is_valid_price(live_price) else None,
#             "last_valid_price": float(last_valid_price) if is_valid_price(last_valid_price) else None,
#             "alerts": alerts[-100:],
#             "initial_balance": float(initial_balance),
#             "entryprice": float(entryprice) if is_valid_price(entryprice) else None,
#             "running_pnl": float(running_pnl),
#             "upper_bound": float(upper_bound) if is_valid_price(upper_bound) else None,
#             "lower_bound": float(lower_bound) if is_valid_price(lower_bound) else None,
#             "_bounds_candle_ts": _bounds_candle_ts.isoformat() if _bounds_candle_ts else None,
#             "_triggered_window_id": _triggered_window_id.isoformat() if _triggered_window_id else None,
#             "_triggered_window_side": _triggered_window_side,
#             "_last_exit_lock": _last_exit_lock,
#             "EntryCount": int(EntryCount),
#             "LastSide": LastSide,
#             "LastLastSide": LastLastSide,
#             "status": status,
#             "fillcheck": int(fillcheck),
#             "fillcount": int(fillcount),
#             "totaltradecount": int(totaltradecount),
#             "unfilledpnl": float(unfilledpnl),
#             "long_entry_price": float(long_entry_price) if is_valid_price(long_entry_price) else None,
#             "long_exit_price": float(long_exit_price) if is_valid_price(long_exit_price) else None,
#             "last_atr": float(last_atr) if is_valid_price(last_atr) else None,
#             "_triggered_entry_for_window": bool(_triggered_entry_for_window),
#             "_triggered_exit_for_window": bool(_triggered_exit_for_window),
#         }
#         state_col.replace_one({"_id": "bot_state"}, state, upsert=True)
#     except Exception as e:
#         print("⚠️ Error saving state to MongoDB:", e)


# def load_state():
#     """Restore global state from MongoDB."""
#     global candles, live_price, last_valid_price, alerts
#     global initial_balance, entryprice, running_pnl
#     global upper_bound, lower_bound, _bounds_candle_ts, _triggered_window_id
#     global EntryCount, LastSide, LastLastSide, status
#     global fillcheck, fillcount, totaltradecount, unfilledpnl
#     global long_entry_price, long_exit_price, last_atr, _last_exit_lock
#     global _triggered_entry_for_window, _triggered_exit_for_window

#     try:
#         doc = state_col.find_one({"_id": "bot_state"})
#         if not doc:
#             print("ℹ️ No saved state found in DB, fresh start.")
#             return

#         candles = _deserialize_candles(doc.get("candles", []))
#         live_price = doc.get("live_price")
#         last_valid_price = doc.get("last_valid_price")
#         alerts = doc.get("alerts", []) or []

#         initial_balance = doc.get("initial_balance", 10.0)
#         entryprice = doc.get("entryprice")
#         running_pnl = doc.get("running_pnl", 0.0)

#         upper_bound = doc.get("upper_bound")
#         lower_bound = doc.get("lower_bound")

#         _bounds_candle_ts = (
#             pd.to_datetime(doc.get("_bounds_candle_ts")).tz_convert(STORE_TZ)
#             if doc.get("_bounds_candle_ts") else None
#         )
#         _triggered_window_id = (
#             pd.to_datetime(doc.get("_triggered_window_id")).tz_convert(STORE_TZ)
#             if doc.get("_triggered_window_id") else None
#         )
#         _triggered_window_side = doc.get("_triggered_window_side","buy")
#         _last_exit_lock = doc.get("_last_exit_lock","unlock")

#         EntryCount = doc.get("EntryCount", 0)
#         LastSide = doc.get("LastSide")
#         LastLastSide = doc.get("LastLastSide", "buy")
#         status = doc.get("status")

#         fillcheck = doc.get("fillcheck", 0)
#         fillcount = doc.get("fillcount", 0)
#         totaltradecount = doc.get("totaltradecount", 0)
#         unfilledpnl = doc.get("unfilledpnl", 0.0)

#         long_entry_price = doc.get("long_entry_price")
#         long_exit_price = doc.get("long_exit_price")
#         last_atr = doc.get("last_atr")

#         # restore new flags if present
#         _triggered_entry_for_window = bool(doc.get("_triggered_entry_for_window", False))
#         _triggered_exit_for_window = bool(doc.get("_triggered_exit_for_window", False))

#         print("✅ State restored from MongoDB")
#     except Exception as e:
#         print("⚠️ Error loading state from MongoDB:", e)


# # ==========================
# # Utilities
# # ==========================
# def is_valid_price(x):
#     try:
#         return float(x) > 0
#     except Exception:
#         return False


# def fmt_price(p):
#     if p is None:
#         return "--"
#     ap = abs(p)
#     if ap >= 100:
#         return f"{p:.2f}"
#     elif ap >= 1:
#         return f"{p:.4f}"
#     else:
#         return f"{p:.8f}"


# # ==========================
# # Strategy helpers (fixed)
# # ==========================
# def compute_true_range_series(df: pd.DataFrame) -> pd.Series:
#     """Compute True Range (TR) for a DataFrame of closed candles.
#     Expects df has columns Open/High/Low/Close and index is chronological.
#     """
#     if df is None or df.empty or len(df) < 2:
#         return pd.Series(dtype=float)
#     prev_close = df["Close"].shift(1)
#     tr1 = df["High"] - df["Low"]
#     tr2 = (df["High"] - prev_close).abs()
#     tr3 = (df["Low"] - prev_close).abs()
#     tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
#     return tr


# def compute_atr_from_closed_candles(atr_len=ATR_LENGTH):
#     """Compute ATR using last `atr_len` closed candles.
#     Algorithm:
#       - closed = all candles except the current open (candles.iloc[:-1])
#       - compute TR series on closed (needs at least 2 closed candles)
#       - take the last `atr_len` TR values (if available) and average them
#     Returns average TR (ATR) or None.
#     """
#     global candles
#     if candles is None or candles.empty:
#         return None
#     closed = candles.iloc[:-1].copy() if len(candles) >= 2 else pd.DataFrame()
#     if closed is None or len(closed) < 2:
#         # not enough closed data to compute TR
#         return None

#     tr = compute_true_range_series(closed)
#     if tr.empty:
#         return None

#     # pick the last `atr_len` TR values (if there are fewer, use what's available)
#     if atr_len <= 0:
#         return None
#     take = min(len(tr), atr_len)
#     last_tr = tr.tail(take)
#     if last_tr.empty:
#         return None
#     atr = float(last_tr.mean())
#     return atr


# # ==========================
# # Helpers (unchanged)
# # ==========================
# def fetch_initial_candles():
#     """Seed candles from Binance REST klines endpoint."""
#     global candles, live_price, last_valid_price
#     url = (
#         f"https://fapi.binance.com/fapi/v1/klines?symbol={SYMBOL.upper()}&interval={INTERVAL}&limit={CANDLE_LIMIT}"
#     )
#     r = requests.get(url, timeout=10)
#     r.raise_for_status()
#     data = r.json()

#     rows = []
#     for k in data:
#         rows.append(
#             {
#                 "time": datetime.fromtimestamp(k[0] / 1000, tz=STORE_TZ),  # UTC
#                 "Open": float(k[1]),
#                 "High": float(k[2]),
#                 "Low": float(k[3]),
#                 "Close": float(k[4]),
#             }
#         )
#     candles = pd.DataFrame(rows)
#     if not candles.empty:
#         live_price = float(candles["Close"].iloc[-1])
#         last_valid_price = live_price if is_valid_price(live_price) else None
#     save_state()


# def calculate_pnl(entry_price: float, closing_price: float, side: str) -> float:
#     quantity = 0.005  # fixed quantity
#     if side == "buy":
#         return (closing_price - entry_price) * quantity
#     elif side == "sell":
#         return (entry_price - closing_price) * quantity
#     else:
#         return 0.0


# def send_webhook(trigger_time_iso: str, entry_price_in: float, side: str, status_fun: str):
#     global EntryCount, LastSide, status, entryprice, running_pnl, initial_balance, lastpnl
#     global fillcheck, totaltradecount, unfilledpnl, LastLastSide

#     secret = "gajraj09"
#     quantity = 0.005

#     status = status_fun
#     pnl = 0.0

#     if status == "exit":
#         if entryprice is None:
#             print("[WEBHOOK] Exit requested but no stored entryprice; ignoring pnl update.")
#         else:
#             pnl = calculate_pnl(entryprice, entry_price_in, LastLastSide)
#             if fillcheck == 0:
#                 initial_balance += pnl

#             running_pnl += pnl
#             lastpnl = pnl
#         entryprice = None
#         if fillcheck == 1:
#             unfilledpnl += pnl
#         fillcheck = 0
#     else:  # status == "entry"
#         if entryprice is not None:
#             # Close any previous implicit position first for accounting
#             pnl = calculate_pnl(entryprice, entry_price_in, LastLastSide)
#             if fillcheck == 0:
#                 initial_balance += pnl
#             running_pnl += pnl
#             lastpnl = pnl
#         else:
#             print(f"[ENTRY] Opening first position at {fmt_price(entry_price_in)}")
#         entryprice = entry_price_in
#         totaltradecount += 1
#         fillcheck = 1
#     LastLastSide = LastSide

#     try:
#         payload = {
#             "symbol": SYMBOL.upper(),
#             "side": side,
#             "quantity": quantity,
#             "price": entry_price_in,
#             "status": status,
#             "secret": secret,
#         }
#         requests.post(WEBHOOK_URL, json=payload, timeout=5)
#         print("Sent payload:", payload)
#     except Exception as e:
#         print("Webhook error:", e)
#     save_state()


# def recompute_bounds_on_close():
#     """Compute channel bounds and ATR-based entry/exit levels when a candle closes."""
#     global upper_bound, lower_bound, _bounds_candle_ts, _triggered_window_id
#     global long_entry_price, long_exit_price, last_atr
#     global _triggered_entry_for_window, _triggered_exit_for_window

#     # Need at least LENGTH closed candles + current open => len(candles) >= LENGTH + 1
#     if len(candles) < LENGTH + 1:
#         upper_bound = None
#         lower_bound = None
#         _bounds_candle_ts = None
#         _triggered_window_id = None
#         long_entry_price = None
#         long_exit_price = None
#         last_atr = None
#         # reset per-window triggers
#         _triggered_entry_for_window = False
#         _triggered_exit_for_window = False
#         save_state()
#         return

#     closed = candles.iloc[:-1]  # exclude current open
#     window = closed.tail(LENGTH)
#     highs = window["High"][window["High"] > 0]
#     lows = window["Low"][window["Low"] > 0]
#     if highs.empty or lows.empty:
#         return

#     upper_bound = float(highs.max())
#     lower_bound = float(lows.min())
#     _bounds_candle_ts = window["time"].iloc[-1]  # UTC
#     _triggered_window_id = None

#     # reset per-window triggers whenever bounds are recomputed
#     _triggered_entry_for_window = False
#     _triggered_exit_for_window = False

#     # Compute ATR using closed candles
#     atr = compute_atr_from_closed_candles(ATR_LENGTH)
#     if atr is None:
#         last_atr = None
#         long_entry_price = None
#         long_exit_price = None
#         save_state()
#         return

#     last_atr = atr * ATR_MULT

#     # reference close = last closed candle's close
#     last_closed = closed.iloc[-1]
#     ref_close = float(last_closed["Close"])

#     long_entry_price = float(ref_close + last_atr)
#     long_exit_price = float(ref_close - last_atr)

#     print(f"[BOUNDS] upper={fmt_price(upper_bound)} lower={fmt_price(lower_bound)} | ATR={last_atr:.6f}")
#     print(f"[LEVELS] ENTRY={fmt_price(long_entry_price)} EXIT={fmt_price(long_exit_price)}")

#     save_state()


# # ==========================
# # Fillcheck update on every tick
# # ==========================
# def update_fillcheck(trade_price: float):
#     global fillcheck, fillcount, entryprice, LastSide, status, totaltradecount, unfilledpnl

#     if entryprice is None or fillcheck == 0:
#         return
#     was_fill = False
#     if status == "entry":
#         if LastSide == "buy" and trade_price <= entryprice:  # <= to allow exact touch
#             fillcheck = 0
#             fillcount += 1
#             was_fill = True
#         elif LastSide == "sell" and trade_price >= entryprice:
#             fillcheck = 0
#             fillcount += 1
#             was_fill = True
#     elif status == "exit":
#         fillcheck = 0
#         was_fill = True
#     if was_fill:
#         save_state()


# # ==========================
# # Trigger logic
# # ==========================
# def try_trigger_on_trade(trade_price: float, trade_ts_ms: int):
#     """
#     ENTRY (long-only): if no open position and trade_price >= long_entry_price -> send entry webhook
#     EXIT (for longs): if we have an open position and trade_price <= long_exit_price -> send exit webhook (apply slippage)
#     Ensures only one entry and one exit per computed bounds window by using per-window flags.
#     """
#     global alerts, _triggered_window_id, _triggered_window_side
#     global status, EntryCount, LastSide, _last_exit_lock
#     global long_entry_price, long_exit_price, last_atr, entryprice, fillcheck
#     global _triggered_entry_for_window, _triggered_exit_for_window

#     # Preconditions
#     if not is_valid_price(trade_price) or long_entry_price is None or long_exit_price is None:
#         return

#     # timestamps
#     trigger_time = datetime.fromtimestamp(trade_ts_ms / 1000, tz=KOLKATA_TZ)
#     trigger_time_iso = trigger_time.isoformat()
#     ts_dt = datetime.fromtimestamp(trade_ts_ms / 1000, tz=STORE_TZ)

#     def log_and_send(side: str, price_val: float, friendly: str, st: str):
#         """Helper to send webhook + alert + save state"""
#         nonlocal trigger_time_iso, ts_dt
#         global alerts, _triggered_window_id, _triggered_window_side, LastSide
#         global _triggered_entry_for_window, _triggered_exit_for_window

#         # ensure we only trigger once per window per action
#         if st == "entry" and _triggered_entry_for_window:
#             print("Skipping duplicate ENTRY for same window")
#             return False
#         if st == "exit" and _triggered_exit_for_window:
#             print("Skipping duplicate EXIT for same window")
#             return False

#         LastSide = side
#         _triggered_window_id = _bounds_candle_ts
#         _triggered_window_side = side

#         # mark flags so we don't re-trigger this action again for same window
#         if st == "entry":
#             _triggered_entry_for_window = True
#         elif st == "exit":
#             _triggered_exit_for_window = True

#         send_webhook(trigger_time_iso, price_val, side, st)

#         msg = (
#             f"{friendly} | {st}: {fmt_price(price_val)} | Live {fmt_price(trade_price)} "
#             f"| Trigger {ts_dt.astimezone(KOLKATA_TZ).strftime('%H:%M:%S')}"
#         )
#         alerts.append(msg)
#         alerts[:] = alerts[-50:]
#         save_state()
#         return True

#     # -------- EXIT logic (priority) ----------
#     if entryprice is not None and is_valid_price(entryprice):
#         if trade_price <= long_exit_price:
#             # if exit already triggered for this window, skip
#             if _triggered_exit_for_window:
#                 print("Exit already triggered for this window; skipping")
#                 return
#             slippage_amount = SLIPPAGE_TICKS * TICK_SIZE
#             exit_price_with_slippage = max(0.0, long_exit_price - slippage_amount)
#             print(f"[TRIGGER] EXIT hit at {fmt_price(trade_price)} <= {fmt_price(long_exit_price)}; sending exit @ {fmt_price(exit_price_with_slippage)}")
#             log_and_send("buy", exit_price_with_slippage, "EXIT (ATR-based)", "exit")
#             return

#     # -------- ENTRY logic ----------
#     has_open_position = entryprice is not None and fillcheck == 1
#     if not has_open_position:
#         if trade_price >= long_entry_price:
#             # if entry already triggered for this window, skip
#             if _triggered_entry_for_window:
#                 print("Entry already triggered for this window; skipping")
#                 return
#             print(f"[TRIGGER] ENTRY conditions met: live {fmt_price(trade_price)} >= entry {fmt_price(long_entry_price)}")
#             log_and_send("buy", long_entry_price, "LONG (ATR-based)", "entry")
#             return


# # ==========================
# # WebSocket handlers
# # ==========================
# def on_message(ws, message):
#     global candles, live_price, last_valid_price, status, lastpnl, _triggered_window_id, _last_exit_lock
#     try:
#         data = json.loads(message)
#         stream = data.get("stream")
#         payload = data.get("data")

#         if stream and "kline" in stream:
#             kline = payload["k"]
#             ts_dt = datetime.fromtimestamp(kline["t"] / 1000, tz=STORE_TZ)  # UTC

#             o, h, l, c = (
#                 float(kline["o"]),
#                 float(kline["h"]),
#                 float(kline["l"]),
#                 float(kline["c"]),
#             )

#             if is_valid_price(c):
#                 last_valid_price = c
#                 live_price = c

#             new_candle_detected = candles.empty or candles.iloc[-1]["time"] != ts_dt

#             if new_candle_detected:
#                 open_val = o if is_valid_price(o) else (last_valid_price if last_valid_price is not None else None)
#                 if open_val is None:
#                     return

#                 high_val = h if is_valid_price(h) else open_val
#                 low_val = l if is_valid_price(l) else open_val
#                 close_val = c if is_valid_price(c) else open_val

#                 new_row = {
#                     "time": ts_dt,  # UTC
#                     "Open": open_val,
#                     "High": max(high_val, open_val),
#                     "Low": min(low_val, open_val),
#                     "Close": close_val,
#                 }
#                 candles = pd.concat([candles, pd.DataFrame([new_row])], ignore_index=True)

#                 # just mark lock state; do not auto-exit here
#                 _last_exit_lock = "unlock"

#                 save_state()

#             else:
#                 idx = candles.index[-1]
#                 if is_valid_price(h):
#                     candles.at[idx, "High"] = max(candles.at[idx, "High"], h)
#                 if is_valid_price(l):
#                     candles.at[idx, "Low"] = min(candles.at[idx, "Low"], l)
#                 if is_valid_price(c):
#                     candles.at[idx, "Close"] = c
#                 live_price = c
#                 last_valid_price = c

#             # trim
#             if len(candles) > CANDLE_LIMIT:
#                 candles = candles.tail(CANDLE_LIMIT).reset_index(drop=True)

#             if kline.get("x"):
#                 # Candle closed -> recompute bounds (ATR & entry/exit levels)
#                 recompute_bounds_on_close()

#         elif stream and "trade" in stream:
#             trade_price_raw = payload.get("p")
#             if not is_valid_price(trade_price_raw):
#                 return

#             trade_price = float(trade_price_raw)
#             live_price = trade_price
#             last_valid_price = trade_price

#             if not candles.empty:
#                 idx = candles.index[-1]
#                 # Update last candle tick by tick
#                 candles.at[idx, "Close"] = trade_price
#                 candles.at[idx, "High"] = max(candles.at[idx, "High"], trade_price)
#                 candles.at[idx, "Low"] = min(candles.at[idx, "Low"], trade_price)

#             # Update fill status and try triggers
#             update_fillcheck(trade_price)
#             trade_ts_ms = int(payload.get("T") or payload.get("E") or time.time() * 1000)
#             try_trigger_on_trade(trade_price, trade_ts_ms)

#     except Exception as e:
#         print("on_message error:", e)


# def on_error(ws, error):
#     print("WebSocket error:", error)


# def on_close(ws, code, msg):
#     print("WebSocket closed", code, msg)

#     def _restart():
#         time.sleep(2)
#         run_ws()

#     threading.Thread(target=_restart, daemon=True).start()


# def on_open(ws):
#     print("WebSocket connected")
#     params = [f"{SYMBOL}@kline_{INTERVAL}", f"{SYMBOL}@trade"]
#     msg = {"method": "SUBSCRIBE", "params": params, "id": 1}
#     ws.send(json.dumps(msg))


# def run_ws():
#     url = "wss://fstream.binance.com/stream"
#     ws = websocket.WebSocketApp(
#         url,
#         on_open=on_open,
#         on_message=on_message,
#         on_error=on_error,
#         on_close=on_close,
#     )
#     ws.run_forever(ping_interval=20, ping_timeout=10)


# # ==========================
# # Dash App (display updated levels)
# # ==========================
# app = dash.Dash(__name__)
# server = app.server
# app.layout = html.Div(
#     [
#         html.H1(f"{SYMBOL.upper()} Live Prices & Last {CANDLE_LIMIT} Candles"),
#         html.Div(
#             [
#                 html.H2(id="live-price", style={"color": "black", "marginRight": "20px"}),
#                 html.H2(id="bal", style={"color": "black", "marginRight": "20px"}),
#                 html.Div(id="trade-stats", style={"display": "flex", "gap": "20px", "alignItems": "center"}),
#             ],
#             style={"display": "flex", "flexDirection": "row", "alignItems": "center"},
#         ),
#         html.Div(id="ohlc-values"),
#         html.Div(id="bounds", style={"marginTop": "8px", "color": "#9ad"}),
#         html.Div(id="levels", style={"marginTop": "8px", "color": "#f9a"}),
#         html.H3("Strategy Alerts"),
#         html.Ul(id="strategy-alerts"),
#         dcc.Interval(id="interval", interval=500, n_intervals=0),
#     ]
# )


# @app.callback(
#     [
#         Output("live-price", "children"),
#         Output("bal", "children"),
#         Output("trade-stats", "children"),
#         Output("ohlc-values", "children"),
#         Output("strategy-alerts", "children"),
#         Output("bounds", "children"),
#         Output("levels", "children"),
#     ],
#     [Input("interval", "n_intervals")],
# )
# def update_display(_):
#     if len(candles) == 0:
#         return (
#             "Live Price: --",
#             f"Balance: {fmt_price(initial_balance)}",
#             [],
#             [],
#             [],
#             "Bounds: waiting for enough closed candles...",
#             "Levels: waiting..."
#         )

#     lp = f"Live Price: {fmt_price(live_price)}"
#     bal = f"Balance: {fmt_price(initial_balance)}"

#     stats_html = [
#         html.Div(f"FillCheck: {fillcheck}"),
#         html.Div(f"FillCount: {fillcount}"),
#         html.Div(f"TotalTrades: {totaltradecount}"),
#         html.Div(f"UnfilledPnL: {fmt_price(unfilledpnl)}"),
#         html.Div(f"ATR(multiplied): {fmt_price(last_atr) if last_atr else '--'}"),
#     ]

#     ohlc_html = []
#     for idx, row in candles.iterrows():
#         ts_str = row["time"].astimezone(KOLKATA_TZ).strftime("%H:%M:%S")
#         text = (
#             f"{ts_str} → O:{fmt_price(row['Open'])}, H:{fmt_price(row['High'])}, "
#             f"L:{fmt_price(row['Low'])}, C:{fmt_price(row['Close'])}"
#         )
#         ohlc_html.append(html.Div(text))

#     alerts_html = [html.Li(a) for a in alerts[-10:]]

#     if upper_bound is not None and lower_bound is not None and _bounds_candle_ts is not None:
#         btxt = (
#             f"Bounds[{LENGTH}] → Upper {fmt_price(upper_bound)}, Lower {fmt_price(lower_bound)} "
#             f"(from candle {_bounds_candle_ts.astimezone(KOLKATA_TZ).strftime('%H:%M:%S')})"
#         )
#     else:
#         btxt = "Bounds: waiting for enough closed candles..."

#     lvls = f"Long ENTRY: {fmt_price(long_entry_price)} | Long EXIT: {fmt_price(long_exit_price)} | ATR(mult): {fmt_price(last_atr)}"

#     return lp, bal, stats_html, ohlc_html, alerts_html, btxt, lvls


# # Keep-alive ping thread
# def keep_alive():
#     try:
#         print(f"🔄 Pinging {PING_URL}")
#         r = requests.get(PING_URL, timeout=10)
#         print("✅ Ping response:", r.status_code)
#     except Exception as e:
#         print("⚠️ Keep-alive ping failed:", str(e))


# @server.route('/ping', methods=['GET'])
# def ping():
#     keep_alive()
#     return jsonify({"status": "alive"}), 200


# # ==========================
# # Main (Render compatible)
# # ==========================
# if __name__ == "__main__":
#     try:
#         load_state()
#     except Exception as e:
#         print("Warning: load_state() failed:", e)

#     # fetch initial candles if needed
#     if candles.empty:
#         try:
#             fetch_initial_candles()
#         except Exception as e:
#             print("Initial fetch failed:", e)

#     # compute levels if possible
#     if len(candles) >= LENGTH + 1:
#         recompute_bounds_on_close()

#     t = threading.Thread(target=run_ws, daemon=True)
#     t.start()

#     def periodic_save_loop(interval_s=30):
#         while True:
#             try:
#                 save_state()
#             except Exception as e:
#                 print("Periodic save error:", e)
#             time.sleep(interval_s)

#     saver_thread = threading.Thread(target=periodic_save_loop, args=(30,), daemon=True)
#     saver_thread.start()

#     port = int(os.environ.get("PORT", 10000))
#     app.run(host="0.0.0.0", port=port, debug=False)



# import json
# import os
# import time
# import threading
# from datetime import datetime, timezone, timedelta

# import pandas as pd
# import requests
# import websocket
# import dash
# from dash import dcc, html
# from dash.dependencies import Input, Output
# from flask import Flask, request, jsonify
# from pymongo import MongoClient

# # ==========================
# # Config (tweak here)
# # ==========================
# SYMBOL = "ethusdc"       # keep lowercase for websocket streams
# INTERVAL = "5m"
# CANDLE_LIMIT = 50         # needs to be large enough to compute ATR reliably
# PING_URL = os.environ.get("PING_URL", "https://bot-reviver.onrender.com/ping")
# WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://binance-65gz.onrender.com/web")

# # === Strategy params (change to your preference) ===
# ATR_LENGTH = 5           # lookback for ATR (use >=1). 5 is reasonable.
# ATR_MULT = 0.75          # ATR multiplier
# SLIPPAGE_TICKS = 1       # exit slippage in ticks
# TICK_SIZE = 0.01         # ETHUSDC tick size (adjust if needed)

# # ====For channel bounds (keeps earlier behaviour)
# LENGTH = 3  # number of LAST CLOSED candles to compute channel bounds

# MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
# DB_NAME = "trading_bot_eth_atr"
# COLLECTION_STATE = "bot_state"

# # ==========================
# # Timezones
# # ==========================
# KOLKATA_TZ = timezone(timedelta(hours=5, minutes=30))
# STORE_TZ = timezone.utc

# # ==========================
# # Globals
# # ==========================
# candles = pd.DataFrame(columns=["time", "Open", "High", "Low", "Close"])  # 'time' is UTC-aware
# live_price = None
# last_valid_price = None
# alerts = []

# initial_balance = 10.0
# entryprice = None
# running_pnl = 0.0

# upper_bound = None
# lower_bound = None
# _bounds_candle_ts = None  # UTC datetime of the candle used to compute bounds
# _triggered_window_id = None
# _triggered_window_side = None
# _last_exit_lock = None

# EntryCount = 0
# LastSide = None
# LastLastSide = "buy"
# status = None

# # Order filling data
# fillcheck = 0
# fillcount = 0
# totaltradecount = 0
# unfilledpnl = 0.0
# lastpnl = 0

# # Strategy levels (computed)
# long_entry_price = None
# long_exit_price = None
# last_atr = None

# # ==========================
# # MongoDB Setup
# # ==========================
# mongo_client = MongoClient(MONGO_URI)
# db = mongo_client[DB_NAME]
# state_col = db[COLLECTION_STATE]


# def _serialize_candles(df: pd.DataFrame):
#     """Return list[dict] with 'time' as ISO strings for storage."""
#     if df is None or df.empty:
#         return []
#     tmp = df.copy()

#     def _iso_safe(x):
#         if pd.isna(x):
#             return None
#         if isinstance(x, str):
#             return x
#         if getattr(x, "tzinfo", None) is None:
#             x = x.replace(tzinfo=STORE_TZ)
#         return x.isoformat()

#     tmp["time"] = tmp["time"].apply(_iso_safe)
#     return tmp.to_dict(orient="records")


# def _deserialize_candles(records):
#     """Turn stored records (with time iso) into DataFrame with timezone-aware datetimes (UTC)."""
#     if not records:
#         return pd.DataFrame(columns=["time", "Open", "High", "Low", "Close"])
#     df = pd.DataFrame(records)
#     if "time" in df.columns:
#         df["time"] = pd.to_datetime(df["time"], utc=True)
#         df["time"] = df["time"].dt.tz_convert(STORE_TZ)
#     return df


# def save_state():
#     """Save global state + candles + alerts to MongoDB."""
#     global candles, live_price, last_valid_price, alerts
#     global initial_balance, entryprice, running_pnl
#     global upper_bound, lower_bound, _bounds_candle_ts, _triggered_window_id
#     global EntryCount, LastSide, LastLastSide, status
#     global fillcheck, fillcount, totaltradecount, unfilledpnl
#     global long_entry_price, long_exit_price, last_atr

#     try:
#         state = {
#             "_id": "bot_state",
#             "candles": _serialize_candles(candles),
#             "live_price": float(live_price) if is_valid_price(live_price) else None,
#             "last_valid_price": float(last_valid_price) if is_valid_price(last_valid_price) else None,
#             "alerts": alerts[-100:],
#             "initial_balance": float(initial_balance),
#             "entryprice": float(entryprice) if is_valid_price(entryprice) else None,
#             "running_pnl": float(running_pnl),
#             "upper_bound": float(upper_bound) if is_valid_price(upper_bound) else None,
#             "lower_bound": float(lower_bound) if is_valid_price(lower_bound) else None,
#             "_bounds_candle_ts": _bounds_candle_ts.isoformat() if _bounds_candle_ts else None,
#             "_triggered_window_id": _triggered_window_id.isoformat() if _triggered_window_id else None,
#             "_triggered_window_side": _triggered_window_side,
#             "_last_exit_lock": _last_exit_lock,
#             "EntryCount": int(EntryCount),
#             "LastSide": LastSide,
#             "LastLastSide": LastLastSide,
#             "status": status,
#             "fillcheck": int(fillcheck),
#             "fillcount": int(fillcount),
#             "totaltradecount": int(totaltradecount),
#             "unfilledpnl": float(unfilledpnl),
#             "long_entry_price": float(long_entry_price) if is_valid_price(long_entry_price) else None,
#             "long_exit_price": float(long_exit_price) if is_valid_price(long_exit_price) else None,
#             "last_atr": float(last_atr) if is_valid_price(last_atr) else None,
#         }
#         state_col.replace_one({"_id": "bot_state"}, state, upsert=True)
#     except Exception as e:
#         print("⚠️ Error saving state to MongoDB:", e)


# def load_state():
#     """Restore global state from MongoDB."""
#     global candles, live_price, last_valid_price, alerts
#     global initial_balance, entryprice, running_pnl
#     global upper_bound, lower_bound, _bounds_candle_ts, _triggered_window_id
#     global EntryCount, LastSide, LastLastSide, status
#     global fillcheck, fillcount, totaltradecount, unfilledpnl
#     global long_entry_price, long_exit_price, last_atr, _last_exit_lock

#     try:
#         doc = state_col.find_one({"_id": "bot_state"})
#         if not doc:
#             print("ℹ️ No saved state found in DB, fresh start.")
#             return

#         candles = _deserialize_candles(doc.get("candles", []))
#         live_price = doc.get("live_price")
#         last_valid_price = doc.get("last_valid_price")
#         alerts = doc.get("alerts", []) or []

#         initial_balance = doc.get("initial_balance", 10.0)
#         entryprice = doc.get("entryprice")
#         running_pnl = doc.get("running_pnl", 0.0)

#         upper_bound = doc.get("upper_bound")
#         lower_bound = doc.get("lower_bound")

#         _bounds_candle_ts = (
#             pd.to_datetime(doc.get("_bounds_candle_ts")).tz_convert(STORE_TZ)
#             if doc.get("_bounds_candle_ts") else None
#         )
#         _triggered_window_id = (
#             pd.to_datetime(doc.get("_triggered_window_id")).tz_convert(STORE_TZ)
#             if doc.get("_triggered_window_id") else None
#         )
#         _triggered_window_side = doc.get("_triggered_window_side","buy")
#         _last_exit_lock = doc.get("_last_exit_lock","unlock")

#         EntryCount = doc.get("EntryCount", 0)
#         LastSide = doc.get("LastSide")
#         LastLastSide = doc.get("LastLastSide", "buy")
#         status = doc.get("status")

#         fillcheck = doc.get("fillcheck", 0)
#         fillcount = doc.get("fillcount", 0)
#         totaltradecount = doc.get("totaltradecount", 0)
#         unfilledpnl = doc.get("unfilledpnl", 0.0)

#         long_entry_price = doc.get("long_entry_price")
#         long_exit_price = doc.get("long_exit_price")
#         last_atr = doc.get("last_atr")

#         print("✅ State restored from MongoDB")
#     except Exception as e:
#         print("⚠️ Error loading state from MongoDB:", e)


# # ==========================
# # Utilities
# # ==========================
# def is_valid_price(x):
#     try:
#         return float(x) > 0
#     except Exception:
#         return False


# def fmt_price(p):
#     if p is None:
#         return "--"
#     ap = abs(p)
#     if ap >= 100:
#         return f"{p:.2f}"
#     elif ap >= 1:
#         return f"{p:.4f}"
#     else:
#         return f"{p:.8f}"


# # ==========================
# # Strategy helpers (fixed)
# # ==========================
# def compute_true_range_series(df: pd.DataFrame) -> pd.Series:
#     """Compute True Range (TR) for a DataFrame of closed candles.
#     Expects df has columns Open/High/Low/Close and index is chronological.
#     """
#     if df is None or df.empty or len(df) < 2:
#         return pd.Series(dtype=float)
#     prev_close = df["Close"].shift(1)
#     tr1 = df["High"] - df["Low"]
#     tr2 = (df["High"] - prev_close).abs()
#     tr3 = (df["Low"] - prev_close).abs()
#     tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
#     return tr


# def compute_atr_from_closed_candles(atr_len=ATR_LENGTH):
#     """Compute ATR using last `atr_len` closed candles.
#     Algorithm:
#       - closed = all candles except the current open (candles.iloc[:-1])
#       - compute TR series on closed (needs at least 2 closed candles)
#       - take the last `atr_len` TR values (if available) and average them
#     Returns average TR (ATR) or None.
#     """
#     global candles
#     if candles is None or candles.empty:
#         return None
#     closed = candles.iloc[:-1].copy() if len(candles) >= 2 else pd.DataFrame()
#     if closed is None or len(closed) < 2:
#         # not enough closed data to compute TR
#         return None

#     tr = compute_true_range_series(closed)
#     if tr.empty:
#         return None

#     # pick the last `atr_len` TR values (if there are fewer, use what's available)
#     if atr_len <= 0:
#         return None
#     take = min(len(tr), atr_len)
#     last_tr = tr.tail(take)
#     if last_tr.empty:
#         return None
#     atr = float(last_tr.mean())
#     return atr


# # ==========================
# # Helpers (unchanged)
# # ==========================
# def fetch_initial_candles():
#     """Seed candles from Binance REST klines endpoint."""
#     global candles, live_price, last_valid_price
#     url = (
#         f"https://fapi.binance.com/fapi/v1/klines?symbol={SYMBOL.upper()}&interval={INTERVAL}&limit={CANDLE_LIMIT}"
#     )
#     r = requests.get(url, timeout=10)
#     r.raise_for_status()
#     data = r.json()

#     rows = []
#     for k in data:
#         rows.append(
#             {
#                 "time": datetime.fromtimestamp(k[0] / 1000, tz=STORE_TZ),  # UTC
#                 "Open": float(k[1]),
#                 "High": float(k[2]),
#                 "Low": float(k[3]),
#                 "Close": float(k[4]),
#             }
#         )
#     candles = pd.DataFrame(rows)
#     if not candles.empty:
#         live_price = float(candles["Close"].iloc[-1])
#         last_valid_price = live_price if is_valid_price(live_price) else None
#     save_state()


# def calculate_pnl(entry_price: float, closing_price: float, side: str) -> float:
#     quantity = 0.005  # fixed quantity
#     if side == "buy":
#         return (closing_price - entry_price) * quantity
#     elif side == "sell":
#         return (entry_price - closing_price) * quantity
#     else:
#         return 0.0


# def send_webhook(trigger_time_iso: str, entry_price_in: float, side: str, status_fun: str):
#     global EntryCount, LastSide, status, entryprice, running_pnl, initial_balance, lastpnl
#     global fillcheck, totaltradecount, unfilledpnl, LastLastSide

#     secret = "gajraj09"
#     quantity = 0.005

#     status = status_fun
#     pnl = 0.0

#     if status == "exit":
#         if entryprice is None:
#             print("[WEBHOOK] Exit requested but no stored entryprice; ignoring pnl update.")
#         else:
#             pnl = calculate_pnl(entryprice, entry_price_in, LastLastSide)
#             if fillcheck == 0:
#                 initial_balance += pnl

#             running_pnl += pnl
#             lastpnl = pnl
#         entryprice = None
#         if fillcheck == 1:
#             unfilledpnl += pnl
#         fillcheck = 0
#     else:  # status == "entry"
#         if entryprice is not None:
#             # Close any previous implicit position first for accounting
#             pnl = calculate_pnl(entryprice, entry_price_in, LastLastSide)
#             if fillcheck == 0:
#                 initial_balance += pnl
#             running_pnl += pnl
#             lastpnl = pnl
#         else:
#             print(f"[ENTRY] Opening first position at {fmt_price(entry_price_in)}")
#         entryprice = entry_price_in
#         totaltradecount += 1
#         fillcheck = 1
#     LastLastSide = LastSide

#     try:
#         payload = {
#             "symbol": SYMBOL.upper(),
#             "side": side,
#             "quantity": quantity,
#             "price": entry_price_in,
#             "status": status,
#             "secret": secret,
#         }
#         requests.post(WEBHOOK_URL, json=payload, timeout=5)
#         print("Sent payload:", payload)
#     except Exception as e:
#         print("Webhook error:", e)
#     save_state()


# def recompute_bounds_on_close():
#     """Compute channel bounds and ATR-based entry/exit levels when a candle closes."""
#     global upper_bound, lower_bound, _bounds_candle_ts, _triggered_window_id
#     global long_entry_price, long_exit_price, last_atr

#     # Need at least LENGTH closed candles + current open => len(candles) >= LENGTH + 1
#     if len(candles) < LENGTH + 1:
#         upper_bound = None
#         lower_bound = None
#         _bounds_candle_ts = None
#         _triggered_window_id = None
#         long_entry_price = None
#         long_exit_price = None
#         last_atr = None
#         save_state()
#         return

#     closed = candles.iloc[:-1]  # exclude current open
#     window = closed.tail(LENGTH)
#     highs = window["High"][window["High"] > 0]
#     lows = window["Low"][window["Low"] > 0]
#     if highs.empty or lows.empty:
#         return

#     upper_bound = float(highs.max())
#     lower_bound = float(lows.min())
#     _bounds_candle_ts = window["time"].iloc[-1]  # UTC
#     _triggered_window_id = None

#     # Compute ATR using closed candles
#     atr = compute_atr_from_closed_candles(ATR_LENGTH)
#     if atr is None:
#         last_atr = None
#         long_entry_price = None
#         long_exit_price = None
#         save_state()
#         return

#     last_atr = atr * ATR_MULT

#     # reference close = last closed candle's close
#     last_closed = closed.iloc[-1]
#     ref_close = float(last_closed["Close"])

#     long_entry_price = float(ref_close + last_atr)
#     long_exit_price = float(ref_close - last_atr)

#     print(f"[BOUNDS] upper={fmt_price(upper_bound)} lower={fmt_price(lower_bound)} | ATR={last_atr:.6f}")
#     print(f"[LEVELS] ENTRY={fmt_price(long_entry_price)} EXIT={fmt_price(long_exit_price)}")

#     save_state()


# # ==========================
# # Fillcheck update on every tick
# # ==========================
# def update_fillcheck(trade_price: float):
#     global fillcheck, fillcount, entryprice, LastSide, status, totaltradecount, unfilledpnl

#     if entryprice is None or fillcheck == 0:
#         return
#     was_fill = False
#     if status == "entry":
#         if LastSide == "buy" and trade_price <= entryprice:  # <= to allow exact touch
#             fillcheck = 0
#             fillcount += 1
#             was_fill = True
#         elif LastSide == "sell" and trade_price >= entryprice:
#             fillcheck = 0
#             fillcount += 1
#             was_fill = True
#     elif status == "exit":
#         fillcheck = 0
#         was_fill = True
#     if was_fill:
#         save_state()


# # ==========================
# # Trigger logic
# # ==========================
# def try_trigger_on_trade(trade_price: float, trade_ts_ms: int):
#     """
#     ENTRY (long-only): if no open position and trade_price >= long_entry_price -> send entry webhook
#     EXIT (for longs): if we have an open position and trade_price <= long_exit_price -> send exit webhook (apply slippage)
#     """
#     global alerts, _triggered_window_id, _triggered_window_side
#     global status, EntryCount, LastSide, _last_exit_lock
#     global long_entry_price, long_exit_price, last_atr, entryprice, fillcheck

#     # Preconditions
#     if not is_valid_price(trade_price) or long_entry_price is None or long_exit_price is None:
#         return

#     # timestamps
#     trigger_time = datetime.fromtimestamp(trade_ts_ms / 1000, tz=KOLKATA_TZ)
#     trigger_time_iso = trigger_time.isoformat()
#     ts_dt = datetime.fromtimestamp(trade_ts_ms / 1000, tz=STORE_TZ)

#     def log_and_send(side: str, price_val: float, friendly: str, st: str):
#         """Helper to send webhook + alert + save state"""
#         nonlocal trigger_time_iso, ts_dt
#         global alerts, _triggered_window_id, _triggered_window_side, LastSide

#         LastSide = side
#         _triggered_window_id = _bounds_candle_ts
#         _triggered_window_side = side

#         send_webhook(trigger_time_iso, price_val, side, st)

#         msg = (
#             f"{friendly} | {st}: {fmt_price(price_val)} | Live {fmt_price(trade_price)} "
#             f"| Trigger {ts_dt.astimezone(KOLKATA_TZ).strftime('%H:%M:%S')}"
#         )
#         alerts.append(msg)
#         alerts[:] = alerts[-50:]
#         save_state()

#     # -------- EXIT logic (priority) ----------
#     if entryprice is not None and is_valid_price(entryprice):
#         if trade_price <= long_exit_price:
#             slippage_amount = SLIPPAGE_TICKS * TICK_SIZE
#             exit_price_with_slippage = max(0.0, long_exit_price - slippage_amount)
#             print(f"[TRIGGER] EXIT hit at {fmt_price(trade_price)} <= {fmt_price(long_exit_price)}; sending exit @ {fmt_price(exit_price_with_slippage)}")
#             log_and_send("buy", exit_price_with_slippage, "EXIT (ATR-based)", "exit")
#             return

#     # -------- ENTRY logic ----------
#     has_open_position = entryprice is not None and fillcheck == 1
#     if not has_open_position:
#         if trade_price >= long_entry_price:
#             print(f"[TRIGGER] ENTRY conditions met: live {fmt_price(trade_price)} >= entry {fmt_price(long_entry_price)}")
#             log_and_send("buy", long_entry_price, "LONG (ATR-based)", "entry")
#             return


# # ==========================
# # WebSocket handlers
# # ==========================
# def on_message(ws, message):
#     global candles, live_price, last_valid_price, status, lastpnl, _triggered_window_id, _last_exit_lock
#     try:
#         data = json.loads(message)
#         stream = data.get("stream")
#         payload = data.get("data")

#         if stream and "kline" in stream:
#             kline = payload["k"]
#             ts_dt = datetime.fromtimestamp(kline["t"] / 1000, tz=STORE_TZ)  # UTC

#             o, h, l, c = (
#                 float(kline["o"]),
#                 float(kline["h"]),
#                 float(kline["l"]),
#                 float(kline["c"]),
#             )

#             if is_valid_price(c):
#                 last_valid_price = c
#                 live_price = c

#             new_candle_detected = candles.empty or candles.iloc[-1]["time"] != ts_dt

#             if new_candle_detected:
#                 open_val = o if is_valid_price(o) else (last_valid_price if last_valid_price is not None else None)
#                 if open_val is None:
#                     return

#                 high_val = h if is_valid_price(h) else open_val
#                 low_val = l if is_valid_price(l) else open_val
#                 close_val = c if is_valid_price(c) else open_val

#                 new_row = {
#                     "time": ts_dt,  # UTC
#                     "Open": open_val,
#                     "High": max(high_val, open_val),
#                     "Low": min(low_val, open_val),
#                     "Close": close_val,
#                 }
#                 candles = pd.concat([candles, pd.DataFrame([new_row])], ignore_index=True)

#                 # just mark lock state; do not auto-exit here
#                 _last_exit_lock = "unlock"

#                 save_state()

#             else:
#                 idx = candles.index[-1]
#                 if is_valid_price(h):
#                     candles.at[idx, "High"] = max(candles.at[idx, "High"], h)
#                 if is_valid_price(l):
#                     candles.at[idx, "Low"] = min(candles.at[idx, "Low"], l)
#                 if is_valid_price(c):
#                     candles.at[idx, "Close"] = c
#                 live_price = c
#                 last_valid_price = c

#             # trim
#             if len(candles) > CANDLE_LIMIT:
#                 candles = candles.tail(CANDLE_LIMIT).reset_index(drop=True)

#             if kline.get("x"):
#                 # Candle closed -> recompute bounds (ATR & entry/exit levels)
#                 recompute_bounds_on_close()

#         elif stream and "trade" in stream:
#             trade_price_raw = payload.get("p")
#             if not is_valid_price(trade_price_raw):
#                 return

#             trade_price = float(trade_price_raw)
#             live_price = trade_price
#             last_valid_price = trade_price

#             if not candles.empty:
#                 idx = candles.index[-1]
#                 # Update last candle tick by tick
#                 candles.at[idx, "Close"] = trade_price
#                 candles.at[idx, "High"] = max(candles.at[idx, "High"], trade_price)
#                 candles.at[idx, "Low"] = min(candles.at[idx, "Low"], trade_price)

#             # Update fill status and try triggers
#             update_fillcheck(trade_price)
#             trade_ts_ms = int(payload.get("T") or payload.get("E") or time.time() * 1000)
#             try_trigger_on_trade(trade_price, trade_ts_ms)

#     except Exception as e:
#         print("on_message error:", e)


# def on_error(ws, error):
#     print("WebSocket error:", error)


# def on_close(ws, code, msg):
#     print("WebSocket closed", code, msg)

#     def _restart():
#         time.sleep(2)
#         run_ws()

#     threading.Thread(target=_restart, daemon=True).start()


# def on_open(ws):
#     print("WebSocket connected")
#     params = [f"{SYMBOL}@kline_{INTERVAL}", f"{SYMBOL}@trade"]
#     msg = {"method": "SUBSCRIBE", "params": params, "id": 1}
#     ws.send(json.dumps(msg))


# def run_ws():
#     url = "wss://fstream.binance.com/stream"
#     ws = websocket.WebSocketApp(
#         url,
#         on_open=on_open,
#         on_message=on_message,
#         on_error=on_error,
#         on_close=on_close,
#     )
#     ws.run_forever(ping_interval=20, ping_timeout=10)


# # ==========================
# # Dash App (display updated levels)
# # ==========================
# app = dash.Dash(__name__)
# server = app.server
# app.layout = html.Div(
#     [
#         html.H1(f"{SYMBOL.upper()} Live Prices & Last {CANDLE_LIMIT} Candles"),
#         html.Div(
#             [
#                 html.H2(id="live-price", style={"color": "black", "marginRight": "20px"}),
#                 html.H2(id="bal", style={"color": "black", "marginRight": "20px"}),
#                 html.Div(id="trade-stats", style={"display": "flex", "gap": "20px", "alignItems": "center"}),
#             ],
#             style={"display": "flex", "flexDirection": "row", "alignItems": "center"},
#         ),
#         html.Div(id="ohlc-values"),
#         html.Div(id="bounds", style={"marginTop": "8px", "color": "#9ad"}),
#         html.Div(id="levels", style={"marginTop": "8px", "color": "#f9a"}),
#         html.H3("Strategy Alerts"),
#         html.Ul(id="strategy-alerts"),
#         dcc.Interval(id="interval", interval=500, n_intervals=0),
#     ]
# )


# @app.callback(
#     [
#         Output("live-price", "children"),
#         Output("bal", "children"),
#         Output("trade-stats", "children"),
#         Output("ohlc-values", "children"),
#         Output("strategy-alerts", "children"),
#         Output("bounds", "children"),
#         Output("levels", "children"),
#     ],
#     [Input("interval", "n_intervals")],
# )
# def update_display(_):
#     if len(candles) == 0:
#         return (
#             "Live Price: --",
#             f"Balance: {fmt_price(initial_balance)}",
#             [],
#             [],
#             [],
#             "Bounds: waiting for enough closed candles...",
#             "Levels: waiting..."
#         )

#     lp = f"Live Price: {fmt_price(live_price)}"
#     bal = f"Balance: {fmt_price(initial_balance)}"

#     stats_html = [
#         html.Div(f"FillCheck: {fillcheck}"),
#         html.Div(f"FillCount: {fillcount}"),
#         html.Div(f"TotalTrades: {totaltradecount}"),
#         html.Div(f"UnfilledPnL: {fmt_price(unfilledpnl)}"),
#         html.Div(f"ATR(multiplied): {fmt_price(last_atr) if last_atr else '--'}"),
#     ]

#     ohlc_html = []
#     for idx, row in candles.iterrows():
#         ts_str = row["time"].astimezone(KOLKATA_TZ).strftime("%H:%M:%S")
#         text = (
#             f"{ts_str} → O:{fmt_price(row['Open'])}, H:{fmt_price(row['High'])}, "
#             f"L:{fmt_price(row['Low'])}, C:{fmt_price(row['Close'])}"
#         )
#         ohlc_html.append(html.Div(text))

#     alerts_html = [html.Li(a) for a in alerts[-10:]]

#     if upper_bound is not None and lower_bound is not None and _bounds_candle_ts is not None:
#         btxt = (
#             f"Bounds[{LENGTH}] → Upper {fmt_price(upper_bound)}, Lower {fmt_price(lower_bound)} "
#             f"(from candle {_bounds_candle_ts.astimezone(KOLKATA_TZ).strftime('%H:%M:%S')})"
#         )
#     else:
#         btxt = "Bounds: waiting for enough closed candles..."

#     lvls = f"Long ENTRY: {fmt_price(long_entry_price)} | Long EXIT: {fmt_price(long_exit_price)} | ATR(mult): {fmt_price(last_atr)}"

#     return lp, bal, stats_html, ohlc_html, alerts_html, btxt, lvls


# # Keep-alive ping thread
# def keep_alive():
#     try:
#         print(f"🔄 Pinging {PING_URL}")
#         r = requests.get(PING_URL, timeout=10)
#         print("✅ Ping response:", r.status_code)
#     except Exception as e:
#         print("⚠️ Keep-alive ping failed:", str(e))


# @server.route('/ping', methods=['GET'])
# def ping():
#     keep_alive()
#     return jsonify({"status": "alive"}), 200


# # ==========================
# # Main (Render compatible)
# # ==========================
# if __name__ == "__main__":
#     try:
#         load_state()
#     except Exception as e:
#         print("Warning: load_state() failed:", e)

#     # fetch initial candles if needed
#     if candles.empty:
#         try:
#             fetch_initial_candles()
#         except Exception as e:
#             print("Initial fetch failed:", e)

#     # compute levels if possible
#     if len(candles) >= LENGTH + 1:
#         recompute_bounds_on_close()

#     t = threading.Thread(target=run_ws, daemon=True)
#     t.start()

#     def periodic_save_loop(interval_s=30):
#         while True:
#             try:
#                 save_state()
#             except Exception as e:
#                 print("Periodic save error:", e)
#             time.sleep(interval_s)

#     saver_thread = threading.Thread(target=periodic_save_loop, args=(30,), daemon=True)
#     saver_thread.start()

#     port = int(os.environ.get("PORT", 10000))
#     app.run(host="0.0.0.0", port=port, debug=False)







#================================================================================================================================================
#================================================================================================================================================
# import json
# import os
# import time
# import threading
# from datetime import datetime, timezone, timedelta

# import pandas as pd
# import requests
# import websocket
# import dash
# from dash import dcc, html
# from dash.dependencies import Input, Output
# from flask import Flask, request, jsonify
# from pymongo import MongoClient



# # ==========================
# # Config
# # ==========================
# SYMBOL = "ethusdc"       # keep lowercase for websocket streams
# INTERVAL = "15m"
# CANDLE_LIMIT = 10
# PING_URL = os.environ.get("PING_URL", "https://bot-reviver.onrender.com/ping")
# WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://binance-65gz.onrender.com/webh")

# # ====For ETHUSDC 15 Min Chart
# LENGTH = 3  # number of LAST CLOSED candles to compute bounds
# # ====For XRPUSDC 15 Min Chart
# # LENGTH = 2  # number of LAST CLOSED candles to compute bounds

# MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
# DB_NAME = "trading_bot_eth_longonly"
# COLLECTION_STATE = "bot_state"

# # ==========================
# # Timezones
# # ==========================
# # Store all candle timestamps in UTC. Convert to Kolkata only for display.
# KOLKATA_TZ = timezone(timedelta(hours=5, minutes=30))
# STORE_TZ = timezone.utc

# # ==========================
# # Globals
# # ==========================
# candles = pd.DataFrame(columns=["time", "Open", "High", "Low", "Close"])  # 'time' is UTC-aware
# live_price = None
# last_valid_price = None
# alerts = []

# initial_balance = 10.0
# entryprice = None
# running_pnl = 0.0

# upper_bound = None
# lower_bound = None
# _bounds_candle_ts = None  # UTC datetime of the candle used to compute bounds
# _triggered_window_id = None
# _triggered_window_side = None
# _last_exit_lock = "unlock"

# EntryCount = 0
# LastSide = None
# LastLastSide = "buy"
# status = None

# #condition_lock
# _condition_lock = 0

# # Order filling data
# fillcheck = 0
# fillcount = 0
# totaltradecount = 0
# unfilledpnl = 0.0
# lastpnl = 0
# # exitfillcheck=0
# # exitfillcount=0
# # exitfilltotal=0


# # ==========================
# # MongoDB Setup
# # ==========================
# mongo_client = MongoClient(MONGO_URI)
# db = mongo_client[DB_NAME]
# state_col = db[COLLECTION_STATE]


# def _serialize_candles(df: pd.DataFrame):
#     """Return list[dict] with 'time' as ISO strings for storage."""
#     if df is None or df.empty:
#         return []
#     tmp = df.copy()
#     def _iso_safe(x):
#         if pd.isna(x):
#             return None
#         if isinstance(x, str):
#             return x
#         # ensure timezone-aware
#         if getattr(x, "tzinfo", None) is None:
#             x = x.replace(tzinfo=STORE_TZ)
#         return x.isoformat()
#     tmp["time"] = tmp["time"].apply(_iso_safe)
#     return tmp.to_dict(orient="records")


# def _deserialize_candles(records):
#     """Turn stored records (with time iso) into DataFrame with timezone-aware datetimes (UTC)."""
#     if not records:
#         return pd.DataFrame(columns=["time", "Open", "High", "Low", "Close"])
#     df = pd.DataFrame(records)
#     if "time" in df.columns:
#         # parse ISO strings into timezone-aware UTC datetimes
#         df["time"] = pd.to_datetime(df["time"], utc=True)
#         # ensure tz is STORE_TZ (UTC)
#         df["time"] = df["time"].dt.tz_convert(STORE_TZ)
#     return df


# def save_state():
#     """Save global state + candles + alerts to MongoDB."""
#     global candles, live_price, last_valid_price, alerts
#     global initial_balance, entryprice, running_pnl
#     global upper_bound, lower_bound, _bounds_candle_ts, _triggered_window_id
#     global EntryCount, LastSide, LastLastSide, status
#     global fillcheck, fillcount, totaltradecount, unfilledpnl

#     try:
#         state = {
#             "_id": "bot_state",
#             "candles": _serialize_candles(candles),
#             "live_price": float(live_price) if is_valid_price(live_price) else None,
#             "last_valid_price": float(last_valid_price) if is_valid_price(last_valid_price) else None,
#             "alerts": alerts[-100:],  # keep last 100
#             "initial_balance": float(initial_balance),
#             "entryprice": float(entryprice) if is_valid_price(entryprice) else None,
#             "running_pnl": float(running_pnl),
#             "upper_bound": float(upper_bound) if is_valid_price(upper_bound) else None,
#             "lower_bound": float(lower_bound) if is_valid_price(lower_bound) else None,
#             "_bounds_candle_ts": _bounds_candle_ts.isoformat() if _bounds_candle_ts else None,
#             "_triggered_window_id": _triggered_window_id.isoformat() if _triggered_window_id else None,
#             "_triggered_window_side": _triggered_window_side,
#             "_last_exit_lock":_last_exit_lock,
#             "EntryCount": int(EntryCount),
#             "LastSide": LastSide,
#             "LastLastSide": LastLastSide,
#             "status": status,
#             "fillcheck": int(fillcheck),
#             "fillcount": int(fillcount),
#             "totaltradecount": int(totaltradecount),
#             "unfilledpnl": float(unfilledpnl)
#         }
#         state_col.replace_one({"_id": "bot_state"}, state, upsert=True)
#     except Exception as e:
#         print("⚠️ Error saving state to MongoDB:", e)


# def load_state():
#     """Restore global state from MongoDB."""
#     global candles, live_price, last_valid_price, alerts
#     global initial_balance, entryprice, running_pnl
#     global upper_bound, lower_bound, _bounds_candle_ts, _triggered_window_id
#     global EntryCount, LastSide, LastLastSide, status
#     global fillcheck, fillcount, totaltradecount, unfilledpnl

#     try:
#         doc = state_col.find_one({"_id": "bot_state"})
#         if not doc:
#             print("ℹ️ No saved state found in DB, fresh start.")
#             return

#         candles = _deserialize_candles(doc.get("candles", []))
#         live_price = doc.get("live_price")
#         last_valid_price = doc.get("last_valid_price")
#         alerts = doc.get("alerts", []) or []

#         initial_balance = doc.get("initial_balance", 10.0)
#         entryprice = doc.get("entryprice")
#         running_pnl = doc.get("running_pnl", 0.0)

#         upper_bound = doc.get("upper_bound")
#         lower_bound = doc.get("lower_bound")

#         _bounds_candle_ts = (
#             pd.to_datetime(doc.get("_bounds_candle_ts")).tz_convert(STORE_TZ)
#             if doc.get("_bounds_candle_ts") else None
#         )
#         _triggered_window_id = (
#             pd.to_datetime(doc.get("_triggered_window_id")).tz_convert(STORE_TZ)
#             if doc.get("_triggered_window_id") else None
#         )
#         _triggered_window_side = doc.get("_triggered_window_side","buy")
#         _last_exit_lock = doc.get("_last_exit_lock","unlock")

#         EntryCount = doc.get("EntryCount", 0)
#         LastSide = doc.get("LastSide")
#         LastLastSide = doc.get("LastLastSide", "buy")
#         status = doc.get("status")

#         fillcheck = doc.get("fillcheck", 0)
#         fillcount = doc.get("fillcount", 0)
#         totaltradecount = doc.get("totaltradecount", 0)
#         unfilledpnl = doc.get("unfilledpnl", 0.0)

#         print("✅ State restored from MongoDB")
#     except Exception as e:
#         print("⚠️ Error loading state from MongoDB:", e)


# # ==========================
# # Utilities
# # ==========================

# def is_valid_price(x):
#     try:
#         return float(x) > 0
#     except Exception:
#         return False


# def fmt_price(p):
#     if p is None:
#         return "--"
#     ap = abs(p)
#     if ap >= 100:
#         return f"{p:.2f}"
#     elif ap >= 1:
#         return f"{p:.4f}"
#     else:
#         return f"{p:.8f}"


# # ==========================
# # Helpers
# # ==========================

# def fetch_initial_candles():
#     """Fetch last CANDLE_LIMIT klines from Binance Futures API and seed the DF.
#     Ensures timestamps are stored in UTC (STORE_TZ)."""
#     global candles, live_price, last_valid_price
#     url = (
#         f"https://fapi.binance.com/fapi/v1/klines?symbol={SYMBOL.upper()}&interval={INTERVAL}&limit={CANDLE_LIMIT}"
#     )
#     r = requests.get(url, timeout=10)
#     r.raise_for_status()
#     data = r.json()

#     rows = []
#     for k in data:
#         rows.append(
#             {
#                 "time": datetime.fromtimestamp(k[0] / 1000, tz=STORE_TZ),  # UTC
#                 "Open": float(k[1]),
#                 "High": float(k[2]),
#                 "Low": float(k[3]),
#                 "Close": float(k[4]),
#             }
#         )
#     candles = pd.DataFrame(rows)
#     if not candles.empty:
#         live_price = float(candles["Close"].iloc[-1])
#         last_valid_price = live_price if is_valid_price(live_price) else None
#     save_state()


# def calculate_pnl(entry_price: float, closing_price: float, side: str) -> float:
#     quantity = 0.005  # fixed quantity
#     if side == "buy":
#         return (closing_price - entry_price) * quantity
#     elif side == "sell":
#         return (entry_price - closing_price) * quantity
#     else:
#         return 0.0


# def send_webhook(trigger_time_iso: str, entry_price_in: float, side: str, status_fun: str):
#     global EntryCount, LastSide, status, entryprice, running_pnl, initial_balance, lastpnl
#     global fillcheck, totaltradecount, unfilledpnl, LastLastSide,exitfillcheck, exitfillcount,exitfillcount

#     if status_fun == "enter":
#         try:
#             payload = {
#                 "symbol": SYMBOL.upper(),
#                 "side": "buy",
#                 "quantity": 0.005,
#                 "price": entry_price_in,
#                 "status": "entry",
#                 "secret": "gajraj09",
#             }
#             requests.post(WEBHOOK_URL, json=payload, timeout=5)
#             print("Sent payload:", payload)
#         except Exception as e:
#             print("Webhook error:", e)
#         return

#     secret = "gajraj09"
#     quantity = 0.005

#     status = status_fun
#     pnl = 0.0

#     if status == "exit":
        
#         if entryprice is None:
#             print("[WEBHOOK] Exit requested but no stored entryprice; ignoring pnl update.")
#         else:
#             pnl = calculate_pnl(entryprice, entry_price_in, LastLastSide)
#             if fillcheck == 0:
#                 initial_balance += pnl
                
#             running_pnl += pnl
#             lastpnl = pnl
#         entryprice = None
#         if fillcheck == 1:
#             unfilledpnl += pnl
#         fillcheck = 0
#         # exitfillcheck = 1   # <--- start tracking exit fills
#         # exitfilltotal += 1   # <--- start tracking exit fills
#         try:
#             payload = {
#                 "symbol": SYMBOL.upper(),
#                 "side": side,
#                 "quantity": quantity,
#                 "price": entry_price_in,
#                 "status": status,
#                 "secret": secret,
#             }
#             requests.post(WEBHOOK_URL, json=payload, timeout=5)
#             print("Sent payload:", payload)
#         except Exception as e:
#             print("Webhook error:", e)
#     else:  # status == "entry"
#         if entryprice is not None:
#             # Close any previous implicit position first for accounting
#             pnl = calculate_pnl(entryprice, entry_price_in, LastLastSide)
#             if fillcheck == 0:
#                 initial_balance += pnl
#             running_pnl += pnl
#             lastpnl = pnl
#         else:
#             print(f"[ENTRY] Opening first position at {fmt_price(entry_price_in)}")
#         entryprice = entry_price_in
#         totaltradecount += 1
#         fillcheck = 1
#     LastLastSide = LastSide
    
#     save_state()


# def recompute_bounds_on_close():
#     global upper_bound, lower_bound, _bounds_candle_ts, _triggered_window_id
#     if len(candles) < LENGTH:
#         upper_bound = None
#         lower_bound = None
#         _bounds_candle_ts = None
#         _triggered_window_id = None
#         save_state()
#         return

#     window = candles.tail(LENGTH)
#     highs = window["High"][window["High"] > 0]
#     lows = window["Low"][window["Low"] > 0]
#     if highs.empty or lows.empty:
#         return

#     upper_bound = float(highs.max())
#     lower_bound = float(lows.min())
#     _bounds_candle_ts = window["time"].iloc[-1]  # UTC
#     _triggered_window_id = None
#     save_state()


# # ==========================
# # Fillcheck update on every tick
# # ==========================

# def update_fillcheck(trade_price: float):
#     global fillcheck, fillcount, entryprice, LastSide, status, totaltradecount, unfilledpnl

#     if entryprice is None or fillcheck == 0:
#         return
#     was_fill = False
#     if status == "entry":
#         if LastSide == "buy" and trade_price <= entryprice:  # <= to allow exact touch
#             fillcheck = 0
#             fillcount += 1
#             was_fill = True
#         elif LastSide == "sell" and trade_price >= entryprice:
#             fillcheck = 0
#             fillcount += 1
#             was_fill = True
#     elif status == "exit":
#         # Exit fill handling can be more sophisticated if you post exit orders
#         fillcheck = 0
#         was_fill = True
#     if was_fill:
#         save_state()

# # def update_fillcheck(trade_price: float):
# #     global fillcheck, fillcount, entryprice, LastSide, status
# #     global totaltradecount, unfilledpnl, exitfillcheck, exitfillcount

# #     if entryprice is None:
# #         return

# #     was_fill = False
# #     if status == "entry" and fillcheck == 1:
# #         if LastSide == "buy" and trade_price <= entryprice:  # touched
# #             fillcheck = 0
# #             fillcount += 1
# #             was_fill = True
# #         elif LastSide == "sell" and trade_price >= entryprice:
# #             fillcheck = 0
# #             fillcount += 1
# #             was_fill = True

# #     elif status == "exit" and exitfillcheck == 1:
# #         # Exit fill condition: reached exit price
# #         if LastSide == "buy" and trade_price >= entryprice:  # exited long
# #             exitfillcheck = 0
# #             exitfillcount += 1
# #             was_fill = True
# #         elif LastSide == "sell" and trade_price <= entryprice:  # exited short
# #             exitfillcheck = 0
# #             exitfillcount += 1
# #             was_fill = True

# #     if was_fill:
# #         save_state()


# # ==========================
# # Trigger logic
# # ==========================



# def try_trigger_on_trade(trade_price: float, trade_ts_ms: int):
#     global alerts, _triggered_window_id, _triggered_window_side
#     global status, EntryCount, LastSide,_last_exit_lock,_condition_lock

#     # Preconditions: skip if required values not set
#     if not (
#         is_valid_price(trade_price)
#         and upper_bound is not None
#         and lower_bound is not None
#         and _bounds_candle_ts is not None
#     ):
#         return

#     # Convert timestamps
#     trigger_time = datetime.fromtimestamp(trade_ts_ms / 1000, tz=KOLKATA_TZ)
#     trigger_time_iso = trigger_time.isoformat()
#     ts_dt = datetime.fromtimestamp(trade_ts_ms / 1000, tz=STORE_TZ)

#     def process_trigger(side: str, entry_val: float, friendly: str):
#         """Handles sending webhook + logging + updating state"""
#         nonlocal trigger_time_iso, ts_dt
#         global alerts, _triggered_window_id, _triggered_window_side, LastSide

#         LastSide = side
#         _triggered_window_id = _bounds_candle_ts
#         _triggered_window_side = side

#         # Send webhook
#         send_webhook(trigger_time_iso, entry_val, side, "entry")

#         # Prepare alert message
#         msg = (
#             f"{friendly} | {status}: {fmt_price(entry_val)} "
#             f"| Live {fmt_price(trade_price)} "
#             f"| Trigger {ts_dt.astimezone(KOLKATA_TZ).strftime('%H:%M:%S')}"
#         )
#         alerts.append(msg)
#         alerts[:] = alerts[-50:]  # keep only last 50

#         # Save state only when trigger fires
#         save_state()
        
#     if _triggered_window_id != _bounds_candle_ts and _condition_lock == 0:
#         if _last_exit_lock == "unlock":
#             _condition_lock = 1
#             send_webhook(trigger_time_iso, upper_bound, "buy", "enter")
#     # === Trigger logic ===
#     if trade_price > upper_bound:
#         if _triggered_window_id != _bounds_candle_ts:
#             if _last_exit_lock == "unlock":
#                 process_trigger("buy", upper_bound, "LONG")

# # ==========================
# # WebSocket handlers
# # ==========================

# def on_message(ws, message):
#     global candles, live_price, last_valid_price,status,lastpnl,_triggered_window_id,_last_exit_lock,_condition_lock
#     try:
#         data = json.loads(message)
#         stream = data.get("stream")
#         payload = data.get("data")

#         if stream and "kline" in stream:
#             kline = payload["k"]
#             ts_dt = datetime.fromtimestamp(kline["t"] / 1000, tz=STORE_TZ)  # UTC

#             o, h, l, c = (
#                 float(kline["o"]),
#                 float(kline["h"]),
#                 float(kline["l"]),
#                 float(kline["c"]),
#             )

#             if is_valid_price(c):
#                 last_valid_price = c
#                 live_price = c

#             new_candle_detected = candles.empty or candles.iloc[-1]["time"] != ts_dt

#             if new_candle_detected:
#                 open_val = o if is_valid_price(o) else (last_valid_price if last_valid_price is not None else None)
#                 if open_val is None:
#                     return

#                 high_val = h if is_valid_price(h) else open_val
#                 low_val = l if is_valid_price(l) else open_val
#                 close_val = c if is_valid_price(c) else open_val

#                 new_row = {
#                     "time": ts_dt,  # UTC
#                     "Open": open_val,
#                     "High": max(high_val, open_val),
#                     "Low": min(low_val, open_val),
#                     "Close": close_val,
#                 }
#                 candles = pd.concat([candles, pd.DataFrame([new_row])], ignore_index=True)

#                 # ---------- OPTIONAL: Send webhook on new candle open (EXIT) ----------
#                 if status == "exit":
#                     _last_exit_lock = "unlock"
#                     _condition_lock = 0
#                 if status != "exit":
#                     _last_exit_lock = "lock"
#                     try:
#                         send_webhook(ts_dt.astimezone(KOLKATA_TZ).strftime("%H:%M:%S"), open_val, "sell", "exit")
#                         msg = f"EXIT | Price: {fmt_price(open_val)} | Time: {ts_dt.astimezone(KOLKATA_TZ).strftime('%H:%M:%S')}| PnL: {lastpnl}"
#                         alerts.append(msg)
#                         alerts[:] = alerts[-50:]
#                     except Exception as e:
#                         print("New candle webhook error:", e)
                
#                 save_state()

#             else:
#                 idx = candles.index[-1]
#                 if is_valid_price(h):
#                     candles.at[idx, "High"] = max(candles.at[idx, "High"], h)
#                 if is_valid_price(l):
#                     candles.at[idx, "Low"] = min(candles.at[idx, "Low"], l)
#                 if is_valid_price(c):
#                     candles.at[idx, "Close"] = c
#                 live_price = c
#                 last_valid_price = c

#             if len(candles) > CANDLE_LIMIT:
#                 candles = candles.tail(CANDLE_LIMIT).reset_index(drop=True)

#             if kline.get("x"):
#                 # Candle closed -> recompute bounds
#                 recompute_bounds_on_close()

#         elif stream and "trade" in stream:
#             trade_price_raw = payload.get("p")
#             if not is_valid_price(trade_price_raw):
#                 return

#             trade_price = float(trade_price_raw)
#             live_price = trade_price
#             last_valid_price = trade_price

#             if not candles.empty:
#                 idx = candles.index[-1]
#                 # Update last candle tick by tick
#                 candles.at[idx, "Close"] = trade_price
#                 candles.at[idx, "High"] = max(candles.at[idx, "High"], trade_price)
#                 candles.at[idx, "Low"] = min(candles.at[idx, "Low"], trade_price)

#             # Update fill status and try triggers
#             update_fillcheck(trade_price)
#             trade_ts_ms = int(payload.get("T") or payload.get("E") or time.time() * 1000)
#             try_trigger_on_trade(trade_price, trade_ts_ms)

#     except Exception as e:
#         print("on_message error:", e)


# def on_error(ws, error):
#     print("WebSocket error:", error)


# def on_close(ws, code, msg):
#     print("WebSocket closed", code, msg)
#     # Single delayed restart to avoid tight loops
#     def _restart():
#         time.sleep(2)
#         run_ws()
#     threading.Thread(target=_restart, daemon=True).start()


# def on_open(ws):
#     print("WebSocket connected")
#     params = [f"{SYMBOL}@kline_{INTERVAL}", f"{SYMBOL}@trade"]
#     msg = {"method": "SUBSCRIBE", "params": params, "id": 1}
#     ws.send(json.dumps(msg))


# def run_ws():
#     url = "wss://fstream.binance.com/stream"
#     ws = websocket.WebSocketApp(
#         url,
#         on_open=on_open,
#         on_message=on_message,
#         on_error=on_error,
#         on_close=on_close,
#     )
#     # Ping to keep alive
#     ws.run_forever(ping_interval=20, ping_timeout=10)


# # ==========================
# # Dash App
# # ==========================
# app = dash.Dash(__name__)
# server = app.server  # expose Flask server for Render/Heroku
# app.layout = html.Div(
#     [
#         html.H1(f"{SYMBOL.upper()} Live Prices & Last {CANDLE_LIMIT} Candles"),
#         html.Div(
#             [
#                 html.H2(id="live-price", style={"color": "black", "marginRight": "20px"}),
#                 html.H2(id="bal", style={"color": "black", "marginRight": "20px"}),
#                 html.Div(id="trade-stats", style={"display": "flex", "gap": "20px", "alignItems": "center"}),
#             ],
#             style={"display": "flex", "flexDirection": "row", "alignItems": "center"},
#         ),
#         html.Div(id="ohlc-values"),
#         html.Div(id="bounds", style={"marginTop": "8px", "color": "#9ad"}),
#         html.H3("Strategy Alerts"),
#         html.Ul(id="strategy-alerts"),
#         dcc.Interval(id="interval", interval=500, n_intervals=0),  # update every 0.5s
#     ]
# )


# @app.callback(
#     [
#         Output("live-price", "children"),
#         Output("bal", "children"),
#         Output("trade-stats", "children"),
#         Output("ohlc-values", "children"),
#         Output("strategy-alerts", "children"),
#         Output("bounds", "children"),
#     ],
#     [Input("interval", "n_intervals")],
# )
# def update_display(_):
#     if len(candles) == 0:
#         return (
#             "Live Price: --",
#             f"Balance: {fmt_price(initial_balance)}",
#             [],
#             [],
#             [],
#             "Bounds: waiting for enough closed candles...",
#         )

#     lp = f"Live Price: {fmt_price(live_price)}"
#     bal = f"Balance: {fmt_price(initial_balance)}"

#     stats_html = [
#         html.Div(f"FillCheck: {fillcheck}"),
#         html.Div(f"FillCount: {fillcount}"),
#         html.Div(f"TotalTrades: {totaltradecount}"),
#         html.Div(f"UnfilledPnL: {fmt_price(unfilledpnl)}"),
#     ]
#     # stats_html = [
#     #     html.Div(f"FillCheck: {fillcheck}"),
#     #     html.Div(f"FillCount: {fillcount}"),
#     #     html.Div(f"ExitFillCheck: {exitfillcheck}"),
#     #     html.Div(f"ExitFillCount: {exitfillcount}"),
#     #     html.Div(f"TotalTrades: {totaltradecount}"),
#     #     html.Div(f"UnfilledPnL: {fmt_price(unfilledpnl)}"),
#     # ]

#     ohlc_html = []
#     for idx, row in candles.iterrows():
#         ts_str = row["time"].astimezone(KOLKATA_TZ).strftime("%H:%M:%S")
#         text = (
#             f"{ts_str} → O:{fmt_price(row['Open'])}, H:{fmt_price(row['High'])}, "
#             f"L:{fmt_price(row['Low'])}, C:{fmt_price(row['Close'])}"
#         )
#         ohlc_html.append(html.Div(text))

#     alerts_html = [html.Li(a) for a in alerts[-10:]]

#     if upper_bound is not None and lower_bound is not None and _bounds_candle_ts is not None:
#         btxt = (
#             f"Bounds[{LENGTH}] → Upper {fmt_price(upper_bound)}, Lower {fmt_price(lower_bound)} "
#             f"(from candle {_bounds_candle_ts.astimezone(KOLKATA_TZ).strftime('%H:%M:%S')})"
#         )
#     else:
#         btxt = "Bounds: waiting for enough closed candles..."

#     return lp, bal, stats_html, ohlc_html, alerts_html, btxt


# # Keep-alive ping thread
# def keep_alive():
#     """Send a ping to the server itself."""
#     try:
#         print(f"🔄 Pinging {PING_URL}")
#         r = requests.get(PING_URL, timeout=10)
#         print("✅ Ping response:", r.status_code)
#     except Exception as e:
#         print("⚠️ Keep-alive ping failed:", str(e))

# @server.route('/ping', methods=['GET'])
# def ping():
#     keep_alive()
#     return jsonify({"status": "alive"}), 200

# # ==========================
# # Main (Render compatible)
# # ==========================
# if __name__ == "__main__":
#         # 1) Attempt to load saved state
#     try:
#         load_state()
#     except Exception as e:
#         print("Warning: load_state() failed:", e)

#     # 2) If DB had no candles, fetch from Binance for initial seeding
#     if candles.empty:
#         try:
#             fetch_initial_candles()
#         except Exception as e:
#             print("Initial fetch failed:", e)

#     # 3) If we have enough candles, recompute bounds
#     if len(candles) >= LENGTH and (upper_bound is None or lower_bound is None):
#         recompute_bounds_on_close()

#     # 4) Start websocket thread
#     t = threading.Thread(target=run_ws, daemon=True)
#     t.start()

#     # 5) Optionally start a periodic save (safeguard) every N seconds to reduce risk of data loss
#     def periodic_save_loop(interval_s=30):
#         while True:
#             try:
#                 save_state()
#             except Exception as e:
#                 print("Periodic save error:", e)
#             time.sleep(interval_s)

#     saver_thread = threading.Thread(target=periodic_save_loop, args=(30,), daemon=True)
#     saver_thread.start()

#     port = int(os.environ.get("PORT", 10000))  # Render sets PORT
#     app.run(host="0.0.0.0", port=port, debug=False)
#=======================================================================================================================================================
#=================================================                                       ===============================================================
#=================================================           * CODE                            ===============================================================
#=================================================                                       ===============================================================
#=======================================================================================================================================================
import json
import os
import time
import threading
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests
import websocket
import dash
from dash import dcc, html
from dash.dependencies import Input, Output
from flask import Flask, request, jsonify
from pymongo import MongoClient



# ==========================
# Config
# ==========================
SYMBOL = "ethusdc"       # keep lowercase for websocket streams
INTERVAL = "15m"
CANDLE_LIMIT = 10
PING_URL = os.environ.get("PING_URL", "https://bot-reviver.onrender.com/ping")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://binance-65gz.onrender.com/web")

# ====For ETHUSDC 15 Min Chart
LENGTH = 3  # number of LAST CLOSED candles to compute bounds
# ====For XRPUSDC 15 Min Chart
# LENGTH = 2  # number of LAST CLOSED candles to compute bounds

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = "trading_bot_eth_longonly_28-8"
COLLECTION_STATE = "bot_state"

# ==========================
# Timezones
# ==========================
# Store all candle timestamps in UTC. Convert to Kolkata only for display.
KOLKATA_TZ = timezone(timedelta(hours=5, minutes=30))
STORE_TZ = timezone.utc

# ==========================
# Globals
# ==========================
candles = pd.DataFrame(columns=["time", "Open", "High", "Low", "Close"])  # 'time' is UTC-aware
live_price = None
last_valid_price = None
alerts = []

initial_balance = 10.0
entryprice = None
running_pnl = 0.0

upper_bound = None
lower_bound = None
_bounds_candle_ts = None  # UTC datetime of the candle used to compute bounds
_triggered_window_id = None
_triggered_window_side = None
_last_exit_lock = None

EntryCount = 0
LastSide = None
LastLastSide = "buy"
status = None

# Order filling data
fillcheck = 0
fillcount = 0
totaltradecount = 0
unfilledpnl = 0.0
lastpnl = 0


# ==========================
# MongoDB Setup
# ==========================
mongo_client = MongoClient(MONGO_URI)
db = mongo_client[DB_NAME]
state_col = db[COLLECTION_STATE]


def _serialize_candles(df: pd.DataFrame):
    """Return list[dict] with 'time' as ISO strings for storage."""
    if df is None or df.empty:
        return []
    tmp = df.copy()
    def _iso_safe(x):
        if pd.isna(x):
            return None
        if isinstance(x, str):
            return x
        # ensure timezone-aware
        if getattr(x, "tzinfo", None) is None:
            x = x.replace(tzinfo=STORE_TZ)
        return x.isoformat()
    tmp["time"] = tmp["time"].apply(_iso_safe)
    return tmp.to_dict(orient="records")


def _deserialize_candles(records):
    """Turn stored records (with time iso) into DataFrame with timezone-aware datetimes (UTC)."""
    if not records:
        return pd.DataFrame(columns=["time", "Open", "High", "Low", "Close"])
    df = pd.DataFrame(records)
    if "time" in df.columns:
        # parse ISO strings into timezone-aware UTC datetimes
        df["time"] = pd.to_datetime(df["time"], utc=True)
        # ensure tz is STORE_TZ (UTC)
        df["time"] = df["time"].dt.tz_convert(STORE_TZ)
    return df


def save_state():
    """Save global state + candles + alerts to MongoDB."""
    global candles, live_price, last_valid_price, alerts
    global initial_balance, entryprice, running_pnl
    global upper_bound, lower_bound, _bounds_candle_ts, _triggered_window_id
    global EntryCount, LastSide, LastLastSide, status
    global fillcheck, fillcount, totaltradecount, unfilledpnl

    try:
        state = {
            "_id": "bot_state",
            "candles": _serialize_candles(candles),
            "live_price": float(live_price) if is_valid_price(live_price) else None,
            "last_valid_price": float(last_valid_price) if is_valid_price(last_valid_price) else None,
            "alerts": alerts[-100:],  # keep last 100
            "initial_balance": float(initial_balance),
            "entryprice": float(entryprice) if is_valid_price(entryprice) else None,
            "running_pnl": float(running_pnl),
            "upper_bound": float(upper_bound) if is_valid_price(upper_bound) else None,
            "lower_bound": float(lower_bound) if is_valid_price(lower_bound) else None,
            "_bounds_candle_ts": _bounds_candle_ts.isoformat() if _bounds_candle_ts else None,
            "_triggered_window_id": _triggered_window_id.isoformat() if _triggered_window_id else None,
            "_triggered_window_side": _triggered_window_side,
            "_last_exit_lock":_last_exit_lock,
            "EntryCount": int(EntryCount),
            "LastSide": LastSide,
            "LastLastSide": LastLastSide,
            "status": status,
            "fillcheck": int(fillcheck),
            "fillcount": int(fillcount),
            "totaltradecount": int(totaltradecount),
            "unfilledpnl": float(unfilledpnl)
        }
        state_col.replace_one({"_id": "bot_state"}, state, upsert=True)
    except Exception as e:
        print("⚠️ Error saving state to MongoDB:", e)


def load_state():
    """Restore global state from MongoDB."""
    global candles, live_price, last_valid_price, alerts
    global initial_balance, entryprice, running_pnl
    global upper_bound, lower_bound, _bounds_candle_ts, _triggered_window_id
    global EntryCount, LastSide, LastLastSide, status
    global fillcheck, fillcount, totaltradecount, unfilledpnl

    try:
        doc = state_col.find_one({"_id": "bot_state"})
        if not doc:
            print("ℹ️ No saved state found in DB, fresh start.")
            return

        candles = _deserialize_candles(doc.get("candles", []))
        live_price = doc.get("live_price")
        last_valid_price = doc.get("last_valid_price")
        alerts = doc.get("alerts", []) or []

        initial_balance = doc.get("initial_balance", 10.0)
        entryprice = doc.get("entryprice")
        running_pnl = doc.get("running_pnl", 0.0)

        upper_bound = doc.get("upper_bound")
        lower_bound = doc.get("lower_bound")

        _bounds_candle_ts = (
            pd.to_datetime(doc.get("_bounds_candle_ts")).tz_convert(STORE_TZ)
            if doc.get("_bounds_candle_ts") else None
        )
        _triggered_window_id = (
            pd.to_datetime(doc.get("_triggered_window_id")).tz_convert(STORE_TZ)
            if doc.get("_triggered_window_id") else None
        )
        _triggered_window_side = doc.get("_triggered_window_side","buy")
        _last_exit_lock = doc.get("_last_exit_lock","unlock")

        EntryCount = doc.get("EntryCount", 0)
        LastSide = doc.get("LastSide")
        LastLastSide = doc.get("LastLastSide", "buy")
        status = doc.get("status")

        fillcheck = doc.get("fillcheck", 0)
        fillcount = doc.get("fillcount", 0)
        totaltradecount = doc.get("totaltradecount", 0)
        unfilledpnl = doc.get("unfilledpnl", 0.0)

        print("✅ State restored from MongoDB")
    except Exception as e:
        print("⚠️ Error loading state from MongoDB:", e)


# ==========================
# Utilities
# ==========================

def is_valid_price(x):
    try:
        return float(x) > 0
    except Exception:
        return False


def fmt_price(p):
    if p is None:
        return "--"
    ap = abs(p)
    if ap >= 100:
        return f"{p:.2f}"
    elif ap >= 1:
        return f"{p:.4f}"
    else:
        return f"{p:.8f}"


# ==========================
# Helpers
# ==========================

def fetch_initial_candles():
    """Fetch last CANDLE_LIMIT klines from Binance Futures API and seed the DF.
    Ensures timestamps are stored in UTC (STORE_TZ)."""
    global candles, live_price, last_valid_price
    url = (
        f"https://fapi.binance.com/fapi/v1/klines?symbol={SYMBOL.upper()}&interval={INTERVAL}&limit={CANDLE_LIMIT}"
    )
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()

    rows = []
    for k in data:
        rows.append(
            {
                "time": datetime.fromtimestamp(k[0] / 1000, tz=STORE_TZ),  # UTC
                "Open": float(k[1]),
                "High": float(k[2]),
                "Low": float(k[3]),
                "Close": float(k[4]),
            }
        )
    candles = pd.DataFrame(rows)
    if not candles.empty:
        live_price = float(candles["Close"].iloc[-1])
        last_valid_price = live_price if is_valid_price(live_price) else None
    save_state()


def calculate_pnl(entry_price: float, closing_price: float, side: str) -> float:
    quantity = 0.005  # fixed quantity
    if side == "buy":
        return (closing_price - entry_price) * quantity
    elif side == "sell":
        return (entry_price - closing_price) * quantity
    else:
        return 0.0


def send_webhook(trigger_time_iso: str, entry_price_in: float, side: str, status_fun: str):
    global EntryCount, LastSide, status, entryprice, running_pnl, initial_balance, lastpnl
    global fillcheck, totaltradecount, unfilledpnl, LastLastSide

    secret = "gajraj09"
    quantity = 0.005

    status = status_fun
    pnl = 0.0

    if status == "exit":
        if entryprice is None:
            print("[WEBHOOK] Exit requested but no stored entryprice; ignoring pnl update.")
        else:
            pnl = calculate_pnl(entryprice, entry_price_in, LastLastSide)
            if fillcheck == 0:
                initial_balance += pnl
                
            running_pnl += pnl
            lastpnl = pnl
        entryprice = None
        if fillcheck == 1:
            unfilledpnl += pnl
        fillcheck = 0
    else:  # status == "entry"
        if entryprice is not None:
            # Close any previous implicit position first for accounting
            pnl = calculate_pnl(entryprice, entry_price_in, LastLastSide)
            if fillcheck == 0:
                initial_balance += pnl
            running_pnl += pnl
            lastpnl = pnl
        else:
            print(f"[ENTRY] Opening first position at {fmt_price(entry_price_in)}")
        entryprice = entry_price_in
        totaltradecount += 1
        fillcheck = 1
    LastLastSide = LastSide


    try:
        payload = {
            "symbol": SYMBOL.upper(),
            "side": side,
            "quantity": quantity,
            "price": entry_price_in,
            "status": status,
            "secret": secret,
        }
        requests.post(WEBHOOK_URL, json=payload, timeout=5)
        print("Sent payload:", payload)
    except Exception as e:
        print("Webhook error:", e)
    save_state()


def recompute_bounds_on_close():
    global upper_bound, lower_bound, _bounds_candle_ts, _triggered_window_id
    if len(candles) < LENGTH:
        upper_bound = None
        lower_bound = None
        _bounds_candle_ts = None
        _triggered_window_id = None
        save_state()
        return

    window = candles.tail(LENGTH)
    highs = window["High"][window["High"] > 0]
    lows = window["Low"][window["Low"] > 0]
    if highs.empty or lows.empty:
        return

    upper_bound = float(highs.max())
    lower_bound = float(lows.min())
    _bounds_candle_ts = window["time"].iloc[-1]  # UTC
    _triggered_window_id = None
    save_state()


# ==========================
# Fillcheck update on every tick
# ==========================

def update_fillcheck(trade_price: float):
    global fillcheck, fillcount, entryprice, LastSide, status, totaltradecount, unfilledpnl

    if entryprice is None or fillcheck == 0:
        return
    was_fill = False
    if status == "entry":
        if LastSide == "buy" and trade_price <= entryprice:  # <= to allow exact touch
            fillcheck = 0
            fillcount += 1
            was_fill = True
        elif LastSide == "sell" and trade_price >= entryprice:
            fillcheck = 0
            fillcount += 1
            was_fill = True
    elif status == "exit":
        # Exit fill handling can be more sophisticated if you post exit orders
        fillcheck = 0
        was_fill = True
    if was_fill:
        save_state()



# ==========================
# Trigger logic
# ==========================



# def try_trigger_on_trade(trade_price: float, trade_ts_ms: int):
#     global alerts, _triggered_window_id, _triggered_window_side
#     global status, EntryCount, LastSide,_last_exit_lock

#     # Preconditions: skip if required values not set
#     if not (
#         is_valid_price(trade_price)
#         and upper_bound is not None
#         and lower_bound is not None
#         and _bounds_candle_ts is not None
#     ):
#         return

#     # Convert timestamps
#     trigger_time = datetime.fromtimestamp(trade_ts_ms / 1000, tz=KOLKATA_TZ)
#     trigger_time_iso = trigger_time.isoformat()
#     ts_dt = datetime.fromtimestamp(trade_ts_ms / 1000, tz=STORE_TZ)

#     def process_trigger(side: str, entry_val: float, friendly: str):
#         """Handles sending webhook + logging + updating state"""
#         nonlocal trigger_time_iso, ts_dt
#         global alerts, _triggered_window_id, _triggered_window_side, LastSide

#         LastSide = side
#         _triggered_window_id = _bounds_candle_ts
#         _triggered_window_side = side

#         # Send webhook
#         send_webhook(trigger_time_iso, entry_val, side, "entry")

#         # Prepare alert message
#         msg = (
#             f"{friendly} | {status}: {fmt_price(entry_val)} "
#             f"| Live {fmt_price(trade_price)} "
#             f"| Trigger {ts_dt.astimezone(KOLKATA_TZ).strftime('%H:%M:%S')}"
#         )
#         alerts.append(msg)
#         alerts[:] = alerts[-50:]  # keep only last 50

#         # Save state only when trigger fires
#         save_state()
        

#     # === Trigger logic ===
#     if trade_price > upper_bound:
#         if _triggered_window_id != _bounds_candle_ts:
#             if _last_exit_lock == "unlock":
#                 process_trigger("buy", upper_bound, "LONG")


def try_trigger_on_trade(trade_price: float, trade_ts_ms: int):
    global alerts, _triggered_window_id, _triggered_window_side
    global status, EntryCount, LastSide, _last_exit_lock

    # Preconditions: skip if required values not set
    if not (
        is_valid_price(trade_price)
        and upper_bound is not None
        and lower_bound is not None
        and _bounds_candle_ts is not None
    ):
        return

    # Convert timestamps
    trigger_time = datetime.fromtimestamp(trade_ts_ms / 1000, tz=KOLKATA_TZ)
    trigger_time_iso = trigger_time.isoformat()
    ts_dt = datetime.fromtimestamp(trade_ts_ms / 1000, tz=STORE_TZ)

    # ---- tick size & buffer ----
    tick_size = 0.01   # for ETHUSDC, adjust if needed
    buffer_ticks = 100
    entry_threshold = upper_bound - buffer_ticks * tick_size

    def process_trigger(side: str, entry_val: float, friendly: str):
        """Handles sending webhook + logging + updating state"""
        nonlocal trigger_time_iso, ts_dt
        global alerts, _triggered_window_id, _triggered_window_side, LastSide

        LastSide = side
        _triggered_window_id = _bounds_candle_ts
        _triggered_window_side = side

        # Send webhook
        send_webhook(trigger_time_iso, entry_val, side, "entry")

        # Prepare alert message
        msg = (
            f"{friendly} | {status}: {fmt_price(entry_val)} "
            f"| Live {fmt_price(trade_price)} "
            f"| Trigger {ts_dt.astimezone(KOLKATA_TZ).strftime('%H:%M:%S')}"
        )
        alerts.append(msg)
        alerts[:] = alerts[-50:]  # keep only last 50

        save_state()

    # === Modified Trigger logic with buffer ===
    if trade_price >= entry_threshold:  
        if _triggered_window_id != _bounds_candle_ts:
            if _last_exit_lock == "unlock":
                process_trigger("buy", entry_threshold, "LONG (buffered)")


# ==========================
# WebSocket handlers
# ==========================

def on_message(ws, message):
    global candles, live_price, last_valid_price,status,lastpnl,_triggered_window_id,_last_exit_lock
    try:
        data = json.loads(message)
        stream = data.get("stream")
        payload = data.get("data")

        if stream and "kline" in stream:
            kline = payload["k"]
            ts_dt = datetime.fromtimestamp(kline["t"] / 1000, tz=STORE_TZ)  # UTC

            o, h, l, c = (
                float(kline["o"]),
                float(kline["h"]),
                float(kline["l"]),
                float(kline["c"]),
            )

            if is_valid_price(c):
                last_valid_price = c
                live_price = c

            new_candle_detected = candles.empty or candles.iloc[-1]["time"] != ts_dt

            if new_candle_detected:
                open_val = o if is_valid_price(o) else (last_valid_price if last_valid_price is not None else None)
                if open_val is None:
                    return

                high_val = h if is_valid_price(h) else open_val
                low_val = l if is_valid_price(l) else open_val
                close_val = c if is_valid_price(c) else open_val

                new_row = {
                    "time": ts_dt,  # UTC
                    "Open": open_val,
                    "High": max(high_val, open_val),
                    "Low": min(low_val, open_val),
                    "Close": close_val,
                }
                candles = pd.concat([candles, pd.DataFrame([new_row])], ignore_index=True)

                # ---------- OPTIONAL: Send webhook on new candle open (EXIT) ----------
                if status == "exit":
                    _last_exit_lock = "unlock"
                if status != "exit":
                    _last_exit_lock = "lock"
                    try:
                        send_webhook(ts_dt.astimezone(KOLKATA_TZ).strftime("%H:%M:%S"), open_val, "buy", "exit")
                        msg = f"EXIT | Price: {fmt_price(open_val)} | Time: {ts_dt.astimezone(KOLKATA_TZ).strftime('%H:%M:%S')}| PnL: {lastpnl}"
                        alerts.append(msg)
                        alerts[:] = alerts[-50:]
                    except Exception as e:
                        print("New candle webhook error:", e)
                
                save_state()

            else:
                idx = candles.index[-1]
                if is_valid_price(h):
                    candles.at[idx, "High"] = max(candles.at[idx, "High"], h)
                if is_valid_price(l):
                    candles.at[idx, "Low"] = min(candles.at[idx, "Low"], l)
                if is_valid_price(c):
                    candles.at[idx, "Close"] = c
                live_price = c
                last_valid_price = c

            if len(candles) > CANDLE_LIMIT:
                candles = candles.tail(CANDLE_LIMIT).reset_index(drop=True)

            if kline.get("x"):
                # Candle closed -> recompute bounds
                recompute_bounds_on_close()

        elif stream and "trade" in stream:
            trade_price_raw = payload.get("p")
            if not is_valid_price(trade_price_raw):
                return

            trade_price = float(trade_price_raw)
            live_price = trade_price
            last_valid_price = trade_price

            if not candles.empty:
                idx = candles.index[-1]
                # Update last candle tick by tick
                candles.at[idx, "Close"] = trade_price
                candles.at[idx, "High"] = max(candles.at[idx, "High"], trade_price)
                candles.at[idx, "Low"] = min(candles.at[idx, "Low"], trade_price)

            # Update fill status and try triggers
            update_fillcheck(trade_price)
            trade_ts_ms = int(payload.get("T") or payload.get("E") or time.time() * 1000)
            try_trigger_on_trade(trade_price, trade_ts_ms)

    except Exception as e:
        print("on_message error:", e)


def on_error(ws, error):
    print("WebSocket error:", error)


def on_close(ws, code, msg):
    print("WebSocket closed", code, msg)
    # Single delayed restart to avoid tight loops
    def _restart():
        time.sleep(2)
        run_ws()
    threading.Thread(target=_restart, daemon=True).start()


def on_open(ws):
    print("WebSocket connected")
    params = [f"{SYMBOL}@kline_{INTERVAL}", f"{SYMBOL}@trade"]
    msg = {"method": "SUBSCRIBE", "params": params, "id": 1}
    ws.send(json.dumps(msg))


def run_ws():
    url = "wss://fstream.binance.com/stream"
    ws = websocket.WebSocketApp(
        url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )
    # Ping to keep alive
    ws.run_forever(ping_interval=20, ping_timeout=10)


# ==========================
# Dash App
# ==========================
app = dash.Dash(__name__)
server = app.server  # expose Flask server for Render/Heroku
app.layout = html.Div(
    [
        html.H1(f"{SYMBOL.upper()} Live Prices & Last {CANDLE_LIMIT} Candles"),
        html.Div(
            [
                html.H2(id="live-price", style={"color": "black", "marginRight": "20px"}),
                html.H2(id="bal", style={"color": "black", "marginRight": "20px"}),
                html.Div(id="trade-stats", style={"display": "flex", "gap": "20px", "alignItems": "center"}),
            ],
            style={"display": "flex", "flexDirection": "row", "alignItems": "center"},
        ),
        html.Div(id="ohlc-values"),
        html.Div(id="bounds", style={"marginTop": "8px", "color": "#9ad"}),
        html.H3("Strategy Alerts"),
        html.Ul(id="strategy-alerts"),
        dcc.Interval(id="interval", interval=500, n_intervals=0),  # update every 0.5s
    ]
)


@app.callback(
    [
        Output("live-price", "children"),
        Output("bal", "children"),
        Output("trade-stats", "children"),
        Output("ohlc-values", "children"),
        Output("strategy-alerts", "children"),
        Output("bounds", "children"),
    ],
    [Input("interval", "n_intervals")],
)
def update_display(_):
    if len(candles) == 0:
        return (
            "Live Price: --",
            f"Balance: {fmt_price(initial_balance)}",
            [],
            [],
            [],
            "Bounds: waiting for enough closed candles...",
        )

    lp = f"Live Price: {fmt_price(live_price)}"
    bal = f"Balance: {fmt_price(initial_balance)}"

    stats_html = [
        html.Div(f"FillCheck: {fillcheck}"),
        html.Div(f"FillCount: {fillcount}"),
        html.Div(f"TotalTrades: {totaltradecount}"),
        html.Div(f"UnfilledPnL: {fmt_price(unfilledpnl)}"),
    ]

    ohlc_html = []
    for idx, row in candles.iterrows():
        ts_str = row["time"].astimezone(KOLKATA_TZ).strftime("%H:%M:%S")
        text = (
            f"{ts_str} → O:{fmt_price(row['Open'])}, H:{fmt_price(row['High'])}, "
            f"L:{fmt_price(row['Low'])}, C:{fmt_price(row['Close'])}"
        )
        ohlc_html.append(html.Div(text))

    alerts_html = [html.Li(a) for a in alerts[-10:]]

    if upper_bound is not None and lower_bound is not None and _bounds_candle_ts is not None:
        btxt = (
            f"Bounds[{LENGTH}] → Upper {fmt_price(upper_bound)}, Lower {fmt_price(lower_bound)} "
            f"(from candle {_bounds_candle_ts.astimezone(KOLKATA_TZ).strftime('%H:%M:%S')})"
        )
    else:
        btxt = "Bounds: waiting for enough closed candles..."

    return lp, bal, stats_html, ohlc_html, alerts_html, btxt


# Keep-alive ping thread
def keep_alive():
    """Send a ping to the server itself."""
    try:
        print(f"🔄 Pinging {PING_URL}")
        r = requests.get(PING_URL, timeout=10)
        print("✅ Ping response:", r.status_code)
    except Exception as e:
        print("⚠️ Keep-alive ping failed:", str(e))

@server.route('/ping', methods=['GET'])
def ping():
    keep_alive()
    return jsonify({"status": "alive"}), 200

# ==========================
# Main (Render compatible)
# ==========================
if __name__ == "__main__":
        # 1) Attempt to load saved state
    try:
        load_state()
    except Exception as e:
        print("Warning: load_state() failed:", e)

    # 2) If DB had no candles, fetch from Binance for initial seeding
    if candles.empty:
        try:
            fetch_initial_candles()
        except Exception as e:
            print("Initial fetch failed:", e)

    # 3) If we have enough candles, recompute bounds
    if len(candles) >= LENGTH and (upper_bound is None or lower_bound is None):
        recompute_bounds_on_close()

    # 4) Start websocket thread
    t = threading.Thread(target=run_ws, daemon=True)
    t.start()

    # 5) Optionally start a periodic save (safeguard) every N seconds to reduce risk of data loss
    def periodic_save_loop(interval_s=30):
        while True:
            try:
                save_state()
            except Exception as e:
                print("Periodic save error:", e)
            time.sleep(interval_s)

    saver_thread = threading.Thread(target=periodic_save_loop, args=(30,), daemon=True)
    saver_thread.start()

    port = int(os.environ.get("PORT", 10000))  # Render sets PORT
    app.run(host="0.0.0.0", port=port, debug=False)

#=======================================================================================================================================================
#=================================================                                       ===============================================================
#=================================================                                       ===============================================================
#=================================================                                       ===============================================================
#=======================================================================================================================================================


# =============================================================================================================================================
# ==================== 15 Min candle chart =====================================================================================================
# ===========================================================================================================================================

# import json
# import os
# import time
# import threading
# from datetime import datetime, timezone, timedelta

# import pandas as pd
# import requests
# import websocket
# import dash
# from dash import dcc, html
# from dash.dependencies import Input, Output
# from flask import Flask, request, jsonify
# from pymongo import MongoClient



# # ==========================
# # Config
# # ==========================
# SYMBOL = "ethusdc"       # keep lowercase for websocket streams
# INTERVAL = "15m"
# CANDLE_LIMIT = 10
# PING_URL = os.environ.get("PING_URL", "https://bot-reviver.onrender.com/ping")
# WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "mongodb+srv://gsr939988_db_user:ROYfY6z5kxUumq5i@tradingbot.lvnxj3a.mongodb.net/?retryWrites=true&w=majority&appName=TradingBot")

# # ====For ETHUSDC 15 Min Chart
# LENGTH = 1  # number of LAST CLOSED candles to compute bounds
# # ====For XRPUSDC 15 Min Chart
# # LENGTH = 2  # number of LAST CLOSED candles to compute bounds

# MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
# DB_NAME = "trading_bot_eth_without_repeat"
# COLLECTION_STATE = "bot_state"

# # ==========================
# # Timezones
# # ==========================
# # Store all candle timestamps in UTC. Convert to Kolkata only for display.
# KOLKATA_TZ = timezone(timedelta(hours=5, minutes=30))
# STORE_TZ = timezone.utc

# # ==========================
# # Globals
# # ==========================
# candles = pd.DataFrame(columns=["time", "Open", "High", "Low", "Close"])  # 'time' is UTC-aware
# live_price = None
# last_valid_price = None
# alerts = []

# initial_balance = 10.0
# entryprice = None
# running_pnl = 0.0

# upper_bound = None
# lower_bound = None
# _bounds_candle_ts = None  # UTC datetime of the candle used to compute bounds
# _triggered_window_id = None
# _triggered_window_side = None

# EntryCount = 0
# LastSide = None
# LastLastSide = "buy"
# status = None

# # Order filling data
# fillcheck = 0
# fillcount = 0
# totaltradecount = 0
# unfilledpnl = 0.0
# lastpnl = 0


# # ==========================
# # MongoDB Setup
# # ==========================
# mongo_client = MongoClient(MONGO_URI)
# db = mongo_client[DB_NAME]
# state_col = db[COLLECTION_STATE]


# def _serialize_candles(df: pd.DataFrame):
#     """Return list[dict] with 'time' as ISO strings for storage."""
#     if df is None or df.empty:
#         return []
#     tmp = df.copy()
#     def _iso_safe(x):
#         if pd.isna(x):
#             return None
#         if isinstance(x, str):
#             return x
#         # ensure timezone-aware
#         if getattr(x, "tzinfo", None) is None:
#             x = x.replace(tzinfo=STORE_TZ)
#         return x.isoformat()
#     tmp["time"] = tmp["time"].apply(_iso_safe)
#     return tmp.to_dict(orient="records")


# def _deserialize_candles(records):
#     """Turn stored records (with time iso) into DataFrame with timezone-aware datetimes (UTC)."""
#     if not records:
#         return pd.DataFrame(columns=["time", "Open", "High", "Low", "Close"])
#     df = pd.DataFrame(records)
#     if "time" in df.columns:
#         # parse ISO strings into timezone-aware UTC datetimes
#         df["time"] = pd.to_datetime(df["time"], utc=True)
#         # ensure tz is STORE_TZ (UTC)
#         df["time"] = df["time"].dt.tz_convert(STORE_TZ)
#     return df


# def save_state():
#     """Save global state + candles + alerts to MongoDB."""
#     global candles, live_price, last_valid_price, alerts
#     global initial_balance, entryprice, running_pnl
#     global upper_bound, lower_bound, _bounds_candle_ts, _triggered_window_id
#     global EntryCount, LastSide, LastLastSide, status
#     global fillcheck, fillcount, totaltradecount, unfilledpnl

#     try:
#         state = {
#             "_id": "bot_state",
#             "candles": _serialize_candles(candles),
#             "live_price": float(live_price) if is_valid_price(live_price) else None,
#             "last_valid_price": float(last_valid_price) if is_valid_price(last_valid_price) else None,
#             "alerts": alerts[-100:],  # keep last 100
#             "initial_balance": float(initial_balance),
#             "entryprice": float(entryprice) if is_valid_price(entryprice) else None,
#             "running_pnl": float(running_pnl),
#             "upper_bound": float(upper_bound) if is_valid_price(upper_bound) else None,
#             "lower_bound": float(lower_bound) if is_valid_price(lower_bound) else None,
#             "_bounds_candle_ts": _bounds_candle_ts.isoformat() if _bounds_candle_ts else None,
#             "_triggered_window_id": _triggered_window_id.isoformat() if _triggered_window_id else None,
#             "_triggered_window_side": _triggered_window_side,
#             "EntryCount": int(EntryCount),
#             "LastSide": LastSide,
#             "LastLastSide": LastLastSide,
#             "status": status,
#             "fillcheck": int(fillcheck),
#             "fillcount": int(fillcount),
#             "totaltradecount": int(totaltradecount),
#             "unfilledpnl": float(unfilledpnl)
#         }
#         state_col.replace_one({"_id": "bot_state"}, state, upsert=True)
#     except Exception as e:
#         print("⚠️ Error saving state to MongoDB:", e)


# def load_state():
#     """Restore global state from MongoDB."""
#     global candles, live_price, last_valid_price, alerts
#     global initial_balance, entryprice, running_pnl
#     global upper_bound, lower_bound, _bounds_candle_ts, _triggered_window_id
#     global EntryCount, LastSide, LastLastSide, status
#     global fillcheck, fillcount, totaltradecount, unfilledpnl

#     try:
#         doc = state_col.find_one({"_id": "bot_state"})
#         if not doc:
#             print("ℹ️ No saved state found in DB, fresh start.")
#             return

#         candles = _deserialize_candles(doc.get("candles", []))
#         live_price = doc.get("live_price")
#         last_valid_price = doc.get("last_valid_price")
#         alerts = doc.get("alerts", []) or []

#         initial_balance = doc.get("initial_balance", 10.0)
#         entryprice = doc.get("entryprice")
#         running_pnl = doc.get("running_pnl", 0.0)

#         upper_bound = doc.get("upper_bound")
#         lower_bound = doc.get("lower_bound")

#         _bounds_candle_ts = (
#             pd.to_datetime(doc.get("_bounds_candle_ts")).tz_convert(STORE_TZ)
#             if doc.get("_bounds_candle_ts") else None
#         )
#         _triggered_window_id = (
#             pd.to_datetime(doc.get("_triggered_window_id")).tz_convert(STORE_TZ)
#             if doc.get("_triggered_window_id") else None
#         )
#         _triggered_window_side = doc.get("_triggered_window_side","buy")

#         EntryCount = doc.get("EntryCount", 0)
#         LastSide = doc.get("LastSide")
#         LastLastSide = doc.get("LastLastSide", "buy")
#         status = doc.get("status")

#         fillcheck = doc.get("fillcheck", 0)
#         fillcount = doc.get("fillcount", 0)
#         totaltradecount = doc.get("totaltradecount", 0)
#         unfilledpnl = doc.get("unfilledpnl", 0.0)

#         print("✅ State restored from MongoDB")
#     except Exception as e:
#         print("⚠️ Error loading state from MongoDB:", e)


# # ==========================
# # Utilities
# # ==========================

# def is_valid_price(x):
#     try:
#         return float(x) > 0
#     except Exception:
#         return False


# def fmt_price(p):
#     if p is None:
#         return "--"
#     ap = abs(p)
#     if ap >= 100:
#         return f"{p:.2f}"
#     elif ap >= 1:
#         return f"{p:.4f}"
#     else:
#         return f"{p:.8f}"


# # ==========================
# # Helpers
# # ==========================

# def fetch_initial_candles():
#     """Fetch last CANDLE_LIMIT klines from Binance Futures API and seed the DF.
#     Ensures timestamps are stored in UTC (STORE_TZ)."""
#     global candles, live_price, last_valid_price
#     url = (
#         f"https://fapi.binance.com/fapi/v1/klines?symbol={SYMBOL.upper()}&interval={INTERVAL}&limit={CANDLE_LIMIT}"
#     )
#     r = requests.get(url, timeout=10)
#     r.raise_for_status()
#     data = r.json()

#     rows = []
#     for k in data:
#         rows.append(
#             {
#                 "time": datetime.fromtimestamp(k[0] / 1000, tz=STORE_TZ),  # UTC
#                 "Open": float(k[1]),
#                 "High": float(k[2]),
#                 "Low": float(k[3]),
#                 "Close": float(k[4]),
#             }
#         )
#     candles = pd.DataFrame(rows)
#     if not candles.empty:
#         live_price = float(candles["Close"].iloc[-1])
#         last_valid_price = live_price if is_valid_price(live_price) else None
#     save_state()


# def calculate_pnl(entry_price: float, closing_price: float, side: str) -> float:
#     quantity = 0.006  # fixed quantity
#     if side == "buy":
#         return (closing_price - entry_price) * quantity
#     elif side == "sell":
#         return (entry_price - closing_price) * quantity
#     else:
#         return 0.0


# def send_webhook(trigger_time_iso: str, entry_price_in: float, side: str, status_fun: str):
#     global EntryCount, LastSide, status, entryprice, running_pnl, initial_balance, lastpnl
#     global fillcheck, totaltradecount, unfilledpnl, LastLastSide

#     secret = "gajraj09"
#     quantity = 0.006

#     status = status_fun
#     pnl = 0.0

#     if status == "exit":
#         if entryprice is None:
#             print("[WEBHOOK] Exit requested but no stored entryprice; ignoring pnl update.")
#         else:
#             pnl = calculate_pnl(entryprice, entry_price_in, LastLastSide)
#             if fillcheck == 0:
#                 initial_balance += pnl
                
#             running_pnl += pnl
#             lastpnl = pnl
#         entryprice = None
#         if fillcheck == 1:
#             unfilledpnl += pnl
#         fillcheck = 0
#     else:  # status == "entry"
#         if entryprice is not None:
#             # Close any previous implicit position first for accounting
#             pnl = calculate_pnl(entryprice, entry_price_in, LastLastSide)
#             if fillcheck == 0:
#                 initial_balance += pnl
#             running_pnl += pnl
#             lastpnl = pnl
#         else:
#             print(f"[ENTRY] Opening first position at {fmt_price(entry_price_in)}")
#         entryprice = entry_price_in
#         totaltradecount += 1
#         fillcheck = 1
#     LastLastSide = LastSide


#     try:
#         payload = {
#             "symbol": SYMBOL.upper(),
#             "side": side,
#             "quantity": quantity,
#             "price": entry_price_in,
#             "status": status,
#             "secret": secret,
#         }
#         requests.post(WEBHOOK_URL, json=payload, timeout=5)
#         print("Sent payload:", payload)
#     except Exception as e:
#         print("Webhook error:", e)
#     save_state()


# def recompute_bounds_on_close():
#     global upper_bound, lower_bound, _bounds_candle_ts, _triggered_window_id
#     if len(candles) < LENGTH:
#         upper_bound = None
#         lower_bound = None
#         _bounds_candle_ts = None
#         _triggered_window_id = None
#         save_state()
#         return

#     window = candles.tail(LENGTH)
#     highs = window["High"][window["High"] > 0]
#     lows = window["Low"][window["Low"] > 0]
#     if highs.empty or lows.empty:
#         return

#     upper_bound = float(highs.max())
#     lower_bound = float(lows.min())
#     _bounds_candle_ts = window["time"].iloc[-1]  # UTC
#     _triggered_window_id = None
#     save_state()


# # ==========================
# # Fillcheck update on every tick
# # ==========================

# def update_fillcheck(trade_price: float):
#     global fillcheck, fillcount, entryprice, LastSide, status, totaltradecount, unfilledpnl

#     if entryprice is None or fillcheck == 0:
#         return
#     was_fill = False
#     if status == "entry":
#         if LastSide == "buy" and trade_price <= entryprice:  # <= to allow exact touch
#             fillcheck = 0
#             fillcount += 1
#             was_fill = True
#         elif LastSide == "sell" and trade_price >= entryprice:
#             fillcheck = 0
#             fillcount += 1
#             was_fill = True
#     elif status == "exit":
#         # Exit fill handling can be more sophisticated if you post exit orders
#         fillcheck = 0
#         was_fill = True
#     if was_fill:
#         save_state()



# # ==========================
# # Trigger logic
# # ==========================

# # def try_trigger_on_trade(trade_price: float, trade_ts_ms: int):
# #     global alerts, _triggered_window_id,_triggered_window_side, status, EntryCount, LastSide

# #     if not (
# #         is_valid_price(trade_price)
# #         and upper_bound is not None
# #         and lower_bound is not None
# #         and _bounds_candle_ts is not None
# #     ):
# #         return
# #     if _triggered_window_id == _bounds_candle_ts:
# #         return
    

# #     trigger_time = datetime.fromtimestamp(trade_ts_ms / 1000, tz=KOLKATA_TZ)
# #     trigger_time_iso = trigger_time.isoformat()
# #     ts_dt = datetime.fromtimestamp(trade_ts_ms / 1000, tz=STORE_TZ)

# #     def _process_side(side_name, entry_val, friendly):
# #         nonlocal trigger_time_iso
# #         global EntryCount, LastSide, alerts, _triggered_window_id
# #         LastSide = side_name

# #         send_webhook(trigger_time_iso, entry_val, side_name, "entry")
# #         msg = f"{friendly} | {status}: {fmt_price(entry_val)} | Live {fmt_price(trade_price)} | Trigger {ts_dt.astimezone(KOLKATA_TZ).strftime('%H:%M:%S')}"
# #         alerts.append(msg)
# #         alerts[:] = alerts[-50:]
# #         _triggered_window_id = _bounds_candle_ts
# #         # _triggered_window_side = side_name
# #         save_state()

# #     if trade_price > upper_bound:
# #         _process_side("buy", upper_bound, "LONG")
# #     elif trade_price < lower_bound:
# #         _process_side("sell", lower_bound, "SHORT")

# def try_trigger_on_trade(trade_price: float, trade_ts_ms: int):
#     global alerts, _triggered_window_id, _triggered_window_side
#     global status, EntryCount, LastSide

#     # Preconditions: skip if required values not set
#     if not (
#         is_valid_price(trade_price)
#         and upper_bound is not None
#         and lower_bound is not None
#         and _bounds_candle_ts is not None
#     ):
#         return

#     # Convert timestamps
#     trigger_time = datetime.fromtimestamp(trade_ts_ms / 1000, tz=KOLKATA_TZ)
#     trigger_time_iso = trigger_time.isoformat()
#     ts_dt = datetime.fromtimestamp(trade_ts_ms / 1000, tz=STORE_TZ)

#     def process_trigger(side: str, entry_val: float, friendly: str):
#         """Handles sending webhook + logging + updating state"""
#         nonlocal trigger_time_iso, ts_dt
#         global alerts, _triggered_window_id, _triggered_window_side, LastSide

#         LastSide = side
#         _triggered_window_id = _bounds_candle_ts
#         _triggered_window_side = side

#         # Send webhook
#         send_webhook(trigger_time_iso, entry_val, side, "entry")

#         # Prepare alert message
#         msg = (
#             f"{friendly} | {status}: {fmt_price(entry_val)} "
#             f"| Live {fmt_price(trade_price)} "
#             f"| Trigger {ts_dt.astimezone(KOLKATA_TZ).strftime('%H:%M:%S')}"
#         )
#         alerts.append(msg)
#         alerts[:] = alerts[-50:]  # keep only last 50

#         # Save state only when trigger fires
#         save_state()
        

#     # === Trigger logic ===
#     if trade_price > upper_bound:
#         if not (_triggered_window_id == _bounds_candle_ts and status == "entry"):
#             if _triggered_window_side == "buy" and status == "exit":
#                 return
#             process_trigger("buy", upper_bound, "LONG")

# #     # Preconditions: skip if required values not set
# #     if not (
# #         is_valid_price(trade_price)
# #         and upper_bound is not None
# #         and lower_bound is not None
# #         and _bounds_candle_ts is not None
# #     ):
# #         return

# #     # Convert timestamps
# #     trigger_time = datetime.fromtimestamp(trade_ts_ms / 1000, tz=KOLKATA_TZ)
# #     trigger_time_iso = trigger_time.isoformat()
# #     ts_dt = datetime.fromtimestamp(trade_ts_ms / 1000, tz=STORE_TZ)

# #     def process_trigger(side: str, entry_val: float, friendly: str):
# #         """Handles sending webhook + logging + updating state"""
# #         nonlocal trigger_time_iso, ts_dt
# #         global alerts, _triggered_window_id, _triggered_window_side, LastSide

# #         LastSide = side
# #         _triggered_window_id = _bounds_candle_ts
# #         _triggered_window_side = side

# #         # Send webhook
# #         send_webhook(trigger_time_iso, entry_val, side, "entry")

# #         # Prepare alert message
# #         msg = (
# #             f"{friendly} | {status}: {fmt_price(entry_val)} "
# #             f"| Live {fmt_price(trade_price)} "
# #             f"| Trigger {ts_dt.astimezone(KOLKATA_TZ).strftime('%H:%M:%S')}"
# #         )
# #         alerts.append(msg)
# #         alerts[:] = alerts[-50:]  # keep only last 50

# #         # Save state only when trigger fires
# #         save_state()
        

# #     # === Trigger logic ===
# #     if trade_price > upper_bound:
# #         if not (_triggered_window_id == _bounds_candle_ts and status == "entry"):
# #             if _triggered_window_side == "buy" and status == "exit":
# #                 return
# #             process_trigger("buy", upper_bound, "LONG")

# #     elif trade_price < lower_bound:
# #         if not (_triggered_window_id == _bounds_candle_ts and status == "entry"):
# #             if _triggered_window_side == "sell" and status == "exit":
# #                 return
# #             process_trigger("sell", lower_bound, "SHORT")

# # ==========================
# # WebSocket handlers
# # ==========================

# def on_message(ws, message):
#     global candles, live_price, last_valid_price,status,lastpnl,_triggered_window_id
#     try:
#         data = json.loads(message)
#         stream = data.get("stream")
#         payload = data.get("data")

#         if stream and "kline" in stream:
#             kline = payload["k"]
#             ts_dt = datetime.fromtimestamp(kline["t"] / 1000, tz=STORE_TZ)  # UTC

#             o, h, l, c = (
#                 float(kline["o"]),
#                 float(kline["h"]),
#                 float(kline["l"]),
#                 float(kline["c"]),
#             )

#             if is_valid_price(c):
#                 last_valid_price = c
#                 live_price = c

#             new_candle_detected = candles.empty or candles.iloc[-1]["time"] != ts_dt

#             if new_candle_detected:
#                 open_val = o if is_valid_price(o) else (last_valid_price if last_valid_price is not None else None)
#                 if open_val is None:
#                     return

#                 high_val = h if is_valid_price(h) else open_val
#                 low_val = l if is_valid_price(l) else open_val
#                 close_val = c if is_valid_price(c) else open_val

#                 new_row = {
#                     "time": ts_dt,  # UTC
#                     "Open": open_val,
#                     "High": max(high_val, open_val),
#                     "Low": min(low_val, open_val),
#                     "Close": close_val,
#                 }
#                 candles = pd.concat([candles, pd.DataFrame([new_row])], ignore_index=True)

#                 # ---------- OPTIONAL: Send webhook on new candle open (EXIT) ----------
#                 if status != "exit":
#                     try:
#                         send_webhook(ts_dt.astimezone(KOLKATA_TZ).strftime("%H:%M:%S"), open_val, "buy", "exit")
#                         msg = f"EXIT | Price: {fmt_price(open_val)} | Time: {ts_dt.astimezone(KOLKATA_TZ).strftime('%H:%M:%S')}| PnL: {lastpnl}"
#                         alerts.append(msg)
#                         alerts[:] = alerts[-50:]
#                     except Exception as e:
#                         print("New candle webhook error:", e)
#                     save_state()

#             else:
#                 idx = candles.index[-1]
#                 if is_valid_price(h):
#                     candles.at[idx, "High"] = max(candles.at[idx, "High"], h)
#                 if is_valid_price(l):
#                     candles.at[idx, "Low"] = min(candles.at[idx, "Low"], l)
#                 if is_valid_price(c):
#                     candles.at[idx, "Close"] = c
#                 live_price = c
#                 last_valid_price = c

#             if len(candles) > CANDLE_LIMIT:
#                 candles = candles.tail(CANDLE_LIMIT).reset_index(drop=True)

#             if kline.get("x"):
#                 # Candle closed -> recompute bounds
#                 recompute_bounds_on_close()

#         elif stream and "trade" in stream:
#             trade_price_raw = payload.get("p")
#             if not is_valid_price(trade_price_raw):
#                 return

#             trade_price = float(trade_price_raw)
#             live_price = trade_price
#             last_valid_price = trade_price

#             if not candles.empty:
#                 idx = candles.index[-1]
#                 # Update last candle tick by tick
#                 candles.at[idx, "Close"] = trade_price
#                 candles.at[idx, "High"] = max(candles.at[idx, "High"], trade_price)
#                 candles.at[idx, "Low"] = min(candles.at[idx, "Low"], trade_price)

#             # Update fill status and try triggers
#             update_fillcheck(trade_price)
#             trade_ts_ms = int(payload.get("T") or payload.get("E") or time.time() * 1000)
#             try_trigger_on_trade(trade_price, trade_ts_ms)

#     except Exception as e:
#         print("on_message error:", e)


# def on_error(ws, error):
#     print("WebSocket error:", error)


# def on_close(ws, code, msg):
#     print("WebSocket closed", code, msg)
#     # Single delayed restart to avoid tight loops
#     def _restart():
#         time.sleep(2)
#         run_ws()
#     threading.Thread(target=_restart, daemon=True).start()


# def on_open(ws):
#     print("WebSocket connected")
#     params = [f"{SYMBOL}@kline_{INTERVAL}", f"{SYMBOL}@trade"]
#     msg = {"method": "SUBSCRIBE", "params": params, "id": 1}
#     ws.send(json.dumps(msg))


# def run_ws():
#     url = "wss://fstream.binance.com/stream"
#     ws = websocket.WebSocketApp(
#         url,
#         on_open=on_open,
#         on_message=on_message,
#         on_error=on_error,
#         on_close=on_close,
#     )
#     # Ping to keep alive
#     ws.run_forever(ping_interval=20, ping_timeout=10)


# # ==========================
# # Dash App
# # ==========================
# app = dash.Dash(__name__)
# server = app.server  # expose Flask server for Render/Heroku
# app.layout = html.Div(
#     [
#         html.H1(f"{SYMBOL.upper()} Live Prices & Last {CANDLE_LIMIT} Candles"),
#         html.Div(
#             [
#                 html.H2(id="live-price", style={"color": "black", "marginRight": "20px"}),
#                 html.H2(id="bal", style={"color": "black", "marginRight": "20px"}),
#                 html.Div(id="trade-stats", style={"display": "flex", "gap": "20px", "alignItems": "center"}),
#             ],
#             style={"display": "flex", "flexDirection": "row", "alignItems": "center"},
#         ),
#         html.Div(id="ohlc-values"),
#         html.Div(id="bounds", style={"marginTop": "8px", "color": "#9ad"}),
#         html.H3("Strategy Alerts"),
#         html.Ul(id="strategy-alerts"),
#         dcc.Interval(id="interval", interval=500, n_intervals=0),  # update every 0.5s
#     ]
# )


# @app.callback(
#     [
#         Output("live-price", "children"),
#         Output("bal", "children"),
#         Output("trade-stats", "children"),
#         Output("ohlc-values", "children"),
#         Output("strategy-alerts", "children"),
#         Output("bounds", "children"),
#     ],
#     [Input("interval", "n_intervals")],
# )
# def update_display(_):
#     if len(candles) == 0:
#         return (
#             "Live Price: --",
#             f"Balance: {fmt_price(initial_balance)}",
#             [],
#             [],
#             [],
#             "Bounds: waiting for enough closed candles...",
#         )

#     lp = f"Live Price: {fmt_price(live_price)}"
#     bal = f"Balance: {fmt_price(initial_balance)}"

#     stats_html = [
#         html.Div(f"FillCheck: {fillcheck}"),
#         html.Div(f"FillCount: {fillcount}"),
#         html.Div(f"TotalTrades: {totaltradecount}"),
#         html.Div(f"UnfilledPnL: {fmt_price(unfilledpnl)}"),
#     ]

#     ohlc_html = []
#     for idx, row in candles.iterrows():
#         ts_str = row["time"].astimezone(KOLKATA_TZ).strftime("%H:%M:%S")
#         text = (
#             f"{ts_str} → O:{fmt_price(row['Open'])}, H:{fmt_price(row['High'])}, "
#             f"L:{fmt_price(row['Low'])}, C:{fmt_price(row['Close'])}"
#         )
#         ohlc_html.append(html.Div(text))

#     alerts_html = [html.Li(a) for a in alerts[-10:]]

#     if upper_bound is not None and lower_bound is not None and _bounds_candle_ts is not None:
#         btxt = (
#             f"Bounds[{LENGTH}] → Upper {fmt_price(upper_bound)}, Lower {fmt_price(lower_bound)} "
#             f"(from candle {_bounds_candle_ts.astimezone(KOLKATA_TZ).strftime('%H:%M:%S')})"
#         )
#     else:
#         btxt = "Bounds: waiting for enough closed candles..."

#     return lp, bal, stats_html, ohlc_html, alerts_html, btxt


# # Keep-alive ping thread
# def keep_alive():
#     """Send a ping to the server itself."""
#     try:
#         print(f"🔄 Pinging {PING_URL}")
#         r = requests.get(PING_URL, timeout=10)
#         print("✅ Ping response:", r.status_code)
#     except Exception as e:
#         print("⚠️ Keep-alive ping failed:", str(e))

# @server.route('/ping', methods=['GET'])
# def ping():
#     keep_alive()
#     return jsonify({"status": "alive"}), 200

# # ==========================
# # Main (Render compatible)
# # ==========================
# if __name__ == "__main__":
#         # 1) Attempt to load saved state
#     try:
#         load_state()
#     except Exception as e:
#         print("Warning: load_state() failed:", e)

#     # 2) If DB had no candles, fetch from Binance for initial seeding
#     if candles.empty:
#         try:
#             fetch_initial_candles()
#         except Exception as e:
#             print("Initial fetch failed:", e)

#     # 3) If we have enough candles, recompute bounds
#     if len(candles) >= LENGTH and (upper_bound is None or lower_bound is None):
#         recompute_bounds_on_close()

#     # 4) Start websocket thread
#     t = threading.Thread(target=run_ws, daemon=True)
#     t.start()

#     # 5) Optionally start a periodic save (safeguard) every N seconds to reduce risk of data loss
#     def periodic_save_loop(interval_s=30):
#         while True:
#             try:
#                 save_state()
#             except Exception as e:
#                 print("Periodic save error:", e)
#             time.sleep(interval_s)

#     saver_thread = threading.Thread(target=periodic_save_loop, args=(30,), daemon=True)
#     saver_thread.start()
#     # try:
#     #     fetch_initial_candles()
#     # except Exception as e:
#     #     print("Initial fetch failed:", e)

#     # if len(candles) >= LENGTH:
#     #     recompute_bounds_on_close()

#     # t = threading.Thread(target=run_ws, daemon=True)
#     # t.start()

#     port = int(os.environ.get("PORT", 10000))  # Render sets PORT
#     app.run(host="0.0.0.0", port=port, debug=False)


# ===========================================================================================================================================
# ========================================== 10 Min Candle derived by 5 Min Candle ==========================================================
# ===========================================================================================================================================

# import json
# import os
# import time
# import threading
# from datetime import datetime, timezone, timedelta

# import pandas as pd
# import requests
# import websocket
# import dash
# from dash import dcc, html
# from dash.dependencies import Input, Output
# from flask import Flask, request, jsonify

# # ==========================
# # Config
# # ==========================
# SYMBOL = "xrpusdc"  # lowercase for websocket streams
# INTERVAL = "5m"     # 5-min candles (source)
# CANDLE_LIMIT = 10   # keep enough 5m candles so we can build 10m candles
# PING_URL = os.environ.get("PING_URL", "https://bot-reviver.onrender.com/ping")
# WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://www.example.com/webhook")
# LENGTH = 1  # last closed 10-min candles for bounds

# # ==========================
# # Timezone (UTC+5:30 Kolkata)
# # ==========================
# KOLKATA_TZ = timezone(timedelta(hours=5, minutes=30))

# # ==========================
# # Globals (state)
# # ==========================
# candles = pd.DataFrame(columns=["time", "Open", "High", "Low", "Close"])  # 5m candles
# ten_min_candles = pd.DataFrame(columns=["time", "Open", "High", "Low", "Close"])  # derived even 10m
# live_price = None
# last_valid_price = None
# alerts = []

# # Strategy / trade state (kept from your other code)
# initial_balance = 50.0
# entryprice = None
# panl = 0.0

# upper_bound = None
# lower_bound = None
# _bounds_candle_ts = None  # timestamp (time) of the 10-min candle used for bounds
# _triggered_window_id = None

# EntryCount = 0
# current_side = None
# status = None

# # Order filling data
# fillcheck = 0
# fillcount = 0
# totaltradecount = 0
# unfilledpnl = 0.0

# # ==========================
# # Utilities
# # ==========================
# def is_valid_price(x):
#     try:
#         return float(x) > 0
#     except Exception:
#         return False

# def fmt_price(p):
#     if p is None:
#         return "--"
#     ap = abs(p)
#     if ap >= 100:
#         return f"{p:.2f}"
#     elif ap >= 1:
#         return f"{p:.4f}"
#     else:
#         return f"{p:.8f}"

# # ==========================
# # Helpers: initial fetch
# # ==========================
# def fetch_initial_candles():
#     """Fetch recent 5m candles from Binance and populate `candles` DataFrame."""
#     global candles, live_price, last_valid_price
#     url = f"https://fapi.binance.com/fapi/v1/klines?symbol={SYMBOL.upper()}&interval={INTERVAL}&limit={CANDLE_LIMIT}"
#     r = requests.get(url, timeout=10)
#     r.raise_for_status()
#     data = r.json()

#     rows = []
#     for k in data:
#         rows.append({
#             "time": datetime.fromtimestamp(k[0] / 1000, tz=KOLKATA_TZ),
#             "Open": float(k[1]),
#             "High": float(k[2]),
#             "Low": float(k[3]),
#             "Close": float(k[4]),
#         })
#     candles = pd.DataFrame(rows)
#     if not candles.empty:
#         live_price = float(candles["Close"].iloc[-1])
#         last_valid_price = live_price if is_valid_price(live_price) else None

# # ==========================
# # Build even 10-min candles from 5-min candles
# # - only pairs where the first candle minute % 10 == 0
# # - timestamp uses first candle's time
# # - update ten_min_candles globally
# # ==========================
# _prev_ten_len = 0  # helper to detect new 10m closed candle

# def update_10min_candles():
#     global ten_min_candles, candles, _prev_ten_len
#     if len(candles) < 2:
#         return

#     combined_rows = []
#     i = 0
#     # iterate sequentially and combine pairs when the first candle minute % 10 == 0
#     while i < len(candles) - 1:
#         first = candles.iloc[i]
#         second = candles.iloc[i + 1]

#         if first["time"].minute % 10 == 0:
#             combined = {
#                 "time": first["time"],
#                 "Open": first["Open"],
#                 "High": max(first["High"], second["High"]),
#                 "Low": min(first["Low"], second["Low"]),
#                 "Close": second["Close"],
#             }
#             combined_rows.append(combined)
#             i += 2
#         else:
#             i += 1

#     new_df = pd.DataFrame(combined_rows)
#     ten_changed = len(new_df) != len(ten_min_candles)
#     ten_min_candles = new_df

#     if ten_changed:
#         recompute_bounds_on_close()

# # ==========================
# # Recompute bounds on latest closed 10-min candles
# # Use last LENGTH 10-min candles
# # ==========================
# def recompute_bounds_on_close():
#     global upper_bound, lower_bound, _bounds_candle_ts, _triggered_window_id
#     if len(ten_min_candles) < LENGTH:
#         upper_bound = None
#         lower_bound = None
#         _bounds_candle_ts = None
#         _triggered_window_id = None
#         return

#     window = ten_min_candles.tail(LENGTH)
#     highs = window["High"][window["High"] > 0]
#     lows = window["Low"][window["Low"] > 0]
#     if highs.empty or lows.empty:
#         upper_bound = None
#         lower_bound = None
#         _bounds_candle_ts = None
#         _triggered_window_id = None
#         return

#     upper_bound = float(highs.max())
#     lower_bound = float(lows.min())
#     _bounds_candle_ts = window["time"].iloc[-1]
#     _triggered_window_id = None
#     print(f"[BOUNDS] Recomputed from last {LENGTH} 10-min candles -> upper={upper_bound}, lower={lower_bound} (ts={_bounds_candle_ts.strftime('%H:%M:%S')})")

# # ==========================
# # PnL and webhook sending (kept logic) — FIXED to consider side
# # ==========================
# def calculate_pnl(entry_price: float, closing_price: float, side: str) -> float:
#     """
#     Calculate PnL based on side.
#     - for buy (long): pnl = (closing - entry) * factor
#     - for sell (short): pnl = (entry - closing) * factor
#     """
#     factor = 1.8
#     if side is None:
#         return (closing_price - entry_price) * factor
#     s = side.lower()
#     if s in ("buy", "long"):
#         return (closing_price - entry_price) * factor
#     elif s in ("sell", "short"):
#         return (entry_price - closing_price) * factor
#     else:
#         return (closing_price - entry_price) * factor

# def send_webhook(trigger_time_iso: str, entry_price_in: float, new_side: str, prev_side: str):
#     """
#     new_side: the side we want to open now (buy/sell)
#     prev_side: the side of existing open position (can be None)
#     """
#     global EntryCount, current_side, status, entryprice, panl, initial_balance, fillcheck, totaltradecount, unfilledpnl

#     secret = os.environ.get("WEBHOOK_SECRET", "gajraj09")
#     quantity = 10
#     status = "entry"

#     pnl = 0.0

#     # If there was an existing entryprice, compute its PnL using the previous side
#     if entryprice is not None and prev_side is not None:
#         try:
#             pnl = calculate_pnl(entryprice, entry_price_in, prev_side)
#             if fillcheck == 0:
#                 initial_balance += pnl
#             else:
#                 unfilledpnl += pnl
#             print(f"[PNL] Closed previous {prev_side} at {fmt_price(entry_price_in)} -> pnl {fmt_price(pnl)} | balance now {fmt_price(initial_balance)}")
#         except Exception as e:
#             print("Error computing pnl:", e)
#     else:
#         print(f"[ENTRY] Opening first position at {fmt_price(entry_price_in)}")

#     # set new entry
#     entryprice = entry_price_in
#     totaltradecount += 1
#     fillcheck = 1

#     print(f"[WEBHOOK] {trigger_time_iso} | Opening {new_side} | Entry/Price: {entry_price_in} | symbol: {SYMBOL.upper()} | status: {status} | QUANTITY: {quantity}")
#     try:
#         payload = {
#             "symbol": SYMBOL.upper(),
#             "side": new_side,
#             "quantity": quantity,
#             "price": entry_price_in,
#             "status": status,
#             "secret": secret
#         }
#         # send webhook (best-effort)
#         requests.post(WEBHOOK_URL, json=payload, timeout=5)
#         print("Sent payload:", payload)
#     except Exception as e:
#         print("Webhook error:", e)

# # ==========================
# # Fillcheck update on every tick (trade)
# # ==========================
# def update_fillcheck(trade_price: float):
#     global fillcheck, fillcount, entryprice, current_side, status, totaltradecount, unfilledpnl

#     if entryprice is None or fillcheck == 0:
#         return

#     # For buy entry we expect price to fall to be filled (trade_price < entryprice)
#     if status == "entry":
#         if current_side == "buy" and trade_price < entryprice:
#             fillcheck = 0
#             fillcount += 1
#             print(f"[FILL] Buy entry filled at {fmt_price(entryprice)} | Total fills={fillcount}/{totaltradecount}")
#         elif current_side == "sell" and trade_price > entryprice:
#             fillcheck = 0
#             fillcount += 1
#             print(f"[FILL] Sell entry filled at {fmt_price(entryprice)} | Total fills={fillcount}/{totaltradecount}")
#     elif status == "exit":
#         fillcheck = 0
#         print(f"[FILL] Exit completed at {fmt_price(trade_price)}")

# # ==========================
# # Trigger logic on trade tick (uses 10-min bounds)
# # ==========================
# def try_trigger_on_trade(trade_price: float, trade_ts_ms: int):
#     global alerts, _triggered_window_id, status, EntryCount, current_side, upper_bound, lower_bound, _bounds_candle_ts

#     if not (is_valid_price(trade_price) and upper_bound is not None and lower_bound is not None and _bounds_candle_ts is not None):
#         return
#     # prevent retriggering on same 10-min bounds candle
#     if _triggered_window_id == _bounds_candle_ts:
#         return

#     trigger_time = datetime.fromtimestamp(trade_ts_ms / 1000, tz=KOLKATA_TZ)
#     trigger_time_iso = trigger_time.isoformat()

#     def _process_side(side_name, entry_val, friendly):
#         nonlocal trigger_time_iso
#         global EntryCount, current_side, alerts, _triggered_window_id
#         prev_side = current_side

#         if prev_side != side_name:
#             send_webhook(trigger_time_iso, entry_val, side_name, prev_side)
#             current_side = side_name

#             msg = f"{friendly} | {status}: {fmt_price(entry_val)} | Live {fmt_price(trade_price)} | Trigger {trigger_time_iso}"
#             print("[ALERT]", msg)
#             alerts.append(msg)
#             # cap alerts history for memory
#             alerts[:] = alerts[-200:]
#             _triggered_window_id = _bounds_candle_ts

#     if trade_price >= upper_bound:
#         _process_side("buy", upper_bound, "LONG breakout Buy")
#     elif trade_price <= lower_bound:
#         _process_side("sell", lower_bound, "SHORT breakout Sell")

# # ==========================
# # WebSocket handlers (kline_5m + trade streams)
# # ==========================
# def on_message(ws, message):
#     global candles, live_price, last_valid_price
#     try:
#         data = json.loads(message)
#         stream = data.get("stream")
#         payload = data.get("data")

#         # kline (5m)
#         if stream and "kline" in stream:
#             kline = payload["k"]
#             ts_dt = datetime.fromtimestamp(kline["t"] / 1000, tz=KOLKATA_TZ)
#             o, h, l, c = float(kline["o"]), float(kline["h"]), float(kline["l"]), float(kline["c"])

#             if is_valid_price(c):
#                 last_valid_price = c
#                 live_price = c

#             # Add or update last 5m candle with fallbacks
#             if candles.empty or candles.iloc[-1]["time"] != ts_dt:
#                 open_val = o if is_valid_price(o) else (last_valid_price if last_valid_price is not None else None)
#                 if open_val is None:
#                     return
#                 high_val = h if is_valid_price(h) else open_val
#                 low_val = l if is_valid_price(l) else open_val
#                 close_val = c if is_valid_price(c) else open_val

#                 new_row = {
#                     "time": ts_dt,
#                     "Open": open_val,
#                     "High": max(high_val, open_val),
#                     "Low": min(low_val, open_val),
#                     "Close": close_val
#                 }
#                 candles_loc = pd.DataFrame([new_row])
#                 candles = pd.concat([candles, candles_loc], ignore_index=True)
#             else:
#                 idx = candles.index[-1]
#                 if is_valid_price(h):
#                     candles.at[idx, "High"] = max(candles.at[idx, "High"], h)
#                 if is_valid_price(l):
#                     candles.at[idx, "Low"] = min(candles.at[idx, "Low"], l)
#                 if is_valid_price(c):
#                     candles.at[idx, "Close"] = c
#                     live_price = c
#                     last_valid_price = c

#             # keep last CANDLE_LIMIT 5m candles
#             if len(candles) > CANDLE_LIMIT:
#                 candles = candles.tail(CANDLE_LIMIT).reset_index(drop=True)

#             # when kline finished (closed), update derived 10-min candles
#             if kline.get("x"):
#                 update_10min_candles()

#         # trade stream
#         elif stream and "trade" in stream:
#             trade_price_raw = payload.get("p")
#             if not is_valid_price(trade_price_raw):
#                 return
#             trade_price = float(trade_price_raw)
#             live_price = trade_price
#             last_valid_price = trade_price

#             # update current 5m candle's OHLC with trade tick
#             if not candles.empty:
#                 idx = candles.index[-1]
#                 try:
#                     candles.at[idx, "Close"] = trade_price
#                     candles.at[idx, "High"] = max(candles.at[idx, "High"], trade_price)
#                     candles.at[idx, "Low"] = min(candles.at[idx, "Low"], trade_price)
#                 except Exception:
#                     pass

#             # update fillcheck on every tick
#             update_fillcheck(trade_price)

#             trade_ts_ms = int(payload.get("T") or payload.get("E") or (time.time() * 1000))
#             try_trigger_on_trade(trade_price, trade_ts_ms)

#     except Exception as e:
#         print("on_message error:", e)

# def on_error(ws, error):
#     print("WebSocket error:", error)

# def on_close(ws, code, msg):
#     print("WebSocket closed", code, msg)
#     # restart after short delay
#     time.sleep(2)
#     threading.Thread(target=run_ws, daemon=True).start()

# def on_open(ws):
#     print("WebSocket connected")
#     params = [f"{SYMBOL}@kline_{INTERVAL}", f"{SYMBOL}@trade"]
#     msg = {"method": "SUBSCRIBE", "params": params, "id": 1}
#     try:
#         ws.send(json.dumps(msg))
#     except Exception as e:
#         print("Error sending subscribe message:", e)

# def run_ws():
#     url = "wss://fstream.binance.com/stream"
#     ws = websocket.WebSocketApp(
#         url,
#         on_open=on_open,
#         on_message=on_message,
#         on_error=on_error,
#         on_close=on_close
#     )
#     ws.run_forever(ping_interval=20, ping_timeout=10)

# # ==========================
# # Dash App (display both 5m and even 10m candles, bounds, alerts and stats)
# # ==========================
# app = dash.Dash(__name__)
# server = app.server  # expose Flask server for Render/Heroku
# app.layout = html.Div([
#     html.H1(f"{SYMBOL.upper()} Live Prices & Derived Even 10-min Strategy"),
#     html.Div([
#         html.H2(id="live-price", style={"color": "black", "marginRight": "20px"}),
#         html.H2(id="bal", style={"color": "black", "marginRight": "20px"}),
#         html.Div(id="trade-stats", style={"display": "flex", "gap": "20px", "alignItems": "center"})
#     ], style={"display": "flex", "flexDirection": "row", "alignItems": "center"}),
#     html.Div(id="ohlc-values"),
#     html.H3("Derived Even 10-min Candles"),
#     html.Div(id="ten-min-ohlc"),
#     html.Div(id="bounds", style={"marginTop": "8px", "color": "#9ad"}),
#     html.H3("Strategy Alerts"),
#     html.Ul(id="strategy-alerts"),
#     dcc.Interval(id="interval", interval=500, n_intervals=0)  # update every 0.5s
# ])

# @app.callback(
#     [Output("live-price", "children"),
#      Output("bal", "children"),
#      Output("trade-stats", "children"),
#      Output("ohlc-values", "children"),
#      Output("strategy-alerts", "children"),
#      Output("bounds", "children"),
#      Output("ten-min-ohlc", "children")],
#     [Input("interval", "n_intervals")]
# )
# def update_display(_):
#     # live price / balance / stats
#     lp = f"Live Price: {fmt_price(live_price)}" if live_price is not None else "Live Price: --"
#     bal = f"Balance: {fmt_price(initial_balance)}"

#     stats_html = [
#         html.Div(f"FillCheck: {fillcheck}"),
#         html.Div(f"FillCount: {fillcount}"),
#         html.Div(f"TotalTrades: {totaltradecount}"),
#         html.Div(f"UnfilledPnL: {fmt_price(unfilledpnl)}")
#     ]

#     # 5-min candles HTML
#     ohlc_html = []
#     for idx, row in candles.iterrows():
#         try:
#             ts_str = row['time'].astimezone(KOLKATA_TZ).strftime("%H:%M:%S")
#         except Exception:
#             ts_str = str(row['time'])
#         text = f"{ts_str} → O:{fmt_price(row['Open'])}, H:{fmt_price(row['High'])}, L:{fmt_price(row['Low'])}, C:{fmt_price(row['Close'])}"
#         ohlc_html.append(html.Div(text))

#     # 10-min candles HTML
#     ten_min_html = []
#     for idx, row in ten_min_candles.iterrows():
#         try:
#             ts_str = row['time'].astimezone(KOLKATA_TZ).strftime("%H:%M:%S")
#         except Exception:
#             ts_str = str(row['time'])
#         text = f"{ts_str} → O:{fmt_price(row['Open'])}, H:{fmt_price(row['High'])}, L:{fmt_price(row['Low'])}, C:{fmt_price(row['Close'])}"
#         ten_min_html.append(html.Div(text))

#     # alerts
#     alerts_html = [html.Li(a) for a in alerts[-50:]]

#     if upper_bound is not None and lower_bound is not None and _bounds_candle_ts is not None:
#         btxt = (f"Bounds[{LENGTH}] → Upper {fmt_price(upper_bound)}, Lower {fmt_price(lower_bound)} "
#                 f"(from 10-min candle {_bounds_candle_ts.astimezone(KOLKATA_TZ).strftime('%H:%M:%S')})")
#     else:
#         btxt = "Bounds: waiting for enough closed 10-min candles..."

#     return lp, bal, stats_html, ohlc_html, alerts_html, btxt, ten_min_html


# # Keep-alive ping thread
# def keep_alive():
#     """Send a ping to the server itself."""
#     try:
#         print(f"🔄 Pinging {PING_URL}")
#         r = requests.get(PING_URL, timeout=10)
#         print("✅ Ping response:", r.status_code)
#     except Exception as e:
#         print("⚠️ Keep-alive ping failed:", str(e))

# @server.route('/ping', methods=['GET'])
# def ping():
#     keep_alive()
#     return jsonify({"status": "alive"}), 200

# # ==========================
# # Main (Render compatible)
# # ==========================
# if __name__ == "__main__":
#     try:
#         fetch_initial_candles()
#     except Exception as e:
#         print("Initial fetch failed:", e)

#     if len(candles) >= LENGTH:
#         recompute_bounds_on_close()

#     t = threading.Thread(target=run_ws, daemon=True)
#     t.start()

#     port = int(os.environ.get("PORT", 10000))  # Render sets PORT
#     app.run(host="0.0.0.0", port=port, debug=False)




    
# ======================================================================================================================
# ==========================   Chartifier ==============================================================================
# ======================================================================================================================

# import json
# import os
# import time
# import threading
# from datetime import datetime, timezone, timedelta

# import pandas as pd
# import requests
# import websocket
# import dash
# from dash import dcc, html
# from dash.dependencies import Input, Output
# from flask import Flask, request, jsonify

# SYMBOL = "xrpusdc"       # keep lowercase for websocket streams
# INTERVAL = "10m"
# CANDLE_LIMIT = 10
# PING_URL = os.environ.get("PING_URL", "https://bot-reviver.onrender.com/ping")
# WEBHOOK_URL = "https://www.example.com/webhook"

# LENGTH = 1  # last closed candles for bounds

# # ==========================
# # Timezone (UTC+5:30 Kolkata)
# # ==========================
# KOLKATA_TZ = timezone(timedelta(hours=5, minutes=30))

# # ==========================
# # Globals
# # ==========================
# candles = pd.DataFrame(columns=["time", "Open", "High", "Low", "Close"])
# live_price = None
# last_valid_price = None
# alerts = []

# initail_balance = 10.0
# entryprice = None
# panl = 0.0

# upper_bound = None
# lower_bound = None
# _bounds_candle_ts = None
# _triggered_window_id = None

# EntryCount = 0
# LastSide = None
# LastLastSide = "buy"
# status = None

# # Order Filling data
# fillcheck = 0
# fillcount = 0
# totaltradecount = 0
# unfilledpnl = 0.0

# # ==========================
# # Utilities
# # ==========================
# def is_valid_price(x):
#     try:
#         return float(x) > 0
#     except Exception:
#         return False

# def fmt_price(p):
#     if p is None:
#         return "--"
#     ap = abs(p)
#     if ap >= 100:
#         return f"{p:.2f}"
#     elif ap >= 1:
#         return f"{p:.4f}"
#     else:
#         return f"{p:.8f}"

# # ==========================
# # Helpers
# # ==========================
# def fetch_initial_candles():
#     global candles, live_price, last_valid_price
#     url = f"https://fapi.binance.com/fapi/v1/klines?symbol={SYMBOL.upper()}&interval={INTERVAL}&limit={CANDLE_LIMIT}"
#     r = requests.get(url, timeout=10)
#     r.raise_for_status()
#     data = r.json()

#     rows = []
#     for k in data:
#         rows.append({
#             "time": datetime.fromtimestamp(k[0] / 1000, tz=KOLKATA_TZ),
#             "Open": float(k[1]),
#             "High": float(k[2]),
#             "Low": float(k[3]),
#             "Close": float(k[4]),
#         })
#     candles = pd.DataFrame(rows)
#     if not candles.empty:
#         live_price = float(candles["Close"].iloc[-1])
#         last_valid_price = live_price if is_valid_price(live_price) else None

# def get_status(EntryCount: int) -> str:
#     return "entry" if EntryCount % 2 == 1 else "exit"

# def calculate_pnl(entry_price: float, closing_price: float, side: str) -> float:
#     quantity = 1.8  # fixed quantity
#     if side == "buy":
#         return (closing_price - entry_price) * quantity
#     elif side == "sell":
#         return (entry_price - closing_price) * quantity
#     else:
#         return 0.0

# def send_webhook(trigger_time_iso: str, entry_price_in: float, side: str):
#     global EntryCount, LastSide, status, entryprice, panl, initail_balance, fillcheck, totaltradecount, unfilledpnl, LastLastSide

#     secret = "gajraj09"
#     quantity = 1.8
#     status = get_status(EntryCount)
    
#     pnl = 0.0

#     if status == "exit":
#         if entryprice is None:
#             print("[WEBHOOK] Exit requested but no stored entryprice; ignoring pnl update.")
#         else:
#             pnl = calculate_pnl(entryprice, entry_price_in, LastLastSide)
#             if fillcheck == 0:
#                 initail_balance += pnl
#             panl += pnl
#         entryprice = None
#         if fillcheck == 1:
#             unfilledpnl += pnl
#         fillcheck = 0
#     else:  # entry
#         if entryprice is not None:
#             pnl = calculate_pnl(entryprice, entry_price_in, LastLastSide)
#             if fillcheck == 0:
#                 initail_balance += pnl
#             panl += pnl
#         else:
#             print(f"[ENTRY] Opening first position at {fmt_price(entry_price_in)}")
#         entryprice = entry_price_in
#         totaltradecount += 1
#         fillcheck = 1
#     LastLastSide = LastSide

#     print(f"[WEBHOOK] {trigger_time_iso} | {side} | Entry/Price: {entry_price_in} | symbol: {SYMBOL.upper()} | status: {status} | QUANTITY: {quantity}")
#     try:
#         payload = {
#             "symbol": SYMBOL.upper(),
#             "side": side,
#             "quantity": quantity,
#             "price": entry_price_in,
#             "status": status,
#             "secret": secret
#         }
#         requests.post(WEBHOOK_URL, json=payload, timeout=5)
#         print("Sent payload:", payload)
#     except Exception as e:
#         print("Webhook error:", e)

# def recompute_bounds_on_close():
#     global upper_bound, lower_bound, _bounds_candle_ts, _triggered_window_id
#     if len(candles) < LENGTH:
#         upper_bound = None
#         lower_bound = None
#         _bounds_candle_ts = None
#         _triggered_window_id = None
#         return

#     window = candles.tail(LENGTH)
#     highs = window["High"][window["High"] > 0]
#     lows = window["Low"][window["Low"] > 0]
#     if highs.empty or lows.empty:
#         return

#     upper_bound = float(highs.max())
#     lower_bound = float(lows.min())
#     _bounds_candle_ts = window["time"].iloc[-1]
#     _triggered_window_id = None

# # ==========================
# # Fillcheck update on every tick
# # ==========================
# def update_fillcheck(trade_price: float):
#     global fillcheck, fillcount, entryprice, LastSide, status, totaltradecount, unfilledpnl

#     if entryprice is None or fillcheck == 0:
#         return

#     if status == "entry":
#         if LastSide == "buy" and trade_price < entryprice:
#             fillcheck = 0
#             fillcount += 1
#             print(f"[FILL] Buy entry filled at {fmt_price(entryprice)} | Total fills={fillcount}/{totaltradecount}")
#         elif LastSide == "sell" and trade_price > entryprice:
#             fillcheck = 0
#             fillcount += 1
#             print(f"[FILL] Sell entry filled at {fmt_price(entryprice)} | Total fills={fillcount}/{totaltradecount}")
#     elif status == "exit":
#         fillcheck = 0
#         print(f"[FILL] Exit completed at {fmt_price(trade_price)}")

# # ==========================
# # Trigger logic
# # ==========================
# def try_trigger_on_trade(trade_price: float, trade_ts_ms: int):
#     global alerts, _triggered_window_id, status, EntryCount, LastSide

#     if not (is_valid_price(trade_price) and upper_bound is not None and lower_bound is not None and _bounds_candle_ts):
#         return
#     if _triggered_window_id == _bounds_candle_ts:
#         return

#     trigger_time = datetime.fromtimestamp(trade_ts_ms / 1000, tz=KOLKATA_TZ)
#     trigger_time_iso = trigger_time.isoformat()

#     def _process_side(side_name, entry_val, friendly):
#         global EntryCount, LastSide, alerts, _triggered_window_id
#         if EntryCount == 0 or LastSide != side_name:
#             EntryCount = 1
#         else:
#             EntryCount += 1
#         LastSide = side_name

#         send_webhook(trigger_time_iso, entry_val, side_name)
#         msg = f"{friendly} | {status}: {fmt_price(entry_val)} | Live {fmt_price(trade_price)} | Trigger {trigger_time_iso}"
#         alerts.append(msg)
#         alerts[:] = alerts[-50:]
#         _triggered_window_id = _bounds_candle_ts

#     if trade_price >= upper_bound:
#         _process_side("buy", upper_bound, "LONG breakout Buy")
#     elif trade_price <= lower_bound:
#         _process_side("sell", lower_bound, "SHORT breakout Sell")

# # ==========================
# # WebSocket handlers
# # ==========================
# def on_message(ws, message):
#     global candles, live_price, last_valid_price
#     try:
#         data = json.loads(message)
#         stream = data.get("stream")
#         payload = data.get("data")

#         if stream and "kline" in stream:
#             kline = payload["k"]
#             ts_dt = datetime.fromtimestamp(kline["t"] / 1000, tz=KOLKATA_TZ)
#             o, h, l, c = float(kline["o"]), float(kline["h"]), float(kline["l"]), float(kline["c"])

#             if is_valid_price(c):
#                 last_valid_price = c
#                 live_price = c

#             if candles.empty or candles.iloc[-1]["time"] != ts_dt:
#                 open_val = o if is_valid_price(o) else (last_valid_price if last_valid_price is not None else None)
#                 if open_val is None:
#                     return
#                 high_val = h if is_valid_price(h) else open_val
#                 low_val = l if is_valid_price(l) else open_val
#                 close_val = c if is_valid_price(c) else open_val

#                 new_row = {
#                     "time": ts_dt,
#                     "Open": open_val,
#                     "High": max(high_val, open_val),
#                     "Low": min(low_val, open_val),
#                     "Close": close_val
#                 }
#                 candles = pd.concat([candles, pd.DataFrame([new_row])], ignore_index=True)
#             else:
#                 idx = candles.index[-1]
#                 if is_valid_price(h):
#                     candles.at[idx, "High"] = max(candles.at[idx, "High"], h)
#                 if is_valid_price(l):
#                     candles.at[idx, "Low"] = min(candles.at[idx, "Low"], l)
#                 if is_valid_price(c):
#                     candles.at[idx, "Close"] = c
#                     live_price = c
#                     last_valid_price = c

#             if len(candles) > CANDLE_LIMIT:
#                 candles = candles.tail(CANDLE_LIMIT).reset_index(drop=True)

#             if kline.get("x"):
#                 recompute_bounds_on_close()

#         elif stream and "trade" in stream:
#             trade_price_raw = payload.get("p")
#             if not is_valid_price(trade_price_raw):
#                 return
#             trade_price = float(trade_price_raw)
#             live_price = trade_price
#             last_valid_price = trade_price

#             if not candles.empty:
#                 idx = candles.index[-1]
#                 candles.at[idx, "Close"] = trade_price
#                 candles.at[idx, "High"] = max(candles.at[idx, "High"], trade_price)
#                 candles.at[idx, "Low"] = min(candles.at[idx, "Low"], trade_price)

#             # --- Update fillcheck dynamically on every tick ---
#             update_fillcheck(trade_price)

#             trade_ts_ms = int(payload.get("T") or payload.get("E") or time.time() * 1000)
#             try_trigger_on_trade(trade_price, trade_ts_ms)

#     except Exception as e:
#         print("on_message error:", e)

# def on_error(ws, error):
#     print("WebSocket error:", error)

# def on_close(ws, code, msg):
#     print("WebSocket closed", code, msg)
#     time.sleep(2)
#     threading.Thread(target=run_ws, daemon=True).start()

# def on_open(ws):
#     print("WebSocket connected")
#     params = [f"{SYMBOL}@kline_{INTERVAL}", f"{SYMBOL}@trade"]
#     msg = {"method": "SUBSCRIBE", "params": params, "id": 1}
#     ws.send(json.dumps(msg))

# def run_ws():
#     url = "wss://fstream.binance.com/stream"
#     ws = websocket.WebSocketApp(
#         url,
#         on_open=on_open,
#         on_message=on_message,
#         on_error=on_error,
#         on_close=on_close
#     )
#     ws.run_forever(ping_interval=20, ping_timeout=10)

# # ==========================
# # Dash App
# # ==========================
# app = dash.Dash(__name__)
# server = app.server  # expose Flask server for Render/Heroku
# app.layout = html.Div([
#     html.H1(f"{SYMBOL.upper()} Live Prices & Last {CANDLE_LIMIT} Candles"),
#     html.Div([
#         html.H2(id="live-price", style={"color": "black", "marginRight": "20px"}),
#         html.H2(id="bal", style={"color": "black", "marginRight": "20px"}),
#         html.Div(id="trade-stats", style={"display": "flex", "gap": "20px", "alignItems": "center"})
#     ], style={"display": "flex", "flexDirection": "row", "alignItems": "center"}),
#     html.Div(id="ohlc-values"),
#     html.Div(id="bounds", style={"marginTop": "8px", "color": "#9ad"}),
#     html.H3("Strategy Alerts"),
#     html.Ul(id="strategy-alerts"),
#     dcc.Interval(id="interval", interval=500, n_intervals=0)  # update every 0.5s
# ])

# @app.callback(
#     [Output("live-price", "children"),
#      Output("bal", "children"),
#      Output("trade-stats", "children"),
#      Output("ohlc-values", "children"),
#      Output("strategy-alerts", "children"),
#      Output("bounds", "children")],
#     [Input("interval", "n_intervals")]
# )
# def update_display(_):
#     if len(candles) == 0:
#         return (
#             "Live Price: --",
#             f"Balance: {fmt_price(initail_balance)}",
#             [],
#             [],
#             [],
#             "Bounds: waiting for enough closed candles..."
#         )

#     lp = f"Live Price: {fmt_price(live_price)}"
#     bal = f"Balance: {fmt_price(initail_balance)}"

#     stats_html = [
#         html.Div(f"FillCheck: {fillcheck}"),
#         html.Div(f"FillCount: {fillcount}"),
#         html.Div(f"TotalTrades: {totaltradecount}"),
#         html.Div(f"UnfilledPnL: {fmt_price(unfilledpnl)}")
#     ]

#     ohlc_html = []
#     for idx, row in candles.iterrows():
#         ts_str = row['time'].astimezone(KOLKATA_TZ).strftime("%H:%M:%S")
#         text = f"{ts_str} → O:{fmt_price(row['Open'])}, H:{fmt_price(row['High'])}, L:{fmt_price(row['Low'])}, C:{fmt_price(row['Close'])}"
#         ohlc_html.append(html.Div(text))

#     alerts_html = [html.Li(a) for a in alerts[-10:]]

#     if upper_bound is not None and lower_bound is not None and _bounds_candle_ts is not None:
#         btxt = (f"Bounds[{LENGTH}] → Upper {fmt_price(upper_bound)}, Lower {fmt_price(lower_bound)} "
#                 f"(from candle {_bounds_candle_ts.astimezone(KOLKATA_TZ).strftime('%H:%M:%S')})")
#     else:
#         btxt = "Bounds: waiting for enough closed candles..."

#     return lp, bal, stats_html, ohlc_html, alerts_html, btxt


# # Keep-alive ping thread
# def keep_alive():
#     """Send a ping to the server itself."""
#     try:
#         print(f"🔄 Pinging {PING_URL}")
#         r = requests.get(PING_URL, timeout=10)
#         print("✅ Ping response:", r.status_code)
#     except Exception as e:
#         print("⚠️ Keep-alive ping failed:", str(e))

# @server.route('/ping', methods=['GET'])
# def ping():
#     keep_alive()
#     return jsonify({"status": "alive"}), 200

# # ==========================
# # Main (Render compatible)
# # ==========================
# if __name__ == "__main__":
#     try:
#         fetch_initial_candles()
#     except Exception as e:
#         print("Initial fetch failed:", e)

#     if len(candles) >= LENGTH:
#         recompute_bounds_on_close()

#     t = threading.Thread(target=run_ws, daemon=True)
    # t.start()

    # port = int(os.environ.get("PORT", 10000))  # Render sets PORT
    # app.run(host="0.0.0.0", port=port, debug=False)
