import os, json, requests

TOKEN = os.environ["TELEGRAM_TOKEN"].strip()
CHAT = os.environ["CHAT_ID"].strip()
KEY = os.environ["TWELVE_KEY"].strip()
MANUAL = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

MIN_RR = 2
N = 3
BUFFER = 0.5
MIN_RISK = 2.0
ENTRY_TFS = ["15min", "1h"]
STATE_FILE = "sent.json"

def send(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT, "text": text}, timeout=20)
    print("TELEGRAM:", r.status_code, r.text[:100])

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s[-200:], f)

def get_candles(interval, size=150):
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={"symbol": "XAU/USD", "interval": interval,
                "outputsize": size, "apikey": KEY},
        timeout=20)
    d = r.json()
    if "values" not in d:
        return None, d.get("message", "unknown")
    rows = [{"t": v["datetime"], "h": float(v["high"]),
             "l": float(v["low"]), "c": float(v["close"])}
            for v in reversed(d["values"])]
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

def bias(rows):
    highs, lows = swings(rows)
    if len(highs) < 2 or len(lows) < 2:
        return None
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return "BUY"
    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return "SELL"
    return None

def market_structure(rows):
    highs, lows = swings(rows)
    if not highs or not lows:
        return None
    last, prev = rows[-1]["c"], rows[-2]["c"]
    H, L = highs[-1], lows[-1]
    if prev <= H < last:
        sl = L - BUFFER
        risk = last - sl
        if risk >= MIN_RISK:
            return ("BUY", last, sl, last + MIN_RR * risk)
    if prev >= L > last:
        sl = H + BUFFER
        risk = sl - last
        if risk >= MIN_RISK:
            return ("SELL", last, sl, last - MIN_RR * risk)
    return None

state = load_state()
rows4, err = get_candles("4h")
if err:
    print("Error:", err)
    if MANUAL:
        send(f"Minas error: {err}")
else:
    trend = bias(rows4)
    print("4h bias:", trend)
    if trend is None:
        if MANUAL:
            send("Minas scan done: no clear 4h trend, no trades.")
    else:
        sent_any = False
        for tf in ENTRY_TFS:
            rows, err = get_candles(tf)
            if err:
                print(tf, "error:", err)
                continue
            setup = market_structure(rows)
            if not setup or setup[0] != trend:
                continue
            side, e, sl, tp = setup
            key = f"{tf}:{side}:{rows[-1]['t']}"
            if key in state:
                continue
            send(f"Gold {side} ({tf} structure break)\n"
                 f"4h trend: {trend}\n"
                 f"Entry: {e:.2f}\nSL: {sl:.2f}\nTP: {tp:.2f}\n"
                 f"RR: 1:{MIN_RR}")
            state.append(key)
            sent_any = True
        if not sent_any and MANUAL:
            send(f"Minas scan done: 4h trend {trend}, no setup right now.")

save_state(state)
