# scripts/lighter_example/fetch_historical_ticks.py
import os
import time
import asyncio
import aiohttp
import csv
import logging
from datetime import datetime

# === 配置区域 ===
MARKET_ID = 2            # SOL
BATCH_SIZE = 1000        # 每次拉取多少条
LIMIT_DAYS = 3           # 拉取过去 3 天
DATA_DIR = "data"

# 尝试使用公开的 trades 接口
BASE_URL = "https://mainnet.zklighter.elliot.ai/api/v1/trades"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("TickFetcher")

async def fetch_trades(session, cursor=None):
    """
    通用拉取函数，支持多种分页参数尝试
    """
    params = {
        "market_id": MARKET_ID,
        "limit": BATCH_SIZE,
    }
    
    # 尝试适配分页逻辑
    # 某些 API 用 end_time，某些用 timestamp，某些用 cursor/before_id
    # 我们这里先尝试用 end_time (毫秒)
    if cursor:
        params["end_time"] = int(cursor)

    try:
        # 打印请求 URL 方便调试
        # log.info(f"GET {BASE_URL} params={params}")
        
        async with session.get(BASE_URL, params=params, timeout=10) as resp:
            if resp.status != 200:
                text = await resp.text()
                log.error(f"API请求失败: {resp.status} | Body: {text}")
                return None
            
            data = await resp.json()
            
            # 解析数据结构
            trades = []
            if isinstance(data, list):
                trades = data
            elif isinstance(data, dict):
                # 尝试常见的字段名
                for key in ["trades", "result", "data"]:
                    if key in data:
                        trades = data[key]
                        break
            
            return trades
            
    except Exception as e:
        log.error(f"网络异常: {e}")
        return None

def save_ticks(ticks, filename):
    file_exists = os.path.isfile(filename)
    
    # API 返回通常是倒序 (最新 -> 最旧)，我们需要正序保存吗？
    # 为了追加方便，通常保存为原始顺序，回测时再排序。
    # 这里我们按时间戳正序排列，方便人类阅读
    ticks.sort(key=lambda x: x['timestamp'])
    
    with open(filename, "a", newline='') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "price", "position", "equity"])
            
        for t in ticks:
            # 兼容毫秒/秒
            ts = t['timestamp']
            if ts > 10000000000: ts = ts / 1000.0
            
            price = float(t['price'])
            writer.writerow([ts, price, 0, 0])

async def main():
    if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)
    now_str = datetime.now().strftime('%Y%m%d_%H%M')
    filename = f"{DATA_DIR}/historical_ticks_sol_{now_str}.csv"
    
    log.info(f"🚀 启动 Tick 下载器... 目标文件: {filename}")
    
    # 截止时间 (3天前)
    stop_ts = (time.time() - LIMIT_DAYS * 86400) * 1000 
    
    current_cursor = None # 起始游标 (空代表拉取最新)
    total_count = 0
    
    async with aiohttp.ClientSession() as session:
        while True:
            trades = await fetch_trades(session, current_cursor)
            
            if not trades:
                log.info("⏹️ 拉取结束 (无数据或报错)")
                break
                
            # 找到这批数据里最旧的时间戳
            # 假设 API 返回的是按时间倒序排列 (index 0 最新, -1 最旧)
            oldest_trade = trades[-1]
            oldest_ts = oldest_trade['timestamp']
            
            # 检查是否到达时间限制
            if oldest_ts < stop_ts:
                # 过滤掉超出时间的数据
                valid_trades = [t for t in trades if t['timestamp'] >= stop_ts]
                save_ticks(valid_trades, filename)
                total_count += len(valid_trades)
                log.info(f"✅ 已到达 {LIMIT_DAYS} 天前的边界，任务完成。")
                break
            
            # 保存
            save_ticks(trades, filename)
            total_count += len(trades)
            
            # 更新游标: 下一次拉取比最旧那条更旧的数据
            # 稍微减 1ms 防止重复
            current_cursor = oldest_ts - 1
            
            # 打印进度
            dt_str = datetime.fromtimestamp(oldest_ts/1000).strftime('%m-%d %H:%M:%S')
            print(f"\r已获取: {total_count} 条 | 追溯至: {dt_str}", end="", flush=True)
            
            # 稍微休息，防封
            await asyncio.sleep(0.2)

    print(f"\n💾 下载完成！总计 {total_count} 条数据保存在 {filename}")

if __name__ == "__main__":
    asyncio.run(main())