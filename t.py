# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "aiohttp",
#     "py-clob-client",
#     "python-dotenv",
#     "rich",
# ]
# ///

import os
import sys
import json
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs
from py_clob_client.constants import POLYGON

# --- CONFIG ---
load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TEST_BUY")

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
POLYMARKET_PROXY = os.getenv("POLYMARKET_PROXY", "0x2BA56d3A4492Cda34c31dA0a8d0a48c7e9932560")
CLOB_API = "https://clob.polymarket.com"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"

if not PRIVATE_KEY:
    print("Error: PRIVATE_KEY not set")
    sys.exit(1)

# --- CLIENT INIT ---
try:
    client = ClobClient(
        host=CLOB_API,
        key=PRIVATE_KEY,
        chain_id=POLYGON,
        signature_type=2,
        funder=POLYMARKET_PROXY
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    print(f"[+] Client initialized for proxy: {POLYMARKET_PROXY}")
except Exception as e:
    print(f"[!] Client init failed: {e}")
    sys.exit(1)


def get_15min_window_epoch(offset=0):
    now = int(datetime.now(timezone.utc).timestamp())
    window = 900
    start = (now // window) * window
    return start + (offset * window)


async def run_test():
    async with aiohttp.ClientSession() as session:
        # 1. FIND MARKET
        print("[*] Finding active market...")
        market = None

        # Check current and next window
        for offset in [0, 1]:
            epoch = get_15min_window_epoch(offset)
            for sym in ['xrp', 'btc', 'eth', 'sol']:
                slug = f"{sym}-updown-15m-{epoch}"
                try:
                    url = f"{GAMMA_MARKETS_URL.replace('/markets', '')}/events"
                    async with session.get(url, params={"slug": slug}) as r:
                        if r.status == 200:
                            data = await r.json()
                            if data and not data[0].get('closed'):
                                m = data[0]['markets'][0]
                                end_dt = datetime.fromisoformat(m['endDate'].replace('Z', '+00:00'))

                                # Must end in future
                                if end_dt > datetime.now(timezone.utc):
                                    t_ids = m['clobTokenIds']
                                    if isinstance(t_ids, str): t_ids = json.loads(t_ids)
                                    market = {"slug": slug, "token_id": t_ids[0]}  # Buy YES
                                    break
                except:
                    continue
            if market: break

        if not market:
            print("[!] No active market found.")
            return

        print(f"[+] Found: {market['slug']}")

        # 2. GET PRICE
        print("[*] Checking price...")
        try:
            book = client.get_order_book(market['token_id'])
            # Handling different library response formats
            asks = getattr(book, 'asks', [])
            if not asks and isinstance(book, dict): asks = book.get('asks', [])

            if not asks:
                print("[!] Orderbook empty.")
                return

            price = float(asks[0].price) if hasattr(asks[0], 'price') else float(asks[0][0])
            print(f"[*] Lowest Ask: {price}")
        except Exception as e:
            print(f"[!] Error fetching book: {e}")
            return

        # 3. EXECUTE (With Correct Expiration)
        buy_usd = 2.0
        size = round(buy_usd / price, 2)

        # --- THE FIX ---
        # Polymarket requires expiration > now + 60s
        # We set it to 120s (2 mins) to be safe and fast.
        expiration = int((datetime.now(timezone.utc) + timedelta(minutes=2)).timestamp())

        print(f"[*] Buying {size} shares @ {price} (Exp: +2m)")

        try:
            order = OrderArgs(
                price=price,
                size=size,
                side="BUY",
                token_id=market['token_id'],
                expiration=expiration
            )

            signed = client.create_order(order)
            resp = client.post_order(signed, orderType="GTD")

            print("-" * 20)
            print(json.dumps(resp, indent=2))
            print("-" * 20)

            if isinstance(resp, dict) and resp.get("orderID"):
                print("[SUCCESS] Order Placed!")
            else:
                print("[FAIL] Order not accepted.")

        except Exception as e:
            print(f"[CRITICAL] {e}")


if __name__ == "__main__":
    asyncio.run(run_test())