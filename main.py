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
TARGET_SPREAD = 0.015
BET_SIZE_USDC = 10.0

# --- RISK SETTINGS (NEW) ---
SOFT_LIMIT_USD = 100.0  # Stop opening NEW positions here
HARD_LIMIT_USD = 300.0  # Absolute stop.
MAX_IMBALANCE_SHARES = 50.0  # <--- NEW: CRITICAL FIX. Max difference between YES/NO shares.

# NETWORK CONSTANTS
WS_ENDPOINT = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

# LOGGING
logging.basicConfig(level=logging.ERROR)


# --- UTILS ---
def fire_and_forget(f):
    def wrapped(*args, **kwargs):
        return asyncio.create_task(f(*args, **kwargs))

    return wrapped


# --- RISK MANAGER ---
class RiskManager:
    def __init__(self, soft_limit, hard_limit, max_imbalance):
        self.soft_limit = soft_limit
        self.hard_limit = hard_limit
        self.max_imbalance = max_imbalance

        # Tracks current exposure in USD
        self.current_gross_exposure = 0.0
        # Tracks share counts: {'market_id': {'YES': 0.0, 'NO': 0.0}}
        self.positions = {}

    def reset_for_new_market(self):
        """Resets internal counters when bot switches to a new market"""
        self.current_gross_exposure = 0.0
        self.positions = {}

    def get_position(self, market_id):
        if market_id not in self.positions:
            self.positions[market_id] = {'YES': 0.0, 'NO': 0.0}
        return self.positions[market_id]

    def sync_from_api(self, market_id, qty_yes, cost_yes, qty_no, cost_no):
        """Syncs the Risk Manager state with actual API data"""
        self.current_gross_exposure = cost_yes + cost_no
        self.positions[market_id] = {'YES': qty_yes, 'NO': qty_no}

    def update_post_trade(self, market_id, side, cost, shares):
        """Call this AFTER a trade is successfully executed"""
        self.current_gross_exposure += cost
        position = self.get_position(market_id)
        position[side] += shares

    def check_order_permission(self, market_id, side, estimated_cost, current_qty_yes, current_qty_no) -> tuple[
        bool, str]:
        """
        Returns (Allowed: bool, Reason: str)
        Checks Exposure Limits AND Imbalance Limits.
        """
        # 1. CHECK IMBALANCE (The Fix)
        # If we want to buy YES, we check if YES is already too far ahead of NO
        if side == "YES":
            if (current_qty_yes - current_qty_no) > self.max_imbalance:
                return False, f"IMBALANCE: YES is +{current_qty_yes - current_qty_no:.1f} ahead"
        elif side == "NO":
            if (current_qty_no - current_qty_yes) > self.max_imbalance:
                return False, f"IMBALANCE: NO is +{current_qty_no - current_qty_yes:.1f} ahead"

        # 2. CHECK FINANCIAL LIMITS
        projected_exposure = self.current_gross_exposure + estimated_cost

        # Hard Limit
        if projected_exposure > self.hard_limit:
            return False, f"HARD LIMIT: ${projected_exposure:.2f} > ${self.hard_limit}"

        # Soft Limit
        if projected_exposure <= self.soft_limit:
            return True, "OK"

        # Buffer Zone (Only Allow Catch-up Trades)
        # If we are in buffer zone, we ONLY allow trades that reduce imbalance
        is_catchup = False
        if side == "YES" and current_qty_yes < current_qty_no: is_catchup = True
        if side == "NO" and current_qty_no < current_qty_yes: is_catchup = True

        if is_catchup:
            return True, "OK (Catch-up in Buffer)"
        else:
            return False, "BLOCKED: Risk Increase in Buffer"


# --- STATE MANAGEMENT ---
class MarketState:
    def __init__(self):
        self.reset()
        self.status = "Initializing..."
        self.total_trades_session = 0
        self.debug = ""
        self.last_trade_ts = 0

    def reset(self):
        self.slug = ""
        self.question = ""
        self.token_yes = ""
        self.token_no = ""
        self.end_time = datetime.now(timezone.utc)
        self.debug = ""
        self.ask_yes = 0.0
        self.ask_no = 0.0
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

    @property
    def imbalance(self):
        return self.qty_yes - self.qty_no


