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

SYMBOL = "xrpusdc"       # keep lowercase for websocket streams
INTERVAL = "3m"
CANDLE_LIMIT = 10
PING_URL = os.environ.get("PING_URL", "https://bot-reviver.onrender.com/ping")
WEBHOOK_URL = "https://www.example.com/webhook"

LENGTH = 3  # last closed candles for bounds

# ==========================
# Timezone (UTC+5:30 Kolkata)
# ==========================
KOLKATA_TZ = timezone(timedelta(hours=5, minutes=30))

# ==========================
# Globals
# ==========================
candles = pd.DataFrame(columns=["time", "Open", "High", "Low", "Close"])
live_price = None
last_valid_price = None
alerts = []

initail_balance = 5.0
entryprice = None
panl = 0.0

upper_bound = None
lower_bound = None
_bounds_candle_ts = None
_triggered_window_id = None

EntryCount = 0
LastSide = None
LastLastSide = "buy"
status = None

# Order Filling data
fillcheck = 0
fillcount = 0
totaltradecount = 0
unfilledpnl = 0.0

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
    global candles, live_price, last_valid_price
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={SYMBOL.upper()}&interval={INTERVAL}&limit={CANDLE_LIMIT}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()

    rows = []
    for k in data:
        rows.append({
            "time": datetime.fromtimestamp(k[0] / 1000, tz=KOLKATA_TZ),
            "Open": float(k[1]),
            "High": float(k[2]),
            "Low": float(k[3]),
            "Close": float(k[4]),
        })
    candles = pd.DataFrame(rows)
    if not candles.empty:
        live_price = float(candles["Close"].iloc[-1])
        last_valid_price = live_price if is_valid_price(live_price) else None

def get_status(EntryCount: int) -> str:
    return "entry" if EntryCount % 2 == 1 else "exit"

def calculate_pnl(entry_price: float, closing_price: float, side: str) -> float:
    quantity = 1.8  # fixed quantity
    if side == "buy":
        return (closing_price - entry_price) * quantity
    elif side == "sell":
        return (entry_price - closing_price) * quantity
    else:
        return 0.0

def send_webhook(trigger_time_iso: str, entry_price_in: float, side: str):
    global EntryCount, LastSide, status, entryprice, panl, initail_balance, fillcheck, totaltradecount, unfilledpnl, LastLastSide

    secret = "gajraj09"
    quantity = 1.8
    status = get_status(EntryCount)
    
    pnl = 0.0

    if status == "exit":
        if entryprice is None:
            print("[WEBHOOK] Exit requested but no stored entryprice; ignoring pnl update.")
        else:
            pnl = calculate_pnl(entryprice, entry_price_in, LastLastSide)
            if fillcheck == 0:
                initail_balance += pnl
            panl += pnl
        entryprice = None
        if fillcheck == 1:
            unfilledpnl += pnl
        fillcheck = 0
    else:  # entry
        if entryprice is not None:
            pnl = calculate_pnl(entryprice, entry_price_in, LastLastSide)
            if fillcheck == 0:
                initail_balance += pnl
            panl += pnl
        else:
            print(f"[ENTRY] Opening first position at {fmt_price(entry_price_in)}")
        entryprice = entry_price_in
        totaltradecount += 1
        fillcheck = 1
    LastLastSide = LastSide

    print(f"[WEBHOOK] {trigger_time_iso} | {side} | Entry/Price: {entry_price_in} | symbol: {SYMBOL.upper()} | status: {status} | QUANTITY: {quantity}")
    try:
        payload = {
            "symbol": SYMBOL.upper(),
            "side": side,
            "quantity": quantity,
            "price": entry_price_in,
            "status": status,
            "secret": secret
        }
        requests.post(WEBHOOK_URL, json=payload, timeout=5)
        print("Sent payload:", payload)
    except Exception as e:
        print("Webhook error:", e)

def recompute_bounds_on_close():
    global upper_bound, lower_bound, _bounds_candle_ts, _triggered_window_id
    if len(candles) < LENGTH:
        upper_bound = None
        lower_bound = None
        _bounds_candle_ts = None
        _triggered_window_id = None
        return

    window = candles.tail(LENGTH)
    highs = window["High"][window["High"] > 0]
    lows = window["Low"][window["Low"] > 0]
    if highs.empty or lows.empty:
        return

    upper_bound = float(highs.max())
    lower_bound = float(lows.min())
    _bounds_candle_ts = window["time"].iloc[-1]
    _triggered_window_id = None

# ==========================
# Fillcheck update on every tick
# ==========================
def update_fillcheck(trade_price: float):
    global fillcheck, fillcount, entryprice, LastSide, status, totaltradecount, unfilledpnl

    if entryprice is None or fillcheck == 0:
        return

    if status == "entry":
        if LastSide == "buy" and trade_price < entryprice:
            fillcheck = 0
            fillcount += 1
            print(f"[FILL] Buy entry filled at {fmt_price(entryprice)} | Total fills={fillcount}/{totaltradecount}")
        elif LastSide == "sell" and trade_price > entryprice:
            fillcheck = 0
            fillcount += 1
            print(f"[FILL] Sell entry filled at {fmt_price(entryprice)} | Total fills={fillcount}/{totaltradecount}")
    elif status == "exit":
        fillcheck = 0
        print(f"[FILL] Exit completed at {fmt_price(trade_price)}")

# ==========================
# Trigger logic
# ==========================
def try_trigger_on_trade(trade_price: float, trade_ts_ms: int):
    global alerts, _triggered_window_id, status, EntryCount, LastSide

    if not (is_valid_price(trade_price) and upper_bound is not None and lower_bound is not None and _bounds_candle_ts):
        return
    if _triggered_window_id == _bounds_candle_ts:
        return

    trigger_time = datetime.fromtimestamp(trade_ts_ms / 1000, tz=KOLKATA_TZ)
    trigger_time_iso = trigger_time.isoformat()

    def _process_side(side_name, entry_val, friendly):
        global EntryCount, LastSide, alerts, _triggered_window_id
        if EntryCount == 0 or LastSide != side_name:
            EntryCount = 1
        else:
            EntryCount += 1
        LastSide = side_name

        send_webhook(trigger_time_iso, entry_val, side_name)
        msg = f"{friendly} | {status}: {fmt_price(entry_val)} | Live {fmt_price(trade_price)} | Trigger {trigger_time_iso}"
        alerts.append(msg)
        alerts[:] = alerts[-50:]
        _triggered_window_id = _bounds_candle_ts

    if trade_price >= upper_bound:
        _process_side("buy", upper_bound, "LONG breakout Buy")
    elif trade_price <= lower_bound:
        _process_side("sell", lower_bound, "SHORT breakout Sell")

