import asyncio
import re
import socket
import time
import sys
from collections import deque
from datetime import datetime
import pandas as pd
import ta
from telethon import TelegramClient, events
from binance import AsyncClient, BinanceSocketManager

# ---------------------------
# CONFIGURATION
# ---------------------------
API_ID = 34274522
API_HASH = "17d76bf54444a8cac5eb2d990d0b59fb"
SESSION_NAME = "my_session"
CHANNEL_TARGET = "@cryptoinsidebets"

MAX_CONCURRENT_TRADES = 5
LEVERAGE = 10

# STRATEGY SETTINGS (PNL BASED)
BE_TRIGGER = 2.5        # +2.5% PNL triggers Break-Even
TRAIL_ACTIVATE = 5.0    # +5.0% PNL starts Trailing
STOCHRSI_EXIT_TRIG = 80.0
MIN_PROFIT_TO_SELL = 5.0

# SAFETY SETTINGS (PNL BASED)
# 0.02 = -2.00% PNL Stop Loss
# 0.01 = 1.00% PNL Trailing Step
STOP_LOSS_PCT = 0.02  
TRAILING_PCT = 0.01   

# ---------------------------
# GLOBAL STATE
# ---------------------------
ACTIVE_TRADES = {}
ACTIVE_LOCK = asyncio.Lock()
TRADE_HISTORY = []
SKIPPED_HISTORY = []
RECENT_SIGNALS = {}
SIGNAL_COOLDOWN = 60 

# ---------------------------
# UTILS
# ---------------------------
async def wait_for_network():
    while True:
        try:
            socket.create_connection(("8.8.8.8", 53), timeout=3)
            return
        except OSError:
            sys.stdout.write("\r📡 Network lost. Waiting...")
            sys.stdout.flush()
            await asyncio.sleep(5)

LAST_DASHBOARD_TIME = 0
DASHBOARD_INTERVAL = 1.0 

def refresh_dashboard(force=False):
    global LAST_DASHBOARD_TIME
    if not ACTIVE_TRADES:
        return

    now = time.time()
    if not force and now - LAST_DASHBOARD_TIME < DASHBOARD_INTERVAL:
        return

    parts = []
    # Create list copy to avoid dictionary size change error
    current_trades = list(ACTIVE_TRADES.items())
    
    for s, sim in sorted(current_trades):
        side_short = "L" if sim.side == "LONG" else "S"
        # Color coding for better visibility
        pnl = sim.current_pnl
        pnl_str = f"{pnl:+.2f}%"
        parts.append(f"[{s} {side_short} {pnl_str}]")

    current_str = " | ".join(parts)
    # \033[K clears the line to prevent ghost text
    sys.stdout.write(f"\r\033[K⏳ LIVE: {current_str}")
    sys.stdout.flush()
    LAST_DASHBOARD_TIME = now

def print_report():
    print("\n\n" + "=" * 60)
    print("📊 SESSION REPORT")
    print("=" * 60)
    print(f"\n✅ TRADES EXECUTED: {len(TRADE_HISTORY)}")
    if TRADE_HISTORY:
        print(f"{'SYMBOL':<10} {'SIDE':<6} {'PNL':<8} {'REASON'}")
        print("-" * 60)
        for t in TRADE_HISTORY:
            print(f"{t['symbol']:<10} {t['side']:<6} {t['pnl']:<8} {t['reason']}")
    else:
        print(" (No trades closed)")
    
    print(f"\n🚫 SIGNALS SKIPPED: {len(SKIPPED_HISTORY)}")
    if SKIPPED_HISTORY:
        print(f"{'TIME':<10} {'SYMBOL':<10} {'REASON'}")
        print("-" * 60)
        for s in SKIPPED_HISTORY:
            print(f"{s['time']:<10} {s['symbol']:<10} {s['reason']}")
    else:
        print(" (No signals skipped)")
    print("=" * 60 + "\n")

