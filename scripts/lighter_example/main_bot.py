# -*- coding: utf-8 -*-
import os
import asyncio
import time
import logging
import sys
import csv
import aiohttp
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

# 引入你的模块
import lighter
from strategies.ema_crossover import EMACrossover

# ================= 1. 专业彩色日志配置 =================
class ColoredFormatter(logging.Formatter):
    grey = "\x1b[38;20m"
    yellow = "\x1b[33;20m"
    red = "\x1b[31;20m"
    green = "\x1b[32;20m"
    blue = "\x1b[34;20m"
    magenta = "\x1b[35;20m"
    cyan = "\x1b[36;20m"
    reset = "\x1b[0m"
    fmt_str = "%(asctime)s [%(levelname)s] %(message)s"

    def format(self, record):
        log_fmt = self.fmt_str
        if "BUY" in record.msg or "开多" in record.msg: color = self.green
        elif "SELL" in record.msg or "开空" in record.msg: color = self.red
        elif "成交确认" in record.msg: color = self.green
        elif "止盈" in record.msg: color = self.magenta
        elif "止损" in record.msg: color = self.magenta
        elif "熔断" in record.msg: color = self.red 
        elif "失败" in record.msg or "拒单" in record.msg: color = self.red
        elif record.levelno == logging.WARNING: color = self.yellow
        elif record.levelno == logging.ERROR: color = self.red
        elif "Heartbeat" in record.msg: color = self.blue
        elif "Data" in record.msg: color = self.cyan
        else: color = self.grey
        formatter = logging.Formatter(f"{color}{log_fmt}{self.reset}")
        return formatter.format(record)

log = logging.getLogger("LighterBot")
log.setLevel(logging.INFO)
log.propagate = False
if log.hasHandlers(): log.handlers.clear()
ch = logging.StreamHandler(sys.stdout)
ch.setFormatter(ColoredFormatter())
log.addHandler(ch)

# ================= 配置区域 =================
CANDLE_INTERVAL = 30     # K线周期 30s
EMA_FAST = 3
EMA_SLOW = 26
init_band_bps = 10
TRADE_SIZE_SOL = 0.1      
MAX_POSITION = 0.1        # 单方向最大持仓限制

# --- 风控配置 ---
LEVERAGE = 10             # 杠杆倍数
TAKE_PROFIT_PCT = 0.02    # 止盈 2% (ROE)
STOP_LOSS_PCT = 0.02      # 止损 2% (ROE)
DAILY_MAX_LOSS = 0.10     # 🔥 熔断线

PRICE_DECIMALS = 3      # 价格精度 (官方确认)
SIZE_DECIMALS = 3
CROSS_OFFSET_BPS = 500     # 滑点 0.5%
MARKET_ID = 2             # SOL
PRICE_URL = "https://mainnet.zklighter.elliot.ai/api/v1/orderBookDetails?market_id=2"
DASHBOARD_FILE = "DASHBOARD.md"
DATA_DIR = "data"         