# ==========================
# WebSocket handlers
# ==========================
def on_message(ws, message):
    global candles, live_price, last_valid_price
    try:
        data = json.loads(message)
        stream = data.get("stream")
        payload = data.get("data")

        if stream and "kline" in stream:
            kline = payload["k"]
            ts_dt = datetime.fromtimestamp(kline["t"] / 1000, tz=KOLKATA_TZ)
            o, h, l, c = float(kline["o"]), float(kline["h"]), float(kline["l"]), float(kline["c"])

            if is_valid_price(c):
                last_valid_price = c
                live_price = c

            if candles.empty or candles.iloc[-1]["time"] != ts_dt:
                open_val = o if is_valid_price(o) else (last_valid_price if last_valid_price is not None else None)
                if open_val is None:
                    return
                high_val = h if is_valid_price(h) else open_val
                low_val = l if is_valid_price(l) else open_val
                close_val = c if is_valid_price(c) else open_val

                new_row = {
                    "time": ts_dt,
                    "Open": open_val,
                    "High": max(high_val, open_val),
                    "Low": min(low_val, open_val),
                    "Close": close_val
                }
                candles = pd.concat([candles, pd.DataFrame([new_row])], ignore_index=True)
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
                candles.at[idx, "Close"] = trade_price
                candles.at[idx, "High"] = max(candles.at[idx, "High"], trade_price)
                candles.at[idx, "Low"] = min(candles.at[idx, "Low"], trade_price)

            # --- Update fillcheck dynamically on every tick ---
            update_fillcheck(trade_price)

            trade_ts_ms = int(payload.get("T") or payload.get("E") or time.time() * 1000)
            try_trigger_on_trade(trade_price, trade_ts_ms)

    except Exception as e:
        print("on_message error:", e)

def on_error(ws, error):
    print("WebSocket error:", error)

def on_close(ws, code, msg):
    print("WebSocket closed", code, msg)
    time.sleep(2)
    threading.Thread(target=run_ws, daemon=True).start()

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
        on_close=on_close
    )
    ws.run_forever(ping_interval=20, ping_timeout=10)

# ==========================
# Dash App
# ==========================
app = dash.Dash(__name__)
server = app.server  # expose Flask server for Render/Heroku
app.layout = html.Div([
    html.H1(f"{SYMBOL.upper()} Live Prices & Last {CANDLE_LIMIT} Candles"),
    html.Div([
        html.H2(id="live-price", style={"color": "black", "marginRight": "20px"}),
        html.H2(id="bal", style={"color": "black", "marginRight": "20px"}),
        html.Div(id="trade-stats", style={"display": "flex", "gap": "20px", "alignItems": "center"})
    ], style={"display": "flex", "flexDirection": "row", "alignItems": "center"}),
    html.Div(id="ohlc-values"),
    html.Div(id="bounds", style={"marginTop": "8px", "color": "#9ad"}),
    html.H3("Strategy Alerts"),
    html.Ul(id="strategy-alerts"),
    dcc.Interval(id="interval", interval=500, n_intervals=0)  # update every 0.5s
])

@app.callback(
    [Output("live-price", "children"),
     Output("bal", "children"),
     Output("trade-stats", "children"),
     Output("ohlc-values", "children"),
     Output("strategy-alerts", "children"),
     Output("bounds", "children")],
    [Input("interval", "n_intervals")]
)
def update_display(_):
    if len(candles) == 0:
        return (
            "Live Price: --",
            f"Balance: {fmt_price(initail_balance)}",
            [],
            [],
            [],
            "Bounds: waiting for enough closed candles..."
        )

    lp = f"Live Price: {fmt_price(live_price)}"
    bal = f"Balance: {fmt_price(initail_balance)}"

    stats_html = [
        html.Div(f"FillCheck: {fillcheck}"),
        html.Div(f"FillCount: {fillcount}"),
        html.Div(f"TotalTrades: {totaltradecount}"),
        html.Div(f"UnfilledPnL: {fmt_price(unfilledpnl)}")
    ]

    ohlc_html = []
    for idx, row in candles.iterrows():
        ts_str = row['time'].astimezone(KOLKATA_TZ).strftime("%H:%M:%S")
        text = f"{ts_str} → O:{fmt_price(row['Open'])}, H:{fmt_price(row['High'])}, L:{fmt_price(row['Low'])}, C:{fmt_price(row['Close'])}"
        ohlc_html.append(html.Div(text))

    alerts_html = [html.Li(a) for a in alerts[-10:]]

    if upper_bound is not None and lower_bound is not None and _bounds_candle_ts is not None:
        btxt = (f"Bounds[{LENGTH}] → Upper {fmt_price(upper_bound)}, Lower {fmt_price(lower_bound)} "
                f"(from candle {_bounds_candle_ts.astimezone(KOLKATA_TZ).strftime('%H:%M:%S')})")
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
    try:
        fetch_initial_candles()
    except Exception as e:
        print("Initial fetch failed:", e)

    if len(candles) >= LENGTH:
        recompute_bounds_on_close()

    t = threading.Thread(target=run_ws, daemon=True)
    t.start()

    port = int(os.environ.get("PORT", 10000))  # Render sets PORT
    app.run(host="0.0.0.0", port=port, debug=False)




# import os
# import json
# import time
# import threading
# from datetime import datetime, timezone

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
# SYMBOL = "xrpusdc"
# INTERVAL = "5m"
# CANDLE_LIMIT = 10
# PING_URL = os.environ.get("PING_URL", "https://bot-reviver.onrender.com/ping")
# LENGTH = 5  # last closed candles for bounds
# WEBHOOK_URL = "https://binance-65gz.onrender.com/webhook"

# # ==========================
# # Globals
# # ==========================
# candles = pd.DataFrame(columns=["time", "Open", "High", "Low", "Close"])
# live_price = None
# last_valid_price = None
# alerts = []

# upper_bound = None
# lower_bound = None
# _bounds_candle_ts = None
# _triggered_window_id = None

# EntryCount = 0
# LastSide = None
# status = None

# # ==========================
# # Manual Seed Data
# # ==========================
# manual_candles = [
#     ("07:40:00", 3.0467, 3.0467, 3.0406, 3.0416),
#     ("07:45:00", 3.0418, 3.0451, 3.0411, 3.0419),
#     ("07:50:00", 3.0419, 3.0447, 3.0382, 3.0393),
#     ("07:55:00", 3.0397, 3.0411, 3.0345, 3.0353),
#     ("08:00:00", 3.0353, 3.0364, 3.0273, 3.0342),
#     ("08:05:00", 3.0338, 3.0415, 3.0320, 3.0415),
#     ("08:10:00", 3.0414, 3.0471, 3.0404, 3.0454),
#     ("08:15:00", 3.0452, 3.0466, 3.0413, 3.0414),
#     ("08:20:00", 3.0415, 3.0419, 3.0348, 3.0397)
# ]

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
# def safe_price(p):
#     """Return valid price; fallback to last valid price"""
#     global last_valid_price
#     try:
#         p = float(p)
#         if p <= 0:
#             return last_valid_price or 1.0
#         last_valid_price = p
#         return p
#     except Exception:
#         return last_valid_price or 1.0
# def seed_manual_candles():
#     global candles, live_price, last_valid_price
#     today = datetime.now(timezone.utc).date()
#     rows = []
#     for t, o, h, l, c in manual_candles:
#         dt = datetime.strptime(f"{today} {t}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
#         rows.append({"time": dt, "Open": o, "High": h, "Low": l, "Close": c})
#     candles = pd.DataFrame(rows)
#     live_price = candles["Close"].iloc[-1]
#     last_valid_price = live_price
#     print(f"✅ Seeded {len(candles)} manual candles")

# def fetch_initial_candles():
#     global candles, live_price, last_valid_price
#     try:
#         url = f"https://fapi.binance.com/fapi/v1/klines?symbol={SYMBOL.upper()}&interval={INTERVAL}&limit={CANDLE_LIMIT}"
#         r = requests.get(url, timeout=10)
#         r.raise_for_status()
#         data = r.json()
#         rows = []
#         for k in data:
#             rows.append({
#                 "time": datetime.fromtimestamp(k[0]/1000, tz=timezone.utc),
#                 "Open": float(k[1]),
#                 "High": float(k[2]),
#                 "Low": float(k[3]),
#                 "Close": float(k[4]),
#             })
#         candles = pd.DataFrame(rows)
#         if not candles.empty:
#             live_price = float(candles["Close"].iloc[-1])
#             last_valid_price = live_price
#             print(f"✅ Binance returned {len(candles)} candles")
#             return
#     except Exception as e:
#         print("⚠️ Binance fetch failed:", e)
#     seed_manual_candles()

