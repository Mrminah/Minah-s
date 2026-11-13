import os, requests
from datetime import datetime, timedelta

TOKEN = os.environ["TELEGRAM_TOKEN"].strip()
CHAT = os.environ["CHAT_ID"].strip()
KEY = os.environ["TWELVE_KEY"].strip()

N = 3
BUFFER = 0.5
MIN_RISK = 5.0
SPREAD = 0.4
MAX_HOLD = 100
WINDOW = 149

# entry tf, minutes, higher tf, minutes
TFS = [("1h", 60, "4h", 240),
       ("4h", 240, "1day", 1440),
       ("1day", 1440, None, 0)]
# label, RR, needs higher-tf trend agreement
VARIANTS = [("MSS 1:2", 2, False), ("MSS 1:3", 3, False),
            ("MSS 1:4", 4, False), ("MSS 1:3 +HTF", 3, True)]

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

CACHE = {}
def get_candles(interval, size=5000):
    if interval in CACHE:
        return CACHE[interval]
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={"symbol": "XAU/USD", "interval": interval,
                "outputsize": size, "apikey": KEY}, timeout=90)
    d = r.json()
    if "values" not in d:
        print("Error", interval, d.get("message"))
        CACHE[interval] = None
        return None
    rows = [{"dt": parse(v["datetime"]), "o": float(v["open"]),
             "h": float(v["high"]), "l": float(v["low"]),
             "c": float(v["close"])} for v in reversed(d["values"])]
    CACHE[interval] = rows[:-1]
    return CACHE[interval]

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

def mss(rows, want, rr):
    highs, lows = swings(rows)
    if len(highs) < 2 or len(lows) < 2:
        return None
    last, prev = rows[-1], rows[-2]
    bodies = [abs(x["c"] - x["o"]) for x in rows[-21:-1]]
    avg = sum(bodies) / len(bodies)
    body = abs(last["c"] - last["o"])
    rng = last["h"] - last["l"]
    if rng <= 0 or body < 1.5 * avg or body < 0.6 * rng:
        return None
    was_down = highs[-1] < highs[-2] and lows[-1] < lows[-2]
    was_up = highs[-1] > highs[-2] and lows[-1] > lows[-2]
    if want == "BUY" and was_down and last["c"] > last["o"] \
            and prev["c"] <= highs[-1] < last["c"]:
        return build("BUY", last["c"], lows[-1] - BUFFER, rr)
    if want == "SELL" and was_up and last["c"] < last["o"] \
            and prev["c"] >= lows[-1] > last["c"]:
        return build("SELL", last["c"], highs[-1] + BUFFER, rr)
    return None

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

lines = ["Minas BACKTEST v3: MSS only",
         f"Min stop {MIN_RISK}, spread {SPREAD}",
         "Break-even win rate: 36% at 1:2, 27% at 1:3, 20% at 1:4.",
         "H1/H2 = first/second half of the period.", ""]

for tf, mins, htf, hmins in TFS:
    rows = get_candles(tf)
    if not rows:
        lines.append(f"{tf}: no data")
        continue
    rows_hi = get_candles(htf) if htf else None
    lines.append(f"== {tf}: {rows[0]['dt']:%Y-%m-%d} to "
                 f"{rows[-1]['dt']:%Y-%m-%d} ==")
    res, busy = {}, {}
    p, last_p, trend = 0, -1, None
    for i in range(WINDOW, len(rows) - 1):
        end = rows[i]["dt"] + timedelta(minutes=mins)
        if rows_hi:
            while p < len(rows_hi) and \
                    rows_hi[p]["dt"] + timedelta(minutes=hmins) <= end:
                p += 1
            if p != last_p and p >= 20:
                trend = bias(rows_hi[max(0, p - 150):p])
                last_p = p
        win = rows[i - WINDOW + 1:i + 1]
        for label, rr, need in VARIANTS:
            if need:
                if not rows_hi or trend is None:
                    continue
                wants = [trend]
            else:
                wants = ["BUY", "SELL"]
            for w in wants:
                s = mss(win, w, rr)
                if not s:
                    continue
                if busy.get(label, -1) >= i:
                    break
                r, j = simulate(rows, i, s)
                busy[label] = j
                res.setdefault(label, []).append((r, i / len(rows)))
                break
    for label, rr, need in VARIANTS:
        if need and not rows_hi:
            continue
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
    lines.append("")

msg = "\n".join(lines)
print(msg)
send(msg)
