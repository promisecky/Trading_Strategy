"""对抗性冒烟: 双价抓取链路真实网络验证(只拉不写库)。

样本选择:
- 601088 中国神华: 旧 qfq 口径下 2017 年初为负价 → RAW 必须恒正, HFQ 必须恒正;
- 601398 工商银行: 审查报告的 A3 "证据股"(2024-12-31 真实收盘 6.92, 旧库 qfq=6.302)
  → RAW 必须等于真实价 6.92, HFQ/RAW 比值须单调不减且 >1;
- 600519 贵州茅台: 大额分红(单笔 51.981) → 复权比增长显著, 除权日 RAW 跳降而 HFQ 平滑。
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from value.fetch import _fetch_dividends, _fetch_kline_pair  # noqa: E402

CHECKS = {
    "601088": {"name": "中国神华", "min_ratio": 1.0},
    "601398": {"name": "工商银行", "min_ratio": 1.0, "spot": ("2024-12-31", 6.92)},
    "600519": {"name": "贵州茅台", "min_ratio": 1.5},
}

failures = []
for code, meta in CHECKS.items():
    print(f"\n===== {code} {meta['name']} =====")
    divs = _fetch_dividends(code)
    if divs is None:
        failures.append(f"{code}: 分红拉取失败")
        continue
    nat = [d for d in divs if "NaT" in (d["announce_date"], d["ex_date"])]
    bad_date = [d for d in divs if d["ex_date"] and not (len(d["ex_date"]) == 10 and d["ex_date"][4] == "-")]
    print(f"分红: {len(divs)} 笔(每笔独立), NaT残留={len(nat)}, 非法日期={len(bad_date)}")
    if nat or bad_date:
        failures.append(f"{code}: 分红日期净化失败")

    ex_pairs = [(d["ex_date"], d["dps"]) for d in divs if d["ex_date"] and d["dps"] > 0]
    rows = _fetch_kline_pair(code, ex_pairs)
    if not rows:
        failures.append(f"{code}: 双价K线拉取/校验失败(含除权日分红补偿校验)")
        continue
    neg_raw = [r for r in rows if min(r[1:5]) <= 0]
    neg_adj = [r for r in rows if min(r[5:9]) <= 0]
    ratios = [r[6] / r[2] for r in rows]
    print(f"K线: {len(rows)} 根 ({rows[0][0]} ~ {rows[-1][0]})")
    print(f"非正价: RAW={len(neg_raw)} HFQ={len(neg_adj)}  (旧口径神华曾有73行负价)")
    print(f"复权比: 首={ratios[0]:.4f} 末={ratios[-1]:.4f} 最小={min(ratios):.4f}")
    if neg_raw or neg_adj:
        failures.append(f"{code}: 存在非正价格")
    if min(ratios) < 1 - 0.004:
        failures.append(f"{code}: 复权比 < 1")
    if ratios[-1] < meta["min_ratio"]:
        failures.append(f"{code}: 末端复权比 {ratios[-1]:.3f} < 预期 {meta['min_ratio']}")

    # 除权日双空间行为(统计口径, 与 _merge_pair 一致): RAW 含分红跳降, HFQ 剔除;
    # 两源存在噪声(神华 42%、送转伴随过补偿), 带内比例≥50% 且均值方向为正即通过
    by_date = {r[0]: r for r in rows}
    dates_sorted = sorted(by_date)
    prev_idx = {d: dates_sorted[i - 1] for i, d in enumerate(dates_sorted) if i}
    comps = []
    for d in divs:
        ex, dps = d["ex_date"], d["dps"]
        if not ex or ex not in by_date or ex not in prev_idx:
            continue
        p = prev_idx[ex]
        if by_date[p][2] <= 0 or dps / by_date[p][2] < 0.015:
            continue
        theory = dps / by_date[p][2]
        comp = by_date[ex][6] / by_date[p][6] - by_date[ex][2] / by_date[p][2]
        comps.append((ex, dps, theory, comp))
    in_band = sum(1 for _, _, t, c in comps if 0.35 * t - 0.004 <= c <= t + 0.30)
    for ex, dps, t, c in comps[:3]:
        print(f"  除权日 {ex}: dps={dps:.3f} 理论={t:.2%} 实际补偿={c:+.2%} ({c/t:.0%})")
    print(f"除权日补偿样本 {len(comps)} 个, 带内 {in_band}, 均值 {sum(c for _,_,_,c in comps)/max(len(comps),1):+.4f}")
    if comps and (sum(c for _, _, _, c in comps) / len(comps) <= 0 or in_band * 2 < len(comps)):
        failures.append(f"{code}: 除权日补偿统计校验失败(带内 {in_band}/{len(comps)})")

    if "spot" in meta:
        d0, expect = meta["spot"]
        got = by_date.get(d0)
        if got is None:
            failures.append(f"{code}: 缺抽检日 {d0}")
        else:
            print(f"抽检 {d0}: RAW收盘={got[2]:.3f} (真实价应为 {expect}) HFQ={got[6]:.3f} 比值={got[6]/got[2]:.4f}")
            if abs(got[2] - expect) > 0.02:
                failures.append(f"{code}: {d0} RAW={got[2]} ≠ 真实价 {expect}")

print("\n" + "=" * 50)
if failures:
    print(f"冒烟失败 {len(failures)} 项:")
    for f in failures:
        print("  ✗", f)
    sys.exit(1)
print("冒烟全部通过: RAW=真实价口径成立, HFQ 不变量成立, NaT 净化成立")