# def get_status(EntryCount: int) -> str:
#     return "entry" if EntryCount % 2 == 1 else "exit"

# def send_webhook(trigger_time_iso: str, entry_price: float, side: str,status: str):
#     secret = "gajraj09"
#     quantity = 1.8

#     # if EntryCount % 2 == 1:
#     #     status = "entry"
#     # else:
#     #     status = "exit"
#     # if EntryCount % 3 == 0:
#     #     status = "exit"
    

#     print(f"[WEBHOOK] {trigger_time_iso} | {side} | Entry: {entry_price} | {SYMBOL.upper()} | status: {status} | qty: {quantity}")
#     try:
#         payload = {
#             "symbol": "XRPUSDC",
#             "side": side,
#             "quantity": quantity,
#             "price": entry_price,
#             "status": status,
#             "secret": secret
#         }
#         requests.post(WEBHOOK_URL, json=payload, timeout=5)
#         print(payload)
#     except Exception as e:
#         print("Webhook error:", e)
#         print("Payload Error")

# def recompute_bounds_on_close():
#     global upper_bound, lower_bound, _bounds_candle_ts, _triggered_window_id
#     if len(candles) < LENGTH:
#         return
#     window = candles.tail(LENGTH)
#     upper_bound = float(window["High"].max())
#     lower_bound = float(window["Low"].min())
#     _bounds_candle_ts = window["time"].iloc[-1]
#     _triggered_window_id = None
#     print(f"📊 Bounds recomputed: Upper={upper_bound}, Lower={lower_bound}")



# # def try_trigger_on_trade(trade_price: float, trade_ts_ms: int):
# #     global alerts, _triggered_window_id, EntryCount, LastSide, status
# #     if not (is_valid_price(trade_price) and upper_bound and lower_bound and _bounds_candle_ts):
# #         return
# #     if _triggered_window_id == _bounds_candle_ts:
# #         return
# #     trigger_time = datetime.fromtimestamp(trade_ts_ms / 1000, tz=timezone.utc)
# #     iso = trigger_time.isoformat()

# #     if trade_price >= upper_bound:
# #         side = "buy"
# #         EntryCount = EntryCount + 1 if LastSide == side else 1
# #         LastSide = side
# #         status = get_status(EntryCount)
# #         send_webhook(iso, upper_bound, side,status)
# #         alerts.append(f"LONG breakout {fmt_price(upper_bound)} | {status}: {fmt_price(trade_price)} | {iso}")
# #         _triggered_window_id = _bounds_candle_ts
# #     elif trade_price <= lower_bound:
# #         side = "sell"
# #         EntryCount = EntryCount + 1 if LastSide == side else 1
# #         LastSide = side
# #         status = get_status(EntryCount)
# #         send_webhook(iso, lower_bound, side,status)
# #         alerts.append(f"SHORT breakout {fmt_price(lower_bound)} | {status}: {fmt_price(trade_price)} | {iso}")
# #         _triggered_window_id = _bounds_candle_ts
# #     alerts[:] = alerts[-50:]

# def try_trigger_on_trade(trade_price: float, trade_ts_ms: int):
#     global alerts, _triggered_window_id, EntryCount, LastSide, status
#     trade_price = safe_price(trade_price)
#     if not (trade_price and upper_bound and lower_bound and _bounds_candle_ts):
#         return
#     if _triggered_window_id == _bounds_candle_ts:
#         return
#     trigger_time = datetime.fromtimestamp(trade_ts_ms / 1000, tz=timezone.utc)
#     iso = trigger_time.isoformat()

#     if trade_price >= upper_bound:
#         side = "buy"
#         EntryCount = EntryCount + 1 if LastSide == side else 1
#         LastSide = side
#         status = get_status(EntryCount)
#         send_webhook(iso, upper_bound, side, status)
#         alerts.append(f"LONG breakout {fmt_price(upper_bound)} | {status}: {fmt_price(trade_price)} | {iso}")
#         _triggered_window_id = _bounds_candle_ts
#     elif trade_price <= lower_bound:
#         side = "sell"
#         EntryCount = EntryCount + 1 if LastSide == side else 1
#         LastSide = side
#         status = get_status(EntryCount)
#         send_webhook(iso, lower_bound, side, status)
#         alerts.append(f"SHORT breakout {fmt_price(lower_bound)} | {status}: {fmt_price(trade_price)} | {iso}")
#         _triggered_window_id = _bounds_candle_ts
#     alerts[:] = alerts[-50:]

# # ==========================
# # WebSocket
# # ==========================
# # def on_message(ws, message):
# #     global candles, live_price, last_valid_price
# #     try:
# #         data = json.loads(message)
# #         stream, payload = data.get("stream"), data.get("data")

# #         if "kline" in stream:
# #             k = payload["k"]
# #             ts = datetime.fromtimestamp(k["t"]/1000, tz=timezone.utc)
# #             o, h, l, c = float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"])
# #             live_price = c; last_valid_price = c

# #             if candles.empty or candles.iloc[-1]["time"] != ts:
# #                 candles = pd.concat([candles, pd.DataFrame([{"time": ts, "Open": o, "High": h, "Low": l, "Close": c}])], ignore_index=True)
# #             else:
# #                 idx = candles.index[-1]
# #                 candles.at[idx, "High"] = max(candles.at[idx, "High"], h)
# #                 candles.at[idx, "Low"] = min(candles.at[idx, "Low"], l)
# #                 candles.at[idx, "Close"] = c

# #             if len(candles) > CANDLE_LIMIT:
# #                 candles = candles.tail(CANDLE_LIMIT).reset_index(drop=True)
# #             if k["x"]:
# #                 recompute_bounds_on_close()

# #         elif "trade" in stream:
# #             trade_price = float(payload["p"])
# #             live_price = trade_price
# #             last_valid_price = trade_price
# #             if not candles.empty:
# #                 idx = candles.index[-1]
# #                 candles.at[idx, "Close"] = trade_price
# #                 candles.at[idx, "High"] = max(candles.at[idx, "High"], trade_price)
# #                 candles.at[idx, "Low"] = min(candles.at[idx, "Low"], trade_price)
# #             try_trigger_on_trade(trade_price, payload["T"])
# #     except Exception as e:
# #         print("on_message error:", e)

# def on_message(ws, message):
#     global candles, live_price, last_valid_price
#     try:
#         data = json.loads(message)
#         stream, payload = data.get("stream"), data.get("data")

#         if "kline" in stream:
#             k = payload["k"]
#             ts = datetime.fromtimestamp(k["t"]/1000, tz=timezone.utc)
#             o, h, l, c = safe_price(k["o"]), safe_price(k["h"]), safe_price(k["l"]), safe_price(k["c"])
#             live_price = c

#             if candles.empty or candles.iloc[-1]["time"] != ts:
#                 candles = pd.concat([candles, pd.DataFrame([{"time": ts, "Open": o, "High": h, "Low": l, "Close": c}])], ignore_index=True)
#             else:
#                 idx = candles.index[-1]
#                 candles.at[idx, "High"] = max(candles.at[idx, "High"], h)
#                 candles.at[idx, "Low"] = min(candles.at[idx, "Low"], l)
#                 candles.at[idx, "Close"] = c

