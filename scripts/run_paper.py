# scripts/run_paper.py
import asyncio, time, os, yaml
from dotenv import load_dotenv
from strategies.base import Bar
from strategies.ema_crossover import EMACrossover
from bot.exchange_lighter import LighterClient

# 终端颜色
try:
    from colorama import init as _colorama_init, Fore, Style
    _colorama_init(autoreset=True)
except Exception:
    class _D: 
        def __getattr__(self, _): return ""
    Fore = _D(); Style = _D()

def _c(text: str, color: str = "") -> str:
    return f"{color}{Style.BRIGHT}{text}{Style.RESET_ALL}" if color else text

load_dotenv()

# ========= 账户与风控 =========
INIT_BAL_USD      = 10_000.0     # 初始资金
LEVERAGE          = 10           # 杠杆
TRADE_SIZE_SOL    = 100.0        # 固定每次开仓手数

# —— 止盈：以“保证金”为基准的 +100%；止损：以“账户余额”的 1%（你新需求）——
TP_MARGIN_MULT    = 1.00         # +100% * margin
SL_BALANCE_PCT    = 0.01         # -1% * balance（账户余额）

TAKER_FEE_PCT     = 0.0000
SLIPPAGE_PCT      = 0.0000

# —— 轮询周期（改这里）——
POLL_SEC          = 60           # 每 60s 合成一根 bar
# ============================

def fmt_side(side: int) -> str:
    return "LONG" if side > 0 else "SHORT"

async def main():
    # 读取策略参数
    cfg_path = os.getenv("CONFIG_PATH", "config/live.sol.yaml")
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    sym   = cfg.get("symbol", "SOL")
    sconf = cfg["strategies"][0].get("params", {})
    sconf.setdefault("confirm_bars", 2)
    sconf.setdefault("band_bps", 5.0)
    # 让策略在持仓同向时静音 CROSS/TRIGGER，同时仍打印 [EMA]
    sconf.setdefault("mute_same_dir_when_holding", True)
    sconf.setdefault("print_ema_each_bar", True)

    st = EMACrossover(**sconf)

    # 行情
    ex   = LighterClient()
    meta = await ex.market_meta(sym)
    mid_id = int(meta["market_id"])

    print(_c(
        f"[INIT] Paper | {sym}(id={mid_id}) | fast={st.fast}, slow={st.slow}, "
        f"confirm={getattr(st,'confirm_bars','?')}, band={getattr(st,'band_bps','?')}bp | "
        f"LEV={LEVERAGE}x, SIZE={TRADE_SIZE_SOL:.1f} SOL | TP=+100% margin, SL=1.00% balance | poll={POLL_SEC}s",
        Fore.CYAN
    ))

    # 账户/持仓
    balance_usd   = INIT_BAL_USD
    side          = 0          # 1=LONG, -1=SHORT, 0=FLAT
    size          = 0.0
    entry_price   = None

    try:
        while True:
            # 取中间价 → 合成一根“bar”
            px = await ex.mid_price(mid_id)
            ts = int(time.time() * 1000)
            bar = Bar(ts=ts, open=px, high=px*1.001, low=px*0.999, close=px, volume=0.0)

            # 告诉策略当前持仓方向（用于“同向静音”）
            st.set_position_side(side)

            # 计算信号
            sig = st.on_bar(bar)

            # 有持仓 → 止盈止损
            if side != 0 and entry_price is not None:
                margin = (entry_price * size) / LEVERAGE
                pnl_usd = (px - entry_price) * size if side > 0 else (entry_price - px) * size

                tp_hit = (pnl_usd >= TP_MARGIN_MULT * margin)
                sl_hit = (pnl_usd <= -SL_BALANCE_PCT * balance_usd)

                if tp_hit or sl_hit:
                    exit_px = px * (1.0 - SLIPPAGE_PCT)
                    fee_usd = exit_px * size * TAKER_FEE_PCT
                    realized = ((exit_px - entry_price) * size) if side > 0 else ((entry_price - exit_px) * size)
                    net = realized - fee_usd
                    balance_usd += net
                    tag = "TP" if tp_hit else "SL"
                    color = Fore.GREEN if net >= 0 else Fore.RED
                    reason = "+100% margin" if tp_hit else "-1% balance"
                    print(_c(
                        f"[{tag}] {fmt_side(side)} exit@{exit_px:.4f}  realized={net:.2f} USD  "
                        f"reason={reason}  bal={balance_usd:.2f}",
                        color
                    ))
                    side, size, entry_price = 0, 0.0, None

                else:
                    color = Fore.GREEN if pnl_usd >= 0 else Fore.RED
                    print(_c(
                        f"[HOLD] px={px:.4f}  side={fmt_side(side)}  size={size:.3f}  entry={entry_price:.4f}  "
                        f"uPnL={pnl_usd:.2f} USD  margin≈{margin:.2f}  bal={balance_usd:.2f}",
                        color
                    ))

            # 根据信号开/平/反手
            if sig:
                want = 1 if sig.side.lower() == "buy" else -1

                if side == 0:
                    side = want
                    fill = px * (1.0 + SLIPPAGE_PCT if side > 0 else 1.0 - SLIPPAGE_PCT)
                    fee  = fill * TRADE_SIZE_SOL * TAKER_FEE_PCT
                    size = TRADE_SIZE_SOL
                    entry_price = fill
                    margin = (entry_price * size) / LEVERAGE
                    print(_c(
                        f"[ENTER {fmt_side(side)}] {size:.3f} SOL @ {fill:.4f}  margin≈{margin:.2f}  "
                        f"fee={fee:.2f}  reason={sig.reason}",
                        Fore.GREEN if side > 0 else Fore.RED
                    ))

                elif want != side:  # 反手：先平旧仓再开新仓
                    close_px = px * (1.0 - SLIPPAGE_PCT)
                    fee_close = close_px * size * TAKER_FEE_PCT
                    realized = ((close_px - entry_price) * size) if side > 0 else ((entry_price - close_px) * size)
                    net_close = realized - fee_close
                    balance_usd += net_close
                    print(_c(
                        f"[FLIP-CLOSE {fmt_side(side)}] exit@{close_px:.4f}  {net_close:.2f} USD  bal={balance_usd:.2f}",
                        Fore.RED if net_close < 0 else Fore.GREEN
                    ))

                    side = want
                    fill = px * (1.0 + SLIPPAGE_PCT if side > 0 else 1.0 - SLIPPAGE_PCT)
                    fee_open = fill * TRADE_SIZE_SOL * TAKER_FEE_PCT
                    size = TRADE_SIZE_SOL
                    entry_price = fill
                    margin = (entry_price * size) / LEVERAGE
                    print(_c(
                        f"[FLIP-OPEN {fmt_side(side)}] {size:.3f} SOL @ {fill:.4f}  margin≈{margin:.2f}  "
                        f"fee={fee_open:.2f}  reason={sig.reason}",
                        Fore.GREEN if side > 0 else Fore.RED
                    ))

            await asyncio.sleep(POLL_SEC)

    finally:
        if hasattr(ex, "aclose"):
            await ex.aclose()
        print(_c(f"[END] bal={balance_usd:.2f}, pos={size} @ {entry_price}", Fore.CYAN))


if __name__ == "__main__":
    asyncio.run(main())
