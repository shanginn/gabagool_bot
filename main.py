# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "aiohttp",
#     "py-clob-client",
#     "python-dotenv",
#     "rich",
#     "websockets",
# ]
# ///

import asyncio
import os
import sys
import json
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.constants import POLYGON

from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich import box

# --- CONFIGURATION ---
load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
POLYMARKET_PROXY = os.getenv("POLYMARKET_PROXY", "0x2BA56d3A4492Cda34c31dA0a8d0a48c7e9932560")
if not PRIVATE_KEY:
    print("[ERROR] PRIVATE_KEY not found in .env")
    sys.exit(1)

# STRATEGY SETTINGS
TARGET_SPREAD = 0.02  # We want Pair Cost < 0.98
BET_SIZE_USDC = 10.0  # Size per clip
MAX_EXPOSURE = 500.0  # Max capital allocated
WS_ENDPOINT = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

# LOGGING
logging.basicConfig(level=logging.ERROR)


# --- STATE MANAGEMENT ---

class MarketState:
    def __init__(self):
        self.reset()
        self.status = "Initializing..."
        self.total_realized_pnl = 0.0
        self.total_trades_session = 0
        self.debug = ""

    def reset(self):
        self.slug = ""
        self.question = ""
        self.token_yes = ""
        self.token_no = ""
        self.end_time = datetime.now(timezone.utc)
        self.debug = ""

        # Market Data
        self.ask_yes = 0.0
        self.ask_no = 0.0

        # Position Data
        self.qty_yes = 0.0
        self.cost_yes = 0.0
        self.qty_no = 0.0
        self.cost_no = 0.0

    @property
    def avg_yes(self): return self.cost_yes / self.qty_yes if self.qty_yes else 0.0

    @property
    def avg_no(self): return self.cost_no / self.qty_no if self.qty_no else 0.0

    @property
    def locked_profit(self):
        common = min(self.qty_yes, self.qty_no)
        if common == 0: return 0.0
        cost_basis = (self.avg_yes * common) + (self.avg_no * common)
        return common - cost_basis


# --- UI ---

def render_dashboard(state: MarketState) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body", ratio=1),
        Layout(name="footer", size=5)
    )

    # Header
    layout["header"].update(Panel(f"🧠 GABAGOOL BOT | STATUS: [bold green]{state.status}[/]"))

    # Stats Table
    table = Table(box=box.SIMPLE_HEAD, expand=True)
    table.add_column("Metric", style="cyan")
    table.add_column("YES", style="green")
    table.add_column("NO", style="red")
    table.add_column("Signal", style="yellow")

    pair_cost_now = state.ask_yes + state.ask_no
    
    # Current market prices
    table.add_row("Current Ask", f"${state.ask_yes:.3f}", f"${state.ask_no:.3f}", f"Pair: {pair_cost_now:.3f}")
    
    # Position info
    table.add_row("My Quantity", f"{state.qty_yes:.1f}", f"{state.qty_no:.1f}", f"Total: {state.qty_yes + state.qty_no:.1f}")
    table.add_row("My Avg Cost", f"${state.avg_yes:.3f}", f"${state.avg_no:.3f}", f"Profit: ${state.locked_profit:.2f}")
    
    # Decision signals
    if state.ask_yes > 0 and state.ask_no > 0:
        pair_cost_yes = state.ask_yes + state.avg_no
        pair_cost_no = state.ask_no + state.avg_yes
        
        yes_signal = "✓ BUY" if (state.qty_no > 0 and pair_cost_yes < 0.98) else ""
        no_signal = "✓ BUY" if (state.qty_yes > 0 and pair_cost_no < 0.98) else ""
        
        # Entry signal: buy when pair_cost is cheap enough to lock profit
        pair_cost = state.ask_yes + state.ask_no
        entry_signal = f"ENTRY ({pair_cost:.3f})" if pair_cost < 0.98 else ""
        
        table.add_row("Signals", yes_signal, no_signal, entry_signal)

    # Body
    body_content = Table.grid(expand=True)
    body_content.add_row(Panel(table, title=f"Market: {state.question}"))
    layout["body"].update(body_content)

    # Footer
    profit_color = "green" if state.total_realized_pnl > 0 else "yellow"
    footer_text = (
        f"Session Trades: {state.total_trades_session} | "
        f"Exposure: ${state.cost_yes + state.cost_no:.2f} | "
        f"Session PnL: [{profit_color}]${state.total_realized_pnl:.2f}[/]"
    )
    layout["footer"].update(Panel(footer_text))

    return layout


# --- NETWORK & STRATEGY ---

