# -*- coding: utf-8 -*-
import os
import asyncio
import time
import logging
import sys
import csv
import aiohttp
from datetime import datetime, timedelta
from pathlib import Path
from collections import deque
from dotenv import load_dotenv

import lighter
from strategies.ema_crossover import EMACrossover

# ================= 1. 日志配置 =================
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
        elif "决策" in record.msg: color = self.yellow
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
CANDLE_INTERVAL = 900    # 15分钟 (900秒)
EMA_FAST = 3
EMA_SLOW = 20
init_band_bps = 30       # 带宽阈值 10bps

TRADE_SIZE_SOL = 1.0     # 每次交易 1 SOL
MAX_POSITION = 1.0       # 最大持仓 1 SOL

# --- 风控配置 ---
LEVERAGE = 10            
TAKE_PROFIT_PCT = 0.05   # 5%
STOP_LOSS_PCT = 0.03     # 3%
DAILY_MAX_LOSS = 0.10    

PRICE_DECIMALS = 3       
SIZE_DECIMALS = 3
CROSS_OFFSET_BPS = 50    # 0.5%
MARKET_ID = 2            
PRICE_URL = "https://mainnet.zklighter.elliot.ai/api/v1/orderBookDetails?market_id=2"
DASHBOARD_FILE = "DASHBOARD.md"
DATA_DIR = "data"        

