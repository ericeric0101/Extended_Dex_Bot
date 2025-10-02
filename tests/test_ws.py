
import asyncio
import json
import websockets
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# --- Configuration ---
# This is the URL that seems most correct based on the documentation pattern
URL = "wss://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks/ETH-USD"

# Headers that we've determined might be necessary
HEADERS = {
    "User-Agent": "extended-mm-bot/0.1",
    "Origin": "https://app.extended.exchange",
}

# Add API key from your .env file
API_KEY = os.getenv("EXTENDED_API_KEY")
if API_KEY:
    HEADERS["X-Api-Key"] = API_KEY
    print("--- API Key loaded from .env file. ---")
else:
    print("--- WARNING: EXTENDED_API_KEY not found in .env file. ---")

async def listen():
    """Connects to the WebSocket and prints all incoming messages."""
    print(f"--- Connecting to: {URL} ---")
    try:
        async with websockets.connect(URL, extra_headers=HEADERS, ping_interval=60, ping_timeout=30) as ws:
            print("--- Connection SUCCESSFUL ---")
            print("--- Waiting for messages... (Will time out after 25 seconds if nothing is received) ---")
            while True:
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=25.0)
                    print("--- Received message: ---")
                    # Pretty print the JSON message
                    print(json.dumps(json.loads(message), indent=2))
                except asyncio.TimeoutError:
                    print("--- No message received for 25 seconds. The data stream is not active. Terminating. ---")
                    break
                except websockets.exceptions.ConnectionClosed as e:
                    print(f"--- Connection closed unexpectedly: {e} ---")
                    break
                except Exception as e:
                    print(f"--- An error occurred while receiving: {e} ---")
                    break
    except Exception as e:
        print(f"--- FAILED to connect: {e} ---")

if __name__ == "__main__":
    asyncio.run(listen())
