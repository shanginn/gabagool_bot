# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "aiohttp",
#     "py-clob-client",
#     "python-dotenv",
# ]
# ///

import asyncio
import os
import sys
import json
from datetime import datetime, timezone, timedelta

import aiohttp
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs
from py_clob_client.constants import POLYGON

load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
POLYMARKET_PROXY = os.getenv("POLYMARKET_PROXY", "0x2BA56d3A4492Cda34c31dA0a8d0a48c7e9932560")
if not PRIVATE_KEY:
    print("[ERROR] PRIVATE_KEY not found in .env")
    sys.exit(1)

CLOB_API = "https://clob.polymarket.com"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
BET_SIZE_USDC = 2.0  # Min $1, but use $2 for safety margin
POLYGON = 137


async def get_current_market():
    """Find current 15-min market"""
    async with aiohttp.ClientSession() as session:
        now = int(datetime.now(timezone.utc).timestamp())
        window_size = 900
        current_window_start = (now // window_size) * window_size
        
        for offset in [0, 1]:
            epoch = current_window_start + (offset * window_size)
            for symbol in ['xrp', 'sol', 'eth', 'btc']:
                slug = f"{symbol}-updown-15m-{epoch}"
                try:
                    async with session.get(
                        f"{GAMMA_MARKETS_URL.replace('/markets', '')}/events",
                        params={"slug": slug},
                        timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        if resp.status != 200:
                            continue
                        
                        events = await resp.json()
                        if not events:
                            continue
                        
                        event = events[0]
                        if event.get('closed'):
                            continue
                        
                        markets = event.get('markets', [])
                        if not markets:
                            continue
                        
                        market = markets[0]
                        end_date_str = market.get('endDate') or event.get('endDate')
                        if not end_date_str:
                            continue
                        
                        end_dt = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                        if end_dt <= datetime.now(timezone.utc):
                            continue
                        
                        tokens = market.get('clobTokenIds', [])
                        if isinstance(tokens, str):
                            tokens = json.loads(tokens)
                        
                        if len(tokens) < 2:
                            continue
                        
                        return {
                            'id': market.get('id'),
                            'slug': slug,
                            'question': market.get('question') or event.get('title'),
                            'tokens': tokens,
                        }
                except:
                    continue
    return None


async def main():
    print("Finding current market...")
    market = await get_current_market()
    
    if not market:
        print("No market found")
        return
    
    print(f"\n✓ Market: {market['question']}")
    print(f"  Slug: {market['slug']}")
    print(f"  YES Token: {market['tokens'][0]}")
    print(f"  NO Token: {market['tokens'][1]}")
    
    # Initialize client with MetaMask browser wallet setup
    client = ClobClient(
        host=CLOB_API,
        key=PRIVATE_KEY,
        chain_id=POLYGON,
        signature_type=2,  # Browser wallet (MetaMask)
        funder=POLYMARKET_PROXY
    )
    try:
        client.set_api_creds(client.create_or_derive_api_creds())
    except:
        pass
    
    # Get current prices
    print("\nFetching prices...")
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect("wss://ws-subscriptions-clob.polymarket.com/ws/market", ssl=False) as ws:
            await ws.send_json({
                "type": "market",
                "assets_ids": market['tokens']
            })
            
            prices = {market['tokens'][0]: 0, market['tokens'][1]: 0}
            timeout = 5
            start = datetime.now(timezone.utc)
            
            while (datetime.now(timezone.utc) - start).total_seconds() < timeout:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        if isinstance(data, list):
                            data = {'price_changes': data}
                        
                        price_changes = data.get('price_changes', [])
                        for change in price_changes:
                            if isinstance(change, dict):
                                asset = change.get('asset_id')
                                price = float(change.get('price', 0))
                                if asset in prices and change.get('side') == 'SELL':
                                    prices[asset] = price
                        
                        if prices[market['tokens'][0]] > 0 and prices[market['tokens'][1]] > 0:
                            break
                except asyncio.TimeoutError:
                    continue
            
            print(f"YES (token {market['tokens'][0][:8]}...): ${prices[market['tokens'][0]]:.3f}")
            print(f"NO  (token {market['tokens'][1][:8]}...): ${prices[market['tokens'][1]]:.3f}")
            
            # Choose cheaper side
            if prices[market['tokens'][0]] < prices[market['tokens'][1]]:
                chosen = market['tokens'][0]
                side_name = "YES"
                chosen_price = prices[market['tokens'][0]]
            else:
                chosen = market['tokens'][1]
                side_name = "NO"
                chosen_price = prices[market['tokens'][1]]
            
            print(f"\n→ Betting on {side_name} at ${chosen_price:.3f}")
            
            size = round(BET_SIZE_USDC / chosen_price, 2)
            print(f"  Size: {size} shares")
            
            # Set expiration to 5 minutes in future (buffer for submission delay)
            expiration = int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp())
            
            order = OrderArgs(
                price=chosen_price,
                size=size,
                side="BUY",
                token_id=chosen,
                expiration=expiration
            )
            
            print("\nPlacing order...")
            try:
                signed_order = client.create_order(order)
                print(f"✓ Order signed!")
                
                # Submit the signed order
                print("Submitting to blockchain...")
                submit_resp = client.post_order(signed_order, orderType="GTD")
                print(f"✓ Order submitted!")
                print(f"  Order ID: {submit_resp}")
            except Exception as e:
                print(f"✗ Order failed: {e}")
                import traceback
                traceback.print_exc()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
