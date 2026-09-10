"""v3 基线卖出案例诊断 — 参数调优第一步: 先看清 93 笔往返哪里卖早/卖晚。

方法(第一性原理): 每笔 SELL 用 HFQ 空间量化三个事实 —
1. 事后漂移: 卖出后 20/60 交易日该股 HFQ 涨跌 → >+10% 即"卖早"(错失行情);
2. 利润回吐: 持仓期最高浮盈 vs 实现盈亏 → 曾 ≥+15% 但实现 <+5% 即"坐电梯";
3. 规则分层: ①止损/②移动止盈/③bias离场 各自的漂移分布 → 判断各规则的系统性偏差。
另统计买入侧: 深坑深度(bias) 与最终盈亏的关系、满仓错失信号天数。

实验隔离: 独立脚本只读库, 不改生产代码/配置, 不写缓存文件。
"""
from __future__ import annotations

import os
import sys
import time

os.environ["VALUE_NO_PREWARM"] = "1"
sys.path.insert(0, ".")

t0 = time.time()
from value import store
from value.backtest import run_backtest
from value.config import IDLE_FUND_CODE, IDLE_YIELD_PCT

# ---- 数据加载(只读, 与 api._inputs_from_db 同口径但不写落盘缓存) ----
counts = store.kline_row_counts()
missing_adj = set(store.codes_missing_adj())
codes = sorted(c for c in counts if counts[c] > 0 and c not in missing_adj)
kline = {c: store.get_kline(c) for c in codes}
dates = sorted({d for kl in kline.values() for d in kl})
snapshots, pe = {}, {}
for c in codes:
    snap = store.get_snapshot(c)
    if snap:
        snapshots[c] = snap
    pe[c] = store.get_pe(c)
fund = kline.get(IDLE_FUND_CODE, {})
idle_close = {d: r["aclose"] for d, r in fund.items() if r.get("aclose")} or None
print(f"数据: {len(codes)} 只, {len(dates)} 交易日, 加载 {time.time()-t0:.0f}s")

res = run_backtest(dates, kline, snapshots, pe, IDLE_YIELD_PCT, idle_close=idle_close, start="2018-01-01")
m = res["metrics"]
print(f"基线: 累计{m['total_return_pct']}% 年化{m['annual_return_pct']}% 夏普{m['sharpe']} 回撤{m['max_drawdown_pct']}% "
      f"卡玛{m['calmar']} 胜率{m['win_rate_pct']}% 往返{m['round_count']} 利用率{m['utilization_pct']}%")
print(f"回测耗时 {time.time()-t0:.0f}s\n")

# ---- 日期索引(HFQ 事后漂移用) ----
date_idx = {d: i for i, d in enumerate(dates)}


def adj_series(code: str) -> list[tuple[str, float | None]]:
    kl = kline[code]
    out = []
    last = None
    for d in dates:
        row = kl.get(d)
        if row and row.get("aclose"):
            last = row["aclose"]
        out.append((d, last))
    return out


def drift_after(code: str, exec_date: str, n: int) -> float | None:
    """卖出执行日后第 n 个交易日的 HFQ 涨跌(执行日开盘近似为基准收盘)。"""
    ser = adj_series(code)
    i = date_idx.get(exec_date)
    if i is None or i + n >= len(ser):
        return None
    base = ser[i][1]
    after = ser[i + n][1]
    return (after / base - 1) if base and after else None


# ---- 逐笔 SELL 诊断 ----
buys_by_code: dict[str, list[dict]] = {}
for t in res["trade_log"]:
    if t["action"] == "BUY":
        buys_by_code.setdefault(t["code"], []).append(t)

rows = []
for s in res["trade_log"]:
    if s["action"] != "SELL":
        continue
    code = s["code"]
    buy = next((b for b in reversed(buys_by_code.get(code, [])) if b["exec_date"] <= s["exec_date"]), None)
    if buy is None:
        continue
    ser = adj_series(code)
    bi, si = date_idx[buy["exec_date"]], date_idx[s["exec_date"]]
    hold_max = max((v for _, v in ser[bi:si + 1] if v), default=None)
    cost_adj = hold_max and None
    # 持仓期最高浮盈(以 HFQ 成本计)
    kl = kline[code]
    cost = kl[buy["exec_date"]]["aopen"]
    max_gain = (hold_max / cost - 1) * 100 if hold_max and cost else 0.0
    hold_days = si - bi
    rows.append({
        "code": code, "name": s.get("name", ""), "rule": s["rule"],
        "buy": buy["exec_date"], "sell": s["exec_date"], "hold": hold_days,
        "pnl": s["pnl_pct"], "max_gain": round(max_gain, 1),
        "d20": drift_after(code, s["exec_date"], 20),
        "d60": drift_after(code, s["exec_date"], 60),
        "reason": s["reason"][:34],
    })

