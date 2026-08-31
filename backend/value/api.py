"""价值回归 API — /api/value/*。实盘视图=对缓存数据跑确定性回测取末日状态,
保证页面与回测口径一致。"""

from __future__ import annotations

import bisect
import json
import os
import pickle
import threading
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query

from api.data import _prev_fire_time
from base.config import DB_PATH
from base.scheduler.job_manager import job_manager, run_job
from base.scheduler.job_registry import JOB_DEFS, JOB_FNS
from base.scheduler.scheduled_defs import VALUE_SCHEDULED_DEFS
from base.scheduler.tasks import scheduler
from value import cache, store
from value.backtest import run_backtest
from value.config import DY_LOOKBACK_DAYS, IDLE_YIELD_PCT, MA_WINDOW, MAX_SLOTS
from value.engine import bias_series, buy_signals, cooldown_active, sell_signals, z_series

router = APIRouter(prefix="/api/value", tags=["value"])

_CACHE: dict = {"ts": 0.0, "data": None}
# 数据仅随抓取/重建任务变化(任务完成会主动失效), 故 TTL 可拉长到 1 小时, 避免反复重算 ~30s 回测
_CACHE_TTL = 3600
_COMPUTING: dict = {"lock": threading.Lock(), "cond": None}  # single-flight: 冷启动只算一次, 并发请求等结果
_INPUTS_LOCK = threading.Lock()  # 冷启动只加载一次, 避免并发各自重读 200MB 库
STATE_FILE = DB_PATH.parent / "value_state.json"  # 回测状态落盘: 重启后磁盘预热秒开
INPUTS_FILE = DB_PATH.parent / "value_inputs.pkl"  # 底层数据集落盘: 避免每次重启重读 200MB 库(~40s)


def _inputs() -> dict:
    now = time.time()
    if _CACHE["data"] is not None and now - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["data"]
    with _INPUTS_LOCK:
        now = time.time()
        if _CACHE["data"] is not None and now - _CACHE["ts"] < _CACHE_TTL:
            return _CACHE["data"]
        data = _load_inputs_disk()
        if data is None:
            data = _inputs_from_db()
            _save_inputs_disk(data)
        _CACHE.update(ts=time.time(), data=data)
        return data


def _inputs_from_db() -> dict:
    counts = store.kline_row_counts()
    codes = sorted(c for c in counts if counts[c] > 0)
    kline = {c: store.get_kline(c) for c in codes}
    dates = sorted({d for kl in kline.values() for d in kl})
    snapshots = {}
    pe = {}
    for c in codes:
        snap = store.get_snapshot(c)
        if snap:
            snapshots[c] = snap
        pe[c] = store.get_pe(c)
    return {"codes": codes, "kline": kline, "dates": dates, "snapshots": snapshots, "pe": pe}


