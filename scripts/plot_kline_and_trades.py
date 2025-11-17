# -*- coding: utf-8 -*-
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import os

# =========================
# 配置区（按需改这里）
# =========================
USE_OVERVIEW = True  # 是否生成全周期两张概览图
ONLY_DAYS_WITH_TRADES = True  # True: 仅当该日有交易时才导出；False: 无论是否有交易都导出
MIN_TRADES_PER_DAY = 1        # 当 ONLY_DAYS_WITH_TRADES=True 生效，至少多少笔交易才导出

# 选择日期的两种方式（二选一即可）
SELECT_DATES = [
    # 示例：按天明确指定（UTC自然日）
    # "2025-05-15",
    "2025-05-16",
]
DATE_RANGE = (
    # 或者用区间（包含两端），留空表示不用区间
    # "2025-05-15", "2025-05-31"
    None, None
)

# =========================
# 路径（固定，不用命令行）
# =========================
def _project_root():
    # scripts/ 的上一级就是项目根
    return os.path.dirname(os.path.dirname(__file__))

BASE = _project_root()
KLINE_CSV  = os.path.join(BASE, "data", "SOLUSDT_5m.csv")
TRADES_CSV = os.path.join(BASE, "runs", "trades.csv")
OUT_PREFIX = os.path.join(BASE, "data", "SOLUSDT_plot")           # 概览图输出前缀
DAILY_DIR  = os.path.join(BASE, "data", "daily_plots_SOLUSDT_5m") # 每日图输出目录

# =========================
# 工具 & 加载
# =========================
def _ensure_datetime(series):
    s = pd.to_datetime(series, utc=True, errors="coerce")
    if s.isna().all():
        try:
            s = pd.to_datetime(series.astype(np.int64), unit="ms", utc=True, errors="coerce")
        except Exception:
            pass
    return s

def load_kline(csv_path):
    """
    读取K线CSV，索引为UTC DatetimeIndex。
    支持列：ts_iso 或 ts（毫秒），并标准化 open/high/low/close/volume 列名。
    """
    df = pd.read_csv(csv_path)
    if "ts_iso" in df.columns:
        idx = _ensure_datetime(df["ts_iso"])
    elif "ts" in df.columns:
        idx = pd.to_datetime(df["ts"], unit="ms", utc=True)
    else:
        idx = _ensure_datetime(df.iloc[:, 0])
    df.index = idx
    df = df.sort_index()
    cols = {c.lower(): c for c in df.columns}
    rename = {}
    for k in ["open", "high", "low", "close", "volume"]:
        if k in cols:
            rename[cols[k]] = k
    df = df.rename(columns=rename)
    return df[["open", "high", "low", "close", "volume"]]

def load_trades(csv_path):
    """
    读取交易CSV，索引为UTC DatetimeIndex。
    支持时间列：time/timestamp/ts/datetime/date_time/filled_at
    统一 side 为 {-1,0,1}，缺失的 entry/exit/pnl/reason 自动补列。
    """
    t = pd.read_csv(csv_path)
    time_col = None
    for c in ["time", "timestamp", "ts", "datetime", "date_time", "filled_at"]:
        if c in t.columns:
            time_col = c
            break
    if time_col is None:
        raise ValueError("No time-like column found in trades CSV.")
    t["dt"] = pd.to_datetime(t[time_col], utc=True, errors="coerce")
    if t["dt"].isna().any():
        try:
            t["dt"] = pd.to_datetime(t[time_col].astype("int64"), unit="ms", utc=True)
        except Exception:
            pass
    t = t.sort_values("dt").set_index("dt")

    if "side" in t.columns:
        def _norm_side(x):
            if isinstance(x, (int, float, np.integer)):
                return int(np.sign(x))
            s = str(x).lower()
            if "long" in s or s == "1" or s == "buy":
                return 1
            if "short" in s or s == "-1" or s == "sell":
                return -1
            return 0
        t["side"] = t["side"].apply(_norm_side).astype(int)
    else:
        t["side"] = 0

    for col in ["entry", "exit", "pnl"]:
        if col not in t.columns:
            t[col] = np.nan
    if "reason" not in t.columns:
        t["reason"] = ""
    return t[["side", "entry", "exit", "pnl", "reason"]]

# =========================
# 画图
# =========================
def _candles(ax, df):
    """纯 matplotlib 画蜡烛（不依赖第三方K线库）。"""
    x = np.arange(len(df))
    # 上下影线
    for i, (lo, hi) in enumerate(zip(df["low"].to_numpy(), df["high"].to_numpy())):
        ax.vlines(i, lo, hi, linewidth=1)
    # 实体
    width = 0.6
    for i, (o, c) in enumerate(zip(df["open"].to_numpy(), df["close"].to_numpy())):
        bottom = min(o, c)
        height = abs(c - o)
        ax.add_patch(plt.Rectangle((i - width / 2, bottom), width, max(height, 1e-12)))
    ax.set_xlim(-1, len(df))
    ax.set_ylabel("Price")

