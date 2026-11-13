import os, requests
from datetime import datetime, timedelta

TOKEN = os.environ["TELEGRAM_TOKEN"].strip()
CHAT = os.environ["CHAT_ID"].strip()
KEY = os.environ["TWELVE_KEY"].strip()

N = 3
BUFFER = 0.5
MIN_RISK = 5.0   # wider stops so spread matters less
SPREAD = 0.4
MAX_HOLD = 100
WINDOW = 149
DUR = timedelta(minutes=60)

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
                "outputsize": size, "apikey": KEY}, timeout=60)
    d = r.json()
    if "values" not in d:
        print("Error", interval, d.get("message"))
        return None
    rows = [{"dt": parse(v["datetime"]), "o": float(v["open"]),
             "h": float(v["high"]), "l": float(v["low"]),
             "c": float(v["close"])} for v in reversed(d["values"])]
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

def build(side, entry, sl, rr):
    risk = abs(entry - sl)
    if risk < MIN_RISK:
        return None
    tp = entry + rr * risk if side == "BUY" else entry - rr * risk
    return (side, entry, sl, tp)

def structure(rows, want, rr):
    highs, lows = swings(rows)
    if not highs or not lows:
        return None
    last, prev = rows[-1]["c"], rows[-2]["c"]
    H, L = highs[-1], lows[-1]
    if want == "BUY" and prev <= H < last:
        return build("BUY", last, L - BUFFER, rr)
    if want == "SELL" and prev >= L > last:
        return build("SELL", last, H + BUFFER, rr)
    return None

def fvg(rows, want, rr):
    last = rows[-1]
    for i in range(len(rows) - 2, max(len(rows) - 32, 2), -1):
        a, c = rows[i - 2], rows[i]
        between = rows[i + 1:-1]
        if want == "BUY" and a["h"] < c["l"]:
            lo, hi = a["h"], c["l"]
            if any(x["l"] <= hi for x in between):
                continue
            if last["l"] <= hi and last["c"] > lo:
                return build("BUY", last["c"], lo - BUFFER, rr)
            return None
        if want == "SELL" and a["l"] > c["h"]:
            lo, hi = c["h"], a["l"]
            if any(x["h"] >= lo for x in between):
                continue
            if last["h"] >= lo and last["c"] < hi:
                return build("SELL", last["c"], hi + BUFFER, rr)
            return None
    return None

def sweep(rows, want, rr):
    highs, lows = swings(rows)
    last, prev = rows[-1], rows[-2]
    if want == "SELL" and highs:
        H = highs[-1]
        if last["h"] > H and last["c"] < H and prev["h"] <= H:
            return build("SELL", last["c"], last["h"] + BUFFER, rr)
    if want == "BUY" and lows:
        L = lows[-1]
        if last["l"] < L and last["c"] > L and prev["l"] >= L:
            return build("BUY", last["c"], last["l"] - BUFFER, rr)
    return None

def mss(rows, want, rr):
    """Market shift: old trend flips, break of last swing + displacement."""
    highs, lows = swings(rows)
    if len(highs) < 2 or len(lows) < 2:
        return None
    last, prev = rows[-1], rows[-2]
    bodies = [abs(x["c"] - x["o"]) for x in rows[-21:-1]]
    avg = sum(bodies) / len(bodies)
    body = abs(last["c"] - last["o"])
    rng = last["h"] - last["l"]
    if rng <= 0 or body < 1.5 * avg or body < 0.6 * rng:
        return None  # not a displacement candle
    was_down = highs[-1] < highs[-2] and lows[-1] < lows[-2]
    was_up = highs[-1] > highs[-2] and lows[-1] > lows[-2]
    if want == "BUY" and was_down and last["c"] > last["o"] \
            and prev["c"] <= highs[-1] < last["c"]:
        return build("BUY", last["c"], lows[-1] - BUFFER, rr)
    if want == "SELL" and was_up and last["c"] < last["o"] \
            and prev["c"] >= lows[-1] > last["c"]:
        return build("SELL", last["c"], highs[-1] + BUFFER, rr)
    return None

