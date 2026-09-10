"""价值回归抓取净化层对抗性测试: NaT 归一/每笔分红解析/双价不变量/涨跌停判定。

对应修复:
- B1: pd.NaT 拥有 .date 属性, 旧代码 str(NaT.date()) 落库 "NaT" 脏字符串且守卫为死代码;
- A3: qfq 锚点漂移 → 改存 RAW+HFQ 双价, _merge_pair 强制不变量(比值≥1 且单调不减);
- D1: 涨跌停不可成交判定(_sell_blocked/_buy_blocked)。
"""

from __future__ import annotations

import math

import pandas as pd

import value.fetch as vf
from value.backtest import _buy_blocked, _sell_blocked


# ---------------------------------------------------------------- B1: 日期净化


def test_norm_date_nat_and_dirty_inputs():
    assert vf._norm_date(pd.NaT) == ""  # 旧版落库 "NaT" 的根因场景
    assert vf._norm_date(None) == ""
    assert vf._norm_date("NaT") == ""
    assert vf._norm_date("") == ""
    assert vf._norm_date("垃圾数据") == ""
    assert vf._norm_date(float("nan")) == ""
    assert vf._norm_date(pd.Timestamp("2024-03-28")) == "2024-03-28"
    assert vf._norm_date("2024-03-28") == "2024-03-28"
    assert vf._norm_date("2024-03-28 00:00:00") == "2024-03-28"
    import datetime as dt

    assert vf._norm_date(dt.date(2024, 3, 28)) == "2024-03-28"


def test_parse_dividend_df_per_payment_and_nat():
    """每笔独立存行(同财年中期+年报不归并); NaT/NaN/负值/送转行全部净化或拒收。"""
    df = pd.DataFrame(
        {
            "报告期": ["2024-12-31", "2024-06-30", "2023-12-31", "2022-12-31", "2021-12-31"],
            "现金分红-现金分红比例": [3.0, 1.5, float("nan"), -2.0, 2.0],  # nan=送转行, 负值=脏数据
            "预案公告日": [pd.Timestamp("2025-03-28"), pd.Timestamp("2024-08-20"), pd.NaT, pd.Timestamp("2023-03-01"), pd.Timestamp("2022-03-01")],
            "除权除息日": [pd.Timestamp("2025-06-10"), pd.Timestamp("2024-09-05"), pd.NaT, pd.Timestamp("2023-06-01"), pd.Timestamp("2022-06-01")],
        }
    )
    rows = vf._parse_dividend_df(df)
    assert len(rows) == 3  # nan 送转行与负值行拒收
    fy24 = [r for r in rows if r["fiscal_year"] == 2024]
    assert len(fy24) == 2  # 中期+年报两笔独立存行(旧版会归并成一行并丢失一个除权日)
    assert {r["dps"] for r in fy24} == {0.3, 0.15}
    assert {r["ex_date"] for r in fy24} == {"2025-06-10", "2024-09-05"}
    assert all("NaT" not in (r["announce_date"], r["ex_date"]) for r in rows)  # 全库禁绝 "NaT"
    assert all(vf._DATE_RE.match(r["announce_date"]) or r["announce_date"] == "" for r in rows)


def test_parse_dividend_df_naT_string_compare_would_pass_gate():
    """回归防护: 净化后的日期串要么合法要么为空 — 空串被门忽略, 绝无 'NaT'>'2026' 的字典序漏洞。"""
    rows = vf._parse_dividend_df(
        pd.DataFrame(
            {
                "报告期": ["2024-12-31"],
                "现金分红-现金分红比例": [5.0],
                "预案公告日": [pd.NaT],
                "除权除息日": [pd.NaT],
            }
        )
    )
    assert rows[0]["announce_date"] == "" and rows[0]["ex_date"] == ""
    assert "" <= "2026-01-01"  # 空串字典序最小 → as-of 过滤语义安全(缺失=未生效)


# ---------------------------------------------------------------- A3: 双价不变量


def _pair_rows(closes, ratios):
    raw = [(f"2024-01-{i + 1:02d}", c, c, c, c) for i, c in enumerate(closes)]
    hfq = [(f"2024-01-{i + 1:02d}", c * r, c * r, c * r, c * r) for i, (c, r) in enumerate(zip(closes, ratios))]
    return raw, hfq


