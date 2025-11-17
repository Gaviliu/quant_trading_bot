# scripts/list_markets.py
# -*- coding: utf-8 -*-
"""
列出 Lighter 上的 markets，打印 market_index / symbol 等信息。

思路：
- 调用 RootApi.info() 得到 ZkLighterInfo
- 这个 model 只有 contract_address + additional_properties
- 真正的 markets 很可能在 additional_properties 或 info.to_dict() 里
"""

import asyncio
import json

import lighter


async def main():
    client = lighter.ApiClient()
    try:
        root_api = lighter.RootApi(client)

        info = await root_api.info()

        # 1) 先把 dict 结构打出来看一眼
        info_dict = info.to_dict()
        print("=== info.to_dict().keys() ===")
        print(list(info_dict.keys()))

        # 如果你想完整看，可以取消下面注释（会打印很多）：
        # print(json.dumps(info_dict, indent=2))

        # 2) 试着从里面找可能的 markets 字段
        candidates = ["markets", "marketInfos", "market_info_list", "marketInfo"]
        markets = None
        for k in candidates:
            if k in info_dict:
                markets = info_dict[k]
                print(f"\n[USE] info_dict['{k}'] 作为 markets 列表")
                break

        # 3) 如果上面没找到，再尝试 additional_properties 里找
        if markets is None:
            extra = getattr(info, "additional_properties", None)
            if isinstance(extra, dict):
                print("\n=== info.additional_properties.keys() ===")
                print(list(extra.keys()))
                for k in candidates:
                    if k in extra:
                        markets = extra[k]
                        print(f"\n[USE] info.additional_properties['{k}'] 作为 markets 列表")
                        break

        if markets is None:
            print("\n[WARN] 没有在 info_dict / additional_properties 里找到 markets，")
            print("       请把上面 print 出来的 keys 和一小段 json 发给我，我再帮你精确定位。")
            return

        # 4) 打印每个 market 的核心信息
        print("\n==== Markets ====")
        for m in markets:
            # m 是 dict，防御性取字段
            if isinstance(m, dict):
                idx = m.get("market_index") or m.get("index") or m.get("id")
                name = m.get("symbol") or m.get("name")
                base = m.get("base_symbol") or m.get("base")
                quote = m.get("quote_symbol") or m.get("quote")
                base_dec = m.get("base_decimals") or m.get("baseDecimals")
                price_dec = m.get("price_decimals") or m.get("priceDecimals")
            else:
                # 如果是 model 对象，也按属性方式兜底
                idx = getattr(m, "market_index", getattr(m, "index", None))
                name = getattr(m, "symbol", getattr(m, "name", None))
                base = getattr(m, "base_symbol", getattr(m, "base", None))
                quote = getattr(m, "quote_symbol", getattr(m, "quote", None))
                base_dec = getattr(m, "base_decimals", None)
                price_dec = getattr(m, "price_decimals", None)

            print(
                f"market_index={idx}, symbol={name}, "
                f"base={base}, quote={quote}, "
                f"base_decimals={base_dec}, price_decimals={price_dec}"
            )

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
