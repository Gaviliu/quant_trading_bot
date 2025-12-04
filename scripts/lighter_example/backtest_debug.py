# -*- coding: utf-8 -*-
import pandas as pd
import numpy as np
import os
import glob
import sys
from datetime import datetime

# ⚠️ 关键：确保能引用到策略文件
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from strategies.ema_crossover import EMACrossover
except ImportError:
    print("❌ 错误：找不到策略文件。请确保本脚本在 scripts/lighter_example/ 目录下，且 strategies 文件夹在项目根目录。")
    exit()

# ================= 🔍 诊断配置区域 =================
DATA_DIR = "data"
FILE_PATTERN = "sol_ticks_*.csv"

# 仿真账户
INITIAL_CAPITAL = 200.0
LEVERAGE = 10
COST_RATE = 0.0000      # 手续费+滑点 (为了验证策略本身，可设为0)
TRADE_SIZE_SOL = 1.0    # ⚠️ 固定仓位：每次 1 SOL (对齐实盘逻辑)

# 🏆 冠军参数 (15min | 3/20 | Band 10)
EMA_FAST = 3
EMA_SLOW = 20
CANDLE_SEC = 900        # 15分钟
BAND_BPS = 10           # 带宽 0.1%

# 止盈止损
TAKE_PROFIT_PCT = 0.05  # 5%
STOP_LOSS_PCT = 0.02    # 2%

# 断层阈值
GAP_THRESHOLD = 3600 
# ==================================================

def load_and_slice_data(data_dir):
    print(f"📂 正在扫描 {data_dir} ...")
    all_files = glob.glob(os.path.join(data_dir, FILE_PATTERN))
    all_files.sort()
    
    if not all_files:
        print("❌ 未找到数据文件")
        return []

    df_list = []
    for f in all_files:
        try:
            df = pd.read_csv(f)
            if 'timestamp' in df.columns: df_list.append(df)
        except: pass

    if not df_list: return []
    
    full_df = pd.concat(df_list, ignore_index=True)
    full_df['datetime'] = pd.to_datetime(full_df['timestamp'], unit='s')
    full_df = full_df.sort_values('datetime').drop_duplicates('timestamp').reset_index(drop=True)
    
    full_df['gap'] = full_df['timestamp'].diff()
    split_indices = full_df[full_df['gap'] > GAP_THRESHOLD].index.tolist()
    
    sessions = []
    start_idx = 0
    for idx in split_indices:
        sessions.append(full_df.iloc[start_idx:idx].copy())
        start_idx = idx
    sessions.append(full_df.iloc[start_idx:].copy())
    
    valid_sessions = [s for s in sessions if len(s) > 300]
    print(f"✅ 数据加载完成: 切分为 {len(valid_sessions)} 个连续片段")
    return valid_sessions

