import os, requests
from datetime import datetime

TOKEN = os.environ["TELEGRAM_TOKEN"].strip()
CHAT = os.environ["CHAT_ID"].strip()
KEY = os.environ["TWELVE_KEY"].strip()

N = 3
BUFFER = 0.5
SPREAD = 0.4
MAX_HOLD = 100
WINDOW = 149
TARGETS = [2, 3, 5, 10, 20]
MIN_RISK = {"1h": 5.0, "4h": 10.0, "1day": 20.0}

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
                "outputsize": size, "apikey": KEY}, timeout=90)
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

def build(side, entry, sl, tf):
    if abs(entry - sl) < MIN_RISK[tf]:
        return None
    return (side, entry, sl)

def setup_mss(rows, tf):
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
    if was_down and last["c"] > last["o"] \
            and prev["c"] <= highs[-1] < last["c"]:
        return build("BUY", last["c"], lows[-1] - BUFFER, tf)
    if was_up and last["c"] < last["o"] \
            and prev["c"] >= lows[-1] > last["c"]:
        return build("SELL", last["c"], highs[-1] + BUFFER, tf)
    return None

def setup_sweep(rows, tf):
    highs, lows = swings(rows)
    last, prev = rows[-1], rows[-2]
    if highs:
        H = highs[-1]
        if last["h"] > H and last["c"] < H and prev["h"] <= H:
            return build("SELL", last["c"], last["h"] + BUFFER, tf)
    if lows:
        L = lows[-1]
        if last["l"] < L and last["c"] > L and prev["l"] >= L:
            return build("BUY", last["c"], last["l"] - BUFFER, tf)
    return None

SETUPS = [("MSS", setup_mss), ("Sweep", setup_sweep)]

def mfe(rows, i, side, entry, sl):
    """Biggest R reached before the stop is hit (stop checked first)."""
    risk = abs(entry - sl)
    best = 0.0
    end = min(i + 1 + MAX_HOLD, len(rows))
    for j in range(i + 1, end):
        h, l = rows[j]["h"], rows[j]["l"]
        if side == "BUY":
            if l <= sl:
                return best, j
            best = max(best, (h - entry) / risk)
        else:
            if h >= sl:
                return best, j
            best = max(best, (entry - l) / risk)
    return best, end - 1

def spike(rows, i):
    base = rows[i - 23:i - 3]
    avg = sum(x["h"] - x["l"] for x in base) / len(base)
    return any((x["h"] - x["l"]) >= 2 * avg for x in rows[i - 3:i + 1])

def stats(items):
    n = len(items)
    if n == 0:
        return None
    hit = {T: sum(1 for x in items if x["m"] >= T) / n * 100
           for T in TARGETS}
    exp = {T: sum((T if x["m"] >= T else -1) - SPREAD / x["risk"]
                  for x in items) / n for T in TARGETS}
    return n, hit, exp

def short(label, items):
    s = stats(items)
    if not s:
        return f"{label}: 0 trades"
    n, hit, exp = s
    return (f"{label}: n={n}, 3R {hit[3]:.0f}%, 10R {hit[10]:.0f}%, "
            f"avg R at 3R {exp[3]:+.2f}, at 10R {exp[10]:+.2f}")

res, spans = {}, []
for tf in ["1h", "4h", "1day"]:
    rows = get_candles(tf)
    if not rows:
        spans.append(f"{tf}: no data")
        continue
    spans.append(f"{tf}: {rows[0]['dt']:%Y-%m-%d} to "
                 f"{rows[-1]['dt']:%Y-%m-%d}")
    busy = {}
    for i in range(WINDOW, len(rows) - 1):
        win = rows[i - WINDOW + 1:i + 1]
        for name, fn in SETUPS:
            if busy.get(name, -1) >= i:
                continue
            s = fn(win, tf)
            if not s:
                continue
            side, e, sl = s
            m, j = mfe(rows, i, side, e, sl)
            busy[name] = j
            res.setdefault((tf, name), []).append(
                {"m": m, "risk": abs(e - sl), "dt": rows[i]["dt"],
                 "spike": spike(rows, i)})

m1 = ["STUDY 1: how often do setups reach big R?",
      "Stop checked first. Break-even win rate: 1:3 = 27%, "
      "1:5 = 17%, 1:10 = 9%, 1:20 = 5%.", *spans, ""]
for tf in ["1h", "4h", "1day"]:
    for name, _ in SETUPS:
        s = stats(res.get((tf, name), []))
        if not s:
            m1.append(f"{tf} {name}: 0 trades")
            continue
        n, hit, exp = s
        m1.append(f"{tf} {name}: {n} trades")
        m1.append("hit %: " + " | ".join(f"{T}R {hit[T]:.0f}"
                                         for T in TARGETS))
        m1.append("avg R at fixed target: " + " | ".join(
            f"{T}R {exp[T]:+.2f}" for T in TARGETS))
send("\n".join(m1))

m2 = ["STUDY 2: does news or volatility help?",
      "NFP day = first Friday of the month. Spike = a candle 2x normal "
      "range in the last 3 candles.", ""]
for tf in ["4h", "1day"]:
    items = res.get((tf, "MSS"), []) + res.get((tf, "Sweep"), [])
    m2.append(f"== {tf} ==")
    m2.append(short("NFP day", [x for x in items
                                if x["dt"].weekday() == 4
                                and x["dt"].day <= 7]))
    m2.append(short("After spike", [x for x in items if x["spike"]]))
    m2.append(short("Calm", [x for x in items if not x["spike"]]))
items = res.get(("1h", "MSS"), []) + res.get(("1h", "Sweep"), [])
m2.append("== 1h by session (UTC hours) ==")
for label, a, b in [("Asia 0-6", 0, 7), ("London 7-11", 7, 12),
                    ("New York 12-19", 12, 20), ("Late 20-23", 20, 24)]:
    m2.append(short(label, [x for x in items if a <= x["dt"].hour < b]))
send("\n".join(m2))
print("\n".join(m1))
print("\n".join(m2))
