# scripts/run_live_lighter.py
# -*- coding: utf-8 -*-
"""
run_live_lighter.py —— EMA + lighter 实盘骨架（REST 行情 + SignerClient 下单）

功能：
- 从 Lighter REST /api/v1/recentTrades?market_id= 获取最新成交价
- 使用你自己的 EMACrossover 策略产出多空信号
- 支持简单 TP/SL：
    - TP：price >= entry * (1 + tp_pct)   （多）
           price <= entry * (1 - tp_pct)   （空）
    - SL：price <= entry * (1 - sl_pct)   （多）
           price >= entry * (1 + sl_pct)   （空）
- 默认 DRY（只打印参数），加 --send-orders 才真的 create_order

使用示例（先干跑看价格和信号）：
    (.venv) python scripts/run_live_lighter.py --market-index 2 --size-base 0.05

确认没问题后，小仓位真下单：
    (.venv) python scripts/run_live_lighter.py --market-index 2 --size-base 0.05 --send-orders
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional, Dict, Any

import sys
import requests
import lighter
from dotenv import load_dotenv

# ---------- 日志 ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("run_live_lighter")

# ---------- 保证能 import 到你的策略 ----------
_THIS = Path(__file__).resolve()
_ROOT = _THIS.parents[1]
_SRC = _ROOT / "src"
for p in (_ROOT, _SRC):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from strategies.ema_crossover import EMACrossover  # type: ignore


def trim_exception(e: Exception) -> str:
    return str(e).strip().split("\n")[-1]


# ====================== 行情源：REST /api/v1/recentTrades ======================

class LighterRestPriceFeed:
    """
    用 REST recentTrades 拿最近成交价：
      GET /api/v1/recentTrades?market_id=<id>&limit=1

    注意：
      - 这里假设返回里有 price / priceDecimals 之类字段
      - 若以后结构调整，可以在 debug 输出的 JSON 基础上再改解析
    """

    def __init__(self, base_url: str, market_index: int):
        self.base_url = base_url.rstrip("/")
        self.market_index = market_index
        self._debug_done = False  # 避免每次都狂打 debug

    def _request(self, params: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/api/v1/recentTrades"
        resp = requests.get(url, params=params, timeout=5)

        if resp.status_code != 200 and not self._debug_done:
            self._debug_done = True
            print("[DEBUG] recentTrades HTTP", resp.status_code)
            print("[DEBUG] recentTrades URL:", resp.url)
            print("[DEBUG] recentTrades body:", resp.text[:800])

        resp.raise_for_status()

        try:
            return resp.json()
        except Exception as e:
            raise RuntimeError(f"recentTrades 响应不是 JSON: {e}")

    async def next_price(self) -> float:
        """
        返回 float 价格。
        服务器提示必须传 `market_id` 参数。
        """
        data = self._request({"market_id": self.market_index, "limit": 1})

        # 根据常见模式解析 trades
        trades = None
        if isinstance(data, dict):
            if "trades" in data:
                trades = data["trades"]
            elif "items" in data:
                trades = data["items"]
            elif "data" in data:
                inner = data["data"]
                if isinstance(inner, dict):
                    trades = inner.get("trades") or inner.get("items")
                elif isinstance(inner, list):
                    trades = inner
        elif isinstance(data, list):
            trades = data

        if not trades:
            raise RuntimeError(f"recentTrades 返回空 trades: {data}")

        t = trades[-1]

        # 取价格字段
        if isinstance(t, dict):
            raw_price = (
                t.get("price")
                or t.get("px")
                or t.get("tradePrice")
            )
            decimals = (
                t.get("price_decimals")
                or t.get("priceDecimals")
                or 0
            )
        else:
            raw_price = (
                getattr(t, "price", None)
                or getattr(t, "px", None)
                or getattr(t, "tradePrice", None)
            )
            decimals = (
                getattr(t, "price_decimals", None)
                or getattr(t, "priceDecimals", None)
                or 0
            )

        if raw_price is None:
            raise RuntimeError(f"trade 里没有 price 字段: {t}")

        try:
            dec_i = int(decimals)
        except Exception:
            dec_i = 0

        if dec_i:
            price = float(raw_price) / (10 ** dec_i)
        else:
            price = float(raw_price)

        return price


# ====================== lighter SignerClient 构造 ======================

async def build_lighter_signer() -> lighter.SignerClient:
    """
    用 .env 构造 SignerClient：

      - LIGHTER_BASE_URL（可选，默认 mainnet）
      - ACCOUNT_INDEX
      - API_KEY_INDEX
      - API_KEY_PRIVATE_KEY
    """
    load_dotenv()

    base_url = os.getenv("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai")
    api_key_priv = os.getenv("API_KEY_PRIVATE_KEY")
    account_index = os.getenv("ACCOUNT_INDEX")
    api_key_index = os.getenv("API_KEY_INDEX")

    if not api_key_priv or not account_index or not api_key_index:
        raise RuntimeError(
            "缺少 .env 配置：ACCOUNT_INDEX / API_KEY_INDEX / API_KEY_PRIVATE_KEY"
        )

    account_index_int = int(account_index)
    api_key_index_int = int(api_key_index)

    client = lighter.SignerClient(
        url=base_url,
        private_key=api_key_priv,
        account_index=account_index_int,
        api_key_index=api_key_index_int,
    )

    err = client.check_client()
    if err is not None:
        raise RuntimeError(f"SignerClient.check_client 失败: {trim_exception(err)}")

    log.info(
        "SignerClient ready: base_url=%s account_index=%s api_key_index=%s",
        base_url,
        account_index_int,
        api_key_index_int,
    )
    return client


# ====================== 下单封装 ======================

async def submit_order(
    client: lighter.SignerClient,
    market_index: int,
    side: str,          # "buy" / "sell"
    size_base: float,   # 标的数量，例如 0.1 SOL
    price: float,       # 价格，例如 150.12
    send_orders: bool = False,
) -> None:
    """
    使用 lighter.SignerClient.create_order 下单（限价单示例）

    ⚠ 精度一定要你自己确认 ⚠

    暂时用：
      base_amount = size_base * 1e4
      price_int   = price * 1e4

    实盘前建议在前端挂一个极小订单，看链上/接口实际的 base_amount / price 再调整。
    """
    is_ask = True if side == "sell" else False

    # TODO: 根据真实 decimals 改掉这两行
    base_amount = int(size_base * 1e4)
    price_int = int(price * 1e4)

    order_type = lighter.SignerClient.ORDER_TYPE_LIMIT
    tif = lighter.SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME

    log.info(
        "[ORDER-PREP] side=%s market_index=%s size_base=%.6f price=%.4f "
        "=> base_amount=%s price_int=%s",
        side,
        market_index,
        size_base,
        price,
        base_amount,
        price_int,
    )

    if not send_orders:
        log.warning(
            "[DRY-ORDER] 只打印参数，不发送 create_order。"
            "真盘请加 --send-orders 再跑。"
        )
        return

    tx, tx_hash, err = await client.create_order(
        market_index=market_index,
        client_order_index=int(time.time()),
        base_amount=base_amount,
        price=price_int,
        is_ask=is_ask,
        order_type=order_type,
        time_in_force=tif,
        reduce_only=False,
        trigger_price=0,
    )

    log.info(
        "[ORDER-RES] side=%s market_index=%s size_base=%.6f price=%.4f "
        "tx=%s tx_hash=%s err=%s",
        side,
        market_index,
        size_base,
        price,
        tx,
        tx_hash,
        err,
    )
    if err is not None:
        raise RuntimeError(err)


# ====================== 主循环：EMA + Lighter + TP/SL ======================

async def main():
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--market-index",
        type=int,
        default=0,
        help="lighter 的 market_id（你在前端抓包确认过的那个）",
    )
    ap.add_argument(
        "--size-base",
        type=float,
        default=0.1,
        help="每次下单标的数量，例如 0.1 表示 0.1 SOL",
    )
    ap.add_argument(
        "--poll",
        type=int,
        default=30,
        help="每多少秒刷新一次价格并推进策略",
    )
    ap.add_argument(
        "--send-orders",
        action="store_true",
        help="真的发送订单（默认只打印参数，强烈建议先 dry-run）",
    )

    # EMA 参数：直接透传给你的 EMACrossover
    ap.add_argument("--fast-ema", dest="fast_period", type=int, default=9)
    ap.add_argument("--slow-ema", dest="slow_period", type=int, default=26)
    ap.add_argument("--confirm", type=int, default=1)
    ap.add_argument("--band-mode", choices=["bps", "atr_pct"], default="bps")
    ap.add_argument("--band-bps", type=float, default=10.0)
    ap.add_argument("--atr-k", type=float, default=0.6)
    ap.add_argument("--trend-ema", type=int, default=200)
    ap.add_argument("--cooldown", type=int, default=0)

    # TP / SL 参数
    ap.add_argument("--tp-pct", type=float, default=0.02, help="止盈比例，例如 0.02=+2%")
    ap.add_argument("--sl-pct", type=float, default=0.01, help="止损比例，例如 0.01=-1%")

    args = ap.parse_args()

    load_dotenv()
    base_url = os.getenv("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai")

    # 1) REST 行情源
    feed = LighterRestPriceFeed(base_url, market_index=args.market_index)

    # 2) 策略实例（复用你的 EMACrossover）
    st = EMACrossover(
        fast=args.fast_period,
        slow=args.slow_period,
        confirm_bars=args.confirm,
        band_mode=args.band_mode,
        band_bps=args.band_bps,
        atr_k=args.atr_k,
        trend_ema=args.trend_ema,
        cooldown_bars=args.cooldown,
        mute_same_dir_when_holding=True,
        print_ema_each_bar=False,
    )

    pos = 0  # 1=long, -1=short, 0=flat
    entry: Optional[float] = None

    # 3) 下单 client
    signer = await build_lighter_signer()

    log.warning(
        "== START == market_index=%s size_base=%s poll=%s send_orders=%s tp=%.3f sl=%.3f",
        args.market_index,
        args.size_base,
        args.poll,
        args.send_orders,
        args.tp_pct,
        args.sl_pct,
    )
    log.warning(
        "行情源：REST /api/v1/recentTrades?market_id=...。"
        "先确认 price 输出合理、TP/SL 行为符合预期，再考虑 --send-orders 真下单。"
    )

    try:
        while True:
            # === 1) 获取最新价格 ===
            try:
                price = await feed.next_price()
            except Exception as e:
                log.warning("获取价格失败: %s", trim_exception(e))
                await asyncio.sleep(args.poll)
                continue

            # === 2) 先检查 TP / SL ===
            if pos != 0 and entry is not None:
                if pos == 1:
                    # 多仓：TP / SL
                    if price <= entry * (1 - args.sl_pct):
                        log.info("[TP/SL] LONG SL: price=%.4f entry=%.4f", price, entry)
                        await submit_order(
                            signer,
                            args.market_index,
                            "sell",
                            args.size_base,
                            price,
                            send_orders=args.send_orders,
                        )
                        pos, entry = 0, None
                    elif price >= entry * (1 + args.tp_pct):
                        log.info("[TP/SL] LONG TP: price=%.4f entry=%.4f", price, entry)
                        await submit_order(
                            signer,
                            args.market_index,
                            "sell",
                            args.size_base,
                            price,
                            send_orders=args.send_orders,
                        )
                        pos, entry = 0, None

                elif pos == -1:
                    # 空仓：TP / SL
                    if price >= entry * (1 + args.sl_pct):
                        log.info("[TP/SL] SHORT SL: price=%.4f entry=%.4f", price, entry)
                        await submit_order(
                            signer,
                            args.market_index,
                            "buy",
                            args.size_base,
                            price,
                            send_orders=args.send_orders,
                        )
                        pos, entry = 0, None
                    elif price <= entry * (1 - args.tp_pct):
                        log.info("[TP/SL] SHORT TP: price=%.4f entry=%.4f", price, entry)
                        await submit_order(
                            signer,
                            args.market_index,
                            "buy",
                            args.size_base,
                            price,
                            send_orders=args.send_orders,
                        )
                        pos, entry = 0, None

            # === 3) 推进策略（EMA） ===
            if hasattr(st, "set_position_side"):
                st.set_position_side(pos)

            if hasattr(st, "on_close_fast_adapt"):
                ret = st.on_close_fast_adapt(price, price, price)
            else:
                ret = st.on_close_fast(price, price, price)

            if isinstance(ret, int):
                sig = ret
            else:
                side = getattr(ret, "side", None)
                sig = 1 if side == "buy" else (-1 if side == "sell" else 0)

            # === 4) 根据信号开仓 / 反手 ===
            if sig == 1 and pos <= 0:
                # 开多 / 反手多
                log.info("[SIG] BUY signal at price=%.4f (pos=%s)", price, pos)
                if pos == -1:
                    # 先平空
                    await submit_order(
                        signer,
                        args.market_index,
                        "buy",
                        args.size_base,
                        price,
                        send_orders=args.send_orders,
                    )
                await submit_order(
                    signer,
                    args.market_index,
                    "buy",
                    args.size_base,
                    price,
                    send_orders=args.send_orders,
                )
                pos, entry = 1, price

            elif sig == -1 and pos >= 0:
                # 开空 / 反手空
                log.info("[SIG] SELL signal at price=%.4f (pos=%s)", price, pos)
                if pos == 1:
                    # 先平多
                    await submit_order(
                        signer,
                        args.market_index,
                        "sell",
                        args.size_base,
                        price,
                        send_orders=args.send_orders,
                    )
                await submit_order(
                    signer,
                    args.market_index,
                    "sell",
                    args.size_base,
                    price,
                    send_orders=args.send_orders,
                )
                pos, entry = -1, price

            log.info("BAR price=%.4f pos=%s entry=%s", price, pos, entry)
            await asyncio.sleep(args.poll)
    finally:
        try:
            await signer.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
