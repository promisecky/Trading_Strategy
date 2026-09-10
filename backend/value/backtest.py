"""价值回归独立回测器 — 第一性原理实现, 不复用 portfolio/simulator 的再平衡模型。

价格双空间(v3, 与存储层 RAW+HFQ 双价一致):
- HFQ(后复权)空间: bias/z/水位指标、买卖信号、成本、盈亏、净值估值 — 收益率口径,
  分红已折入价格(等价红利再投资), 序列在除权日连续, 且锚点稳定不受日更追加影响;
- RAW(不复权)空间: 基本面门(股息率分母/市值乘数)、涨跌停判定、成交金额展示 —
  真实成交价口径, 与交易所/东财 PE 口径一致。

军规硬约束(与文档逐条对应):
- 资金分 5 档, 单票金额 = 信号日收盘总资产 ÷ 5, 一次性买满槽位, 不分批;
- 满仓出新信号直接放弃, 不换仓; 卖出后不再平衡, 回款闲置;
- 收盘确认信号 → 次日开盘价成交(开盘竞价成交, 不计滑点);
- 止损(①)后该股冷却 COOLDOWN_DAYS 个交易日, 期间信号一律忽略;
- 池于每季度首日(1/4/7/10 月第一个交易日)以 as-of 数据重建, 防前视;
- 闲置现金可选按货币基金实际日收益(HFQ 收盘比)或固定年化和计息。

回测 realism(v3 新增):
- 交易成本: 佣金双边 COST_COMMISSION_BP + 印花税卖方(2023-08-28 前 10bp/后 5bp);
- 涨跌停约束: 一字跌停无法卖出 → 顺延至后续交易日重试; 一字涨停无法买入 → 放弃;
- pnl_pct 为实现盈亏(实际执行开盘价、扣费净额 ÷ 买入现金), 非信号日收盘目标值;
- 夏普扣除无风险利率(RISK_FREE_PCT 年化);
- 暖机: dates 传全量日历(含 start 之前的指标暖机段), 交易与净值自 start 起;
- 停牌: 卖出检查只需当日有行情(昨收用停牌前最后收盘 carry-forward), 复牌首日即恢复检查。

近似项(文档化):
- 市值 as-of = 当日 RAW 收盘 × 当前总股本(股本变动稀薄, 忽略历史股本差异);
- ROE 披露日缺失时用法定截止日(年报 04-30), 见 fetch 层;
- 宇宙预筛用当前快照 → 幸存者偏差无法离线消除, 结论中声明;
- 池重建用当日收盘(与实盘 17:00 重建任务同一信息集, 信号亦在收盘确认, 非前视);
- ST 股 5% 涨跌幅未建模(候选宇宙为市值≥300亿白马, 触及概率极低);
- 停牌复牌日若恰逢除权, 涨跌停基准未按交易所除权调整(罕见, 影响一日判定)。
"""

from __future__ import annotations

from value.config import (
    COST_COMMISSION_BP,
    COOLDOWN_DAYS,
    GEM_20PCT_DATE,
    IDLE_YIELD_PCT,
    LIMIT_RATE_20,
    LIMIT_RATE_MAIN,
    MAX_SLOTS,
    RISK_FREE_PCT,
    STAMP_TAX_BP_LEGACY,
    STAMP_TAX_BP_NEW,
    STAMP_TAX_CUT_DATE,
    TRADING_DAYS_PER_YEAR,
)
from value.engine import (
    activity_series,
    bias_series,
    build_pool,
    buy_signals,
    cooldown_active,
    is_pool_rebuild_day,
    sell_signals,
    z_series,
)

_EPS = 1e-9


def _sell_cost_rate(d: str) -> float:
    """卖出费率 = 佣金 + 印花税(2023-08-28 起减半)。"""
    stamp = STAMP_TAX_BP_NEW if d >= STAMP_TAX_CUT_DATE else STAMP_TAX_BP_LEGACY
    return (COST_COMMISSION_BP + stamp) / 1e4


def _buy_cost_rate() -> float:
    return COST_COMMISSION_BP / 1e4


def _limit_rate(code: str, d: str) -> float:
    """涨跌停幅度: 科创板开板即 20%; 创业板 2020-08-24 注册制起 20%; 其余主板 10%。"""
    if code.startswith(("688", "689")):
        return LIMIT_RATE_20
    if code.startswith(("300", "301")):
        return LIMIT_RATE_20 if d >= GEM_20PCT_DATE else LIMIT_RATE_MAIN
    return LIMIT_RATE_MAIN


