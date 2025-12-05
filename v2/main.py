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

# Markets to track
TRACKED_SYMBOLS = ['xrp', 'sol']

# LOGGING
logging.basicConfig(level=logging.ERROR)


# --- STATE MANAGEMENT ---

class MarketState:
    def __init__(self, symbol: str):
        self.symbol = symbol.upper()
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

def render_market_panel(state: MarketState) -> Panel:
    table = Table(box=box.SIMPLE_HEAD, expand=True)
    table.add_column("Metric", style="cyan")
    table.add_column("YES", style="green")
    table.add_column("NO", style="red")
    table.add_column("Signal", style="yellow")

    pair_cost_now = state.ask_yes + state.ask_no
    
    table.add_row("Current Ask", f"${state.ask_yes:.3f}", f"${state.ask_no:.3f}", f"Pair: {pair_cost_now:.3f}")
    table.add_row("My Quantity", f"{state.qty_yes:.1f}", f"{state.qty_no:.1f}", f"Total: {state.qty_yes + state.qty_no:.1f}")
    table.add_row("My Avg Cost", f"${state.avg_yes:.3f}", f"${state.avg_no:.3f}", f"Profit: ${state.locked_profit:.2f}")
    
    if state.ask_yes > 0 and state.ask_no > 0:
        pair_cost_yes = state.ask_yes + state.avg_no
        pair_cost_no = state.ask_no + state.avg_yes
        
        yes_signal = "✓ BUY" if (state.qty_no > 0 and pair_cost_yes < 0.98) else ""
        no_signal = "✓ BUY" if (state.qty_yes > 0 and pair_cost_no < 0.98) else ""
        entry_signal = ""
        if state.qty_yes == 0 and state.qty_no == 0:
            if state.ask_yes < 0.45:
                entry_signal = "ENTRY YES"
            elif state.ask_no < 0.45:
                entry_signal = "ENTRY NO"
        
        table.add_row("Signals", yes_signal, no_signal, entry_signal)

    profit_color = "green" if state.total_realized_pnl > 0 else "yellow"
    status_line = f"[bold]{state.status}[/] | Trades: {state.total_trades_session} | Exposure: ${state.cost_yes + state.cost_no:.2f} | PnL: [{profit_color}]${state.total_realized_pnl:.2f}[/]"
    
    return Panel(table, title=f"[bold]{state.symbol}[/] - {state.question}", subtitle=status_line)


def render_dashboard(states: list[MarketState]) -> Layout:
    layout = Layout()
    
    layouts = [Layout(name="header", size=3)]
    for i, state in enumerate(states):
        layouts.append(Layout(name=f"market_{i}", ratio=1))
    
    layout.split_column(*layouts)

    layout["header"].update(Panel(f"🧠 GABAGOOL BOT | Tracking: {', '.join(s.symbol for s in states)}"))

    for i, state in enumerate(states):
        layout[f"market_{i}"].update(render_market_panel(state))

    return layout


# --- NETWORK & STRATEGY ---

