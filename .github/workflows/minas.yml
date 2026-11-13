import os, json, requests
from datetime import datetime, timedelta

TOKEN = os.environ["TELEGRAM_TOKEN"].strip()
CHAT = os.environ["CHAT_ID"].strip()
KEY = os.environ["TWELVE_KEY"].strip()
MANUAL = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

RR = 3
N = 3
BUFFER = 0.5
MIN_RISK = 5.0
MAX_HOLD = 100   # candles before a time stop
MAX_EXT = 2      # max TP extensions per trade
STATE_FILE = "trades.json"

# timeframe, minutes, higher timeframe that must agree (None = any trend)
SCANS = [("1h", 60, None), ("4h", 240, "1day"), ("1day", 1440, None)]
MINS = {"1h": 60, "4h": 240, "1day": 1440}

def send(text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT, "text": text[:4000]}, timeout=20)
        print("TELEGRAM:", r.status_code)
    except Exception as ex:
        print("Telegram error:", ex)

def parse(s):
    s = s.strip()
    if len(s) == 10:
        s += " 00:00:00"
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")

def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("sent", [])
    s.setdefault("open", [])
    s.setdefault("stats", {"w": 0, "l": 0, "be": 0, "r": 0.0})
    return s

def save_state(s):
    s["sent"] = s["sent"][-300:]
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=1)

CACHE, ERRORS = {}, []
def candles(interval, size=150):
    if interval in CACHE:
        return CACHE[interval]
    rows = None
    try:
        r = requests.get(
            "https://api.twelvedata.com/time_series",
            params={"symbol": "XAU/USD", "interval": interval,
                    "outputsize": size, "apikey": KEY}, timeout=30)
        d = r.json()
        if "values" in d:
            rows = [{"t": v["datetime"], "dt": parse(v["datetime"]),
                     "o": float(v["open"]), "h": float(v["high"]),
                     "l": float(v["low"]), "c": float(v["close"])}
                    for v in reversed(d["values"])][:-1]  # drop forming candle
        else:
            ERRORS.append(f"{interval}: {d.get('message', 'unknown')}")
    except Exception as ex:
        ERRORS.append(f"{interval}: {ex}")
    CACHE[interval] = rows
    return rows

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
    tp = entry + RR * risk if side == "BUY" else entry - RR * risk
    return (side, entry, sl, tp)

def mss(rows, want):
    """Market shift: old trend flips, last swing breaks, displacement candle."""
    highs, lows = swings(rows)
    if len(highs) < 2 or len(lows) < 2 or len(rows) < 25:
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
        return build("BUY", last["c"], lows[-1] - BUFFER)
    if want == "SELL" and was_up and last["c"] < last["o"] \
            and prev["c"] >= lows[-1] > last["c"]:
        return build("SELL", last["c"], highs[-1] + BUFFER)
    return None

state = load_state()

def record_text():
    s = state["stats"]
    return f"Record: {s['w']}W {s['l']}L {s['be']}BE | total {s['r']:+.1f}R"

def track():
    keep = []
    for t in state["open"]:
        rows = candles(t["tf"])
        if not rows or len(rows) < 5:
            keep.append(t)
            continue
        side, e, risk = t["side"], t["entry"], t["risk"]
        sign = 1 if side == "BUY" else -1
        done = False
        for x in rows:
            if x["dt"] <= parse(t["checked"]):
                continue
            t["checked"] = x["t"]
            t["n"] += 1
            if side == "BUY":
                hit_sl, hit_tp = x["l"] <= t["sl"], x["h"] >= t["tp"]
            else:
                hit_sl, hit_tp = x["h"] >= t["sl"], x["l"] <= t["tp"]
            if hit_sl:  # stop checked first (conservative)
                r = sign * (t["sl"] - e) / risk
                why = "Stop loss hit" if r < -0.01 else "Stopped at breakeven"
            elif hit_tp:
                r = sign * (t["tp"] - e) / risk
                why = "Take profit hit"
            elif t["n"] >= MAX_HOLD:
                r = sign * (x["c"] - e) / risk
                why = "Time stop: close the trade at market"
            else:
                continue
            st = state["stats"]
            st["r"] += r
            if r > 0.01:
                st["w"] += 1
            elif r < -0.01:
                st["l"] += 1
            else:
                st["be"] += 1
            send(f"Gold {side} ({t['tf']}): {why}\n"
                 f"Result: {r:+.1f}R\n{record_text()}")
            done = True
            break
        if done:
            continue
        # TP extension: new break of structure in the trade's direction
        last, prev = rows[-1], rows[-2]
        if t["ext"] < MAX_EXT and last["t"] != t.get("ext_t") \
                and last["dt"] > parse(t["time"]):
            highs, lows = swings(rows)
            ok = False
            if side == "SELL" and lows:
                ok = (prev["c"] >= lows[-1] > last["c"]
                      and last["c"] < last["o"] and last["c"] <= e - risk)
            if side == "BUY" and highs:
                ok = (prev["c"] <= highs[-1] < last["c"]
                      and last["c"] > last["o"] and last["c"] >= e + risk)
            if ok:
                old_tp = t["tp"]
                t["tp"] = old_tp + sign * risk
                t["sl"] = max(t["sl"], e) if side == "BUY" else min(t["sl"], e)
                t["ext"] += 1
                t["ext_t"] = last["t"]
                send(f"Gold {side} ({t['tf']}): still strong\n"
                     f"New structure break in your direction.\n"
                     f"Move TP: {old_tp:.2f} -> {t['tp']:.2f}\n"
                     f"Move SL to breakeven: {t['sl']:.2f}\n"
                     f"Minas now tracks these new levels.")
        keep.append(t)
    state["open"] = keep

def scan():
    found = 0
    for tf, mins, htf in SCANS:
        rows = candles(tf)
        if not rows or len(rows) < 40:
            continue
        if any(o["tf"] == tf for o in state["open"]):
            continue
        note = ""
        if htf:
            hr = candles(htf)
            trend = bias(hr) if hr else None
            if not trend:
                continue
            wants = [trend]
            note = f"\n{htf} trend agrees: {trend}"
        else:
            wants = ["BUY", "SELL"]
        key = f"{tf}:{rows[-1]['t']}"
        if key in state["sent"]:
            continue
        for w in wants:
            s = mss(rows, w)
            if not s:
                continue
            side, e, sl, tp = s
            state["sent"].append(key)
            state["open"].append({
                "tf": tf, "side": side, "entry": e, "sl": sl, "tp": tp,
                "risk": abs(e - sl), "time": rows[-1]["t"],
                "checked": rows[-1]["t"], "n": 0, "ext": 0})
            send(f"Gold {side} ({tf})\n"
                 f"Setup: Market shift (MSS){note}\n"
                 f"Entry: {e:.2f}\nSL: {sl:.2f}\nTP: {tp:.2f}\n"
                 f"RR: 1:{RR}\n"
                 f"Minas will track this trade and tell you if TP can move.")
            found += 1
            break
    return found

track()
found = scan()
if MANUAL:
    if ERRORS:
        send("Minas data error: " + "; ".join(ERRORS))
    elif not found:
        send(f"Minas scan done: no new market shift.\n"
             f"Open trades: {len(state['open'])}\n{record_text()}")
print("Errors:", ERRORS)
save_state(state)
