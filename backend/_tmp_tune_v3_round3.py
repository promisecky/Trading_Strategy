"""v3 卖出参数单因素扫描 + Walk-Forward 分段评估 — 实验隔离脚本(零生产改动)。

诊断驱动的实验方向(见 _tmp_diagnose_v3.py 输出):
- 止损(规则①)30笔卖后20日均+5.0%、9/30被洗 → 测 基准放宽/收紧减弱/冷却缩短;
- 峰值+8%→实现-9% 的电梯 → 测 TP_ARM 提前激活;
- 规则② 60日尾部让利 → 测 TP_TRAIL/TP_CAP 两向;
- 规则③表现最好(均+22.1%) → 不动 BIAS_EXIT。

方法严谨性:
- 指标函数(bias/z/act)按调用序缓存(参数只影响交易环, 指标序列与参数无关), 
  每轮前清计数器对齐遍历顺序;
- Walk-Forward: 同一因果回测按 2022-12-31 切段, 训练段选参 → 盲测段验证无衰减;
- 参数经 monkeypatch 注入模块常量, 生产代码/配置零改动。
"""
from __future__ import annotations

import os
import sys
import time

os.environ["VALUE_NO_PREWARM"] = "1"
sys.path.insert(0, ".")

t0 = time.time()
from value import store
import value.backtest as vb
import value.engine as ve
from value.backtest import run_backtest
from value.config import IDLE_FUND_CODE, IDLE_YIELD_PCT, TRADING_DAYS_PER_YEAR

# ---- 数据加载(只读) ----
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
print(f"数据: {len(codes)} 只 {len(dates)} 日, 加载 {time.time()-t0:.0f}s", flush=True)
date_idx = {d: i for i, d in enumerate(dates)}

# ---- 指标函数调用序缓存(遍历顺序由 dict 插入序决定, 进程内稳定) ----
_counters = {"bias": 0, "z": 0, "act": 0}
_caches: dict[str, list] = {"bias": [], "z": [], "act": []}
_orig = {"bias": vb.bias_series, "z": vb.z_series, "act": vb.activity_series}


def _cached(key):
    def fn(x):
        i = _counters[key]
        _counters[key] += 1
        if i < len(_caches[key]):
            return _caches[key][i]
        r = _orig[key](x)
        _caches[key].append(r)
        return r
    return fn


vb.bias_series = _cached("bias")
vb.z_series = _cached("z")
vb.activity_series = _cached("act")


def reset_counters():
    for k in _counters:
        _counters[k] = 0


# ---- 分段指标(与生产 _metrics 同口径: 夏普扣2%无风险, 胜率按实现pnl) ----
def seg_metrics(history, trade_log, lo=None, hi=None):
    hs = [h for h in history if (lo is None or h["date"] > lo) and (hi is None or h["date"] <= hi)]
    if len(hs) < 30:
        return None
    eq = [h["equity"] for h in hs]
    # 段起点用前一日权益(存在时)保证首日收益计入
    i0 = history.index(hs[0])
    if i0 > 0:
        eq = [history[i0 - 1]["equity"]] + eq
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1] > 0]
    rf = 2.0 / 100 / TRADING_DAYS_PER_YEAR
    ex = [r - rf for r in rets]
    n_years = len(hs) / TRADING_DAYS_PER_YEAR
    ann = (eq[-1] / eq[0]) ** (1 / n_years) - 1 if n_years > 0 and eq[0] > 0 else 0
    mean = sum(ex) / len(ex) if ex else 0
    var = sum((r - mean) ** 2 for r in ex) / len(ex) if ex else 0
    sharpe = mean / var**0.5 * TRADING_DAYS_PER_YEAR**0.5 if var > 0 else 0
    peak, maxdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        maxdd = min(maxdd, v / peak - 1)
    seg_trades = [t for t in trade_log if t["action"] == "SELL" and (lo is None or t["exec_date"] > lo) and (hi is None or t["exec_date"] <= hi)]
    wins = sum(1 for t in seg_trades if (t.get("pnl_pct") or 0) > 0)
    return {
        "ann": ann * 100, "sharpe": sharpe, "maxdd": maxdd * 100,
        "calmar": ann / abs(maxdd) if maxdd < 0 else 0,
        "rounds": len(seg_trades),
        "win": wins / len(seg_trades) * 100 if seg_trades else 0,
        "avg_pnl": sum(t.get("pnl_pct") or 0 for t in seg_trades) / len(seg_trades) if seg_trades else 0,
    }


DEFAULTS = dict(STOP_BASE=0.92, STOP_WIDEN=0.04, STOP_TIGHTEN=0.05, BIAS_EXIT=1.08,
                TP_ARM=1.08, TP_TRAIL=0.96, TP_CAP=1.30, COOLDOWN_DAYS=40)


def apply_params(p: dict):
    for k, v in {**DEFAULTS, **p}.items():
        if k == "COOLDOWN_DAYS":
            vb.COOLDOWN_DAYS = v
        else:
            setattr(ve, k, v)


