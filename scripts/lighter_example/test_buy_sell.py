# scripts/lighter_example/test_buy_sell.py
import os, time, asyncio, logging
import aiohttp 
import lighter
from dotenv import load_dotenv

# === 测试配置 ===
SIZE_SOL = 0.01          # 测试金额
PRICE_DECIMALS = 3       # 价格精度 (标准精度)
SIZE_DECIMALS = 3        # 数量精度 (标准精度)
MARKET_ID = 2            # SOL
PRICE_URL = "https://mainnet.zklighter.elliot.ai/api/v1/orderBookDetails?market_id=2"

# ⚠️ 设置 5% (500bps) 的滑点
# 这能给卖单足够的空间去匹配买盘，同时避免触发"胖手指"风控
SLIPPAGE_BPS = 500       

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("Tester")

def to_int(val, dec): return int(round(val * (10 ** dec)))

# 1. 稳健查仓 (HTTP)
async def get_sol_position(client):
    l1_address = os.getenv("PUBLIC_WALLET_ADDRESS")
    if not l1_address: return None
    url = f"{client.url}/api/v1/account"
    params = {"by": "l1_address", "value": l1_address}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as resp:
                if resp.status != 200: return None
                data = await resp.json()
                if not data.get("accounts"): return 0.0
                for pos in data["accounts"][0].get("positions", []):
                    if pos.get("market_id") == MARKET_ID:
                        return float(pos.get("position", 0)) * int(pos.get("sign", 0))
                return 0.0
    except Exception: return None

# 2. 稳健查价 (HTTP)
async def get_current_price():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(PRICE_URL, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data["order_book_details"][0]["last_trade_price"])
                return None
    except Exception as e:
        log.error(f"查价失败: {e}")
        return None

async def main():
    load_dotenv()
    client = lighter.SignerClient(
        url=os.getenv("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai"),
        private_key=os.getenv("API_KEY_PRIVATE_KEY"),
        account_index=int(os.getenv("ACCOUNT_INDEX")),
        api_key_index=int(os.getenv("API_KEY_INDEX")),
    )

    log.info("🤖 开始双向测试 (统一 create_market_order + 5% 滑点)...")

    # 1. 初始查仓
    start_pos = await get_sol_position(client)
    curr_price = await get_current_price()
    
    if start_pos is None or curr_price is None:
        log.error("❌ 初始化失败")
        await client.close(); return

    log.info(f"🔍 初始持仓: {start_pos} SOL | 市价: {curr_price}")

    # ==========================================
    # 🟢 步骤 1: 市价买入 (BUY)
    # ==========================================
    # 买单保护价 = 现价 * 1.05 (允许高买)
    buy_limit_price = curr_price * (1 + SLIPPAGE_BPS/10000)
    
    buy_price_int = to_int(buy_limit_price, PRICE_DECIMALS)
    size_int = to_int(SIZE_SOL, SIZE_DECIMALS)

    log.info(f"🚀 [1/2] 买入 {SIZE_SOL} SOL (最高愿付: {buy_limit_price:.4f})...")
    
    try:
        ret_buy = await client.create_market_order(
            market_index=MARKET_ID,
            client_order_index=int(time.time()*1000)%1000000,
            base_amount=size_int,
            avg_execution_price=buy_price_int,
            is_ask=False 
        )
        if ret_buy and len(ret_buy) > 1:
            log.info(f"✅ 买单发送成功: Hash={ret_buy[1].tx_hash}")
    except Exception as e:
        log.error(f"❌ 买单报错: {e}")
        await client.close(); return

    log.info("⏳ 等待 5秒...")
    await asyncio.sleep(5)

    # 验证买入
    pos_mid = await get_sol_position(client)
    log.info(f"🧐 买入后持仓: {pos_mid} SOL")
    
    if pos_mid <= start_pos:
        log.error("❌ 买入未成交，停止测试。")
        await client.close(); return

    # ==========================================
    # 🔴 步骤 2: 市价卖出 (SELL)
    # ==========================================
    # 重新获取价格
    curr_price = await get_current_price()
    
    # 卖单保护价 = 现价 * 0.95 (允许低卖)
    # 这里的 5% 空间足够穿透任何正常的买卖价差
    sell_limit_price = curr_price * (1 - SLIPPAGE_BPS/10000)
    
    sell_price_int = to_int(sell_limit_price, PRICE_DECIMALS)

    log.info(f"🚀 [2/2] 卖出 {SIZE_SOL} SOL (最低接受: {sell_limit_price:.4f})...")

    try:
        ret_sell = await client.create_market_order(
            market_index=MARKET_ID,
            client_order_index=int(time.time()*1000)%1000000 + 1,
            base_amount=size_int,
            avg_execution_price=int(sell_price_int),
            is_ask=True # True = 卖
        )
        if ret_sell and len(ret_sell) > 1:
            log.info(f"✅ 卖单发送成功: Hash={ret_sell[1].tx_hash}")
    except Exception as e:
        log.error(f"❌ 卖单报错: {e}")

    log.info("⏳ 等待 5秒...")
    await asyncio.sleep(5)

    # 验证最终结果
    pos_final = await get_sol_position(client)
    log.info(f"🏁 最终持仓: {pos_final} SOL")

    if abs(pos_final - start_pos) < 0.001:
        log.info("🎉 测试完美通过！5% 滑点策略有效。")
    else:
        log.warning(f"⚠️ 卖出仍未成交，请手动检查。")

    await client.close()

if __name__ == "__main__":
    asyncio.run(main())