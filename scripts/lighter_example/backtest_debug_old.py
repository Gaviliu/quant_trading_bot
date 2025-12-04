# -*- coding: utf-8 -*-

import pandas as pd

import numpy as np

import os

import glob

from datetime import datetime



# ================= 🔍 诊断配置区域 =================

DATA_DIR = "data"

FILE_PATTERN = "sol_ticks_*.csv"



# 仿真参数

INITIAL_CAPITAL = 200.0

LEVERAGE = 10

COST_RATE = 0.0000      # 手续费+滑点

TRADE_SIZE_SOL = 1.0    # ⚠️ 新增：每次固定交易 1 SOL (可根据需要改为 0.1)



# 🏆 策略参数 (请填入优化器跑出的冠军参数)

EMA_FAST = 3

EMA_SLOW = 20

CANDLE_SEC = 900        # 15分钟

BAND_BPS = 10           # 带宽 (10 = 0.1%)



# 止盈止损

TAKE_PROFIT_PCT = 0.05

STOP_LOSS_PCT = 0.02



# 断层阈值 (1小时)

GAP_THRESHOLD = 3600

# ==================================================



def load_and_slice_data(data_dir):

    """读取并分段，保证连续性"""

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

    df_kline = df_session.set_index('datetime').resample(f'{CANDLE_SEC}s').agg({'price': 'ohlc'}).dropna()

    df_kline.columns = df_kline.columns.droplevel(0)



    # 2. 计算指标

    df_kline['ema_fast'] = df_kline['close'].ewm(span=EMA_FAST, adjust=False).mean()

    df_kline['ema_slow'] = df_kline['close'].ewm(span=EMA_SLOW, adjust=False).mean()

   

    # 加入 Band Gap 计算

    df_kline['gap'] = (df_kline['ema_fast'] - df_kline['ema_slow']).abs() / df_kline['close']

    threshold = BAND_BPS / 10000.0

   

    df_kline['signal'] = 0

   

    long_cond = (df_kline['ema_fast'] > df_kline['ema_slow']) & (df_kline['gap'] >= threshold)

    short_cond = (df_kline['ema_fast'] < df_kline['ema_slow']) & (df_kline['gap'] >= threshold)

   

    df_kline.loc[long_cond, 'signal'] = 1

    df_kline.loc[short_cond, 'signal'] = -1

   

    # 信号查找表 (Shift 1)

    signal_series = df_kline['signal'].shift(1).fillna(0)

   

    # 准备遍历

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

           

            # 信号反转逻辑

            if position != 0 and current_signal != position:

                # ⚠️ 修正：使用固定仓位计算

                pnl_amt = (price - entry_price) * TRADE_SIZE_SOL if position == 1 else (entry_price - price) * TRADE_SIZE_SOL

                # 成本：仓位价值 * 费率 * 2 (开+平)

                position_val = price * TRADE_SIZE_SOL

                fee = position_val * COST_RATE * 2

               

                net_pnl = pnl_amt - fee

                balance += net_pnl

               

                # 记录 ROE 仅用于显示

                margin = (entry_price * TRADE_SIZE_SOL) / LEVERAGE

                roe_disp = (pnl_amt / margin) if margin > 0 else 0

               

                trades += 1

                if net_pnl > 0: wins += 1

               

                color = "🟢" if net_pnl > 0 else "🔴"

                reason = "反转" if current_signal != 0 else "震荡"

                time_str = str(t_time)[5:-3]

                session_logs.append(f"{time_str:<16} {color}{reason:<6} {price:<8.3f} {roe_disp*100:>6.2f}%  ${net_pnl:>6.2f}  ${balance:.2f}")

                position = 0

           

            # 开仓逻辑

            if position == 0 and current_signal != 0:

                position = current_signal

                entry_price = price

                action = "做多" if position == 1 else "做空"

                time_str = str(t_time)[5:-3]

                session_logs.append(f"{time_str:<16} {action:<6} {price:<8.3f} {'-':<8} {'-':<8} ${balance:.2f}")



        # 实时止盈止损

        if position != 0:

            # 计算 ROE (用于触发风控)

            margin = (entry_price * TRADE_SIZE_SOL) / LEVERAGE

            raw_pnl = (price - entry_price) * TRADE_SIZE_SOL * position if position == 1 else (entry_price - price) * TRADE_SIZE_SOL

            roe = raw_pnl / margin

           

            if roe >= TAKE_PROFIT_PCT or roe <= -STOP_LOSS_PCT:

                position_val = price * TRADE_SIZE_SOL

                fee = position_val * COST_RATE * 2

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

        position_val = last_p * TRADE_SIZE_SOL

        fee = position_val * COST_RATE * 2

        net_pnl = pnl_amt - fee

        balance += net_pnl

       

        trades += 1

        if net_pnl > 0: wins += 1

       

        time_str = str(tick_times[-1])[5:-3]

        session_logs.append(f"{time_str:<16} 🏁结算   {last_p:<8.3f} {'End':>6}  ${net_pnl:>6.2f}  ${balance:.2f}")



    return balance, session_logs, trades, wins



def run_debug():

    sessions = load_and_slice_data(DATA_DIR)

    if not sessions: print("没数据"); return

   

    print(f"\n🔎 精准回测诊断 (固定仓位: {TRADE_SIZE_SOL} SOL | K线: {CANDLE_SEC/60}min)")

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