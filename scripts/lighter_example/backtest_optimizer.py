# -*- coding: utf-8 -*-
import pandas as pd
import numpy as np
import os
import glob
import time
import sys
from datetime import datetime

# ⚠️ 确保能引用策略文件
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from strategies.ema_crossover import EMACrossover
except ImportError:
    print("❌ 错误：找不到策略文件，请检查路径。")
    exit()

# ================= ⚙️ 6维参数配置区域 =================
DATA_DIR = "data"
FILE_PATTERN = "sol_ticks_*.csv"

# 仿真账户
INITIAL_CAPITAL = 200.0
LEVERAGE = 10
COST_RATE = 0.0000      # 万5手续费+滑点
TRADE_SIZE_SOL = 1    # 固定仓位 (与实盘一致)

# 1. K线周期 (Timeframe)
TIMEFRAMES = ['0.5min','1min', '5min', '15min'] 

# 2. EMA 参数 (Fast / Slow)
FAST_LIST = [3, 6, 9, 12]
SLOW_LIST = [10, 15, 20, 26, 50, 60]

# 3. 带宽阈值 (Band Bps)
BAND_LIST = [10, 20, 30, 50]  # 单位: bps

# 4. 止盈 (TP)
TP_LIST = [0.01, 0.02, 0.03, 0.05]

# 5. 止损 (SL)
SL_LIST = [0.01, 0.02, 0.03]

# 断层阈值
GAP_THRESHOLD = 3600      
# =====================================================

def load_and_slice_data(data_dir):
    print(f"📂 正在读取数据...")
    all_files = glob.glob(os.path.join(data_dir, FILE_PATTERN))
    all_files.sort()
    if not all_files: return []
    
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
    print(f"✅ 数据加载完毕: {len(valid_sessions)} 个有效片段")
    return valid_sessions

def get_klines(sessions, timeframe):
    """Layer 1: 重采样 K 线"""
    kline_sessions = []
    for df in sessions:
        # 需要 OHLC 数据喂给策略
        df_k = df.set_index('datetime').resample(timeframe).agg({
            'price': ['first', 'max', 'min', 'last']
        }).dropna()
        df_k.columns = ['open', 'high', 'low', 'close']
        kline_sessions.append(df_k)
    return kline_sessions

def calc_signals_with_strategy(kline_sessions, fast, slow, band_bps):
    """Layer 2: 使用策略类生成信号"""
    signal_sessions = []
    
    for df_k in kline_sessions:
        # 实例化策略
        strategy = EMACrossover(
            fast=fast, slow=slow, 
            band_mode="bps", band_bps=band_bps, 
            confirm_bars=1, print_ema_each_bar=False
        )
        
        sigs = []
        for idx, row in df_k.iterrows():
            # 逐根K线喂入
            s = strategy.on_close_fast_adapt(row['close'], row['high'], row['low'])
            sigs.append(s)
        
        # 将信号序列转换为 Series 并滞后一期
        sig_series = pd.Series(sigs, index=df_k.index).shift(1).fillna(0)
        signal_sessions.append(sig_series)
        
    return signal_sessions

def run_fast_simulation(tick_sessions, signal_sessions, tp, sl):
    """Layer 4: 极速回测 (固定仓位模式)"""
    total_balance = INITIAL_CAPITAL
    total_trades = 0
    total_wins = 0
    
    for sess_idx, df_ticks in enumerate(tick_sessions):
        signals = signal_sessions[sess_idx]
        
        tick_prices = df_ticks['price'].values
        tick_times = df_ticks['datetime'].values
        sig_times = signals.index.values
        sig_vals = signals.values
        
        balance = total_balance
        position = 0
        entry_price = 0.0
        
        s_idx = 0
        n_sigs = len(sig_vals)
        n_ticks = len(tick_prices)
        
        for i in range(n_ticks):
            t_time = tick_times[i]
            price = tick_prices[i]
            
            while s_idx < n_sigs and t_time >= sig_times[s_idx]:
                curr_sig = int(sig_vals[s_idx])
                s_idx += 1
                
                if position != 0 and curr_sig != 0 and curr_sig != position:
                    pnl_amt = (price - entry_price) * TRADE_SIZE_SOL * position if position == 1 else (entry_price - price) * TRADE_SIZE_SOL
                    position_value = price * TRADE_SIZE_SOL
                    fee = position_value * COST_RATE * 2 
                    balance += (pnl_amt - fee)
                    total_trades += 1
                    if pnl_amt > fee: total_wins += 1
                    position = 0
                
                if position == 0 and curr_sig != 0:
                    position = curr_sig
                    entry_price = price
            
            if position != 0:
                margin = (entry_price * TRADE_SIZE_SOL) / LEVERAGE
                raw_pnl = (price - entry_price) * TRADE_SIZE_SOL * position if position == 1 else (entry_price - price) * TRADE_SIZE_SOL
                roe = raw_pnl / margin
                
                if roe >= tp or roe <= -sl:
                    position_value = price * TRADE_SIZE_SOL
                    fee = position_value * COST_RATE * 2
                    balance += (raw_pnl - fee)
                    total_trades += 1
                    if raw_pnl > fee: total_wins += 1
                    position = 0
        
        if position != 0:
            last_p = tick_prices[-1]
            pnl_amt = (last_p - entry_price) * TRADE_SIZE_SOL * position if position == 1 else (entry_price - last_p) * TRADE_SIZE_SOL
            position_value = last_p * TRADE_SIZE_SOL
            fee = position_value * COST_RATE * 2
            balance += (pnl_amt - fee)
            total_trades += 1
            
        total_balance = balance

    return total_balance, total_trades, total_wins