def plot_kline_with_trades(kline_df, trades_df, outfile=None, title="Kline with Trades",
                           annotate_max=200):
    """
    画K线，并标注：
      - 入场: 多(^) / 空(v)，文字 "多 开@xxx" / "空 开@xxx"
      - 出场: o，文字 "平@xxx PnL:yyy"
      - 入场到出场连线（按同一行 trade 视为一笔）
    支持可选列 'exit_time'（如无，则在入场时间处标注出场点与文字）。
    为避免 tz 问题，使用纳秒整数轴做索引对齐。
    """
    import numpy as np
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(14, 7))
    _candles(ax, kline_df)

    # --- 索引用纳秒整数对齐 ---
    k_ns = kline_df.index.view("int64")     # 单调递增
    k_times = kline_df.index                # 仅用于x轴刻度文本

    def nearest_x_by_ns(dt_ns: int) -> int:
        i = np.searchsorted(k_ns, dt_ns)
        if i <= 0:
            return 0
        if i >= len(k_ns):
            return len(k_ns) - 1
        before = k_ns[i - 1]
        after  = k_ns[i]
        return i - 1 if (dt_ns - before) <= (after - dt_ns) else i

    if not trades_df.empty:
        # 可选：单笔限制，避免字太密
        trades_use = trades_df.copy()
        if len(trades_use) > annotate_max:
            trades_use = trades_use.tail(annotate_max)  # 只保留最近 N 笔，按需改

        # 处理 exit_time（可选）
        if "exit_time" in trades_use.columns:
            t_exit = pd.to_datetime(trades_use["exit_time"], utc=True, errors="coerce")
            trades_use = trades_use.assign(_exit_dt=t_exit)
        else:
            trades_use = trades_use.assign(_exit_dt=trades_use.index)  # 无则复用入场时间

        # 转成纳秒整数
        entry_ns = trades_use.index.view("int64")
        exit_ns  = trades_use["_exit_dt"].view("int64")

        # 批量绘制：入场点/出场点/注释/连线
        for (dt_ns, dt_exit_ns), row in zip(zip(entry_ns, exit_ns), trades_use.itertuples()):
            side = int(getattr(row, "side", 0))
            entry = getattr(row, "entry", np.nan)
            exitp = getattr(row, "exit", np.nan)
            pnl = getattr(row, "pnl", np.nan)

            # 入/出 x 坐标
            x_entry = nearest_x_by_ns(dt_ns)
            x_exit  = nearest_x_by_ns(dt_exit_ns)

            # === 入场点 ===
            if not np.isnan(entry):
                if side >= 1:
                    mk = "^"   # 做多
                    txt = f"Long@{entry:.2f}"
                elif side <= -1:
                    mk = "v"   # 做空
                    txt = f"Short@{entry:.2f}"
                else:
                    mk = "^"
                    txt = f"Exit@{entry:.2f}"
                ax.scatter([x_entry], [entry], marker=mk, s=70)
                # 文字放在点上方/下方（多放上、空放下，减少遮挡）
                voff = 0.15 * (1 if side >= 0 else -1)
                ax.annotate(txt, (x_entry, entry), xytext=(0, 8 if side >= 0 else -10),
                            textcoords="offset points", ha="center", va="bottom" if side >= 0 else "top")

            # === 出场点 ===
            if not np.isnan(exitp):
                ax.scatter([x_exit], [exitp], marker="o", s=50)
                txt2 = f"平@{exitp:.2f}"
                if not np.isnan(pnl):
                    txt2 += f" PnL:{pnl:.2f}"
                ax.annotate(txt2, (x_exit, exitp), xytext=(0, 10),
                            textcoords="offset points", ha="center", va="bottom")

            # === 进出连线（只有 entry/exit 都有时才画）===
            if (not np.isnan(entry)) and (not np.isnan(exitp)):
                ax.plot([x_entry, x_exit], [entry, exitp], linestyle="--")

        # 简单图例（marker 说明）
        from matplotlib.lines import Line2D
        legend_elems = [
            Line2D([0], [0], marker="^", linestyle="None", label="Entry (Long)"),
            Line2D([0], [0], marker="v", linestyle="None", label="Entry (Short)"),
            Line2D([0], [0], marker="o", linestyle="None", label="Exit"),
            Line2D([0], [0], linestyle="--", label="Entry→Exit"),
        ]
        ax.legend(handles=legend_elems, loc="upper right")

    ax.set_title(title)
    ax.set_xlabel("Time")

    xticks = np.linspace(0, len(kline_df) - 1, num=10, dtype=int)
    ax.set_xticks(xticks)
    ax.set_xticklabels([k_times[i].strftime("%Y-%m-%d %H:%M") for i in xticks],
                       rotation=30, ha="right")

    fig.tight_layout()
    if outfile:
        os.makedirs(os.path.dirname(outfile), exist_ok=True)
        fig.savefig(outfile, dpi=150)
    return fig