#             if len(candles) > CANDLE_LIMIT:
#                 candles = candles.tail(CANDLE_LIMIT).reset_index(drop=True)
#             if k["x"]:
#                 recompute_bounds_on_close()

#         elif "trade" in stream:
#             trade_price = safe_price(payload["p"])
#             live_price = trade_price
#             if not candles.empty:
#                 idx = candles.index[-1]
#                 candles.at[idx, "Close"] = trade_price
#                 candles.at[idx, "High"] = max(candles.at[idx, "High"], trade_price)
#                 candles.at[idx, "Low"] = min(candles.at[idx, "Low"], trade_price)
#             try_trigger_on_trade(trade_price, payload["T"])
#     except Exception as e:
#         print("on_message error:", e)

# def run_ws():
#     url = "wss://fstream.binance.com/stream"
#     ws = websocket.WebSocketApp(
#         url,
#         on_open=lambda ws: ws.send(json.dumps({
#             "method": "SUBSCRIBE",
#             "params": [f"{SYMBOL}@kline_{INTERVAL}", f"{SYMBOL}@trade"],
#             "id": 1
#         })),
#         on_message=on_message,
#         on_error=lambda ws, err: print("WS error:", err),
#         on_close=lambda ws, code, msg: threading.Thread(target=run_ws, daemon=True).start()
#     )
#     ws.run_forever(ping_interval=20, ping_timeout=10)

# # ==========================
# # Dash + Flask Server
# # ==========================
# app = dash.Dash(__name__)
# server = app.server  # expose Flask server for Render/Heroku

# app.layout = html.Div([
#     html.H1(f"{SYMBOL.upper()} Live Prices & Last {CANDLE_LIMIT} Candles"),
#     html.H2(id="live-price"),
#     html.Div(id="ohlc-values"),
#     html.Div(id="bounds", style={"marginTop": "8px", "color": "#9ad"}),
#     html.H3("Strategy Alerts"),
#     html.Ul(id="strategy-alerts"),
#     dcc.Interval(id="interval", interval=1000, n_intervals=0)
# ])

# @app.callback(
#     [Output("live-price", "children"),
#      Output("ohlc-values", "children"),
#      Output("strategy-alerts", "children"),
#      Output("bounds", "children")],
#     [Input("interval", "n_intervals")]
# )
# def update_display(_):
#     if candles.empty:
#         return "Live Price: --", "OHLC: --", [], "Bounds: --"
#     lp = f"Live Price: {fmt_price(live_price)}"
#     ohlc_html = [html.Div(f"{row['time'].strftime('%H:%M:%S')} → "
#                           f"O:{fmt_price(row['Open'])}, H:{fmt_price(row['High'])}, "
#                           f"L:{fmt_price(row['Low'])}, C:{fmt_price(row['Close'])}") 
#                  for _, row in candles.iterrows()]
#     alerts_html = [html.Li(a) for a in alerts[-10:]]
#     btxt = f"Bounds[{LENGTH}] → Upper {fmt_price(upper_bound)}, Lower {fmt_price(lower_bound)}" if upper_bound and lower_bound else "Bounds: waiting..."
#     return lp, ohlc_html, alerts_html, btxt

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
# # Main
# # ==========================
# if __name__ == "__main__":
#     fetch_initial_candles()
#     if len(candles) >= LENGTH:
#         recompute_bounds_on_close()
#     threading.Thread(target=run_ws, daemon=True).start()
#     # threading.Thread(target=ping_loop, daemon=True).start()
#     port = int(os.environ.get("PORT", 8050))
#     app.run(host="0.0.0.0", port=port, debug=False)


# import os
# import json
# import time
# import threading
# from datetime import datetime, timezone, timedelta

# import pandas as pd
# import requests
# import websocket
# import dash
# from dash import dcc, html
# from dash.dependencies import Input, Output
# import requests
# from flask import Flask, request, jsonify

# pinger = Flask(__name__)

# # ==========================
# # Config
# # ==========================
# SYMBOL = "xrpusdc"
# INTERVAL = "5m"
# CANDLE_LIMIT = 10
# WEBHOOK_URL = "http://localhost:5000/webhook"
# PING_URL = os.environ.get("PING_URL", "https://bot-reviver.onrender.com/ping")

# LENGTH = 5  # last closed candles for bounds

# # ==========================
# # Globals
# # ==========================
# candles = pd.DataFrame(columns=["time", "Open", "High", "Low", "Close"])
# live_price = None
# last_valid_price = None
# alerts = []

# upper_bound = None
# lower_bound = None
# _bounds_candle_ts = None
# _triggered_window_id = None

# EntryCount = 0
# LastSide = None
# status = None

# # ==========================
# # Manual Seed Data (when Binance fetch fails)
# # ==========================
# manual_candles = [
#     ("04:10:00", 3.0467, 3.0520, 3.0451, 3.0493),
#     ("04:15:00", 3.0491, 3.0511, 3.0454, 3.0486),
#     ("04:20:00", 3.0482, 3.0514, 3.0469, 3.0482),
#     ("04:25:00", 3.0483, 3.0491, 3.0442, 3.0442),
#     ("04:30:00", 3.0446, 3.0473, 3.0413, 3.0439),
#     ("04:35:00", 3.0438, 3.0494, 3.0415, 3.0431),
#     ("04:40:00", 3.0428, 3.0458, 3.0420, 3.0446),
#     ("04:45:00", 3.0448, 3.0452, 3.0420, 3.0426),
#     ("04:50:00", 3.0426, 3.0483, 3.0426, 3.0483),
# ]


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
# def seed_manual_candles():
#     """Load fallback candles manually"""
#     global candles, live_price, last_valid_price
#     today = datetime.now(timezone.utc).date()
#     rows = []
#     for t, o, h, l, c in manual_candles:
#         dt = datetime.strptime(f"{today} {t}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
#         rows.append({"time": dt, "Open": o, "High": h, "Low": l, "Close": c})
#     candles = pd.DataFrame(rows)
#     live_price = candles["Close"].iloc[-1]
#     last_valid_price = live_price
#     print(f"✅ Seeded {len(candles)} manual candles")

# def fetch_initial_candles():
#     global candles, live_price, last_valid_price
#     try:
#         url = f"https://fapi.binance.com/fapi/v1/klines?symbol={SYMBOL.upper()}&interval={INTERVAL}&limit={CANDLE_LIMIT}"
#         r = requests.get(url, timeout=10)
#         r.raise_for_status()
#         data = r.json()

#         rows = []
#         for k in data:
#             rows.append({
#                 "time": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
#                 "Open": float(k[1]),
#                 "High": float(k[2]),
#                 "Low": float(k[3]),
#                 "Close": float(k[4]),
#             })
#         candles = pd.DataFrame(rows)
#         if not candles.empty:
#             live_price = float(candles["Close"].iloc[-1])
#             last_valid_price = live_price
#             print(f"✅ Binance returned {len(candles)} candles")
#             return
#     except Exception as e:
#         print("⚠️ Binance fetch failed:", e)

#     # fallback
#     seed_manual_candles()

# def send_webhook(trigger_time_iso: str, entry_price: float, side: str):
#     global EntryCount, LastSide, status
#     secret = "gajraj09"
#     quantity = 1.8

#     if EntryCount % 2 == 1:
#         status = "entry"
#     else:
#         status = "exit"
#     if EntryCount % 3 == 0:
#         status = "exit"

