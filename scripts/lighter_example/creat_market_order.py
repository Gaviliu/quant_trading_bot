# scripts/lighter_example.py
# -*- coding: utf-8 -*-
"""
lighter 官方风格示例（改造版）：
- 从 .env 读取 MAINNET 配置（不要把私钥写死在代码里）
- 使用 SignerClient.check_client() 检查 key 是否可用
- 使用 create_auth_token_with_expiry() 生成 auth token
- 用 auth token 调用一个需要鉴权的私有接口：/api/v1/accountActiveOrders
- 最后调用 create_order() 下一个极小额度的限价单

!!! 安全提醒 !!!
请不要把私钥、API key 明文写在代码里，也不要提交到 GitHub。
"""

import os
import asyncio
import logging

import requests
import lighter
from dotenv import load_dotenv

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger("lighter_example")


def trim_exception(e: Exception) -> str:
    return str(e).strip().split("\n")[-1]


async def main() -> None:
    # -------------------------
    # 1) 读取 .env 配置
    # -------------------------
    load_dotenv()

    base_url = os.getenv("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai")
    api_key_priv = os.getenv("API_KEY_PRIVATE_KEY")
    account_index = os.getenv("ACCOUNT_INDEX")
    api_key_index = os.getenv("API_KEY_INDEX")
    # 你当前在用的 SOL-PERP 的 market_id / market_index = 2
    market_id = int(os.getenv("MARKET_ID", "2"))

    if not api_key_priv or not account_index or not api_key_index:
        raise RuntimeError(
            "缺少 .env 配置：必须设置 LIGHTER_BASE_URL / ACCOUNT_INDEX / API_KEY_INDEX / "
            "API_KEY_PRIVATE_KEY（以及可选的 MARKET_ID）"
        )

    account_index_int = int(account_index)
    api_key_index_int = int(api_key_index)

    log.info(
        "Using base_url=%s account_index=%s api_key_index=%s market_id=%s",
        base_url,
        account_index_int,
        api_key_index_int,
        market_id,
    )

    # -------------------------
    # 2) 初始化 SignerClient
    # -------------------------
    client = lighter.SignerClient(
        url=base_url,
        private_key=api_key_priv,
        account_index=account_index_int,
        api_key_index=api_key_index_int,
    )

    # 检查 key 是否有效
    err = client.check_client()
    if err is not None:
        log.error("CheckClient error: %s", trim_exception(err))
        return
    log.info("CheckClient OK")

    # -----------------------------------
    # 3) 生成 AUTH TOKEN（for private API）
    # -----------------------------------
    # 这里用默认的 10 分钟有效期
    auth, err = client.create_auth_token_with_expiry(
        lighter.SignerClient.DEFAULT_10_MIN_AUTH_EXPIRY
    )
    if err is not None:
        log.error("create_auth_token_with_expiry error: %s", trim_exception(err))
        return

    log.info("Auth Token (short preview) = %s...", auth[:32])

    # 用 auth token 调用一个需要鉴权的私有接口：
    # 参考官方文档：accountActiveOrders 之类都需要 Authorization 头
    url = f"{base_url}/api/v1/accountActiveOrders"
    params = {
        "account_index": account_index_int,
        "market_id": market_id,
    }
    try:
        resp = requests.get(
            url,
            params=params,
            headers={"Authorization": auth},
            timeout=10,
        )
        log.info(
            "accountActiveOrders status=%s body=%s",
            resp.status_code,
            resp.text[:200],
        )
    except Exception as e:
        log.error("accountActiveOrders request failed: %s", trim_exception(e))

    # -----------------------------------
    # 4) 创建一笔极小额度的 LIMIT ORDER（示例）
    # -----------------------------------
    # ⚠ 这里的 base_amount / price 精度只是示例，你实盘前要参照官方文档 / UI 确认 decimals
    # 假设：
    #   - base_amount 以 1e4 为单位：0.005 -> 50
    #   - price       以 1e4 为单位：140.1234 -> 1401234
    size_base = 0.005
    px = 118.00  # 只是示例价，建议改成你 recentTrades 里看到的附近价格

    base_amount = int(size_base * 1e4)
    price_int = int(px * 1e2)

    log.info(
        "Create LIMIT order prep: market_index=%s size_base=%.6f px=%.4f "
        "=> base_amount=%s price_int=%s",
        market_id,
        size_base,
        px,
        base_amount,
        price_int,
    )

    try:
        tx, tx_hash, err = await client.create_order(
            market_index=market_id,  # 这里用同一个 market_id = 2 (SOL-PERP)
            client_order_index=int(asyncio.get_event_loop().time()),
            base_amount=base_amount,
            price=price_int,
            is_ask=False,  # False = BUY, True = SELL
            order_type=lighter.SignerClient.ORDER_TYPE_LIMIT,
            time_in_force=lighter.SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            reduce_only=False,
            trigger_price=0,
        )
        log.info("Create Order result: tx=%s tx_hash=%s err=%s", tx, tx_hash, err)
        if err is not None:
            raise Exception(err)
    except lighter.exceptions.ForbiddenException as e:
        # 专门打印 403 的细节，方便给官方排查
        log.error("ForbiddenException (403) during create_order: %s", trim_exception(e))
        raise
    except Exception as e:
        log.error("Create Order failed: %s", trim_exception(e))
        raise


if __name__ == "__main__":
    asyncio.run(main())
