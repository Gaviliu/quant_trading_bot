# -*- coding: utf-8 -*-
import os
import asyncio
import time
import logging
import sys
import csv
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import aiohttp
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import deque
from dotenv import load_dotenv

import lighter
from strategies.ema_crossover import EMACrossover

# [新增] 引入 Telegram 库 (带异常处理，防止未安装报错)
try:
    from telegram import Bot
    from telegram.error import TelegramError
    TELEGRAM_ENABLED = True
except ImportError:
    TELEGRAM_ENABLED = False
    print("⚠️ 未安装 python-telegram-bot，Telegram 推送功能已禁用。建议运行: pip install python-telegram-bot")


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
        if "BUY" in record.msg or "开多" in record.msg:
            color = self.green
        elif "SELL" in record.msg or "开空" in record.msg:
            color = self.red
        elif "成交确认" in record.msg:
            color = self.green
        elif "止盈" in record.msg:
            color = self.magenta
        elif "止损" in record.msg:
            color = self.magenta
        elif "熔断" in record.msg:
            color = self.red
        elif "失败" in record.msg or "拒单" in record.msg:
            color = self.red
        elif record.levelno == logging.WARNING:
            color = self.yellow
        elif record.levelno == logging.ERROR:
            color = self.red
        elif "Heartbeat" in record.msg:
            color = self.blue
        elif "Data" in record.msg:
            color = self.cyan
        elif "决策" in record.msg:
            color = self.yellow
        else:
            color = self.grey
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
CANDLE_INTERVAL = 60  # 1分钟 (60秒)
EMA_FAST = 1
EMA_SLOW = 60
init_band_bps = 10  # 带宽阈值 10bps

TRADE_SIZE_SOL = 1.0  # 每次交易 1 SOL
MAX_POSITION = 1.0  # 最大持仓 1 SOL

# --- 风控配置 ---
LEVERAGE = 10
TAKE_PROFIT_PCT = 0.05  # 5%
STOP_LOSS_PCT = 0.01  # 2%
DAILY_MAX_LOSS = 0.10

# [新增] 价格判定模式
# 'REALTIME': 实时模式 (默认)。每 2s 获取价格时立即检查风控 (最安全)。
# 'ON_CLOSE': 收盘模式。只在 K 线闭合时检查风控 (忽略盘中波动)。
PRICE_CHECK_MODE = 'REALTIME'

PRICE_DECIMALS = 3
SIZE_DECIMALS = 3
CROSS_OFFSET_BPS = 50  # 0.5%
MARKET_ID = 2
PRICE_URL = "https://mainnet.zklighter.elliot.ai/api/v1/orderBookDetails?market_id=2"
DASHBOARD_FILE = "DASHBOARD.md"
DATA_DIR = "data"
TRADE_HISTORY_FILE = "trade_history_Start_12_21.csv"  # 详细交易对账文件
DAILY_STATS_FILE = "daily_stats_Start_12_21.csv"      # 每日统计文件
EQUITY_IMAGE_FILE = "equity_curve_Start_12_21.png"    # 资金曲线图

# [新增] Telegram 推送间隔配置
# 每隔多少秒向手机发送一次 Dashboard 状态概览 (默认 3600秒 = 1小时)
TG_REPORT_INTERVAL = 300


