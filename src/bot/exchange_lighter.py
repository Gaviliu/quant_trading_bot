"""
Lighter 交易所最小封装（带详细中文注释）

功能：
- 只读/实盘两种模式（通过 .env 的 READ_ONLY 控制）
- 查询市场元数据（market_meta）
- 查询中间价（mid_price），自动兼容不同字段名
- 实盘下单（send_market），只在 READ_ONLY=false 且配置了密钥时生效
- 统一的资源管理（aclose）

依赖环境变量（.env 中配置）：
- LIGHTER_BASE_URL=https://mainnet.zklighter.elliot.ai
- READ_ONLY=true/false
- ACCOUNT_INDEX=0
- API_KEY_INDEX=2
- API_KEY_PRIVATE_KEY=   （实盘必填）
- ETH_PRIVATE_KEY=       （实盘必填）
"""

# -------- 标准库 & 第三方库导入 --------
import os, time                       # os 读环境变量；time 生成 client_order_index
from decimal import Decimal           # 下单数量/价格通常用 Decimal 更稳
from typing import Any, Dict, List, Optional, Iterable  # 类型注解，便于阅读与补全

import aiohttp                        # 作为 SDK 失败后的 HTTP 兜底客户端
from dotenv import load_dotenv        # 读取 .env
import lighter                        # lighter 官方 Python SDK


# -------- 小工具：安全读取 int 环境变量 --------
def _getenv_int(name: str, default: int) -> int:
    """从环境变量读整数，读不到就给默认值，不抛异常。"""
    v = os.getenv(name)               # 读出字符串
    try:
        return int(v)                 # 尝试转成 int
    except:
        return default                # 失败就返回默认值


