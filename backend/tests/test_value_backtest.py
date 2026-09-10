"""价值回归回测器对抗性测试: 双价空间/暖机/成本/涨跌停/停牌/实现盈亏/夏普口径。

v3 口径要点(与 value/backtest.py 对应):
- RAW(不复权)空间: 基本面门、涨跌停判定、成交金额展示;
- HFQ(后复权)空间: 指标、信号、成本、盈亏、净值;
- 成本: 佣金 2.5bp 双边 + 印花税卖方(2023-08-28 前 10bp/后 5bp);
- 一字跌停卖不出 → 顺延; 一字涨停买不进 → 放弃;
- pnl_pct = 实现盈亏(次日开盘实际成交、净额口径), 非信号日收盘目标值。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from value.backtest import run_backtest
from value.config import COST_COMMISSION_BP, STAMP_TAX_BP_LEGACY, STAMP_TAX_BP_NEW

N = 320
BUY_COST = COST_COMMISSION_BP / 1e4
SELL_COST_OLD = (COST_COMMISSION_BP + STAMP_TAX_BP_LEGACY) / 1e4


def biz_dates(start: str = "2018-01-01", n: int = N) -> list[str]:
    d = datetime.strptime(start, "%Y-%m-%d")
    out: list[str] = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


DATES = biz_dates()


def kline_from(
    closes: list[float],
    opens: dict[int, float] | None = None,
    highs: dict[int, float] | None = None,
    lows: dict[int, float] | None = None,
    dates: list[str] | None = None,
    raw_closes: list[float] | None = None,
    raw_opens: dict[int, float] | None = None,
    raw_highs: dict[int, float] | None = None,
    raw_lows: dict[int, float] | None = None,
) -> dict[str, dict]:
    """构造 RAW+HFQ 双价K线。closes/opens/highs/lows = HFQ 空间(信号/盈亏);
    raw_* 缺省时与 HFQ 相同(无除权场景)。"""
    dates = dates or DATES
    if raw_closes is None:  # 无除权场景: RAW 镜像 HFQ(含 opens/highs/lows)
        raw_closes = closes
        raw_opens = opens if raw_opens is None else raw_opens
        raw_highs = highs if raw_highs is None else raw_highs
        raw_lows = lows if raw_lows is None else raw_lows
    out = {}
    for i, d in enumerate(dates):
        o = (opens.get(i) if opens else None) or (closes[i - 1] if i else closes[0])
        ro = (raw_opens.get(i) if raw_opens else None) or (raw_closes[i - 1] if i else raw_closes[0])
        row = {
            "open": ro, "close": raw_closes[i],
            "high": (raw_highs or {}).get(i, max(ro, raw_closes[i])),
            "low": (raw_lows or {}).get(i, min(ro, raw_closes[i])),
            "aopen": o, "aclose": closes[i],
            "ahigh": (highs or {}).get(i, max(o, closes[i])),
            "alow": (lows or {}).get(i, min(o, closes[i])),
        }
        out[d] = row
    return out


def snap(code: str, industry: str = "食品饮料", shares: float = 1000.0) -> dict:
    return {
        "code": code,
        "name": code,
        "industry": industry,
        "total_shares_yi": shares,  # 市值 = shares×RAW价格 ≥ 300 亿
        "dividends": [
            {"fiscal_year": fy, "dps": 1.0, "announce_date": f"{fy + 1}-04-10", "ex_date": f"{fy + 1}-06-01"}
            for fy in range(2008, 2026)
        ],
        "roes": [
            {"fiscal_year": fy, "roe": 15.0, "disclose_date": f"{fy + 1}-04-20"}
            for fy in range(2008, 2026)
        ],
    }


def pe_const(codes: list[str], v: float = 10.0, dates: list[str] | None = None) -> dict:
    return {c: {d: v for d in (dates or DATES)} for c in codes}


def dip_closes(base: float = 1.0, i0: int = 119, lo: float = 0.86, hi: float = 0.87, hold: float = 0.95) -> list[float]:
    """i0 日砸坑、i0+1 企稳(收盘>昨收) → 主信号于 i0+1。

    i0+2 起收盘恒为 hold×base, 避免持仓后被 ①/② 意外触发,
    场景卖出点由各测试显式指定。
    """
    closes = [base] * N
    closes[i0] = base * lo
    closes[i0 + 1] = base * hi
    for i in range(i0 + 2, N):
        closes[i] = base * hold
    return closes


# ---------------------------------------------------------------- 成交与仓位


def test_buy_executes_next_open_with_slot_sizing_and_cost():
    closes = dip_closes()
    opens = {121: 0.90}
    res = run_backtest(DATES, {"A": kline_from(closes, opens)}, {"A": snap("A")}, pe_const(["A"]), idle_yield_pct=0.0)
    buys = [t for t in res["trade_log"] if t["action"] == "BUY"]
    assert len(buys) == 1
    t = buys[0]
    assert t["date"] == DATES[120] and t["exec_date"] == DATES[121]
    assert t["price"] == 0.90  # 次日开盘成交(RAW 价), 非收盘
    assert abs(t["amount"] - 0.2) < 1e-9  # 单票 = 权益1.0 ÷ 5 = 0.2(现金支出口径, 含佣金)
    assert abs(t["shares"] - 0.2 * (1 - BUY_COST) / 0.90) < 1e-9  # 实际持股 = 净额 ÷ 开盘价
    assert t["kind"] == "main"
    pos = res["final"]["positions"]["A"]
    assert pos["cost"] == 0.90 and pos["cost_raw"] == 0.90
    assert abs(pos["cash_in"] - 0.2) < 1e-9


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


def test_metrics_round_trip_with_costs():
    closes = dip_closes()
    closes[150] = 1.05  # 激活移动止盈
    closes[151] = 1.00  # 回落卖, 执行价 1.00
    opens = {121: 0.90}
    res = run_backtest(DATES, {"A": kline_from(closes, opens)}, {"A": snap("A")}, pe_const(["A"]), idle_yield_pct=0.0)
    m = res["metrics"]
    assert m["round_count"] == 1 and m["win_rate_pct"] == 100.0
    # 精确: 1/5 仓, 净额买入 0.2×(1-佣金)÷0.90 股, 1.00 卖出扣佣金+印花税(2018=10bp)
    expect_pnl = (1.00 / 0.90) * (1 - BUY_COST) * (1 - SELL_COST_OLD) - 1
    assert abs(m["total_return_pct"] - expect_pnl * 100 / 5) < 0.02
    assert m["max_drawdown_pct"] <= 0.0


def test_idle_cash_follows_fund_series():
    """闲钱按货币基金实际日线收益(HFQ 收盘比)计息: 无交易时净值=基金首尾比(缺失首日不计)。"""
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
    assert abs(eq_susp - (1.0 - buys[0]["amount"]) - shares * 0.95) < 1e-6  # 最后收盘 0.95 而非成本 0.90
    assert not [t for t in res["trade_log"] if t["exec_date"] in (DATES[123], DATES[124], DATES[125])]


# ---------------------------------------------------------------- v3 新增: 暖机(C2)


def test_warmup_indicators_available_from_start():
    """dates 全量传入 + start 截断: start 当日 bias/z 已就绪(用 start 前的数据暖机),
    且 start 前即使出现深坑信号也绝不交易/记净值。"""
    dates_full = biz_dates("2017-01-02", 500)
    start_i = next(i for i, d in enumerate(dates_full) if d >= "2018-01-01")
    closes = [1.0] * 500
    # 暖机段深坑(idx 149/150, bias≈0.872 可触发主信号): 旧版丢弃暖机时这段本会"可交易" —
    # 新版 loop 自 start 起, 必须无交易; 且证明 start 当日指标已就绪
    closes[149], closes[150] = 0.86, 0.87
    # start 后立刻砸坑: 若暖机被丢弃, bias 要到 start+120 日才可用 → 此信号会丢失
    closes[start_i + 1] = 0.86
    closes[start_i + 2] = 0.87
    for i in range(start_i + 3, 500):
        closes[i] = 0.95
    kl = {"A": kline_from(closes, opens={start_i + 3: 0.90}, dates=dates_full)}
    res = run_backtest(dates_full, kl, {"A": snap("A")}, pe_const(["A"], dates=dates_full), idle_yield_pct=0.0, start="2018-01-01")
    assert res["history"][0]["date"] == dates_full[start_i]  # 净值自 start 起
    buys = [t for t in res["trade_log"] if t["action"] == "BUY"]
    assert len(buys) == 1  # 暖机段深坑无交易, start 后深坑立即成交
    assert buys[0]["date"] == dates_full[start_i + 2] and buys[0]["exec_date"] == dates_full[start_i + 3]


# ---------------------------------------------------------------- v3 新增: 成本(D1)


def test_stamp_tax_halved_after_2023():
    """同一笔往返, 执行日在 2023-08-28 后印花税减半 → 实现盈亏更高, 差值≈5bp。"""
    def one_round(dates: list[str]) -> float:
        closes = dip_closes()
        closes[150] = 1.05
        closes[151] = 1.00
        kl = {"A": kline_from(closes, opens={121: 0.90}, dates=dates)}
        res = run_backtest(dates, kl, {"A": snap("A")}, pe_const(["A"], dates=dates), idle_yield_pct=0.0)
        return next(t["pnl_pct"] for t in res["trade_log"] if t["action"] == "SELL")

    pnl_2018 = one_round(DATES)  # 执行日 2018-08 → 10bp
    dates_2023 = biz_dates("2023-06-01", N)  # 执行日 2023-12 → 5bp
    pnl_2023 = one_round(dates_2023)
    diff = pnl_2023 - pnl_2018
    assert 0.03 < diff < 0.09, f"印花税减半应带来约 5bp 改善, 实际 {diff:.4f}pp"
    expect_2018 = ((1.00 / 0.90) * (1 - BUY_COST) * (1 - SELL_COST_OLD) - 1) * 100
    assert abs(pnl_2018 - expect_2018) < 0.006  # 实现盈亏精确到费率(pnl 落库保留 2 位小数)


# ---------------------------------------------------------------- v3 新增: 涨跌停(D1)


def test_limit_down_one_word_defers_sell():
    """一字跌停(开=高=低=跌停价)卖不出 → 顺延至次日可成交时执行; 冷却自实际执行日起算。"""
    closes = dip_closes()
    closes[150] = 0.79  # 止损信号日
    closes[151] = 0.71  # 执行日一字跌停: round(0.79×0.9,2)=0.71
    closes[152] = 0.70
    opens = {121: 0.90, 151: 0.71, 152: 0.70}
    highs = {151: 0.71, 152: 0.72}
    lows = {151: 0.71, 152: 0.69}
    res = run_backtest(DATES, {"A": kline_from(closes, opens, highs, lows)}, {"A": snap("A")}, pe_const(["A"]), idle_yield_pct=0.0)
    sells = [t for t in res["trade_log"] if t["action"] == "SELL"]
    assert len(sells) == 1
    assert sells[0]["exec_date"] == DATES[152]  # 跌停日顺延, 次日开板成交
    assert any(b["action"] == "SELL" and b["date"] == DATES[151] for b in res["blocked"])
    assert res["final"]["cooldown_until"]["A"] == 152 + 40  # 冷却自实际执行日
    # 实现盈亏按实际成交价 0.70(净额, 落库 2 位小数)
    expect = ((0.70 / 0.90) * (1 - BUY_COST) * (1 - SELL_COST_OLD) - 1) * 100
    assert abs(sells[0]["pnl_pct"] - expect) < 0.006


def test_limit_down_open_but_board_trades_fills():
    """开盘触跌停但盘中开板(high > 跌停价) → 竞价单可成交, 不顺延。"""
    closes = dip_closes()
    closes[150] = 0.79
    closes[151] = 0.74
    opens = {121: 0.90, 151: 0.71}
    highs = {151: 0.75}
    lows = {151: 0.71}
    res = run_backtest(DATES, {"A": kline_from(closes, opens, highs, lows)}, {"A": snap("A")}, pe_const(["A"]), idle_yield_pct=0.0)
    sells = [t for t in res["trade_log"] if t["action"] == "SELL"]
    assert sells[0]["exec_date"] == DATES[151] and sells[0]["price"] == 0.71
    assert not res["blocked"]


def test_limit_up_one_word_abandons_buy():
    """一字涨停买不进 → 放弃(不追价), 资金与槽位保持原状。"""
    closes = dip_closes()
    closes[121] = 0.96  # 执行日一字涨停: round(0.87×1.1,2)=0.96
    for i in range(122, N):
        closes[i] = 0.95
    opens = {121: 0.96}
    highs = {121: 0.96}
    lows = {121: 0.96}
    res = run_backtest(DATES, {"A": kline_from(closes, opens, highs, lows)}, {"A": snap("A")}, pe_const(["A"]), idle_yield_pct=0.0)
    assert not [t for t in res["trade_log"] if t["action"] == "BUY"]
    assert any(b["action"] == "BUY" and b["date"] == DATES[121] for b in res["blocked"])
    assert not res["final"]["positions"]
    assert abs(res["history"][-1]["equity"] - 1.0) < 1e-9


def test_gem_20pct_limit_after_reform():
    """创业板 2020-08-24 后涨跌幅 20%: 开盘 -15% 可成交(主板口径则会误判跌停)。"""
    dates = biz_dates("2021-01-01", N)
    closes = dip_closes()
    closes[150] = 0.79  # 止损信号
    closes[151] = 0.67  # 开盘 -15%: 主板已跌停(0.71), 创业板 20% 跌停价=round(0.79×0.8,2)=0.63
    opens = {121: 0.90, 151: 0.67}
    highs = {151: 0.70}
    lows = {151: 0.66}
    kl = {"300001": kline_from(closes, opens, highs, lows, dates=dates)}
    res = run_backtest(dates, kl, {"300001": snap("300001")}, pe_const(["300001"], dates=dates), idle_yield_pct=0.0)
    sells = [t for t in res["trade_log"] if t["action"] == "SELL"]
    assert sells[0]["exec_date"] == dates[151] and sells[0]["price"] == 0.67  # -15% 未触 20% 跌停 → 成交
    assert not res["blocked"]


def test_buy_deferred_when_suspended_on_exec_day():
    """执行日停牌 → 买入委托当日失效放弃(不追复牌价)。"""
    closes = dip_closes()
    kl = kline_from(closes, opens={121: 0.90})
    del kl[DATES[121]]  # 执行日停牌
    res = run_backtest(DATES, {"A": kl}, {"A": snap("A")}, pe_const(["A"]), idle_yield_pct=0.0)
    assert not [t for t in res["trade_log"] if t["action"] == "BUY"]
    assert any(b["action"] == "BUY" and "停牌" in b["why"] for b in res["blocked"])


# ---------------------------------------------------------------- v3 新增: 停牌卖出(D4)


def test_suspension_sell_check_resumes_on_first_trading_day():
    """持仓停牌数日后复牌即暴跌: 复牌当日必须恢复卖出检查(prev_close 用停牌前最后收盘),
    旧版要求"昨日也有行情"导致检查再延一天。"""
    closes = dip_closes()
    opens = {121: 0.90}
    kl = kline_from(closes, opens)
    for i in (150, 151, 152):
        del kl[DATES[i]]  # 停牌 3 日
    closes[153] = 0.75  # 复牌日暴跌 ≤ 0.90×0.92=0.828 → ①
    kl[DATES[153]]["close"] = 0.75
    kl[DATES[153]]["aclose"] = 0.75
    res = run_backtest(DATES, {"A": kl}, {"A": snap("A")}, pe_const(["A"]), idle_yield_pct=0.0)
    sells = [t for t in res["trade_log"] if t["action"] == "SELL"]
    assert len(sells) == 1 and sells[0]["rule"] == 1
    assert sells[0]["date"] == DATES[153]  # 复牌当日即出信号(不再延后)
    assert sells[0]["exec_date"] == DATES[154]


# ---------------------------------------------------------------- v3 新增: 实现盈亏(D2)


def test_realized_pnl_uses_execution_open_not_signal_close():
    """信号日收盘 1.00(表观 +11.1%), 次日开盘缺口跌至 0.95 → 报告盈亏必须按实际成交 0.95 计。"""
    closes = dip_closes()
    closes[150] = 1.05
    closes[151] = 1.00
    opens = {121: 0.90, 152: 0.95}  # 执行日缺口低开
    res = run_backtest(DATES, {"A": kline_from(closes, opens)}, {"A": snap("A")}, pe_const(["A"]), idle_yield_pct=0.0)
    sell = next(t for t in res["trade_log"] if t["action"] == "SELL")
    expect = ((0.95 / 0.90) * (1 - BUY_COST) * (1 - SELL_COST_OLD) - 1) * 100
    assert abs(sell["pnl_pct"] - expect) < 0.006
    assert sell["pnl_pct"] < 6.0  # 绝非信号日表观 +11.1%
    assert sell["price"] == 0.95


# ---------------------------------------------------------------- v3 新增: 夏普口径(D3)


def test_sharpe_deducts_risk_free():
    closes = [1.0] * N
    kl = {"A": kline_from(closes)}
    s0 = run_backtest(DATES, kl, {"A": snap("A")}, pe_const(["A"]), idle_yield_pct=2.0, risk_free_pct=0.0)
    s2 = run_backtest(DATES, kl, {"A": snap("A")}, pe_const(["A"]), idle_yield_pct=2.0, risk_free_pct=2.0)
    assert s0["metrics"]["sharpe"] > s2["metrics"]["sharpe"]  # 扣无风险利率后系统性下移


# ---------------------------------------------------------------- v3 新增: 双价空间隔离(A2)


def test_fundamental_gates_use_raw_price_space():
    """市值门用 RAW 价: HFQ=3×RAW 时, RAW 市值 150 亿(拒) 不得因 HFQ 450 亿而误入池;
    股息率门用 RAW 分母: RAW 价 20 → 5%(过), 若误用 HFQ 60 → 1.67%(拒)。"""
    closes_adj = [3.0] * N
    closes_raw = [1.0] * N
    kl_bad = kline_from(closes_adj, raw_closes=closes_raw)  # RAW 市值 = 1.0×150 = 150 < 300
    kl_ok = kline_from([60.0] * N, raw_closes=[20.0] * N)  # RAW 市值 2000 ✓, dy = 1/20 = 5% ✓
    res = run_backtest(
        DATES,
        {"BAD": kl_bad, "OK": kl_ok},
        {"BAD": snap("BAD", shares=150.0), "OK": snap("OK", shares=100.0)},
        pe_const(["BAD", "OK"]),
        idle_yield_pct=0.0,
    )
    for r in res["pool_rebuilds"]:
        assert "BAD" not in r["codes"]  # 市值门按 RAW 判定 → 拒
        assert "OK" in r["codes"]  # 股息率/市值按 RAW 判定 → 过


def test_signals_and_pnl_in_adj_space_ignoring_raw_dividend_drop():
    """持仓期间除权: RAW 价跳降 0.15(真实除权, 非亏损), HFQ 连续。
    信号/止损/盈亏必须全在 HFQ 空间 — RAW 跳降不得误触发止损, 盈亏按 HFQ 计。"""
    closes = dip_closes()  # HFQ 空间
    raw = list(closes)
    for i in range(130, N):  # 130 日除权: RAW 永久下移 0.15, HFQ 连续
        raw[i] = closes[i] - 0.15
    raw[130] = closes[130] - 0.15  # 若误用 RAW: 0.80 ≤ 0.90×0.92=0.828 → 假止损
    closes[150] = 1.05
    closes[151] = 1.00
    for i in range(150, N):
        raw[i] = closes[i] - 0.15
    opens = {121: 0.90, 152: 1.00}
    raw_opens = {121: 0.90, 152: 0.85}
    kl = {"A": kline_from(closes, opens, raw_closes=raw, raw_opens=raw_opens)}
    res = run_backtest(DATES, kl, {"A": snap("A")}, pe_const(["A"]), idle_yield_pct=0.0)
    sells = [t for t in res["trade_log"] if t["action"] == "SELL"]
    assert len(sells) == 1 and sells[0]["rule"] == 2  # 除权日无假止损, 唯一卖出=移动止盈
    assert sells[0]["date"] == DATES[151]
    expect = ((1.00 / 0.90) * (1 - BUY_COST) * (1 - SELL_COST_OLD) - 1) * 100  # HFQ 盈亏 ≈ +10.9%
    assert abs(sells[0]["pnl_pct"] - expect) < 0.006
    assert sells[0]["price"] == 0.85  # 成交金额展示为 RAW 真实价


def test_row_without_adj_price_treated_as_suspension():
    """缺 HFQ 价的行(未完成迁移的脏状态)视同停牌: 不产生信号、不可成交。"""
    closes = dip_closes()
    kl = kline_from(closes, opens={121: 0.90})
    for d in (DATES[120], DATES[121]):
        kl[d]["aclose"] = None
    res = run_backtest(DATES, {"A": kl}, {"A": snap("A")}, pe_const(["A"]), idle_yield_pct=0.0)
    assert not [t for t in res["trade_log"] if t["action"] == "BUY"]  # 信号日缺 HFQ → 不出信号
