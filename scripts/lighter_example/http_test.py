import requests
import json
url = "https://mainnet.zklighter.elliot.ai/api/v1/orderBookDetails?market_id=2"
headers = {"accept":"application/json"}
response = requests.get(url, headers=headers)
data = float(json.loads(response.text)["order_book_details"][0]["last_trade_price"])
data1 = float(json.loads(response.text)["order_book_details"][0]["min_quote_amount"])
print(json.loads(response.text))
print(type(response.text))
print("SOL min_quote_amount %.4f，SOL最新的价格%.3f"%(data1,data))
print(f"SOL min_quote_amount {data1:.3f}，SOL最新的价格 {data:.3f}")
print("SOL最新的价格%.3f"%data)
print(f"SOL最新的价格{data:.3f}")
