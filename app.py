# ==================== 15 Min candle chart =========================================
# ==================================================================================

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
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "mongodb+srv://gsr939988_db_user:ROYfY6z5kxUumq5i@tradingbot.lvnxj3a.mongodb.net/?retryWrites=true&w=majority&appName=TradingBot")

# ====For ETHUSDC 15 Min Chart
LENGTH = 1  # number of LAST CLOSED candles to compute bounds
# ====For XRPUSDC 15 Min Chart
# LENGTH = 2  # number of LAST CLOSED candles to compute bounds

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = "trading_bot_eth"
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
    quantity = 0.006  # fixed quantity
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
    quantity = 0.006

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

def try_trigger_on_trade(trade_price: float, trade_ts_ms: int):
    global alerts, _triggered_window_id,_triggered_window_side, status, EntryCount, LastSide

    if not (
        is_valid_price(trade_price)
        and upper_bound is not None
        and lower_bound is not None
        and _bounds_candle_ts is not None
    ):
        return
    if _triggered_window_id == _bounds_candle_ts:
        return
    

    trigger_time = datetime.fromtimestamp(trade_ts_ms / 1000, tz=KOLKATA_TZ)
    trigger_time_iso = trigger_time.isoformat()
    ts_dt = datetime.fromtimestamp(trade_ts_ms / 1000, tz=STORE_TZ)

    def _process_side(side_name, entry_val, friendly):
        nonlocal trigger_time_iso
        global EntryCount, LastSide, alerts, _triggered_window_id
        LastSide = side_name

        send_webhook(trigger_time_iso, entry_val, side_name, "entry")
        msg = f"{friendly} | {status}: {fmt_price(entry_val)} | Live {fmt_price(trade_price)} | Trigger {ts_dt.astimezone(KOLKATA_TZ).strftime('%H:%M:%S')}"
        alerts.append(msg)
        alerts[:] = alerts[-50:]
        _triggered_window_id = _bounds_candle_ts
        # _triggered_window_side = side_name
        save_state()

    if trade_price > upper_bound:
        _process_side("buy", upper_bound, "LONG")
    elif trade_price < lower_bound:
        _process_side("sell", lower_bound, "SHORT")

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

#     elif trade_price < lower_bound:
#         if not (_triggered_window_id == _bounds_candle_ts and status == "entry"):
#             if _triggered_window_side == "sell" and status == "exit":
#                 return
#             process_trigger("sell", lower_bound, "SHORT")

# ==========================
# WebSocket handlers
# ==========================

def on_message(ws, message):
    global candles, live_price, last_valid_price,status,lastpnl
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
                if status != "exit":
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
    # try:
    #     fetch_initial_candles()
    # except Exception as e:
    #     print("Initial fetch failed:", e)

    # if len(candles) >= LENGTH:
    #     recompute_bounds_on_close()

    # t = threading.Thread(target=run_ws, daemon=True)
    # t.start()

    port = int(os.environ.get("PORT", 10000))  # Render sets PORT
    app.run(host="0.0.0.0", port=port, debug=False)


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
