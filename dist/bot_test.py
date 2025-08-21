import requests

BOT_TOKEN = "7553859737:AAHvOlNSgPAyKBEUTIM0dXKIGl2FCCOxWXw"
CHAT_ID = "5587732582"

message = "✅ Bot is working! Hello from your Solana Wallet Tracker."

url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
params = {"chat_id": CHAT_ID, "text": message}

res = requests.get(url, params=params)
print(res.json())
