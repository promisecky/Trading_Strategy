"""价值回归独立回测器 — 第一性原理实现, 不复用 portfolio/simulator 的再平衡模型。

军规硬约束(与文档逐条对应):
- 资金分 5 档, 单票金额 = 信号日收盘总资产 ÷ 5, 一次性买满槽位, 不分批;
- 满仓出新信号直接放弃, 不换仓; 卖出后不再平衡, 回款闲置;
- 收盘确认信号 → 次日开盘价成交;
- 止损(①)后该股冷却 60 个交易日, 期间信号一律忽略;
- 池于每季度首日(1/4/7/10 月第一个交易日)以 as-of 数据重建, 防前视;
- 闲置现金可选按年化 IDLE_YIELD_PCT 日复利(场内货币化)。

近似项(文档化):
- 市值 as-of = 当日收盘 × 当前总股本(股本变动稀薄, 忽略历史股本差异);
- ROE 披露日缺失时用法定截止日(年报 04-30), 见 fetch 层。
"""

from __future__ import annotations

from value.config import (
    COOLDOWN_DAYS,
    IDLE_YIELD_PCT,
    MAX_SLOTS,
    TRADING_DAYS_PER_YEAR,
)
from value.engine import (
    bias_series,
    build_pool,
    buy_signals,
    cooldown_active,
    is_pool_rebuild_day,
    sell_signals,
    z_series,
)


def _metrics(history: list[dict], trade_log: list[dict], dates: list[str]) -> dict:
    eq = [h["equity"] for h in history]
    rets = [(eq[i] / eq[i - 1] - 1) for i in range(1, len(eq)) if eq[i - 1] > 0]
    n_years = len(eq) / TRADING_DAYS_PER_YEAR if eq else 0
    total_ret = (eq[-1] / eq[0] - 1) if eq else 0.0
    ann = ((eq[-1] / eq[0]) ** (1 / n_years) - 1) if eq and n_years > 0 and eq[0] > 0 else 0.0
    mean = sum(rets) / len(rets) if rets else 0.0
    var = sum((r - mean) ** 2 for r in rets) / len(rets) if rets else 0.0
    std = var**0.5
    sharpe = (mean / std * (TRADING_DAYS_PER_YEAR**0.5)) if std > 0 else 0.0
    peak = eq[0] if eq else 1.0
    maxdd = 0.0
    for v in eq:
        peak = max(peak, v)
        maxdd = min(maxdd, v / peak - 1)
    rounds = [t for t in trade_log if t["action"] == "SELL"]
    wins = sum(1 for t in rounds if t.get("pnl_pct", 0) > 0)
    util = sum(h["position_pct"] for h in history) / len(history) if history else 0.0
    return {
        "total_return_pct": round(total_ret * 100, 2),
        "annual_return_pct": round(ann * 100, 2),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(maxdd * 100, 2),
        "calmar": round(ann / abs(maxdd), 2) if maxdd < 0 else 0.0,
        "win_rate_pct": round(wins / len(rounds) * 100, 1) if rounds else 0.0,
        "round_count": len(rounds),
        "trade_count": len(trade_log),
        "trades_per_year": round(len(trade_log) / n_years, 1) if n_years > 0 else 0.0,
        "utilization_pct": round(util, 1),
    }