class TradingBot:
    def __init__(self):
        load_dotenv()
        self.client = None
        # 开启斜率过滤和价格位置过滤
        self.strategy = EMACrossover(
            fast=EMA_FAST, slow=EMA_SLOW, 
            band_mode="bps", band_bps=init_band_bps, 
            check_slope=True, check_price_pos=True,
            print_ema_each_bar=False
        )
        
        self.current_position = 0.0
        self.entry_price = 0.0
        self.initial_equity = None 
        self.current_equity = 0.0  
        
        self.total_trades = 0
        self.operation_count = 0
        self.total_volume = 0.0
        self.win_trades = 0
        self.total_pnl_roe = 0.0
        self.start_time = time.time()
        
        self.bar_start_time = 0
        self.bar_high = -1.0
        self.bar_low = 999999.0
        self.bar_open = 0.0
        self.last_price = 0.0
        
        # 状态诊断
        self.last_decision_reason = "初始化中..." 
        self.last_ema_fast = 0.0
        self.last_ema_slow = 0.0
        
        # 历史记录 (用于 Dashboard)
        self.ema_history = deque(maxlen=5)    # 存最近5次 EMA 数据
        self.recent_trades = deque(maxlen=5)  # 存最近5次交易

        Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

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

    # === 📊 超级看板更新 (已优化：价格置顶) ===
    def update_dashboard(self, current_price, countdown_str):
        # 计算运行时间
        uptime_seconds = int(time.time() - self.start_time)
        uptime_str = str(timedelta(seconds=uptime_seconds))
        
        # 获取当前系统时间
        current_time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        win_rate = (self.win_trades / self.total_trades * 100) if self.total_trades > 0 else 0.0
        
        unrealized_roe = 0.0
        status_icon = "⚪ 空仓观望"
        if abs(self.current_position) > 0.001:
            if self.current_position > 0:
                raw = (current_price - self.entry_price) / self.entry_price
                status_icon = "🟢 持有多单"
            else:
                raw = (self.entry_price - current_price) / self.entry_price
                status_icon = "🔴 持有空单"
            unrealized_roe = raw * LEVERAGE * 100
        
        # EMA Gap 计算
        f_val = self.strategy.fast_ema.value
        s_val = self.strategy.slow_ema.value
        f_str = f"{f_val:.3f}" if f_val else "N/A"
        s_str = f"{s_val:.3f}" if s_val else "N/A"

        ema_gap_str = "N/A"
        ema_trend_icon = "⚪"
        if f_val and s_val:
            gap_val = abs(f_val - s_val) / current_price * 10000
            ema_gap_str = f"{gap_val:.1f}"
            if f_val > s_val: ema_trend_icon = "📈 多头"
            else: ema_trend_icon = "📉 空头"

        # EMA 历史表
        ema_table = "| 时间 | Fast | Slow | Gap (bps) |\n| :--- | :--- | :--- | :--- |\n"
        if self.ema_history:
            for item in reversed(self.ema_history):
                ema_table += f"| {item['time']} | {item['fast']:.3f} | {item['slow']:.3f} | {item['gap']:.1f} |\n"
        else:
            ema_table += "| - | 暂无数据 | - | - |\n"

        # 战绩表
        trades_table = "| 时间 | 方向 | ROE |\n| :--- | :--- | :--- |\n"
        if self.recent_trades:
            for t in reversed(self.recent_trades):
                color = "🔴" if t['roe'] < 0 else "🟢"
                trades_table += f"| {t['time']} | {t['side']} | {color} {t['roe']:+.2f}% |\n"
        else:
            trades_table += "| - | 暂无记录 | - |\n"

        # ⚠️ 核心修改：大标题增加价格显示
        content = f"""
# 💲{current_price:.3f} | 🤖 LighterBot 指挥中心

🕒 **时间**: `{current_time_str}` | ⏱️ **运行**: `{uptime_str}`

---
## 🎮 状态概览
| 项目 | 状态 |
| :--- | :--- |
| **当前持仓** | {status_icon} |
| **K线倒计时** | **`{countdown_str}`** |
| **参数设定** | EMA {EMA_FAST}/{EMA_SLOW} (Band {init_band_bps}) |

---
## 🧠 策略透视
| 指标 | 数值 | 状态/说明 |
| :--- | :--- | :--- |
| **快线 / 慢线** | `{f_str}` / `{s_str}` | {ema_trend_icon} |
| **Band Gap** | `{ema_gap_str}` bps | 阈值: {init_band_bps} |
| **上一决策** | `{self.last_decision_reason}` | |

### 📉 EMA 趋势回放 (Last 5)
{ema_table}

---
## 💰 账户战绩
| 项目 | 数值 |
| :--- | :--- |
| **当前权益** | **`${self.current_equity:.2f}`** (Init: ${self.initial_equity:.2f}) |
| **累计 ROE** | **{self.total_pnl_roe:+.2f}%** |
| **实盘胜率** | `{win_rate:.1f}%` ({self.win_trades}/{self.total_trades}) |
| **浮动盈亏** | **`{unrealized_roe:+.2f}%`** |

---
## 📝 最近平仓
{trades_table}
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
        
        t_str = datetime.now().strftime("%H:%M")
        s_str = "多" if side_direction > 0 else "空"
        self.recent_trades.append({'time': t_str, 'side': s_str, 'roe': realized_roe})
        
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
                if resp.status != 200: return None, None, None
                data = await resp.json()
                if not data.get("accounts"): return 0.0, 0.0, 0.0
                acc = data["accounts"][0]
                equity = float(acc.get("total_asset_value", acc.get("collateral", 0)))
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
        
        if is_ask: worst_price = price_ref * (1.0 - bps / 10000.0)
        else: worst_price = price_ref * (1.0 + bps / 10000.0)
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
        if self.initial_equity is None or self.initial_equity == 0: return
        drawdown_pct = (self.current_equity - self.initial_equity) / self.initial_equity
        if drawdown_pct <= -DAILY_MAX_LOSS:
            log.error(f"🔥 [严重] 触发熔断！亏损 {drawdown_pct*100:.2f}%")
            if abs(self.current_position) > 0.001:
                action = "sell" if self.current_position > 0 else "buy"
                await self.place_order(action, abs(self.current_position), current_price, reduce_only=True)
            sys.exit(1)

        if abs(self.current_position) < 0.001 or self.entry_price <= 0: return
        pnl_pct = (current_price - self.entry_price) / self.entry_price if self.current_position > 0 else (self.entry_price - current_price) / self.entry_price
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
            log.warning(f"🔔 {trigger_msg} 触发!")
            if await self.place_order(target_action, abs(self.current_position), current_price, reduce_only=True):
                log.info("⏳ 等待确认...")
                await asyncio.sleep(3)
                p, e, eq = await self.get_account_full_state(session)
                if abs(p) < 0.001: 
                    old_dir = 1 if target_action == "sell" else -1
                    self.record_trade_result(current_price, old_dir)
                    self.current_position = 0.0
                    self.entry_price = 0.0
                    self.current_equity = eq
                    log.info("✅ 风控平仓成功")
                else:
                    log.error("❌ 风控平仓失败")
                    self.current_position = p 

    async def execute_signal(self, signal: int, price: float, session):
        is_opening = (signal == 1 and self.current_position >= 0) or (signal == -1 and self.current_position <= 0)
        if is_opening and abs(self.current_position) >= MAX_POSITION:
            log.info(f"🚫 仓位已达上限 ({self.current_position}), 忽略信号")
            return

        target_action = "buy" if signal == 1 else "sell"
        reduce_only = False
        
        old_pos = self.current_position
        if abs(old_pos) > 0.001:
            old_dir = 1 if old_pos > 0 else -1
            if (target_action == "sell" and old_dir == 1) or (target_action == "buy" and old_dir == -1):
                self.record_trade_result(price, old_dir)

        if await self.place_order(target_action, TRADE_SIZE_SOL, price, reduce_only=reduce_only):
            await asyncio.sleep(3)
            p, e, eq = await self.get_account_full_state(session)
            if p is not None:
                self.current_position, self.entry_price, self.current_equity = p, e, eq
                log.info(f"✅ 信号执行完毕. Pos: {self.current_position}")

    async def run(self):
        log.info(f"🤖 LighterBot 启动 | 策略: EMA{EMA_FAST}/{EMA_SLOW}")
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
            log.info("同步账户...")
            while True:
                pos, entry, eq = await self.get_account_full_state(session)
                if pos is not None:
                    self.current_position, self.entry_price, self.current_equity = pos, entry, eq
                    self.initial_equity = eq 
                    break
                await asyncio.sleep(3)
            log.info(f"✅ 就绪 | 初始权益: ${self.initial_equity:.2f}")

            self.bar_start_time = time.time()
            self.last_print_time = time.time()
            self.bar_open = -1.0
            
            while True:
                now = time.time()
                price = await self.fetch_price(session)
                if price is None: await asyncio.sleep(2); continue

                self.record_tick_data(price)
                await self.check_risk_and_tpsl(price, session)
                
                time_elapsed = now - self.bar_start_time
                time_left = max(0, CANDLE_INTERVAL - time_elapsed)
                mins, secs = divmod(int(time_left), 60)
                countdown_str = f"{mins:02d}:{secs:02d}"

                self.update_dashboard(price, countdown_str)

                if now - self.last_print_time >= 10:
                    p, e, eq = await self.get_account_full_state(session)
                    if eq: self.current_equity = eq
                    log.info(f"💓 HB: Price={price:.2f} | Pos={self.current_position} | NextK: {countdown_str}")
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
                    
                    signal = self.strategy.on_close_fast_adapt(close_price, self.bar_high, self.bar_low)
                    
                    f_val = self.strategy.fast_ema.value
                    s_val = self.strategy.slow_ema.value
                    self.last_ema_fast = f_val if f_val else 0
                    self.last_ema_slow = s_val if s_val else 0
                    
                    gap_rec = 0
                    if f_val and s_val: gap_rec = abs(f_val - s_val) / close_price * 10000
                    self.ema_history.append({
                        'time': datetime.now().strftime("%H:%M"),
                        'fast': f_val if f_val else 0, 
                        'slow': s_val if s_val else 0,
                        'gap': gap_rec
                    })

                    reason = "✅ 触发交易"
                    if signal == 0:
                        if f_val is None: reason = "⏳ EMA计算中"
                        else:
                            gap = abs(f_val - s_val) / close_price
                            thresh = init_band_bps / 10000.0
                            if gap < thresh:
                                reason = f"❌ 震荡 (Gap {gap*10000:.1f} < {init_band_bps})"
                            else:
                                prev_f = getattr(self.strategy, '_prev_fast_val', None)
                                is_bull = f_val > s_val
                                if is_bull:
                                    if close_price < f_val: reason = "❌ 价格 < 快线"
                                    elif prev_f and (f_val - prev_f) <= 0: reason = "❌ 快线向下"
                                    else: reason = "⚖️ 趋势未确认"
                                else:
                                    if close_price > f_val: reason = "❌ 价格 > 快线"
                                    elif prev_f and (f_val - prev_f) >= 0: reason = "❌ 快线向上"
                                    else: reason = "⚖️ 趋势未确认"
                    
                    self.last_decision_reason = reason
                    log.info(f"🧐 决策: {reason}")
                    
                    pos_dir = 1 if self.current_position > 0.001 else (-1 if self.current_position < -0.001 else 0)
                    self.strategy.set_position_side(pos_dir)
                    
                    if signal != 0:
                        await self.execute_signal(signal, close_price, session)
                    elif signal == 0 and abs(self.current_position) > 0.001:
                        log.info(f"📉 趋势消失，平仓...")
                        old_dir = 1 if self.current_position > 0 else -1
                        self.record_trade_result(close_price, old_dir)
                        action = "sell" if self.current_position > 0 else "buy"
                        await self.place_order(action, abs(self.current_position), close_price, reduce_only=True)
                        self.current_position = 0.0
                    
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