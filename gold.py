import os, json, requests
from datetime import datetime, timedelta

TOKEN = os.environ["TELEGRAM_TOKEN"].strip()
CHAT = os.environ["CHAT_ID"].strip()
KEY = os.environ["TWELVE_KEY"].strip()
MANUAL = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

SYMBOL = "XAU/USD"
ASSET = "Gold"
TAG = "Minas Pro"            # change the bot's name here
STATE_FILE = "gold_trades.json"

DUR = {"15min": 15, "1h": 60, "4h": 240, "1day": 1440}
HTF = {"15min": "1h", "1h": "4h", "4h": "1day"}
SCAN_TFS = ["4h", "1h", "15min"]
FETCH_ORDER = ["1day", "4h", "1h"]
# strategy, needs the bigger-timeframe trend to agree
PLAN = {
    "4h": [("MSS", True), ("SMC", True), ("CRT", True), ("TL", True)],
    "1h": [("MSS", False), ("SMC", True), ("CRT", True), ("TL", True)],
    "15min": [("SMC", True), ("CRT", True), ("TL", True)],
}

RR_MIN = 2.0
MAX_RR = 5.0
BUF_PCT = 0.02
MIN_RISK_PCT = {"15min": 0.15, "1h": 0.20, "4h": 0.40}
MAX_RISK_PCT = {"15min": 0.40, "1h": 0.90, "4h": 1.80}
PEND_MAX = {"15min": 8, "1h": 16, "4h": 32}      # 15m candles
HOLD_MAX = {"15min": 96, "1h": 288, "4h": 672}   # 15m candles
MAX_OPEN = 2
MAX_PER_DAY = 5
MAX_EXT = 1
USE_HTF = True    # False = ignore the bigger trend (more signals)
MIN_POINTS_15M = 2    # 15m signals need 2+ confirmations

ERRORS = []

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
    s.setdefault("trades", [])
    s.setdefault("stats", {"w": 0, "l": 0, "be": 0, "r": 0.0})
    s.setdefault("day", {"d": "", "n": 0})
    s.setdefault("next", {})
    s.setdefault("trend", {})
    s.setdefault("scanned", {})
    return s

def save_state(s):
    s["sent"] = s["sent"][-300:]
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=1)

def candles(interval, size):
    try:
        r = requests.get(
            "https://api.twelvedata.com/time_series",
            params={"symbol": SYMBOL, "interval": interval,
                    "outputsize": size, "apikey": KEY}, timeout=30)
        d = r.json()
        if "values" in d:
            return [{"t": v["datetime"], "dt": parse(v["datetime"]),
                     "o": float(v["open"]), "h": float(v["high"]),
                     "l": float(v["low"]), "c": float(v["close"])}
                    for v in reversed(d["values"])][:-1]  # drop forming candle
        ERRORS.append(f"{interval}: {d.get('message', 'unknown')}")
    except Exception as ex:
        ERRORS.append(f"{interval}: {ex}")
    return None

def swing_pts(rows, n):
    hs, ls = [], []
    for i in range(n, len(rows) - n):
        w = rows[i - n:i + n + 1]
        if rows[i]["h"] == max(x["h"] for x in w):
            hs.append((i, rows[i]["h"]))
        if rows[i]["l"] == min(x["l"] for x in w):
            ls.append((i, rows[i]["l"]))
    return hs, ls

def swings(rows, n):
    hs, ls = swing_pts(rows, n)
    return [p for _, p in hs], [p for _, p in ls]

def bias(rows):
    highs, lows = swings(rows, 3)
    if len(highs) < 2 or len(lows) < 2:
        return None
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return "BUY"
    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return "SELL"
    return None

def displaced(rows, m, side):
    if m < 21:
        return False
    c = rows[m]
    bodies = [abs(x["c"] - x["o"]) for x in rows[m - 20:m]]
    avg = sum(bodies) / len(bodies)
    body = abs(c["c"] - c["o"])
    rng = c["h"] - c["l"]
    if rng <= 0 or body < 1.3 * avg or body < 0.5 * rng:
        return False
    return c["c"] < c["o"] if side == "SELL" else c["c"] > c["o"]

