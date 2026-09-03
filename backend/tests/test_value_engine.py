"""价值回归引擎对抗性测试: 门边界/防前视/指标暖机/买卖规则/槽位/冷却。"""

from __future__ import annotations

import copy

from value.engine import (
    activity_series,
    bias_series,
    build_pool,
    buy_signals,
    check_gates,
    cooldown_active,
    is_pool_rebuild_day,
    sell_signals,
    z_series,
)

AS_OF = "2024-06-03"  # 季度重建日样例


def _div(fy, dps=1.0, ann=None, ex=None):
    return {
        "fiscal_year": fy,
        "dps": dps,
        "announce_date": ann or f"{fy + 1}-04-10",
        "ex_date": ex or f"{fy + 1}-06-01",
    }


def _roe(fy, roe=15.0, disc=None):
    return {"fiscal_year": fy, "roe": roe, "disclose_date": disc or f"{fy + 1}-04-20"}


def base_snapshot(**over):
    snap = {
        "code": "600000",
        "name": "测试白马",
        "industry": "食品饮料",
        "mcap_yi": 1000.0,
        "pe_ttm": 10.0,
        "price": 20.0,  # 近365天分红 1.0 → 股息率 5%
        "dividends": [_div(fy) for fy in (2019, 2020, 2021, 2022, 2023)],
        "roes": [_roe(fy) for fy in (2019, 2020, 2021, 2022, 2023)],
    }
    snap.update(over)
    return snap


def _gate(snap, name):
    return next(g for g in check_gates(snap, AS_OF) if g["gate"] == name)


# ---------------------------------------------------------------- 七道门边界


def test_base_snapshot_passes_all_gates():
    assert all(g["passed"] for g in check_gates(base_snapshot(), AS_OF))
    assert build_pool([base_snapshot()], AS_OF)[0]["code"] == "600000"


def test_gates_resilient_to_dirty_rows():
    # 对抗性: 脏数据行(空 dps/空公告日/空 ROE, 真实抓取中送转行与未决议案会产生)不得崩溃,
    # 且脏行不参与任何门的计算(股息率仍由正常行决定)
    divs = [_div(fy) for fy in (2019, 2020, 2021, 2022, 2023)]
    divs.append({"fiscal_year": 2024, "dps": None, "announce_date": "", "ex_date": "2025-06-01"})
    divs.append(_div(2024, dps=1.0, ann="", ex="2025-06-01"))  # 公告日缺失 → 未生效, 忽略
    roes = [_roe(fy) for fy in (2019, 2020, 2021, 2022, 2023)]
    roes.append({"fiscal_year": 2024, "roe": None, "disclose_date": "2025-04-20"})
    snap = base_snapshot(dividends=divs, roes=roes)
    gates = check_gates(snap, AS_OF)
    assert len(gates) == 6
    assert _gate(snap, "股息托底")["passed"]
    assert _gate(snap, "连续分红")["passed"]
    assert _gate(snap, "盈利稳定")["passed"]
    assert build_pool([snap], AS_OF)[0]["code"] == "600000"


def test_gate_pe_boundaries():
    assert _gate(base_snapshot(pe_ttm=20.0), "盈利且不贵")["passed"]
    assert not _gate(base_snapshot(pe_ttm=20.01), "盈利且不贵")["passed"]
    assert not _gate(base_snapshot(pe_ttm=0.0), "盈利且不贵")["passed"]
    assert not _gate(base_snapshot(pe_ttm=-5.0), "盈利且不贵")["passed"]


def test_gate_dividend_cut_and_missing_year():
    divs = [_div(fy) for fy in (2019, 2020, 2021, 2022)] + [_div(2023, dps=0.4)]
    assert not _gate(base_snapshot(dividends=divs), "连续分红")["passed"]  # 0.4 < 1.0×50%
    divs_missing = [_div(fy) for fy in (2019, 2020, 2022, 2023)]  # 缺 2021
    assert not _gate(base_snapshot(dividends=divs_missing), "连续分红")["passed"]


def test_gate_dividend_yield_boundary():
    # 1.0 / price: price=33.44 → 2.99% 拒; price=33.33 → 3.0% 拒(需 >3); price=25 → 4% 过
    assert not _gate(base_snapshot(price=33.44), "股息托底")["passed"]
    assert _gate(base_snapshot(price=25.0), "股息托底")["passed"]


def test_gate_mcap_boundary():
    assert _gate(base_snapshot(mcap_yi=300.0), "大盘股")["passed"]
    assert not _gate(base_snapshot(mcap_yi=299.99), "大盘股")["passed"]


def test_gate_real_estate_excluded():
    assert not _gate(base_snapshot(industry="房地产开发"), "非地产")["passed"]
    assert not _gate(base_snapshot(industry="房地产服务"), "非地产")["passed"]
    assert _gate(base_snapshot(industry="银行"), "非地产")["passed"]


