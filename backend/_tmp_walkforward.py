"""临时 Walk-Forward 验证(_tmp 前缀, 不改动系统):
训练期 2018-2022 / 盲测期 2023-2026(盲测指标用全期回测的 2023 起切片, 净值按切片起点归一,
等价于"带着训练期末持仓继续实盘"的延续口径)。
候选: v1=现行(0.88/固定1.10/60)  v2=A动态止损+移动止盈+40  alt=固定0.92+固定1.08+40
"""
import bisect
import sys
import time

sys.path.insert(0, ".")

from value import store
from value.backtest import _metrics
from value.config import BIAS_EXIT, IDLE_YIELD_PCT, MAX_SLOTS, TRADING_DAYS_PER_YEAR
from value.engine import bias_series, build_pool, buy_signals, cooldown_active, is_pool_rebuild_day

t0 = time.time()
counts = store.kline_row_counts()
codes = sorted(c for c in counts if counts[c] > 0)
kline = {c: store.get_kline(c) for c in codes}
dates = [d for d in sorted({d for kl in kline.values() for d in kl}) if d >= "2018-01-01"]
snapshots, pe = {}, {}
for c in codes:
    s = store.get_snapshot(c)
    if s:
        snapshots[c] = s
    pe[c] = store.get_pe(c)
print(f"[inputs] {time.time() - t0:.0f}s")

