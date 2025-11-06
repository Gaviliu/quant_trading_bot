# scripts/run_live.py
import asyncio
import os, time, yaml
from decimal import Decimal
from dotenv import load_dotenv

from bot.exchange_lighter import LighterClient
from strategies.base import Bar
from strategies.ema_crossover import EMACrossover
from utils.logger import setup_logger


log = setup_logger("live", "runtime.log")


async def main():
    log.info("robot started")
    load_dotenv()

    # 读取配置
    with open("config/live.sol-usdc.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    symbol = cfg.get("symbol", "SOL-USDC")

    # ⚠️ 关键：在“正在运行的事件循环”里实例化 LighterClient
    ex = LighterClient()

    # 兼容两种返回字段：id / market_id
    meta = ex.market_meta(symbol)
    market_id = int(meta.get("id", meta.get("market_id")))
    st = EMACrossover(**cfg["strategies"][0].get("params", {}))

    print(f"[INIT] Market={symbol} (id={market_id}) | Strategy=EMA(fast={st.fast}, slow={st.slow})")

    while True:
        # 简化：用订单簿中间价作为当前价；生产建议使用真实K线/成交聚合
        mid = ex.mid_price(market_id)
        now_ms = int(time.time() * 1000)
        bar = Bar(ts=now_ms, open=mid, high=mid * 1.001, low=mid * 0.999, close=mid, volume=0.0)

        sig = st.on_bar(bar)
        if sig and sig.side:
            msg = f"[SIGNAL] {sig.side.name} at {bar.close:.4f}  reason={sig.reason}"
            print(msg)
            log.info(msg)

            # ✅ 确认后再放开实盘下单（小额）
            # res = ex.send_market(market_id, sig.side, Decimal("0.1"))
            # print("[ORDER]", res)
            # log.info(f"[ORDER] {res}")

        # 用异步休眠，避免阻塞事件循环
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