def test_gate_roe_boundary_and_missing():
    roes = [_roe(fy) for fy in (2019, 2020, 2021, 2022)] + [_roe(2023, roe=9.99)]
    assert not _gate(base_snapshot(roes=roes), "盈利稳定")["passed"]
    assert _gate(base_snapshot(roes=[_roe(fy) for fy in (2019, 2020, 2021, 2022)] + [_roe(2023, roe=10.0)]), "盈利稳定")["passed"]
    roes_missing = [_roe(fy) for fy in (2019, 2020, 2021, 2023)]
    assert not _gate(base_snapshot(roes=roes_missing), "盈利稳定")["passed"]


def test_gate_no_lookahead_dividend_and_roe():
    """预案公告日/披露日晚于 as_of 的记录不得计入。"""
    divs = [_div(fy) for fy in (2019, 2020, 2021, 2022)] + [_div(2023, ann="2024-07-01")]
    assert not _gate(base_snapshot(dividends=divs), "连续分红")["passed"]
    roes = [_roe(fy) for fy in (2019, 2020, 2021, 2022)] + [_roe(2023, disc="2024-08-01")]
    assert not _gate(base_snapshot(roes=roes), "盈利稳定")["passed"]
    # 除权日晚于 as_of 不计入股息率
    divs_ex = [_div(fy) for fy in (2019, 2020, 2021, 2022)] + [_div(2023, ex="2024-06-20")]
    assert not _gate(base_snapshot(dividends=divs_ex, price=25.0), "股息托底")["passed"]


# ---------------------------------------------------------------- 指标暖机


def test_bias_warmup_and_value():
    closes = [2.0] * 119 + [1.76]
    b = bias_series(closes)
    assert all(x is None for x in b[:119])
    # 精确: MA = (119*2.0 + 1.76)/120
    ma = (119 * 2.0 + 1.76) / 120
    assert abs(b[119] - 1.76 / ma) < 1e-9


def test_z_warmup_and_hand_computed():
    bias = [1.0] * 249 + [0.5]
    z = z_series(bias)
    assert all(x is None for x in z[:249])
    # 手算: mean=0.998, var=(249×0.002²+0.498²)/250=0.000996, z=(0.5−0.998)/√0.000996≈−15.78
    assert z[249] is not None and abs(z[249] - (-15.78)) < 0.01
    # 常数序列标准差 0 → None
    assert z_series([1.0] * 250)[249] is None


# ---------------------------------------------------------------- 卖出优先级


def _q(code, close, prev_close=None, bias=None, z=None, pe=None, ind="食品饮料", act=None):
    return {"code": code, "close": close, "prev_close": prev_close if prev_close is not None else close, "bias": bias, "z": z, "pe_ttm": pe, "industry": ind, "act": act}


def test_sell_priority_and_rules():
    pos = {"A": {"cost": 10.0}, "C": {"cost": 10.0}, "D": {"cost": 10.0}}
    quotes = {
        "A": _q("A", 8.8, bias=1.2),  # ①(8.8 ≤ 9.2)+③ 同触 → ①
        "C": _q("C", 10.5, bias=1.08),  # ③
        "D": _q("D", 10.5, bias=1.0),  # 不触发
    }
    sig = {s["code"]: s for s in sell_signals(pos, quotes)}
    assert sig["A"]["rule"] == 1
    assert sig["C"]["rule"] == 3
    assert "D" not in sig
    # ② 移动止盈: 首日+20%激活不卖, 次日自高点回落≥4% 卖; 与③同触时②优先
    posB = {"B": {"cost": 10.0}}
    assert sell_signals(posB, {"B": _q("B", 12.0)}) == []  # 激活, 无信号
    assert posB["B"]["armed"] and posB["B"]["max_close"] == 12.0
    sig2 = {s["code"]: s for s in sell_signals(posB, {"B": _q("B", 11.4, bias=1.2)})}  # 回落+③同触
    assert sig2["B"]["rule"] == 2


def test_dynamic_stop_quiet_vs_heavy():
    """缩幅阴跌(水位0.3)→ s=0.92-0.04×0.7=0.892 抗洗盘; 放幅急跌(水位2.0)→ s=0.92+0.05×0.5=0.945 砍出货。"""
    pos = {"Q": {"cost": 10.0}, "H": {"cost": 10.0}}
    sig = {s["code"]: s for s in sell_signals(pos, {"Q": _q("Q", 8.95, act=0.3), "H": _q("H", 8.95, act=2.0)})}
    assert "Q" not in sig  # 8.95 > 8.92 → 宽限
    assert sig["H"]["rule"] == 1  # 8.95 ≤ 9.45 → 收紧