def test_merge_pair_happy_path():
    # 除权日 dps=0.5(理论 5%): raw 10.0→9.8(含跳降), hfq 剔除跳降(+3%) → comp=5% ✓
    raw = [("2024-01-01", 10.0, 10.0, 10.1, 9.9), ("2024-01-02", 9.7, 9.8, 9.9, 9.6), ("2024-01-03", 9.85, 9.9, 10.0, 9.8)]
    hfq = [("2024-01-01", 12.0, 12.0, 12.12, 11.88), ("2024-01-02", 12.24, 12.36, 12.48, 12.11), ("2024-01-03", 12.42, 12.49, 12.61, 12.36)]
    rows, err = vf._merge_pair(raw, hfq, [("2024-01-02", 0.5)])
    assert err == "" and len(rows) == 3
    assert rows[0] == ("2024-01-01", 10.0, 10.0, 10.1, 9.9, 12.0, 12.0, 12.12, 11.88)


def test_merge_pair_rejects_qfq_contamination_by_slope():
    """qfq 混入(减法复权, 斜率恒为1): 全窗口斜率 F=Δhfq/Δraw≈1 → 拒收。
    真 hfq 在有显著除权时 F 必偏离 1(每笔至少 +1.5%)。"""
    raw = [("2024-01-01", 10.0, 10.0, 10.0, 10.0), ("2024-01-02", 9.5, 9.5, 9.5, 9.5), ("2024-01-03", 8.0, 8.0, 8.0, 8.0)]
    qfq = [("2024-01-01", 9.4, 9.4, 9.4, 9.4), ("2024-01-02", 8.9, 8.9, 8.9, 8.9), ("2024-01-03", 7.4, 7.4, 7.4, 7.4)]  # = raw − 0.6
    rows, err = vf._merge_pair(raw, qfq, [("2024-01-02", 0.6)])
    assert rows == [] and "斜率" in err


def test_merge_pair_accepts_affine_ratio_decline():
    """腾讯 HFQ 为仿射复权(hfq = F×raw + D): raw 上涨时 ratio 自然回落 —
    旧版"单调不减"不变量会误拦全部真数据(实证: 茅台 6.106→6.065), 必须放行。"""
    raw, hfq = _pair_rows([549.09, 563.00, 599.90], [6.1057 / 5.6279, 6.0939 / 5.6279, 6.0652 / 5.6279])
    # 构造: hfq = 5.6279×raw + 262.3 的近似递减 ratio 序列
    rows, err = vf._merge_pair(raw, hfq, None)
    assert err == "" and len(rows) == 3


def test_merge_pair_rejects_degraded_hfq_with_dividends():
    """有显著除权(单笔≥1.5%)但 hfq≡raw(未补偿) → 分红补偿校验拒收。"""
    raw, hfq = _pair_rows([10.0, 9.6, 9.7], [1.0, 1.0, 1.0])
    rows, err = vf._merge_pair(raw, hfq, [("2024-01-02", 0.4)])
    assert rows == [] and ("退化" in err or "补偿" in err)


def test_merge_pair_accepts_scale_and_negative_offset_affine():
    """仿射复权合法形态(实测): 511880 份额折算 ratio≈0.0113; 天风证券配股致 D<0
    (hfq = 1.287×raw − 1.004, ratio 0.82~1.16 波动) — 旧版绝对 ratio≥1 会误杀两者。"""
    # 511880: 恒定尺度因子(无分红)
    raw, hfq = _pair_rows([100.0, 100.05, 100.1], [0.0113, 0.0113, 0.0113])
    rows, err = vf._merge_pair(raw, hfq, None)
    assert err == "" and len(rows) == 3
    # 天风证券形态: D<0 仿射, ratio 全段 <1 且随 raw 波动 — 不得被绝对 ratio 检查误杀
    # (无显著除权样本: 隔离检验点, 斜率/补偿判别由 qfq 混入用例覆盖)
    raw2 = [("2024-01-01", 3.00, 3.00, 3.00, 3.00), ("2024-06-03", 2.50, 2.50, 2.50, 2.50), ("2024-12-31", 2.13, 2.13, 2.13, 2.13)]
    hfq2 = [("2024-01-01", 2.857, 2.857, 2.857, 2.857), ("2024-06-03", 2.214, 2.214, 2.214, 2.214), ("2024-12-31", 1.737, 1.737, 1.737, 1.737)]
    rows2, err2 = vf._merge_pair(raw2, hfq2, [("2024-06-03", 0.001)])
    assert err2 == "" and len(rows2) == 3, f"配股致 ratio<1 的真 hfq 应放行: {err2}"