class MarketRunner:
    def __init__(self, symbol: str, client: ClobClient, state: MarketState):
        self.symbol = symbol.lower()
        self.client = client
        self.state = state

    def get_15min_window_epoch(self, offset_windows=0) -> int:
        now = int(datetime.now(timezone.utc).timestamp())
        window_size = 900
        current_window_start = (now // window_size) * window_size
        return current_window_start + (offset_windows * window_size)

    async def discover_market(self, session: aiohttp.ClientSession):
        self.state.status = "Scanning 15-min windows..."
        try:
            for offset in [0, 1]:
                epoch = self.get_15min_window_epoch(offset)
                slug = f"{self.symbol}-updown-15m-{epoch}"
                
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
                        
                        if event.get('closed'):
                            continue
                        
                        markets = event.get('markets', [])
                        if len(markets) == 0:
                            continue
                        
                        market = markets[0]
                        
                        end_date_str = market.get('endDate') or event.get('endDate')
                        if not end_date_str:
                            continue
                        
                        end_dt = datetime.fromisoformat(
                            end_date_str.replace('Z', '+00:00')
                        )
                        if end_dt <= datetime.now(timezone.utc):
                            continue
                        
                        tokens = market.get('clobTokenIds', [])
                        if isinstance(tokens, str):
                            tokens = json.loads(tokens)
                        
                        if len(tokens) < 2:
                            continue
                        
                        time_left = (end_dt - datetime.now(timezone.utc)).total_seconds()
                        self.state.status = f"Found (ends in {int(time_left)}s)"
                        
                        return {
                            'id': market.get('id') or event.get('id'),
                            'slug': market.get('slug') or event.get('slug') or slug,
                            'question': market.get('question') or event.get('title'),
                            'endDate': end_date_str,
                            'clobTokenIds': tokens,
                        }
                except Exception:
                    continue
            
            self.state.status = "No market found. Retrying..."
            
        except Exception as e:
            self.state.status = f"Discovery Failed: {str(e)[:30]}"
        
        return None

    async def place_order(self, token_id, price, side_str):
        try:
            size = round(BET_SIZE_USDC / price, 2)
            if size < 1: return

            if (self.state.cost_yes + self.state.cost_no) > MAX_EXPOSURE:
                return

            expiration = int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp())
            order = OrderArgs(
                price=price, size=size, side="BUY", token_id=token_id, expiration=expiration
            )

            loop = asyncio.get_running_loop()
            signed_order = await loop.run_in_executor(None, lambda: self.client.create_order(order))
            resp = await loop.run_in_executor(None, lambda: self.client.post_order(signed_order, orderType="GTD"))

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

    async def run_market_loop(self, session: aiohttp.ClientSession, update_callback):
        while True:
            market = await self.discover_market(session)

            if not market:
                self.state.status = "No market found. Retrying..."
                update_callback()
                await asyncio.sleep(3)
                continue

            self.state.reset()
            self.state.question = market['question']
            self.state.slug = market['slug']

            try:
                t_ids = market.get('clobTokenIds', [])
                if isinstance(t_ids, str): t_ids = json.loads(t_ids)
                self.state.token_yes = t_ids[0]
                self.state.token_no = t_ids[1]
            except:
                self.state.status = "Error parsing Token IDs"
                update_callback()
                await asyncio.sleep(3)
                continue

            end_str = market['endDate'].replace('Z', '+00:00')
            self.state.end_time = datetime.fromisoformat(end_str)

            self.state.status = f"LIVE: {self.state.slug}"
            update_callback()

            try:
                async with session.ws_connect(WS_ENDPOINT, ssl=False, timeout=aiohttp.ClientTimeout(total=10)) as ws:
                    sub_msg = {
                        "type": "market",
                        "assets_ids": [self.state.token_yes, self.state.token_no]
                    }
                    await ws.send_json(sub_msg)

                    self.state.status = f"LIVE: {self.state.slug}"
                    update_callback()

                    try:
                        while datetime.now(timezone.utc) < self.state.end_time:
                            msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
                            
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                
                                if isinstance(data, dict):
                                    price_changes = data.get('price_changes', [])
                                    for change in price_changes:
                                        if isinstance(change, dict):
                                            try:
                                                asset_id = change.get('asset_id', '')
                                                price = float(change.get('price', 0))
                                                side = change.get('side', 'BUY')
                                                
                                                if side == 'SELL':
                                                    if asset_id == self.state.token_yes:
                                                        self.state.ask_yes = price
                                                    elif asset_id == self.state.token_no:
                                                        self.state.ask_no = price
                                            except:
                                                pass

                                    if self.state.ask_yes > 0 and self.state.ask_no > 0:
                                        pair_cost = self.state.ask_yes + self.state.ask_no
                                        
                                        pair_cost_yes = self.state.ask_yes + self.state.avg_no
                                        if self.state.qty_no > 0 and pair_cost_yes < (1.0 - TARGET_SPREAD):
                                            await self.place_order(self.state.token_yes, self.state.ask_yes, "YES")

                                        pair_cost_no = self.state.ask_no + self.state.avg_yes
                                        if self.state.qty_yes > 0 and pair_cost_no < (1.0 - TARGET_SPREAD):
                                            await self.place_order(self.state.token_no, self.state.ask_no, "NO")

                                        if self.state.qty_yes == 0 and self.state.qty_no == 0:
                                            if self.state.ask_yes < 0.45:
                                                await self.place_order(self.state.token_yes, self.state.ask_yes, "YES")
                                            elif self.state.ask_no < 0.45:
                                                await self.place_order(self.state.token_no, self.state.ask_no, "NO")

                                    update_callback()

                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                    except asyncio.TimeoutError:
                        pass
            except Exception as e:
                self.state.status = f"WS Error: {str(e)[:30]}"
                update_callback()
                await asyncio.sleep(2)

            self.state.status = "Market Ended. Finding new one..."
            update_callback()
            await asyncio.sleep(2)


class Bot:
    def __init__(self):
        self.states = [MarketState(symbol) for symbol in TRACKED_SYMBOLS]
        self.client = ClobClient(
            host=CLOB_API,
            key=PRIVATE_KEY,
            chain_id=POLYGON,
            signature_type=2,
            funder=POLYMARKET_PROXY
        )
        try:
            self.client.set_api_creds(self.client.create_or_derive_api_creds())
        except Exception:
            pass

    async def run(self):
        with Live(render_dashboard(self.states), refresh_per_second=4, screen=True) as live:
            def update_ui():
                live.update(render_dashboard(self.states))

            async with aiohttp.ClientSession() as session:
                runners = [
                    MarketRunner(symbol, self.client, state)
                    for symbol, state in zip(TRACKED_SYMBOLS, self.states)
                ]
                
                tasks = [
                    runner.run_market_loop(session, update_ui)
                    for runner in runners
                ]
                
                await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        bot = Bot()
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        pass
