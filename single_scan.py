import sys
import requests
import os

symbol = sys.argv[1]

print(f"Analyzing: {symbol}")

# ---- SAMPLE RESULT (replace later with real scanner output)
signal = f"📊 AlphaEdge Alert\nSymbol: {symbol}\nStatus: SCANNED"

# ---- TELEGRAM SEND ----
telegram_token = os.environ.get("TELEGRAM_TOKEN")
chat_id = os.environ.get("CHAT_ID")

if telegram_token and chat_id:

    url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"

    payload = {
        "chat_id": chat_id,
        "text": signal
    }

    response = requests.post(url, json=payload)

    print("Telegram status:", response.status_code)
else:
    print("Missing Telegram credentials")