#     print(f"[WEBHOOK] {trigger_time_iso} | {side} | Entry: {entry_price} | {SYMBOL.upper()} | status: {status} | qty: {quantity}")
#     try:
#         payload = {
#             "symbol": SYMBOL.upper(),
#             "side": side,
#             "quantity": quantity,
#             "price": entry_price,
#             "status": status,
#             "secret": secret
#         }
#         requests.post(WEBHOOK_URL, json=payload, timeout=5)
#     except Exception as e:
#         print("Webhook error:", e)

# def recompute_bounds_on_close():
#     global upper_bound, lower_bound, _bounds_candle_ts, _triggered_window_id
#     if len(candles) < LENGTH:
#         return
#     window = candles.tail(LENGTH)
#     upper_bound = float(window["High"].max())
#     lower_bound = float(window["Low"].min())
#     _bounds_candle_ts = window["time"].iloc[-1]
#     _triggered_window_id = None
#     print(f"📊 Bounds recomputed: Upper={upper_bound}, Lower={lower_bound}")

# def try_trigger_on_trade(trade_price: float, trade_ts_ms: int):
#     global alerts, _triggered_window_id, EntryCount, LastSide, status
#     if not (is_valid_price(trade_price) and upper_bound and lower_bound and _bounds_candle_ts):
#         return
#     if _triggered_window_id == _bounds_candle_ts:
#         return
#     trigger_time = datetime.fromtimestamp(trade_ts_ms / 1000, tz=timezone.utc)
#     iso = trigger_time.isoformat()

#     if trade_price >= upper_bound:
#         side = "buy"
#         EntryCount = EntryCount + 1 if LastSide == side else 1
#         LastSide = side
#         send_webhook(iso, upper_bound, side)
#         alerts.append(f"LONG breakout {fmt_price(upper_bound)} | Live {fmt_price(trade_price)} | {iso}")
#         _triggered_window_id = _bounds_candle_ts
#     elif trade_price <= lower_bound:
#         side = "sell"
#         EntryCount = EntryCount + 1 if LastSide == side else 1
#         LastSide = side
#         send_webhook(iso, lower_bound, side)
#         alerts.append(f"SHORT breakout {fmt_price(lower_bound)} | Live {fmt_price(trade_price)} | {iso}")
#         _triggered_window_id = _bounds_candle_ts
#     alerts[:] = alerts[-50:]

# # ==========================
# # WebSocket
# # ==========================
# def on_message(ws, message):
#     global candles, live_price, last_valid_price
#     try:
#         data = json.loads(message)
#         stream, payload = data.get("stream"), data.get("data")

#         if "kline" in stream:
#             k = payload["k"]
#             ts = datetime.fromtimestamp(k["t"] / 1000, tz=timezone.utc)
#             o, h, l, c = float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"])
#             live_price = c; last_valid_price = c

#             if candles.empty or candles.iloc[-1]["time"] != ts:
#                 candles = pd.concat([candles, pd.DataFrame([{"time": ts, "Open": o, "High": h, "Low": l, "Close": c}])], ignore_index=True)
#             else:
#                 idx = candles.index[-1]
#                 candles.at[idx, "High"] = max(candles.at[idx, "High"], h)
#                 candles.at[idx, "Low"] = min(candles.at[idx, "Low"], l)
#                 candles.at[idx, "Close"] = c

#             if len(candles) > CANDLE_LIMIT:
#                 candles = candles.tail(CANDLE_LIMIT).reset_index(drop=True)
#             if k["x"]:
#                 recompute_bounds_on_close()

#         elif "trade" in stream:
#             trade_price = float(payload["p"])
#             live_price = trade_price
#             last_valid_price = trade_price
#             if not candles.empty:
#                 idx = candles.index[-1]
#                 candles.at[idx, "Close"] = trade_price
#                 candles.at[idx, "High"] = max(candles.at[idx, "High"], trade_price)
#                 candles.at[idx, "Low"] = min(candles.at[idx, "Low"], trade_price)
#             try_trigger_on_trade(trade_price, payload["T"])
#     except Exception as e:
#         print("on_message error:", e)

# def run_ws():
#     url = "wss://fstream.binance.com/stream"
#     ws = websocket.WebSocketApp(url, on_open=lambda ws: ws.send(json.dumps({
#         "method": "SUBSCRIBE", "params": [f"{SYMBOL}@kline_{INTERVAL}", f"{SYMBOL}@trade"], "id": 1
#     })), on_message=on_message, on_error=lambda ws, err: print("WS error:", err),
#         on_close=lambda ws, code, msg: threading.Thread(target=run_ws, daemon=True).start())
#     ws.run_forever(ping_interval=20, ping_timeout=10)

# # ==========================
# # Dash App
# # ==========================
# app = dash.Dash(__name__)
# app.layout = html.Div([
#     html.H1(f"{SYMBOL.upper()} Live Prices & Last {CANDLE_LIMIT} Candles"),
#     html.H2(id="live-price"),
#     html.Div(id="ohlc-values"),
#     html.Div(id="bounds", style={"marginTop": "8px", "color": "#9ad"}),
#     html.H3("Strategy Alerts"),
#     html.Ul(id="strategy-alerts"),
#     dcc.Interval(id="interval", interval=1000, n_intervals=0)
# ])

# @app.callback(
#     [Output("live-price", "children"),
#      Output("ohlc-values", "children"),
#      Output("strategy-alerts", "children"),
#      Output("bounds", "children")],
#     [Input("interval", "n_intervals")]
# )
# def update_display(_):
#     if candles.empty:
#         return "Live Price: --", "OHLC: --", [], "Bounds: --"
#     lp = f"Live Price: {fmt_price(live_price)}"
#     ohlc_html = [html.Div(f"{row['time'].strftime('%H:%M:%S')} → "
#                           f"O:{fmt_price(row['Open'])}, H:{fmt_price(row['High'])}, "
#                           f"L:{fmt_price(row['Low'])}, C:{fmt_price(row['Close'])}") 
#                  for _, row in candles.iterrows()]
#     alerts_html = [html.Li(a) for a in alerts[-10:]]
#     if upper_bound and lower_bound:
#         btxt = f"Bounds[{LENGTH}] → Upper {fmt_price(upper_bound)}, Lower {fmt_price(lower_bound)}"
#     else:
#         btxt = "Bounds: waiting..."
#     return lp, ohlc_html, alerts_html, btxt


# def keep_alive():
#     """Send a ping to the server itself."""
#     try:
#         print(f"🔄 Pinging {PING_URL}")
#         r = requests.get(PING_URL, timeout=10)
#         print("✅ Ping response:", r.status_code)
#     except Exception as e:
#         print("⚠️ Keep-alive ping failed:", str(e))


# @pinger.route('/ping', methods=['GET'])
# def ping():
#     """Simple endpoint to check server health and trigger keep-alive."""
#     keep_alive()
#     return jsonify({"status": "alive"}), 200
# # ==========================
# # Main
# # ==========================
# if __name__ == "__main__":
#     fetch_initial_candles()
#     if len(candles) >= LENGTH:
#         recompute_bounds_on_close()
#     threading.Thread(target=run_ws, daemon=True).start()
#     # app.run(debug=True, port=8050)
#     port = int(os.environ.get("PORT", 8050))
#     app.run(host="0.0.0.0", port=port, debug=False)




# import os
# import json
# import time
# import threading
# from datetime import datetime, timezone,timedelta

# import pandas as pd
# import requests
# import websocket
# import dash
# from dash import dcc, html
# from dash.dependencies import Input, Output
# import plotly.graph_objs as go
# from flask import Flask, request, jsonify