class TradingBot:
    def __init__(self):
        load_dotenv()
        self.client = None
        self.strategy = EMACrossover(fast=EMA_FAST, slow=EMA_SLOW, band_mode="bps", band_bps=init_band_bps, print_ema_each_bar=True)
        
        # 核心状态
        self.current_position = 0.0
        self.entry_price = 0.0
        self.initial_equity = None 
        self.current_equity = 0.0  
        
        # 统计
        self.total_trades = 0
        self.operation_count = 0
        self.total_volume = 0.0
        self.win_trades = 0
        self.total_pnl_roe = 0.0
        self.start_time = time.time()
        
        # K线
        self.bar_start_time = 0
        self.bar_high = -1.0
        self.bar_low = 999999.0
        self.bar_open = 0.0
        self.last_price = 0.0

        Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

    # === 💾 数据录制模块 ===
    def record_tick_data(self, price):
        today = datetime.now().strftime("%Y%m%d")
        file_path = f"{DATA_DIR}/sol_ticks_{today}.csv"
        file_exists = os.path.isfile(file_path)
        try:
            with open(file_path, "a", newline='') as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(["timestamp", "price", "position", "equity"])
                writer.writerow([time.time(), price, self.current_position, self.current_equity])
        except Exception as e:
            log.warning(f"Data Write Error: {e}")

    # === 📊 看板更新 ===
    def update_dashboard(self, current_price):
        runtime_min = (time.time() - self.start_time) / 60
        win_rate = (self.win_trades / self.total_trades * 100) if self.total_trades > 0 else 0.0
        
        drawdown_pct = 0.0
        if self.initial_equity and self.initial_equity > 0:
            drawdown_pct = (self.current_equity - self.initial_equity) / self.initial_equity * 100

        unrealized_roe = 0.0
        status_icon = "⚪ 空仓"
        if abs(self.current_position) > 0.001 and self.entry_price > 0:
            if self.current_position > 0:
                raw = (current_price - self.entry_price) / self.entry_price
                status_icon = "🟢 持有多单"
            else:
                raw = (self.entry_price - current_price) / self.entry_price
                status_icon = "🔴 持有空单"
            unrealized_roe = raw * LEVERAGE * 100

        content = f"""
# 🤖 Lighter Quant Dashboard

**状态**: {status_icon} | **更新**: {time.strftime('%H:%M:%S')}
**时长**: {runtime_min:.1f} min | **杠杆**: {LEVERAGE}x

---
## 🚨 风控监控
| 指标 | 当前值 | 阈值 | 状态 |
| :--- | :--- | :--- | :--- |
| **仓位硬限** | `{abs(self.current_position)}` | `{MAX_POSITION}` | {'✅' if abs(self.current_position) <= MAX_POSITION else '❌'} |
| **当日盈亏** | **`{drawdown_pct:+.2f}%`** | `-{DAILY_MAX_LOSS*100}%` | {'✅' if drawdown_pct > -DAILY_MAX_LOSS*100 else '🔥 熔断'} |
| **当前权益** | `${self.current_equity:.2f}` | Init: `${self.initial_equity:.2f}` | - |

---
## 💰 策略统计
* **累计 ROE**: `{self.total_pnl_roe:+.2f}%`
* **胜率**: `{win_rate:.1f}%` ({self.win_trades}/{self.total_trades})
* **成交额**: `${self.total_volume:,.2f}`

---
## 📈 实时行情
* **Price**: `{current_price:.4f}`
* **Entry**: `{self.entry_price:.4f}`
* **PnL**: `{unrealized_roe:+.2f}%`
"""
        try:
            with open(DASHBOARD_FILE, "w", encoding="utf-8") as f: f.write(content.strip())
        except: pass

    def record_trade_result(self, close_price, side_direction):
        if self.entry_price <= 0: return
        raw_pnl = 0.0
        if side_direction > 0: raw_pnl = (close_price - self.entry_price) / self.entry_price
        else: raw_pnl = (self.entry_price - close_price) / self.entry_price
        realized_roe = raw_pnl * LEVERAGE * 100
        self.total_trades += 1
        self.total_pnl_roe += realized_roe
        if realized_roe > 0: self.win_trades += 1
        log.info(f"📝 平仓战绩: 本单 {realized_roe:+.2f}% | 总计 {self.total_pnl_roe:+.2f}%")

    async def fetch_price(self, session):
        try:
            async with session.get(PRICE_URL, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data["order_book_details"][0]["last_trade_price"])
                return None
        except: return None

    def to_int(self, val: float, dec: int) -> int: return int(round(val * (10 ** dec)))
    
    async def get_account_full_state(self, session):
        l1_address = os.getenv("PUBLIC_WALLET_ADDRESS")
        if not l1_address or not self.client: return None, None, None
        url = f"{self.client.url}/api/v1/account"
        params = {"by": "l1_address", "value": l1_address}
        try:
            async with session.get(url, params=params, timeout=10) as resp:
                if resp.status != 200: 
                    return None, None, None
                
                data = await resp.json()
                if not data.get("accounts"): return 0.0, 0.0, 0.0
                
                acc = data["accounts"][0]
                equity = float(acc.get("collateral", 0)) 
                if "total_asset_value" in acc:
                    equity = float(acc.get("total_asset_value", 0))

                pos_val, entry = 0.0, 0.0
                for pos in acc.get("positions", []):
                    if pos.get("market_id") == MARKET_ID:
                        pos_val = float(pos.get("position", 0)) * int(pos.get("sign", 0))
                        entry = float(pos.get("avg_entry_price", 0))
                        break
                return pos_val, entry, equity
        except Exception as e:
            log.warning(f"查账失败: {e}")
            return None, None, None

    async def place_order(self, side: str, size: float, price_ref: float, reduce_only: bool = False):
        if not self.client: return False
        is_ask = (side.lower() == "sell")
        base_amount = self.to_int(size, SIZE_DECIMALS)
        bps = CROSS_OFFSET_BPS
        
        if is_ask:
            worst_price = price_ref * (1.0 - bps / 10000.0)
        else:
            worst_price = price_ref * (1.0 + bps / 10000.0)
            
        worst_price_int = self.to_int(worst_price, PRICE_DECIMALS)
        log.info(f"🚀 发送{'卖' if is_ask else '买'}单: {size} SOL (保护价:{worst_price:.3f} | R:{reduce_only})")
        
        try:
            ret = await self.client.create_market_order(
                market_index=MARKET_ID,
                client_order_index=int(time.time() * 1000) % 10_000_000,
                base_amount=base_amount,
                avg_execution_price=worst_price_int,
                is_ask=is_ask,
                reduce_only=reduce_only
            )
            if ret and len(ret) > 1 and hasattr(ret[1], 'tx_hash'):
                self.operation_count += 1
                self.total_volume += size * price_ref
                log.info(f"✅ 受理成功: Hash={ret[1].tx_hash}")
                return True
            self.operation_count += 1
            return True 
        except Exception as e:
            log.error(f"❌ 下单异常: {e}")
            return False

    async def check_risk_and_tpsl(self, current_price, session):
        if abs(self.current_position) < 0.001 or self.entry_price <= 0: return

        pnl_pct = 0.0
        if self.current_position > 0:
            pnl_pct = (current_price - self.entry_price) / self.entry_price
        elif self.current_position < 0:
            pnl_pct = (self.entry_price - current_price) / self.entry_price
        
        roe_pct = pnl_pct * LEVERAGE
        
        target_action = ""
        trigger_msg = ""
        if roe_pct >= TAKE_PROFIT_PCT:
            target_action = "sell" if self.current_position > 0 else "buy"
            trigger_msg = f"🚀 止盈 (+{roe_pct*100:.2f}%)"
        elif roe_pct <= -STOP_LOSS_PCT:
            target_action = "sell" if self.current_position > 0 else "buy"
            trigger_msg = f"🛡️ 止损 ({roe_pct*100:.2f}%)"

        if target_action:
            log.warning(f"🔔 {trigger_msg} 触发! 正在市价平仓...")
            old_direction = 1 if self.current_position > 0 else -1
            trade_size = abs(self.current_position)
            
            if await self.place_order(target_action, trade_size, current_price, reduce_only=True):
                log.info("⏳ 等待链上确认 (3s)...")
                await asyncio.sleep(3)
                
                new_pos, new_entry, new_eq = await self.get_account_full_state(session)
                
                if new_pos is None:
                    log.error("⚠️ 网络波动，无法确认结果，将在下次心跳重试。")
                    return

                if abs(new_pos) < 0.001: 
                    # ✅ 核心修复：这里补上了记录战绩的逻辑
                    self.record_trade_result(current_price, old_direction)
                    # ---------------------------------------------
                    self.current_position = 0.0
                    self.entry_price = 0.0
                    self.current_equity = new_eq
                    log.info(f"✅ 风控平仓成功！")
                else:
                    log.error(f"❌ 止盈/止损失败！仓位未变！")
                    self.current_position = new_pos 
            else:
                log.error("❌ 风控下单请求发送失败！")

    async def execute_signal(self, signal: int, price: float, session):
        is_opening = (signal == 1 and self.current_position >= 0) or (signal == -1 and self.current_position <= 0)
        if is_opening and abs(self.current_position) >= MAX_POSITION:
            log.info(f"🚫 仓位已达上限 ({self.current_position}), 忽略信号")
            return

        target_action = "buy" if signal == 1 else "sell"
        reduce_only = False
        
        # 策略平仓/反手逻辑
        old_position = self.current_position
        if abs(old_position) > 0.001:
            old_dir = 1 if old_position > 0 else -1
            # 如果是反向操作(平仓)，记录上一单战绩
            if (target_action == "sell" and old_dir == 1) or (target_action == "buy" and old_dir == -1):
                self.record_trade_result(price, old_dir)

        if await self.place_order(target_action, TRADE_SIZE_SOL, price, reduce_only=reduce_only):
            await asyncio.sleep(3)
            p, e, eq = await self.get_account_full_state(session)
            if p is not None:
                self.current_position, self.entry_price, self.current_equity = p, e, eq
                log.info(f"✅ 信号执行完毕. Pos: {self.current_position}")

    async def run(self):
        log.info(f"🤖 LighterBot 启动 | 监控: {DASHBOARD_FILE} | 滑点: 0.5%")
        while True:
            try:
                self.client = lighter.SignerClient(
                    url=os.getenv("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai"),
                    private_key=os.getenv("API_KEY_PRIVATE_KEY"),
                    account_index=int(os.getenv("ACCOUNT_INDEX")),
                    api_key_index=int(os.getenv("API_KEY_INDEX")),
                )
                if self.client: break
            except Exception: await asyncio.sleep(3)

        async with aiohttp.ClientSession() as session:
            log.info("正在同步链上数据...")
            while True:
                pos, entry, eq = await self.get_account_full_state(session)
                if pos is not None and eq is not None:
                    self.current_position, self.entry_price, self.current_equity = pos, entry, eq
                    self.initial_equity = eq 
                    break
                await asyncio.sleep(3)
            log.info(f"✅ 状态同步完成: {self.current_position} SOL | 均价: {self.entry_price}")

            self.bar_start_time = time.time()
            self.last_print_time = time.time()
            self.bar_open = -1.0
            
            while True:
                now = time.time()
                price = await self.fetch_price(session)
                if price is None: await asyncio.sleep(2); continue

                self.record_tick_data(price)
                self.update_dashboard(price)
                await self.check_risk_and_tpsl(price, session)

                if now - self.last_print_time >= 10:
                    p, e, eq = await self.get_account_full_state(session)
                    if eq: self.current_equity = eq
                    log.info(f"💓 HB: Price={price:.2f} | Eq=${self.current_equity:.2f} | Pos={self.current_position}")
                    self.last_print_time = now

                if self.bar_open == -1.0: 
                    self.bar_open = self.bar_high = self.bar_low = price
                    self.bar_start_time = now 
                else:
                    self.bar_high = max(self.bar_high, price)
                    self.bar_low = min(self.bar_low, price)

                if now - self.bar_start_time >= CANDLE_INTERVAL:
                    close_price = price
                    log.info(f"📊 K线闭合: C={close_price:.2f}")
                    
                    pos_dir = 1 if self.current_position > 0.001 else (-1 if self.current_position < -0.001 else 0)
                    self.strategy.set_position_side(pos_dir)
                    signal = self.strategy.on_close_fast_adapt(close_price, self.bar_high, self.bar_low)
                    
                    if signal != 0:
                        await self.execute_signal(signal, close_price, session)
                    
                    self.bar_open = -1.0
                    self.bar_high = -1.0
                    self.bar_low = 999999.0
                
                await asyncio.sleep(0.5)

    async def close(self):
        if self.client: await self.client.close()

if __name__ == "__main__":
    bot = TradingBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        pass
    finally:
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(bot.close())
        except: pass