# name, function, RR, needs 4h trend agreement, premium/discount, session
VARIANTS = [
    ("Structure raw", structure, 2, True, False, False),
    ("Structure +PD+session", structure, 2, True, True, True),
    ("FVG +PD+session", fvg, 2, True, True, True),
    ("Sweep +PD+session", sweep, 2, True, True, True),
    ("MSS 1:3 any trend", mss, 3, False, False, False),
    ("MSS 1:3 +4h agree", mss, 3, True, False, False),
    ("MSS 1:3 +4h +session", mss, 3, True, False, True),
]

def simulate(rows, i, setup):
    side, e, sl, tp = setup
    risk = abs(e - sl)
    reward = abs(tp - e)
    end = min(i + 1 + MAX_HOLD, len(rows))
    for j in range(i + 1, end):
        h, l = rows[j]["h"], rows[j]["l"]
        if side == "BUY":
            if l <= sl:
                return (-risk - SPREAD) / risk, j
            if h >= tp:
                return (reward - SPREAD) / risk, j
        else:
            if h >= sl:
                return (-risk - SPREAD) / risk, j
            if l <= tp:
                return (reward - SPREAD) / risk, j
    j = end - 1
    c = rows[j]["c"]
    pnl = (c - e) if side == "BUY" else (e - c)
    return (pnl - SPREAD) / risk, j

rows4 = get_candles("4h")
rows = get_candles("1h")
if not rows4 or not rows:
    send("Backtest error: could not load data.")
    raise SystemExit

res, busy = {}, {}
p4, last_p4, trend = 0, -1, None
for i in range(WINDOW, len(rows) - 1):
    end = rows[i]["dt"] + DUR
    while p4 < len(rows4) and rows4[p4]["dt"] + timedelta(hours=4) <= end:
        p4 += 1
    if p4 < 20:
        continue
    if p4 != last_p4:
        trend = bias(rows4[max(0, p4 - 150):p4])
        last_p4 = p4
    win = rows[i - WINDOW + 1:i + 1]
    in_sess = 7 <= end.hour < 20  # 07:00-20:00 UTC (London + New York)
    rg = win[-50:]
    eq = (max(x["h"] for x in rg) + min(x["l"] for x in rg)) / 2
    for label, fn, rr, need4h, pd, sess in VARIANTS:
        if sess and not in_sess:
            continue
        if need4h:
            if trend is None:
                continue
            wants = [trend]
        else:
            wants = ["BUY", "SELL"]
        for w in wants:
            s = fn(win, w, rr)
            if not s:
                continue
            if pd and ((s[0] == "BUY" and s[1] >= eq) or
                       (s[0] == "SELL" and s[1] <= eq)):
                continue
            if busy.get(label, -1) >= i:
                continue
            r, j = simulate(rows, i, s)
            busy[label] = j
            res.setdefault(label, []).append((r, i / len(rows)))
            break

lines = ["Minas BACKTEST v2 (1h)",
         f"{rows[0]['dt']:%Y-%m-%d} to {rows[-1]['dt']:%Y-%m-%d}",
         f"Min stop {MIN_RISK}, spread {SPREAD}",
         "Break-even win rate: about 36% at 1:2, 27% at 1:3.",
         "H1/H2 = result in first/second half of the period.", ""]
for label, *_ in VARIANTS:
    r = res.get(label, [])
    n = len(r)
    if n == 0:
        lines.append(f"{label}: 0 trades")
        continue
    tot = sum(x for x, _ in r)
    wins = sum(1 for x, _ in r if x > 0)
    h1 = sum(x for x, f in r if f < 0.5)
    h2 = sum(x for x, f in r if f >= 0.5)
    lines.append(f"{label}: {n} trades, win {wins / n * 100:.0f}%, "
                 f"total {tot:+.1f}R, avg {tot / n:+.2f}R, "
                 f"H1 {h1:+.1f} / H2 {h2:+.1f}")
msg = "\n".join(lines)
print(msg)
send(msg)
