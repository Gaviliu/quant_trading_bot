import os, time, yaml
from dotenv import load_dotenv
from decimal import Decimal
from bot.exchange_lighter import LighterClient
from strategies.base import Bar
from strategies.ema_crossover import EMACrossover
from utils.logger import setup_logger
log = setup_logger("live", "runtime.log")
log.info("robot started")



load_dotenv()
with open("config/live.sol-usdc.yaml", "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

symbol = cfg.get("symbol", "SOL-USDC")
ex = LighterClient()
meta = ex.market_meta(symbol)
market_id = int(meta["market_id"])
st = EMACrossover(**cfg["strategies"][0].get("params", {}))

print(f"[INIT] Market={symbol} (id={market_id}) | Strategy=EMA(fast={st.fast}, slow={st.slow})")

while True:
    # 简化：用订单簿中间价当作当前价；生产建议使用K线/成交数据
    mid = ex.mid_price(market_id)
    bar = Bar(ts=int(time.time()*1000), open=mid, high=mid*1.001, low=mid*0.999, close=mid, volume=0)
    sig = st.on_bar(bar)
    if sig and sig.side:
        print(f"[SIGNAL] {sig.side} at {bar.close:.4f}  reason={sig.reason}")
        # ⚠️ 确认无误再放开实盘下单（小额）：
        # res = ex.send_market(market_id, sig.side, Decimal("0.1"))
        # print("[ORDER]", res)
    time.sleep(60)
