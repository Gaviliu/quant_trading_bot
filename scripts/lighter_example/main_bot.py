# -*- coding: utf-8 -*-
import os
import asyncio
import time
import logging
import sys
import aiohttp
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
    magenta = "\x1b[35;20m"   # 紫色 (TP/SL)
    reset = "\x1b[0m"
    fmt_str = "%(asctime)s [%(levelname)s] %(message)s"

    def format(self, record):
        log_fmt = self.fmt_str
        if "BUY" in record.msg or "开多" in record.msg or "平空" in record.msg:
            color = self.green
        elif "SELL" in record.msg or "开空" in record.msg or "平多" in record.msg:
            color = self.red
        elif "成交确认" in record.msg:
            color = self.green
        elif "止盈" in record.msg or "止损" in record.msg:
            color = self.magenta
        elif "失败" in record.msg or "拒单" in record.msg or "无法确认" in record.msg:
            color = self.red
        elif record.levelno == logging.WARNING:
            color = self.yellow
        elif record.levelno == logging.ERROR:
            color = self.red
        elif "Heartbeat" in record.msg:
            color = self.blue
        else:
            color = self.grey
        formatter = logging.Formatter(f"{color}{log_fmt}{self.reset}")
        return formatter.format(record)

# 强制接管日志
log = logging.getLogger("LighterBot")
log.setLevel(logging.INFO)
log.propagate = False
if log.hasHandlers(): log.handlers.clear()
ch = logging.StreamHandler(sys.stdout)
ch.setFormatter(ColoredFormatter())
log.addHandler(ch)

# ================= 配置区域 =================
CANDLE_INTERVAL = 300     # K线周期 (30秒)
EMA_FAST = 9
EMA_SLOW = 26
init_band_bps = 50
TRADE_SIZE_SOL = 0.1      
MAX_POSITION = 0.1        

# --- 止盈止损与杠杆配置 ---
LEVERAGE = 10             # 杠杆倍数
TAKE_PROFIT_PCT = 0.02    # 止盈 2% (ROE)
STOP_LOSS_PCT = 0.02      # 止损 1% (ROE)

PRICE_DECIMALS = 3
SIZE_DECIMALS = 3
# 滑点设为 0.5% (50bps) 即可
# 因为我们下面加了卖单精度补丁，不需要靠超大滑点来强撑了
CROSS_OFFSET_BPS = 50     
MARKET_ID = 2             # SOL
PRICE_URL = "https://mainnet.zklighter.elliot.ai/api/v1/orderBookDetails?market_id=2"

# 看板文件路径
DASHBOARD_FILE = "DASHBOARD.md"

class TradingBot:
    def __init__(self):
        load_dotenv()
        self.client = None
        self.strategy = EMACrossover(fast=EMA_FAST, slow=EMA_SLOW, band_mode="bps", band_bps=init_band_bps, print_ema_each_bar=True)
        
        # 实时状态
        self.current_position = 0.0
        self.entry_price = 0.0
        
        # === 📊 统计数据 ===
        self.total_trades = 0       # 平仓次数
        self.operation_count = 0    # 操作次数 (仅统计成功的下单)
        self.total_volume = 0.0     # 总交易额
        self.win_trades = 0         # 盈利次数
        self.total_pnl_roe = 0.0    # 累计 ROE
        self.start_time = time.time()
        
        # K线变量
        self.bar_start_time = 0
        self.bar_high = -1.0
        self.bar_low = 999999.0
        self.bar_open = 0.0
        self.last_price = 0.0

    # --- 看板生成函数 ---
    def update_dashboard(self, current_price):
        runtime_min = (time.time() - self.start_time) / 60
        win_rate = (self.win_trades / self.total_trades * 100) if self.total_trades > 0 else 0.0
        
        unrealized_roe = 0.0
        status_icon = "⚪ 空仓观望"
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

## 💰 战绩统计
| 指标 | 数值 |
| :--- | :--- |
| **累计 ROE** | **{self.total_pnl_roe:+.2f}%** |
| **胜率** | **{win_rate:.1f}%** ({self.win_trades}/{self.total_trades}) |
| **成功操作** | {self.operation_count} 次 |
| **总交易额** | ${self.total_volume:,.2f} |