class Bot:
    def __init__(self):
        self.state = MarketState()
        # Use MetaMask browser wallet setup
        self.client = ClobClient(
            host=CLOB_API,
            key=PRIVATE_KEY,
            chain_id=POLYGON,
            signature_type=2,  # Browser wallet (MetaMask)
            funder=POLYMARKET_PROXY
        )
        try:
            self.client.set_api_creds(self.client.create_or_derive_api_creds())
        except Exception:
            pass  # Usually means creds already exist or derived successfully

    def get_15min_window_epoch(self, offset_windows=0) -> int:
        """Calculate epoch timestamp for 15-min trading windows"""
        now = int(datetime.now(timezone.utc).timestamp())
        window_size = 900  # 15 minutes in seconds
        current_window_start = (now // window_size) * window_size
        return current_window_start + (offset_windows * window_size)

    async def fetch_positions(self, session: aiohttp.ClientSession):
        """Fetch real positions from Polymarket Data API"""
        try:
            async with session.get(
                f"{DATA_API}/positions",
                params={"user": POLYMARKET_PROXY, "sizeThreshold": 0},
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    positions = await resp.json()
                    for pos in positions:
                        asset = pos.get('asset', '')
                        size = float(pos.get('size', 0))
                        avg_price = float(pos.get('avgPrice', 0))
                        
                        if asset == self.state.token_yes:
                            self.state.qty_yes = size
                            self.state.cost_yes = size * avg_price
                        elif asset == self.state.token_no:
                            self.state.qty_no = size
                            self.state.cost_no = size * avg_price
        except Exception as e:
            self.state.debug = f"Position fetch error: {str(e)[:30]}"

    async def discover_market(self):
        """
        Finds 15-minute up/down crypto markets using slug pattern:
        {symbol}-updown-15m-{epoch}
        
        Tries: XRP, SOL, ETH, BTC in that order
        Checks current and next 15-min windows
        """
        self.state.status = "Scanning 15-min windows..."
        async with aiohttp.ClientSession() as session:
            try:
                crypto_symbols = ['xrp'] #, 'sol', 'eth', 'btc']
                
                # Try current and next 15-min window
                for offset in [0, 1]:
                    epoch = self.get_15min_window_epoch(offset)
                    
                    # Try each crypto
                    for symbol in crypto_symbols:
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
                                if not events or len(events) == 0:
                                    continue
                                
                                event = events[0]
                                
                                # Skip if closed
                                if event.get('closed'):
                                    continue
                                
                                markets = event.get('markets', [])
                                if len(markets) == 0:
                                    continue
                                
                                market = markets[0]
                                
                                # Check end date
                                end_date_str = market.get('endDate') or event.get('endDate')
                                if not end_date_str:
                                    continue
                                
                                end_dt = datetime.fromisoformat(
                                    end_date_str.replace('Z', '+00:00')
                                )
                                if end_dt <= datetime.now(timezone.utc):
                                    continue
                                
                                # Parse token IDs
                                tokens = market.get('clobTokenIds', [])
                                if isinstance(tokens, str):
                                    tokens = json.loads(tokens)
                                
                                if len(tokens) < 2:
                                    continue
                                
                                time_left = (end_dt - datetime.now(timezone.utc)).total_seconds()
                                self.state.status = f"Found: {symbol.upper()} 15-min (ends in {int(time_left)}s)"
                                
                                return {
                                    'id': market.get('id') or event.get('id'),
                                    'slug': market.get('slug') or event.get('slug') or slug,
                                    'question': market.get('question') or event.get('title'),
                                    'endDate': end_date_str,
                                    'clobTokenIds': tokens,
                                }
                        except Exception:
                            continue
                
                self.state.status = "No 15-min market found. Retrying..."
                await asyncio.sleep(1)
                
            except Exception as e:
                self.state.status = f"Discovery Failed: {str(e)}"
                await asyncio.sleep(2)
        
        return None

    async def place_order(self, token_id, price, side_str):
        """Non-blocking order execution."""
        try:
            size = round(BET_SIZE_USDC / price, 2)
            if size < 1: return

            # Simple check: Don't exceed exposure
            if (self.state.cost_yes + self.state.cost_no) > MAX_EXPOSURE:
                return

            expiration = int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp())
            order = OrderArgs(
                price=price, size=size, side="BUY", token_id=token_id, expiration=expiration
            )

            loop = asyncio.get_running_loop()
            signed_order = await loop.run_in_executor(None, lambda: self.client.create_order(order))
            resp = await loop.run_in_executor(None, lambda: self.client.post_order(signed_order, orderType="GTD"))

            # If order succeeded (no exception), update state
            if resp:
                self.state.total_trades_session += 1
                cost = size * price
                if side_str == "YES":
                    self.state.qty_yes += size
                    self.state.cost_yes += cost
                else:
                    self.state.qty_no += size
                    self.state.cost_no += cost
        except Exception:
            pass

    async def run(self):
        with Live(render_dashboard(self.state), refresh_per_second=4, screen=True) as live:
            while True:
                # 1. Discovery
                market = await self.discover_market()

                if not market:
                    self.state.status = "No 15-min BTC market found. Retrying..."
                    live.update(render_dashboard(self.state))
                    await asyncio.sleep(3)
                    continue

                # 2. Setup
                self.state.reset()
                self.state.question = market['question']
                self.state.slug = market['slug']

                # Parse Token IDs safely
                try:
                    t_ids = market.get('clobTokenIds', [])
                    if isinstance(t_ids, str): t_ids = json.loads(t_ids)
                    self.state.token_yes = t_ids[0]
                    self.state.token_no = t_ids[1]
                except:
                    self.state.status = "Error parsing Token IDs"
                    live.update(render_dashboard(self.state))
                    await asyncio.sleep(3)
                    continue

                end_str = market['endDate'].replace('Z', '+00:00')
                self.state.end_time = datetime.fromisoformat(end_str)

                self.state.status = f"LIVE: {self.state.slug}"
                live.update(render_dashboard(self.state))

                # 3. Execution (WebSocket)
                try:
                    self.state.debug = "Creating session..."
                    live.update(render_dashboard(self.state))
                    async with aiohttp.ClientSession() as session:
                        # Fetch existing positions for this market
                        self.state.debug = "Fetching positions..."
                        live.update(render_dashboard(self.state))
                        await self.fetch_positions(session)
                        
                        self.state.status = "Connecting to WebSocket..."
                        self.state.debug = "Connecting..."
                        live.update(render_dashboard(self.state))
                        async with session.ws_connect(WS_ENDPOINT, ssl=False, timeout=aiohttp.ClientTimeout(total=10)) as ws:
                            # Sub to Orderbook
                            sub_msg = {
                                "type": "market",
                                "assets_ids": [self.state.token_yes, self.state.token_no]
                            }
                            self.state.debug = "Subscribed"
                            live.update(render_dashboard(self.state))
                            await ws.send_json(sub_msg)

                            self.state.status = f"LIVE: {self.state.slug}"
                            self.state.debug = "Listening..."
                            live.update(render_dashboard(self.state))
                            msg_count = 0
                            try:
                                while datetime.now(timezone.utc) < self.state.end_time:
                                    msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
                                    msg_count += 1
                                    
                                    if msg.type == aiohttp.WSMsgType.TEXT:
                                        data = json.loads(msg.data)
                                        self.state.debug = f"Msg {msg_count}: {str(data)[:120]}"
                                        
                                        # Parse market data structure
                                        if isinstance(data, dict):
                                            # Check for 'price_changes' key
                                            price_changes = data.get('price_changes', [])
                                            for change in price_changes:
                                                if isinstance(change, dict):
                                                    try:
                                                        asset_id = change.get('asset_id', '')
                                                        price = float(change.get('price', 0))
                                                        side = change.get('side', 'BUY')  # SELL = ask price
                                                        
                                                        if side == 'SELL':
                                                            if asset_id == self.state.token_yes:
                                                                self.state.ask_yes = price
                                                            elif asset_id == self.state.token_no:
                                                                self.state.ask_no = price
                                                    except:
                                                        pass

                                        # --- STRATEGY ---
                                        # Need valid prices
                                        if self.state.ask_yes > 0 and self.state.ask_no > 0:
                                            pair_cost = self.state.ask_yes + self.state.ask_no
                                            action = ""
                                            
                                            # Entry: Buy the cheaper side when pair_cost is attractive
                                            if pair_cost < (1.0 - TARGET_SPREAD):
                                                if self.state.ask_yes <= self.state.ask_no:
                                                    await self.place_order(self.state.token_yes, self.state.ask_yes, "YES")
                                                    action = "BUY_YES"
                                                else:
                                                    await self.place_order(self.state.token_no, self.state.ask_no, "NO")
                                                    action = "BUY_NO"
                                            
                                            if action:
                                                self.state.debug = f"[{action}] Y:{self.state.ask_yes:.3f} N:{self.state.ask_no:.3f} Cost:{pair_cost:.3f}"
                                            else:
                                                self.state.debug = f"Y:{self.state.ask_yes:.3f} N:{self.state.ask_no:.3f} Cost:{pair_cost:.3f}"

                                        live.update(render_dashboard(self.state))

                                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                        self.state.debug = f"WS closed: {msg.type}"
                                        break
                            except asyncio.TimeoutError:
                                self.state.debug = "Receive timeout"
                except Exception as e:
                    self.state.status = f"WS Error: {str(e)[:50]}"
                    self.state.debug = str(e)[:80]
                    live.update(render_dashboard(self.state))
                    await asyncio.sleep(2)

                # Market ended loop
                self.state.status = "Market Ended. Finding new one..."
                live.update(render_dashboard(self.state))
                await asyncio.sleep(2)


if __name__ == "__main__":
    try:
        bot = Bot()
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        pass