# [新增] Telegram 通知类
class TelegramNotifier:
    def __init__(self):
        self.token = os.getenv("TELEGRAM_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.bot = None
        if TELEGRAM_ENABLED and self.token and self.chat_id:
            try:
                self.bot = Bot(token=self.token)
                log.info("✅ Telegram 模块初始化成功")
            except Exception as e:
                log.error(f"❌ Telegram 初始化失败: {e}")

    async def send_message(self, text):
        if not self.bot: return
        try:
            # parse_mode='HTML' 允许使用简单的加粗等格式
            await self.bot.send_message(chat_id=self.chat_id, text=text, parse_mode='HTML')
        except TelegramError as e:
            log.warning(f"⚠️ Telegram 发送失败: {e}")
        except Exception as e:
            log.warning(f"⚠️ Telegram 未知错误: {e}")

    async def send_image(self, image_path, caption=""):
        if not self.bot or not os.path.exists(image_path): return
        try:
            with open(image_path, 'rb') as photo:
                await self.bot.send_photo(chat_id=self.chat_id, photo=photo, caption=caption)
        except Exception as e:
            log.warning(f"⚠️ Telegram 发图失败: {e}")


class TradingBot:
    def __init__(self):
        load_dotenv()
        self.client = None
        
        # [修改] 策略初始化：关闭斜率过滤和价格位置过滤，完全对齐 debug 脚本
        self.strategy = EMACrossover(
            fast=EMA_FAST, slow=EMA_SLOW,
            band_mode="bps", band_bps=init_band_bps,
            check_slope=False,      # 🔴 已修改：对齐 debug 脚本
            check_price_pos=False,  # 🔴 已修改：对齐 debug 脚本
            mute_same_dir_when_holding=False, # [新增] 允许持仓时继续发出同向信号(尽管execute_signal里有过滤，但策略层需放行)
            print_ema_each_bar=False
        )

        # [新增] 初始化 Telegram 通知器
        self.tg = TelegramNotifier()
        self.last_tg_report_time = 0

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
        self.last_gap = 0.0
        self.last_signal = 0

        # 历史记录 (用于 Dashboard)
        self.ema_history = deque(maxlen=5)  # 存最近5次 EMA 数据
        self.recent_trades = deque(maxlen=5)  # 存最近5次交易

        Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
        self.init_trade_history_file()
        
        # 设置绘图风格 (用于生成资金曲线)
        try:
            plt.style.use('seaborn-v0_8-darkgrid')
        except:
            plt.style.use('bmh')

    # [修改] 初始化交易对账文件 - 列名对齐 debug 脚本
    def init_trade_history_file(self):
        file_path = os.path.join(DATA_DIR, TRADE_HISTORY_FILE)
        # 如果文件不存在，创建并写入表头
        if not os.path.isfile(file_path):
            try:
                # utf-8-sig 用于 Excel 正确显示中文
                with open(file_path, "w", newline='', encoding='utf-8-sig') as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        "timestamp", "datetime_utc", "datetime_bj", "action", "price", "size", 
                        "position_before", "position_after", "equity", "pnl", "reason",
                        "ema_fast", "ema_slow", "gap_bps", "signal"
                    ])
            except Exception as e:
                log.error(f"❌ 初始化交易日志失败: {e}")

    # [修改] 记录交易对账日志 - 增加参数并计算北京时间
    def log_trade_action(self, action, price, size, pos_before, pos_after, equity, pnl=0.0, reason="", 
                         f_ema=0, s_ema=0, gap=0, sig=0):
        file_path = os.path.join(DATA_DIR, TRADE_HISTORY_FILE)
        try:
            ts = time.time()
            # 计算 UTC 和 北京时间
            tz_bj = timezone(timedelta(hours=8))
            dt_obj = datetime.fromtimestamp(ts, tz=timezone.utc)
            dt_bj_str = dt_obj.astimezone(tz_bj).strftime('%Y-%m-%d %H:%M:%S')
            dt_utc_str = dt_obj.strftime('%Y-%m-%d %H:%M:%S')

            with open(file_path, "a", newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                writer.writerow([
                    ts, dt_utc_str, dt_bj_str, action, price, size,
                    pos_before, pos_after, equity, pnl, reason,
                    f"{f_ema:.3f}", f"{s_ema:.3f}", f"{gap:.1f}", sig
                ])
        except Exception as e:
            log.warning(f"Data Write Error: {e}")

    # [新增] 更新每日统计 CSV (对齐 debug 脚本逻辑)
    def update_daily_stats(self):
        hist_path = os.path.join(DATA_DIR, TRADE_HISTORY_FILE)
        stats_path = os.path.join(DATA_DIR, DAILY_STATS_FILE)
        
        if not os.path.exists(hist_path): return

        try:
            # 读取历史记录
            df = pd.read_csv(hist_path)
            if df.empty: return
            
            # 筛选出有 PnL 的记录 (平仓/止盈/止损/反手平仓)
            df = df[df['pnl'] != 0].copy()
            if df.empty: return

            # 转换时间戳为北京时间日期
            df['datetime_bj'] = pd.to_datetime(df['datetime_bj'])
            df['date'] = df['datetime_bj'].dt.date
            
            # 判定胜负
            df['result'] = df['pnl'].apply(lambda x: 'win' if x > 0 else 'loss')

            # 聚合计算
            daily_summary = df.groupby('date').agg({
                'pnl': 'sum',
                'result': lambda x: (x == 'win').mean() * 100, # 胜率
                'timestamp': 'count' # 交易次数
            }).rename(columns={'pnl': '当日盈亏', 'result': '当日胜率(%)', 'timestamp': '交易次数'})
            
            daily_summary = daily_summary.sort_index(ascending=False)
            
            # 保存
            daily_summary.to_csv(stats_path, encoding='utf-8-sig')
            log.info(f"📅 每日统计已更新: {DAILY_STATS_FILE}")
            
        except Exception as e:
            log.warning(f"更新每日统计失败: {e}")

    # [新增] 生成资金曲线图 (对齐 debug 脚本)
    def generate_equity_curve(self):
        hist_path = os.path.join(DATA_DIR, TRADE_HISTORY_FILE)
        img_path = os.path.join(DATA_DIR, EQUITY_IMAGE_FILE)
        
        if not os.path.exists(hist_path): return

        try:
            df = pd.read_csv(hist_path)
            if df.empty: return
            
            # 只需要时间 equity 列
            df['dt'] = pd.to_datetime(df['timestamp'], unit='s')
            
            # 绘图
            fig, ax = plt.subplots(figsize=(12, 6))
            ax.plot(df['dt'], df['equity'], label='Total Assets ($)', color='#9C27B0', linewidth=2)
            
            # 填充
            initial = self.initial_equity if self.initial_equity else df['equity'].iloc[0]
            ax.fill_between(df['dt'], initial, df['equity'], 
                            where=(df['equity'] >= initial), 
                            interpolate=True, color='#4CAF50', alpha=0.15, label='Profit Area')
            ax.fill_between(df['dt'], initial, df['equity'], 
                            where=(df['equity'] < initial), 
                            interpolate=True, color='#F44336', alpha=0.15, label='Loss Area')
            
            ax.axhline(y=initial, color='black', linestyle='--', alpha=0.5, linewidth=1, label='Initial Capital')
            
            curr_eq = df['equity'].iloc[-1]
            ret_pct = ((curr_eq - initial) / initial) * 100
            
            ax.set_title(f"Live Equity Curve (Return: {ret_pct:.2f}%)", fontsize=14, fontweight='bold')
            ax.set_ylabel("Total Assets ($)", fontsize=12)
            ax.set_xlabel("Time", fontsize=12)
            ax.legend(loc='upper left')
            ax.grid(True, linestyle='--', linewidth=0.5)
            
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
            plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha='right')
            plt.tight_layout()
            
            plt.savefig(img_path, dpi=100)
            plt.close(fig) # 释放内存
            log.info(f"📊 资金曲线图已更新: {EQUITY_IMAGE_FILE}")
            
        except Exception as e:
            log.warning(f"生成资金曲线失败: {e}")

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

        # [修正] 累计收益率计算：使用 (当前净值 - 初始净值) / 初始净值
        total_return_pct = 0.0
        if self.initial_equity and self.initial_equity > 0:
            total_return_pct = (self.current_equity - self.initial_equity) / self.initial_equity * 100

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
            if f_val > s_val:
                ema_trend_icon = "📈 多头"
            else:
                ema_trend_icon = "📉 空头"

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
| **风控模式** | **`{PRICE_CHECK_MODE}`** |
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
| **累计 ROE** | **{total_return_pct:+.2f}%** |
| **实盘胜率** | `{win_rate:.1f}%` ({self.win_trades}/{self.total_trades}) |
| **浮动盈亏** | **`{unrealized_roe:+.2f}%`** |

