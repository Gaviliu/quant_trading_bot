from .base import Bar, Signal

class EMACrossover:
    def __init__(self, **params):
        self.fast = int(params.get("fast", 21))
        self.slow = int(params.get("slow", 55))
        self._emaf = None
        self._emas = None
        self._kf = 2/(self.fast+1)
        self._ks = 2/(self.slow+1)
        self._state = 0  # -1/0/1

    def warmup(self):
        return self.slow

    def on_bar(self, bar: Bar):
        p = bar.close
        self._emaf = p if self._emaf is None else self._kf*p + (1-self._kf)*self._emaf
        self._emas = p if self._emas is None else self._ks*p + (1-self._ks)*self._emas
        if self._emaf is None or self._emas is None:
            return None
        cross = 1 if self._emaf > self._emas else (-1 if self._emaf < self._emas else 0)
        if cross != self._state and cross != 0:
            self._state = cross
            return Signal(side="BUY" if cross > 0 else "SELL", reason="ema-cross")
        return None
