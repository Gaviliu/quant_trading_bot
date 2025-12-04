# -*- coding: utf-8 -*-
from __future__ import annotations
from dataclasses import dataclass
from collections import deque
from typing import Optional, Deque, Tuple

@dataclass
class _EMA:
    period: int
    k: float
    value: Optional[float] = None

    @classmethod
    def create(cls, period: int) -> "_EMA":
        assert period > 0
        return cls(period=period, k=2.0 / (period + 1.0), value=None)

    def update(self, x: float) -> float:
        self.value = x if self.value is None else (x - self.value) * self.k + self.value
        return self.value

class EMACrossover:
    """
    流式 EMA 穿越策略（与 runner 对齐）
    - on_close_fast_adapt(close, high, low) -> 1/-1/0
    - set_position_side(pos) : 通知当前持仓(1/-1/0)，用于抑制同向重复信号
    过滤项：
      * confirm_bars 连续K确认
      * band_mode: "bps" -> gap_pct >= band_bps/10000
                   "atr_pct" -> gap_pct >= atr_k * (ATR/price)
      * trend_ema: 多头仅在 price>=EMA_trend；空头仅在 price<=EMA_trend
      * cooldown_bars: 触发后冷却N根
    """

    def __init__(
        self,
        fast: int = 9,
        slow: int = 26,
        confirm_bars: int = 1,
        band_mode: str = "bps",
        band_bps: float = 50.0,
        atr_k: float = 0.6,
        trend_ema: int = 200,
        cooldown_bars: int = 0,
        mute_same_dir_when_holding: bool = True,
        print_ema_each_bar: bool = False,
        atr_period: int = 14,
    ):
        assert fast > 0 and slow > 0 and fast < slow, "fast/slow 参数不合法"
        assert band_mode in ("bps", "atr_pct"), "band_mode 仅支持 bps/atr_pct"

        self.fast_ema = _EMA.create(fast)
        self.slow_ema = _EMA.create(slow)
        self.trend_ema = _EMA.create(trend_ema) if trend_ema and trend_ema > 0 else None

        # ATR(Wilder) 计算
        self.atr_period = max(1, int(atr_period))
        self._atr: Optional[float] = None
        self._prev_close: Optional[float] = None
        self._tr_hist: Deque[float] = deque(maxlen=self.atr_period)

        # 参数
        self.confirm_bars = max(1, int(confirm_bars))
        self.band_mode = band_mode
        self.band_bps = float(band_bps)
        self.atr_k = float(atr_k)
        self.cooldown_bars = max(0, int(cooldown_bars))
        self.mute_same_dir_when_holding = bool(mute_same_dir_when_holding)
        self.print_ema_each_bar = bool(print_ema_each_bar)

        # 状态
        self._cross_dir: int = 0        # 最近一根K的穿越方向：1,-1,0
        self._streak: int = 0           # 连续满足方向的计数
        self._cooldown_left: int = 0
        self._holding_side: int = 0     # 由 set_position_side(pos) 注入
        self._last_signal: int = 0      # 上一次发出的信号(1/-1/0)

    # ============== 工具 ==============
    @staticmethod
    def _gap_pct(fast: float, slow: float, px: float) -> float:
        return abs(fast - slow) / max(1e-12, px)

    def _band_ok(self, gap_pct: float, px: float) -> bool:
        if self.band_mode == "bps":
            need = self.band_bps / 10000.0
            return gap_pct >= need
        else:  # atr_pct
            if self._atr is None:
                return False
            need = self.atr_k * (self._atr / max(1e-12, px))
            return gap_pct >= need

    def _trend_ok(self, px: float, dir_: int) -> bool:
        if self.trend_ema is None or self.trend_ema.value is None:
            return True
        if dir_ > 0:
            return px >= self.trend_ema.value
        if dir_ < 0:
            return px <= self.trend_ema.value
        return False

    def _update_atr(self, high: float, low: float, close: float) -> None:
        # True Range
        if self._prev_close is None:
            tr = float(high - low)
        else:
            tr = max(
                float(high - low),
                abs(float(high - self._prev_close)),
                abs(float(low - self._prev_close)),
            )
        self._prev_close = float(close)

        if len(self._tr_hist) < self.atr_period:
            self._tr_hist.append(tr)
            self._atr = (sum(self._tr_hist) / len(self._tr_hist)) if self._tr_hist else tr
        else:
            # Wilder EMA: ATR_t = ATR_{t-1}*(n-1)/n + TR_t/n
            self._atr = (self._atr * (self.atr_period - 1) + tr) / self.atr_period  # type: ignore

    # ============== 对外接口 ==============
    def set_position_side(self, pos: int) -> None:
        """1/-1/0"""
        self._holding_side = 1 if pos > 0 else (-1 if pos < 0 else 0)

    def on_close_fast_adapt(self, close: float, high: float, low: float) -> int:
        """
        流式更新一根K线后在“收盘”时调用。
        返回：1=做多，-1=做空，0=无动作
        """
        px = float(close)
        f = self.fast_ema.update(px)
        s = self.slow_ema.update(px)
        if self.trend_ema is not None:
            self.trend_ema.update(px)
        self._update_atr(float(high), float(low), px)

        if self.print_ema_each_bar:
            print(f"[EMA] fast={f:.6f} slow={s:.6f} trend={self.trend_ema.value if self.trend_ema else None}")

        # 初期EMA尚未就绪
        if self.fast_ema.value is None or self.slow_ema.value is None:
            return 0

        # 方向：fast 相对 slow
        dir_now = 1 if f > s else (-1 if f < s else 0)

        # 冷却计时
        if self._cooldown_left > 0:
            self._cooldown_left -= 1

        # 连续确认
        if dir_now == 0:
            self._streak = 0
            self._cross_dir = 0
            return 0
        else:
            if dir_now == self._cross_dir:
                self._streak += 1
            else:
                self._cross_dir = dir_now
                self._streak = 1

        # 达到确认根数?
        if self._streak < self.confirm_bars:
            return 0

        # 带宽过滤
        gap = self._gap_pct(f, s, px)
        if not self._band_ok(gap, px):
            return 0

        # 趋势过滤
        if not self._trend_ok(px, dir_now):
            return 0

        # 冷却期内不发新信号
        if self._cooldown_left > 0:
            return 0

        # 如果正在持有同向仓位，且要求静音，则不重复发信号
        if self.mute_same_dir_when_holding and dir_now == self._holding_side and self._holding_side != 0:
            return 0

        # 通过所有条件 -> 触发信号
        self._last_signal = dir_now
        if self.cooldown_bars > 0:
            self._cooldown_left = self.cooldown_bars
        return dir_now
