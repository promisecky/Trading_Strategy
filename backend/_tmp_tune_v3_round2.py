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


DEFAULTS = dict(STOP_BASE=0.92, STOP_WIDEN=0.04, STOP_TIGHTEN=0.05,
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
    def fmt(s):
        return f"年化{s['ann']:+6.2f} 夏普{s['sharpe']:5.2f} 回撤{s['maxdd']:6.2f} 卡玛{s['calmar']:4.2f} 往返{s['rounds']:3d} 胜率{s['win']:5.1f}% 均益{s['avg_pnl']:+5.2f}%"
    print(f"\n### {label}  ({dt:.0f}s)", flush=True)
    print(f"  全段  : {fmt(full)}")
    print(f"  训练段: {fmt(train)}" if train else "  训练段: n/a")
    print(f"  盲测段: {fmt(blind)}" if blind else "  盲测段: n/a")
    return {"label": label, "params": p, "full": full, "train": train, "blind": blind}


# 组合候选 + 邻域稳健性(冠军参数两侧必须同向优良, 防单点噪声峰)
PLAN2 = [
    ({"TP_ARM": 1.10, "TP_TRAIL": 0.96}, "R1: ARM1.10+TRAIL0.96(邻域)"),
    ({"TP_ARM": 1.10, "TP_TRAIL": 0.95}, "R2: ARM1.10+TRAIL0.95(邻域)"),
    ({"TP_ARM": 1.12, "TP_TRAIL": 0.96}, "R3: ARM1.12+TRAIL0.96(邻域)"),
    ({"TP_ARM": 1.15, "TP_TRAIL": 0.95}, "R4: ARM1.15+TRAIL0.95(外推)"),
    ({"TP_ARM": 1.12, "TP_TRAIL": 0.94}, "R5: ARM1.12+TRAIL0.94(外推)"),
    ({"TP_ARM": 1.12, "TP_TRAIL": 0.95}, "C1: ARM1.12+TRAIL0.95(双冠军)"),
    ({"TP_ARM": 1.12, "TP_TRAIL": 0.95, "STOP_BASE": 0.88}, "C2: C1+止损0.88"),
    ({"TP_ARM": 1.12, "TP_TRAIL": 0.95, "STOP_BASE": 0.90}, "C3: C1+止损0.90"),
    ({"TP_ARM": 1.12, "TP_TRAIL": 0.95, "STOP_BASE": 0.90, "STOP_WIDEN": 0.06}, "C4: C3+缩幅放宽0.06"),
    ({"TP_ARM": 1.12, "TP_TRAIL": 0.95, "TP_CAP": 9.99}, "C5: C1+封顶取消"),
    ({"TP_ARM": 1.12, "TP_TRAIL": 0.95, "STOP_BASE": 0.90, "TP_CAP": 9.99, "COOLDOWN_DAYS": 10}, "C6: C3+取消封顶+冷却10"),
]

results = []
for p, label in PLAN2:
    results.append(run(p, label))

print("\n\n==== 汇总表(按全段夏普排序) ====")
print(f"{'方案':<22} {'全段夏普':>7} {'全段年化':>8} {'全段回撤':>8} {'训练夏普':>8} {'盲测夏普':>8} {'盲测年化':>8} {'往返':>4}")
for r in sorted(results, key=lambda x: -x["full"]["sharpe"]):
    tr = r["train"] or {}
    bl = r["blind"] or {}
    print(f"{r['label']:<22} {r['full']['sharpe']:>7.2f} {r['full']['ann']:>7.2f}% {r['full']['maxdd']:>7.2f}% "
          f"{tr.get('sharpe', 0):>8.2f} {bl.get('sharpe', 0):>8.2f} {bl.get('ann', 0):>7.2f}% {r['full']['rounds']:>4d}")

print(f"\n总耗时 {time.time()-t0:.0f}s")