---

## 📈 实时监控
| 项目 | 当前数值 |
| :--- | :--- |
| **SOL 现价** | `{current_price:.4f}` |
| **持仓均价** | `{self.entry_price:.4f}` |
| **持仓数量** | `{self.current_position} SOL` |
| **浮动盈亏** | **`{unrealized_roe:+.2f}%`** |

---
> *提示: 点击 VS Code 右上角图标打开侧边预览，数据会自动刷新。*
"""
        try:
            with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
                f.write(content.strip())
        except Exception: pass

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
        except Exception: return None

    def to_int(self, val: float, dec: int) -> int:
        return int(round(val * (10 ** dec)))
    
    async def get_sol_position_and_entry(self, session):
        """
        查询链上持仓
        ⚠️ 修复：如果查询失败返回 None，不再盲目返回 0
        """
        l1_address = os.getenv("PUBLIC_WALLET_ADDRESS")
        if not l1_address or not self.client: return None, None
        url = f"{self.client.url}/api/v1/account"
        params = {"by": "l1_address", "value": l1_address}
        
        try:
            async with session.get(url, params=params, timeout=10) as resp:
                if resp.status != 200: 
                    log.warning(f"查询账户 API 非200: {resp.status}")
                    return None, None
                
                data = await resp.json()
                if not data.get("accounts"): 
                    return 0.0, 0.0 
                
                for pos in data["accounts"][0].get("positions", []):
                    if pos.get("market_id") == MARKET_ID:
                        raw_size = float(pos.get("position", 0))
                        sign = int(pos.get("sign", 0))
                        entry_price = float(pos.get("avg_entry_price", 0))
                        return raw_size * sign, entry_price
                
                return 0.0, 0.0
        except Exception as e:
            log.warning(f"⚠️ 查仓发生异常: {e}")
            return None, None

    async def place_order(self, side: str, size: float, price_ref: float):
        """
        下单逻辑 - 使用 create_market_order 
        ⚠️ 包含针对卖单精度的热修复补丁
        """
        if not self.client: return False
        is_ask = (side.lower() == "sell")
        base_amount = self.to_int(size, SIZE_DECIMALS)
        
        # 计算滑点保护价
        bps = CROSS_OFFSET_BPS
        
        if is_ask:
            # 卖单：底线 = 现价 * (1 - 0.5%)
            worst_price = price_ref * (1.0 - bps / 10000.0)
            worst_price_int = self.to_int(worst_price, PRICE_DECIMALS)
            
            # 🛠️ 核心补丁：SDK 卖单精度有 Bug，手动 * 0.1 对齐
            worst_price_int = int(worst_price_int)
            log.info(f"🚀 发送市价卖单: {size} SOL (修正底线价: {worst_price_int})")
            
        else:
            # 买单：上限 = 现价 * (1 + 0.5%)
            worst_price = price_ref * (1.0 + bps / 10000.0)
            worst_price_int = self.to_int(worst_price, PRICE_DECIMALS)
            log.info(f"🚀 发送市价买单: {size} SOL (保护价: {worst_price:.2f})")
        
        try:
            ret = await self.client.create_market_order(
                market_index=MARKET_ID,
                client_order_index=int(time.time() * 1000) % 10_000_000,
                base_amount=base_amount,
                avg_execution_price=worst_price_int,
                is_ask=is_ask
            )
            # ⚠️ 修复：只有真正拿到 Hash 才算成功，才记录到看板
            if ret and len(ret) > 1 and hasattr(ret[1], 'tx_hash'):
                self.operation_count += 1
                self.total_volume += size * price_ref
                log.info(f"✅ 交易所受理成功: Hash={ret[1].tx_hash}")
                return True
            
            log.warning("⚠️ 订单已发送但无Hash返回，不计入看板统计。")
            return True 
        except Exception as e:
            log.error(f"❌ 下单异常: {e}")
            return False

    async def check_and_execute_tp_sl(self, current_price: float, session):
        # 安全检查
        if abs(self.current_position) < 0.001 or self.entry_price <= 0: return

        pnl_pct = 0.0
        if self.current_position > 0:
            pnl_pct = (current_price - self.entry_price) / self.entry_price
        elif self.current_position < 0:
            pnl_pct = (self.entry_price - current_price) / self.entry_price
        
        roe_pct = pnl_pct * LEVERAGE
        target_action = ""
        trigger_type = ""

        if roe_pct >= TAKE_PROFIT_PCT:
            target_action = "sell" if self.current_position > 0 else "buy"
            trigger_type = f"🚀 止盈 (+{roe_pct*100:.2f}%)"
        elif roe_pct <= -STOP_LOSS_PCT:
            target_action = "sell" if self.current_position > 0 else "buy"
            trigger_type = f"🛡️ 止损 ({roe_pct*100:.2f}%)"

        if target_action:
            log.warning(f"🔔 {trigger_type} 触发! 正在市价平仓...")
            old_direction = 1 if self.current_position > 0 else -1
            trade_size = abs(self.current_position)
            
            if await self.place_order(target_action, trade_size, current_price):
                log.info("⏳ 等待链上确认 (3s)...")
                await asyncio.sleep(3)
                
                new_pos, new_entry = await self.get_sol_position_and_entry(session)
                
                if new_pos is None:
                    log.error("⚠️ 网络波动，无法确认结果，将在下次心跳重试。")
                    return

                if abs(new_pos) < 0.001: 
                    self.record_trade_result(current_price, old_direction)
                    self.current_position = 0.0
                    self.entry_price = 0.0
                    log.info(f"✅ 风控平仓成功！")
                else:
                    log.error(f"❌ 止盈/止损失败！仓位未变！")
                    self.current_position = new_pos 
            else:
                log.error("❌ 风控下单请求发送失败！")

    async def execute_signal(self, signal: int, price: float, session):
        target_action = ""
        trade_size = 0.0
        
        if signal == 1: 
            if self.current_position >= MAX_POSITION: return
            target_action = "buy"
            trade_size = abs(self.current_position) if self.current_position < 0 else TRADE_SIZE_SOL
        elif signal == -1: 
            if self.current_position <= -MAX_POSITION: return
            target_action = "sell"
            trade_size = abs(self.current_position) if self.current_position > 0 else TRADE_SIZE_SOL
        
        if not target_action: return

        old_position = self.current_position
        if abs(old_position) > 0.001:
            old_dir = 1 if old_position > 0 else -1
            if (target_action == "sell" and old_dir == 1) or (target_action == "buy" and old_dir == -1):
                self.record_trade_result(price, old_dir)

        if await self.place_order(target_action, trade_size, price):
            log.info("⏳ 等待确认...")
            await asyncio.sleep(3) 
            
            new_pos, new_entry = await self.get_sol_position_and_entry(session)
            if new_pos is None: return

            self.current_position = new_pos
            self.entry_price = new_entry
            
            if abs(self.current_position - old_position) > 0.001:
                log.info(f"✅ 成交确认! 最新持仓: {self.current_position}")
            else:
                log.error(f"❌ 疑似成交失败 (仓位未变)")

    async def run(self):
        log.info(f"🤖 LighterBot 启动 | 监控面板: {DASHBOARD_FILE} | 卖单补丁: 开启")
        
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
            # 初始查仓
            log.info("正在同步链上数据...")
            while True:
                pos, entry = await self.get_sol_position_and_entry(session)
                if pos is not None:
                    self.current_position, self.entry_price = pos, entry
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

                # 1. 更新看板
                self.update_dashboard(price)
                # 2. 检查风控
                await self.check_and_execute_tp_sl(price, session)

                # 心跳
                if now - self.last_print_time >= 10:
                    roe_str = "0.00%"
                    if abs(self.current_position) > 0 and self.entry_price > 0:
                        if self.current_position > 0: raw = (price - self.entry_price) / self.entry_price
                        else: raw = (self.entry_price - price) / self.entry_price
                        roe_str = f"{raw*LEVERAGE*100:+.2f}%"
                    
                    log.info(f"💓 Heartbeat: Price={price:.2f} | Pos={self.current_position} | ROE={roe_str}")
                    self.last_print_time = now

                # K线逻辑
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
                
                await asyncio.sleep(2)

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