def _save_inputs_disk(data: dict) -> None:
    try:
        with open(INPUTS_FILE, "wb") as f:
            pickle.dump({"fp": _fingerprint(), "data": data}, f, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as e:
        print(f"[value] persist inputs failed: {type(e).__name__} {e}")


def _load_inputs_disk() -> dict | None:
    try:
        with open(INPUTS_FILE, "rb") as f:
            d = pickle.load(f)
        return d["data"] if d.get("fp") == _fingerprint() else None
    except Exception:
        return None


def _compute_state(start: str, idle_yield: float) -> dict:
    inp = _inputs()
    dates = [d for d in inp["dates"] if d >= start]
    if len(dates) < 2:
        return {"ready": False, "metrics": None, "history": [], "trade_log": [], "final": None, "signals": None}
    res = run_backtest(dates, inp["kline"], inp["snapshots"], inp["pe"], idle_yield)
    last, prev = dates[-1], dates[-2]
    idx = len(dates) - 1
    final = res["final"]
    pool_codes = {p["code"] for p in final["pool"]}
    quotes = {}
    for c in pool_codes | set(final["positions"]):
        kl = inp["kline"].get(c, {})
        if last not in kl or prev not in kl:
            continue
        quotes[c] = {
            "code": c,
            "close": kl[last]["close"],
            "prev_close": kl[prev]["close"],
            "bias": None,
            "z": None,
            "pe_ttm": inp["pe"].get(c, {}).get(last),
            "industry": inp["snapshots"].get(c, {}).get("industry", ""),
        }
    # 末日 bias/z 从回测内部不可得 → 用引擎序列重算(停牌日前收盘填充, 与回测口径一致)
    for c in quotes:
        kl = inp["kline"][c]
        cs: list[float | None] = []
        last_close: float | None = None
        for d in dates:
            row = kl.get(d)
            if row is not None:
                last_close = row["close"]
            cs.append(last_close)
        b = bias_series(cs)
        z = z_series(b)
        quotes[c]["bias"] = b[-1]
        quotes[c]["z"] = z[-1]
    sells = sell_signals(final["positions"], quotes)
    free = MAX_SLOTS - len(final["positions"]) + len(sells)
    cd = {c for c in final["cooldown_until"] if cooldown_active(final["cooldown_until"], c, idx)}
    cands = [q for c, q in quotes.items() if c in pool_codes and c not in final["positions"]]
    buys = buy_signals(cands, free, cd)
    return {
        "ready": True,
        "last_date": last,
        "metrics": res["metrics"],
        "history": res["history"],
        "trade_log": res["trade_log"],
        "pool_rebuilds": res["pool_rebuilds"],
        "final": final,
        "signals": {"sells": sells, "buys": buys},
    }


_STATE_CACHE: dict = {"ts": 0.0, "key": None, "data": None}


def _fingerprint() -> str:
    """数据指纹: 最新K线日+样本数+池重建日, 变化则落盘状态作废。"""
    latest = store.latest_kline_dates()
    pool_date, _ = store.latest_pool()
    return f"{max(latest.values()) if latest else ''}|{len(latest)}|{pool_date or ''}"


def _persist_state(key: tuple, st: dict) -> None:
    if not st.get("ready"):
        return
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"fp": _fingerprint(), "key": list(key), "state": st}, f, ensure_ascii=False)
    except Exception as e:
        print(f"[value] persist state failed: {type(e).__name__} {e}")


def _load_persisted() -> tuple[tuple, dict] | None:
    """启动时从磁盘恢复回测状态(指纹匹配才用), 避免重启后首访等 ~30s。"""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("fp") != _fingerprint() or not d.get("state", {}).get("ready"):
            return None
        return tuple(d["key"]), d["state"]
    except Exception:
        return None


def _invalidate() -> None:
    """数据任务完成后调用: 清空各级缓存并后台重算, 下个请求直接命中。"""
    _CACHE.update(ts=0.0, data=None)
    _STATE_CACHE.update(ts=0.0, key=None, data=None)
    _POOL_CACHE.update(ts=0.0, data=None)
    _STOCK_CACHE.update(ts=0.0, fp=None, data={})
    if not _BACKGROUND_DISABLED:
        threading.Thread(target=lambda: _live_state(), daemon=True).start()


_BACKGROUND_DISABLED = os.environ.get("VALUE_NO_PREWARM") == "1"  # 测试环境禁用后台预热/重算
cache.register(_invalidate)


def _prewarm() -> None:
    """启动预热: 状态已随导入时同步恢复(见模块底部), 此处仅后台预载/重算。"""
    try:
        if _STATE_CACHE["data"] is None:
            _live_state()  # 磁盘无状态→后台全量重算(首访会等, 仅发生在数据变更后首次启动)
        else:
            _inputs()  # 状态已恢复→预载底层数据(池/个股接口首访不阻塞)
        value_pool()  # 预热股票池卡片, 首访不阻塞 ~10s 卡片计算
    except Exception as e:
        print(f"[value] prewarm failed: {type(e).__name__} {e}")


# 导入时同步恢复落盘状态/数据(秒级): uvicorn 绑端口前完成, 首访零等待; 失败则退回后台预热
def _restore_from_disk() -> None:
    try:
        hit = _load_persisted()
        if hit is not None:
            key, st = hit
            _STATE_CACHE.update(ts=time.time(), key=key, data=st)
        data = _load_inputs_disk()
        if data is not None:
            _CACHE.update(ts=time.time(), data=data)
            print("[value] state+inputs restored from disk")
    except Exception as e:
        print(f"[value] restore failed: {type(e).__name__} {e}")


if not _BACKGROUND_DISABLED:
    _restore_from_disk()