# ---------------------------
# TELEGRAM MONITOR
# ---------------------------
class TelegramMonitor:
    def __init__(self, q):
        self.client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
        self.queue = q

    async def run(self):
        await self.client.start()
        @self.client.on(events.NewMessage(chats=CHANNEL_TARGET))
        async def handler(event):
            if event.message.media: return
            
            text = event.raw_text.upper()
            coins = re.findall(r"#([A-Z0-9]{2,10})", text)
            
            if coins:
                pair = f"{coins[0]}USDT"
                now = datetime.now().timestamp()
                last_signal = RECENT_SIGNALS.get(pair, 0)
                
                if now - last_signal > SIGNAL_COOLDOWN:
                    RECENT_SIGNALS[pair] = now
                    sys.stdout.write(f"\n\r📥 New signal: {pair}\n")
                    await self.queue.put(pair)
        
        print(f"✅ Monitoring {CHANNEL_TARGET}")
        await self.client.run_until_disconnected()

# ---------------------------
# CALCULATOR
# ---------------------------
class StochCalculator:
    def __init__(self, window=14, smooth1=3, smooth2=3, maxlen=200):
        self.window, self.smooth1, self.smooth2 = window, smooth1, smooth2
        self.closes = deque(maxlen=maxlen)
        self.k_last = None

    def add_close(self, close):
        self.closes.append(float(close))
        if len(self.closes) >= self.window + 5:
            s = pd.Series(self.closes)
            ind = ta.momentum.StochRSIIndicator(close=s, window=self.window, smooth1=self.smooth1, smooth2=self.smooth2)
            k = ind.stochrsi_k().iloc[-1] * 100
            self.k_last = float(k) if pd.notna(k) else None
        return self.k_last

    def latest(self):
        return self.k_last

# ---------------------------
# TRADE SIMULATOR
# ---------------------------
class TradeSimulator:
    def __init__(self, symbol, entry, side):
        self.symbol = symbol
        self.entry_price = entry
        self.side = side
        self.current_pnl = 0.0
        self.highest_pnl = 0.0
        self.is_active = True
        self.be_locked = False
        self.stoch_15m = StochCalculator()
        
        # --- FIXED LOGIC: PNL BASED STOP LOSS ---
        # To get -2% PNL with 10x leverage, price only needs to move 0.2%
        price_distance_pct = STOP_LOSS_PCT / LEVERAGE
        
        if side == "LONG":
            self.stop_loss_price = self.entry_price * (1 - price_distance_pct)
        else:
            self.stop_loss_price = self.entry_price * (1 + price_distance_pct)
        
        sys.stdout.write(f"\n\r🚀 {side} {symbol} @ {entry:.4f} | SL: -{STOP_LOSS_PCT*100:.2f}% PNL\n")
        refresh_dashboard(force=True)

    def update_price(self, price):
        if not self.is_active: return
        
        # PNL Calc
        if self.side == "LONG":
            diff_pct = (price - self.entry_price) / self.entry_price
        else:
            diff_pct = (self.entry_price - price) / self.entry_price
            
        self.current_pnl = diff_pct * 100 * LEVERAGE

        # Break-Even Logic
        if not self.be_locked and self.current_pnl >= BE_TRIGGER:
            # Set SL slightly in profit
            buffer = 0.001 / LEVERAGE # tiny buffer
            if self.side == "LONG":
                self.stop_loss_price = self.entry_price * (1 + buffer)
            else:
                self.stop_loss_price = self.entry_price * (1 - buffer)
            
            self.be_locked = True
            sys.stdout.write(f"\n\r🛡️ {self.symbol} BE Locked\n")
            refresh_dashboard(force=True)

        # Trailing Logic (PNL Based)
        if self.current_pnl >= TRAIL_ACTIVATE and self.current_pnl > self.highest_pnl:
            self.highest_pnl = self.current_pnl
            
            # Convert Trailing PNL % to Price Distance %
            trail_dist_price = TRAILING_PCT / LEVERAGE
            
            if self.side == "LONG":
                new_sl = price * (1 - trail_dist_price)
                self.stop_loss_price = max(self.stop_loss_price, new_sl)
            else:
                new_sl = price * (1 + trail_dist_price)
                self.stop_loss_price = min(self.stop_loss_price, new_sl)

        # Stop Hit Check
        sl_hit = (price <= self.stop_loss_price) if self.side == "LONG" else (price >= self.stop_loss_price)
        if sl_hit:
            self.close(price, "SL / Trailing Hit")
        
        refresh_dashboard()

    def close(self, price, reason):
        if not self.is_active: return
        self.is_active = False
        
        TRADE_HISTORY.append({
            "symbol": self.symbol, "side": self.side, 
            "pnl": f"{self.current_pnl:+.2f}%", "reason": reason
        })
        
        sys.stdout.write(f"\n\r🛑 CLOSED {self.symbol} | {reason} | PNL {self.current_pnl:+.2f}%\n")
        
        async def cleanup():
            async with ACTIVE_LOCK:
                ACTIVE_TRADES.pop(self.symbol, None)
            refresh_dashboard(force=True)
        asyncio.create_task(cleanup())