print(f"==== 卖出案例诊断({len(rows)} 笔) ====")
for rule in (1, 2, 3):
    g = [r for r in rows if r["rule"] == rule]
    if not g:
        continue
    d20 = [r["d20"] for r in g if r["d20"] is not None]
    d60 = [r["d60"] for r in g if r["d60"] is not None]
    early20 = sum(1 for x in d20 if x > 0.10)
    early60 = sum(1 for x in d60 if x > 0.10)
    cont_down = sum(1 for x in d20 if x < -0.05)
    pnls = sorted(r["pnl"] for r in g)
    med = pnls[len(pnls) // 2]
    print(f"\n规则{rule}: {len(g)} 笔 | pnl 均{sum(pnls)/len(pnls):+.1f}% 中位{med:+.1f}% "
          f"[min{pnls[0]:+.1f} max{pnls[-1]:+.1f}] 胜率{sum(1 for p in pnls if p>0)/len(pnls):.0%}")
    if d20:
        print(f"  卖后20日: 均{sum(d20)/len(d20):+.1%} 中位{sorted(d20)[len(d20)//2]:+.1%} "
              f"| >+10%(卖早) {early20}/{len(d20)} | <-5%(续跌,卖对) {cont_down}/{len(d20)}")
    if d60:
        print(f"  卖后60日: 均{sum(d60)/len(d60):+.1%} | >+10%(卖早) {early60}/{len(d60)}")
    gave_back = [r for r in g if r["max_gain"] >= 15 and r["pnl"] < 5]
    if gave_back:
        print(f"  坐电梯(曾浮盈≥15%但实现<5%): {len(gave_back)} 笔")

print("\n==== 最典型'卖早'案例 TOP12(卖后60日涨幅最大) ====")
for r in sorted([r for r in rows if r["d60"] is not None], key=lambda x: -x["d60"])[:12]:
    print(f"  规则{r['rule']} {r['code']} {r['name']:<5} 买{r['buy']} 卖{r['sell']} 持{r['hold']}日 "
          f"pnl{r['pnl']:+6.1f}% 峰值浮盈{r['max_gain']:+6.1f}% 卖后20日{r['d20'] if r['d20'] is None else format(r['d20'],'+.1%')} "
          f"60日{r['d60']:+.1%} | {r['reason']}")

print("\n==== 最典型'坐电梯'案例(峰值浮盈-实现 差距最大) TOP8 ====")
for r in sorted(rows, key=lambda x: -(x["max_gain"] - x["pnl"]))[:8]:
    print(f"  规则{r['rule']} {r['code']} {r['name']:<5} 峰值{r['max_gain']:+.1f}% → 实现{r['pnl']:+.1f}% "
          f"(回吐{r['max_gain']-r['pnl']:.1f}pp) 持{r['hold']}日 卖后20日{r['d20'] if r['d20'] is None else format(r['d20'],'+.1%')}")

print("\n==== 止损(规则1)细节: 洗盘 vs 真跌 ====")
g1 = [r for r in rows if r["rule"] == 1]
wash = [r for r in g1 if r["d20"] is not None and r["d20"] > 0.10]
right = [r for r in g1 if r["d20"] is not None and r["d20"] < -0.05]
print(f"止损 {len(g1)} 笔: 卖后20日>+10%(被洗/杀错) {len(wash)} 笔, <-5%(躲过续跌) {len(right)} 笔, 其余中性")
for r in wash[:6]:
    print(f"  杀错: {r['code']} {r['name']:<5} 卖{r['sell']} pnl{r['pnl']:+.1f}% 后20日{r['d20']:+.1%} | {r['reason']}")

# ---- 买入侧: 坑深与收益 ----
print("\n==== 买入侧: 信号日 bias 深度 vs 实现盈亏 ====")
mains = [t for t in res["trade_log"] if t["action"] == "BUY" and t.get("kind") == "main"]
import re
buckets = {}
for b in mains:
    mt = re.search(r"bias ([\d.]+)", b["reason"])
    if not mt:
        continue
    bias = float(mt.group(1))
    sell = next((s for s in rows if s["code"] == b["code"] and s["buy"] == b["exec_date"]), None)
    if sell is None:
        continue
    key = "<=0.82" if bias <= 0.82 else ("0.82-0.85" if bias <= 0.85 else "0.85-0.88")
    buckets.setdefault(key, []).append(sell["pnl"])
for k in ("<=0.82", "0.82-0.85", "0.85-0.88"):
    v = buckets.get(k, [])
    if v:
        print(f"  bias {k}: {len(v)} 笔 均pnl {sum(v)/len(v):+.1f}% 胜率 {sum(1 for x in v if x>0)/len(v):.0%}")

# ---- 满仓错失 ----
print("\n==== 满仓/冷却错失(买入被拒) ====")
blocked_buy = [b for b in res["blocked"] if b["action"] == "BUY"]
print(f"涨停/停牌放弃买入: {len(blocked_buy)} 条")
# 满仓日中池内主信号出现次数(粗估机会成本)
full_days = sum(1 for h in res["history"] if h["position_pct"] > 78)
print(f"接近满仓(>78%)天数: {full_days}/{len(res['history'])} ({full_days/len(res['history']):.0%})")

print(f"\n总耗时 {time.time()-t0:.0f}s")