---
## 📝 最近平仓
{trades_table}
"""
        try:
            with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
                f.write(content.strip())
        except:
            pass

    # [修改] 发送 Dashboard 概览到 Telegram (已增加初始资产显示)
    async def report_dashboard_to_telegram(self, current_price):
        if not self.tg.bot: return
        
        now = time.time()
        # 限制发送频率 (防止刷屏)
        if now - self.last_tg_report_time < TG_REPORT_INTERVAL:
            return
            
        uptime = str(timedelta(seconds=int(now - self.start_time)))
        win_rate = (self.win_trades / self.total_trades * 100) if self.total_trades > 0 else 0.0
        
        # 状态图标
        icon = "⚪ 空仓"
        unrealized = 0.0
        if abs(self.current_position) > 0.001:
            icon = "🟢 多单" if self.current_position > 0 else "🔴 空单"
            pnl_pct = (current_price - self.entry_price) / self.entry_price if self.current_position > 0 else (self.entry_price - current_price) / self.entry_price
            unrealized = pnl_pct * LEVERAGE * 100
        
        # 格式化初始资金，防止初始化未完成时报错
        init_eq = self.initial_equity if self.initial_equity else 0.0
        
        msg = (
            f"<b>🤖 LighterBot 运行报告</b>\n"
            f"--------------------------------\n"
            f"⏱️ 运行时长: {uptime}\n"
            f"💲 当前价格: <b>{current_price:.3f}</b>\n"
            f"💰 资金变动: ${init_eq:.2f} ➔ <b>${self.current_equity:.2f}</b>\n"
            f"📊 持仓状态: {icon}\n"
            f"🌊 浮动盈亏: <b>{unrealized:+.2f}%</b>\n"
            f"📈 累计 ROE: {self.total_pnl_roe:+.2f}%\n"
            f"🏆 交易胜率: {win_rate:.1f}% ({self.win_trades}/{self.total_trades})\n"
            f"--------------------------------\n"
            f"🧠 策略: EMA{EMA_FAST}/{EMA_SLOW} | Gap: {self.last_gap:.1f}bps"
        )
        
        await self.tg.send_message(msg)
        # 如果有资金曲线图，顺便发过去
        img_path = os.path.join(DATA_DIR, EQUITY_IMAGE_FILE)
        if os.path.exists(img_path):
            await self.tg.send_image(img_path, caption="📊 最新资金曲线")
            
        self.last_tg_report_time = now

    def record_trade_result(self, close_price, side_direction):
        if self.entry_price <= 0: return
        raw_pnl = 0.0
        if side_direction > 0:
            raw_pnl = (close_price - self.entry_price) / self.entry_price
        else:
            raw_pnl = (self.entry_price - close_price) / self.entry_price
        realized_roe = raw_pnl * LEVERAGE * 100

        self.total_trades += 1
        self.total_pnl_roe += realized_roe
        if realized_roe > 0: self.win_trades += 1

        t_str = datetime.now().strftime("%H:%M")
        s_str = "多" if side_direction > 0 else "空"
        self.recent_trades.append({'time': t_str, 'side': s_str, 'roe': realized_roe})

        log.info(f"📝 平仓战绩: 本单 {realized_roe:+.2f}% | 总计 {self.total_pnl_roe:+.2f}%")

        # [修改] 记录 PnL 日志，并更新统计和图片
        self.log_trade_action("PnlRecord", close_price, 0, 0, 0, self.current_equity, pnl=realized_roe, reason=s_str,
                              f_ema=self.last_ema_fast, s_ema=self.last_ema_slow, gap=self.last_gap, sig=self.last_signal)
        
        # 触发统计更新和绘图
        self.update_daily_stats()
        self.generate_equity_curve()

    async def fetch_price(self, session):
        try:
            async with session.get(PRICE_URL, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data["order_book_details"][0]["last_trade_price"])
                return None
        except:
            return None

    def to_int(self, val: float, dec: int) -> int:
        return int(round(val * (10 ** dec)))

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
        if self.initial_equity is None or self.initial_equity == 0: return
        drawdown_pct = (self.current_equity - self.initial_equity) / self.initial_equity
        if drawdown_pct <= -DAILY_MAX_LOSS:
            log.error(f"🔥 [严重] 触发熔断！亏损 {drawdown_pct * 100:.2f}%")
            # [新增] 熔断推送
            msg = f"🔥 <b>[严重] 熔断警告</b>\n亏损已达 {drawdown_pct*100:.2f}%，脚本正在紧急平仓并停止。"
            await self.tg.send_message(msg)
            
            if abs(self.current_position) > 0.001:
                action = "sell" if self.current_position > 0 else "buy"
                await self.place_order(action, abs(self.current_position), current_price, reduce_only=True)
                self.log_trade_action("Meltdown", current_price, abs(self.current_position), self.current_position, 0,
                                      self.current_equity, reason="MaxLoss",
                                      f_ema=self.last_ema_fast, s_ema=self.last_ema_slow, gap=self.last_gap, sig=self.last_signal)
            sys.exit(1)

        if abs(self.current_position) < 0.001 or self.entry_price <= 0: return
        pnl_pct = (current_price - self.entry_price) / self.entry_price if self.current_position > 0 else (
                                                                                                                      self.entry_price - current_price) / self.entry_price
        roe_pct = pnl_pct * LEVERAGE

        target_action = ""
        trigger_msg = ""
        reason_code = ""
        if roe_pct >= TAKE_PROFIT_PCT:
            target_action = "sell" if self.current_position > 0 else "buy"
            trigger_msg = f"🚀 止盈 (+{roe_pct * 100:.2f}%)"
            reason_code = "TP"
        elif roe_pct <= -STOP_LOSS_PCT:
            target_action = "sell" if self.current_position > 0 else "buy"
            trigger_msg = f"🛡️ 止损 ({roe_pct * 100:.2f}%)"
            reason_code = "SL"

        if target_action:
            log.warning(f"🔔 {trigger_msg} 触发!")
            # [新增] 发送风控通知
            await self.tg.send_message(f"🔔 <b>{trigger_msg}</b>\n正在执行风控平仓...")
            
            if await self.place_order(target_action, abs(self.current_position), current_price, reduce_only=True):
                log.info("⏳ 等待确认...")
                await asyncio.sleep(3)
                p, e, eq = await self.get_account_full_state(session)

                # 记录对账 (带 EMA 信息)
                self.log_trade_action(reason_code, current_price, abs(self.current_position), self.current_position, p,
                                      eq, reason=trigger_msg,
                                      f_ema=self.last_ema_fast, s_ema=self.last_ema_slow, gap=self.last_gap, sig=self.last_signal)

                if abs(p) < 0.001:
                    old_dir = 1 if target_action == "sell" else -1
                    self.record_trade_result(current_price, old_dir)
                    
                    # [新增] 发送成交结果 (含图片)
                    pnl_val = (eq - self.current_equity)
                    res_msg = f"✅ <b>风控平仓完成</b>\n盈亏额: ${pnl_val:.2f}\n当前权益: ${eq:.2f}"
                    await self.tg.send_message(res_msg)
                    img_path = os.path.join(DATA_DIR, EQUITY_IMAGE_FILE)
                    if os.path.exists(img_path): await self.tg.send_image(img_path)

                    self.current_position = 0.0
                    self.entry_price = 0.0
                    self.current_equity = eq
                    log.info("✅ 风控平仓成功")
                else:
                    log.error("❌ 风控平仓失败")
                    await self.tg.send_message("❌ 风控平仓执行失败，请检查！")
                    self.current_position = p

    async def execute_signal(self, signal: int, price: float, session):
        current_pos = self.current_position

        # 1. 过滤：如果已经同向满仓，忽略信号
        if (signal == 1 and current_pos >= 0.99) or (signal == -1 and current_pos <= -0.99):
            log.info(f"🚫 仓位已达上限 ({current_pos}), 忽略信号")
            return

        # 2. 准备交易参数
        target_action = "buy" if signal == 1 else "sell"
        trade_size = TRADE_SIZE_SOL
        reduce_only = False
        action_type = "Strategy_Open" # [修改] 明确信号来源

        # 3. 判断是否需要反手 (Flip)
        is_flip = False
        if (current_pos > 0.001 and signal == -1) or (current_pos < -0.001 and signal == 1):
            is_flip = True
            action_type = "Flip"
            log.info(f"🔄 触发反手逻辑: 当前 {current_pos} -> 信号 {signal}")
            # [新增] 反手通知
            await self.tg.send_message(f"🔄 <b>触发反手逻辑</b>\n方向变更为: {'做空' if signal == -1 else '做多'}")

            # 第一步：平掉当前仓位
            close_action = "sell" if current_pos > 0 else "buy"
            close_size = abs(current_pos)
            log.info(f"🔄 [反手 Step 1] 平旧仓: {close_action} {close_size}")

            if await self.place_order(close_action, close_size, price, reduce_only=True):
                await asyncio.sleep(2)  # 等待成交
                # 记录平仓结果
                old_dir = 1 if current_pos > 0 else -1
                self.record_trade_result(price, old_dir)
                
                self.log_trade_action("FlipClose", price, close_size, current_pos, 0, self.current_equity,
                                      reason="Flip_Step1",
                                      f_ema=self.last_ema_fast, s_ema=self.last_ema_slow, gap=self.last_gap, sig=self.last_signal)
                
                self.current_position = 0.0  # 临时归零
            else:
                log.error("❌ 反手第一步(平仓)失败，终止反手")
                return

        # 4. 执行开仓 (无论是普通开仓还是反手的第二步)
        # 此时理论上 self.current_position 应该接近 0 (如果是反手的话)
        log.info(f"🚀 [{'反手 Step 2' if is_flip else '开仓'}] 执行: {target_action} {trade_size}")
        
        # [新增] 发送开仓通知
        emoji = "🟢" if target_action == "buy" else "🔴"
        await self.tg.send_message(f"{emoji} <b>执行开仓</b>\n方向: {target_action.upper()}\n数量: {trade_size} SOL\n价格: {price:.3f}")

        if await self.place_order(target_action, trade_size, price, reduce_only=False):
            await asyncio.sleep(3)
            p, e, eq = await self.get_account_full_state(session)
            if p is not None:
                # 记录对账 (带 EMA 信息)
                self.log_trade_action(action_type, price, trade_size, 0, p, eq, reason="Strategy_Signal", # [修改] 明确信号来源
                                      f_ema=self.last_ema_fast, s_ema=self.last_ema_slow, gap=self.last_gap, sig=self.last_signal)

                self.current_position, self.entry_price, self.current_equity = p, e, eq
                log.info(f"✅ 信号执行完毕. Pos: {self.current_position}")

    async def run(self):
        log.info(f"🤖 LighterBot 启动 | 策略: EMA{EMA_FAST}/{EMA_SLOW} | 风控模式: {PRICE_CHECK_MODE}")
        
        # [新增] 启动通知
        await self.tg.send_message("🤖 <b>LighterBot 已启动</b>\n策略正在运行中...")

        while True:
            try:
                self.client = lighter.SignerClient(
                    url=os.getenv("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai"),
                    private_key=os.getenv("API_KEY_PRIVATE_KEY"),
                    account_index=int(os.getenv("ACCOUNT_INDEX")),
                    api_key_index=int(os.getenv("API_KEY_INDEX")),
                )
                if self.client: break
            except Exception:
                await asyncio.sleep(3)

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

            # [关键修改] 启动时对齐时间到最近的整周期
            self.bar_start_time = int(time.time() // CANDLE_INTERVAL) * CANDLE_INTERVAL
            self.last_print_time = time.time()
            self.bar_open = -1.0

            while True:
                now = time.time()
                price = await self.fetch_price(session)
                if price is None: await asyncio.sleep(2); continue

                self.record_tick_data(price)

                # [新增] 只有在 REALTIME 模式下才实时检查风控
                if PRICE_CHECK_MODE == 'REALTIME':
                    await self.check_risk_and_tpsl(price, session)

                time_elapsed = now - self.bar_start_time
                time_left = max(0, CANDLE_INTERVAL - time_elapsed)
                mins, secs = divmod(int(time_left), 60)
                countdown_str = f"{mins:02d}:{secs:02d}"

                self.update_dashboard(price, countdown_str)
                
                # [新增] 尝试发送 Telegram 周期报告
                await self.report_dashboard_to_telegram(price)

                if now - self.last_print_time >= 10:
                    p, e, eq = await self.get_account_full_state(session)
                    if eq: self.current_equity = eq
                    log.info(f"💓 HB: Price={price:.2f} | Pos={self.current_position} | NextK: {countdown_str}")
                    self.last_print_time = now

                if self.bar_open == -1.0:
                    self.bar_open = self.bar_high = self.bar_low = price
                    # [关键修改] 不再在此处重置 start_time，保持对齐逻辑
                else:
                    self.bar_high = max(self.bar_high, price)
                    self.bar_low = min(self.bar_low, price)

                # [关键修改] 严格按照时间间隔闭合 K 线
                if now >= self.bar_start_time + CANDLE_INTERVAL:
                    close_price = price
                    log.info(f"📊 K线闭合: C={close_price:.2f}")

                    # [新增] 如果是 ON_CLOSE 模式，在K线闭合时检查风控
                    if PRICE_CHECK_MODE == 'ON_CLOSE':
                        await self.check_risk_and_tpsl(close_price, session)

                    signal = self.strategy.on_close_fast_adapt(close_price, self.bar_high, self.bar_low)

                    f_val = self.strategy.fast_ema.value
                    s_val = self.strategy.slow_ema.value
                    self.last_ema_fast = f_val if f_val else 0
                    self.last_ema_slow = s_val if s_val else 0

                    gap_rec = 0
                    if f_val and s_val: gap_rec = abs(f_val - s_val) / close_price * 10000
                    
                    self.last_gap = gap_rec
                    self.last_signal = signal
                    
                    self.ema_history.append({
                        'time': datetime.now().strftime("%H:%M"),
                        'fast': f_val if f_val else 0,
                        'slow': s_val if s_val else 0,
                        'gap': gap_rec
                    })

                    # --- 决策原因分析 ---
                    reason = "✅ 触发交易"
                    if signal == 0:
                        if f_val is None:
                            reason = "⏳ EMA计算中"
                        else:
                            gap = abs(f_val - s_val) / close_price
                            thresh = init_band_bps / 10000.0
                            if gap < thresh:
                                reason = f"❌ 震荡 (Gap {gap * 10000:.1f} < {init_band_bps})"
                            else:
                                prev_f = getattr(self.strategy, '_prev_fast_val', None)
                                is_bull = f_val > s_val
                                if is_bull:
                                    if close_price < f_val:
                                        reason = "❌ 价格 < 快线"
                                    elif prev_f and (f_val - prev_f) <= 0:
                                        reason = "❌ 快线向下"
                                    else:
                                        reason = "⚖️ 趋势未确认"
                                else:
                                    if close_price > f_val:
                                        reason = "❌ 价格 > 快线"
                                    elif prev_f and (f_val - prev_f) >= 0:
                                        reason = "❌ 快线向上"
                                    else:
                                        reason = "⚖️ 趋势未确认"

                    self.last_decision_reason = reason
                    
                    # [关键新增] 无论是否交易，记录当前 K 线状态，用于后期回测对比
                    self.log_trade_action("BarData", close_price, 0, self.current_position, 
                                          self.current_position, self.current_equity, 
                                          reason=reason, # 记录具体的决策逻辑
                                          f_ema=self.last_ema_fast, s_ema=self.last_ema_slow, gap=gap_rec, sig=signal)

                    log.info(f"🧐 决策: {reason}")

                    pos_dir = 1 if self.current_position > 0.001 else (-1 if self.current_position < -0.001 else 0)
                    self.strategy.set_position_side(pos_dir)

                    if signal != 0:
                        await self.execute_signal(signal, close_price, session)
                    elif signal == 0 and abs(self.current_position) > 0.001:
                        log.info(f"📉 趋势消失，平仓...")
                        
                        await self.tg.send_message(f"📉 <b>趋势结束</b>\n执行平仓，当前价格: {close_price:.3f}")

                        old_dir = 1 if self.current_position > 0 else -1
                        self.record_trade_result(close_price, old_dir)
                        action = "sell" if self.current_position > 0 else "buy"
                        await self.place_order(action, abs(self.current_position), close_price, reduce_only=True)

                        # [修改] 明确平仓来源
                        self.log_trade_action("CloseSignal", close_price, abs(self.current_position),
                                              self.current_position, 0, self.current_equity, reason="Strategy_TrendEnd",
                                              f_ema=self.last_ema_fast, s_ema=self.last_ema_slow, gap=self.last_gap, sig=self.last_signal)

                        self.current_position = 0.0

                    # [关键修改] 递增时间到下一个整周期，并重置 High/Low
                    self.bar_start_time += CANDLE_INTERVAL
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
        except:
            pass