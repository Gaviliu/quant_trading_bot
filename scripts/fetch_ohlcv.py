import argparse, time, os
import pandas as pd
import ccxt

TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000
}

def fetch_all(exchange, symbol, timeframe, since_ms, until_ms):
    tf_ms = TF_MS[timeframe]
    # 交易所分页上限：kucoin 1500、okx 100
    limit = 1500 if exchange.id == "kucoin" else 100
    rows = []
    cursor = since_ms
    while cursor < until_ms:
        # 目标这一批拉到「最多limit根」或「直到until」
        batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=limit)
        if not batch:
            break
        rows += batch
        last_ts = batch[-1][0]
        # 下一页起点 = 最后一根的时间戳 + 一个周期
        cursor = last_ts + tf_ms
        # 安全兜底：如果时间没有向前推进，避免死循环
        if cursor <= since_ms:
            cursor = since_ms + tf_ms
        since_ms = cursor
        # 速率限制
        time.sleep(exchange.rateLimit / 1000)
        # 提前结束：若最后一根已经超过 until
        if last_ts >= until_ms - tf_ms:
            break
    return rows

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exchange", default="okx", choices=["kucoin", "okx"])
    ap.add_argument("--symbol",   default="SOL/USDT")  # 现货：两家都用这个写法
    ap.add_argument("--timeframe",default="5m")
    ap.add_argument("--days",     type=int, default=360)
    ap.add_argument("--out",      default=None)
    args = ap.parse_args()

    ex = getattr(ccxt, args.exchange)({"enableRateLimit": True})
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - args.days * 24 * 3600 * 1000
    until_ms = now_ms

    data = fetch_all(ex, args.symbol, args.timeframe, since_ms, until_ms)
    if not data:
        raise SystemExit("No data fetched. Try a longer timeframe or different exchange.")

    df = pd.DataFrame(data, columns=["ts","open","high","low","close","volume"])
    df.drop_duplicates(subset=["ts"], inplace=True)
    df.sort_values("ts", inplace=True)
    df["ts_iso"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert("UTC")

    out = args.out or f"data/{args.symbol.replace('/','')}_{args.timeframe}.csv"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    df.to_csv(out, index=False)

    minutes = len(df) * (TF_MS[args.timeframe] / 60_000)
    days_cov = minutes / (60 * 24)
    print(f"saved -> {out}  rows={len(df)}  "
          f"from={df['ts_iso'].iloc[0]} to={df['ts_iso'].iloc[-1]}  "
          f"~coverage={days_cov:.1f} days  exchange={args.exchange}")

if __name__ == "__main__":
    main()
