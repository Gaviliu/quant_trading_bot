# -*- coding: utf-8 -*-
"""
Grid search for EMA crossover backtester (FULL REPLACEMENT, faster + robust).

Run from repo root:
    python scripts/sweep_params.py

Outputs:
    runs/sweep_YYYYMMDD_HHMMSS.csv  (all results)
    prints Top 15 by total_pnl

Improvements vs previous:
- Fixed print() syntax bug.
- Avoids passing None tp/sl (fixes TypeError).
- Robust import of Backtester.
- Parallel evaluation with ProcessPoolExecutor (set N_JOBS below).
- Periodic flushing to CSV (SAVE_EVERY) + graceful KeyboardInterrupt save.
- Smaller default grid; edit GRIDS section to widen.
- Optional random subsample MAX_CONFIGS to cap total runs.
- Optional filters: MIN_TRADES, FREQ_RANGE, MDD_LIMIT, PF_MIN.
- Optional time-slice by START_TS_MS / END_TS_MS (epoch ms).
"""
from __future__ import annotations
import os
import sys
import math
import time
from dataclasses import asdict, dataclass
from typing import Optional, Tuple, List
import itertools as it
import random
from concurrent.futures import ProcessPoolExecutor, as_completed

import pandas as pd

# --- robust import Backtester ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
try:
    from scripts.run_backtest import Backtester  # preferred path
except ModuleNotFoundError:  # fallback when invoked differently
    from run_backtest import Backtester

# =========================== GLOBALS (EDIT HERE) ===========================
CSV_PATH = "data/SOLUSDT_5m.csv"

# Time range (epoch ms); None = full file
START_TS_MS: Optional[int] = None
END_TS_MS:   Optional[int] = None

# Filters (set to None to disable)
MIN_TRADES: Optional[int] = 150                     # drop very sparse configs
FREQ_RANGE: Optional[Tuple[float, float]] = None    # e.g., (0.5, 2.0) trades/hour
MDD_LIMIT:  Optional[float] = None                  # keep mdd >= -0.30 (<=30% dd)
PF_MIN:     Optional[float] = None                  # profit factor >= PF_MIN

# Parallelism & IO
N_JOBS: int = max(1, (os.cpu_count() or 2) - 1)     # processes
SAVE_EVERY: int = 200                                # flush every N results
MAX_CONFIGS: Optional[int] = None                    # e.g., 2000 for random cap
RANDOM_SEED: int = 42

# Fixed defaults (can also be swept in grids below if you want)
FEE_BPS_DEFAULT = 0.0
SIZE_DEFAULT    = 10.0

# ============================= GRIDS (EDIT HERE) ===========================
# Keep defaults modest; widen once pipeline is stable.
FAST_LIST      = [9, 21]            # was [9,12,21]
SLOW_LIST      = [26, 55]           # was [26,34,55]
CONFIRM_LIST   = [1, 2]             # was [1,2,3]

BAND_MODES     = ["atr_pct", "bps"]
ATR_K_LIST     = [0.3, 0.6, 1.0]    # was [0.2,0.4,0.6,0.8,1.0]
BAND_BPS_LIST  = [50, 120]          # was [30,50,80,120,160]

TP_LIST        = [0.015, 0.02]      # was [0.01,0.015,0.02,0.03]
SL_LIST        = [0.01, 0.015]      # was [0.005,0.01,0.015,0.02]

TREND_EMA_LIST = [0, 200]
COOLDOWN_LIST  = [0, 1]

FEE_BPS_LIST   = [FEE_BPS_DEFAULT]
SIZE_LIST      = [SIZE_DEFAULT]
# ===========================================================================

@dataclass(frozen=True)
class Config:
    fast: int
    slow: int
    confirm: int
    band_mode: str
    atr_k: float
    band_bps: float
    tp_pct: float
    sl_pct: float
    trend_ema: int
    cooldown: int
    fee_bps: float
    size: float


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    roll_max = equity.cummax()
    dd = equity / roll_max - 1.0
    return float(dd.min())


def _profit_factor(pnl: pd.Series) -> float:
    if pnl.empty:
        return 0.0
    gains = pnl[pnl > 0].sum()
    losses = -pnl[pnl < 0].sum()
    if losses == 0:
        return float("inf") if gains > 0 else 0.0
    return float(gains / losses)


def _daily_sharpe(trades_df: pd.DataFrame) -> float:
    if trades_df.empty:
        return 0.0
    d = trades_df.copy()
    d["date"] = d["time"].dt.date
    daily = d.groupby("date", as_index=False)["pnl"].sum()
    if daily.empty:
        return 0.0
    mu = daily["pnl"].mean()
    sd = daily["pnl"].std(ddof=1)
    if not sd or math.isnan(sd) or sd <= 0:
        return 0.0
    return float(mu / sd * math.sqrt(365.0))


def _slice_df(df: pd.DataFrame, start_ms: Optional[int], end_ms: Optional[int]) -> pd.DataFrame:
    if start_ms is None and end_ms is None:
        return df
    m = pd.Series([True] * len(df))
    if start_ms is not None:
        m &= df["ts"] >= start_ms
    if end_ms is not None:
        m &= df["ts"] <= end_ms
    return df[m].reset_index(drop=True)