# ---------------------------
# BINANCE STREAMER
# ---------------------------
async def binance_streamer(bn, sim):
    retry_count = 0
    while sim.is_active:
        try:
            bsm = BinanceSocketManager(bn)
            streams = [f"{sim.symbol.lower()}@markPrice", f"{sim.symbol.lower()}@kline_15m"]
            
            async with bsm.futures_multiplex_socket(streams) as socket:
                while sim.is_active:
                    res = await socket.recv()
                    if not res or "data" not in res: continue
                    
                    if "markPrice" in res["stream"]:
                        sim.update_price(float(res["data"]["p"]))
                    
                    elif "kline_15m" in res["stream"]:
                        k = res["data"]["k"]
                        if k["x"]:
                            sim.stoch_15m.add_close(float(k["c"]))
                            k15 = sim.stoch_15m.latest()
                            if k15:
                                exit_l = sim.side == "LONG" and k15 >= STOCHRSI_EXIT_TRIG
                                exit_s = sim.side == "SHORT" and k15 <= (100 - STOCHRSI_EXIT_TRIG)
                                if (exit_l or exit_s) and sim.current_pnl >= MIN_PROFIT_TO_SELL:
                                    sim.close(float(k["c"]), "15m StochRSI Exit")
            retry_count = 0 
        except Exception:
            retry_count += 1
            if retry_count > 5: await asyncio.sleep(5) 
            await wait_for_network()

# ---------------------------
# WORKER
# ---------------------------
async def worker(bn, q):
    while True:
        pair = await q.get()
        skipped = False
        try:
            def record_skip(reason):
                nonlocal skipped
                SKIPPED_HISTORY.append({"time": datetime.now().strftime("%H:%M:%S"), "symbol": pair, "reason": reason})
                skipped = True
                sys.stdout.write(f"\n\r🚫 Skipped {pair}: {reason}\n")

            async with ACTIVE_LOCK:
                if pair in ACTIVE_TRADES:
                    record_skip("Already active")
                    continue
                if len(ACTIVE_TRADES) >= MAX_CONCURRENT_TRADES:
                    record_skip("Max trades reached")
                    continue
            
            try:
                ticker = await bn.futures_symbol_ticker(symbol=pair)
                px = float(ticker["price"])
                
                async def calc(tf):
                    kl = await bn.futures_klines(symbol=pair, interval=tf, limit=100)
                    c = StochCalculator()
                    for k in kl: c.add_close(k[4])
                    return c.latest()
                
                k1, k5, k15 = await asyncio.gather(calc("1m"), calc("5m"), calc("15m"))
            except Exception as e:
                record_skip(f"API Error: {e}")
                continue

            side = None
            if k1 is not None and k5 is not None and k15 is not None:
                if k1 <= 20 and k5 < 80 and k15 < 80: side = "LONG"
                elif k1 >= 80 and k5 > 20 and k15 > 20: side = "SHORT"
            
            if not side:
                debug_info = f"k1={k1:.1f} k5={k5:.1f} k15={k15:.1f}" if k1 is not None else "Insufficient history"
                record_skip(f"No entry setup | {debug_info}")
                continue

            sim = TradeSimulator(pair, px, side)
            async with ACTIVE_LOCK:
                ACTIVE_TRADES[pair] = sim
            asyncio.create_task(binance_streamer(bn, sim))

        except Exception as e:
            if not skipped: record_skip(f"Worker Error: {str(e)}")
        finally:
            q.task_done()

# ---------------------------
# MAIN
# ---------------------------
async def main():
    q = asyncio.Queue()
    bn = await AsyncClient.create()
    try:
        print("\n--- Bot Started (Fixed PNL Logic) ---")
        await asyncio.gather(TelegramMonitor(q).run(), worker(bn, q))
    finally:
        await bn.close_connection()
        print_report()
        print("🛑 Bot stopped.")

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