def test_merge_pair_accepts_small_dividend_ratio_near_one():
    """小额分红股(单笔<1.5%, 实测科创板多为这种): hfq≈raw 的未复权误差可忽略,
    不得触发"退化"拒收。"""
    raw = [("2024-01-01", 50.0, 50.0, 50.0, 50.0), ("2024-01-02", 49.95, 49.95, 49.95, 49.95)]
    rows, err = vf._merge_pair(raw, raw, [("2024-01-02", 0.05)])  # theory=0.1% 非显著
    assert err == "" and len(rows) == 2


def test_merge_pair_rejects_uncompensated_ex_day():
    """对抗性(A3 核心不变量): 除权日 RAW 跳降 dps 而 "HFQ" 同步跳降(未补偿=伪 hfq)
    → 分红补偿统计校验拒收。真 hfq 在该日应剔除跳降(comp ≈ dps/前收)。"""
    # 前收 10.0, 除权日 raw 跌至 9.5(dps=0.5, 理论 5%); 伪 hfq = 1.3×raw → 同步跌, comp≡0
    raw = [("2024-01-01", 10.0, 10.0, 10.0, 10.0), ("2024-01-02", 9.5, 9.5, 9.5, 9.5)]
    fake = [("2024-01-01", 13.0, 13.0, 13.0, 13.0), ("2024-01-02", 12.35, 12.35, 12.35, 12.35)]
    rows, err = vf._merge_pair(raw, fake, [("2024-01-02", 0.5)])
    assert rows == [] and "补偿" in err
    # 真 hfq: 除权日 hfq 持平(剔除跳降) → comp = 0 − (−5%) = +5% = 理论值 → 放行
    real = [("2024-01-01", 13.0, 13.0, 13.0, 13.0), ("2024-01-02", 13.0, 13.0, 13.0, 13.0)]
    rows2, err2 = vf._merge_pair(raw, real, [("2024-01-02", 0.5)])
    assert err2 == "" and len(rows2) == 2


def test_merge_pair_tolerates_noisy_minority_ex_days():
    """实测两源存在噪声(神华 42%、兖矿送转伴随 440%): 少数离群不得误拦,
    多数带内 + 均值方向为正即通过; 全部未补偿才拒。"""
    raw, hfq = [], []
    divs = []
    # 6 个除权日: 4 个精确补偿(带内), 1 个欠补偿(42%), 1 个送转伴随过补偿
    price = 10.0
    hprice = 20.0
    for k in range(7):
        raw.append((f"2024-0{k + 1}-01", price, price, price, price))
        hfq.append((f"2024-0{k + 1}-01", hprice, hprice, hprice, hprice))
        price_next = price - 0.5 if k < 6 else price  # 每次除权 raw 跌 0.5
        if k < 6:
            divs.append((f"2024-0{k + 2}-01", 0.5))
            if k == 2:  # 欠补偿: hfq 只剔除 42%
                hprice = hprice * (1 - 0.5 * 0.58 / price)
            elif k == 4:  # 送转伴随: raw 额外跳降但 hfq 不跌(comp 远超 theory)
                price_next -= 1.5
            else:  # 精确补偿: hfq 持平
                pass
        price = price_next
    rows, err = vf._merge_pair(raw, hfq, divs)
    assert err == "" and len(rows) == 7, f"少数噪声不应误拦: {err}"


def test_merge_pair_tolerates_transfer_along_ex_day():
    """送转伴随除权(实测拓普 601689/博腾 300548 comp≈+32%): raw 跳降含送转份额,
    hfq 不跌 → comp ≈ −raw_ret, 上界自适应放宽后必须放行; 伪 hfq(comp≈0)仍拒。"""
    # 前收 30.0, 除权日 10送4派0.45: raw 跌至 21.4(−3.4%现金 −28.6%送转), theory=1.5%
    raw = [("2024-06-03", 30.0, 30.0, 30.2, 29.8), ("2024-06-04", 21.3, 21.4, 21.6, 21.2)]
    hfq = [("2024-06-03", 45.0, 45.0, 45.3, 44.7), ("2024-06-04", 44.8, 45.1, 45.4, 44.6)]  # 不跌(剔除现金+送转)
    rows, err = vf._merge_pair(raw, hfq, [("2024-06-04", 0.45)])
    assert err == "" and len(rows) == 2, f"送转伴随除权应放行: {err}"
    # 同日若 hfq 随 raw 同跌(伪 hfq) → 仍拒
    fake = [("2024-06-03", 45.0, 45.0, 45.3, 44.7), ("2024-06-04", 31.9, 32.1, 32.4, 31.8)]
    rows2, err2 = vf._merge_pair(raw, fake, [("2024-06-04", 0.45)])
    assert rows2 == [] and "补偿" in err2


