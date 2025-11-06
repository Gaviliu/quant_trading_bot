import os, time
from decimal import Decimal
import lighter

class LighterClient:
    def __init__(self, base_url: str | None = None):
        base_url = base_url or os.getenv("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai")
        self.account_index = int(os.getenv("ACCOUNT_INDEX", "0"))
        self.api_key_index = int(os.getenv("API_KEY_INDEX", "2"))
        self.signer = lighter.SignerClient(
            url=base_url,
            private_key=os.getenv("API_KEY_PRIVATE_KEY"),
            account_index=self.account_index,
            api_key_index=self.api_key_index,
        )
        self.acct_api  = lighter.AccountApi(base_url)
        self.order_api = lighter.OrderApi(base_url)
        self.tx_api    = lighter.TransactionApi(base_url)

    def market_meta(self, symbol: str):
        books = self.order_api.order_books()
        for m in books["markets"]:
            if m["symbol"] == symbol:
                return m
        raise RuntimeError(f"market {symbol} not found")

    def mid_price(self, market_id: int) -> float:
        ob = self.order_api.order_book_details(market_id=market_id)
        return float(ob["mid_price"])

    def next_nonce(self) -> int:
        return int(self.tx_api.next_nonce(api_key_index=self.api_key_index)["next_nonce"])

    def send_market(self, market_id: int, side: str, base_amount: Decimal, coi: int | None = None):
        # ⚠️ 实盘下单接口；默认在示例脚本中被注释，防误下单
        tx = self.signer.create_market_order(
            market_id=market_id, side=side, base_amount=str(base_amount),
            client_order_index=coi or int(time.time()*1000),
            nonce=self.next_nonce(),
        )
        return self.tx_api.send_tx(tx=tx)
