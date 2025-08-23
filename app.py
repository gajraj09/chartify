import os
import json
import time
import threading
from datetime import datetime, timezone

import pandas as pd
import requests
import websocket
import dash
from dash import dcc, html
from dash.dependencies import Input, Output
import plotly.graph_objs as go
from flask import Flask, request, jsonify
app = Flask(__name__)

# ==========================
# Config
# ==========================
SYMBOL = "xrpusdc"                     # Binance lowercase for WS
INTERVAL = "5m"                        # Binance interval
CANDLE_LIMIT = 10                     # Display last N candles
WEBHOOK_URL = "http://localhost:5000/webhook"  # Replace with your webhook URL

LENGTH = 5  # last closed candles to form bounds

PING_URL = os.environ.get("PING_URL", "https://bot-reviver.onrender.com/ping")

# ==========================
# Globals
# ==========================
candles = pd.DataFrame(columns=["time", "Open", "High", "Low", "Close"])
live_price = None
last_valid_price = None
alerts = []

upper_bound = None
lower_bound = None
_bounds_candle_ts = None
_triggered_window_id = None  # prevents duplicate alerts within one window

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
            "time": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
            "Open": float(k[1]),
            "High": float(k[2]),
            "Low": float(k[3]),
            "Close": float(k[4]),
        })
    candles = pd.DataFrame(rows)
    if not candles.empty:
        live_price = float(candles["Close"].iloc[-1])
        last_valid_price = live_price if is_valid_price(live_price) else None

