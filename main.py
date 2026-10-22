import os, time, threading, requests, random
from flask import Flask
import yfinance as yf
import pandas as pd
import ta

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

app = Flask(__name__)
last_price = 0
last_rsi = 0
last_check = "Never"
active_trade = None

def send_tg(text):
    for attempt in range(3):
        try:
            r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=12)
            if r.status_code == 200:
                return True
        except Exception as e:
            print(f"SEND FAIL {e}", flush=True)
        time.sleep(2)
    return False

def get_spot_price():
    urls = ["https://api.gold-api.com/price/XAU","https://api.metals.live/v1/spot"]
    for url in urls:
        try:
            r = requests.get(url, timeout=8).json()
            if "price" in r: return float(r["price"])
            if isinstance(r, list) and len(r)>0 and "gold" in r[0]: return float(r[0]["gold"])
        except: continue
    return None

def get_history():
    for retry in range(3):
        try:
            time.sleep(random.uniform(1,2))
            df = yf.download("GC=F", period="7d", interval="5m", progress=False, auto_adjust=True, threads=False)
            if df.empty: continue
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
            df = df.dropna()
            close = df['Close']
            vol = df['Volume'] if 'Volume' in df.columns else None
            if len(close) < 100: return None

            ema9 = float(ta.trend.ema_indicator(close, window=9).iloc[-1])
            ema20 = float(ta.trend.ema_indicator(close, window=20).iloc[-1])
            ema21 = float(ta.trend.ema_indicator(close, window=21).iloc[-1])
            ema200 = float(ta.trend.ema_indicator(close, window=200).iloc[-1] if len(close)>=200 else ta.trend.ema_indicator(close, window=100).iloc[-1])
            rsi = float(ta.momentum.rsi(close, window=14).iloc[-1])
            roc = float(ta.momentum.roc(close, window=9).iloc[-1])
            fut_price = float(close.iloc[-1])

            vol_avg = float(vol.rolling(20).mean().iloc[-1]) if vol is not None and len(vol)>=20 else 0
            vol_now = float(vol.iloc[-1]) if vol is not None else 0

            # V11 KOYEB FIX: Yahoo sometimes returns 0 volume
            if vol_avg == 0 or vol_now == 0 or pd.isna(vol_avg) or pd.isna(vol_now):
                vol_ok = True
                vol_avg = 200
                vol_now = 200
            else:
                vol_ok = vol_now >= vol_avg*0.40

            ema9_prev = float(ta.trend.ema_indicator(close, window=9).iloc[-2])
            return {"ema9":ema9,"ema20":ema20,"ema21":ema21,"ema200":ema200,"rsi":rsi,"roc":roc,"fut_price":fut_price,"vol_ok":vol_ok,"vol_now":vol_now,"vol_avg":vol_avg,"ema9_slope_up":ema9>ema9_prev,"ema9_slope_down":ema9<ema9_prev}
        except Exception as e:
            print(f"HISTORY FAIL retry {retry} {e}", flush=True)
            time.sleep(40)
    return None

def get_data():
    spot = get_spot_price()
    hist = get_history()
    if hist is None: return None
    if spot is None: spot = hist["fut_price"] - 18.0
    premium = hist["fut_price"] - spot
    return {"price":spot,"fut_price":hist["fut_price"],"ema9":hist["ema9"]-premium,"ema20":hist["ema20"]-premium,"ema21":hist["ema21"]-premium,"ema200":hist["ema200"]-premium,"rsi":hist["rsi"],"roc":hist["roc"],"vol_ok":hist["vol_ok"],"vol_now":hist["vol_now"],"vol_avg":hist["vol_avg"],"ema9_slope_up":hist["ema9_slope_up"],"ema9_slope_down":hist["ema9_slope_down"]}