def _sell_blocked(code: str, d: str, row: dict, prev_raw_close: float | None) -> bool:
    """一字跌停无法卖出: 开盘触跌停且全天未开板(high 亦 ≤ 跌停价)。
    缺 high/low 数据时退化为仅开盘价判定(保守)。"""
    if prev_raw_close is None or row.get("open") is None:
        return False
    limit_down = round(prev_raw_close * (1 - _limit_rate(code, d)), 2)
    if row["open"] > limit_down + _EPS:
        return False
    hi = row.get("high")
    return True if hi is None else hi <= limit_down + _EPS


def _buy_blocked(code: str, d: str, row: dict, prev_raw_close: float | None) -> bool:
    """一字涨停无法买入: 开盘触涨停且全天未开板(low 亦 ≥ 涨停价)。"""
    if prev_raw_close is None or row.get("open") is None:
        return False
    limit_up = round(prev_raw_close * (1 + _limit_rate(code, d)), 2)
    if row["open"] < limit_up - _EPS:
        return False
    lo = row.get("low")
    return True if lo is None else lo >= limit_up - _EPS


def _metrics(history: list[dict], trade_log: list[dict], dates: list[str], risk_free_pct: float) -> dict:
    eq = [h["equity"] for h in history]
    rf_d = risk_free_pct / 100 / TRADING_DAYS_PER_YEAR
    rets = [(eq[i] / eq[i - 1] - 1) for i in range(1, len(eq)) if eq[i - 1] > 0]
    excess = [r - rf_d for r in rets]
    n_years = len(eq) / TRADING_DAYS_PER_YEAR if eq else 0
    total_ret = (eq[-1] / eq[0] - 1) if eq else 0.0
    ann = ((eq[-1] / eq[0]) ** (1 / n_years) - 1) if eq and n_years > 0 and eq[0] > 0 else 0.0
    mean = sum(excess) / len(excess) if excess else 0.0
    var = sum((r - mean) ** 2 for r in excess) / len(excess) if excess else 0.0
    std = var**0.5
    sharpe = (mean / std * (TRADING_DAYS_PER_YEAR**0.5)) if std > 0 else 0.0
    peak = eq[0] if eq else 1.0
    maxdd = 0.0
    for v in eq:
        peak = max(peak, v)
        maxdd = min(maxdd, v / peak - 1)
    rounds = [t for t in trade_log if t["action"] == "SELL"]
    wins = sum(1 for t in rounds if (t.get("pnl_pct") or 0) > 0)
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
    idle_close: dict[str, float] | None = None,
    start: str | None = None,
    risk_free_pct: float = RISK_FREE_PCT,
) -> dict:
    """逐日推进回测。

    dates: 全量交易日历(含暖机段, 指标在此全轴上计算 — 修复旧版丢弃暖机的缺陷);
    start: 交易/净值起点(缺省=dates[0]); 早于 start 的日子只参与指标与池暖机, 不交易不记净值;
    kline: {code: {date: {"open","close","high","low","aopen","aclose","ahigh","alow"}}} —
      RAW 四价 + HFQ 四价; 缺 HFQ 的行视同停牌(不参与指标/交易);
    snapshots 含分红/ROE/行业/总股本; idle_close: 货币基金 HFQ 收盘 {date: aclose}。
    slot_divisor: 单票金额除数(组合=5 等权; 个股独立视角=1 全仓)。
    """
    pe_series = pe_series or {}
    n_dates = len(dates)
    start_idx = 0
    if start:
        start_idx = next((i for i, d in enumerate(dates) if d >= start), n_dates)

    # ---- 指标与 carry-forward 序列按全量 dates 对齐 ----
    # 停牌日: HFQ 收盘前值填充(价格不变, 指标连续); 上市前为 None(暖机期)
    bias_by_code: dict[str, list[float | None]] = {}
    z_by_code: dict[str, list[float | None]] = {}
    act_by_code: dict[str, list[float | None]] = {}
    cf_adj_close: dict[str, list[float | None]] = {}  # 当日或之前最后 HFQ 收盘(估值/信号)
    prev_adj_close: dict[str, list[float | None]] = {}  # 严格早于当日的最后 HFQ 收盘(企稳判定/停牌恢复)
    prev_raw_close: dict[str, list[float | None]] = {}  # 严格早于当日的最后 RAW 收盘(涨跌停基准)
    for c, kl in kline.items():
        rng: list[float | None] = []
        cfa: list[float | None] = []
        pra: list[float | None] = []
        prr: list[float | None] = []
        last_a: float | None = None
        last_r: float | None = None
        for d in dates:
            row = kl.get(d)
            pra.append(last_a)
            prr.append(last_r)
            if row is not None and row.get("aclose"):
                last_a = row["aclose"]
                rng.append((row["ahigh"] - row["alow"]) / row["aclose"] if row.get("ahigh") else None)
            else:
                rng.append(None)
            if row is not None and row.get("close"):
                last_r = row["close"]
            cfa.append(last_a)
        bias_by_code[c] = bias_series(cfa)  # bias 用 carry-forward 收盘(停牌冻结价格)
        z_by_code[c] = z_series(bias_by_code[c])
        act_by_code[c] = activity_series(rng)
        cf_adj_close[c] = cfa
        prev_adj_close[c] = pra
        prev_raw_close[c] = prr

    daily_factor = (1 + idle_yield_pct / 100) ** (1 / TRADING_DAYS_PER_YEAR) if idle_yield_pct else 1.0

    cash = 1.0
    positions: dict[str, dict] = {}
    cooldown_until: dict[str, int] = {}
    pending: list[dict] = []
    history: list[dict] = []
    trade_log: list[dict] = []
    blocked_log: list[dict] = []
    pool: list[dict] = []
    pool_rebuilds: list[dict] = []

    def snap_as_of(code: str, d: str) -> dict | None:
        """基本面门快照 — 价格/市值一律 RAW 口径(修复旧版用前复权导致的门槛跨期漂移)。"""
        row = kline[code].get(d)
        price = row["close"] if row and row.get("close") else None
        if price is None:
            return None  # 当日停牌: 不参与本次重建判定
        shares_yi = snapshots[code].get("total_shares_yi") or 0
        return {
            **snapshots[code],
            "price": price,
            "mcap_yi": price * shares_yi,
            "pe_ttm": pe_series.get(code, {}).get(d),
        }

    for idx in range(start_idx, n_dates):
        d = dates[idx]
        next_d = dates[idx + 1] if idx + 1 < n_dates else None

        # ---- 季度首日重建池(as-of 防前视; RAW 口径) ----
        if is_pool_rebuild_day(dates, idx):
            snaps = [s for s in (snap_as_of(c, d) for c in snapshots if c in kline) if s is not None]
            pool = build_pool(snaps, d)
            pool_rebuilds.append({"date": d, "codes": [p["code"] for p in pool]})

        # ---- 执行 pending(先卖后买; 跌停卖顺延/涨停买放弃) ----
        due = [ev for ev in pending if ev["exec_date"] == d]
        pending = [ev for ev in pending if ev["exec_date"] != d]
        for ev in sorted(due, key=lambda e: 0 if e["action"] == "SELL" else 1):
            code = ev["code"]
            row = kline[code].get(d)
            tradable = row is not None and row.get("open") and row.get("aopen")
            if ev["action"] == "SELL":
                pos = positions.get(code)
                if not pos:
                    continue
                if not tradable:  # 停牌 → 顺延复牌再卖
                    if next_d:
                        ev["exec_date"] = next_d
                        pending.append(ev)
                    continue
                if _sell_blocked(code, d, row, prev_raw_close[code][idx]):
                    blocked_log.append({"date": d, "code": code, "action": "SELL", "why": "一字跌停无法卖出, 顺延"})
                    if next_d:
                        ev["exec_date"] = next_d
                        pending.append(ev)
                    continue
                positions.pop(code, None)
                adj_open = row["aopen"]
                gross = pos["adj_shares"] * adj_open
                net = gross * (1 - _sell_cost_rate(d))
                cash += net
                pnl_pct = round((net / pos["cash_in"] - 1) * 100, 2) if pos["cash_in"] > 0 else 0.0  # 实现盈亏(净额口径)
                if ev["rule"] == 1:
                    cooldown_until[code] = idx + COOLDOWN_DAYS
                trade_log.append(
                    {
                        **{k: v for k, v in ev.items() if k != "exec_date"},
                        "exec_date": d,
                        "name": snapshots.get(code, {}).get("name", ""),
                        "price": row["open"],  # RAW 成交价(真实金额口径)
                        "shares": pos["raw_shares"],
                        "amount": round(pos["raw_shares"] * row["open"], 2),
                        "net_proceeds": round(net, 6),
                        "pnl_pct": pnl_pct,
                    }
                )
            else:  # BUY
                if code in positions:
                    continue
                if not tradable:  # 停牌 → 委托当日失效, 放弃(不追复牌价)
                    blocked_log.append({"date": d, "code": code, "action": "BUY", "why": "执行日停牌, 放弃"})
                    continue
                if _buy_blocked(code, d, row, prev_raw_close[code][idx]):
                    blocked_log.append({"date": d, "code": code, "action": "BUY", "why": "一字涨停无法买入, 放弃"})
                    continue
                amount = min(ev["amount"], cash)
                if amount <= 0:
                    continue
                net_invest = amount * (1 - _buy_cost_rate())
                adj_open, raw_open = row["aopen"], row["open"]
                adj_shares = net_invest / adj_open
                raw_shares = net_invest / raw_open
                cash -= amount
                positions[code] = {
                    "adj_shares": adj_shares,
                    "raw_shares": raw_shares,
                    "cost": adj_open,  # 规则基准成本(HFQ 空间)
                    "cost_raw": raw_open,
                    "cash_in": amount,
                    "buy_date": d,
                }
                trade_log.append(
                    {
                        **{k: v for k, v in ev.items() if k != "exec_date"},
                        "exec_date": d,
                        "name": snapshots.get(code, {}).get("name", ""),
                        "price": raw_open,
                        "shares": raw_shares,
                        "amount": round(amount, 6),  # 实际现金支出(含佣金)
                    }
                )

        # ---- 闲置现金货币化: 基金 HFQ 实际日收益优先, 缺失日回退固定年化 ----
        grew = False
        if idle_close:
            cur_c = idle_close.get(d)
            prev_c = idle_close.get(dates[idx - 1]) if idx > 0 else None
            if cur_c and prev_c and prev_c > 0:
                cash *= cur_c / prev_c
                grew = True
        if not grew and daily_factor != 1.0:
            cash *= daily_factor

        # ---- 收盘估值(HFQ 空间; 停牌日按最后收盘估值) ----
        equity = cash
        for c, p in positions.items():
            px = cf_adj_close[c][idx] or p["cost"]
            equity += p["adj_shares"] * px
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
        pending_sell_codes = {ev["code"] for ev in pending if ev["action"] == "SELL"}
        quotes: dict[str, dict] = {}
        for c in pool_codes | set(positions):
            row = kline[c].get(d)
            if row is None or not row.get("aclose"):
                continue  # 当日停牌: 不产生信号(卖出检查待复牌日, prev_close 用 carry-forward)
            quotes[c] = {
                "code": c,
                "close": row["aclose"],  # 信号空间 = HFQ
                "prev_close": prev_adj_close[c][idx],  # 停牌恢复后=停牌前最后收盘(D4 修复)
                "bias": bias_by_code[c][idx],
                "z": z_by_code[c][idx],
                "act": act_by_code[c][idx],
                "pe_ttm": pe_series.get(c, {}).get(d),
                "industry": snapshots[c].get("industry", ""),
            }

        # 已有在途卖单的持仓不重复出信号(跌停顺延中的仓位次日由 pending 继续处理)
        open_positions = {c: p for c, p in positions.items() if c not in pending_sell_codes}
        sells = sell_signals(open_positions, quotes)
        for s in sells:
            if next_d is None:
                break
            pending.append(
                {
                    "date": d,
                    "exec_date": next_d,
                    "code": s["code"],
                    "action": "SELL",
                    "rule": s["rule"],
                    "reason": s["reason"],
                }
            )
        selling = {s["code"] for s in sells}
        # 槽位: 在途卖单(含跌停顺延)视为即将腾出的槽; 现金约束在执行时兜底
        free_slots = MAX_SLOTS - len(positions) + len(pending_sell_codes | selling)
        cd_set = {c for c in cooldown_until if cooldown_active(cooldown_until, c, idx)}
        candidates = [
            q for c, q in quotes.items()
            if c in pool_codes and c not in positions and c not in cd_set and q.get("prev_close") is not None
        ]
        for b in buy_signals(candidates, free_slots, cd_set):
            if next_d is None:
                break
            pending.append(
                {
                    "date": d,
                    "exec_date": next_d,
                    "code": b["code"],
                    "action": "BUY",
                    "kind": b["kind"],
                    "reason": b["reason"],
                    "amount": equity / slot_divisor,  # 单票 = 总资产 ÷ 档位数
                }
            )

    return {
        "history": history,
        "trade_log": trade_log,
        "metrics": _metrics(history, trade_log, dates, risk_free_pct),
        "pool_rebuilds": pool_rebuilds,
        "blocked": blocked_log,
        "final": {
            "positions": {c: {**p} for c, p in positions.items()},
            "cooldown_until": dict(cooldown_until),
            "pool": pool,
            "cash": cash,
        },
    }
