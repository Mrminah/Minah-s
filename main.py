import os, requests

TOKEN = os.environ["TELEGRAM_TOKEN"].strip()
CHAT = os.environ["CHAT_ID"].strip()
KEY = os.environ["TWELVE_KEY"].strip()
MANUAL = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

MIN_RR = 2
N = 3          # candles each side for a swing point
BUFFER = 0.5   # extra room for SL
MIN_RISK = 2.0 # ignore tiny stops

def send(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT, "text": text}, timeout=20)
    print("TELEGRAM:", r.status_code, r.text[:100])

def get_candles(interval="1h", size=120):
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={"symbol": "XAU/USD", "interval": interval,
                "outputsize": size, "apikey": KEY},
        timeout=20)
    d = r.json()
    if "values" not in d:
        return None, d.get("message", "unknown")
    rows = [{"h": float(v["high"]), "l": float(v["low"]),
             "c": float(v["close"])} for v in reversed(d["values"])]
    return rows[:-1], None  # drop the still-forming candle

def swings(rows):
    highs, lows = [], []
    for i in range(N, len(rows) - N):
        w = rows[i - N:i + N + 1]
        if rows[i]["h"] == max(x["h"] for x in w):
            highs.append(rows[i]["h"])
        if rows[i]["l"] == min(x["l"] for x in w):
            lows.append(rows[i]["l"])
    return highs, lows

def market_structure(rows):
    highs, lows = swings(rows)
    if not highs or not lows:
        return None
    last, prev = rows[-1]["c"], rows[-2]["c"]
    H, L = highs[-1], lows[-1]
    if prev <= H < last:  # bullish break of structure
        sl = L - BUFFER
        risk = last - sl
        if risk >= MIN_RISK:
            return ("BUY", last, sl, last + MIN_RR * risk)
    if prev >= L > last:  # bearish break of structure
        sl = H + BUFFER
        risk = sl - last
        if risk >= MIN_RISK:
            return ("SELL", last, sl, last - MIN_RR * risk)
    return None

rows, err = get_candles()
if err:
    send(f"Minas error: {err}")
else:
    setup = market_structure(rows)
    if setup:
        side, e, sl, tp = setup
        send(f"Gold {side} (1h structure break)\n"
             f"Entry: {e:.2f}\nSL: {sl:.2f}\nTP: {tp:.2f}\nRR: 1:{MIN_RR}")
    else:
        print("No setup")
        if MANUAL:
            send("Minas scan done: no setup right now.")
