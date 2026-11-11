import os
import time
import threading
import yfinance as yf
import pandas as pd
import numpy as np
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ===== CONFIG =====
TOKEN = os.environ.get("TELEGRAM_TOKEN", "PASTE_YOUR_TOKEN_HERE")
CHAT_ID = os.environ.get("CHAT_ID", "")  # optional, bot will learn from /start
SYMBOL = "GC=F"  # Gold Futures - use for XAUUSD

TP_POINTS = 40
SL_POINTS = 20
BE_POINTS = 15

RSI_PERIOD = 14
EMA_HTF = 200

# State
open_trade = None
score = {"TP": 0, "SL": 0, "BE": 0}
last_signal_time = 0
user_chat_id = None

app_flask = Flask(__name__)

@app_flask.route("/")
def home():
    return "Bot is running - V12.2"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port)

# ===== INDICATORS =====
def rsi_calc(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = -delta.where(delta < 0, 0).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def get_data():
    # 15m for signals, 1H for HTF filter
    df_15m = yf.download(SYMBOL, interval="15m", period="2d", progress=False, auto_adjust=True)
    df_1h = yf.download(SYMBOL, interval="1h", period="5d", progress=False, auto_adjust=True)
    if df_15m.empty or df_1h.empty:
        return None, None, None, None
    # flatten if multi-index
    if isinstance(df_15m.columns, pd.MultiIndex):
        df_15m.columns = df_15m.columns.get_level_values(0)
    if isinstance(df_1h.columns, pd.MultiIndex):
        df_1h.columns = df_1h.columns.get_level_values(0)
    df_15m["RSI"] = rsi_calc(df_15m["Close"], RSI_PERIOD)
    df_1h["EMA200"] = df_1h["Close"].ewm(span=EMA_HTF, adjust=False).mean()
    price = float(df_15m["Close"].iloc[-1])
    rsi = float(df_15m["RSI"].iloc[-1])
    ema200_1h = float(df_1h["EMA200"].iloc[-1])
    return price, rsi, ema200_1h, df_15m

# ===== BOT COMMANDS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global user_chat_id
    user_chat_id = update.effective_chat.id
    if open_trade:
        t = open_trade
        await update.message.reply_text(
            f"Bot is LIVE - V12.2 ✅\n\n"
            f"Open: {t['side']} @ {t['entry']:.2f}\n"
            f"TP: {t['tp']:.2f} (+{TP_POINTS})\n"
            f"SL: {t['sl']:.2f} (-{SL_POINTS})\n"
            f"BE: {t['be']:.2f} (+{BE_POINTS})\n\n"
            f"Score TP:{score['TP']} SL:{score['SL']} BE:{score['BE']}"
        )
    else:
        await update.message.reply_text(
            f"Bot is LIVE - V12.2 ✅\n"
            f"No open trade. Watching Gold...\n\n"
            f"Settings: TP +{TP_POINTS} / SL -{SL_POINTS} = 1:2 RR\n"
            f"Score TP:{score['TP']} SL:{score['SL']} BE:{score['BE']}"
        )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price, rsi, ema200, _ = get_data()
    if price is None:
        await update.message.reply_text("Can't fetch price right now, try again.")
        return
    htf = price < ema200_1h if ema200 else False
    await update.message.reply_text(
        f"Gold: {price:.2f}\nRSI (15m): {rsi:.1f}\n1H EMA200: {ema200:.2f}\nHTF S={htf} (Price under EMA = SELL bias)"
    )

# ===== MAIN LOOP =====
async def check_loop(app):
    global open_trade, last_signal_time, user_chat_id
    while True:
        try:
            if user_chat_id is None:
                await asyncio_sleep(10)
                continue

            price, rsi, ema200, _ = get_data()
            if price is None:
                await asyncio_sleep(30)
                continue

            htf_sell = price < ema200  # Price under 1H EMA200
            htf_buy = price > ema200

            # --- If trade is OPEN, check TP / SL / BE ---
            if open_trade:
                t = open_trade
                # BE logic: if +15 reached, move SL to entry
                if not t["be_done"]:
                    if t["side"] == "SELL" and price <= t["entry"] - BE_POINTS:
                        t["sl"] = t["entry"]
                        t["be_done"] = True
                    if t["side"] == "BUY" and price >= t["entry"] + BE_POINTS:
                        t["sl"] = t["entry"]
                        t["be_done"] = True

                # Check TP / SL
                closed = None
                if t["side"] == "SELL":
                    if price <= t["tp"]:
                        closed = "TP"
                    elif price >= t["sl"]:
                        closed = "BE" if t["be_done"] else "SL"
                else:  # BUY
                    if price >= t["tp"]:
                        closed = "TP"
                    elif price <= t["sl"]:
                        closed = "BE" if t["be_done"] else "SL"

                if closed:
                    score[closed] += 1
                    if closed == "TP":
                        emoji = "✅"
                        txt = f"{emoji} TP HIT {t['side']} +{TP_POINTS}\nEntry {t['entry']:.2f} Exit {price:.2f}\nScore TP:{score['TP']} SL:{score['SL']} BE:{score['BE']}"
                    elif closed == "SL":
                        emoji = "❌"
                        txt = f"{emoji} SL HIT {t['side']} -{SL_POINTS}\nEntry {t['entry']:.2f} Exit {price:.2f}\nScore TP:{score['TP']} SL:{score['SL']} BE:{score['BE']}"
                    else:
                        txt = f"🔒 BREAKEVEN CLOSED {t['side']}\nEntry {t['entry']:.2f} Exit {price:.2f}\nScore TP:{score['TP']} SL:{score['SL']} BE:{score['BE']}"
                    await app.bot.send_message(chat_id=user_chat_id, text=txt)
                    open_trade = None

                await asyncio_sleep(30)
                continue

            # --- No open trade, look for new signal ---
            # Prevent spam: only 1 signal per 15 min candle
            now = time.time()
            if now - last_signal_time < 900:
                await asyncio_sleep(30)
                continue

            signal = None
            # SELL: RSI 50-68 + Price under 1H EMA200
            if 50 <= rsi <= 68 and htf_sell:
                signal = "SELL"
            # BUY: RSI 32-50 + Price above 1H EMA200
            elif 32 <= rsi <= 50 and htf_buy:
                signal = "BUY"

            if signal:
                last_signal_time = now
                entry = price
                if signal == "SELL":
                    tp_price = entry - TP_POINTS
                    sl_price = entry + SL_POINTS
                    be_price = entry - BE_POINTS
                    emoji = "🔴"
                else:
                    tp_price = entry + TP_POINTS
                    sl_price = entry - SL_POINTS
                    be_price = entry + BE_POINTS
                    emoji = "🟢"

                open_trade = {
                    "side": signal,
                    "entry": entry,
                    "tp": tp_price,
                    "sl": sl_price,
                    "be": be_price,
                    "be_done": False,
                }

                msg = (
                    f"{emoji} {signal} @ {entry:.2f}\n"
                    f"TP: {tp_price:.2f} (+{TP_POINTS})\n"
                    f"SL: {sl_price:.2f} (-{SL_POINTS})\n"
                    f"BE: {be_price:.2f} (+{BE_POINTS} then to breakeven)\n"
                    f"RSI {rsi:.1f} | HTF S={htf_sell}"
                )
                await app.bot.send_message(chat_id=user_chat_id, text=msg)

            await asyncio_sleep(30)

        except Exception as e:
            print(f"Loop error: {e}")
            await asyncio_sleep(30)

async def asyncio_sleep(sec):
    import asyncio
    await asyncio.sleep(sec)

def main():
    threading.Thread(target=run_flask, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))

    # Start checker in background after bot starts
    async def post_init(app):
        import asyncio
        asyncio.create_task(check_loop(app))
    app.post_init = post_init

    print("Starting V12.2 Bot...")
    app.run_polling()

if __name__ == "__main__":
    main()
