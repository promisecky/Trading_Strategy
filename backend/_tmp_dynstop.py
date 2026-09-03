"""临时动态止损实验(_tmp 前缀, 独立回测循环, 不改动系统代码)。

幅度水位 = 近5日均(高-低)/收 ÷ 近60日中位数(帕金森幅度作成交量代理)。
A 幅度自适应: stop = base - widen*quiet + tighten*heavy
B 论点止损:   bias ≤ 入场bias - dd 或 硬底 floor
C 混合:       (bias恶化 且 放量) 或 硬底
"""
import statistics
import sys
import time

sys.path.insert(0, ".")

from value import store
from value.backtest import _metrics
from value.config import (
    BIAS_EXIT,
    COOLDOWN_DAYS,
    IDLE_YIELD_PCT,
    MAX_SLOTS,
    POOL_MONTHS,
    TAKE_PROFIT,
    TRADING_DAYS_PER_YEAR,
)
from value.engine import bias_series, build_pool, buy_signals, cooldown_active, is_pool_rebuild_day, sell_signals

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
print(f"[inputs] {time.time() - t0:.0f}s codes={len(codes)} days={len(dates)}")

# ---- 预计算: 前复权收盘(停牌填充) / bias / 幅度水位 act ----
import bisect

bias_by_code: dict[str, dict[str, float]] = {}
act_by_code: dict[str, dict[str, float]] = {}
for c, kl in kline.items():
    cs: list[float | None] = []
    last = None
    rng: list[float | None] = []
    for d in dates:
        row = kl.get(d)
        if row is not None:
            last = row["close"]
            rng.append((row["high"] - row["low"]) / row["close"] if row["close"] else None)
        else:
            rng.append(None)
        cs.append(last)
    bs = bias_series(cs)
    bias_by_code[c] = {d: b for d, b in zip(dates, bs) if b is not None}
    # 幅度水位: 近5日均 / 近60日中位数(滑动有序窗口)
    act: dict[str, float] = {}
    win: list[float] = []
    for i, r in enumerate(rng):
        if i >= 60:
            old = rng[i - 60]
            if old is not None:
                win.pop(bisect.bisect_left(win, old))
        if r is not None:
            bisect.insort(win, r)
        if r is None or len(win) < 30:
            continue
        recent = [x for x in rng[max(0, i - 4) : i + 1] if x is not None]
        if not recent:
            continue
        med = win[len(win) // 2]
        if med > 0:
            act[dates[i]] = (sum(recent) / len(recent)) / med
    act_by_code[c] = act
print(f"[series] {time.time() - t0:.0f}s")

clip = lambda v, lo, hi: max(lo, min(hi, v))


def run_backtest_dyn(stop_mode: str, p: dict) -> dict:
    """复制系统回测循环, 仅卖出规则①替换为动态止损; ②止盈/③bias离场/冷却/槽位与系统一致。"""
    daily_factor = (1 + IDLE_YIELD_PCT / 100) ** (1 / TRADING_DAYS_PER_YEAR)
    cash = 1.0
    positions: dict[str, dict] = {}
    cooldown_until: dict[str, int] = {}
    pending: list[dict] = []
    history: list[dict] = []
    trade_log: list[dict] = []
    pool: list[dict] = []

    def snap_as_of(code, d):
        kl = kline[code]
        price = kl[d]["close"] if d in kl else None
        shares_yi = snapshots[code].get("total_shares_yi") or 0
        return {**snapshots[code], "price": price, "mcap_yi": price * shares_yi if price else None,
                "pe_ttm": pe.get(code, {}).get(d)}

    for idx, d in enumerate(dates):
        if is_pool_rebuild_day(dates, idx):
            pool = build_pool([snap_as_of(c, d) for c in snapshots if d in kline[c]], d)
        due = [ev for ev in pending if ev["exec_date"] == d]
        pending = [ev for ev in pending if ev["exec_date"] != d]
        for ev in sorted(due, key=lambda e: 0 if e["action"] == "SELL" else 1):
            code = ev["code"]
            open_px = kline[code][d]["open"] if d in kline[code] else None
            if open_px is None or open_px <= 0:
                continue
            if ev["action"] == "SELL":
                pos = positions.pop(code, None)
                if not pos:
                    continue
                cash += pos["shares"] * open_px
                if ev.get("is_stop"):
                    cooldown_until[code] = idx + COOLDOWN_DAYS
                trade_log.append({**ev, "price": open_px, "shares": pos["shares"], "amount": pos["shares"] * open_px})
            else:
                amount = min(ev["amount"], cash)
                if amount <= 0:
                    continue
                shares = amount / open_px
                cash -= amount
                positions[code] = {"shares": shares, "cost": open_px, "buy_date": d,
                                   "entry_bias": ev.get("entry_bias")}
                trade_log.append({**ev, "price": open_px, "shares": shares, "amount": amount})
        cash *= daily_factor
        equity = cash
        for c, pos in positions.items():
            px = kline[c][d]["close"] if d in kline[c] else pos["cost"]
            equity += pos["shares"] * px
        invested = equity - cash
        history.append({"date": d, "equity": round(equity, 6),
                        "position_pct": round(invested / equity * 100, 1) if equity > 0 else 0.0,
                        "cash": round(cash, 6)})
        pool_codes = {pp["code"] for pp in pool}
        quotes: dict[str, dict] = {}
        for c in pool_codes | set(positions):
            if d not in kline[c] or idx == 0 or dates[idx - 1] not in kline[c]:
                continue
            quotes[c] = {"code": c, "close": kline[c][d]["close"],
                         "prev_close": kline[c][dates[idx - 1]]["close"],
                         "bias": bias_by_code[c].get(d), "z": None,
                         "pe_ttm": pe.get(c, {}).get(d),
                         "industry": snapshots[c].get("industry", "")}
        # ---- 卖出: ②③用系统规则, ①动态 ----
        sells = []
        for code, pos in positions.items():
            q = quotes.get(code)
            if not q:
                continue
            close, cost = q["close"], pos["cost"]
            rule, is_stop = None, False
            if close >= cost * TAKE_PROFIT:
                rule = 2
            elif q["bias"] is not None and q["bias"] >= BIAS_EXIT:
                rule = 3
            else:
                act = act_by_code[code].get(d, 1.0)
                if stop_mode == "fixed":
                    hit = close <= cost * p["s"]
                elif stop_mode == "act":
                    quiet = clip(1 - act, 0, 1)
                    heavy = clip(act - 1.5, 0, 1)
                    s = p["base"] - p["widen"] * quiet + p["tighten"] * heavy
                    hit = close <= cost * s
                elif stop_mode == "bias":
                    eb = pos.get("entry_bias")
                    hit = (eb is not None and q["bias"] is not None and q["bias"] <= eb - p["dd"]) or close <= cost * p["floor"]
                elif stop_mode == "hybrid":
                    eb = pos.get("entry_bias")
                    hit = close <= cost * p["floor"] or (
                        eb is not None and q["bias"] is not None and q["bias"] <= eb - p["dd"] and act > p["act_q"]
                    )
                else:
                    hit = False
                if hit:
                    rule, is_stop = 1, True
            if rule:
                sells.append({"code": code, "rule": rule, "is_stop": is_stop,
                              "reason": f"dyn({stop_mode})" if rule == 1 else ""})
        for s in sells:
            pending.append({"date": d, "exec_date": dates[idx + 1] if idx + 1 < len(dates) else None,
                            "code": s["code"], "action": "SELL", "rule": s["rule"],
                            "is_stop": s["is_stop"], "reason": s["reason"],
                            "pnl_pct": round((quotes[s["code"]]["close"] / positions[s["code"]]["cost"] - 1) * 100, 2)})
        selling = {s["code"] for s in sells}
        free_slots = MAX_SLOTS - len(positions) + len(selling)
        cd_set = {c for c in cooldown_until if cooldown_active(cooldown_until, c, idx)}
        cands = [q for c, q in quotes.items() if c in pool_codes and c not in positions and c not in selling]
        for b in buy_signals(cands, free_slots, cd_set):
            pending.append({"date": d, "exec_date": dates[idx + 1] if idx + 1 < len(dates) else None,
                            "code": b["code"], "action": "BUY", "kind": b["kind"], "reason": b["reason"],
                            "entry_bias": b["bias"], "amount": equity / MAX_SLOTS})
        pending = [ev for ev in pending if ev["exec_date"] is not None]
    return _metrics(history, trade_log, dates)


def show(tag, m):
    print(f"{tag:<26} 累计{m['total_return_pct']:>8.1f}% 年化{m['annual_return_pct']:>6.2f}% 夏普{m['sharpe']:>5.2f} "
          f"回撤{m['max_drawdown_pct']:>7.2f}% 胜率{m['win_rate_pct']:>5.1f}% 往返{m['round_count']:>4} 利用率{m['utilization_pct']:>5.1f}%")


show("基线 固定0.88", run_backtest_dyn("fixed", {"s": 0.88}))
show("参照 固定0.94", run_backtest_dyn("fixed", {"s": 0.94}))
print("--- A 幅度自适应 ---")
for base in (0.88, 0.92):
    for widen in (0.04, 0.08):
        for tighten in (0.0, 0.05):
            show(f"A b{base:.2f} w{widen:.2f} t{tighten:.2f}",
                 run_backtest_dyn("act", {"base": base, "widen": widen, "tighten": tighten}))
print("--- B 论点止损(bias恶化+硬底) ---")
for dd in (0.08, 0.10, 0.12):
    for floor in (0.80, 0.85):
        show(f"B dd{dd:.2f} floor{floor:.2f}", run_backtest_dyn("bias", {"dd": dd, "floor": floor}))
print("--- C 混合(放量确认论点止损+硬底) ---")
for dd in (0.08, 0.10):
    for act_q in (1.2, 1.5):
        show(f"C dd{dd:.2f} q{act_q:.1f} f0.85", run_backtest_dyn("hybrid", {"dd": dd, "act_q": act_q, "floor": 0.85}))
print(f"[total] {time.time() - t0:.0f}s")