# =========================
# 按天切分 & 按天导出
# =========================
def _day_slice(df, day: pd.Timestamp):
    """取某UTC自然日的数据 [day, day+1)。"""
    start = pd.Timestamp(day.date(), tz="UTC")
    end = start + pd.Timedelta(days=1)
    return df.loc[(df.index >= start) & (df.index < end)]

def _ensure_dates_to_plot(kline_df):
    """根据 SELECT_DATES / DATE_RANGE 生成要导出的日期列表（UTC自然日）"""
    dates = []
    if SELECT_DATES:
        dates = [pd.Timestamp(d).tz_localize("UTC") for d in SELECT_DATES]
    else:
        start, end = DATE_RANGE
        if start and end:
            rng = pd.date_range(pd.Timestamp(start).tz_localize("UTC"),
                                pd.Timestamp(end).tz_localize("UTC"),
                                freq="D")
            dates = list(rng)
    # 如果两者都没设，默认不导出任何日图；你也可以在此回退成“导出全覆盖”，视需要修改
    return dates

def plot_daily_for_dates(kline_df, trades_df, out_dir, dates,
                         only_days_with_trades=True,
                         min_trades_per_day=1,
                         title_prefix="SOLUSDT 5m"):
    os.makedirs(out_dir, exist_ok=True)
    saved = 0
    for d in dates:
        kday = _day_slice(kline_df, d)
        if kday.empty:
            continue
        tday = _day_slice(trades_df, d)
        if only_days_with_trades and (tday.empty or len(tday) < min_trades_per_day):
            continue
        title = f"{title_prefix} - {d.strftime('%Y-%m-%d')} (UTC)"
        outfile = os.path.join(out_dir, f"{d.strftime('%Y-%m-%d')}_kline_trades.png")
        plot_kline_with_trades(kday, tday, outfile=outfile, title=title)
        saved += 1
    print(f"✅ Daily charts saved: {saved} file(s) -> {out_dir}")

def plot_equity_curve(trades_df, outfile=None, title="Equity Curve"):
    """累计PnL曲线。"""
    if trades_df.empty:
        raise ValueError("Trades DataFrame is empty; cannot plot equity.")
    eq = trades_df["pnl"].fillna(0).cumsum()
    fig, ax = plt.subplots(figsize=(14, 4))
    ax.plot(eq.index, eq.values)
    ax.set_title(title)
    ax.set_xlabel("Time")
    ax.set_ylabel("PnL (cum)")
    fig.tight_layout()
    if outfile:
        os.makedirs(os.path.dirname(outfile), exist_ok=True)
        fig.savefig(outfile, dpi=150)
    return fig

# =========================
# 主流程
# =========================
def main():
    # 加载数据
    kdf = load_kline(KLINE_CSV)     # tz-aware UTC
    tdf = load_trades(TRADES_CSV)   # tz-aware UTC

    # 概览图（可关）
    if USE_OVERVIEW:
        plot_kline_with_trades(
            kdf, tdf,
            outfile=f"{OUT_PREFIX}_kline_trades.png",
            title="SOLUSDT 5m - Kline with Trades (All)"
        )
        plot_equity_curve(
            tdf,
            outfile=f"{OUT_PREFIX}_equity.png",
            title="SOLUSDT 5m - Equity Curve (All)"
        )
        print(f"✅ Overview charts saved -> {os.path.dirname(OUT_PREFIX)}")

    # 仅导出你指定的日期（不会一次性生成 180 张）
    dates = _ensure_dates_to_plot(kdf)
    if dates:
        plot_daily_for_dates(
            kdf, tdf,
            out_dir=DAILY_DIR,
            dates=dates,
            only_days_with_trades=ONLY_DAYS_WITH_TRADES,
            min_trades_per_day=MIN_TRADES_PER_DAY,
            title_prefix="SOLUSDT 5m"
        )
    else:
        print("ℹ️ 未设置 SELECT_DATES 或有效的 DATE_RANGE；未导出任何日图。")

if __name__ == "__main__":
    main()