def finalize(side, entry, sl, far, tf):
    risk = abs(entry - sl)
    if risk <= 0 or entry <= 0:
        return None
    rp = risk / entry * 100
    if rp < MIN_RISK_PCT[tf] or rp > MAX_RISK_PCT[tf]:
        return None
    if side == "SELL" and (far >= entry or sl <= entry):
        return None
    if side == "BUY" and (far <= entry or sl >= entry):
        return None
    rr = abs(entry - far) / risk
    if rr < RR_MIN:
        return None
    rr = min(rr, MAX_RR)
    tp = entry - rr * risk if side == "SELL" else entry + rr * risk
    return {"side": side, "entry": entry, "sl": sl, "tp": tp,
            "risk": risk, "rr": rr}

def crt(rows, tf, want):
    """Last candle sweeps one side of the one before and closes back inside;
    target = other side of that range."""
    if len(rows) < 30:
        return None
    A, B = rows[-2], rows[-1]
    if A["h"] <= A["l"] or not (A["l"] < B["c"] < A["h"]):
        return None
    b = B["c"] * BUF_PCT / 100
    if want == "SELL" and B["h"] > A["h"] and B["l"] >= A["l"]:
        s = finalize("SELL", B["c"], B["h"] + b, A["l"], tf)
    elif want == "BUY" and B["l"] < A["l"] and B["h"] <= A["h"]:
        s = finalize("BUY", B["c"], B["l"] - b, A["h"], tf)
    else:
        return None
    if s:
        s["t"] = B["t"]
    return s

def smc(rows, tf, want):
    """Sweep of a swing level -> displacement break -> FVG limit entry."""
    n = len(rows)
    if n < 70:
        return None
    hi, lo = swings(rows[:-8], 2)
    levels = (hi if want == "SELL" else lo)[-3:]
    if not levels:
        return None
    pool = rows[-60:]
    b = rows[-1]["c"] * BUF_PCT / 100
    for s in range(n - 8, n - 3):
        sc = rows[s]
        if want == "SELL":
            if not any(sc["h"] > L > sc["c"] for L in levels):
                continue
        else:
            if not any(sc["l"] < L < sc["c"] for L in levels):
                continue
        for m in range(s + 1, n - 1):
            if not displaced(rows, m, want):
                continue
            a, c = rows[m - 1], rows[m + 1]
            if want == "SELL":
                ref = min(x["l"] for x in rows[s - 4:s + 1])
                if not (rows[m]["c"] < ref and a["l"] > c["h"]):
                    continue
                entry = (a["l"] + c["h"]) / 2
                if any(x["h"] >= entry for x in rows[m + 2:]):
                    continue
                sl = max(x["h"] for x in rows[s:m + 1]) + b
                res = finalize("SELL", entry, sl,
                               min(x["l"] for x in pool), tf)
            else:
                ref = max(x["h"] for x in rows[s - 4:s + 1])
                if not (rows[m]["c"] > ref and a["h"] < c["l"]):
                    continue
                entry = (a["h"] + c["l"]) / 2
                if any(x["l"] <= entry for x in rows[m + 2:]):
                    continue
                sl = min(x["l"] for x in rows[s:m + 1]) - b
                res = finalize("BUY", entry, sl,
                               max(x["h"] for x in pool), tf)
            if res:
                res["t"] = rows[m]["t"]
                return res
    return None