def test_trailing_cap30():
    pos = {"X": {"cost": 10.0}}
    sell_signals(pos, {"X": _q("X", 11.0)})  # 激活
    sig = sell_signals(pos, {"X": _q("X", 13.0)})  # +30% 封顶
    assert sig[0]["rule"] == 2


def test_activity_series_trailing_only():
    r = [0.02] * 100
    a1 = activity_series(r)
    r2 = list(r)
    r2[99] = 0.5
    a2 = activity_series(r2)
    assert a1[50] == a2[50]  # 未来幅度变化不影响历史水位(无前视)
    assert a1[99] != a2[99]


# ---------------------------------------------------------------- 买入规则


def test_buy_main_and_falling_knife():
    q = _q("A", 10.0, prev_close=9.9, bias=0.88, pe=10.0)
    assert buy_signals([q], 1, set())[0]["kind"] == "main"
    knife = _q("A", 10.0, prev_close=10.0, bias=0.80, pe=10.0)  # 收盘未 > 昨收
    assert buy_signals([knife], 1, set()) == []


def test_buy_main_boundary():
    ok = _q("A", 10.0, prev_close=9.9, bias=0.88, pe=10.0)
    no = _q("B", 10.0, prev_close=9.9, bias=0.8801, z=-0.5, pe=10.0)  # 主拒, z 不够侧门
    assert [p["code"] for p in buy_signals([ok, no], 2, set())] == ["A"]


def test_buy_side_only_when_no_main_and_free_slot():
    side = _q("S", 10.0, prev_close=9.9, bias=0.89, z=-2.0, pe=10.0)
    assert buy_signals([side], 1, set())[0]["kind"] == "side"
    assert buy_signals([side], 0, set()) == []  # 无空槽
    main = _q("M", 10.0, prev_close=9.9, bias=0.87, pe=10.0)
    picks = buy_signals([side, main], 2, set())  # 有主信号 → 侧门不启用
    assert [p["code"] for p in picks] == ["M"]


def test_buy_sort_by_bias_not_z():
    deep = _q("DEEP", 10.0, prev_close=9.9, bias=0.84, z=-0.5, pe=10.0)
    shallow = _q("SHAL", 10.0, prev_close=9.9, bias=0.86, z=-3.0, pe=10.0)  # z 更极端但 bias 更浅
    picks = buy_signals([shallow, deep], 2, set())
    assert [p["code"] for p in picks] == ["DEEP", "SHAL"]


def test_buy_pe_industry_gate():
    # 行业 3 只 + 候选: 中位取含候选的池内正PE中位([10,12,14,x]→13) → 闸 15.6
    peers = [_q("P1", 10, pe=10.0), _q("P2", 10, pe=12.0), _q("P3", 10, pe=14.0)]
    cand = _q("C", 10.0, prev_close=9.9, bias=0.85, pe=16.0)
    assert buy_signals(peers + [cand], 1, set()) == []
    cand_ok = _q("C", 10.0, prev_close=9.9, bias=0.85, pe=14.3)
    assert [p["code"] for p in buy_signals(peers + [cand_ok], 1, set())] == ["C"]
    # 行业不足 3 只 → 跳过该闸
    cand2 = _q("C", 10.0, prev_close=9.9, bias=0.85, pe=30.0, ind="稀有行业")
    assert [p["code"] for p in buy_signals([cand2], 1, set())] == ["C"]


def test_buy_cooldown_ignored():
    q = _q("A", 10.0, prev_close=9.9, bias=0.80, pe=10.0)
    assert buy_signals([q], 1, {"A"}) == []


# ---------------------------------------------------------------- 日历/冷却


def test_pool_rebuild_day():
    dates = ["2024-03-29", "2024-04-01", "2024-04-02", "2024-05-06", "2024-06-03"]
    assert not is_pool_rebuild_day(dates, 0)
    assert is_pool_rebuild_day(dates, 1)
    assert not is_pool_rebuild_day(dates, 2)
    assert not is_pool_rebuild_day(dates, 3)
    assert not is_pool_rebuild_day(dates, 4)
    jan = ["2023-12-29", "2024-01-02"]
    assert is_pool_rebuild_day(jan, 1)


def test_cooldown_window():
    cd = {"A": 60}
    assert cooldown_active(cd, "A", 60)
    assert not cooldown_active(cd, "A", 61)
    assert not cooldown_active(cd, "B", 0)


def test_build_pool_excludes_failing():
    good = base_snapshot(code="600001")
    bad = base_snapshot(code="600002", pe_ttm=30.0)
    pool = build_pool([good, bad], AS_OF)
    assert [p["code"] for p in pool] == ["600001"]


def _snapshot_copy():
    return copy.deepcopy(base_snapshot())
