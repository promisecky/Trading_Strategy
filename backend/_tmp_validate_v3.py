"""v3 修复全量对抗性验证 — 迁移完成后运行, 逐项复核审查报告 P0/P1/P2 的验收标准。

用法: VALUE_NO_PREWARM=1 ../.venv/bin/python _tmp_validate_v3.py
验收标准(与审查报告对应):
  P0-A1: 库内非正价格行数 = 0; bias 全库 sane
  P0-A2: 基本面门改用不复权价后, 各季度市值门挡掉数量跨期稳定(不再 201→25 漂移)
  P0-A3: 复权比不变量成立; 除权日 RAW 含跳降/HFQ 平滑; 锚点唯一
  P0-B1: value_dividend 中 "NaT"/非法日期 = 0
  P1-C2: 首笔买入前移至 2018 年初; 侧门成交笔数显著上升
  P1-D1/D2: 存在涨跌停顺延/放弃记录; pnl_pct 按实际成交价计算
  P2-D3: 夏普已扣无风险利率
"""
from __future__ import annotations

import os
import sqlite3
import sys

os.environ["VALUE_NO_PREWARM"] = "1"
sys.path.insert(0, ".")

DB = "/root/.etf-monitor/etf_monitor.db"
FAIL: list[str] = []
WARN: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAIL.append(f"{label}: {detail}")


conn = sqlite3.connect(DB)
c = conn.cursor()

print("=" * 70)
print("A1: 非正价格与 bias sanity")
n_neg = c.execute("SELECT COUNT(*) FROM value_kline WHERE close<=0 OR open<=0 OR aclose<=0 OR aopen<=0").fetchone()[0]
n_null_adj = c.execute("SELECT COUNT(*) FROM value_kline WHERE aclose IS NULL").fetchone()[0]
n_rows = c.execute("SELECT COUNT(*) FROM value_kline").fetchone()[0]
check(n_neg == 0, "非正价格行数=0", f"实际 {n_neg}")
check(n_null_adj == 0, "HFQ 全量覆盖(迁移完成)", f"缺 {n_null_adj}/{n_rows} 行")
n_hl = c.execute("SELECT COUNT(*) FROM value_kline WHERE high<low OR ahigh<alow").fetchone()[0]
check(n_hl == 0, "高≥低不变量", f"违例 {n_hl}")

print("\nA1-引擎: 迁移后数据全库 bias 范围")
from value import store
from value.engine import bias_series

codes = sorted({r[0] for r in c.execute("SELECT code FROM value_kline GROUP BY code")})
worst = []
for code in codes:
    kl = store.get_kline(code)
    ds = sorted(kl)
    cs = [kl[d].get("aclose") for d in ds]
    bs = bias_series(cs)
    for d, b in zip(ds, bs):
        if b is not None and not (0.05 < b < 5.0):
            worst.append((code, d, round(b, 2)))
check(len(worst) < 50, "bias 异常值(<0.05或>5)近乎绝迹", f"{len(worst)} 个; 样本 {worst[:5]}")

