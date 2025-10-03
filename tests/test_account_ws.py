
import asyncio
import json
import os
import websockets
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# --- Configuration ---
URL = "ws://api.starknet.extended.exchange/stream.extended.exchange/v1/account"

HEADERS = {
    "User-Agent": "extended-mm-bot/0.1",
    "Origin": "https://app.extended.exchange",
}

# Load API Key from .env file
API_KEY = os.getenv("EXTENDED_API_KEY")
if API_KEY:
    HEADERS["X-Api-Key"] = API_KEY
    print("--- API Key loaded successfully. ---")
else:
    print("--- FATAL: EXTENDED_API_KEY not found in .env file. Cannot proceed. ---")
    exit()

async def listen_account_updates():
    """Connects to the private account WebSocket and prints all incoming messages."""
    print(f"--- Attempting to connect to: {URL} ---")
    try:
        async with websockets.connect(URL, extra_headers=HEADERS, ping_interval=15, ping_timeout=10) as ws:
            print("--- ✅ Connection SUCCESSFUL ---")
            print("--- Waiting for account updates (e.g., orders, positions, trades)... ---")
            print("--- You can now go to the exchange website and manually place or cancel an order to trigger an event. ---")
            
            while True:
                try:
                    message = await ws.recv()
                    print("\n--- 🎉 Received Message: ---")
                    # Pretty print the JSON message
                    print(json.dumps(json.loads(message), indent=2))
                except websockets.exceptions.ConnectionClosed as e:
                    print(f"--- ❌ Connection closed unexpectedly: {e} ---")
                    break
                except Exception as e:
                    print(f"--- 🚨 An error occurred while receiving: {e} ---")
                    break
    except websockets.exceptions.InvalidStatusCode as e:
        print(f"--- ❌ FAILED to connect: HTTP Status {e.status_code} ---")
        print("--- Please double-check your API Key and ensure it has the correct permissions. ---")
    except Exception as e:
        print(f"--- ❌ FAILED to connect with an unexpected error: {e} ---")

if __name__ == "__main__":
    try:
        asyncio.run(listen_account_updates())
    except KeyboardInterrupt:
        print("\n--- Test script terminated by user. ---")
