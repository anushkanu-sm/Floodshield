import os
import requests
from dotenv import load_dotenv

load_dotenv()

token = os.getenv("TELEGRAM_TOKEN")
chat_id = os.getenv("TELEGRAM_CHAT_ID")

if not token or not chat_id:
    print("Missing values: check that .env is in the backend folder and has both lines")
else:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    message = "FloodShield test: CRITICAL flood risk at Silk Board Junction. Avoid the underpass."
    response = requests.post(url, data={"chat_id": chat_id, "text": message})
    print(response.status_code, response.text)