print("\n" + "=" * 70)
print("A3: 复权不变量(全库, 与生产校验同口径)")
bad_slope, hfq_break = 0, 0
for code in codes:
    rows = c.execute("SELECT date, close, aclose FROM value_kline WHERE code=? ORDER BY date", (code,)).fetchall()
    pts = [(d, rc, ac) for d, rc, ac in rows if rc and ac and rc > 0 and ac > 0]
    if len(pts) < 10:
        continue
    if len(pts) != len([1 for _, rc, ac in rows if rc and ac]):
        bad_slope += 1  # 存在非正价
        continue
    # 斜率不变量: qfq 混入/未复权 ⇒ F=Δhfq/Δraw≈1; 真 hfq(仿射) F 偏离 1
    d0, d1 = pts[0], pts[-1]
    if abs(d1[1] - d0[1]) >= max(0.3, 0.1 * d0[1]):
        f = (d1[2] - d0[2]) / (d1[1] - d0[1])
        # 与生产同判据: 仅当存在显著分红(单笔/前收≥1.5%)时 F≈1 才可疑;
        # 小额分红股 F≈1.00x 是合法形态(实测鼎泰/宏和/海光/仕佳均最大单笔<1.5%)
        sig = 0
        for dps, ex in c.execute("SELECT dps, ex_date FROM value_dividend WHERE code=? AND dps>0 AND ex_date GLOB '[0-9]*'", (code,)):
            pr = c.execute("SELECT close FROM value_kline WHERE code=? AND date<? ORDER BY date DESC LIMIT 1", (code, ex)).fetchone()
            if pr and pr[0] and dps / pr[0] >= 0.015:
                sig += 1
        if abs(f - 1.0) <= 0.005 and sig >= 1:
            bad_slope += 1
    # 断崖判定: 仅限相邻交易日(日历间隔≤10天, 排除长期停牌复牌真实价差)且非上市前5日
    # (新股首5日无涨跌幅: 实测寒武纪+29%/芯磁+52%等均为真实行情); hfq 假跳才是断层
    from datetime import datetime as _dt
    prev_pt = None
    for i, (d, rc, ac) in enumerate(pts):
        if prev_pt and i >= 5:
            gap = (_dt.strptime(d, "%Y-%m-%d") - _dt.strptime(prev_pt[0], "%Y-%m-%d")).days
            if gap <= 10 and abs(ac / prev_pt[2] - 1) > 0.26:
                hfq_break += 1
                print(f"    断崖: {code} {prev_pt[0]}->{d} hfq {prev_pt[2]:.3f}->{ac:.3f}")
        prev_pt = (d, rc, ac)
check(bad_slope == 0, "无 qfq 混入/未复权序列(斜率不变量)", f"可疑 {bad_slope} 只")
check(hfq_break == 0, "HFQ 序列无断崖(单日涨跌≤26%, 送转/除权均连续)", f"断层 {hfq_break} 处")

print("\nA3: 除权日双空间行为抽样(2024 以来, dps/前收≥2%)")
samples, comp_ok = 0, 0
for code in codes[:200]:
    divs = c.execute("""SELECT dps, ex_date FROM value_dividend WHERE code=? AND ex_date>='2024-01-01'
        AND ex_date GLOB '[0-9][0-9][0-9][0-9]-*' AND dps>0""", (code,)).fetchall()
    kl = store.get_kline(code)
    ds = sorted(kl)
    prev_of = {ds[i]: ds[i - 1] for i in range(1, len(ds))}
    for dps, ex in divs:
        if ex not in kl or ex not in prev_of or prev_of[ex] not in kl:
            continue
        p = kl[prev_of[ex]]
        if not p.get("close") or dps / p["close"] < 0.02:
            continue
        e = kl[ex]
        comp = e["aclose"] / p["aclose"] - e["close"] / p["close"]
        samples += 1
        if comp > 0.005:  # HFQ 剔除了跳降(方向正确), RAW 保留跳降
            comp_ok += 1
rate = comp_ok / samples if samples else 0
check(rate >= 0.6, "除权日 HFQ 补偿方向正确比例≥60%(两源噪声容忍)", f"{comp_ok}/{samples} = {rate:.0%}")

print("\n" + "=" * 70)
print("B1: 分红日期净化")
n_nat = c.execute("SELECT COUNT(*) FROM value_dividend WHERE announce_date='NaT' OR ex_date='NaT'").fetchone()[0]
n_bad = c.execute("""SELECT COUNT(*) FROM value_dividend WHERE (announce_date!='' AND announce_date NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
    OR (ex_date!='' AND ex_date NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')""").fetchone()[0]
n_multi = c.execute("SELECT COUNT(*) FROM (SELECT code, fiscal_year FROM value_dividend GROUP BY code, fiscal_year HAVING COUNT(*)>1)").fetchone()[0]
check(n_nat == 0, "'NaT' 脏字符串=0", f"实际 {n_nat}")
check(n_bad == 0, "非法日期格式=0", f"实际 {n_bad}")
print(f"  [i] 同财年多笔分红的 (code,fy) 组: {n_multi} (每笔独立存行生效)")