def _live_state(start: str = "2018-01-01", idle_yield: float = IDLE_YIELD_PCT) -> dict:
    """回测结果级缓存 + single-flight: 冷启动 ~25s 的重回测不会被并发请求重复执行,
    也不会堆满线程池拖死事件循环。"""
    key = (start, idle_yield)
    now = time.time()
    if _STATE_CACHE["data"] is not None and _STATE_CACHE["key"] == key and now - _STATE_CACHE["ts"] < _CACHE_TTL:
        return _STATE_CACHE["data"]
    with _COMPUTING["lock"]:
        if _COMPUTING["cond"] is None:
            _COMPUTING["cond"] = threading.Condition()
            first = True
        else:
            first = False
        cond = _COMPUTING["cond"]
    if first:
        try:
            st = _compute_state(start, idle_yield)
            _STATE_CACHE.update(ts=time.time(), key=key, data=st)
            _persist_state(key, st)
        finally:
            with cond:
                _COMPUTING["cond"] = None
                cond.notify_all()
    else:
        with cond:
            while _COMPUTING["cond"] is cond:
                cond.wait()
    if _STATE_CACHE["data"] is not None and _STATE_CACHE["key"] == key:
        return _STATE_CACHE["data"]
    return _compute_state(start, idle_yield)  # 计算失败或参数不同的兜底直算


@router.get("/overview")
def value_overview(idle_yield: float = Query(IDLE_YIELD_PCT)):
    st = _live_state(idle_yield=idle_yield)
    if not st["ready"]:
        return {"ready": False, "hint": "无数据: 请在数据管理页运行「价值回归-拉取个股数据」"}
    final = st["final"]
    return {
        "ready": True,
        "last_date": st["last_date"],
        "pool_size": len(final["pool"]),
        "pool": final["pool"],
        "positions": final["positions"],
        "slots_used": len(final["positions"]),
        "max_slots": MAX_SLOTS,
        "cooldown": [
            {"code": c, "until_idx": u}
            for c, u in sorted(final["cooldown_until"].items(), key=lambda kv: -kv[1])
        ],
        "signals": st["signals"],
        "metrics": st["metrics"],
        "last_rebuild": st["pool_rebuilds"][-1] if st["pool_rebuilds"] else None,
    }


@router.get("/backtest")
def value_backtest(
    start: str = Query("2018-01-01"),
    end: str | None = Query(None, description="回测截止日(含), 缺省=全部数据"),
    idle_yield: float = Query(IDLE_YIELD_PCT),
):
    """指标基于全量回测(口径稳定), history/trades/重建记录按 [start, end] 窗口返回。"""
    st = _live_state(start=start, idle_yield=idle_yield)
    if not st["ready"]:
        raise HTTPException(status_code=404, detail="无可用数据, 先运行 value_fetch_data")
    history = st["history"]
    trades = st["trade_log"]
    rebuilds = st["pool_rebuilds"]
    if end:
        history = [h for h in history if h["date"] <= end]
        trades = [t for t in trades if t["exec_date"] <= end]
        rebuilds = [r for r in rebuilds if r["date"] <= end]
    return {
        "start": start,
        "end": end,
        "idle_yield_pct": idle_yield,
        "metrics": st["metrics"],
        "history": history,
        "trades": trades,
        "pool_rebuilds": rebuilds,
    }


def _carry_closes(kl: dict[str, dict], dates: list[str]) -> list[float | None]:
    """按全局交易日对齐的收盘序列(停牌前收盘填充, 上市前 None)。"""
    cs: list[float | None] = []
    last: float | None = None
    for d in dates:
        row = kl.get(d)
        if row is not None:
            last = row["close"]
        cs.append(last)
    return cs


