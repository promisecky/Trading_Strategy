"""v3.1 结果特征深挖诊断 — 寻找结构性优化空间(只读实验, 零生产改动)。

观察问题:
1. 止损①的时间聚类: 是否集中在市场崩盘月(系统性误杀) → 市场状态豁免的依据;
2. 止损的行业/个股聚类: 周期股是否被"白马门"误判(深坑是周期陷阱非恐慌错杀);
3. 止损的持仓天数: "企稳一日游"(持有≤7日即止损)占比 → 企稳确认强度的依据;
4. v3.1 残留电梯案例: 峰值浮盈≥+6%但实现≤0(ARM=1.10 激活线下的裸奔区);
5. 盈利单峰值分布: 多少单摸到过 +10% 激活线? 5-10% 裸奔区占多大比例;
6. 侧门 13 笔的独立质量;
7. 满仓期错失的买入信号量(机会成本上界);
8. 市场代理: 全池等权 bias 均值在止损日的水平(验证"个股深坑+市场崩盘=黄金坑"假设)。
"""
from __future__ import annotations

import os
import re
import sys
import time
from collections import Counter

os.environ["VALUE_NO_PREWARM"] = "1"
sys.path.insert(0, ".")

t0 = time.time()
import json
import sqlite3

STATE = json.load(open("/root/.etf-monitor/value_state.json"))
assert STATE.get("rules") == "v3.1-arm110", f"落盘状态非 v3.1: {STATE.get('rules')}"
st = STATE["state"]
res = {"trade_log": st["trade_log"], "history": st["history"], "metrics": st["metrics"]}
dates = [h["date"] for h in st["history"]]
m = res["metrics"]
print(f"复用生产落盘 v3.1 回测: 年化{m['annual_return_pct']}% 夏普{m['sharpe']} 回撤{m['max_drawdown_pct']}% 往返{m['round_count']} 胜率{m['win_rate_pct']}% ({dates[0]}~{dates[-1]})")

conn = sqlite3.connect("/root/.etf-monitor/etf_monitor.db")
cur = conn.cursor()
trade_codes = sorted({t["code"] for t in res["trade_log"]})
print(f"交易涉及个股: {len(trade_codes)} 只")

kl_min = {}
for code in trade_codes:
    rows = cur.execute("SELECT date, aclose, aopen FROM value_kline WHERE code=? ORDER BY date", (code,)).fetchall()
    kl_min[code] = {d: (ac, ao) for d, ac, ao in rows if ac}

all_dates = [r[0] for r in cur.execute("SELECT DISTINCT date FROM value_kline WHERE date>='2017-01-01' ORDER BY date")]
date_idx = {d: i for i, d in enumerate(all_dates)}

snap_ind = {}
for code in trade_codes:
    r = cur.execute("SELECT industry FROM value_stock_info WHERE code=?", (code,)).fetchone()
    snap_ind[code] = r[0] if r else "?"

import random
random.seed(42)
sample = [r[0] for r in cur.execute("SELECT code FROM value_stock_info ORDER BY RANDOM() LIMIT 120")]
from value.engine import bias_series
mkt_sum = [0.0] * len(all_dates)
mkt_cnt = [0] * len(all_dates)
for c in sample:
    rows = cur.execute("SELECT date, aclose FROM value_kline WHERE code=? ORDER BY date", (c,)).fetchall()
    if len(rows) < 400:
        continue
    kd = dict(rows)
    cs, last = [], None
    for d in all_dates:
        v = kd.get(d)
        if v:
            last = v
        cs.append(last)
    bs = bias_series(cs)
    for i, b in enumerate(bs):
        if b is not None:
            mkt_sum[i] += b
            mkt_cnt[i] += 1
    del cs, bs, kd
mkt_bias = [mkt_sum[i] / mkt_cnt[i] if mkt_cnt[i] else None for i in range(len(all_dates))]
print(f"市场代理完成 {time.time()-t0:.0f}s")

def adj_closes(code):
    kd = kl_min[code]
    out, last = [], None
    for d in all_dates:
        row = kd.get(d)
        if row:
            last = row[0]
        out.append(last)
    return out

buys_by_code = {}
for t in res["trade_log"]:
    if t["action"] == "BUY":
        buys_by_code.setdefault(t["code"], []).append(t)

records = []
for s in res["trade_log"]:
    if s["action"] != "SELL":
        continue
    code = s["code"]
    b = next((x for x in reversed(buys_by_code.get(code, [])) if x["exec_date"] <= s["exec_date"]), None)
    if b is None:
        continue
    ser = adj_closes(code)
    bi, si = date_idx[b["exec_date"]], date_idx[s["exec_date"]]
    cost = kl_min[code][b["exec_date"]][1]  # aopen
    peak = max(v for v in ser[bi:si + 1] if v)
    records.append({
        "code": code, "name": s.get("name", ""), "rule": s["rule"], "kind": b.get("kind"),
        "industry": snap_ind.get(code, "?"),
        "buy": b["exec_date"], "sell": s["exec_date"], "hold": si - bi,
        "pnl": s["pnl_pct"], "peak_gain": (peak / cost - 1) * 100,
        "mkt_bias_buy": mkt_bias[bi],
    })
