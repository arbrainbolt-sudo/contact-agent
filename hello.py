import os
import requests
from dotenv import load_dotenv

load_dotenv()                                   # read the .env file
API_KEY = os.getenv("OPENROUTER_API_KEY")       # pull the key out of it

response = requests.post(
    "https://openrouter.ai/api/v1/chat/completions",
    headers={"Authorization": f"Bearer {API_KEY}"},
    json={
        "model": "openrouter/free",
        "messages": [{"role": "user", "content": "Say hello in exactly five words."}],
    },
    timeout=60,
)

print(response.json()["choices"][0]["message"]["content"])