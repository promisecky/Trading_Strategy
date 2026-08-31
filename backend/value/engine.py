"""价值回归策略引擎 — 纯函数, 数据注入, 无 I/O。

第一性原理设计:
- 所有基本面记录带生效日(分红=预案公告日/除权日, ROE=披露日), as-of 过滤杜绝前视;
- 指标(bias/z)仅用历史收盘价滚动计算, 含完整暖机期;
- 买卖判定与仓位军规分离为独立纯函数, 便于对抗性单测。

数据结构约定:
  dividend: {"fiscal_year": int, "dps": float, "announce_date": str, "ex_date": str}
  roe:      {"fiscal_year": int, "roe": float, "disclose_date": str}
  snapshot: {"code","name","industry","total_shares_yi" 无关—市值直接注入,
             "mcap_yi","pe_ttm","price","dividends":[...],"roes":[...]}
  quote:    {"code","close","prev_close","bias","z","pe_ttm","industry"}
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta

from value.config import (
    BIAS_MAIN,
    BIAS_SIDE_HI,
    BIAS_SIDE_LO,
    COOLDOWN_DAYS,  # noqa: F401  (re-export for backtest/tests)
    DIV_MIN_LAST_RATIO,
    DIV_YEARS,
    DY_LOOKBACK_DAYS,
    DY_MIN_PCT,
    IND_MIN_COUNT,
    MA_WINDOW,
    MCAP_MIN_YI,
    PE_IND_MULT,
    PE_MAX,
    PE_MIN,
    REAL_ESTATE_KEYWORDS,
    ROE_MIN_PCT,
    ROE_YEARS,
    STOP_LOSS,  # noqa: F401
    TAKE_PROFIT,  # noqa: F401
    BIAS_EXIT,  # noqa: F401
    Z_SIDE,
    Z_WINDOW,
)


# ---------------------------------------------------------------- 指标


def bias_series(closes: list[float | None]) -> list[float | None]:
    """bias = 收盘 ÷ MA120; 不足 MA_WINDOW 点或窗口含缺失值返回 None(暖机期/停牌)。
    滑动和实现, O(n)。"""
    n = len(closes)
    out: list[float | None] = [None] * n
    if n < MA_WINDOW:
        return out
    window_sum = 0.0
    missing = 0
    for i in range(n):
        v = closes[i]
        if v is None:
            missing += 1
        else:
            window_sum += v
        if i >= MA_WINDOW:
            old = closes[i - MA_WINDOW]
            if old is None:
                missing -= 1
            else:
                window_sum -= old
        if i + 1 >= MA_WINDOW:
            if missing > 0 or v is None:
                continue
            ma = window_sum / MA_WINDOW
            out[i] = v / ma if ma else None
    return out


def z_series(bias: list[float | None]) -> list[float | None]:
    """z = (bias − bias 的 250 日均值) ÷ 250 日标准差; 窗口需满 250 个非 None。滑动和实现, O(n)。"""
    n = len(bias)
    out: list[float | None] = [None] * n
    if n < Z_WINDOW:
        return out
    wsum = 0.0
    wsum2 = 0.0
    missing = 0
    for i in range(n):
        v = bias[i]
        if v is None:
            missing += 1
        else:
            wsum += v
            wsum2 += v * v
        if i >= Z_WINDOW:
            old = bias[i - Z_WINDOW]
            if old is None:
                missing -= 1
            else:
                wsum -= old
                wsum2 -= old * old
        if i + 1 >= Z_WINDOW and missing == 0 and v is not None:
            mean = wsum / Z_WINDOW
            var = wsum2 / Z_WINDOW - mean * mean
            std = math.sqrt(var) if var > 0 else 0.0
            out[i] = (v - mean) / std if std > 0 else None
    return out


# ---------------------------------------------------------------- 七道安检门


def _effective_dividends(dividends: list[dict], as_of: str) -> list[dict]:
    """仅计入预案公告日 ≤ as_of 的分红记录(防前视)。"""
    return [d for d in dividends if d.get("announce_date") and d["announce_date"] <= as_of and (d.get("dps") or 0) > 0]


def _effective_roes(roes: list[dict], as_of: str) -> list[dict]:
    """仅计入披露日 ≤ as_of 的 ROE 记录(防前视)。"""
    return [r for r in roes if r.get("disclose_date") and r["disclose_date"] <= as_of and r.get("roe") is not None]


def check_gates(snap: dict, as_of: str) -> list[dict]:
    """七道安检门逐条判定, 返回 [{gate, passed, reason}], 全部通过方可入池。"""
    gates: list[dict] = []

    # 1 连续分红: 最近 5 个已生效财年每年有分红 + 最新一年 ≥ 上年 50%
    divs = _effective_dividends(snap.get("dividends", []), as_of)
    by_fy: dict[int, float] = {}
    for d in divs:
        by_fy[d["fiscal_year"]] = by_fy.get(d["fiscal_year"], 0.0) + d["dps"]
    latest_fy = max(by_fy) if by_fy else None
    required = list(range(latest_fy - DIV_YEARS + 1, latest_fy + 1)) if latest_fy else []
    missing = [fy for fy in required if fy not in by_fy]
    ratio_ok = (
        latest_fy is not None
        and (latest_fy - 1) in by_fy
        and by_fy[latest_fy] >= by_fy[latest_fy - 1] * DIV_MIN_LAST_RATIO
    )
    passed = latest_fy is not None and not missing and ratio_ok
    reason = (
        f"近{DIV_YEARS}生效财年({required[0]}~{required[-1]})均分红且最新≥上年50%"
        if passed
        else (f"缺分红财年 {missing}" if missing else "最新一年分红<上年50%或无生效分红")
    )
    gates.append({"gate": "连续分红", "passed": passed, "reason": reason})

    # 2 盈利且不贵: 0 < PE(TTM) ≤ 20
    pe = snap.get("pe_ttm")
    passed = pe is not None and PE_MIN < pe <= PE_MAX
    gates.append(
        {"gate": "盈利且不贵", "passed": passed, "reason": f"PE(TTM)={pe if pe is not None else '缺'}需在(0,{PE_MAX}]"}
    )

    # 3 股息托底: 近 365 天除权分红总额 ÷ 当前价 > 3%
    price = snap.get("price") or 0
    cutoff = (datetime.strptime(as_of, "%Y-%m-%d") - timedelta(days=DY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    dy_sum = sum(
        d["dps"]
        for d in snap.get("dividends", [])
        if d.get("ex_date") and cutoff < d["ex_date"] <= as_of and (d.get("dps") or 0) > 0
    )
    dy = (dy_sum / price * 100) if price > 0 else 0.0
    passed = dy > DY_MIN_PCT
    gates.append({"gate": "股息托底", "passed": passed, "reason": f"滚动一年股息率 {dy:.2f}% 需 > {DY_MIN_PCT}%"})

    # 4 大盘股: 总市值 ≥ 300 亿
    mcap = snap.get("mcap_yi")
    passed = mcap is not None and mcap >= MCAP_MIN_YI
    gates.append({"gate": "大盘股", "passed": passed, "reason": f"总市值 {mcap if mcap is not None else '缺'} 亿 ≥ {MCAP_MIN_YI}亿"})

    # 5 非地产
    industry = snap.get("industry") or ""
    passed = not any(k in industry for k in REAL_ESTATE_KEYWORDS)
    gates.append({"gate": "非地产", "passed": passed, "reason": f"行业={industry or '缺'}"})

    # 6 盈利稳定: 最近 5 个已披露财年每年 ROE ≥ 10%
    roes = _effective_roes(snap.get("roes", []), as_of)
    r_by_fy = {r["fiscal_year"]: r["roe"] for r in roes}
    r_latest = max(r_by_fy) if r_by_fy else None
    r_required = list(range(r_latest - ROE_YEARS + 1, r_latest + 1)) if r_latest else []
    r_missing = [fy for fy in r_required if fy not in r_by_fy]
    r_low = [fy for fy in r_required if fy in r_by_fy and r_by_fy[fy] < ROE_MIN_PCT]
    passed = r_latest is not None and not r_missing and not r_low
    reason = (
        f"近{ROE_YEARS}披露财年({r_required[0]}~{r_required[-1]})ROE均≥{ROE_MIN_PCT}%"
        if passed
        else (f"缺ROE财年 {r_missing}" if r_missing else f"ROE<10% 财年 {r_low}")
    )
    gates.append({"gate": "盈利稳定", "passed": passed, "reason": reason})

    return gates


def build_pool(stocks: list[dict], as_of: str) -> list[dict]:
    """全过门入池, 返回 [{code, name, industry, gates}]。"""
    pool = []
    for snap in stocks:
        gates = check_gates(snap, as_of)
        if all(g["passed"] for g in gates):
            pool.append(
                {
                    "code": snap["code"],
                    "name": snap.get("name", snap["code"]),
                    "industry": snap.get("industry", ""),
                    "gates": gates,
                }
            )
    return sorted(pool, key=lambda p: p["code"])


# ---------------------------------------------------------------- 买卖信号


def _industry_pe_medians(quotes: list[dict]) -> dict[str, float]:
    """行业内正 PE 中位数; 行业样本 < IND_MIN_COUNT 不出中位数(跳过该闸)。"""
    groups: dict[str, list[float]] = {}
    for q in quotes:
        pe = q.get("pe_ttm")
        ind = q.get("industry") or ""
        if pe is not None and pe > 0 and ind:
            groups.setdefault(ind, []).append(pe)
    return {ind: statistics.median(v) for ind, v in groups.items() if len(v) >= IND_MIN_COUNT}


def sell_signals(positions: dict[str, dict], quotes_by_code: dict[str, dict]) -> list[dict]:
    """持仓每日收盘检查, 按 ①止损→②止盈→③bias 优先级触发其一。

    positions: {code: {"cost": float, ...}}; 返回 [{code, rule, reason}]。
    """
    out = []
    for code, pos in positions.items():
        q = quotes_by_code.get(code)
        cost = pos.get("cost")
        if q is None or not cost:
            continue
        close = q["close"]
        if close <= cost * STOP_LOSS:
            out.append({"code": code, "rule": 1, "reason": f"①止损: 收盘{close:.2f} ≤ 成本{cost:.2f}×{STOP_LOSS}"})
        elif close >= cost * TAKE_PROFIT:
            out.append({"code": code, "rule": 2, "reason": f"②止盈: 收盘{close:.2f} ≥ 成本{cost:.2f}×{TAKE_PROFIT}"})
        elif q.get("bias") is not None and q["bias"] >= BIAS_EXIT:
            out.append({"code": code, "rule": 3, "reason": f"③情绪修复: bias {q['bias']:.3f} ≥ {BIAS_EXIT}"})
    return out


def buy_signals(
    pool_quotes: list[dict],
    free_slots: int,
    cooldown_codes: set[str],
) -> list[dict]:
    """买入候选: 主信号优先于侧门; 同类永远 bias 最小优先; 满仓/冷却忽略。

    返回 [{code, kind, bias, reason}], 长度 ≤ free_slots。
    """
    if free_slots <= 0:
        return []
    medians = _industry_pe_medians(pool_quotes)

    def eligible(q: dict) -> bool:
        return q["code"] not in cooldown_codes and q.get("bias") is not None

    def pe_gate_ok(q: dict) -> bool:
        ind = q.get("industry") or ""
        med = medians.get(ind)
        if med is None:
            return True  # 行业不足 3 只跳过该闸
        pe = q.get("pe_ttm")
        return pe is not None and pe < med * PE_IND_MULT

    mains = [
        q
        for q in pool_quotes
        if eligible(q)
        and q["bias"] <= BIAS_MAIN
        and q["close"] > q["prev_close"]
        and pe_gate_ok(q)
    ]
    mains.sort(key=lambda q: q["bias"])
    picks = [
        {"code": q["code"], "kind": "main", "bias": q["bias"], "reason": f"主信号: bias {q['bias']:.3f} ≤ {BIAS_MAIN} 且企稳"}
        for q in mains[:free_slots]
    ]

    # 侧门: 仅当主信号不够(无主候选)且有空槽
    if not picks:
        sides = [
            q
            for q in pool_quotes
            if eligible(q)
            and BIAS_SIDE_LO < q["bias"] <= BIAS_SIDE_HI
            and q.get("z") is not None
            and q["z"] <= Z_SIDE
            and q["close"] > q["prev_close"]
        ]
        sides.sort(key=lambda q: q["bias"])
        picks = [
            {"code": q["code"], "kind": "side", "bias": q["bias"], "reason": f"侧门: bias {q['bias']:.3f} + z {q['z']:.2f}"}
            for q in sides[:free_slots]
        ]
    return picks


def is_pool_rebuild_day(dates: list[str], idx: int) -> bool:
    """季度首日: 1/4/7/10 月的第一个交易日。"""
    month = int(dates[idx][5:7])
    if month not in (1, 4, 7, 10):
        return False
    return idx == 0 or int(dates[idx - 1][5:7]) != month


def cooldown_active(cooldown_until_idx: dict[str, int], code: str, idx: int) -> bool:
    """冷却期内(idx ≤ 止损日+60 个交易日)该股信号一律忽略。"""
    return idx <= cooldown_until_idx.get(code, -1)