def mss(rows, tf, want):
    """Market shift: old trend flips, last swing breaks on a strong candle."""
    if len(rows) < 30:
        return None
    highs, lows = swings(rows, 3)
    if len(highs) < 2 or len(lows) < 2:
        return None
    last, prev = rows[-1], rows[-2]
    bodies = [abs(x["c"] - x["o"]) for x in rows[-21:-1]]
    avg = sum(bodies) / len(bodies)
    body = abs(last["c"] - last["o"])
    rng = last["h"] - last["l"]
    if rng <= 0 or body < 1.5 * avg or body < 0.6 * rng:
        return None
    b = last["c"] * BUF_PCT / 100
    was_down = highs[-1] < highs[-2] and lows[-1] < lows[-2]
    was_up = highs[-1] > highs[-2] and lows[-1] > lows[-2]
    if want == "BUY" and was_down and last["c"] > last["o"] \
            and prev["c"] <= highs[-1] < last["c"]:
        e, sl = last["c"], lows[-1] - b
        s = finalize("BUY", e, sl, e + 3 * (e - sl), tf)
    elif want == "SELL" and was_up and last["c"] < last["o"] \
            and prev["c"] >= lows[-1] > last["c"]:
        e, sl = last["c"], highs[-1] + b
        s = finalize("SELL", e, sl, e - 3 * (sl - e), tf)
    else:
        return None
    if s:
        s["t"] = last["t"]
    return s

def tline(rows, tf, want):
    """Trendline break: line through the last two swing points, then a close
    through it in the direction of the bigger trend."""
    n = len(rows)
    if n < 60:
        return None
    hs, ls = swing_pts(rows, 3)
    last, prev = rows[-1], rows[-2]
    b = last["c"] * BUF_PCT / 100
    pool = rows[-60:]
    if want == "BUY" and len(hs) >= 2:
        (i1, p1), (i2, p2) = hs[-2], hs[-1]
        if p2 >= p1 or i2 - i1 < 4 or n - 1 - i2 > 40:
            return None
        slope = (p2 - p1) / (i2 - i1)
        line = lambda i: p2 + slope * (i - i2)
        if prev["c"] <= line(n - 2) and last["c"] > line(n - 1) \
                and last["c"] > last["o"]:
            sl = min(x["l"] for x in rows[i2:]) - b
            s = finalize("BUY", last["c"], sl,
                         max(x["h"] for x in pool), tf)
            if s:
                s["t"] = last["t"]
            return s
    if want == "SELL" and len(ls) >= 2:
        (i1, p1), (i2, p2) = ls[-2], ls[-1]
        if p2 <= p1 or i2 - i1 < 4 or n - 1 - i2 > 40:
            return None
        slope = (p2 - p1) / (i2 - i1)
        line = lambda i: p2 + slope * (i - i2)
        if prev["c"] >= line(n - 2) and last["c"] < line(n - 1) \
                and last["c"] < last["o"]:
            sl = max(x["h"] for x in rows[i2:]) + b
            s = finalize("SELL", last["c"], sl,
                         min(x["l"] for x in pool), tf)
            if s:
                s["t"] = last["t"]
            return s
    return None

STRATS = {"MSS": mss, "SMC": smc, "CRT": crt, "TL": tline}
NAMES = {"SMC": "SMC (sweep + shift + FVG)", "CRT": "CRT (range sweep)",
         "MSS": "Market shift (MSS)", "TL": "Trendline break"}
PREF = {"SMC": 0, "CRT": 1, "TL": 2, "MSS": 3}

def sr_levels(rows):
    """Support/resistance: swing points that price turned at 2+ times."""
    hs, ls = swings(rows[-200:], 2)
    levels = []
    for p in sorted(hs + ls):
        for lv in levels:
            if abs(p - lv["p"]) / p * 100 <= 0.15:
                lv["n"] += 1
                lv["p"] = (lv["p"] * (lv["n"] - 1) + p) / lv["n"]
                break
        else:
            levels.append({"p": p, "n": 1})
    return [lv for lv in levels if lv["n"] >= 2]

def at_level(rows, entry):
    for lv in sr_levels(rows):
        if abs(entry - lv["p"]) / entry * 100 <= 0.15:
            return lv
    return None

state = load_state()

def record_text():
    s = state["stats"]
    return f"Record: {s['w']}W {s['l']}L {s['be']}BE | total {s['r']:+.1f}R"