def run_backtest(
    dates: list[str],
    kline: dict[str, dict[str, dict]],
    snapshots: dict[str, dict],
    pe_series: dict[str, dict[str, float]] | None = None,
    idle_yield_pct: float = IDLE_YIELD_PCT,
    slot_divisor: int = MAX_SLOTS,
) -> dict:
    """逐日推进回测。kline: {code: {date: {"open","close"}}}; snapshots 含分红/ROE/行业/总股本。
    slot_divisor: 单票金额除数(组合=5 等权; 个股独立视角=1 全仓)。"""
    pe_series = pe_series or {}
    # 指标序列按 dates 对齐: 停牌日用前收盘填充(价格不变, 指标连续), 上市前为 None(暖机期)
    bias_by_code: dict[str, dict[str, float]] = {}
    z_by_code: dict[str, dict[str, float]] = {}
    cf_close: dict[str, dict[str, float]] = {}  # 前收盘填充表(停牌日估值用)
    for c, kl in kline.items():
        cs: list[float | None] = []
        last: float | None = None
        for d in dates:
            row = kl.get(d)
            if row is not None:
                last = row["close"]
            cs.append(last)
        bs = bias_series(cs)
        zs = z_series(bs)
        bias_by_code[c] = {d: b for d, b in zip(dates, bs) if b is not None}
        z_by_code[c] = {d: z for d, z in zip(dates, zs) if z is not None}
        cf_close[c] = {d: v for d, v in zip(dates, cs) if v is not None}

    daily_factor = (1 + idle_yield_pct / 100) ** (1 / TRADING_DAYS_PER_YEAR) if idle_yield_pct else 1.0

    cash = 1.0
    positions: dict[str, dict] = {}
    cooldown_until: dict[str, int] = {}
    pending: list[dict] = []
    history: list[dict] = []
    trade_log: list[dict] = []
    pool: list[dict] = []
    pool_rebuilds: list[dict] = []

    def snap_as_of(code: str, d: str) -> dict:
        kl = kline[code]
        price = kl[d]["close"] if d in kl else None
        shares_yi = snapshots[code].get("total_shares_yi") or 0
        return {
            **snapshots[code],
            "price": price,
            "mcap_yi": price * shares_yi if price else None,
            "pe_ttm": pe_series.get(code, {}).get(d),
        }

    for idx, d in enumerate(dates):
        # ---- 季度首日重建池(as-of 防前视) ----
        if is_pool_rebuild_day(dates, idx):
            pool = build_pool([snap_as_of(c, d) for c in snapshots if d in kline[c]], d)
            pool_rebuilds.append({"date": d, "codes": [p["code"] for p in pool]})

        # ---- 次日开盘执行 pending(先卖后买) ----
        due = [ev for ev in pending if ev["exec_date"] == d]
        pending = [ev for ev in pending if ev["exec_date"] != d]
        for ev in sorted(due, key=lambda e: 0 if e["action"] == "SELL" else 1):
            code = ev["code"]
            open_px = kline[code][d]["open"] if d in kline[code] else None
            if open_px is None or open_px <= 0:
                continue
            if ev["action"] == "SELL":
                pos = positions.pop(code, None)
                if not pos:
                    continue
                cash += pos["shares"] * open_px
                if ev["rule"] == 1:
                    cooldown_until[code] = idx + COOLDOWN_DAYS
                trade_log.append(
                    {
                        **ev,
                        "name": snapshots.get(code, {}).get("name", ""),
                        "price": open_px,
                        "shares": pos["shares"],
                        "amount": pos["shares"] * open_px,
                    }
                )
            else:
                amount = min(ev["amount"], cash)
                if amount <= 0:
                    continue
                shares = amount / open_px
                cash -= amount
                positions[code] = {"shares": shares, "cost": open_px, "buy_date": d}
                trade_log.append(
                    {
                        **ev,
                        "name": snapshots.get(code, {}).get("name", ""),
                        "price": open_px,
                        "shares": shares,
                        "amount": amount,
                    }
                )

        # ---- 闲置现金货币化(日复利) ----
        if daily_factor != 1.0:
            cash *= daily_factor

        # ---- 收盘估值 ----
        equity = cash
        for c, p in positions.items():
            px = cf_close.get(c, {}).get(d) or p["cost"]  # 停牌日按最后收盘估值, 非成本价
            equity += p["shares"] * px
        invested = equity - cash
        history.append(
            {
                "date": d,
                "equity": round(equity, 6),
                "position_pct": round(invested / equity * 100, 1) if equity > 0 else 0.0,
                "cash": round(cash, 6),
            }
        )

        # ---- 收盘信号 → 次日开盘执行 ----
        pool_codes = {p["code"] for p in pool}
        quotes: dict[str, dict] = {}
        for c in pool_codes | set(positions):
            if d not in kline[c] or idx == 0 or dates[idx - 1] not in kline[c]:
                continue
            quotes[c] = {
                "code": c,
                "close": kline[c][d]["close"],
                "prev_close": kline[c][dates[idx - 1]]["close"],
                "bias": bias_by_code[c].get(d),
                "z": z_by_code[c].get(d),
                "pe_ttm": pe_series.get(c, {}).get(d),
                "industry": snapshots[c].get("industry", ""),
            }

        sells = sell_signals(positions, quotes)
        for s in sells:
            pending.append(
                {
                    "date": d,
                    "exec_date": dates[idx + 1] if idx + 1 < len(dates) else None,
                    "code": s["code"],
                    "action": "SELL",
                    "rule": s["rule"],
                    "reason": s["reason"],
                    "pnl_pct": round((quotes[s["code"]]["close"] / positions[s["code"]]["cost"] - 1) * 100, 2),
                }
            )
        selling = {s["code"] for s in sells}
        free_slots = MAX_SLOTS - len(positions) + len(selling)
        cd_set = {c for c in cooldown_until if cooldown_active(cooldown_until, c, idx)}
        candidates = [q for c, q in quotes.items() if c in pool_codes and c not in positions and c not in selling]
        for b in buy_signals(candidates, free_slots, cd_set):
            pending.append(
                {
                    "date": d,
                    "exec_date": dates[idx + 1] if idx + 1 < len(dates) else None,
                    "code": b["code"],
                    "action": "BUY",
                    "kind": b["kind"],
                    "reason": b["reason"],
                    "amount": equity / slot_divisor,  # 单票 = 总资产 ÷ 档位数
                }
            )
        pending = [ev for ev in pending if ev["exec_date"] is not None]

    return {
        "history": history,
        "trade_log": trade_log,
        "metrics": _metrics(history, trade_log, dates),
        "pool_rebuilds": pool_rebuilds,
        "final": {
            "positions": {c: {**p} for c, p in positions.items()},
            "cooldown_until": dict(cooldown_until),
            "pool": pool,
            "cash": cash,
        },
    }
