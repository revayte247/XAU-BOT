import os
import time
import threading
import requests
import yfinance as yf
import pandas as pd
import numpy as np
from flask import Flask
from datetime import datetime

# ===== CONFIG =====
VERSION = "V12.1 LIVE"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SYMBOL = "GC=F"  # Gold futures = XAU/USD
CHECK_INTERVAL = 120  # seconds, every 2 min

# V12.1 RSI RANGES - WIDENED
BUY_RSI_MIN = 30
BUY_RSI_MAX = 50
SELL_RSI_MIN = 50
SELL_RSI_MAX = 68

TP_PIPS = 30
SL_PIPS = 20
BE_TRIGGER = 15  # move to breakeven after +15

# Stats
stats = {"tp": 0, "sl": 0, "be": 0, "last_check": "Never", "last_price": 0, "open_trade": None}

app = Flask(__name__)

@app.route("/")
def home():
    return f"XAU BOT {VERSION} - Running"

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def get_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def fetch_data():
    # 5m for entry
    df5 = yf.download(SYMBOL, period="2d", interval="5m", progress=False, auto_adjust=True)
    # 1h for trend filter
    df1h = yf.download(SYMBOL, period="10d", interval="1h", progress=False, auto_adjust=True)
    if df5.empty or df1h.empty:
        return None, None
    # flatten if MultiIndex
    if isinstance(df5.columns, pd.MultiIndex):
        df5.columns = df5.columns.get_level_values(0)
    if isinstance(df1h.columns, pd.MultiIndex):
        df1h.columns = df1h.columns.get_level_values(0)
    return df5, df1h

def check_trade():
    global stats
    df5, df1h = fetch_data()
    if df5 is None:
        print("No data")
        return

    close5 = df5["Close"]
    vol = df5["Volume"]
    price = float(close5.iloc[-1])

    rsi_series = get_rsi(close5, 14)
    rsi = float(rsi_series.iloc[-1])

    # ROC - Rate of Change last 3 candles
    roc = float((close5.iloc[-1] - close5.iloc[-4]) / close5.iloc[-4] * 100) if len(close5) > 4 else 0

    # Volume filter - current vs avg 20
    vol_now = float(vol.iloc[-1]) if len(vol) > 0 else 0
    vol_avg = float(vol.tail(20).mean()) if len(vol) > 0 else 1
    vol_ok = vol_now >= vol_avg * 0.5
    vol_thresh = int(vol_avg)

    # HTF - 1H EMA200
    df1h["EMA200"] = df1h["Close"].ewm(span=200, adjust=False).mean()
    ema200_1h = float(df1h["EMA200"].iloc[-1])
    htf_buy = price > ema200_1h
    htf_sell = price < ema200_1h

    stats["last_check"] = datetime.utcnow().strftime("%H:%M:%S")
    stats["last_price"] = price

    print(f"CHECK {price:.2f} RSI {rsi:.1f} ROC {roc:.4f} VOL {int(vol_now)}/{vol_thresh} OK={vol_ok} HTF B={htf_buy} S={htf_sell}", flush=True)

    # If trade open, manage BE / TP / SL (simulated tracking)
    if stats["open_trade"]:
        t = stats["open_trade"]
        if t["side"] == "BUY":
            profit = price - t["entry"]
        else:
            profit = t["entry"] - price

        # Breakeven
        if not t["be_done"] and profit >= BE_TRIGGER:
            t["be_done"] = True
            t["sl_price"] = t["entry"]
            send_telegram(f"🔒 BREAKEVEN\n{t['side']} @ {t['entry']:.2f} -> BE at {price:.2f}")
            print("BREAKEVEN hit", flush=True)

        # TP
        if profit >= TP_PIPS:
            stats["tp"] += 1
            stats["open_trade"] = None
            send_telegram(f"✅ TP HIT {t['side']} +{TP_PIPS}\nEntry {t['entry']:.2f} Exit {price:.2f}\nScore TP:{stats['tp']} SL:{stats['sl']} BE:{stats['be']}")
            return
        # SL (or BE SL)
        if profit <= -SL_PIPS or (t["be_done"] and profit <= 0):
            if t["be_done"]:
                stats["be"] += 1
                send_telegram(f"🔒 BREAKEVEN CLOSED {t['side']}\nEntry {t['entry']:.2f} Exit {price:.2f} = $0")
            else:
                stats["sl"] += 1
                send_telegram(f"❌ SL HIT {t['side']} -{SL_PIPS}\nEntry {t['entry']:.2f} Exit {price:.2f}\nScore TP:{stats['tp']} SL:{stats['sl']} BE:{stats['be']}")
            stats["open_trade"] = None
            return
        return  # don't open new trade while one is open

    # No open trade - look for entry
    # V12.1 LOGIC
    buy_signal = (BUY_RSI_MIN <= rsi <= BUY_RSI_MAX) and htf_buy and vol_ok
    sell_signal = (SELL_RSI_MIN <= rsi <= SELL_RSI_MAX) and htf_sell and vol_ok

    if buy_signal:
        stats["open_trade"] = {"side": "BUY", "entry": price, "be_done": False, "sl_price": price - SL_PIPS}
        send_telegram(f"🟢 BUY SIGNAL V12.1\nPrice {price:.2f} RSI {rsi:.1f} HTF B={htf_buy}\nTP +{TP_PIPS} SL -{SL_PIPS} BE +{BE_TRIGGER}")
        print("OPEN BUY", flush=True)
    elif sell_signal:
        stats["open_trade"] = {"side": "SELL", "entry": price, "be_done": False, "sl_price": price + SL_PIPS}
        send_telegram(f"🔴 SELL SIGNAL V12.1\nPrice {price:.2f} RSI {rsi:.1f} HTF S={htf_sell}\nTP +{TP_PIPS} SL -{SL_PIPS} BE +{BE_TRIGGER}")
        print("OPEN SELL", flush=True)

def bot_loop():
    time.sleep(5)
    send_telegram(f"🤖 XAU BOT {VERSION}\nNo trade - Waiting for setup\nRSI BUY {BUY_RSI_MIN}-{BUY_RSI_MAX} | SELL {SELL_RSI_MIN}-{SELL_RSI_MAX}\nHTF: 1H EMA200")
    while True:
        try:
            check_trade()
        except Exception as e:
            print(f"Loop error: {e}", flush=True)
        time.sleep(CHECK_INTERVAL)

def telegram_poll():
    # Simple /start responder
    offset = 0
    while True:
        try:
            if not TELEGRAM_TOKEN:
                time.sleep(10)
                continue
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={offset}&timeout=20"
            r = requests.get(url, timeout=30).json()
            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                text = msg.get("text", "")
                if text == "/start":
                    total = stats["tp"] + stats["sl"] + stats["be"]
                    reply = (f"🤖 XAU BOT {VERSION}\n"
                             f"Price: {stats['last_price']:.2f}\n"
                             f"Last: {stats['last_check']} UTC\n"
                             f"Score TP:{stats['tp']} SL:{stats['sl']} BE:{stats['be']} Total:{total}\n"
                             f"Open: {stats['open_trade']['side'] if stats['open_trade'] else 'No trade'}")
                    send_telegram(reply)
        except Exception as e:
            print(f"Poll error: {e}")
        time.sleep(2)

if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    threading.Thread(target=telegram_poll, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