bias_by_code, act_by_code = {}, {}
for c, kl in kline.items():
    cs, last, rng = [], None, []
    for d in dates:
        row = kl.get(d)
        if row is not None:
            last = row["close"]
            rng.append((row["high"] - row["low"]) / row["close"] if row["close"] else None)
        else:
            rng.append(None)
        cs.append(last)
    bias_by_code[c] = {d: b for d, b in zip(dates, bias_series(cs)) if b is not None}
    act, win = {}, []
    for i, r in enumerate(rng):
        if i >= 60 and rng[i - 60] is not None:
            win.pop(bisect.bisect_left(win, rng[i - 60]))
        if r is not None:
            bisect.insort(win, r)
        if r is None or len(win) < 30:
            continue
        recent = [x for x in rng[max(0, i - 4) : i + 1] if x is not None]
        if recent and win[len(win) // 2] > 0:
            act[dates[i]] = (sum(recent) / len(recent)) / win[len(win) // 2]
    act_by_code[c] = act
print(f"[series] {time.time() - t0:.0f}s")

clip = lambda v, lo, hi: max(lo, min(hi, v))
TEST_START = "2023-01-01"


def run(variant: str) -> dict:
    if variant == "v1":
        stop_mode, s_base, widen, tighten, tp_mode, cd = "fixed", 0.88, 0, 0, "fixed110", 60
    elif variant == "v2":
        stop_mode, s_base, widen, tighten, tp_mode, cd = "act", 0.92, 0.04, 0.05, "trail", 40
    else:
        stop_mode, s_base, widen, tighten, tp_mode, cd = "fixed", 0.92, 0, 0, "fixed108", 40
    daily_factor = (1 + IDLE_YIELD_PCT / 100) ** (1 / TRADING_DAYS_PER_YEAR)
    cash, positions, cooldown_until, pending = 1.0, {}, {}, []
    history, trade_log, pool = [], [], []

    def snap_as_of(code, d):
        kl = kline[code]
        price = kl[d]["close"] if d in kl else None
        return {**snapshots[code], "price": price,
                "mcap_yi": price * (snapshots[code].get("total_shares_yi") or 0) if price else None,
                "pe_ttm": pe.get(code, {}).get(d)}

    for idx, d in enumerate(dates):
        if is_pool_rebuild_day(dates, idx):
            pool = build_pool([snap_as_of(c, d) for c in snapshots if d in kline[c]], d)
        due = [ev for ev in pending if ev["exec_date"] == d]
        pending = [ev for ev in pending if ev["exec_date"] != d]
        for ev in sorted(due, key=lambda e: 0 if e["action"] == "SELL" else 1):
            code = ev["code"]
            open_px = kline[code][d]["open"] if d in kline[code] else None
            if not open_px or open_px <= 0:
                continue
            if ev["action"] == "SELL":
                pos = positions.pop(code, None)
                if not pos:
                    continue
                cash += pos["shares"] * open_px
                if ev.get("is_stop"):
                    cooldown_until[code] = idx + cd
                trade_log.append({**ev, "price": open_px, "shares": pos["shares"], "amount": pos["shares"] * open_px})
            else:
                amount = min(ev["amount"], cash)
                if amount <= 0:
                    continue
                cash -= amount
                positions[code] = {"shares": amount / open_px, "cost": open_px, "buy_date": d,
                                   "entry_bias": ev.get("entry_bias"), "armed": False, "max_close": 0.0}
                trade_log.append({**ev, "price": open_px, "shares": amount / open_px, "amount": amount})
        cash *= daily_factor
        equity = cash + sum(p["shares"] * (kline[c][d]["close"] if d in kline[c] else p["cost"]) for c, p in positions.items())
        invested = equity - cash
        history.append({"date": d, "equity": equity,
                        "position_pct": invested / equity * 100 if equity > 0 else 0.0, "cash": cash})
        pool_codes = {pp["code"] for pp in pool}
        quotes = {}
        for c in pool_codes | set(positions):
            if d not in kline[c] or idx == 0 or dates[idx - 1] not in kline[c]:
                continue
            quotes[c] = {"code": c, "close": kline[c][d]["close"], "prev_close": kline[c][dates[idx - 1]]["close"],
                         "bias": bias_by_code[c].get(d), "act": act_by_code[c].get(d),
                         "pe_ttm": pe.get(c, {}).get(d), "industry": snapshots[c].get("industry", "")}
        sells = []
        for code, pos in positions.items():
            q = quotes.get(code)
            if not q:
                continue
            close, cost = q["close"], pos["cost"]
            rule, is_stop = None, False
            act = q.get("act") or 1.0
            if stop_mode == "fixed":
                hit = close <= cost * s_base
            else:
                s = s_base - widen * clip(1 - act, 0, 1) + tighten * clip(act - 1.5, 0, 1)
                hit = close <= cost * s
            if hit:
                rule, is_stop = 1, True
            if rule is None:
                if tp_mode == "fixed110":
                    if close >= cost * 1.10:
                        rule = 2
                elif tp_mode == "fixed108":
                    if close >= cost * 1.08:
                        rule = 2
                else:
                    if not pos["armed"] and close >= cost * 1.08:
                        pos["armed"], pos["max_close"] = True, close
                    if pos["armed"]:
                        pos["max_close"] = max(pos["max_close"], close)
                        if close <= pos["max_close"] * 0.96 or close >= cost * 1.30:
                            rule = 2
            if rule is None and q["bias"] is not None and q["bias"] >= BIAS_EXIT:
                rule = 3
            if rule:
                sells.append({"code": code, "rule": rule, "is_stop": is_stop, "reason": ""})
        for s in sells:
            pending.append({"date": d, "exec_date": dates[idx + 1] if idx + 1 < len(dates) else None, "code": s["code"],
                            "action": "SELL", "rule": s["rule"], "is_stop": s["is_stop"], "reason": "",
                            "pnl_pct": round((quotes[s["code"]]["close"] / positions[s["code"]]["cost"] - 1) * 100, 2)})
        selling = {s["code"] for s in sells}
        free = MAX_SLOTS - len(positions) + len(selling)
        cd_set = {c for c in cooldown_until if cooldown_active(cooldown_until, c, idx)}
        cands = [q for c, q in quotes.items() if c in pool_codes and c not in positions and c not in selling]
        for b in buy_signals(cands, free, cd_set):
            pending.append({"date": d, "exec_date": dates[idx + 1] if idx + 1 < len(dates) else None, "code": b["code"],
                            "action": "BUY", "kind": b["kind"], "reason": b["reason"], "entry_bias": b["bias"],
                            "amount": equity / MAX_SLOTS})
        pending = [ev for ev in pending if ev["exec_date"] is not None]
    return {"history": history, "trade_log": trade_log}


def seg_metrics(res, start=None):
    h = res["history"] if start is None else [x for x in res["history"] if x["date"] >= start]
    if start is not None and h:
        base = h[0]["equity"]
        h = [{"date": x["date"], "equity": x["equity"] / base, "position_pct": x.get("position_pct", 0), "cash": 0} for x in h]
    tr = res["trade_log"] if start is None else [t for t in res["trade_log"] if t["exec_date"] >= start]
    return _metrics(h, tr, [x["date"] for x in h])


def show(tag, m):
    cal = round(m["annual_return_pct"] / abs(m["max_drawdown_pct"]), 2) if m["max_drawdown_pct"] < 0 else 0
    print(f"{tag:<8} 年化{m['annual_return_pct']:>6.2f}% 夏普{m['sharpe']:>5.2f} 回撤{m['max_drawdown_pct']:>7.2f}% "
          f"胜率{m['win_rate_pct']:>5.1f}% 卡玛{cal:>5.2f} 往返{m['round_count']:>4}")


for v in ("v1", "v2", "alt"):
    res = run(v)
    print(f"== {v} ==")
    show("全期", seg_metrics(res, None))
    # 分段
    train_h = [x for x in res["history"] if x["date"] < TEST_START]
    base_t = train_h[-1]["equity"] if train_h else 1
    show("训练期", _metrics(train_h,
                            [t for t in res["trade_log"] if t["exec_date"] < TEST_START],
                            [x["date"] for x in train_h]))
    show("盲测期", seg_metrics(res, TEST_START))
print(f"[total] {time.time() - t0:.0f}s")
