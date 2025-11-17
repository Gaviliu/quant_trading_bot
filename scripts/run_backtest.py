# -*- coding: utf-8 -*-
from __future__ import annotations
import argparse, os
import pandas as pd, numpy as np
from strategies.base import Bar
from strategies.ema_crossover import EMACrossover

class Position:
    def __init__(self):
        self.side: int = 0
        self.entry: float | None = None
        self.size: float = 0.0

class Backtester:
    def __init__(
        self, df: pd.DataFrame, fast: int, slow: int, confirm: int, band_bps: float,
        *, band_mode: str = "bps", atr_k: float = 0.6,
        trend_ema: int = 200, cooldown_bars: int = 0,
        fee_bps: float = 0.0, tp_pct: float = 0.02, sl_pct: float = 0.01,
        start_balance: float = 10_000.0, fixed_size_in_coin: float = 10.0,
        fast_mode: bool = True,
    ):
        self.df = df; self.fast_mode = fast_mode
        self.st = EMACrossover(
            fast=fast, slow=slow, confirm_bars=confirm,
            band_mode=band_mode, band_bps=band_bps, atr_k=atr_k,
            trend_ema=trend_ema, cooldown_bars=cooldown_bars,
            mute_same_dir_when_holding=True, print_ema_each_bar=False,
        )
        self.fee = fee_bps/10000.0; self.tp = tp_pct; self.sl = sl_pct
        self.equity0 = start_balance; self.equity = start_balance
        self.pos = Position(); self.fixed_size = fixed_size_in_coin
        self.trades: list[dict] = []

    def _fee_cost(self, px: float, size: float) -> float:
        return px * abs(size) * self.fee

    def _enter(self, side: int, px: float, ts_ms: int, reason="signal"):
        if self.pos.side == side:       # 同向忽略
            return
        if self.pos.side != 0 and self.pos.side != side:   # 反向先平
            self._exit(px, ts_ms, "reverse")
        self.pos.side = side; self.pos.entry = px
        self.pos.size = self.fixed_size * (1 if side==1 else -1)
        self.equity -= self._fee_cost(px, self.pos.size)
        t = pd.to_datetime(ts_ms, unit="ms", utc=True).tz_convert("UTC")
        self.trades.append({"time": t, "side": "OPEN_LONG" if side==1 else "OPEN_SHORT",
                            "entry": px, "exit": px, "pnl": 0.0, "reason": reason})

    def _exit(self, px: float, ts_ms: int, reason="signal"):
        if self.pos.side == 0: return
        size = self.pos.size; entry = self.pos.entry if self.pos.entry is not None else px
        pnl = (px - entry) * size - self._fee_cost(px, size)
        self.equity += pnl
        t = pd.to_datetime(ts_ms, unit="ms", utc=True).tz_convert("UTC")
        self.trades.append({"time": t, "side": "LONG" if size>0 else "SHORT",
                            "entry": entry, "exit": px, "pnl": pnl, "reason": reason})
        self.pos = Position()

    def _dir_from_ret(self, ret) -> int:
        if isinstance(ret, int): return int(np.sign(ret))
        if ret is None: return 0
        side = getattr(ret, "side", None)
        return 1 if side=="buy" else (-1 if side=="sell" else 0)

    def run(self):
        if hasattr(self.st, "reset"): self.st.reset()
        ts = self.df["ts"].to_numpy("int64")
        high = self.df["high"].to_numpy("float64")
        low  = self.df["low"].to_numpy("float64")
        close= self.df["close"].to_numpy("float64")

        if self.fast_mode:
            for i in range(len(close)):
                if self.pos.side != 0:
                    if self.pos.side == 1:
                        sl = self.pos.entry*(1-self.sl); tp = self.pos.entry*(1+self.tp)
                        if low[i] <= sl: self._exit(sl, ts[i], "SL")
                        elif high[i] >= tp: self._exit(tp, ts[i], "TP")
                    else:
                        sl = self.pos.entry*(1+self.sl); tp = self.pos.entry*(1-self.tp)
                        if high[i] >= sl: self._exit(sl, ts[i], "SL")
                        elif low[i] <= tp: self._exit(tp, ts[i], "TP")

                d = self._dir_from_ret(self.st.on_close_fast(close[i], self.pos.side))
                if d == 1: self._enter(1, close[i], ts[i])
                elif d == -1: self._enter(-1, close[i], ts[i])
        else:
            for _, r in self.df.iterrows():
                bar = Bar(ts=int(r.ts), open=float(r.open), high=float(r.high),
                          low=float(r.low), close=float(r.close), volume=float(r.volume))
                sig = self.st.on_bar(bar)
                if hasattr(self.st, "set_position_side"): self.st.set_position_side(self.pos.side)
                if self.pos.side != 0:
                    if self.pos.side == 1:
                        sl = self.pos.entry*(1-self.sl); tp = self.pos.entry*(1+self.tp)
                        if r.low <= sl: self._exit(sl, r.ts, "SL")
                        elif r.high >= tp: self._exit(tp, r.ts, "TP")
                    else:
                        sl = self.pos.entry*(1+self.sl); tp = self.pos.entry*(1-self.tp)
                        if r.high >= sl: self._exit(sl, r.ts, "SL")
                        elif r.low <= tp: self._exit(tp, r.ts, "TP")
                if sig is not None:
                    if getattr(sig, "side", None) == "buy": self._enter(1, r.close, r.ts)
                    elif getattr(sig, "side", None) == "sell": self._enter(-1, r.close, r.ts)

        if self.pos.side != 0: self._exit(close[-1], ts[-1], "end")

    def report(self, out_dir="runs"):
        os.makedirs(out_dir, exist_ok=True)
        trades_df = pd.DataFrame(self.trades)
        total_pnl = trades_df["pnl"].sum() if not trades_df.empty else 0.0
        n = len(trades_df)
        hours = (self.df["ts"].iloc[-1]-self.df["ts"].iloc[0])/1000/3600
        freq = n/max(hours,1e-9)
        if not trades_df.empty:
            trades_df["date"] = trades_df["time"].dt.date
            daily_df = trades_df.groupby("date", as_index=False)["pnl"].sum()
        else:
            daily_df = pd.DataFrame(columns=["date","pnl"])
        trades_df.to_csv(os.path.join(out_dir,"trades.csv"), index=False)
        daily_df.to_csv(os.path.join(out_dir,"daily_pnl.csv"), index=False)
        print("=== SUMMARY ===")
        print(f"trades: {n}   freq: {freq:.2f} / hour")
        print(f"equity0: {self.equity0:.2f}  equity: {self.equity:.2f}  PnL: {total_pnl:.2f}")
        print(f"daily pnl saved -> runs/daily_pnl.csv")
        print(f"trades saved    -> runs/trades.csv")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/SOLUSDT_5m.csv")
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--fast-ema", dest="fast_period", type=int, default=9)
    ap.add_argument("--slow-ema", dest="slow_period", type=int, default=26)
    ap.add_argument("--confirm", type=int, default=1)
    ap.add_argument("--band-mode", choices=["atr_pct","bps"], default="bps")
    ap.add_argument("--atr-k", type=float, default=0.6)
    ap.add_argument("--band-bps", type=float, default=50.0)
    ap.add_argument("--trend-ema", type=int, default=200)
    ap.add_argument("--cooldown", type=int, default=0)
    ap.add_argument("--fee-bps", type=float, default=0.0)
    ap.add_argument("--tp-pct", type=float, default=0.02)
    ap.add_argument("--sl-pct", type=float, default=0.01)
    ap.add_argument("--size", type=float, default=10.0)
    args = ap.parse_args()

    df = pd.read_csv(args.csv, dtype={"ts":"int64","open":"float64","high":"float64",
                                      "low":"float64","close":"float64","volume":"float64"})
    bt = Backtester(df, args.fast_period, args.slow_period, args.confirm, args.band_bps,
                    band_mode=args.band_mode, atr_k=args.atr_k, trend_ema=args.trend_ema,
                    cooldown_bars=args.cooldown, fee_bps=args.fee_bps, tp_pct=args.tp_pct,
                    sl_pct=args.sl_pct, fixed_size_in_coin=args.size,
                    fast_mode=(not args.debug))
    bt.run(); bt.report()

if __name__ == "__main__":
    main()
