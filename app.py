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
INTERVAL = "10m"
CANDLE_LIMIT = 10
PING_URL = os.environ.get("PING_URL", "https://bot-reviver.onrender.com/ping")
WEBHOOK_URL = "https://www.example.com/webhook"

LENGTH = 1  # last closed candles for bounds

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

initail_balance = 10.0
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
