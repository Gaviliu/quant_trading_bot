import itertools
import pandas as pd
from run_backtest import Backtester   # 你已有
from strategies.ema_crossover import EMACrossover

def run_one(df, fast, slow, confirm, band_mode, atr_k, cooldown, trend_ema, band_bps):
    bt = Backtester(
        df=df,
        fast=fast,
        slow=slow,
        confirm=confirm,
        band_bps=band_bps,
        band_mode=band_mode,
        atr_k=atr_k,
        trend_ema=trend_ema,
        cooldown_bars=cooldown,
        fee_bps=0.0,
        tp_pct=0.02,
        sl_pct=0.01,
        fixed_size_in_coin=10.0,
        fast_mode=True
    )
    bt.run()
    trades = pd.DataFrame(bt.trades)
    total_pnl = trades["pnl"].sum() if not trades.empty else 0
    return total_pnl

def main():
    df = pd.read_csv("data/SOLUSDT_5m.csv")

    fast_values = [9, 13, 21]
    slow_values = [21, 34, 55]
    confirm_values = [1, 2, 3]
    band_mode_values = ["bps", "atr_pct"]
    atr_k_values = [0.3, 0.6, 1.0]
    band_bps_values = [50, 120, 200]
    cooldown_values = [0, 1, 2]
    trend_ema_values = [0, 200]

    results = []

    for fast, slow, confirm, band_mode, atr_k, band_bps, cooldown, trend_ema in itertools.product(
        fast_values, slow_values, confirm_values, band_mode_values,
        atr_k_values, band_bps_values, cooldown_values, trend_ema_values
    ):
        pnl = run_one(df, fast, slow, confirm, band_mode, atr_k, cooldown, trend_ema, band_bps)
        results.append({
            "fast": fast, "slow": slow, "confirm": confirm,
            "band_mode": band_mode, "atr_k": atr_k, "band_bps": band_bps,
            "cooldown": cooldown, "trend_ema": trend_ema,
            "pnl": pnl
        })
        print(f"[DONE] fast={fast} slow={slow} confirm={confirm} mode={band_mode} atr_k={atr_k} bps={band_bps} cooldown={cooldown} trend={trend_ema} → pnl={pnl:.2f}")

    df_out = pd.DataFrame(results).sort_values("pnl", ascending=False)
    df_out.to_csv("runs/grid_search_results.csv", index=False)
    print("\n✅ 搜索结束 → 结果已保存：runs/grid_search_results.csv")
    print(df_out.head(20))

if __name__ == "__main__":
    main()