def send_webhook(message: str, trigger_time_iso: str, entry_price: float, side: str):
    try:
        payload = {
            "symbol": "XRPUSDC",
            "alert": message,
            "side": side,
            "entry_price": entry_price,
        }
        requests.post(WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        print("Webhook error:", e)

def recompute_bounds_on_close():
    """Recompute bounds using last LENGTH CLOSED candles."""
    global upper_bound, lower_bound, _bounds_candle_ts, _triggered_window_id
    if len(candles) < LENGTH:
        upper_bound = None
        lower_bound = None
        _bounds_candle_ts = None
        _triggered_window_id = None
        return

    window = candles.tail(LENGTH)
    # ensure bounds use valid prices only
    highs = window["High"][window["High"] > 0]
    lows = window["Low"][window["Low"] > 0]
    if highs.empty or lows.empty:
        return

    upper_bound = float(highs.max())
    lower_bound = float(lows.min())
    _bounds_candle_ts = window["time"].iloc[-1]
    _triggered_window_id = None  # reset guard for new window

def try_trigger_on_trade(trade_price: float, trade_ts_ms: int):
    """Trigger if trade price crosses bounds (one signal per window)."""
    global alerts, _triggered_window_id
    if not (is_valid_price(trade_price) and upper_bound is not None and lower_bound is not None and _bounds_candle_ts):
        return
    if _triggered_window_id == _bounds_candle_ts:
        return

    trigger_time = datetime.fromtimestamp(trade_ts_ms / 1000, tz=timezone.utc)
    trigger_time_iso = trigger_time.isoformat().replace("+00:00", "Z")

    if trade_price >= upper_bound:
        entry = upper_bound
        msg = f"LONG breakout | Entry {fmt_price(entry)} | Live {fmt_price(trade_price)} | Trigger {trigger_time_iso}"
        alerts.append(msg); alerts[:] = alerts[-50:]
        send_webhook(msg, trigger_time_iso, entry, "LONG")
        _triggered_window_id = _bounds_candle_ts
    elif trade_price <= lower_bound:
        entry = lower_bound
        msg = f"SHORT breakout | Entry {fmt_price(entry)} | Live {fmt_price(trade_price)} | Trigger {trigger_time_iso}"
        alerts.append(msg); alerts[:] = alerts[-50:]
        send_webhook(msg, trigger_time_iso, entry, "SHORT")
        _triggered_window_id = _bounds_candle_ts

# ==========================
# WebSocket
# ==========================
def on_message(ws, message):
    global candles, live_price, last_valid_price
    try:
        data = json.loads(message)
        stream = data.get("stream")
        payload = data.get("data")

        # Kline updates (candle forming + close)
        if stream and "kline" in stream:
            kline = payload["k"]
            ts_dt = datetime.fromtimestamp(kline["t"] / 1000, tz=timezone.utc)
            o, h, l, c = float(kline["o"]), float(kline["h"]), float(kline["l"]), float(kline["c"])

            # Keep last_valid_price current if c is valid
            if is_valid_price(c):
                last_valid_price = c
                live_price = c  # latest kline 'c' is a good approximation between trades

            # New candle row?
            if candles.empty or candles.iloc[-1]["time"] != ts_dt:
                # Seed with safe values to avoid zeros
                open_val = o if is_valid_price(o) else (last_valid_price if last_valid_price is not None else None)
                if open_val is None:
                    # if we truly have no valid price, skip this kline update
                    return
                high_val = h if is_valid_price(h) else open_val
                low_val = l if is_valid_price(l) else open_val
                close_val = c if is_valid_price(c) else open_val

                new_row = {"time": ts_dt, "Open": open_val, "High": max(high_val, open_val),
                           "Low": min(low_val, open_val), "Close": close_val}
                candles = pd.concat([candles, pd.DataFrame([new_row])], ignore_index=True)
            else:
                # Update current forming candle safely (ignore zero/invalid)
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

            # On candle close: recompute bounds
            if kline["x"]:
                recompute_bounds_on_close()

        # Trade ticks: update live price and current candle close (tick-by-tick)
        elif stream and "trade" in stream:
            trade_price_raw = payload.get("p")
            if not is_valid_price(trade_price_raw):
                return  # ignore invalid/zero trades
            trade_price = float(trade_price_raw)
            live_price = trade_price
            last_valid_price = trade_price

            if not candles.empty:
                idx = candles.index[-1]
                # Ensure the candle's High/Low reflect the tick as well
                candles.at[idx, "Close"] = trade_price
                candles.at[idx, "High"] = max(candles.at[idx, "High"], trade_price)
                candles.at[idx, "Low"] = min(candles.at[idx, "Low"], trade_price)

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
    ws = websocket.WebSocketApp(url,
                                on_open=on_open,
                                on_message=on_message,
                                on_error=on_error,
                                on_close=on_close)
    ws.run_forever(ping_interval=20, ping_timeout=10)

# ==========================
# Dash App
# ==========================
app = dash.Dash(__name__)
app.layout = html.Div([
    html.H1(f"{SYMBOL.upper()} Live Candlestick (Interval: {INTERVAL})"),
    dcc.Graph(id="candlestick"),
    html.H2(id="live-price", style={"color": "black"}),
    html.Div(id="ohlc-values"),
    html.Div(id="bounds", style={"marginTop": "8px", "color": "#9ad"}),
    html.H3("Strategy Alerts"),
    html.Ul(id="strategy-alerts"),
    dcc.Interval(id="interval", interval=1000, n_intervals=0)
])

@app.callback(
    [Output("candlestick", "figure"),
     Output("live-price", "children"),
     Output("ohlc-values", "children"),
     Output("strategy-alerts", "children"),
     Output("bounds", "children")],
    [Input("interval", "n_intervals")]
)
def update_graph(_):
    if len(candles) == 0:
        return go.Figure(), "Live Price: --", "OHLC: --", [], "Bounds: --"

    fig = go.Figure(data=[go.Candlestick(
        x=candles["time"],
        open=candles["Open"],
        high=candles["High"],
        low=candles["Low"],
        close=candles["Close"],
        increasing_line_color='green',
        decreasing_line_color='red'
    )])
    fig.update_layout(
        xaxis_rangeslider_visible=False,
        yaxis=dict(autorange=True),
        template="plotly_dark"
    )

    if upper_bound is not None and lower_bound is not None:
        fig.add_hline(y=upper_bound, line_dash="dot", line_color="lime",
                      annotation_text=f"Upper {fmt_price(upper_bound)}", annotation_position="top left")
        fig.add_hline(y=lower_bound, line_dash="dot", line_color="red",
                      annotation_text=f"Lower {fmt_price(lower_bound)}", annotation_position="bottom left")

    last = candles.iloc[-1]
    ohlc_text = (f"OHLC → O:{fmt_price(last['Open'])}, "
                 f"H:{fmt_price(last['High'])}, "
                 f"L:{fmt_price(last['Low'])}, "
                 f"C:{fmt_price(last['Close'])}")
    alerts_html = [html.Li(a) for a in alerts[-10:]]
    lp = f"Live Price: {fmt_price(live_price)}"

    if upper_bound is not None and lower_bound is not None and _bounds_candle_ts is not None:
        btxt = (f"Bounds[{LENGTH}] → Upper {fmt_price(upper_bound)}, Lower {fmt_price(lower_bound)} "
                f"(from candle {_bounds_candle_ts.isoformat().replace('+00:00','Z')})")
    else:
        btxt = "Bounds: waiting for enough closed candles..."

    return fig, lp, ohlc_text, alerts_html, btxt

# ==========================
# Keep-alive functionality
# ==========================
def keep_alive():
    """Send a ping to the server itself."""
    try:
        print(f"🔄 Pinging {PING_URL}")
        r = requests.get(PING_URL, timeout=10)
        print("✅ Ping response:", r.status_code)
    except Exception as e:
        print("⚠️ Keep-alive ping failed:", str(e))

@flask_app.route("/ping", methods=["GET"])
def ping():
    """Simple endpoint to check server health and trigger keep-alive."""
    keep_alive()
    return jsonify({"status": "alive"}), 200

# ==========================
# Main
# ==========================
if __name__ == "__main__":
    try:
        fetch_initial_candles()
    except Exception as e:
        print("Initial fetch failed:", e)
    if len(candles) >= LENGTH:
        recompute_bounds_on_close()
    threading.Thread(target=run_ws, daemon=True).start()
    port = int(os.environ.get("PORT", 8050))
    app.run(host="0.0.0.0", port=port, debug=False)
