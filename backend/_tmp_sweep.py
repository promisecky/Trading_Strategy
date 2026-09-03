"""临时参数敏感性实验(_tmp 前缀, 跑完即删; 只读数据库, 不影响运行中的系统)。"""
import sys
import time

sys.path.insert(0, ".")

import value.backtest as B
import value.engine as E
from value import store

t0 = time.time()
counts = store.kline_row_counts()
codes = sorted(c for c in counts if counts[c] > 0)
kline = {c: store.get_kline(c) for c in codes}
dates = [d for d in sorted({d for kl in kline.values() for d in kl}) if d >= "2018-01-01"]
snapshots = {}
pe = {}
for c in codes:
    s = store.get_snapshot(c)
    if s:
        snapshots[c] = s
    pe[c] = store.get_pe(c)
print(f"[inputs] {time.time() - t0:.0f}s, codes={len(codes)}, days={len(dates)}")

BASE = dict(sl=0.88, tp=1.10, be=1.08, cd=60)


def run(sl, tp, be, cd):
    E.STOP_LOSS, E.TAKE_PROFIT, E.BIAS_EXIT, B.COOLDOWN_DAYS = sl, tp, be, cd
    m = B.run_backtest(dates, kline, snapshots, pe)["metrics"]
    return m


def show(tag, m):
    print(
        f"{tag:<22} 累计{m['total_return_pct']:>8.1f}% 年化{m['annual_return_pct']:>6.2f}% "
        f"夏普{m['sharpe']:>5.2f} 最大回撤{m['max_drawdown_pct']:>7.2f}% 胜率{m['win_rate_pct']:>5.1f}% "
        f"往返{m['round_count']:>4} 利用率{m['utilization_pct']:>5.1f}%"
    )


show("基线(0.88/1.10/1.08/60)", run(**BASE))
print("--- 止损 STOP_LOSS 扫描 ---")
for v in [0.80, 0.84, 0.88, 0.90, 0.92, 0.94]:
    show(f"止损 {v:.2f}", run(sl=v, tp=BASE["tp"], be=BASE["be"], cd=BASE["cd"]))
print("--- 止盈 TAKE_PROFIT 扫描 ---")
for v in [1.08, 1.10, 1.15, 1.20, 1.30, 1.50]:
    show(f"止盈 {v:.2f}", run(sl=BASE["sl"], tp=v, be=BASE["be"], cd=BASE["cd"]))
print("--- 过热离场 BIAS_EXIT 扫描 ---")
for v in [1.05, 1.08, 1.12, 1.16, 1.20]:
    show(f"离场 bias {v:.2f}", run(sl=BASE["sl"], tp=BASE["tp"], be=v, cd=BASE["cd"]))
print("--- 冷却 COOLDOWN 扫描 ---")
for v in [20, 40, 60, 90, 120]:
    show(f"冷却 {v}日", run(sl=BASE["sl"], tp=BASE["tp"], be=BASE["be"], cd=v))
print("--- 组合尝试(放宽止损+提高止盈) ---")
for sl, tp in [(0.84, 1.20), (0.84, 1.30), (0.80, 1.30), (0.90, 1.20)]:
    show(f"止损{sl:.2f}+止盈{tp:.2f}", run(sl=sl, tp=tp, be=BASE["be"], cd=BASE["cd"]))