# --- UI ---
def render_dashboard(state: MarketState, risk_manager: RiskManager) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body", ratio=1),
        Layout(name="footer", size=10)
    )

    layout["header"].update(Panel(f"🧠 GABAGOOL BOT | STATUS: [bold green]{state.status}[/]"))

    table = Table(box=box.SIMPLE_HEAD, expand=True)
    table.add_column("Metric", style="cyan")
    table.add_column("YES", style="green")
    table.add_column("NO", style="red")
    table.add_column("Action", style="yellow")

    pair_cost_now = state.ask_yes + state.ask_no
    table.add_row("Market Price", f"${state.ask_yes:.3f}", f"${state.ask_no:.3f}", f"Sum: {pair_cost_now:.3f}")
    table.add_row("My Shares", f"{state.qty_yes:.1f}", f"{state.qty_no:.1f}", f"Delta: {state.imbalance:.1f}")
    table.add_row("My Avg Cost", f"${state.avg_yes:.3f}", f"${state.avg_no:.3f}", f"Locked: ${state.locked_profit:.2f}")

    eff_cost_yes = state.ask_yes + (state.avg_no if state.qty_no > 0 else state.ask_no)
    eff_cost_no = state.ask_no + (state.avg_yes if state.qty_yes > 0 else state.ask_yes)

    target = 1.0 - TARGET_SPREAD

    # Risk/Permission Check
    can_buy_yes, reason_yes = risk_manager.check_order_permission(state.slug, "YES", BET_SIZE_USDC, state.qty_yes,
                                                                  state.qty_no)
    can_buy_no, reason_no = risk_manager.check_order_permission(state.slug, "NO", BET_SIZE_USDC, state.qty_yes,
                                                                state.qty_no)

    # Signal Generation
    if not can_buy_yes:
        sig_yes = f"[dim red]{reason_yes}[/]"
    elif eff_cost_yes < target:
        sig_yes = f"[bold green]BUY @ {state.ask_yes:.2f}[/]"
    else:
        sig_yes = "[dim]Wait[/]"

    if not can_buy_no:
        sig_no = f"[dim red]{reason_no}[/]"
    elif eff_cost_no < target:
        sig_no = f"[bold green]BUY @ {state.ask_no:.2f}[/]"
    else:
        sig_no = "[dim]Wait[/]"

    table.add_row("Strategy", sig_yes, sig_no, f"Target < {target:.3f}")

    body_content = Table.grid(expand=True)
    body_content.add_row(Panel(table, title=f"Market: {state.question}"))
    layout["body"].update(body_content)

    exposure_color = "green"
    if risk_manager.current_gross_exposure > SOFT_LIMIT_USD:
        exposure_color = "yellow"
    if risk_manager.current_gross_exposure >= HARD_LIMIT_USD:
        exposure_color = "red"

    stats_header = (
        f"Trades: {state.total_trades_session} | "
        f"Exp: [{exposure_color}]${risk_manager.current_gross_exposure:.2f}[/] | "
        f"Max Delta: {MAX_IMBALANCE_SHARES}"
    )
    log_style = "red" if "Ex" in state.debug or "Err" in state.debug or "Block" in state.debug else "white"
    layout["footer"].update(Panel(state.debug, title=stats_header, style=log_style))
    return layout