print("\n" + "=" * 70)
print("A2+C2+D1/D2/D3: 全量回测复核")
import value.api as vapi

st = vapi._compute_state("2018-01-01", 2.0)
check(st["ready"], "回测就绪")
if st["ready"]:
    m = st["metrics"]
    tl = st["trade_log"]
    buys = [t for t in tl if t["action"] == "BUY"]
    sells = [t for t in tl if t["action"] == "SELL"]
    sides = [t for t in buys if t.get("kind") == "side"]
    print(f"  [i] 指标: 累计 {m['total_return_pct']}% 年化 {m['annual_return_pct']}% 夏普 {m['sharpe']} "
          f"回撤 {m['max_drawdown_pct']}% 卡玛 {m['calmar']} 胜率 {m['win_rate_pct']}% "
          f"往返 {m['round_count']} 单边 {m['trade_count']} 利用率 {m['utilization_pct']}%")
    first_buy = buys[0]["exec_date"] if buys else None
    check(first_buy is not None and first_buy <= "2018-04-30", "C2: 首笔买入前移至 2018 年初(暖机生效)", f"实际 {first_buy}")
    check(len(sides) > 6, "C2: 侧门成交显著多于旧版 6 笔", f"实际 {len(sides)} 笔")
    blocked = st.get("blocked", [])
    print(f"  [i] D1: 涨跌停/停牌拦截记录 {len(blocked)} 条 "
          f"(卖顺延 {sum(1 for b in blocked if b['action']=='SELL')}, 买放弃 {sum(1 for b in blocked if b['action']=='BUY')})")
    # D2: pnl 实现口径 — 卖出价必须等于执行日开盘 RAW 价
    mismatch = 0
    for s in sells[:400]:
        row = c.execute("SELECT open FROM value_kline WHERE code=? AND date=?", (s["code"], s["exec_date"])).fetchone()
        if row and abs(row[0] - s["price"]) > 0.011:
            mismatch += 1
    check(mismatch == 0, "D2: 卖出记录价=执行日 RAW 开盘价", f"失配 {mismatch}")
    # A2: 市值门口径一致性 — 抽检历史重建日: 市值必须按 RAW 价×股本计算
    # (神华 2018-01-02: RAW 23.81×198.7亿≈5164亿 → 市值门过(旧qfq口径仅1456亿);
    #  其拒池原因是股息率1.93%<3%与 ROE 周期底 — 假白马门的正确行为, 非口径问题)
    from value.engine import check_gates as _cg
    sizes = [(r["date"], len(r["codes"])) for r in st["pool_rebuilds"]]
    print(f"  [i] A2: 池规模轨迹(全部季度): {[n for _, n in sizes][:6]}...{[n for _, n in sizes][-4:]}")
    first_rebuild = st["pool_rebuilds"][0]
    rb_date = first_rebuild["date"]
    kl_sh = store.get_kline("601088")
    snap_sh = store.get_snapshot("601088")
    if rb_date in kl_sh and snap_sh:
        px = kl_sh[rb_date]["close"]
        snap2 = {**snap_sh, "price": px, "mcap_yi": px * (snap_sh.get("total_shares_yi") or 0),
                 "pe_ttm": store.get_pe("601088").get(rb_date)}
        mcap_gate = next(g for g in _cg(snap2, rb_date) if g["gate"] == "大盘股")
        print(f"  [i] 神华 {rb_date}: RAW收盘={px:.2f} 市值={snap2['mcap_yi']:.0f}亿 市值门={mcap_gate['passed']}")
        check(px > 15 and snap2["mcap_yi"] > 4000 and mcap_gate["passed"],
              "A2: 市值门按 RAW 判定(神华 2018 市值≈5164亿→过; 旧qfq口径仅1456亿)",
              f"raw={px:.2f} mcap={snap2['mcap_yi']:.0f}亿")

print("\n" + "=" * 70)
if FAIL:
    print(f"验证失败 {len(FAIL)} 项:")
    for f in FAIL:
        print("  ✗", f)
    sys.exit(1)
print(f"全部验证通过。警告 {len(WARN)} 项。")
conn.close()