def run(p: dict, label: str):
    apply_params(p)
    reset_counters()
    t = time.time()
    res = run_backtest(dates, kline, snapshots, pe, IDLE_YIELD_PCT, idle_close=idle_close, start="2018-01-01")
    full = seg_metrics(res["history"], res["trade_log"])
    train = seg_metrics(res["history"], res["trade_log"], hi="2022-12-31")
    blind = seg_metrics(res["history"], res["trade_log"], lo="2022-12-31")
    dt = time.time() - t
    sells = [x for x in res["trade_log"] if x["action"] == "SELL"]
    r1 = sum(1 for x in sells if x["rule"] == 1)
    r2 = sum(1 for x in sells if x["rule"] == 2)
    r3 = sum(1 for x in sells if x["rule"] == 3)
    buys_by_code = {}
    for x in res["trade_log"]:
        if x["action"] == "BUY":
            buys_by_code.setdefault(x["code"], []).append(x)
    holds = []
    for s_ in sells:
        b = next((bb for bb in reversed(buys_by_code.get(s_["code"], [])) if bb["exec_date"] <= s_["exec_date"]), None)
        if b:
            holds.append(date_idx[s_["exec_date"]] - date_idx[b["exec_date"]])
    dist = f"出口分布 ①止损{r1} ②止盈{r2} ③bias{r3} | 均持仓{sum(holds)/len(holds):.0f}日" if holds else "无交易"
    def fmt(s):
        return f"年化{s['ann']:+6.2f} 夏普{s['sharpe']:5.2f} 回撤{s['maxdd']:6.2f} 卡玛{s['calmar']:4.2f} 往返{s['rounds']:3d} 胜率{s['win']:5.1f}% 均益{s['avg_pnl']:+5.2f}%"
    print(f"\n### {label}  ({dt:.0f}s)", flush=True)
    print(f"  全段  : {fmt(full)}")
    print(f"  训练段: {fmt(train)}" if train else "  训练段: n/a")
    print(f"  盲测段: {fmt(blind)}" if blind else "  盲测段: n/a")
    print(f"  {dist}")
    return {"label": label, "params": p, "full": full, "train": train, "blind": blind, "dist": (r1, r2, r3)}


# ---- 第三轮: bias离场(规则③)主导实验 — 用户标准: 收益优先, 回撤约束, WF 分段 ----
# 自定义卖出: 无回落止盈, 仅 +30% 封顶 + ①止损 + ③bias(实验脚本内实现, 零生产改动)
def sell_cap_only(positions, quotes_by_code):
    out = []
    for code, pos in positions.items():
        q = quotes_by_code.get(code)
        cost = pos.get("cost")
        if q is None or not cost:
            continue
        close = q["close"]
        s = ve.dynamic_stop(q.get("act"))
        if close <= cost * s:
            out.append({"code": code, "rule": 1, "reason": "①动态止损"})
            continue
        if close >= cost * ve.TP_CAP:
            out.append({"code": code, "rule": 2, "reason": "②+30%封顶"})
            continue
        if q.get("bias") is not None and q["bias"] >= ve.BIAS_EXIT:
            out.append({"code": code, "rule": 3, "reason": "③情绪修复"})
    return out

_orig_sell = vb.sell_signals

PLAN = [
    ({"TP_ARM": 1.10, "TP_TRAIL": 0.96}, "B0: v3.1 生产基线(ARM1.10)"),
    ({"TP_ARM": 1.12, "TP_TRAIL": 0.96}, "B1: ARM1.12(对照复核)"),
    ({"TP_ARM": 1.15, "TP_TRAIL": 0.96}, "B2: ARM1.15"),
    ({"TP_ARM": 1.20, "TP_TRAIL": 0.96}, "B3: ARM1.20(半取消②)"),
    ({"TP_ARM": 1.25, "TP_TRAIL": 0.96}, "B4: ARM1.25(仅大肉锁利)"),
    ({"TP_ARM": 9.99}, "B5: 完全取消②(纯①+③)"),
    ({"TP_ARM": 9.99, "BIAS_EXIT": 1.12}, "B6: 纯①③+bias放宽1.12"),
    ({"TP_ARM": 9.99, "BIAS_EXIT": 1.05}, "B7: 纯①③+bias收紧1.05"),
    ({"TP_ARM": 1.10, "BIAS_EXIT": 1.12}, "B8: v3.1+bias放宽1.12"),
    ({"TP_ARM": 1.10, "BIAS_EXIT": 1.05}, "B9: v3.1+bias收紧1.05"),
    ({"TP_ARM": 1.20, "BIAS_EXIT": 1.12}, "B10: ARM1.20+bias1.12"),
]

results = []
for p, label in PLAN:
    results.append(run(p, label))

# B11: 封顶模式(无回落止盈, 仅+30%封顶) — 自定义卖出函数
vb.sell_signals = sell_cap_only
try:
    results.append(run({"TP_ARM": 1.10, "TP_TRAIL": 0.96}, "B11: 封顶模式(仅+30%cap+①③)"))
finally:
    vb.sell_signals = _orig_sell

print("\n\n==== 汇总表(按全段年化排序 — 用户标准: 收益优先) ====")
print(f"{'方案':<26} {'年化':>7} {'回撤':>7} {'夏普':>5} {'卡玛':>5} {'训练夏普':>7} {'盲测夏普':>7} {'盲测年化':>7} {'往返':>4} {'①/②/③':>9}")
for r in sorted(results, key=lambda x: -x["full"]["ann"]):
    tr = r["train"] or {}
    bl = r["blind"] or {}
    d = r.get("dist", (0,0,0))
    print(f"{r['label']:<26} {r['full']['ann']:>6.2f}% {r['full']['maxdd']:>6.2f}% {r['full']['sharpe']:>5.2f} {r['full']['calmar']:>5.2f} "
          f"{tr.get('sharpe', 0):>7.2f} {bl.get('sharpe', 0):>7.2f} {bl.get('ann', 0):>6.2f}% {r['full']['rounds']:>4d} {d[0]:>3}/{d[1]:<2}/{d[2]:<2}")

print(f"\n总耗时 {time.time()-t0:.0f}s")