# --- BOT IMPLEMENTATION ---
class Bot:
    def __init__(self):
        self.state = MarketState()
        self.risk_manager = RiskManager(
            soft_limit=SOFT_LIMIT_USD,
            hard_limit=HARD_LIMIT_USD,
            max_imbalance=MAX_IMBALANCE_SHARES
        )

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

    def get_15min_window_epoch(self, offset_windows=0) -> int:
        now = int(datetime.now(timezone.utc).timestamp())
        window_size = 900
        current_window_start = (now // window_size) * window_size
        return current_window_start + (offset_windows * window_size)

    async def fetch_positions(self, session: aiohttp.ClientSession):
        try:
            async with session.get(
                    f"{DATA_API}/positions",
                    params={"user": POLYMARKET_PROXY, "sizeThreshold": "0"},
                    timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    positions = await resp.json()
                    if isinstance(positions, list):
                        for pos in positions:
                            if isinstance(pos, dict):
                                asset = pos.get('asset', '')
                                size = float(pos.get('size', 0))
                                avg_price = float(pos.get('avgPrice', 0))

                                if asset == self.state.token_yes:
                                    self.state.qty_yes = size
                                    self.state.cost_yes = size * avg_price
                                elif asset == self.state.token_no:
                                    self.state.qty_no = size
                                    self.state.cost_no = size * avg_price

                        if self.state.slug:
                            self.risk_manager.sync_from_api(
                                self.state.slug,
                                self.state.qty_yes, self.state.cost_yes,
                                self.state.qty_no, self.state.cost_no
                            )

        except Exception as e:
            self.state.debug = f"Pos Error: {str(e)}"

    async def discover_market(self):
        self.state.status = "Scanning 15-min windows..."
        async with aiohttp.ClientSession() as session:
            try:
                crypto_symbols = ['eth']  # Focus on ETH as per user data

                for offset in [0, 1]:
                    epoch = self.get_15min_window_epoch(offset)

                    for symbol in crypto_symbols:
                        slug = f"{symbol}-updown-15m-{epoch}"

                        try:
                            async with session.get(
                                    f"{GAMMA_MARKETS_URL.replace('/markets', '')}/events",
                                    params={"slug": slug},
                                    timeout=aiohttp.ClientTimeout(total=5)
                            ) as resp:
                                if resp.status != 200: continue

                                events = await resp.json()
                                if not events or not isinstance(events, list) or len(events) == 0:
                                    continue

                                event = events[0]
                                if not isinstance(event, dict) or event.get('closed'):
                                    continue

                                markets = event.get('markets', [])
                                if not isinstance(markets, list) or len(markets) == 0:
                                    continue

                                market = markets[0]
                                if not isinstance(market, dict): continue

                                end_date_str = market.get('endDate') or event.get('endDate')
                                if not end_date_str: continue

                                end_dt = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))

                                if end_dt <= datetime.now(timezone.utc):
                                    continue

                                tokens = market.get('clobTokenIds', [])
                                if isinstance(tokens, str):
                                    tokens = json.loads(tokens)

                                if not isinstance(tokens, list) or len(tokens) < 2:
                                    continue

                                return {
                                    'id': market.get('id'),
                                    'slug': market.get('slug') or slug,
                                    'question': market.get('question'),
                                    'endDate': end_date_str,
                                    'clobTokenIds': tokens,
                                }
                        except Exception:
                            continue

                self.state.status = "No active market. Retrying..."
                await asyncio.sleep(1)

            except Exception as e:
                self.state.status = f"Discovery Error: {str(e)}"
                await asyncio.sleep(2)

        return None

    @fire_and_forget
    async def place_order(self, token_id, price, side_str):
        try:
            if (datetime.now().timestamp() - self.state.last_trade_ts) < 0.5: return
            self.state.last_trade_ts = datetime.now().timestamp()

            # --- RISK MANAGER CHECK ---
            allowed, reason = self.risk_manager.check_order_permission(
                self.state.slug,
                side_str,
                BET_SIZE_USDC,
                self.state.qty_yes,
                self.state.qty_no
            )

            if not allowed:
                self.state.debug = f"BLOCKED: {reason}"
                return

            size = round(BET_SIZE_USDC / price, 2)
            if size < 2: return

            expiration = int((datetime.now(timezone.utc) + timedelta(minutes=2)).timestamp())

            order = OrderArgs(
                price=price,
                size=size,
                side="BUY",
                token_id=token_id,
                expiration=expiration
            )

            loop = asyncio.get_running_loop()
            signed_order = await loop.run_in_executor(None, lambda: self.client.create_order(order))
            resp = await loop.run_in_executor(None, lambda: self.client.post_order(signed_order, orderType="GTD"))

            if isinstance(resp, dict) and resp.get("orderID"):
                self.state.total_trades_session += 1
                self.state.debug = f"BOUGHT {side_str} @ {price:.3f}"
                cost = size * price

                if side_str == "YES":
                    self.state.qty_yes += size
                    self.state.cost_yes += cost
                else:
                    self.state.qty_no += size
                    self.state.cost_no += cost

                self.risk_manager.update_post_trade(self.state.slug, side_str, cost, size)

            elif isinstance(resp, list):
                self.state.debug = f"Order Err: {resp}"
            else:
                self.state.debug = f"Order Fail: {resp}"
        except Exception as e:
            self.state.debug = f"Order Ex: {str(e)}"

    async def run(self):
        with Live(render_dashboard(self.state, self.risk_manager), refresh_per_second=4, screen=True) as live:
            while True:
                market = await self.discover_market()
                if not market:
                    await asyncio.sleep(2)
                    continue

                if self.state.slug != market['slug']:
                    self.risk_manager.reset_for_new_market()

                self.state.reset()
                self.state.question = market['question']
                self.state.slug = market['slug']

                try:
                    t_ids = market.get('clobTokenIds', [])
                    self.state.token_yes = t_ids[0]
                    self.state.token_no = t_ids[1]
                except:
                    continue

                self.state.end_time = datetime.fromisoformat(market['endDate'].replace('Z', '+00:00'))

                try:
                    async with aiohttp.ClientSession() as session:
                        await self.fetch_positions(session)
                        self.state.status = "Connecting..."
                        live.update(render_dashboard(self.state, self.risk_manager))

                        async with session.ws_connect(
                                WS_ENDPOINT,
                                ssl=False,
                                timeout=10,
                                heartbeat=20,
                                autoping=True
                        ) as ws:
                            await ws.send_json({
                                "type": "market",
                                "assets_ids": [self.state.token_yes, self.state.token_no]
                            })
                            self.state.status = f"LIVE: {self.state.slug}"

                            while datetime.now(timezone.utc) < self.state.end_time:
                                try:
                                    msg = await asyncio.wait_for(ws.receive(), timeout=3.0)

                                    if msg.type == aiohttp.WSMsgType.TEXT:
                                        data = json.loads(msg.data)

                                        if isinstance(data, dict):
                                            # Update Prices
                                            for change in data.get('price_changes', []):
                                                if isinstance(change, dict) and change.get('side') == 'SELL':
                                                    p = float(change.get('price', 0))
                                                    aid = change.get('asset_id')
                                                    if aid == self.state.token_yes:
                                                        self.state.ask_yes = p
                                                    elif aid == self.state.token_no:
                                                        self.state.ask_no = p

                                            if self.state.ask_yes > 0 and self.state.ask_no > 0:
                                                eff_no = self.state.avg_no if self.state.qty_no > 0 else self.state.ask_no
                                                eff_yes = self.state.avg_yes if self.state.qty_yes > 0 else self.state.ask_yes

                                                # --- STRATEGY CORE ---

                                                # Check permissions FIRST
                                                can_buy_yes, _ = self.risk_manager.check_order_permission(
                                                    self.state.slug, "YES", BET_SIZE_USDC, self.state.qty_yes,
                                                    self.state.qty_no
                                                )
                                                can_buy_no, _ = self.risk_manager.check_order_permission(
                                                    self.state.slug, "NO", BET_SIZE_USDC, self.state.qty_yes,
                                                    self.state.qty_no
                                                )

                                                # Execute YES if permitted and profitable
                                                if can_buy_yes and (self.state.ask_yes + eff_no) < (
                                                        1.0 - TARGET_SPREAD):
                                                    await self.place_order(self.state.token_yes, self.state.ask_yes,
                                                                           "YES")

                                                # Execute NO if permitted and profitable
                                                if can_buy_no and (self.state.ask_no + eff_yes) < (1.0 - TARGET_SPREAD):
                                                    await self.place_order(self.state.token_no, self.state.ask_no, "NO")

                                                # Emergency Arb (Both Cheap)
                                                if (self.state.ask_yes + self.state.ask_no) < 0.99:
                                                    # Only fire if not strictly blocked by hard limit
                                                    if can_buy_yes: await self.place_order(self.state.token_yes,
                                                                                           self.state.ask_yes, "YES")
                                                    if can_buy_no: await self.place_order(self.state.token_no,
                                                                                          self.state.ask_no, "NO")

                                        live.update(render_dashboard(self.state, self.risk_manager))

                                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                        self.state.debug = "WS Closed"
                                        break
                                except asyncio.TimeoutError:
                                    pass
                                except Exception as e:
                                    self.state.debug = f"Net Err: {str(e)}"
                                    break
                except Exception as e:
                    self.state.debug = f"Loop Err: {str(e)}"
                    await asyncio.sleep(1)

                self.state.status = "Market Ended (or Reconnecting)..."
                live.update(render_dashboard(self.state, self.risk_manager))
                await asyncio.sleep(2)


if __name__ == "__main__":
    try:
        bot = Bot()
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        pass