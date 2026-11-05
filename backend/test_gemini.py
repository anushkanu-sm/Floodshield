import os
from dotenv import load_dotenv
from google import genai

load_dotenv()
key = os.getenv("GEMINI_API_KEY")
print("Key found:", bool(key))

try:
    client = genai.Client(api_key=key)
    resp = client.models.generate_content(
        model="gemini-3.8-flash",
        contents="Say hello in five words.",
    )
    print("REPLY:", resp.text)
except Exception as e:
    print("ERROR:", e)