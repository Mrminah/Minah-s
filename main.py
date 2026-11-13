import os, requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT = os.environ["CHAT_ID"]
KEY = os.environ["TWELVE_KEY"]

def get_candles(interval="5min", size=5):
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={"symbol": "XAU/USD", "interval": interval,
                "outputsize": size, "apikey": KEY},
        timeout=20)
    return r.json()

def send(text):
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT, "text": text}, timeout=20)

data = get_candles()
if "values" in data:
    price = data["values"][0]["close"]
    send(f"Minas is alive ✅\nGold (XAU/USD): {price}")
else:
    send(f"Minas error: {data.get('message', 'unknown')}")