def check_market():
    global last_price, last_rsi, last_check, active_trade
    print("MARKET V11 KOYEB FIX STARTED", flush=True)
    while True:
        try:
            d = get_data()
            if d is None: time.sleep(120); continue
            price = d["price"]; ema9=d["ema9"]; ema20=d["ema20"]; ema21=d["ema21"]; ema200=d["ema200"]; rsi=d["rsi"]; roc=d["roc"]
            last_price=price; last_rsi=rsi; last_check=time.strftime("%H:%M:%S")
            print(f"CHECK {price:.2f} RSI {rsi:.1f} ROC {roc:.4f} VOL {d['vol_now']:.0f}/{d['vol_avg']:.0f} OK={d['vol_ok']}", flush=True)

            if active_trade:
                entry=active_trade["entry"]
                if active_trade["dir"]=="BUY":
                    if price>=active_trade["tp"]:
                        if send_tg(f"✅ <b>TP HIT BUY</b>\n{entry:.2f} -> {price:.2f}"): active_trade=None
                    elif price<=active_trade["sl"]:
                        if send_tg(f"❌ <b>SL HIT BUY</b>\n{entry:.2f} -> {price:.2f}"): active_trade=None
                else:
                    if price<=active_trade["tp"]:
                        if send_tg(f"✅ <b>TP HIT SELL</b>\n{entry:.2f} -> {price:.2f}"): active_trade=None
                    elif price>=active_trade["sl"]:
                        if send_tg(f"❌ <b>SL HIT SELL</b>\n{entry:.2f} -> {price:.2f}"): active_trade=None
                if active_trade: time.sleep(90); continue

            dist_200 = abs(price-ema200)/price*100
            buy_stack = (ema9 > ema20 > ema21 > ema200) and (price > ema9)
            sell_stack = (ema9 < ema20 < ema21 < ema200) and (price < ema9)
            if abs(roc) < 0.004: print("FLAT SKIP", flush=True); time.sleep(90); continue

            buy_rsi = 40 < rsi < 68
            sell_rsi = 32 < rsi < 58
            dist_ok = 0.05 < dist_200 < 1.5
            buy_mom_vol = (roc > 0.01) and (d["vol_ok"] or roc > 0.03) and d["ema9_slope_up"]
            sell_mom_vol = (roc < -0.01) and (d["vol_ok"] or roc < -0.03) and d["ema9_slope_down"]
            buy_trend = price > ema20
            sell_trend = price < ema20

            buy_filters = [("STACK 9>20>21>200 & P>9" if buy_stack else "STACK FAIL", buy_stack),(f"RSI {rsi:.1f} in 40-68" if buy_rsi else f"RSI {rsi:.1f} BAD", buy_rsi),(f"Dist {dist_200:.2f}% OK" if dist_ok else f"Dist {dist_200:.2f}% BAD", dist_ok),(f"MOM {roc:.3f}%+VOL OK" if buy_mom_vol else f"MOM/VOL FAIL {roc:.3f}%", buy_mom_vol),("Price>EMA20" if buy_trend else "Trend FAIL", buy_trend)]
            sell_filters = [("STACK 9<20<21<200 & P<9" if sell_stack else "STACK FAIL", sell_stack),(f"RSI {rsi:.1f} in 32-58" if sell_rsi else f"RSI {rsi:.1f} BAD", sell_rsi),(f"Dist {dist_200:.2f}% OK" if dist_ok else f"Dist {dist_200:.2f}% BAD", dist_ok),(f"MOM {roc:.3f}%+VOL OK" if sell_mom_vol else f"MOM/VOL FAIL {roc:.3f}%", sell_mom_vol),("Price<EMA20" if sell_trend else "Trend FAIL", sell_trend)]

            buy_score = sum(1 for _,ok in buy_filters if ok)
            sell_score = sum(1 for _,ok in sell_filters if ok)

            if buy_score >=4 and buy_stack:
                tp=price*1.006; sl=price*0.997
                details="\n".join([f"{'✅' if ok else '❌'} {name}" for name,ok in buy_filters])
                if send_tg(f"🟢 <b>BUY 4/5 REAL</b>\nSPOT {price:.2f} FUT {d['fut_price']:.2f}\nEntry {price:.2f}\nTP {tp:.2f} (+0.6%)\nSL {sl:.2f} (-0.3%)\n\n{details}\nScore {buy_score}/5"): active_trade={"dir":"BUY","entry":price,"tp":tp,"sl":sl}
            elif sell_score >=4 and sell_stack:
                tp=price*0.994; sl=price*1.003
                details="\n".join([f"{'✅' if ok else '❌'} {name}" for name,ok in sell_filters])
                if send_tg(f"🔴 <b>SELL 4/5 REAL</b>\nSPOT {price:.2f} FUT {d['fut_price']:.2f}\nEntry {price:.2f}\nTP {tp:.2f} (-0.6%)\nSL {sl:.2f} (+0.3%)\n\n{details}\nScore {sell_score}/5"): active_trade={"dir":"SELL","entry":price,"tp":tp,"sl":sl}
            time.sleep(120)
        except Exception as e:
            print(f"ERR {e}", flush=True); time.sleep(60)

def telegram_loop():
    global last_price
    offset=0
    while True:
        try:
            r=requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates", params={"offset":offset,"timeout":25}, timeout=30).json()
            for upd in r.get("result", []):
                offset=upd["update_id"]+1
                text=upd.get("message",{}).get("text",""); chat=str(upd.get("message",{}).get("chat",{}).get("id",""))
                if chat!=CHAT_ID: continue
                if "/status" in text or "/start" in text:
                    tr="No trade" if not active_trade else f"{active_trade['dir']} {active_trade['entry']:.2f}"
                    send_tg(f"✅ <b>V11 KOYEB LIVE</b>\nSPOT {last_price:.2f}\n{tr}\nLast {last_check}")
        except: time.sleep(3)

@app.route("/")
def home(): return f"V11 KOYEB {last_price} {last_check}"

threading.Thread(target=telegram_loop, daemon=True).start()
threading.Thread(target=check_market, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