sells = records
print(f"==== 1) 止损①({sum(1 for r in sells if r['rule']==1)}笔) 时间分布 ====")
sl = [r for r in sells if r["rule"] == 1]
by_month = Counter(r["sell"][:7] for r in sl)
for ym, n in sorted(by_month.items()):
    print(f"  {ym}: {'█'*n} {n}")
by_year = Counter(r["sell"][:4] for r in sl)
print("  年度:", dict(sorted(by_year.items())))

print("\n==== 2) 止损的行业/个股聚类 ====")
ind = Counter(r["industry"] for r in sl)
for k, n in ind.most_common(8):
    print(f"  {k}: {n} 笔")
nm = Counter(f"{r['code']}{r['name']}" for r in sl)
print("  多次止损个股:", {k: v for k, v in nm.items() if v >= 2})

print("\n==== 3) 止损持仓天数(企稳一日游检测) ====")
h = sorted(r["hold"] for r in sl)
le5 = sum(1 for x in h if x <= 5)
le10 = sum(1 for x in h if x <= 10)
print(f"  ≤5日: {le5}/{len(h)}  ≤10日: {le10}/{len(h)}  中位: {h[len(h)//2]}日  分布: {h}")

print("\n==== 4) 买入日市场水位 vs 止损误杀(mkt_bias<0.92=市场深坑) ====")
for tag, grp in (("市场深坑日买入(mkt_bias<0.92)", [r for r in sl if r["mkt_bias_buy"] and r["mkt_bias_buy"] < 0.92]),
                 ("市场正常日买入(≥0.92)", [r for r in sl if r["mkt_bias_buy"] and r["mkt_bias_buy"] >= 0.92])):
    if grp:
        avg = sum(r["pnl"] for r in grp) / len(grp)
        print(f"  止损单中 {tag}: {len(grp)} 笔, 均亏 {avg:+.1f}%")
all_deep = [r for r in sells if r["mkt_bias_buy"] and r["mkt_bias_buy"] < 0.92]
all_norm = [r for r in sells if r["mkt_bias_buy"] and r["mkt_bias_buy"] >= 0.92]
for tag, grp in (("市场深坑日", all_deep), ("市场正常日", all_norm)):
    if grp:
        print(f"  全部卖出中 {tag}买入: {len(grp)} 笔 均pnl {sum(r['pnl'] for r in grp)/len(grp):+.2f}% 胜率 {sum(1 for r in grp if r['pnl']>0)/len(grp):.0%}")

print("\n==== 5) v3.1 残留电梯案例(峰值≥+6% 但实现≤0) ====")
elev = [r for r in sells if r["peak_gain"] >= 6 and r["pnl"] <= 0]
print(f"  {len(elev)} 笔:")
for r in sorted(elev, key=lambda x: x["peak_gain"] - x["pnl"], reverse=True)[:10]:
    print(f"    规则{r['rule']} {r['code']} {r['name']:<5} 峰值{r['peak_gain']:+.1f}% → 实现{r['pnl']:+.1f}% 持{r['hold']}日 买{r['buy']}")

print("\n==== 6) 盈利单峰值浮盈分布(裸奔区检测) ====")
wins = [r for r in sells if r["pnl"] > 0]
b_ = Counter()
for r in wins:
    pk = r["peak_gain"]
    b_["峰值<5%"] += pk < 5
    b_["峰值5-10%(激活线以下裸奔)"] += 5 <= pk < 10
    b_["峰值10-15%"] += 10 <= pk < 15
    b_["峰值≥15%"] += pk >= 15
for k in ("峰值<5%", "峰值5-10%(激活线以下裸奔)", "峰值10-15%", "峰值≥15%"):
    print(f"  {k}: {b_[k]} 笔")
mid = [r for r in wins if 5 <= r["peak_gain"] < 10]
if mid:
    print(f"  5-10%裸奔区实现均益: {sum(r['pnl'] for r in mid)/len(mid):+.2f}% (峰值均 {sum(r['peak_gain'] for r in mid)/len(mid):+.1f}%)")

print("\n==== 7) 侧门成交质量 ====")
side = [r for r in sells if r["kind"] == "side"]
main = [r for r in sells if r["kind"] == "main"]
if side:
    print(f"  侧门 {len(side)} 笔: 均pnl {sum(r['pnl'] for r in side)/len(side):+.2f}% 胜率 {sum(1 for r in side if r['pnl']>0)/len(side):.0%}")
    for r in side:
        print(f"    {r['buy']} {r['code']} {r['name']:<5} 规则{r['rule']} pnl{r['pnl']:+.1f}%")
print(f"  主信号 {len(main)} 笔: 均pnl {sum(r['pnl'] for r in main)/len(main):+.2f}% 胜率 {sum(1 for r in main if r['pnl']>0)/len(main):.0%}")

print(f"\n总耗时 {time.time()-t0:.0f}s")