# # ==========================
# # Config
# # ==========================
# SYMBOL = "xrpusdc"  # Binance lowercase for WS
# INTERVAL = "5m"     # Binance interval
# CANDLE_LIMIT = 10   # Display last N candles
# WEBHOOK_URL = "https://binance-65gz.onrender.com/webhook"  # Replace with your webhook URL
# LENGTH = 5          # last closed candles to form bounds
# PING_URL = os.environ.get("PING_URL", "https://bot-reviver.onrender.com/ping")

# # ==========================
# # Globals
# # ==========================
# candles = pd.DataFrame(columns=["time", "Open", "High", "Low", "Close"])
# live_price = None
# last_valid_price = None
# alerts = []

# upper_bound = None
# lower_bound = None
# _bounds_candle_ts = None
# _triggered_window_id = None  # prevents duplicate alerts within one window

# #Controllers
# EntryCount = 0
# LastSide = None
# status = None

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
#             "time": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
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
#     # Pattern: 1-entry, 2-exit, 3-exit, repeat
#     cycle = ["entry", "exit", "exit"]
#     return cycle[(EntryCount - 1) % 3]

# def send_webhook(trigger_time_iso: str, entry_price: float, side: str):
#     global EntryCount, LastSide, status
#     secret = "gajraj09"
#     quantity = 1.8
#     status = get_status(EntryCount)
    
#     try:
#         payload = {
#             "symbol": "XRPUSDC",
#             "side": side,
#             "quantity": quantity,
#             "price": entry_price,
#             "status": status,
#             "secret": secret
#         }
#         requests.post(WEBHOOK_URL, json=payload, timeout=5)
#     except Exception as e:
#         print("Webhook error:", e)

# def recompute_bounds_on_close():
#     """Recompute bounds using last LENGTH CLOSED candles."""
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

# # def try_trigger_on_trade(trade_price: float, trade_ts_ms: int):
# #     """Trigger if trade price crosses bounds (one signal per window)."""
# #     global alerts, _triggered_window_id, status, EntryCount, LastSide
# #     if not (is_valid_price(trade_price) and upper_bound is not None and lower_bound is not None and _bounds_candle_ts):
# #         return
# #     if _triggered_window_id == _bounds_candle_ts:
# #         return

# #     trigger_time = datetime.fromtimestamp(trade_ts_ms / 1000, tz=timezone(timedelta(hours=5, minutes=30)))
# #     trigger_time_iso = trigger_time.isoformat().replace("+00:00", "Z")

# #     if trade_price >= upper_bound:
# #         side = "buy"
# #         if EntryCount == 0 and LastSide != side:
# #             EntryCount += 1
# #             LastSide = side
# #         elif EntryCount != 0 and LastSide == side:
# #             EntryCount += 1
# #             LastSide = side
# #         elif EntryCount != 0 and LastSide != side:
# #             EntryCount = 1
# #             LastSide = side
# #         entry = upper_bound
# #         send_webhook(trigger_time_iso, entry, side)
# #         msg = f"LONG breakout Buy | {status}: {fmt_price(entry)} | Live {fmt_price(trade_price)} | Trigger {trigger_time_iso}"
# #         alerts.append(msg); alerts[:] = alerts[-50:]
# #         _triggered_window_id = _bounds_candle_ts
# #     elif trade_price <= lower_bound:
# #         side = "sell"
# #         if EntryCount == 0 and LastSide != side:
# #             EntryCount += 1
# #             LastSide = side
# #         elif EntryCount != 0 and LastSide == side:
# #             EntryCount += 1
# #             LastSide = side
# #         elif EntryCount != 0 and LastSide != side:
# #             EntryCount = 1
# #             LastSide = side
# #         entry = lower_bound
# #         # FIXED: call signature and order (was 4 args, wrong order)
# #         send_webhook(trigger_time_iso, entry, side)
# #         msg = f"SHORT breakout Sell | {status}: {fmt_price(entry)} | Live {fmt_price(trade_price)} | Trigger {trigger_time_iso}"
# #         alerts.append(msg); alerts[:] = alerts[-50:]
# #         _triggered_window_id = _bounds_candle_ts

# def try_trigger_on_trade(trade_price: float, trade_ts_ms: int):
#     global alerts, _triggered_window_id, status, EntryCount, LastSide

#     if not is_valid_price(trade_price) or upper_bound is None or lower_bound is None:
#         return

#     if _bounds_candle_ts is None or pd.isnull(_bounds_candle_ts):
#         return

#     if _triggered_window_id == _bounds_candle_ts:
#         return

#     trigger_time = datetime.fromtimestamp(trade_ts_ms / 1000, tz=timezone(timedelta(hours=5, minutes=30)))
#     trigger_time_iso = trigger_time.isoformat().replace("+00:00", "Z")

#     # Long breakout
#     if trade_price >= upper_bound:
#         side = "buy"
#         EntryCount = EntryCount + 1 if LastSide == side else 1
#         LastSide = side
#         entry = upper_bound
#         send_webhook(trigger_time_iso, entry, side)
#         msg = f"LONG breakout Buy | {status}: {fmt_price(entry)} | Live {fmt_price(trade_price)} | Trigger {trigger_time_iso}"
#         alerts.append(msg); alerts[:] = alerts[-50:]
#         _triggered_window_id = _bounds_candle_ts
#         print("🚀 Alert triggered (LONG)")

#     # Short breakout
#     elif trade_price <= lower_bound:
#         side = "sell"
#         EntryCount = EntryCount + 1 if LastSide == side else 1
#         LastSide = side
#         entry = lower_bound
#         send_webhook(trigger_time_iso, entry, side)
#         msg = f"SHORT breakout Sell | {status}: {fmt_price(entry)} | Live {fmt_price(trade_price)} | Trigger {trigger_time_iso}"
#         alerts.append(msg); alerts[:] = alerts[-50:]
#         _triggered_window_id = _bounds_candle_ts
#         print("🚀 Alert triggered (SHORT)")


# # ==========================
# # WebSocket
# # ==========================
# def on_message(ws, message):
#     global candles, live_price, last_valid_price
#     try:
#         data = json.loads(message)
#         stream = data.get("stream")
#         payload = data.get("data")

#         # Kline updates (candle forming + close)
#         if stream and "kline" in stream:
#             kline = payload["k"]
#             ts_dt = datetime.fromtimestamp(kline["t"] / 1000, tz=timezone.utc)
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

#                 new_row = {"time": ts_dt, "Open": open_val, "High": max(high_val, open_val),
#                            "Low": min(low_val, open_val), "Close": close_val}
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

#             if kline["x"]:
#                 recompute_bounds_on_close()

#         # Trade ticks: update live price and current candle close
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
#     ws = websocket.WebSocketApp(url,
#                                 on_open=on_open,
#                                 on_message=on_message,
#                                 on_error=on_error,
#                                 on_close=on_close)
#     ws.run_forever(ping_interval=20, ping_timeout=10)

# # ==========================
# # Flask + Dash Setup
# # ==========================
# server = Flask(__name__)  # Flask server
# app = dash.Dash(__name__, server=server)  # Dash app on top of Flask

# # ==========================
# # Dash Layout
# # ==========================
# app.layout = html.Div([
#     html.H1(f"{SYMBOL.upper()} Live Candlestick (Interval: {INTERVAL})"),
#     dcc.Graph(id="candlestick"),
#     html.H2(id="live-price", style={"color": "black"}),
#     html.Div(id="ohlc-values"),
#     html.Div(id="bounds", style={"marginTop": "8px", "color": "#9ad"}),
#     html.H3("Strategy Alerts"),
#     html.Ul(id="strategy-alerts"),
#     dcc.Interval(id="interval", interval=1000, n_intervals=0)
# ])