# ========================== 核心封装类 ==========================
class LighterClient:
    """最小可用的 Lighter 客户端封装。"""

    def __init__(self, base_url: str | None = None):
        # 1) 先加载 .env（放在最前，保证下面读取得到）
        load_dotenv()  # 从项目根目录读取 .env，注入到进程环境

        # 2) 基础地址（允许传参覆盖），去掉末尾的斜杠更稳
        base_url = base_url or os.getenv("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai")
        self.base_url = base_url.rstrip("/")  # 统一无尾斜杠，后面拼 path 更安全

        # 3) lighter 的 SDK 客户端（尽量优先用 SDK；失败时再用 HTTP 兜底）
        cfg = lighter.Configuration(host=self.base_url)  # SDK 的配置对象
        self.api_client = lighter.ApiClient(configuration=cfg)  # SDK 的底层 HTTP 客户端
        self.acct_api  = lighter.AccountApi(self.api_client)    # 账户相关 API
        self.order_api = lighter.OrderApi(self.api_client)      # 行情/订单簿相关 API
        self.tx_api    = lighter.TransactionApi(self.api_client)# 交易/nonce 相关 API

        # 4) 只读开关：true=不创建签名器、禁止下单；false=需要私钥并允许下单
        self.read_only = os.getenv("READ_ONLY", "true").lower() == "true"  # 缺省只读更安全
        self.signer = None  # 默认无签名器

        # 5) 实盘模式才创建签名器（SignerClient），否则保持只读
        if not self.read_only:
            # 账号索引 / API-KEY 索引 / 两类私钥（API key 私钥 + EOA/ETH 私钥）
            self.account_index = _getenv_int("ACCOUNT_INDEX", 0)
            self.api_key_index = _getenv_int("API_KEY_INDEX", 0)
            api_key_priv = os.getenv("API_KEY_PRIVATE_KEY", "")
            eth_priv     = os.getenv("ETH_PRIVATE_KEY", "")

            # 两个私钥缺一不可（lighter 的签名需要）
            if not api_key_priv or not eth_priv:
                raise RuntimeError("实盘必须设置 API_KEY_PRIVATE_KEY 和 ETH_PRIVATE_KEY")

            # 创建签名器（用于构造并签名交易）
            self.signer = lighter.SignerClient(
                configuration=cfg,               # 直接传 SDK 的配置
                account_index=self.account_index,# 账户索引
                api_key_index=self.api_key_index,# API key 索引
                api_key_private_key=api_key_priv,# API key 私钥
                eth_private_key=eth_priv,        # EOA/ETH 私钥（扣手续费/做签名等）
            )

        # 6) aiohttp 会话对象（用于兜底直接打 HTTP）
        self._session: Optional[aiohttp.ClientSession] = None

    # -------- 内部工具：保证 aiohttp 会话可用 --------
    async def _ensure_session(self):
        """如果还没有 HTTP 会话，或会话已经关闭，就重新创建一个。"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    # -------- 内部工具：GET 并返回 JSON --------
    async def _get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """以 aiohttp 方式 GET 一个 {base_url}+path，并返回 JSON（失败抛异常）。"""
        await self._ensure_session()                                  # 确保有会话
        url = f"{self.base_url}{path}"                                # 拼 URL
        async with self._session.get(url, params=params, timeout=15) as resp:  # 发请求
            resp.raise_for_status()                                   # 非 2xx 直接抛错
            return await resp.json()                                  # 返回 JSON

    # -------- 内部工具：尝试多个路径，哪个先成功用哪个 --------
    async def _get_json_first(self, paths: Iterable[str], params: Dict[str, Any] | None = None):
        """依次尝试多个 path，只要有一个成功就返回；全部失败则抛最后一个异常。"""
        last = None
        for p in paths:
            try:
                return await self._get_json(p, params=params)         # 成功就立刻返回
            except Exception as e:
                last = e                                              # 记住最后一次的异常
        raise last                                                    # 全部失败 → 抛出

    # ========================== 对外功能：查市场 ==========================
    async def market_meta(self, symbol: str):
        """
        获取某个 symbol 的市场元数据（market_id/status）
        逻辑：
          1) 先用 SDK 的 order_books（可能触发 pydantic 校验）
          2) SDK 失败 → 改用 HTTP 兜底（多路径尝试）
        """
        try:
            books = await self.order_api.order_books()                # SDK 调用
            data = books.to_dict() if hasattr(books, "to_dict") else books  # SDK 对象 → dict
        except Exception:
            # SDK 失败时：尝试多个实际线上常见路径
            data = await self._get_json_first([
                "/api/v1/markets",    # 常见
                "/api/v1/orderBooks", # 有的节点这样暴露
                "/markets",           # 有的挂根路径
            ])

        # 无论 SDK 还是 HTTP，都把“市场列表”拿出来（字段名在不同节点可能不同）
        markets: List[Dict[str, Any]] = (
            data.get("markets")       # 一些节点返回 {"markets":[...]}
            or data.get("order_books")# 也有节点返回 {"order_books":[...]}
            or (data if isinstance(data, list) else [])  # 甚至直接就是一个 list
        )

        # 在列表里找出目标 symbol
        for m in markets:
            if m.get("symbol") == symbol:
                return {
                    "symbol": m.get("symbol"),
                    "market_id": m.get("market_id") or m.get("id"),
                    "status": m.get("status"),
                }

        # 没找到就明确告诉调用方
        raise RuntimeError(f"market {symbol} not found")

    # ========================== 对外功能：查中间价 ==========================
    async def mid_price(self, market_id: int) -> float:
        """
        返回“中间价”。不同节点字段名可能不同，这里做了容错：
        依次尝试：mid_price → mark_price → index_price → last_trade_price
        """
        try:
            ob = await self.order_api.order_book_details(market_id=market_id)  # SDK 调用
            data = ob.to_dict() if hasattr(ob, "to_dict") else ob
        except Exception:
            # SDK 失败时 HTTP 兜底，多路径尝试（大小写差异也考虑进来）
            data = await self._get_json_first([
                "/api/v1/orderBookDetails",
                "/orderBookDetails",
            ], params={"market_id": market_id})

        # 兼容有些节点返回 {"order_book_details":[{...}]}
        if isinstance(data, dict) and "order_book_details" in data and isinstance(data["order_book_details"], list):
            if not data["order_book_details"]:
                raise RuntimeError("order_book_details empty")
            data = data["order_book_details"][0]  # 取第一项作为当前市场

        # 第一轮：在当前 data 顶层找常见价字段
        for key in ("mid_price", "mark_price", "index_price", "last_trade_price"):
            v = data.get(key)
            if v is not None:
                return float(v)

        # 第二轮：有些节点把价格塞在 data["order_book"] 里
        obk = data.get("order_book") or {}
        for key in ("mid_price", "mark_price", "index_price", "last_trade_price"):
            v = obk.get(key)
            if v is not None:
                return float(v)

        # 实在找不到就抛错（避免返回 None）
        raise RuntimeError("mid_price missing")

    # ========================== 对外功能：取 nonce（实盘） ==========================
    async def next_nonce(self) -> int:
        """
        实盘签名/发送交易前，需要从链下服务拿到一个“下一次可用 nonce”。
        只对实盘下单有用；只读模式你一般不会调用它。
        """
        data = await self.tx_api.next_nonce(api_key_index=_getenv_int("API_KEY_INDEX", 0))
        return int(data["next_nonce"])

    # ========================== 对外功能：实盘下单 ==========================
    async def send_market(self, market_id: int, side: str, base_amount: Decimal, coi: int | None = None):
        """
        市场价下单（只有在 READ_ONLY=false 且配置了私钥时才允许）。
        参数：
          - market_id: 市场 ID（整型）
          - side: "buy" / "sell"
          - base_amount: 以基币计价的数量（Decimal），注意满足最小数量与小数位
          - coi: client_order_index（可选；不传则用当前毫秒时间戳）
        """
        if self.signer is None:
            # 保护：只读模式下一律禁止下单
            raise RuntimeError("READ_ONLY = true → 禁止下单")

        # 从链下服务拿一个 nonce（防重放/顺序控制）
        nonce = await self.next_nonce()

        # 用签名器构造并签名交易（SDK 完成序列化与签名）
        tx = self.signer.create_market_order(
            market_id=market_id,
            side=side,
            base_amount=str(base_amount),        # SDK 要求字符串
            client_order_index=coi or int(time.time()*1000),  # 给个唯一 ID
            nonce=nonce,                         # 刚拿到的 nonce
        )

        # 把签过名的交易发出去（SDK 会调用对应的 API）
        return await self.tx_api.send_tx(tx=tx)

    # ========================== 资源清理：关闭会话 ==========================
    async def aclose(self):
        """
        关闭底层资源（aiohttp 会话 & lighter 的 ApiClient）；
        建议在主流程 finally 中调用，避免 "Unclosed client session" 警告。
        """
        if self._session and not self._session.closed:
            await self._session.close()
        await self.api_client.close()
