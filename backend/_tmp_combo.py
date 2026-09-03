"""临时组合实验(_tmp 前缀, 不改动系统): 动态止损 × 止盈方式 × 冷却 的组合扫描。

止盈方式: fixed(tp) / trail(到+8%激活, 自最高点回落4%卖, 上限+30%)
止损方式: fixed(s) / act(base,widen,tighten) / bias(dd,floor)
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


def run(stop: dict, tp_mode: str, tp: float, cd: int) -> dict:
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
        history.append({"date": d, "equity": round(equity, 6),
                        "position_pct": round(invested / equity * 100, 1) if equity > 0 else 0.0, "cash": round(cash, 6)})
        pool_codes = {pp["code"] for pp in pool}
        quotes = {}
        for c in pool_codes | set(positions):
            if d not in kline[c] or idx == 0 or dates[idx - 1] not in kline[c]:
                continue
            quotes[c] = {"code": c, "close": kline[c][d]["close"], "prev_close": kline[c][dates[idx - 1]]["close"],
                         "bias": bias_by_code[c].get(d), "z": None, "pe_ttm": pe.get(c, {}).get(d),
                         "industry": snapshots[c].get("industry", "")}
        sells = []
        for code, pos in positions.items():
            q = quotes.get(code)
            if not q:
                continue
            close, cost = q["close"], pos["cost"]
            rule, is_stop = None, False
            # ① 止损
            act = act_by_code[code].get(d, 1.0)
            if stop["mode"] == "fixed":
                hit = close <= cost * stop["s"]
            elif stop["mode"] == "act":
                s = stop["base"] - stop["widen"] * clip(1 - act, 0, 1) + stop["tighten"] * clip(act - 1.5, 0, 1)
                hit = close <= cost * s
            else:  # bias
                eb = pos.get("entry_bias")
                hit = close <= cost * stop["floor"] or (eb is not None and q["bias"] is not None and q["bias"] <= eb - stop["dd"])
            if hit:
                rule, is_stop = 1, True
            # ② 止盈(固定 / 移动)
            if rule is None:
                if tp_mode == "fixed":
                    if close >= cost * tp:
                        rule = 2
                else:
                    if not pos["armed"] and close >= cost * 1.08:
                        pos["armed"], pos["max_close"] = True, close
                    if pos["armed"]:
                        pos["max_close"] = max(pos["max_close"], close)
                        if close <= pos["max_close"] * 0.96 or close >= cost * 1.30:
                            rule = 2
            # ③ bias 离场
            if rule is None and q["bias"] is not None and q["bias"] >= BIAS_EXIT:
                rule = 3
            if rule:
                sells.append({"code": code, "rule": rule, "is_stop": is_stop, "reason": ""})
        for s in sells:
            pending.append({"date": d, "exec_date": dates[idx + 1] if idx + 1 < len(dates) else None, "code": s["code"],
                            "action": "SELL", "rule": s["rule"], "is_stop": s["is_stop"], "reason": s["reason"],
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
    return _metrics(history, trade_log, dates)


def show(tag, m):
    cal = round(m["annual_return_pct"] / abs(m["max_drawdown_pct"]), 2) if m["max_drawdown_pct"] < 0 else 0
    print(f"{tag:<30} 累计{m['total_return_pct']:>8.1f}% 年化{m['annual_return_pct']:>6.2f}% 夏普{m['sharpe']:>5.2f} "
          f"回撤{m['max_drawdown_pct']:>7.2f}% 胜率{m['win_rate_pct']:>5.1f}% 往返{m['round_count']:>4} 卡玛{cal:>5.2f}")


A = {"mode": "act", "base": 0.92, "widen": 0.04, "tighten": 0.05}
show("基线 fixed0.88/tp1.10/cd60", run({"mode": "fixed", "s": 0.88}, "fixed", 1.10, 60))
show("A动态/tp1.10/cd60(参照)", run(A, "fixed", 1.10, 60))
print("--- 组合扫描 ---")
show("A + tp1.08 + cd60", run(A, "fixed", 1.08, 60))
show("A + tp1.08 + cd40", run(A, "fixed", 1.08, 40))
show("A + tp1.08 + cd30", run(A, "fixed", 1.08, 30))
show("A + trail + cd60", run(A, "trail", 0, 60))
show("A + trail + cd40", run(A, "trail", 0, 40))
show("A + trail + cd30", run(A, "trail", 0, 30))
show("fixed0.94 + tp1.08 + cd60", run({"mode": "fixed", "s": 0.94}, "fixed", 1.08, 60))
show("fixed0.94 + tp1.08 + cd30", run({"mode": "fixed", "s": 0.94}, "fixed", 1.08, 30))
show("fixed0.92 + tp1.08 + cd40", run({"mode": "fixed", "s": 0.92}, "fixed", 1.08, 40))
show("B(dd.10/f.80) + tp1.08 + cd60", run({"mode": "bias", "dd": 0.10, "floor": 0.80}, "fixed", 1.08, 60))
show("B(dd.12/f.80) + tp1.08 + cd40", run({"mode": "bias", "dd": 0.12, "floor": 0.80}, "fixed", 1.08, 40))
print(f"[total] {time.time() - t0:.0f}s")
