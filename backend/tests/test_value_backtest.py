"""价值回归回测器对抗性测试: 次日开盘成交/槽位军规/冷却/季度重建/闲钱收益/指标。"""

from __future__ import annotations

from datetime import datetime, timedelta

from value.backtest import run_backtest

N = 320


def biz_dates(start: str = "2018-01-01", n: int = N) -> list[str]:
    d = datetime.strptime(start, "%Y-%m-%d")
    out: list[str] = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def kline_from(closes: list[float], opens: dict[int, float] | None = None) -> dict[str, dict]:
    out = {}
    for i, d in enumerate(DATES):
        o = opens.get(i) if opens else None
        out[d] = {"open": o if o is not None else (closes[i - 1] if i else closes[0]), "close": closes[i]}
    return out


DATES = biz_dates()


def snap(code: str, industry: str = "食品饮料") -> dict:
    return {
        "code": code,
        "name": code,
        "industry": industry,
        "total_shares_yi": 1000.0,  # 市值 = 1000×price ≥ 300 亿
        "dividends": [
            {"fiscal_year": fy, "dps": 1.0, "announce_date": f"{fy + 1}-04-10", "ex_date": f"{fy + 1}-06-01"}
            for fy in range(2008, 2026)
        ],
        "roes": [
            {"fiscal_year": fy, "roe": 15.0, "disclose_date": f"{fy + 1}-04-20"}
            for fy in range(2008, 2026)
        ],
    }


def pe_const(codes: list[str], v: float = 10.0) -> dict:
    return {c: {d: v for d in DATES} for c in codes}


def dip_closes(base: float = 1.0, i0: int = 119, lo: float = 0.86, hi: float = 0.87, hold: float = 0.95) -> list[float]:
    """i0 日砸坑、i0+1 企稳(收盘>昨收) → 主信号于 i0+1。

    i0+2 起收盘恒为 hold×base(位于成本0.90的 (0.88,1.10) 带内),
    避免持仓后被 ①/② 意外触发, 场景卖出点由各测试显式指定。
    """
    closes = [base] * N
    closes[i0] = base * lo
    closes[i0 + 1] = base * hi
    for i in range(i0 + 2, N):
        closes[i] = base * hold
    return closes


# ---------------------------------------------------------------- 成交与仓位


def test_buy_executes_next_open_with_slot_sizing():
    closes = dip_closes()
    opens = {121: 0.90}
    res = run_backtest(DATES, {"A": kline_from(closes, opens)}, {"A": snap("A")}, pe_const(["A"]), idle_yield_pct=0.0)
    buys = [t for t in res["trade_log"] if t["action"] == "BUY"]
    assert len(buys) == 1
    t = buys[0]
    assert t["date"] == DATES[120] and t["exec_date"] == DATES[121]
    assert t["price"] == 0.90  # 次日开盘成交, 非收盘
    assert abs(t["shares"] - 0.2 / 0.90) < 1e-6  # 单票 = 权益1.0 ÷ 5 = 0.2
    assert t["kind"] == "main"
    assert res["final"]["positions"]["A"]["cost"] == 0.90


def test_take_profit_rule2_no_cooldown():
    closes = dip_closes()
    closes[150] = 1.05  # ≥ 0.90×1.08 → 移动止盈激活
    closes[151] = 1.00  # ≤ 1.05×0.96=1.008 → 回落卖, 151 信号 152 执行
    opens = {121: 0.90}
    res = run_backtest(DATES, {"A": kline_from(closes, opens)}, {"A": snap("A")}, pe_const(["A"]), idle_yield_pct=0.0)
    sells = [t for t in res["trade_log"] if t["action"] == "SELL"]
    assert len(sells) == 1 and sells[0]["rule"] == 2
    assert sells[0]["exec_date"] == DATES[152]
    assert "A" not in res["final"]["cooldown_until"]


def test_stop_loss_rule1_and_40d_cooldown():
    closes = dip_closes()
    closes[150] = 0.79  # ≤ 0.90×0.92=0.828 → ①(动态止损, 无高低数据时水位=1)
    # 冷却期内再给主信号(第170/171日砸坑企稳) → 必须忽略
    closes[170] = 0.75
    closes[171] = 0.76
    for i in range(172, 230):
        closes[i] = 0.76  # 持平不触发买(收盘未>昨收)
    # 冷却结束后(151+40=191 之后)再给信号 → 可买(深坑保证 bias≤0.88)
    closes[230] = 0.70
    closes[231] = 0.71
    opens = {121: 0.90}
    res = run_backtest(DATES, {"A": kline_from(closes, opens)}, {"A": snap("A")}, pe_const(["A"]), idle_yield_pct=0.0)
    buys = [t for t in res["trade_log"] if t["action"] == "BUY"]
    sells = [t for t in res["trade_log"] if t["action"] == "SELL"]
    assert sells[0]["rule"] == 1
    assert [b["date"] for b in buys] == [DATES[120], DATES[231]]  # 170 日信号被冷却吞掉
    assert res["final"]["cooldown_until"]["A"] == 151 + 40  # 自止损执行日起算