# @app.callback(
#     [Output("candlestick", "figure"),
#      Output("live-price", "children"),
#      Output("ohlc-values", "children"),
#      Output("strategy-alerts", "children"),
#      Output("bounds", "children")],
#     [Input("interval", "n_intervals")]
# )
# def update_graph(_):
#     if len(candles) == 0:
#         return go.Figure(), "Live Price: --", "OHLC: --", [], "Bounds: --"

#     fig = go.Figure(data=[go.Candlestick(
#         x=candles["time"],
#         open=candles["Open"],
#         high=candles["High"],
#         low=candles["Low"],
#         close=candles["Close"],
#         increasing_line_color='green',
#         decreasing_line_color='red'
#     )])
#     fig.update_layout(xaxis_rangeslider_visible=False, yaxis=dict(autorange=True), template="plotly_dark")

#     if upper_bound is not None and lower_bound is not None:
#         fig.add_hline(y=upper_bound, line_dash="dot", line_color="lime",
#                       annotation_text=f"Upper {fmt_price(upper_bound)}", annotation_position="top left")
#         fig.add_hline(y=lower_bound, line_dash="dot", line_color="red",
#                       annotation_text=f"Lower {fmt_price(lower_bound)}", annotation_position="bottom left")

#     last = candles.iloc[-1]
#     ohlc_text = (f"OHLC → O:{fmt_price(last['Open'])}, "
#                  f"H:{fmt_price(last['High'])}, "
#                  f"L:{fmt_price(last['Low'])}, "
#                  f"C:{fmt_price(last['Close'])}")
#     alerts_html = [html.Li(a) for a in alerts[-10:]]
#     lp = f"Live Price: {fmt_price(live_price)}"

#     if upper_bound is not None and lower_bound is not None and _bounds_candle_ts is not None:
#         btxt = (f"Bounds[{LENGTH}] → Upper {fmt_price(upper_bound)}, Lower {fmt_price(lower_bound)} "
#                 f"(from candle {_bounds_candle_ts.isoformat().replace('+00:00','Z')})")
#     else:
#         btxt = "Bounds: waiting for enough closed candles..."

#     return fig, lp, ohlc_text, alerts_html, btxt

# # ==========================
# # Keep-alive periodic ping
# # ==========================
# def keep_alive_loop(interval_sec=300):
#     while True:
#         try:
#             print(f"🔄 Pinging {PING_URL}")
#             r = requests.get(PING_URL, timeout=10)
#             print("✅ Ping response:", r.status_code)
#         except Exception as e:
#             print("⚠️ Keep-alive ping failed:", str(e))
#         time.sleep(interval_sec)

# # ==========================
# # Flask route for manual ping
# # ==========================
# @server.route("/ping", methods=["GET"])
# def ping():
#     return jsonify({"status": "alive"}), 200

# # ==========================
# # Main
# # ==========================
# if __name__ == "__main__":
#     try:
#         fetch_initial_candles()
#     except Exception as e:
#         print("Initial fetch failed:", e)
#     if len(candles) >= LENGTH:
#         recompute_bounds_on_close()

#     # Start WebSocket in background
#     threading.Thread(target=run_ws, daemon=True).start()
#     # Start keep-alive ping loop in background
#     threading.Thread(target=keep_alive_loop, daemon=True).start()

#     port = int(os.environ.get("PORT", 8050))
#     server.run(host="0.0.0.0", port=port, debug=False)


# import os
# import json
# import time
# import threading
# from datetime import datetime, timezone

# import pandas as pd
# import requests
# import websocket
# import dash
# from dash import dcc, html
# from dash.dependencies import Input, Output
# import plotly.graph_objs as go
# from flask import Flask, request, jsonify

# # ==========================
# # Config
# # ==========================
# SYMBOL = "xrpusdc"                     # Binance lowercase for WS
# INTERVAL = "5m"                        # Binance interval
# CANDLE_LIMIT = 10                     # Display last N candles
# WEBHOOK_URL = "http://localhost:5000/webhook"  # Replace with your webhook URL

# # Strategy params
# LENGTH = 5  # last closed candles to form bounds

# # ==========================
# # Globals
# # ==========================
# candles = pd.DataFrame(columns=["time", "Open", "High", "Low", "Close"])
# live_price = None
# last_valid_price = None
# alerts = []

# upper_bound = None
# lower_bound = None
# _bounds_candle_ts = None
# _triggered_window_id = None  # prevents duplicate alerts within one window

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
#             "time": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
#             "Open": float(k[1]),
#             "High": float(k[2]),
#             "Low": float(k[3]),
#             "Close": float(k[4]),
#         })
#     candles = pd.DataFrame(rows)
#     if not candles.empty:
#         live_price = float(candles["Close"].iloc[-1])
#         last_valid_price = live_price if is_valid_price(live_price) else None

# def send_webhook(message: str, trigger_time_iso: str, entry_price: float, side: str):
#     try:
#         payload = {
#             "symbol": "XRPUSDC",
#             "alert": message,
#             "side": side,
#             "entry_price": entry_price,
#         }
#         requests.post(WEBHOOK_URL, json=payload, timeout=5)
#     except Exception as e:
#         print("Webhook error:", e)

# def recompute_bounds_on_close():
#     """Recompute bounds using last LENGTH CLOSED candles."""
#     global upper_bound, lower_bound, _bounds_candle_ts, _triggered_window_id
#     if len(candles) < LENGTH:
#         upper_bound = None
#         lower_bound = None
#         _bounds_candle_ts = None
#         _triggered_window_id = None
#         return

#     window = candles.tail(LENGTH)
#     # ensure bounds use valid prices only
#     highs = window["High"][window["High"] > 0]
#     lows = window["Low"][window["Low"] > 0]
#     if highs.empty or lows.empty:
#         return

#     upper_bound = float(highs.max())
#     lower_bound = float(lows.min())
#     _bounds_candle_ts = window["time"].iloc[-1]
#     _triggered_window_id = None  # reset guard for new window

# def try_trigger_on_trade(trade_price: float, trade_ts_ms: int):
#     """Trigger if trade price crosses bounds (one signal per window)."""
#     global alerts, _triggered_window_id
#     if not (is_valid_price(trade_price) and upper_bound is not None and lower_bound is not None and _bounds_candle_ts):
#         return
#     if _triggered_window_id == _bounds_candle_ts:
#         return

#     trigger_time = datetime.fromtimestamp(trade_ts_ms / 1000, tz=timezone.utc)
#     trigger_time_iso = trigger_time.isoformat().replace("+00:00", "Z")

#     if trade_price >= upper_bound:
#         entry = upper_bound
#         msg = f"LONG breakout | Entry {fmt_price(entry)} | Live {fmt_price(trade_price)} | Trigger {trigger_time_iso}"
#         alerts.append(msg); alerts[:] = alerts[-50:]
#         send_webhook(msg, trigger_time_iso, entry, "LONG")
#         _triggered_window_id = _bounds_candle_ts
#     elif trade_price <= lower_bound:
#         entry = lower_bound
#         msg = f"SHORT breakout | Entry {fmt_price(entry)} | Live {fmt_price(trade_price)} | Trigger {trigger_time_iso}"
#         alerts.append(msg); alerts[:] = alerts[-50:]
#         send_webhook(msg, trigger_time_iso, entry, "SHORT")
#         _triggered_window_id = _bounds_candle_ts

