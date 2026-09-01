"""价值回归数据抓取层 — 直连可用 HTTPS 源, 限流/重试/断点续传(已缓存跳过)。

本机出口封锁 HTTP 明文与百度/乐咕, 实测可达源(2026-08):
- 宇宙预筛: push2delay.eastmoney.com clist(延迟行情, 收盘确认策略无影响)
- 行业/总股本: push2delay stock/get
- K线(前复权): 腾讯 ifzq.gtimg.cn(HTTPS, 单次≤~640根故按 850 自然日分块)
- PE-TTM 日线: datacenter.eastmoney.com RPT_VALUEANALYSIS_DET
- 分红: akshare stock_fhps_detail_em(datacenter-web, HTTPS 可达)
- ROE: akshare stock_financial_analysis_indicator(新浪, HTTPS 可达)

近似项(文档化):
- 宇宙预筛用当前快照(市值/行业), 历史季度池仍以 as-of 基本面门判定;
- ROE 披露日缺失时用法定截止日(年报 FY+1-04-30)。
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from value import cache, store
from value.config import KLINE_START_YEAR, MCAP_MIN_YI, POOL_MONTHS, REAL_ESTATE_KEYWORDS
from value.engine import build_pool

FETCH_SLEEP_SEC = 0.4
_EM_UT = "fa5fd1943c7b386f172d6893dbfba10b"


def _try(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # 远端限流/断连: 记录并返回 None, 不崩任务
        print(f"[value-fetch] {getattr(fn, '__name__', fn)} failed: {e}")
        return None


def _https_json(url: str, params: dict, retries: int = 3, timeout: int = 20):
    """HTTPS GET → JSON(urllib 直连, 退避重试); 最终失败返回 None(不崩任务)。
    不用 requests: 东财部分节点对 requests TLS 指纹直接断连, urllib 稳定可达。"""
    full = url + "?" + urllib.parse.urlencode(params)
    host = urllib.parse.urlparse(url).netloc
    for i in range(retries):
        try:
            req = urllib.request.Request(full, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            if i == retries - 1:
                print(f"[value-fetch] {host} failed: {type(e).__name__} {e}")
            time.sleep(1.0 + i)
    return None


def _secid(code: str) -> str:
    """东财 secid: 6/9 开头为沪市(1.), 其余深市/北交所(0.)。"""
    return ("1." if code.startswith(("6", "9")) else "0.") + code


def fetch_universe(progress) -> list[str]:
    """候选宇宙: 市值≥300亿 + PE>0 + 非地产(当前快照预筛, 分页 pz=100)。"""
    pre: list[tuple[str, str, float]] = []
    pn = 1
    while True:
        d = _https_json(
            "https://push2delay.eastmoney.com/api/qt/clist/get",
            {
                "pn": pn, "pz": 100, "po": 1, "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2, "invt": 2, "fid": "f12",
                "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048",
                "fields": "f12,f14,f20,f9",
            },
        )
        data = (d or {}).get("data") or {}
        diff = data.get("diff") or []
        if not diff:
            break
        for r in diff:
            code = str(r.get("f12", ""))
            if code.startswith(("4", "8", "9")):  # 北交所/老三板: 腾讯源不覆盖且不符主板白马定位
                continue
            mcap, pe = r.get("f20"), r.get("f9")
            if not isinstance(mcap, (int, float)) or not isinstance(pe, (int, float)):
                continue
            if mcap / 1e8 >= MCAP_MIN_YI and pe > 0:
                pre.append((code, str(r.get("f14", "")), mcap / 1e8))
        total = data.get("total") or 0
        if pn * 100 >= total:
            break
        pn += 1
        if pn % 10 == 0:
            progress(0, 0, f"预筛扫描 {pn * 100}/{total}, 命中 {len(pre)}")
        time.sleep(0.15)
    progress(0, len(pre), f"预筛 {len(pre)} 只, 逐只取行业/股本")

    codes: list[str] = []
    for i, (code, name, _mcap) in enumerate(pre):
        industry, shares_yi = _fetch_info(code)
        if any(k in industry for k in REAL_ESTATE_KEYWORDS):
            continue
        store.upsert_stock_info(code, name, industry, shares_yi)
        codes.append(code)
        if i % 25 == 0:
            progress(i, len(pre), f"宇宙预筛 {i}/{len(pre)}")
        time.sleep(0.15)
    progress(len(pre), len(pre), f"候选宇宙 {len(codes)} 只")
    return codes


def _fetch_info(code: str) -> tuple[str, float | None]:
    """行业 + 总股本(亿股); 失败返回 ('', None)。"""
    d = _https_json(
        "https://push2delay.eastmoney.com/api/qt/stock/get",
        {"secid": _secid(code), "invt": 2, "fltt": 2, "fields": "f57,f58,f84,f127", "ut": _EM_UT},
    )
    data = (d or {}).get("data") or {}
    industry = str(data.get("f127") or "")
    shares_yi: float | None = None
    try:
        shares_yi = float(data.get("f84") or 0) / 1e8
    except (TypeError, ValueError):
        pass
    return industry, shares_yi


def _fetch_dividends(code: str) -> list[dict]:
    import akshare as ak

    df = _try(ak.stock_fhps_detail_em, symbol=code)
    if df is None or df.empty:
        return []
    by_fy: dict[int, dict] = {}
    for _, r in df.iterrows():
        fy_raw = str(r.get("报告期", ""))[:4]
        ratio = r.get("现金分红-现金分红比例")  # 每 10 股派息
        ann, ex = r.get("预案公告日"), r.get("除权除息日")
        if not fy_raw.isdigit() or ratio is None:
            continue
        try:
            dps = float(ratio) / 10.0
        except (TypeError, ValueError):
            continue
        if not dps > 0:  # 过滤负值与 NaN(送转行现金比例为 NaN, 入库会变 NULL)
            continue
        ann_s = str(ann.date()) if hasattr(ann, "date") else ("" if str(ann) == "NaT" else str(ann or ""))
        ex_s = str(ex.date()) if hasattr(ex, "date") else ("" if str(ex) == "NaT" else str(ex or ""))
        fy = int(fy_raw)
        cur = by_fy.get(fy)
        if cur is None:
            by_fy[fy] = {"fiscal_year": fy, "dps": dps, "announce_date": ann_s, "ex_date": ex_s}
        else:
            cur["dps"] += dps  # 同财年多次分红(中报+年报)累加
    return sorted(by_fy.values(), key=lambda x: x["fiscal_year"])


def _fetch_roes(code: str) -> list[dict]:
    import akshare as ak

    df = _try(ak.stock_financial_analysis_indicator, symbol=code, start_year="2005")
    if df is None or df.empty:
        return []
    out = []
    for _, r in df.iterrows():
        d = str(r.get("日期", ""))
        if not d.endswith("12-31"):
            continue  # 仅年度 ROE
        roe = r.get("净资产收益率(%)")
        if roe is None:
            continue
        try:
            roe_v = float(roe)
        except (TypeError, ValueError):
            continue
        fy = int(d[:4])
        out.append({"fiscal_year": fy, "roe": roe_v, "disclose_date": f"{fy + 1}-04-30"})
    return sorted(out, key=lambda x: x["fiscal_year"])


def _fetch_pe(code: str) -> list[tuple[str, float]]:
    """PE-TTM 日线(东财估值明细, 分页降序全量)。"""
    out: list[tuple[str, float]] = []
    pn = 1
    while True:
        d = _https_json(
            "https://datacenter.eastmoney.com/securities/api/data/v1/get",
            {
                "reportName": "RPT_VALUEANALYSIS_DET",
                "columns": "TRADE_DATE,PE_TTM",
                "filter": f'(SECURITY_CODE="{code}")(TRADE_DATE>=\'{KLINE_START_YEAR}-01-01\')',
                "pageNumber": pn, "pageSize": 500,
                "sortColumns": "TRADE_DATE", "sortTypes": -1,
                "source": "SECURITIES", "client": "PC",
            },
        )
        result = (d or {}).get("result") or {}
        rows = result.get("data") or []
        for r in rows:
            pe = r.get("PE_TTM")
            if pe is None:
                continue
            try:
                out.append((str(r["TRADE_DATE"])[:10], float(pe)))
            except (TypeError, ValueError, KeyError):
                continue
        if not rows or pn >= (result.get("pages") or 0):
            break
        pn += 1
        time.sleep(FETCH_SLEEP_SEC)
    return out


def _fetch_kline(code: str) -> list[tuple[str, float, float, float, float]]:
    """前复权日K(腾讯 HTTPS 源, 按 850 自然日分块规避单次~640根上限), 返回 日/开/收/高/低。"""
    symbol = ("sh" if code.startswith(("6", "9")) else "sz") + code
    start = datetime(KLINE_START_YEAR, 1, 1)
    end = datetime.now()
    out: list[tuple[str, float, float, float, float]] = []
    cur = start
    while cur <= end:
        win_end = min(cur + timedelta(days=850), end)
        d = _tencent_kline(symbol, cur.strftime("%Y-%m-%d"), win_end.strftime("%Y-%m-%d"))
        if d is None:
            return []  # 任一块失败则整只放弃(避免残缺 K 线污染指标)
        node = d.get("data", {}).get(symbol, {})
        rows = node.get("qfqday") or node.get("day") or []
        ws, we = cur.strftime("%Y-%m-%d"), win_end.strftime("%Y-%m-%d")
        for r in rows:
            if len(r) < 5 or not r[0] or not (ws <= r[0] <= we):
                continue  # 部分端点会多返回窗口前的行, 按窗口过滤
            try:
                out.append((r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4])))
            except (TypeError, ValueError):
                continue
        cur = win_end + timedelta(days=1)
        time.sleep(0.3)  # 限流: 风控敏感, 不宜提速(曾触发 501 封锁)
    return out


# 腾讯行情多源: 主域被风控(501)时轮换镜像端点, 均为同一 qfq 口径
_TENCENT_HOSTS = (
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get",
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
)


def _tencent_kline(symbol: str, start: str, end: str):
    param = f"{symbol},day,{start},{end},650,qfq"
    last_err: Exception | None = None
    for base in _TENCENT_HOSTS:
        url = f"{base}?param={param}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    print(f"[value-fetch] tencent kline all hosts failed ({symbol}): {type(last_err).__name__} {last_err}")
    return None


def fetch_stock(code: str) -> dict:
    """逐只全量拉取并落库, 返回各表行数。"""
    kline = _fetch_kline(code)
    store.replace_kline(code, kline)
    divs = _fetch_dividends(code)
    store.replace_dividends(code, divs)
    roes = _fetch_roes(code)
    store.replace_roes(code, roes)
    pe = _fetch_pe(code)
    store.replace_pe(code, pe)
    return {"kline": len(kline), "dividends": len(divs), "roes": len(roes), "pe": len(pe)}


def job_value_fetch(progress) -> dict:
    """抓取候选宇宙 + 逐只基本面/K线。
    断点续传: 无数据或最新K线日落后于宇宙最新日才重拉(每日增量更新靠此)。"""
    info_codes = _existing_codes()
    if not info_codes:
        info_codes = fetch_universe(progress)
    from base.store.calendar_repo import get_trade_days

    days = get_trade_days(end=datetime.now().strftime("%Y-%m-%d"))
    target = days[-1] if days else (max(store.latest_kline_dates().values()) if store.latest_kline_dates() else "")
    latest = store.latest_kline_dates()
    todo = [c for c in info_codes if latest.get(c, "") < target]
    done = 0
    failed = 0
    for i, code in enumerate(todo):
        try:
            n = fetch_stock(code)
        except Exception as e:  # 单只异常不中断整个任务(断点续传下次重试)
            failed += 1
            print(f"[value-fetch] {code} 抓取异常: {type(e).__name__} {e}")
            continue
        done += 1
        progress(i + 1, len(todo), f"{code} K线{n['kline']} 分红{n['dividends']} ROE{n['roes']} PE{n['pe']}")
        time.sleep(FETCH_SLEEP_SEC)
    cache.invalidate_all()  # 数据已变: 失效回测/池缓存并后台重算, 前端下个请求即命中新数据
    return {"universe": len(info_codes), "fetched": done, "failed": failed, "skipped": len(info_codes) - len(todo)}


def job_value_upgrade_kline(progress) -> dict:
    """一次性升级: 为缺 high/low 的存量个股重拉全量 K 线(幂等, 可断点续传)。"""
    todo = store.codes_missing_hl()
    done = 0
    failed = 0
    for i, code in enumerate(todo):
        try:
            kl = _fetch_kline(code)
            if kl:
                store.replace_kline(code, kl)
                done += 1
                progress(i + 1, len(todo), f"{code} K线{len(kl)}根(含高低)")
            else:
                failed += 1
                progress(i + 1, len(todo), f"{code} 拉取失败, 下次重试")
        except Exception as e:
            failed += 1
            print(f"[value-fetch] {code} K线升级异常: {type(e).__name__} {e}")
        time.sleep(FETCH_SLEEP_SEC)
    cache.invalidate_all()
    return {"todo": len(todo), "upgraded": done, "failed": failed}


def task_value_daily_update() -> dict:
    """定时任务(收盘后): 个股数据每日轻量增量更新(仅最新K线窗+PE增量)。"""
    return job_value_daily_fetch(lambda c, t, m: print(f"[value-daily] {c}/{t} {m}"))


def _fetch_pe_since(code: str, since: str) -> list[tuple[str, float]]:
    """PE-TTM 增量: 仅拉取 since 之后的新行(东财估值明细, 按日降序)。"""
    d = _https_json(
        "https://datacenter.eastmoney.com/securities/api/data/v1/get",
        {
            "reportName": "RPT_VALUEANALYSIS_DET",
            "columns": "TRADE_DATE,PE_TTM",
            "filter": f'(SECURITY_CODE="{code}")(TRADE_DATE>\'{since}\')',
            "pageNumber": 1, "pageSize": 500,
            "sortColumns": "TRADE_DATE", "sortTypes": -1,
            "source": "SECURITIES", "client": "PC",
        },
    )
    out: list[tuple[str, float]] = []
    for r in ((d or {}).get("result") or {}).get("data") or []:
        pe = r.get("PE_TTM")
        if pe is None:
            continue
        try:
            out.append((str(r["TRADE_DATE"])[:10], float(pe)))
        except (TypeError, ValueError, KeyError):
            continue
    return out


def job_value_daily_fetch(progress) -> dict:
    """日更轻量路径: 每只仅拉最新K线窗(自该股最新日起) + PE增量首页,
    不拉分红/ROE(仅季报披露期变化, 由全量任务覆盖)。约 2 请求/只, 618 只≈10分钟。
    完成后重建池预览, 保证卡片指标与最新数据同步。"""
    latest = store.latest_kline_dates()
    universe_latest = max(latest.values()) if latest else ""
    info_codes = _existing_codes()
    if not info_codes:
        info_codes = fetch_universe(progress)
        latest = store.latest_kline_dates()
        universe_latest = max(latest.values()) if latest else ""
    # 目标日 = 日历上≤今天的最近交易日(全宇宙落后于它才需更新, 而非互相比较)
    from base.store.calendar_repo import get_trade_days

    days = get_trade_days(end=datetime.now().strftime("%Y-%m-%d"))
    target = days[-1] if days else universe_latest
    todo = [c for c in info_codes if latest.get(c, "") < target]
    today = datetime.now().strftime("%Y-%m-%d")
    done = 0
    failed = 0
    for i, code in enumerate(todo):
        n_kl = 0
        try:
            since = latest.get(code, "")
            if not since:  # 新股无历史→全量
                kl = _fetch_kline(code)
                if kl:
                    store.replace_kline(code, kl)
                    n_kl = len(kl)
            else:
                symbol = ("sh" if code.startswith(("6", "9")) else "sz") + code
                d = _tencent_kline(symbol, since, today)
                if d is not None:
                    node = d.get("data", {}).get(symbol, {})
                    rows = node.get("qfqday") or node.get("day") or []
                    new = []
                    for r in rows:
                        if len(r) >= 5 and r[0] and r[0] > since:
                            try:
                                new.append((r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4])))
                            except (TypeError, ValueError):
                                continue
                    if new:
                        store.upsert_kline(code, new)
                        n_kl = len(new)
            pe_since = store.pe_max_date(code) or f"{KLINE_START_YEAR}-01-01"
            new_pe = _fetch_pe_since(code, pe_since)
            if new_pe:
                store.upsert_pe(code, new_pe)
            done += 1
            progress(i + 1, len(todo), f"{code} +{n_kl}K +{len(new_pe)}PE")
        except Exception as e:
            failed += 1
            print(f"[value-daily] {code} 更新异常: {type(e).__name__} {e}")
        time.sleep(FETCH_SLEEP_SEC)
    cache.invalidate_all()
    job_value_rebuild_pool(lambda c, t, m: None)  # 池预览随数据同步刷新
    return {"todo": len(todo), "updated": done, "failed": failed}


def task_value_quarterly_rebuild() -> dict:
    """定时任务(每日检查): 仅当最新K线日=季度月(1/4/7/10)首个交易日时才重建池,
    其余交易日直接跳过(幂等无副作用)。"""
    latest_dates = store.latest_kline_dates()
    latest = max(latest_dates.values()) if latest_dates else ""
    if not latest:
        return {"skipped": True, "reason": "无K线数据"}
    month = int(latest[5:7])
    if month not in POOL_MONTHS:
        return {"skipped": True, "reason": f"{latest} 非季度月"}
    from base.store.calendar_repo import get_trade_days

    month_days = get_trade_days(f"{latest[:7]}-01", latest)
    if not month_days or month_days[0] != latest:
        return {"skipped": True, "reason": f"{latest} 非 {month} 月首个交易日"}
    return job_value_rebuild_pool(lambda c, t, m: print(f"[value-rebuild] {c}/{t} {m}"))


def _existing_codes() -> list[str]:
    from base.store.database import get_connection

    conn = get_connection()
    try:
        return [r["code"] for r in conn.execute("SELECT code FROM value_stock_info ORDER BY code")]
    finally:
        conn.close()


def _pe_at(pe_map: dict[str, float], date: str):
    v = pe_map.get(date)
    if v is not None:
        return v
    prev = [k for k in pe_map if k <= date]
    return pe_map[max(prev)] if prev else None


def job_value_rebuild_pool(progress) -> dict:
    """以最新K线日为 as-of 重建季度池并落库(防前视: 仅用生效日≤as_of 数据)。"""
    counts = store.kline_row_counts()
    codes = [c for c in counts if counts[c] > 0]
    if not codes:
        progress(1, 1, "无K线数据, 先运行 value_fetch_data")
        return {"codes": 0, "pool": 0}
    kline = {c: store.get_kline(c) for c in codes}
    as_of = max(max(kl) for kl in kline.values())
    snaps = []
    for c in codes:
        snap = store.get_snapshot(c)
        if not snap or as_of not in kline[c]:
            continue
        price = kline[c][as_of]["close"]
        snap["price"] = price
        snap["mcap_yi"] = price * (snap.get("total_shares_yi") or 0)
        snap["pe_ttm"] = _pe_at(store.get_pe(c), as_of)
        snaps.append(snap)
    pool = build_pool(snaps, as_of)
    store.save_pool(as_of, pool)
    cache.invalidate_all()
    progress(1, 1, f"as_of {as_of}: 池 {len(pool)} 只")
    return {"codes": len(snaps), "pool": len(pool), "as_of": as_of}