def process_session(df_session, start_balance):
    if df_session.empty: return start_balance, [], 0, 0
    
    # 1. 合成 K 线
    df_kline = df_session.set_index('datetime').resample(f'{CANDLE_SEC}s').agg({
        'price': ['first', 'max', 'min', 'last']
    }).dropna()
    # 扁平化列名: open, high, low, close
    df_kline.columns = ['open', 'high', 'low', 'close']

    # 2. 初始化策略对象 (1:1 还原实盘)
    strategy = EMACrossover(
        fast=EMA_FAST,
        slow=EMA_SLOW,
        band_mode="bps",
        band_bps=BAND_BPS,
        confirm_bars=1,
        print_ema_each_bar=False
    )
    
    # 3. 遍历 K 线生成信号
    signals_list = []
    for idx, row in df_kline.iterrows():
        # 模拟实盘：K线闭合时喂入数据
        sig = strategy.on_close_fast_adapt(row['close'], row['high'], row['low'])
        signals_list.append(sig)
    
    df_kline['signal'] = signals_list
    
    # 信号查找表 (Shift 1: K线收盘产生的信号，下一秒生效)
    signal_series = df_kline['signal'].shift(1).fillna(0)
    
    # 4. Tick 级回测循环
    balance = start_balance
    position = 0
    entry_price = 0.0
    session_logs = []
    trades = 0
    wins = 0
    
    sig_times = signal_series.index.values
    sig_vals = signal_series.values
    tick_times = df_session['datetime'].values
    tick_prices = df_session['price'].values
    
    k_idx = 0
    n_sigs = len(sig_vals)
    
    for i in range(len(tick_prices)):
        t_time = tick_times[i]
        price = tick_prices[i]
        
        # 更新信号
        while k_idx < n_sigs and t_time >= sig_times[k_idx]:
            current_signal = int(sig_vals[k_idx])
            k_idx += 1
            
            # 策略反转逻辑
            # 如果策略发出了反向信号 (或0，如果在实盘里处理了0的话)
            # 这里假设只处理 1 和 -1 的强反转
            if position != 0 and current_signal != 0 and current_signal != position:
                # 平仓
                pnl_amt = (price - entry_price) * TRADE_SIZE_SOL if position == 1 else (entry_price - price) * TRADE_SIZE_SOL
                fee = price * TRADE_SIZE_SOL * COST_RATE * 2
                net_pnl = pnl_amt - fee
                balance += net_pnl
                
                trades += 1
                if net_pnl > 0: wins += 1
                
                color = "🟢" if net_pnl > 0 else "🔴"
                time_str = str(t_time)[5:-3]
                
                # 计算展示用的 ROE
                margin = (entry_price * TRADE_SIZE_SOL) / LEVERAGE
                roe_disp = (pnl_amt / margin) * 100 if margin > 0 else 0
                
                session_logs.append(f"{time_str:<16} {color}反转   {price:<8.3f} {roe_disp:>6.2f}%  ${net_pnl:>6.2f}  ${balance:.2f}")
                position = 0 
            
            # 开仓逻辑
            if position == 0 and current_signal != 0:
                position = current_signal
                entry_price = price
                action = "做多" if position == 1 else "做空"
                time_str = str(t_time)[5:-3]
                session_logs.append(f"{time_str:<16} {action:<6} {price:<8.3f} {'-':<8} {'-':<8} ${balance:.2f}")

        # 实时止盈止损 (Tick Level)
        if position != 0:
            margin = (entry_price * TRADE_SIZE_SOL) / LEVERAGE
            raw_pnl = (price - entry_price) * TRADE_SIZE_SOL if position == 1 else (entry_price - price) * TRADE_SIZE_SOL
            roe = raw_pnl / margin # 实际 ROE
            
            if roe >= TAKE_PROFIT_PCT or roe <= -STOP_LOSS_PCT:
                fee = price * TRADE_SIZE_SOL * COST_RATE * 2
                net_pnl = raw_pnl - fee
                balance += net_pnl
                
                trades += 1
                if net_pnl > 0: wins += 1
                
                reason = "🟢止盈" if roe > 0 else "🔴止损"
                time_str = str(t_time)[5:-3]
                session_logs.append(f"{time_str:<16} {reason:<6} {price:<8.3f} {roe*100:>6.2f}%  ${net_pnl:>6.2f}  ${balance:.2f}")
                position = 0

    # Session 结束强制结算
    if position != 0:
        last_p = tick_prices[-1]
        pnl_amt = (last_p - entry_price) * TRADE_SIZE_SOL if position == 1 else (entry_price - last_p) * TRADE_SIZE_SOL
        fee = last_p * TRADE_SIZE_SOL * COST_RATE * 2
        net_pnl = pnl_amt - fee
        balance += net_pnl
        trades += 1
        time_str = str(tick_times[-1])[5:-3]
        session_logs.append(f"{time_str:<16} 🏁结算   {last_p:<8.3f} {'End':>6}  ${net_pnl:>6.2f}  ${balance:.2f}")

    return balance, session_logs, trades, wins

def run_debug():
    sessions = load_and_slice_data(DATA_DIR)
    if not sessions: print("没数据"); return
    
    print(f"\n🔎 精准回测诊断 (策略: EMACrossover | K线: {CANDLE_SEC/60}min)")
    print(f"⚙️ 参数: EMA {EMA_FAST}/{EMA_SLOW} | Band {BAND_BPS} | TP {TAKE_PROFIT_PCT:.0%} | SL {STOP_LOSS_PCT:.0%}")
    print(f"💰 资金: {INITIAL_CAPITAL}U | 每次: {TRADE_SIZE_SOL} SOL")
    print("-" * 80)
    print(f"{'时间':<16} {'动作':<6} {'价格':<8} {'盈亏(ROE)':<8} {'盈亏额':<8} {'当前余额'}")
    print("-" * 80)
    
    current_balance = INITIAL_CAPITAL
    total_trades = 0
    total_wins = 0
    
    for df_slice in sessions:
        print(f"\n--- 新时段开始: {df_slice['datetime'].iloc[0]} ---")
        end_bal, logs, tr, w = process_session(df_slice, current_balance)
        for log in logs: print(log)
        
        current_balance = end_bal
        total_trades += tr
        total_wins += w

    print("-" * 80)
    win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0
    total_ret = (current_balance - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    
    print(f"💰 最终资金: ${current_balance:.2f}")
    print(f"📈 总收益率: {total_ret:+.2f}%")
    print(f"📊 总操作数: {total_trades} 次")
    print(f"🏆 胜率:     {win_rate:.1f}%")

if __name__ == "__main__":
    run_debug()