def test_merge_pair_drops_unaligned_dates_only():
    raw, hfq = _pair_rows([10.0, 10.1, 10.2], [1.0, 1.0, 1.0])
    hfq = hfq[:2]  # 端点少返回一天 → 弃该日, 其余保留
    rows, err = vf._merge_pair(raw, hfq, None)
    assert err == "" and [r[0] for r in rows] == ["2024-01-01", "2024-01-02"]


def test_kline_rows_rejects_garbage():
    resp = {
        "data": {
            "sh600000": {
                "day": [
                    ["2024-01-02", 10.0, 10.1, 10.2, 9.9],
                    ["2024-01-03", -1.0, 10.1, 10.2, 9.9],  # 负价拒收
                    ["2024-01-04", 10.0, 10.1, 9.8, 9.9],  # 高<低 拒收
                    ["bad-date", 10.0, 10.1, 10.2, 9.9],  # 日期非法拒收
                    ["2023-12-29", 10.0, 10.1, 10.2, 9.9],  # 窗口外拒收
                    ["2024-01-05", 10.0, None, 10.2, 9.9],  # 非数值拒收
                    ["2024-01-08", 10.0, 10.3, 10.4, 9.9],
                ]
            }
        }
    }
    rows = vf._kline_rows(resp, "sh600000", "", "2024-01-01", "2024-01-31")
    assert [r[0] for r in rows] == ["2024-01-02", "2024-01-08"]


# ---------------------------------------------------------------- D1: 涨跌停判定


def _row(o, h=None, l=None):
    return {"open": o, "high": h if h is not None else o, "low": l if l is not None else o}


def test_sell_blocked_main_board():
    # 主板 10%: 昨收 10.00 → 跌停 9.00
    assert _sell_blocked("600000", "2024-01-02", _row(9.0), 10.0)  # 一字跌停
    assert not _sell_blocked("600000", "2024-01-02", _row(9.0, h=9.5), 10.0)  # 开盘触停但开板 → 可成交
    assert not _sell_blocked("600000", "2024-01-02", _row(9.01), 10.0)  # 未触停
    assert not _sell_blocked("600000", "2024-01-02", _row(9.5), None)  # 无昨收(首日) → 不拦


def test_sell_blocked_rounding():
    # 跌停价四舍五入到分: 昨收 7.77 → 7.77×0.9=6.993 → 6.99
    assert _sell_blocked("600000", "2024-01-02", _row(6.99), 7.77)
    assert not _sell_blocked("600000", "2024-01-02", _row(7.00), 7.77)


def test_buy_blocked_main_board():
    assert _buy_blocked("600000", "2024-01-02", _row(11.0), 10.0)  # 一字涨停
    assert not _buy_blocked("600000", "2024-01-02", _row(11.0, l=10.5), 10.0)  # 开板可成交
    assert not _buy_blocked("600000", "2024-01-02", _row(10.99), 10.0)


def test_limit_rate_board_and_era():
    # 科创板 20%
    assert not _sell_blocked("688001", "2024-01-02", _row(9.0), 10.0)  # -10% 未触科创跌停(8.00)
    assert _sell_blocked("688001", "2024-01-02", _row(8.0), 10.0)
    # 创业板: 2020-08-24 前 10%, 后 20%
    assert _sell_blocked("300001", "2020-08-21", _row(9.0), 10.0)
    assert not _sell_blocked("300001", "2020-08-25", _row(9.0), 10.0)
    assert _sell_blocked("300001", "2020-08-25", _row(8.0), 10.0)


def test_cost_functions():
    from value.backtest import _sell_cost_rate

    assert math.isclose(_sell_cost_rate("2023-08-25"), (2.5 + 10.0) / 1e4)
    assert math.isclose(_sell_cost_rate("2023-08-28"), (2.5 + 5.0) / 1e4)
    assert math.isclose(_sell_cost_rate("2026-01-01"), (2.5 + 5.0) / 1e4)