def _pool_cards() -> list[dict]:
    """股票池卡片(对标 ETF 流向分析卡): 价格/涨跌/PE/股息率/市值/ROE/恐慌指标。"""
    inp = _inputs()
    dates = inp["dates"]
    if len(dates) < 2:
        return []
    last, prev = dates[-1], dates[-2]
    cutoff = (datetime.strptime(last, "%Y-%m-%d") - timedelta(days=DY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    rebuild_date, pool = store.latest_pool()
    st = _live_state()
    holding = set((st.get("final") or {}).get("positions", {}).keys()) if st.get("ready") else set()
    active_pool = {p["code"] for p in ((st.get("final") or {}).get("pool", []) if st.get("ready") else [])}
    cards = []
    for p in pool:
        c = p["code"]
        kl = inp["kline"].get(c, {})
        snap = inp["snapshots"].get(c, {})
        row_last, row_prev = kl.get(last), kl.get(prev)
        close = row_last["close"] if row_last else None
        prev_close = row_prev["close"] if row_prev else None
        chg = round((close / prev_close - 1) * 100, 2) if close and prev_close else None
        # 滚动一年股息率(已除权口径)
        dy = None
        if close:
            dy_sum = sum(
                d["dps"]
                for d in snap.get("dividends", [])
                if d.get("ex_date") and cutoff < d["ex_date"] <= last and (d.get("dps") or 0) > 0
            )
            dy = round(dy_sum / close * 100, 2)
        # 最新已披露年报 ROE(展示用, 不做前视约束)
        roes = [r for r in snap.get("roes", []) if r.get("roe") is not None]
        roe = roes[-1]["roe"] if roes else None
        shares = snap.get("total_shares_yi") or 0
        pe_map = inp["pe"].get(c, {})
        pe = pe_map.get(last)
        if pe is None:
            prior = [k for k in pe_map if k <= last]
            pe = pe_map[max(prior)] if prior else None
        cs = _carry_closes(kl, dates)
        bs = bias_series(cs)
        zs = z_series(bs)
        cards.append(
            {
                "code": c,
                "name": snap.get("name"),
                "industry": snap.get("industry"),
                "gates": p.get("gates", []),
                "price": close,
                "prev_close": prev_close,
                "change_pct": chg,
                "pe_ttm": round(pe, 2) if pe is not None else None,
                "dy_pct": dy,
                "mcap_yi": round(close * shares, 1) if close and shares else None,
                "roe_pct": roe,
                "bias": round(bs[-1], 3) if bs and bs[-1] is not None else None,
                "z": round(zs[-1], 2) if zs and zs[-1] is not None else None,
                "holding": c in holding,
                "in_active_pool": c in active_pool,
            }
        )
    return cards


_POOL_CACHE: dict = {"ts": 0.0, "data": None}


@router.get("/pool")
def value_pool():
    now = time.time()
    if _POOL_CACHE["data"] is not None and now - _POOL_CACHE["ts"] < _CACHE_TTL:
        return _POOL_CACHE["data"]
    rebuild_date, _pool = store.latest_pool()
    out = {
        "rebuild_date": rebuild_date,
        "cards": _pool_cards(),
        "hl_ready": not store.codes_missing_hl(),  # K线高低价升级是否完成(前端渲染模式提示)
    }
    _POOL_CACHE.update(ts=now, data=out)
    return out


@router.get("/kline")
def value_kline(code: str = Query(...)):
    """个股K线(含高低/MA120) + 该股在回测中的全部买卖点。"""
    inp = _inputs()
    kl = inp["kline"].get(code)
    if not kl:
        raise HTTPException(status_code=404, detail=f"无 {code} 的K线数据")
    dates = sorted(kl)
    cs = [kl[d]["close"] for d in dates]
    ma = _ma_series(cs, 120)
    st = _live_state()
    trades = [t for t in (st.get("trade_log") or []) if t["code"] == code] if st.get("ready") else []
    return {
        "code": code,
        "name": inp["snapshots"].get(code, {}).get("name"),
        "kline": [
            {
                "date": d,
                "open": kl[d]["open"],
                "close": kl[d]["close"],
                "high": kl[d].get("high"),
                "low": kl[d].get("low"),
            }
            for d in dates
        ],
        "ma120": ma,
        "trades": trades,
    }


def _ma_series(closes: list[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    s = 0.0
    for i, v in enumerate(closes):
        s += v
        if i >= window:
            s -= closes[i - window]
        out.append(s / window if i + 1 >= window else None)
    return out


@router.get("/scheduled")
def value_scheduled():
    """价值回归专属定时任务全景(仅本页展示, 不并入 ETF 定时任务页)。"""
    now = datetime.now().astimezone()
    jobs = {j.id: j for j in scheduler.get_jobs()}
    out = []
    for tid, defn in VALUE_SCHEDULED_DEFS.items():
        j = jobs.get(tid)
        nr = j.next_run_time if j else None
        pr = None
        if nr:
            interval = getattr(j.trigger, "interval", None)
            pr = nr - interval if interval is not None else _prev_fire_time(j.trigger, now)
        out.append(
            {
                "id": tid,
                **defn,
                "next_run": nr.isoformat() if nr else None,
                "prev_run": pr.isoformat() if pr else None,
            }
        )
    return out


_STOCK_CACHE: dict = {"ts": 0.0, "fp": None, "data": {}}  # 个股独立回测结果缓存(按数据指纹失效)


@router.get("/stock_trades")
def value_stock_trades(code: str = Query(...)):
    """个股独立策略视角: 假设全仓只做这一只, 按策略信号规则跑完整历史,
    返回买卖点/往返胜率/累计盈亏(不受组合 5 槽位资金约束)。"""
    fp = _fingerprint()
    now = time.time()
    if _STOCK_CACHE["fp"] != fp or now - _STOCK_CACHE["ts"] >= _CACHE_TTL:
        _STOCK_CACHE.update(ts=now, fp=fp, data={})
    hit = _STOCK_CACHE["data"].get(code)
    if hit is not None:
        return hit
    inp = _inputs()
    kl = inp["kline"].get(code)
    snap = inp["snapshots"].get(code)
    if not kl or not snap:
        raise HTTPException(status_code=404, detail=f"无 {code} 的数据")
    dates = [d for d in inp["dates"] if d >= "2018-01-01"]
    res = run_backtest(
        dates, {code: kl}, {code: snap}, {code: inp["pe"].get(code, {})}, IDLE_YIELD_PCT, slot_divisor=1
    )
    sells = [t for t in res["trade_log"] if t["action"] == "SELL"]
    wins = sum(1 for t in sells if (t.get("pnl_pct") or 0) > 0)
    total = 1.0
    for t in sells:
        total *= 1 + (t.get("pnl_pct") or 0) / 100
    out = {
        "code": code,
        "name": snap.get("name"),
        "trades": res["trade_log"],
        "round_count": len(sells),
        "win_rate_pct": round(wins / len(sells) * 100, 1) if sells else 0.0,
        "total_return_pct": round((total - 1) * 100, 2) if sells else 0.0,
        "holding": code in res["final"]["positions"],
        "equity": round(res["history"][-1]["equity"], 4) if res["history"] else 1.0,
    }
    _STOCK_CACHE["data"][code] = out
    return out


@router.get("/indicators")
def value_indicators(code: str = Query(...)):
    """选股指标日线序列(与 /kline 同日期轴): PE(TTM)/滚动一年股息率%/距120日线比例(=bias)。"""
    inp = _inputs()
    kl = inp["kline"].get(code)
    if not kl:
        raise HTTPException(status_code=404, detail=f"无 {code} 的K线数据")
    dates = sorted(kl)
    snap = inp["snapshots"].get(code, {})
    pe_map = inp["pe"].get(code, {})
    # 滚动一年股息率: 对每个除权日累计其后 365 天内的贡献, 查询日二分定位, O((n+m)log)
    divs = [d for d in snap.get("dividends", []) if d.get("ex_date") and (d.get("dps") or 0) > 0]
    ex_list = sorted(divs, key=lambda d: d["ex_date"])
    ex_dates = [d["ex_date"] for d in ex_list]
    closes = [kl[d]["close"] for d in dates]
    ma = _ma_series(closes, MA_WINDOW)
    bias: list[float | None] = [round(c / m, 4) if m else None for c, m in zip(closes, ma)]
    dy: list[float | None] = []
    pe: list[float | None] = []
    for i, d in enumerate(dates):
        cutoff = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=DY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        lo = bisect.bisect_right(ex_dates, cutoff)
        hi = bisect.bisect_right(ex_dates, d)
        s = sum(ex_list[j]["dps"] for j in range(lo, hi))
        dy.append(round(s / closes[i] * 100, 3) if closes[i] else None)
        v = pe_map.get(d)
        pe.append(round(v, 2) if v is not None else None)
    return {"code": code, "dates": dates, "pe": pe, "dy": dy, "bias": bias}


@router.post("/refresh")
def value_refresh():
    """触发个股数据抓取任务(后台异步, 断点续传)。"""
    task = "value_fetch_data"
    if not job_manager.can_start(task, JOB_DEFS[task]["exclusive"]):
        raise HTTPException(status_code=409, detail="任务正在运行,请稍后再试")
    import asyncio
    from functools import partial

    job_id = job_manager.submit(task, {}, JOB_DEFS[task]["exclusive"])
    asyncio.create_task(run_job(job_id, partial(JOB_FNS[task])))
    _invalidate()
    return {"job_id": job_id}


# 预热线程必须在所有函数定义之后启动(其内部引用 value_pool 等后定义符号)
if not _BACKGROUND_DISABLED:
    threading.Thread(target=_prewarm, daemon=True).start()
