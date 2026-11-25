"""Use your local AI from any device on your Wi-Fi / LAN.

Setup:  pip install openai
"""
import json
from pathlib import Path

from openai import OpenAI

# On the gateway PC the key is read from gateway-keys.json; on other devices paste your lan_key.
KEYS = Path(__file__).with_name("gateway-keys.json")
LAN_KEY = json.loads(KEYS.read_text(encoding="utf-8"))["lan_key"] if KEYS.exists() else "paste-your-lan-key"

client = OpenAI(
    # On the gateway PC itself you can use localhost.
    # From other devices use: http://<gateway PC's IP>:8000/v1
    base_url="http://localhost:8000/v1",
    api_key=LAN_KEY,
)


def ask(prompt: str) -> str:
    raw = client.chat.completions.with_raw_response.create(
        model="gemma",
        messages=[{"role": "user", "content": prompt}],
    )
    print(f"(answered by: {raw.headers.get('x-served-by')})")
    return raw.parse().choices[0].message.content


def ask_streaming(prompt: str) -> None:
    stream = client.chat.completions.create(
        model="gemma",
        messages=[{"role": "user", "content": prompt}],
        stream=True,
    )
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            print(chunk.choices[0].delta.content, end="", flush=True)
    print()


if __name__ == "__main__":
    print(ask("Give me three project ideas using local LLMs."))
    ask_streaming("Explain what a load-balancing gateway does in two sentences.")
