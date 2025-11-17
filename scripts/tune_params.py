import argparse, itertools, pandas as pd, numpy as np
from run_backtest import Backtester  # 复用上面的类

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/SOLUSDT_5m.csv")
    ap.add_argument("--fee-bps", type=float, default=6.0)
    ap.add_argument("--tp-pct", type=float, default=0.02)
    ap.add_argument("--sl-pct", type=float, default=0.01)
    ap.add_argument("--size", type=float, default=10.0)
    # 搜索空间（可根据经验缩放）
    ap.add_argument("--fast",  nargs="+", type=int,   default=[10, 14, 21])
    ap.add_argument("--slow",  nargs="+", type=int,   default=[34, 55, 89])
    ap.add_argument("--confirm", nargs="+", type=int, default=[1, 2, 3])
    ap.add_argument("--band-bps", nargs="+", type=float, default=[2.5, 5, 7.5, 10])
    ap.add_argument("--target-min", type=float, default=0.5)  # 次/小时
    ap.add_argument("--target-max", type=float, default=1.0)
    args = ap.parse_args()

    df = pd.read_csv(args.csv)
    if "ts_iso" not in df.columns:
        df["ts_iso"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert("UTC")

    results = []
    hours = (df["ts"].iloc[-1] - df["ts"].iloc[0]) / 1000 / 3600

    for f, s, c, b in itertools.product(args.fast, args.slow, args.confirm, args["band_bps" if False else "band_bps"]):
        if f >= s:   # EMA必须快<慢
            continue
        bt = Backtester(df, f, s, c, b, fee_bps=args.fee_bps, tp_pct=args.tp_pct,
                        sl_pct=args.sl_pct, fixed_size_in_coin=args.size)
        bt.run()
        n = len(bt.trades)
        freq = n / max(hours, 1e-9)
        total_pnl = sum(t["pnl"] for t in bt.trades)
        # 粗略夏普：日收益/日波动
        daily = pd.DataFrame(bt.daily)
        sharpe = 0.0
        if not daily.empty and daily["pnl"].std(ddof=1) > 0:
            sharpe = (daily["pnl"].mean() / daily["pnl"].std(ddof=1)) * (365**0.5)

        results.append({"fast": f, "slow": s, "confirm": c, "band_bps": b,
                        "trades": n, "freq_per_hour": freq,
                        "total_pnl": total_pnl, "sharpe_like": sharpe})

    res = pd.DataFrame(results)
    # 过滤目标频率
    res = res[(res["freq_per_hour"] >= args.target_min) & (res["freq_per_hour"] <= args.target_max)]
    res = res.sort_values(["total_pnl","sharpe_like"], ascending=False)
    out = "runs/tuning_results.csv"
    res.to_csv(out, index=False)
    print(f"saved -> {out}")
    print(res.head(10).to_string(index=False))

if __name__ == "__main__":
    main()
