import os
import time
import threading
import requests
import yfinance as yf
import pandas as pd
import numpy as np
from flask import Flask
from datetime import datetime

VERSION = "V12.4 LIVE"
# FIX: Accept BOTH env var names
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "") or os.getenv("TELEGRAM_BOT_TOKEN", "") or os.getenv("BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "") or os.getenv("TELEGRAM_BOT_CHAT_ID", "") or os.getenv("CHAT_ID", "")

SYMBOL = "GC=F"
SPOT_OFFSET = 10.0  # GC=F Futures is ~$10 above XAUUSD Spot, subtract it to match broker
CHECK_INTERVAL = 120

BUY_RSI_MIN = 30
BUY_RSI_MAX = 50
SELL_RSI_MIN = 50
SELL_RSI_MAX = 68

TP_PIPS = 40
SL_PIPS = 20
BE_TRIGGER = 15

stats = {"tp": 0, "sl": 0, "be": 0, "last_check": "Never", "last_price": 0, "open_trade": None}

app = Flask(__name__)

@app.route("/")
def home():
    return f"XAU BOT {VERSION} - Running"

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"Telegram skipped - Token set={bool(TELEGRAM_TOKEN)} Chat set={bool(TELEGRAM_CHAT_ID)}", flush=True)
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}", flush=True)

def get_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def fetch_data():
    df5 = yf.download(SYMBOL, period="2d", interval="5m", progress=False, auto_adjust=True)
    df1h = yf.download(SYMBOL, period="10d", interval="1h", progress=False, auto_adjust=True)
    if df5.empty or df1h.empty:
        return None, None
    if isinstance(df5.columns, pd.MultiIndex):
        df5.columns = df5.columns.get_level_values(0)
    if isinstance(df1h.columns, pd.MultiIndex):
        df1h.columns = df1h.columns.get_level_values(0)
    return df5, df1h

def check_trade():
    df5, df1h = fetch_data()
    if df5 is None:
        print("No data", flush=True)
        return
    close5 = df5["Close"]
    vol = df5["Volume"]
    # Convert Futures price to Spot price
    price_raw = float(close5.iloc[-1])
    price = price_raw - SPOT_OFFSET
    rsi = float(get_rsi(close5, 14).iloc[-1])
    roc = float((close5.iloc[-1] - close5.iloc[-4]) / close5.iloc[-4] * 100) if len(close5) > 4 else 0
    vol_now = float(vol.iloc[-1]) if len(vol) > 0 else 0
    vol_avg = float(vol.tail(20).mean()) if len(vol) > 0 else 1
    vol_ok = vol_now >= vol_avg * 0.5
    vol_thresh = int(vol_avg)
    df1h["EMA200"] = df1h["Close"].ewm(span=200, adjust=False).mean()
    ema200_1h = float(df1h["EMA200"].iloc[-1]) - SPOT_OFFSET
    htf_buy = price > ema200_1h
    htf_sell = price < ema200_1h
    stats["last_check"] = datetime.utcnow().strftime("%H:%M:%S")
    stats["last_price"] = price
    print(f"CHECK {price:.2f} (GC {price_raw:.2f} - {SPOT_OFFSET}) RSI {rsi:.1f} ROC {roc:.4f} VOL {int(vol_now)}/{vol_thresh} OK={vol_ok} HTF B={htf_buy} S={htf_sell}", flush=True)

    if stats["open_trade"]:
        t = stats["open_trade"]
        profit = (price - t["entry"]) if t["side"] == "BUY" else (t["entry"] - price)
        if not t["be_done"] and profit >= BE_TRIGGER:
            t["be_done"] = True
            send_telegram(f"🔒 BREAKEVEN\n{t['side']} @ {t['entry']:.2f} -> BE at {price:.2f}")
        if profit >= TP_PIPS:
            stats["tp"] += 1
            stats["open_trade"] = None
            send_telegram(f"✅ TP HIT {t['side']} +{TP_PIPS}\nEntry {t['entry']:.2f} Exit {price:.2f}\nScore TP:{stats['tp']} SL:{stats['sl']} BE:{stats['be']}")
            return
        if profit <= -SL_PIPS or (t["be_done"] and profit <= 0):
            if t["be_done"]:
                stats["be"] += 1
                send_telegram(f"🔒 BREAKEVEN CLOSED {t['side']}\nEntry {t['entry']:.2f} Exit {price:.2f} = $0")
            else:
                stats["sl"] += 1
                send_telegram(f"❌ SL HIT {t['side']} -{SL_PIPS}\nEntry {t['entry']:.2f} Exit {price:.2f}\nScore TP:{stats['tp']} SL:{stats['sl']} BE:{stats['be']}")
            stats["open_trade"] = None
            return
        return

    buy_signal = (BUY_RSI_MIN <= rsi <= BUY_RSI_MAX) and htf_buy and vol_ok
    sell_signal = (SELL_RSI_MIN <= rsi <= SELL_RSI_MAX) and htf_sell and vol_ok
    if buy_signal:
        tp_price = price + TP_PIPS
        sl_price = price - SL_PIPS
        be_price = price + BE_TRIGGER
        stats["open_trade"] = {"side": "BUY", "entry": price, "be_done": False}
        send_telegram(f"🟢 BUY SIGNAL V12.4\nPrice {price:.2f} RSI {rsi:.1f} HTF B={htf_buy}\nTP: {tp_price:.2f} (+{TP_PIPS})\nSL: {sl_price:.2f} (-{SL_PIPS})\nBE: {be_price:.2f} (+{BE_TRIGGER} then to breakeven)")
    elif sell_signal:
        tp_price = price - TP_PIPS
        sl_price = price + SL_PIPS
        be_price = price - BE_TRIGGER
        stats["open_trade"] = {"side": "SELL", "entry": price, "be_done": False}
        send_telegram(f"🔴 SELL SIGNAL V12.4\nPrice {price:.2f} RSI {rsi:.1f} HTF S={htf_sell}\nTP: {tp_price:.2f} (+{TP_PIPS})\nSL: {sl_price:.2f} (-{SL_PIPS})\nBE: {be_price:.2f} (+{BE_TRIGGER} then to breakeven)")

def bot_loop():
    time.sleep(5)
    send_telegram(f"🤖 XAU BOT {VERSION}\nNo trade - Waiting for setup\nRSI BUY {BUY_RSI_MIN}-{BUY_RSI_MAX} | SELL {SELL_RSI_MIN}-{SELL_RSI_MAX}\nHTF: 1H EMA200\nTP +{TP_PIPS} / SL -{SL_PIPS} = 1:2 RR\nPrice: GC=F - $10 = Spot (matches broker)")
    while True:
        try:
            check_trade()
        except Exception as e:
            print(f"Loop error: {e}", flush=True)
        time.sleep(CHECK_INTERVAL)

def telegram_poll():
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
                    reply = (f"🤖 XAU BOT {VERSION}\nPrice: {stats['last_price']:.2f}\nLast: {stats['last_check']} UTC\nScore TP:{stats['tp']} SL:{stats['sl']} BE:{stats['be']} Total:{total}\nOpen: {stats['open_trade']['side'] if stats['open_trade'] else 'No trade'}")
                    send_telegram(reply)
        except Exception as e:
            print(f"Poll error: {e}", flush=True)
        time.sleep(2)

_bot_started = False
def start_bot():
    global _bot_started
    if _bot_started:
        return
    _bot_started = True
    threading.Thread(target=bot_loop, daemon=True).start()
    threading.Thread(target=telegram_poll, daemon=True).start()
    print("BOT THREADS STARTED", flush=True)

start_bot()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
