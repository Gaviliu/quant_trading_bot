import argparse, json, os
from typing import Any
import numpy as np
import pandas as pd
import optuna

# 复用你的回测器（确保可被导入）
from run_backtest import Backtester


# ---------- 工具函数 ----------
def equity_curve_from_trades(trades, equity0=10_000.0):
    eq = [equity0]
    cur = equity0
    for t in trades:
        cur += float(t["pnl"])
        eq.append(cur)
    return np.asarray(eq, dtype=float)

def max_drawdown(equity_curve: np.ndarray) -> float:
    if equity_curve.size < 2:
        return 0.0
    peaks = np.maximum.accumulate(equity_curve)
    dd = equity_curve - peaks
    return float(-dd.min())

def json_default(o: Any):
    """把常见的 numpy / pandas 标量转换为可 JSON 序列化的类型。"""
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (pd.Timestamp,)):
        return o.isoformat()
    if isinstance(o, (pd.Timedelta,)):
        return o.total_seconds()
    # 最后兜底：转字符串（避免 DataFrame/Series 泄入）
    return str(o)

def split_walk_forward(df, train_days=20, valid_days=10):
    """简单的 walk-forward: 20 天训练 + 10 天验证（训练段这里不直接用，只做切分基准）"""
    df = df.copy()
    df["t"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert("UTC")
    first = df["t"].min()
    cut1  = first + pd.Timedelta(days=train_days)
    cut2  = cut1 + pd.Timedelta(days=valid_days)
    train = df[(df["t"] >= first) & (df["t"] < cut1)]
    valid = df[(df["t"] >= cut1) & (df["t"] < cut2)]
    return train, valid

def run_one(df, params, fee_bps=6.0, tp_pct=0.02, sl_pct=0.01, size=10.0):
    bt = Backtester(
        df,
        fast=params["fast"],
        slow=params["slow"],
        confirm=params["confirm"],
        band_bps=params["band_bps"],
        fee_bps=fee_bps,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        fixed_size_in_coin=size,
        # Backtester 内部已有 fast_mode 默认 True（或你在构造时传入）
    )
    bt.run()

    trades = bt.trades  # list[dict]: {time, side, entry, exit, pnl}
    total_pnl = float(sum(float(t["pnl"]) for t in trades))
    n_trades  = len(trades)

    # 估算频率（/小时）
    hours = max((df["ts"].iloc[-1] - df["ts"].iloc[0]) / 1000 / 3600, 1e-9)
    freq = n_trades / hours

    # 计算“类夏普”（基于每日 pnl）
    if n_trades > 0:
        tdf = pd.DataFrame(trades).copy()
        # 直接以交易发生日聚合日 pnl
        tdf["date"] = pd.to_datetime(tdf["time"]).dt.date
        daily_df = tdf.groupby("date", as_index=False)["pnl"].sum()
        if len(daily_df) >= 2 and daily_df["pnl"].std(ddof=1) > 0:
            sharpe_like = (daily_df["pnl"].mean() / daily_df["pnl"].std(ddof=1)) * (365 ** 0.5)
        else:
            sharpe_like = 0.0
    else:
        sharpe_like = 0.0

    # 最大回撤（基于逐笔权益曲线）
    eq = equity_curve_from_trades(trades)
    mdd = max_drawdown(eq)

    return {
        "trades": n_trades,
        "freq_per_hour": float(freq),
        "total_pnl": float(total_pnl),
        "sharpe_like": float(sharpe_like),
        "max_drawdown": float(mdd),
    }


# ---------- Optuna 目标 ----------
def objective(trial, df, target_min=0.5, target_max=1.0, prune_on_freq=False,
              fee_bps=6.0, size=10.0):
    # 搜索空间（5m K 线较合理区间，可按需调整）
    fast  = trial.suggest_int("fast", 8, 26)
    slow  = trial.suggest_int("slow", 34, 120)
    if fast >= slow:
        raise optuna.TrialPruned()  # 明显不合格

    confirm  = trial.suggest_int("confirm", 1, 3)
    band_bps = trial.suggest_float("band_bps", 2.0, 12.0)
    tp_pct   = trial.suggest_float("tp_pct", 0.005, 0.03)   # 0.5%~3%
    sl_pct   = trial.suggest_float("sl_pct", 0.005, 0.02)   # 0.5%~2%

    params = dict(fast=fast, slow=slow, confirm=confirm, band_bps=band_bps)

    # Walk-forward 仅用验证段评分
    _, valid_df = split_walk_forward(df, train_days=20, valid_days=10)
    if len(valid_df) < 100:
        raise optuna.TrialPruned()

    metrics = run_one(valid_df, params, fee_bps=fee_bps, tp_pct=tp_pct, sl_pct=sl_pct, size=size)

    # 频率约束：默认不剪枝，施加惩罚；可通过 --prune-on-freq 开启剪枝
    freq = metrics["freq_per_hour"]
    if prune_on_freq and (freq < target_min or freq > target_max):
        raise optuna.TrialPruned()
    # 惩罚项（距离窗口越远罚越重）
    penalty = 0.0
    if not prune_on_freq:
        if freq < target_min:
            penalty = (target_min - freq) * 2000.0
        elif freq > target_max:
            penalty = (freq - target_max) * 2000.0

    # 目标：最大化 profit - 0.5*MDD + 0.1*Sharpe_like - penalty
    score = metrics["total_pnl"] - 0.5 * metrics["max_drawdown"] + 0.1 * metrics["sharpe_like"] - penalty

    # 仅存入**可序列化**的简单类型
    trial.set_user_attr("params", {**params, "tp_pct": float(tp_pct), "sl_pct": float(sl_pct)})
    trial.set_user_attr("metrics", {k: float(v) for k, v in metrics.items()})
    trial.set_user_attr("score", float(score))
    return float(score)


# ---------- 主函数 ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/SOLUSDT_5m.csv")
    ap.add_argument("--n-trials", type=int, default=200)
    ap.add_argument("--target-min", type=float, default=0.5, help="目标最小频率（次/小时）")
    ap.add_argument("--target-max", type=float, default=1.0, help="目标最大频率（次/小时）")
    ap.add_argument("--fee-bps",  type=float, default=6.0, help="手续费 bps（1bps=0.01%）")
    ap.add_argument("--tp-pct",   type=float, default=0.02, help="止盈百分比，比如 0.02=2%")
    ap.add_argument("--sl-pct",   type=float, default=0.01, help="止损百分比")
    ap.add_argument("--size",     type=float, default=10.0, help="每次固定开仓币数")
    ap.add_argument("--n-jobs",   type=int, default=1, help="并行线程数（Optuna 线程并行）")
    ap.add_argument("--prune-on-freq", action="store_true", help="频率不合格时直接剪枝（默认使用惩罚而非剪枝）")
    args = ap.parse_args()

    df = pd.read_csv(args.csv, dtype={"ts":"int64","open":"float64","high":"float64","low":"float64","close":"float64","volume":"float64"})
    if "ts" not in df.columns:
        raise SystemExit("CSV 缺少 ts 列（毫秒时间戳）")

    # 固定采样器种子，便于复现实验
    sampler = optuna.samplers.TPESampler(seed=42)
    study = optuna.create_study(direction="maximize", sampler=sampler)

    study.optimize(
        lambda tr: objective(
            tr, df,
            target_min=args.target_min,
            target_max=args.target_max,
            prune_on_freq=args.prune_on_freq,
            fee_bps=args.fee_bps,
            size=args.size
        ),
        n_trials=args.n_trials,
        n_jobs=args.n_jobs,
        show_progress_bar=True
    )

    # 收集 COMPLETE 的 trial
    rows = []
    for t in study.trials:
        if t.state.name != "COMPLETE":
            continue
        attrs = t.user_attrs
        p = dict(attrs.get("params", {}))
        m = dict(attrs.get("metrics", {}))
        rows.append({
            **p,
            **m,
            "score": attrs.get("score", None),
            "trial": t.number,
        })

    os.makedirs("runs", exist_ok=True)
    out_csv = "runs/tuning_results_b.csv"

    if len(rows) == 0:
        # 写空表头，避免 sort 抛错
        pd.DataFrame(columns=["fast","slow","confirm","band_bps","tp_pct","sl_pct",
                              "trades","freq_per_hour","total_pnl","sharpe_like","max_drawdown",
                              "score","trial"]).to_csv(out_csv, index=False)
        print("\n[WARN] 本次优化没有产生任何 COMPLETE 的 Trial。可能所有解都被剪枝。")
        print("      建议：降低约束（放宽 target_min/max），或不加 --prune-on-freq，或扩大搜索空间/样本期。")
        print(f"      已输出空结果表头 -> {out_csv}")
        return

    pd.DataFrame(rows).sort_values("score", ascending=False).to_csv(out_csv, index=False)

    # 最优 Trial（一定存在，因为 rows 非空）
    best_trial = max((t for t in study.trials if t.state.name == "COMPLETE"),
                     key=lambda t: float(t.user_attrs.get("score", float("-inf"))))
    best_attrs = best_trial.user_attrs

    best_json = "runs/best_params.json"
    with open(best_json, "w", encoding="utf-8") as f:
        json.dump(best_attrs, f, ensure_ascii=False, indent=2, default=json_default)

    print("\n=== BEST (B 版) ===")
    print(json.dumps(best_attrs, ensure_ascii=False, indent=2, default=json_default))
    print(f"\nsaved results -> {out_csv}")
    print(f"best params   -> {best_json}")


if __name__ == "__main__":
    main()