def finish(t, r, why):
    st = state["stats"]
    st["r"] += r
    if r > 0.01:
        st["w"] += 1
    elif r < -0.01:
        st["l"] += 1
    else:
        st["be"] += 1
    send(f"{TAG}: {ASSET} {t['side']} ({t['tf']}): {why}\n"
         f"Result: {r:+.1f}R\n{record_text()}")

def track(m15):
    keep = []
    for t in state["trades"]:
        side = t["side"]
        sign = 1 if side == "BUY" else -1
        e, risk = t["entry"], t["risk"]
        done = False
        for x in m15:
            if x["dt"] <= parse(t["checked"]):
                continue
            t["checked"] = x["t"]
            t["n"] += 1
            if side == "SELL":
                hit_e, hit_sl, hit_tp = (x["h"] >= e, x["h"] >= t["sl"],
                                         x["l"] <= t["tp"])
            else:
                hit_e, hit_sl, hit_tp = (x["l"] <= e, x["l"] <= t["sl"],
                                         x["h"] >= t["tp"])
            if t["status"] == "pending":
                if hit_e:
                    t["status"] = "live"
                    t["n"] = 0
                    send(f"{TAG}: {ASSET} {side} order FILLED at {e:.2f}\n"
                         f"Trade is live. SL {t['sl']:.2f} | TP {t['tp']:.2f}")
                    if hit_sl:
                        finish(t, -1.0, "Stop loss hit right after the fill")
                        done = True
                        break
                    continue
                if hit_tp:
                    send(f"{TAG}: {ASSET} {side}: price ran to TP without "
                         f"filling.\nCANCEL your limit order at {e:.2f}.")
                    done = True
                    break
                if t["n"] >= t["pend"]:
                    send(f"{TAG}: {ASSET} {side}: limit order expired.\n"
                         f"CANCEL your order at {e:.2f}.")
                    done = True
                    break
                continue
            if hit_sl:
                r = sign * (t["sl"] - e) / risk
                finish(t, r, "Stop loss hit" if r < -0.01
                       else "Stopped at breakeven")
                done = True
                break
            if hit_tp:
                finish(t, sign * (t["tp"] - e) / risk, "Take profit hit")
                done = True
                break
            if t["n"] >= t["hold"]:
                finish(t, sign * (x["c"] - e) / risk,
                       "Time stop: close the trade at market")
                done = True
                break
        if done:
            continue
        if t["status"] == "live" and t["ext"] < MAX_EXT and len(m15) > 20:
            last, prev = m15[-1], m15[-2]
            if last["t"] != t.get("ext_t"):
                far = t["tp"] - e
                prog = (last["c"] - e) / far if far else 0
                highs, lows = swings(m15[-80:], 3)
                ok = False
                if side == "SELL" and lows:
                    ok = (prog >= 0.6 and prev["c"] >= lows[-1] > last["c"]
                          and last["c"] < last["o"])
                if side == "BUY" and highs:
                    ok = (prog >= 0.6 and prev["c"] <= highs[-1] < last["c"]
                          and last["c"] > last["o"])
                if ok:
                    old = t["tp"]
                    t["tp"] = old + sign * risk
                    t["sl"] = max(t["sl"], e) if side == "BUY" \
                        else min(t["sl"], e)
                    t["ext"] += 1
                    t["ext_t"] = last["t"]
                    send(f"{TAG}: {ASSET} {side}: still strong\n"
                         f"New structure break in your direction.\n"
                         f"Move TP: {old:.2f} -> {t['tp']:.2f}\n"
                         f"Move SL to breakeven: {t['sl']:.2f}")
        keep.append(t)
    state["trades"] = keep

