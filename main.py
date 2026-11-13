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
        json.dump(s[-300:], f)

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
    return rows[:-1], None

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

def build(side, entry, sl):
    risk = abs(entry - sl)
    if risk < MIN_RISK:
        return None
    tp = entry + MIN_RR * risk if side == "BUY" else entry - MIN_RR * risk
    return (side, entry, sl, tp)

# ---------- Strategy 1: break of structure ----------
def structure(rows, want):
    highs, lows = swings(rows)
    if not highs or not lows:
        return None
    last, prev = rows[-1]["c"], rows[-2]["c"]
    H, L = highs[-1], lows[-1]
    if want == "BUY" and prev <= H < last:
        return build("BUY", last, L - BUFFER)
    if want == "SELL" and prev >= L > last:
        return build("SELL", last, H + BUFFER)
    return None

# ---------- Strategy 2: fair value gap retest ----------
def fvg(rows, want):
    last = rows[-1]
    for i in range(len(rows) - 2, max(len(rows) - 32, 2), -1):
        a, c = rows[i - 2], rows[i]
        between = rows[i + 1:-1]
        if want == "BUY" and a["h"] < c["l"]:
            lo, hi = a["h"], c["l"]
            if any(x["l"] <= hi for x in between):
                continue  # gap already tested
            if last["l"] <= hi and last["c"] > lo:
                return build("BUY", last["c"], lo - BUFFER)
            return None
        if want == "SELL" and a["l"] > c["h"]:
            lo, hi = c["h"], a["l"]
            if any(x["h"] >= lo for x in between):
                continue
            if last["h"] >= lo and last["c"] < hi:
                return build("SELL", last["c"], hi + BUFFER)
            return None
    return None

# ---------- Strategy 3: liquidity sweep ----------
def sweep(rows, want):
    highs, lows = swings(rows)
    last, prev = rows[-1], rows[-2]
    if want == "SELL" and highs:
        H = highs[-1]
        if last["h"] > H and last["c"] < H and prev["h"] <= H:
            return build("SELL", last["c"], last["h"] + BUFFER)
    if want == "BUY" and lows:
        L = lows[-1]
        if last["l"] < L and last["c"] > L and prev["l"] >= L:
            return build("BUY", last["c"], last["l"] - BUFFER)
    return None

STRATS = [("Structure break", structure),
          ("Fair value gap", fvg),
          ("Liquidity sweep", sweep)]

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
            hits = []
            for name, fn in STRATS:
                s = fn(rows, trend)
                if s:
                    hits.append((name, s))
            new = [(n, s) for n, s in hits
                   if f"{n}:{tf}:{trend}:{rows[-1]['t']}" not in state]
            if not new:
                continue
            side, e, sl, tp = new[0][1]
            names = " + ".join(n for n, _ in new)
            strength = "STRONG (confluence)" if len(new) > 1 else "Normal"
            send(f"Gold {side} ({tf})\n"
                 f"Setup: {names}\n"
                 f"Strength: {strength}\n"
                 f"4h trend: {trend}\n"
                 f"Entry: {e:.2f}\nSL: {sl:.2f}\nTP: {tp:.2f}\n"
                 f"RR: 1:{MIN_RR}")
            for n, _ in new:
                state.append(f"{n}:{tf}:{trend}:{rows[-1]['t']}")
            sent_any = True
        if not sent_any and MANUAL:
            send(f"Minas scan done: 4h trend {trend}, no setup right now.")

save_state(state)