# # ==========================
# # WebSocket
# # ==========================
# def on_message(ws, message):
#     global candles, live_price, last_valid_price
#     try:
#         data = json.loads(message)
#         stream = data.get("stream")
#         payload = data.get("data")

#         # Kline updates (candle forming + close)
#         if stream and "kline" in stream:
#             kline = payload["k"]
#             ts_dt = datetime.fromtimestamp(kline["t"] / 1000, tz=timezone.utc)
#             o, h, l, c = float(kline["o"]), float(kline["h"]), float(kline["l"]), float(kline["c"])

#             # Keep last_valid_price current if c is valid
#             if is_valid_price(c):
#                 last_valid_price = c
#                 live_price = c  # latest kline 'c' is a good approximation between trades

#             # New candle row?
#             if candles.empty or candles.iloc[-1]["time"] != ts_dt:
#                 # Seed with safe values to avoid zeros
#                 open_val = o if is_valid_price(o) else (last_valid_price if last_valid_price is not None else None)
#                 if open_val is None:
#                     # if we truly have no valid price, skip this kline update
#                     return
#                 high_val = h if is_valid_price(h) else open_val
#                 low_val = l if is_valid_price(l) else open_val
#                 close_val = c if is_valid_price(c) else open_val

#                 new_row = {"time": ts_dt, "Open": open_val, "High": max(high_val, open_val),
#                            "Low": min(low_val, open_val), "Close": close_val}
#                 candles = pd.concat([candles, pd.DataFrame([new_row])], ignore_index=True)
#             else:
#                 # Update current forming candle safely (ignore zero/invalid)
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

#             # On candle close: recompute bounds
#             if kline["x"]:
#                 recompute_bounds_on_close()

#         # Trade ticks: update live price and current candle close (tick-by-tick)
#         elif stream and "trade" in stream:
#             trade_price_raw = payload.get("p")
#             if not is_valid_price(trade_price_raw):
#                 return  # ignore invalid/zero trades
#             trade_price = float(trade_price_raw)
#             live_price = trade_price
#             last_valid_price = trade_price

#             if not candles.empty:
#                 idx = candles.index[-1]
#                 # Ensure the candle's High/Low reflect the tick as well
#                 candles.at[idx, "Close"] = trade_price
#                 candles.at[idx, "High"] = max(candles.at[idx, "High"], trade_price)
#                 candles.at[idx, "Low"] = min(candles.at[idx, "Low"], trade_price)

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
#     ws = websocket.WebSocketApp(url,
#                                 on_open=on_open,
#                                 on_message=on_message,
#                                 on_error=on_error,
#                                 on_close=on_close)
#     ws.run_forever(ping_interval=20, ping_timeout=10)

# # ==========================
# # Flask + Dash Setup
# # ==========================
# server = Flask(__name__)  # Flask server
# app = dash.Dash(__name__, server=server)  # Dash app on top of Flask

# # ==========================
# # Dash Layout
# # ==========================
# app.layout = html.Div([
#     html.H1(f"{SYMBOL.upper()} Live Candlestick (Interval: {INTERVAL})"),
#     dcc.Graph(id="candlestick"),
#     html.H2(id="live-price", style={"color": "black"}),
#     html.Div(id="ohlc-values"),
#     html.Div(id="bounds", style={"marginTop": "8px", "color": "#9ad"}),
#     html.H3("Strategy Alerts"),
#     html.Ul(id="strategy-alerts"),
#     dcc.Interval(id="interval", interval=1000, n_intervals=0)
# ])

# @app.callback(
#     [Output("candlestick", "figure"),
#      Output("live-price", "children"),
#      Output("ohlc-values", "children"),
#      Output("strategy-alerts", "children"),
#      Output("bounds", "children")],
#     [Input("interval", "n_intervals")]
# )
# def update_graph(_):
#     if len(candles) == 0:
#         return go.Figure(), "Live Price: --", "OHLC: --", [], "Bounds: --"

#     fig = go.Figure(data=[go.Candlestick(
#         x=candles["time"],
#         open=candles["Open"],
#         high=candles["High"],
#         low=candles["Low"],
#         close=candles["Close"],
#         increasing_line_color='green',
#         decreasing_line_color='red'
#     )])
#     fig.update_layout(
#         xaxis_rangeslider_visible=False,
#         yaxis=dict(autorange=True),
#         template="plotly_dark"
#     )

#     if upper_bound is not None and lower_bound is not None:
#         fig.add_hline(y=upper_bound, line_dash="dot", line_color="lime",
#                       annotation_text=f"Upper {fmt_price(upper_bound)}", annotation_position="top left")
#         fig.add_hline(y=lower_bound, line_dash="dot", line_color="red",
#                       annotation_text=f"Lower {fmt_price(lower_bound)}", annotation_position="bottom left")

#     last = candles.iloc[-1]
#     ohlc_text = (f"OHLC → O:{fmt_price(last['Open'])}, "
#                  f"H:{fmt_price(last['High'])}, "
#                  f"L:{fmt_price(last['Low'])}, "
#                  f"C:{fmt_price(last['Close'])}")
#     alerts_html = [html.Li(a) for a in alerts[-10:]]
#     lp = f"Live Price: {fmt_price(live_price)}"

#     if upper_bound is not None and lower_bound is not None and _bounds_candle_ts is not None:
#         btxt = (f"Bounds[{LENGTH}] → Upper {fmt_price(upper_bound)}, Lower {fmt_price(lower_bound)} "
#                 f"(from candle {_bounds_candle_ts.isoformat().replace('+00:00','Z')})")
#     else:
#         btxt = "Bounds: waiting for enough closed candles..."

#     return fig, lp, ohlc_text, alerts_html, btxt


# # ==========================
# # Keep-alive periodic ping
# # ==========================
# # def keep_alive_loop(interval_sec=300):
# #     while True:
# #         try:
# #             print(f"🔄 Pinging {PING_URL}")
# #             r = requests.get(PING_URL, timeout=10)
# #             print("✅ Ping response:", r.status_code)
# #         except Exception as e:
# #             print("⚠️ Keep-alive ping failed:", str(e))
# #         time.sleep(interval_sec)

# # # ==========================
# # # Flask route for manual ping
# # # ==========================
# # @server.route("/ping", methods=["GET"])
# # def ping():
# #     return jsonify({"status": "alive"}), 200

# # ==========================
# # Main
# # ==========================
# # if __name__ == "__main__":
# #     try:
# #         fetch_initial_candles()
# #     except Exception as e:
# #         print("Initial fetch failed:", e)
# #     if len(candles) >= LENGTH:
# #         recompute_bounds_on_close()

# #     # Start WebSocket in background
# #     threading.Thread(target=run_ws, daemon=True).start()
# #     # Start keep-alive ping loop in background
# #     threading.Thread(target=keep_alive_loop, daemon=True).start()

# #     port = int(os.environ.get("PORT", 8050))
# #     server.run(host="0.0.0.0", port=port, debug=False)


# if __name__ == "__main__":
#     try:
#         fetch_initial_candles()  # <- ensures last N candles are loaded
#     except Exception as e:
#         print("Initial fetch failed:", e)

#     if len(candles) >= LENGTH:
#         recompute_bounds_on_close()

#     threading.Thread(target=run_ws, daemon=True).start()
#     port = int(os.environ.get("PORT", 8050))
#     server.run(host="0.0.0.0", port=port, debug=True)