def main():
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if state["day"]["d"] != today:
        state["day"] = {"d": today, "n": 0}
    m15 = candles("15min", 300)
    if not m15 or len(m15) < 80:
        if MANUAL:
            send(f"{TAG} data error: " + "; ".join(ERRORS or ["no 15m data"]))
        return
    now_data = m15[-1]["dt"] + timedelta(minutes=15)
    if state["trades"]:
        track(m15)
    rows_by = {"15min": m15}
    for tf in FETCH_ORDER:     # bigger timeframes only when a new candle closed
        nxt = state["next"].get(tf)
        if nxt is None or now_data >= parse(nxt):
            rows = candles(tf, 120 if tf == "1day" else 200)
            if rows and len(rows) > 30:
                rows_by[tf] = rows
                state["trend"][tf] = bias(rows)
                state["next"][tf] = (rows[-1]["dt"] + timedelta(
                    minutes=2 * DUR[tf])).strftime("%Y-%m-%d %H:%M:%S")
    sent_now = 0
    for tf in SCAN_TFS:
        rows = rows_by.get(tf)
        if not rows:
            continue
        if state["scanned"].get(tf) == rows[-1]["t"]:
            continue
        state["scanned"][tf] = rows[-1]["t"]
        if len(state["trades"]) >= MAX_OPEN or state["day"]["n"] >= MAX_PER_DAY:
            continue
        tr = state["trend"].get(HTF[tf])
        hits = []
        for name, need in PLAN[tf]:
            if need and USE_HTF:
                if not tr:
                    continue
                wants = [tr]
            else:
                wants = ["BUY", "SELL"]
            for w in wants:
                s = STRATS[name](rows, tf, w)
                if s:
                    hits.append((name, s))
        if not hits:
            continue
        sides = {}
        for nm, s in hits:
            sides.setdefault(s["side"], []).append((nm, s))
        side = max(sides, key=lambda k: len(sides[k]))
        group = sorted(sides[side], key=lambda x: PREF[x[0]])
        keys = [f"{tf}:{side}:{nm}:{s['t']}" for nm, s in group]
        if any(k in state["sent"] for k in keys):
            continue
        if any(t["side"] == side for t in state["trades"]):
            continue
        state["sent"].extend(keys)
        s = group[0][1]
        lv = at_level(rows, s["entry"])
        points = len(group) + (1 if lv else 0)
        if tf == "15min" and points < MIN_POINTS_15M:
            continue
        state["day"]["n"] += 1
        strength = ("STRONG" if points >= 3 else "Good" if points == 2
                    else "Normal")
        state["trades"].append({
            "tf": tf, "side": side, "entry": s["entry"], "sl": s["sl"],
            "tp": s["tp"], "risk": s["risk"], "status": "pending",
            "time": rows[-1]["t"], "checked": m15[-1]["t"], "n": 0,
            "ext": 0, "pend": PEND_MAX[tf], "hold": HOLD_MAX[tf]})
        lvl_line = (f"Key level near entry: {lv['p']:.2f} "
                    f"({lv['n']} touches)\n") if lv else ""
        send(f"{TAG}: {ASSET} {side} LIMIT ({tf})\n"
             f"Setup: " + " + ".join(NAMES[nm] for nm, _ in group) + "\n"
             f"Strength: {strength} ({points} signals)\n"
             f"{HTF[tf]} trend: {tr or 'none'}\n" + lvl_line +
             f"Place a {side} LIMIT at {s['entry']:.2f}\n"
             f"SL: {s['sl']:.2f}\nTP: {s['tp']:.2f}\n"
             f"RR: 1:{s['rr']:.1f}\n"
             f"Valid about {PEND_MAX[tf] * 15 / 60:.0f} hours. If price "
             f"reaches TP first, cancel it. I tell you when it fills.")
        sent_now += 1
    print("Errors:", ERRORS, "| signals:", sent_now)
    if MANUAL and not sent_now:
        if ERRORS:
            send(f"{TAG} data error: " + "; ".join(ERRORS))
        else:
            tr = state["trend"]
            send(f"{TAG} scan done.\nTrends: 1h {tr.get('1h') or 'none'}, "
                 f"4h {tr.get('4h') or 'none'}, 1day {tr.get('1day') or 'none'}\n"
                 f"Open trades: {len(state['trades'])}\n{record_text()}")

main()
save_state(state)