def main():
    start_time = time.time()
    tick_sessions = load_and_slice_data(DATA_DIR)
    if not tick_sessions: return

    total_combos = len(TIMEFRAMES) * len(FAST_LIST) * len(SLOW_LIST) * len(BAND_LIST) * len(TP_LIST) * len(SL_LIST)
    print(f"\n🚀 6维全参数回测启动! (策略类模式)")
    print(f"📊 组合数: {total_combos} | 模式: 固定仓位 {TRADE_SIZE_SOL} SOL")
    print("-" * 60)
    
    results = []
    counter = 0
    
    # Layer 1: K线周期
    for tf in TIMEFRAMES:
        kline_sessions = get_klines(tick_sessions, tf)
        
        # Layer 2: EMA 参数
        for fast in FAST_LIST:
            for slow in SLOW_LIST:
                if fast >= slow: continue
                
                # Layer 3: 带宽 (这里要实例化策略了)
                for band in BAND_LIST:
                    # 计算信号 (现在使用真实的策略类)
                    signal_sessions = calc_signals_with_strategy(kline_sessions, fast, slow, band)
                    
                    # Layer 4: 止盈止损
                    for tp in TP_LIST:
                        for sl in SL_LIST:
                            counter += 1
                            
                            end_bal, trades, wins = run_fast_simulation(tick_sessions, signal_sessions, tp, sl)
                            
                            roe = (end_bal - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
                            win_rate = (wins/trades*100) if trades > 0 else 0
                            
                            results.append({
                                'TF': tf, 'Fast': fast, 'Slow': slow, 
                                'Band': band, 'TP': tp, 'SL': sl,
                                'ROE': roe, 'Trades': trades, 'WinRate': win_rate
                            })
                            
                            if counter % 100 == 0:
                                print(f"进度: {counter}/{total_combos} ...")
                            
                            if roe > 0 and trades > 10:
                                print(f"✨ [盈利] {tf} {fast}/{slow} B:{band} TP:{tp:.0%} SL:{sl:.0%} -> ROE: {roe:.2f}% ({trades}次)")

    print("-" * 60)
    print(f"✅ 计算耗时: {time.time() - start_time:.1f}秒")
    
    if results:
        df = pd.DataFrame(results)
        df.sort_values(by='ROE', ascending=False, inplace=True)
        
        best = df.iloc[0]
        print(f"\n🏆 === 冠军参数组合 ===")
        print(f"1️⃣ 周期: {best['TF']}")
        print(f"2️⃣ EMA : {best['Fast']} / {best['Slow']}")
        print(f"3️⃣ 带宽: {best['Band']} bps")
        print(f"4️⃣ 止盈: {best['TP']:.1%}")
        print(f"5️⃣ 止损: {best['SL']:.1%}")
        print(f"💰 收益: {best['ROE']:.2f}%")
        print(f"📊 胜率: {best['WinRate']:.1f}%")
        print(f"🔢 交易: {best['Trades']}")
        
        df.to_csv("optimization_6d_results.csv", index=False)
        print("\n💾 完整榜单已保存至 optimization_6d_results.csv")

if __name__ == "__main__":
    main()