def _eval_one(df: pd.DataFrame, cfg: Config) -> dict:
    bt = Backtester(
        df,
        cfg.fast,
        cfg.slow,
        cfg.confirm,
        cfg.band_bps,
        band_mode=cfg.band_mode,
        atr_k=cfg.atr_k,
        trend_ema=cfg.trend_ema,
        cooldown_bars=cfg.cooldown,
        fee_bps=cfg.fee_bps,
        tp_pct=cfg.tp_pct,
        sl_pct=cfg.sl_pct,
        fixed_size_in_coin=cfg.size,
        fast_mode=True,
    )
    bt.run()

    trades_df = pd.DataFrame(bt.trades)
    total_pnl = float(trades_df["pnl"].sum()) if not trades_df.empty else 0.0
    n_trades  = int(len(trades_df))
    hours     = (df["ts"].iloc[-1] - df["ts"].iloc[0]) / 1000 / 3600
    freq_per_hour = n_trades / max(hours, 1e-9)

    equity = [bt.equity0]
    for v in trades_df["pnl"].tolist():
        equity.append(equity[-1] + v)
    equity_s = pd.Series(equity)

    res = {
        **asdict(cfg),
        "trades": n_trades,
        "freq_per_hour": freq_per_hour,
        "total_pnl": total_pnl,
        "avg_pnl": float(trades_df["pnl"].mean()) if n_trades else 0.0,
        "win_rate": float((trades_df["pnl"] > 0).mean()) if n_trades else 0.0,
        "profit_factor": _profit_factor(trades_df["pnl"]) if n_trades else 0.0,
        "mdd": _max_drawdown(equity_s),
        "sharpe": _daily_sharpe(trades_df),
    }
    return res


def _build_configs() -> List[Config]:
    cfgs: List[Config] = []
    for fast in FAST_LIST:
        for slow in SLOW_LIST:
            if not (fast > 0 and slow > 0 and fast < slow):
                continue
            for confirm in CONFIRM_LIST:
                for trend in TREND_EMA_LIST:
                    for cd in COOLDOWN_LIST:
                        for fee in FEE_BPS_LIST:
                            for size in SIZE_LIST:
                                # ATR mode
                                for k in ATR_K_LIST:
                                    for tp in TP_LIST:
                                        for sl in SL_LIST:
                                            cfgs.append(Config(fast, slow, confirm, "atr_pct", k, 0.0, tp, sl, trend, cd, fee, size))
                                # BPS mode
                                for bps in BAND_BPS_LIST:
                                    for tp in TP_LIST:
                                        for sl in SL_LIST:
                                            cfgs.append(Config(fast, slow, confirm, "bps", 0.0, bps, tp, sl, trend, cd, fee, size))
    # Optional random cap
    if MAX_CONFIGS is not None and len(cfgs) > MAX_CONFIGS:
        random.seed(RANDOM_SEED)
        cfgs = random.sample(cfgs, MAX_CONFIGS)
    return cfgs


def main() -> None:
    os.makedirs("runs", exist_ok=True)

    df = pd.read_csv(
        CSV_PATH,
        dtype={
            "ts": "int64",
            "open": "float64",
            "high": "float64",
            "low": "float64",
            "close": "float64",
            "volume": "float64",
        },
    )
    df = _slice_df(df, START_TS_MS, END_TS_MS)
    if df.empty:
        raise SystemExit("[sweep] Empty dataframe after slicing; adjust START_TS_MS/END_TS_MS or CSV_PATH.")

    cfgs = _build_configs()
    print(f"Total configs to evaluate: {len(cfgs)} | parallel jobs = {N_JOBS}")

    out_path = os.path.join("runs", f"sweep_{time.strftime('%Y%m%d_%H%M%S')}.csv")
    cols = [
        "fast","slow","confirm","band_mode","atr_k","band_bps","tp_pct","sl_pct","trend_ema","cooldown","fee_bps","size",
        "trades","freq_per_hour","total_pnl","avg_pnl","win_rate","profit_factor","mdd","sharpe"
    ]
    pd.DataFrame(columns=cols).to_csv(out_path, index=False)

    results: List[dict] = []
    started = time.time()
    done = 0

    try:
        with ProcessPoolExecutor(max_workers=N_JOBS) as ex:
            futures = {ex.submit(_eval_one, df, cfg): cfg for cfg in cfgs}
            for fut in as_completed(futures):
                rec = fut.result()
                results.append(rec)
                done += 1
                if done % 50 == 0:
                    elapsed = time.time() - started
                    print(f"progress: {done}/{len(cfgs)} evaluated | elapsed {elapsed:.1f}s")
                if done % SAVE_EVERY == 0:
                    pd.DataFrame(results).to_csv(out_path, mode='a', header=False, index=False)
                    results.clear()
    finally:
        if results:
            pd.DataFrame(results).to_csv(out_path, mode='a', header=False, index=False)

    # Load all results for ranking
    res_df = pd.read_csv(out_path)

    # Optional filters
    if MIN_TRADES is not None:
        res_df = res_df[res_df["trades"] >= MIN_TRADES]
    if FREQ_RANGE is not None:
        lo, hi = FREQ_RANGE
        res_df = res_df[(res_df["freq_per_hour"] >= lo) & (res_df["freq_per_hour"] <= hi)]
    if MDD_LIMIT is not None:
        res_df = res_df[res_df["mdd"] >= MDD_LIMIT]
    if PF_MIN is not None:
        res_df = res_df[res_df["profit_factor"] >= PF_MIN]

    res_df = res_df.sort_values(["total_pnl", "sharpe"], ascending=[False, False]).reset_index(drop=True)

    res_df.to_csv(out_path, index=False)

    took = time.time() - started
    print(f"Done. {len(res_df)} configs evaluated in {took:.1f}s")
    print(f"Saved -> {out_path}")
    print("Top 15 by total_pnl:")
    print(res_df.head(15).to_string(index=False))


if __name__ == "__main__":
    main()