def test_max_five_slots_no_swap():
    codes = [f"C{i}" for i in range(6)]
    kline = {c: kline_from(dip_closes(base=1.0 + i * 0.001)) for i, c in enumerate(codes)}
    snaps = {c: snap(c) for c in codes}
    res = run_backtest(DATES, kline, snaps, pe_const(codes), idle_yield_pct=0.0)
    buys = [t for t in res["trade_log"] if t["action"] == "BUY"]
    assert len(buys) == 5  # 第 6 个信号直接放弃
    assert len(res["final"]["positions"]) == 5


def test_idle_cash_yield():
    closes = [1.0] * N  # bias=1.0 无信号
    res_on = run_backtest(DATES, {"A": kline_from(closes)}, {"A": snap("A")}, pe_const(["A"]), idle_yield_pct=2.0)
    res_off = run_backtest(DATES, {"A": kline_from(closes)}, {"A": snap("A")}, pe_const(["A"]), idle_yield_pct=0.0)
    assert abs(res_off["history"][-1]["equity"] - 1.0) < 1e-9
    years = N / 252
    expect = 1.02**years
    assert abs(res_on["history"][-1]["equity"] - expect) < 1e-3


def test_quarterly_rebuild_and_gate_filter():
    closes = [1.0] * N
    # B 的 PE=30 → 每次重建都被门2 拦下
    res = run_backtest(DATES, {"A": kline_from(closes), "B": kline_from(closes)}, {"A": snap("A"), "B": snap("B")}, pe_const(["A"], 10.0) | pe_const(["B"], 30.0))
    months = [int(d[5:7]) for d in [r["date"] for r in res["pool_rebuilds"]]]
    assert set(months) <= {1, 4, 7, 10}
    assert res["pool_rebuilds"][0]["date"] == "2018-01-01"
    for r in res["pool_rebuilds"]:
        assert "A" in r["codes"] and "B" not in r["codes"]


def test_metrics_round_trip():
    closes = dip_closes()
    closes[150] = 1.05  # 激活移动止盈
    closes[151] = 1.00  # 回落卖, 执行价 1.00 → +11.1%
    opens = {121: 0.90}
    res = run_backtest(DATES, {"A": kline_from(closes, opens)}, {"A": snap("A")}, pe_const(["A"]), idle_yield_pct=0.0)
    m = res["metrics"]
    assert m["round_count"] == 1 and m["win_rate_pct"] == 100.0
    # 1/5 仓位 +11.1% → 总收益 ≈ +2.2%
    assert abs(m["total_return_pct"] - 2.22) < 0.15
    assert m["max_drawdown_pct"] <= 0.0


def test_idle_cash_follows_fund_series():
    """闲钱按货币基金实际日线收益计息: 无交易时净值=基金首尾比(缺失首日不计)。"""
    closes = [1.0] * N  # bias=1.0 无信号
    fund = {d: 100 * (1.001 ** (i + 1)) for i, d in enumerate(DATES)}
    res = run_backtest(
        DATES, {"A": kline_from(closes)}, {"A": snap("A")}, pe_const(["A"]),
        idle_yield_pct=0.0, idle_close=fund,
    )
    expect = fund[DATES[-1]] / fund[DATES[0]]
    assert abs(res["history"][-1]["equity"] - expect) < 1e-6


def test_suspension_gap_no_crash_and_carry_forward_valuation():
    # 对抗性: 持仓期间停牌 3 天(真实数据必然存在), 不得 KeyError,
    # 停牌日按最后收盘估值, 且停牌日不产生/执行任何交易
    closes = dip_closes()
    opens = {121: 0.90}
    kl = kline_from(closes, opens)
    for i in (123, 124, 125):
        del kl[DATES[i]]
    res = run_backtest(DATES, {"A": kl}, {"A": snap("A")}, pe_const(["A"]), idle_yield_pct=0.0)
    assert len(res["history"]) == N
    buys = [t for t in res["trade_log"] if t["action"] == "BUY"]
    assert len(buys) == 1
    shares = buys[0]["shares"]
    eq_susp = res["history"][124]["equity"]
    assert abs(eq_susp - (1.0 - shares * 0.90) - shares * 0.95) < 1e-6  # 最后收盘 0.95 而非成本 0.90
    assert not [t for t in res["trade_log"] if t["exec_date"] in (DATES[123], DATES[124], DATES[125])]
