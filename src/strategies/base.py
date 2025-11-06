from dataclasses import dataclass
from typing import Optional

@dataclass
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float

@dataclass
class Signal:
    side: Optional[str]  # "BUY" / "SELL" / None
    sl: Optional[float] = None
    tp: Optional[float] = None
    reason: str = ""
