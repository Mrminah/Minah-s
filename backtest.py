import os, requests
from datetime import datetime, timedelta

TOKEN = os.environ["TELEGRAM_TOKEN"].strip()
CHAT = os.environ["CHAT_ID"].strip()
KEY = os.environ["TWELVE_KEY"].strip()

MIN_RR = 2
N = 3
BUFFER = 0.5
MIN_RISK = 2.0
SPREAD = 0.4     # assumed cost per trade in price units
MAX_HOLD = 100   # candles before a trade is closed at market
WINDOW = 149
TF_MIN = {"15min": 15, "1h": 60}

def send(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT, "text": text[:4000]}, timeout=20)
    print("TELEGRAM:", r.status_code)

def parse(s):
    s = s.strip()
    if len(s) == 10:
        s += " 00:00:00"
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")

def get_candles(interval, size=5000):
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={"symbol": "XAU/USD", "interval": interval,
                "outputsize": size, "apikey": KEY},
        timeout=60)
    d = r.json()
    if "values" not in d:
        print("Error", interval, d.get("message"))
        return None
    rows = [{"dt": parse(v["datetime"]), "h": float(v["high"]),
             "l": float(v["low"]), "c": float(v["close"])}
            for v in reversed(d["values"])]
    return rows[:-1]

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

def fvg(rows, want):
    last = rows[-1]
    for i in range(len(rows) - 2, max(len(rows) - 32, 2), -1):
        a, c = rows[i - 2], rows[i]
        between = rows[i + 1:-1]
        if want == "BUY" and a["h"] < c["l"]:
            lo, hi = a["h"], c["l"]
            if any(x["l"] <= hi for x in between):
                continue
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

def simulate(rows, i, setup):
    side, e, sl, tp = setup
    risk = abs(e - sl)
    end = min(i + 1 + MAX_HOLD, len(rows))
    for j in range(i + 1, end):
        h, l = rows[j]["h"], rows[j]["l"]
        if side == "BUY":
            if l <= sl:
                return (-risk - SPREAD) / risk, j   # SL checked first
            if h >= tp:
                return (MIN_RR * risk - SPREAD) / risk, j
        else:
            if h >= sl:
                return (-risk - SPREAD) / risk, j
            if l <= tp:
                return (MIN_RR * risk - SPREAD) / risk, j
    j = end - 1
    c = rows[j]["c"]
    pnl = (c - e) if side == "BUY" else (e - c)
    return (pnl - SPREAD) / risk, j

def run(tf, rows, rows4, res):
    dur = timedelta(minutes=TF_MIN[tf])
    p4, last_p4, trend = 0, -1, None
    busy = {}
    for i in range(WINDOW, len(rows) - 1):
        end = rows[i]["dt"] + dur
        while p4 < len(rows4) and rows4[p4]["dt"] + timedelta(hours=4) <= end:
            p4 += 1
        if p4 < 20:
            continue
        if p4 != last_p4:
            trend = bias(rows4[max(0, p4 - 150):p4])
            last_p4 = p4
        if trend is None:
            continue
        win = rows[i - WINDOW + 1:i + 1]
        hits = []
        for name, fn in STRATS:
            s = fn(win, trend)
            if s:
                hits.append((name, s))
        todo = list(hits)
        if len(hits) >= 2:
            todo.append(("Confluence (2+)", hits[0][1]))
        for name, setup in todo:
            if busy.get(name, -1) >= i:
                continue
            r, j = simulate(rows, i, setup)
            busy[name] = j
            res.setdefault(f"{tf} | {name}", []).append(r)

rows4 = get_candles("4h")
if not rows4:
    send("Backtest error: could not load 4h data.")
    raise SystemExit

res, spans = {}, []
for tf in ["1h", "15min"]:
    rows = get_candles(tf)
    if not rows:
        spans.append(f"{tf}: no data")
        continue
    spans.append(f"{tf}: {rows[0]['dt']:%Y-%m-%d} to {rows[-1]['dt']:%Y-%m-%d}")
    run(tf, rows, rows4, res)

lines = [f"Minas BACKTEST (RR 1:{MIN_RR}, spread {SPREAD})", *spans,
         "Break-even win rate is about 34%.", ""]
for key in sorted(res):
    r = res[key]
    n = len(r)
    wins = sum(1 for x in r if x > 0)
    tot = sum(r)
    lines.append(f"{key}: {n} trades, win {wins / n * 100:.0f}%, "
                 f"total {tot:+.1f}R, avg {tot / n:+.2f}R")
if not res:
    lines.append("No trades found.")
msg = "\n".join(lines)
print(msg)
send(msg)
