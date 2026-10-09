from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

from base.config import (
    ETFS,
    SHARES_RETRY,
    SHARES_RETRY_BACKOFF_SEC,
    SSE_TIMEOUT,
)

_SSE_CACHE: dict[str, dict[str, float]] = {}
_SZSE_CACHE: dict[str, dict[str, float]] = {}
_SSE_LOF_CACHE: dict[str, dict[str, float]] = {}

# SSE 源封锁冷却: 连续失败后短时间内不再发请求(被 WAF reset 时重试无意义)
_SSE_COOLDOWN_UNTIL = 0.0
_SSE_SOURCE_BLOCKED = False  # 最近一次 SSE 请求为网络层异常(封锁), 重试无意义
SSE_COOLDOWN_SEC = 600
SSE_PROXY_COOLDOWN_SEC = 10  # 走代理时失败多为瞬断/超时, 短冷却即可


def _sse_proxies() -> dict[str, str] | None:
    """SSE 专用代理: 环境变量 SSE_PROXY_URL(本机出口 IP 被上交所封锁时换出口)。"""
    proxy = os.environ.get("SSE_PROXY_URL", "").strip()
    return {"http": proxy, "https": proxy} if proxy else None


def _date_to_ak(d: str) -> str:
    return d.replace("-", "")


def _ak_to_date(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def fetch_shares_sse(date_str: str) -> dict[str, float]:
    global _SSE_COOLDOWN_UNTIL, _SSE_SOURCE_BLOCKED
    key = date_str
    if key in _SSE_CACHE:
        return _SSE_CACHE[key]
    if time.time() < _SSE_COOLDOWN_UNTIL:
        return {}

    try:
        import requests

        url = "https://query.sse.com.cn/commonQuery.do"
        params = {
            "isPagination": "true",
            "pageHelp.pageSize": "10000",
            "pageHelp.pageNo": "1",
            "pageHelp.beginPage": "1",
            "pageHelp.cacheSize": "1",
            "pageHelp.endPage": "1",
            "sqlId": "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L",
            "STAT_DATE": date_str,
        }
        headers = {
            "Referer": "https://www.sse.com.cn/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        r = requests.get(
            url, params=params, headers=headers, timeout=SSE_TIMEOUT, proxies=_sse_proxies()
        )
        rows = r.json().get("result") or []
        if not rows:
            return {}
        result = {}
        for row in rows:
            code = str(row.get("SEC_CODE", ""))
            vol = row.get("TOT_VOL")
            if code and vol is not None:
                result[code] = float(vol) / 1e4
        _SSE_COOLDOWN_UNTIL = 0.0
        _SSE_SOURCE_BLOCKED = False
        _SSE_CACHE[key] = result
        return result
    except Exception as e:
        if _sse_proxies():
            # 代理链路失败多为瞬断/超时, 不标记封锁, 短冷却后继续
            _SSE_COOLDOWN_UNTIL = time.time() + SSE_PROXY_COOLDOWN_SEC
            _SSE_SOURCE_BLOCKED = False
        else:
            _SSE_COOLDOWN_UNTIL = time.time() + SSE_COOLDOWN_SEC
            _SSE_SOURCE_BLOCKED = True
        print(f"[FETCH] SSE shares {date_str} failed: {e}")
        return {}


def sse_source_blocked() -> bool:
    """SSE 源最近一次请求是否网络层失败(封锁/断网), 用于跳过无意义重试。"""
    return _SSE_SOURCE_BLOCKED


def fetch_shares_szse(date_str: str) -> dict[str, float]:
    key = date_str
    if key in _SZSE_CACHE:
        return _SZSE_CACHE[key]

    try:
        import akshare as ak

        d8 = _date_to_ak(date_str)
        df = ak.fund_scale_daily_szse(start_date=d8, end_date=d8, symbol="ETF")
        if df is None or df.empty:
            return {}
        result = {}
        for _, row in df.iterrows():
            code = str(row.get("基金代码", ""))
            shares = row.get("基金份额")
            if code and shares is not None:
                result[code] = float(shares) / 1e8
        _SZSE_CACHE[key] = result
        return result
    except Exception as e:
        print(f"[FETCH] SZSE shares {date_str} failed: {e}")
        return {}


def fetch_shares_sse_lof() -> dict[str, float]:
    """上交所 LOF 规模快照(单位: 亿份; 键为基金代码)。

    注意: 该接口**只返回最新交易日一个快照**(日期参数会被忽略, 实测传历史
    日期仍返回最新), 因此不能用于历史回填 —— 当前仅用于识别"LOF 标的没有
    历史份额序列"。ETF 的逐日份额请用 fetch_shares_sse。
    按本地日期缓存(快照基本日更, 避免同进程反复请求)。
    """
    key = datetime.now().strftime("%Y-%m-%d")
    if key in _SSE_LOF_CACHE:
        return _SSE_LOF_CACHE[key]

    try:
        import requests

        url = "https://query.sse.com.cn/commonQuery.do"
        params = {
            "isPagination": "true",
            "pageHelp.pageSize": "10000",
            "pageHelp.pageNo": "1",
            "pageHelp.beginPage": "1",
            "pageHelp.cacheSize": "1",
            "pageHelp.endPage": "1",
            "sqlId": "COMMON_SSE_FUND_LOF_SCALE_CX_S",
        }
        headers = {
            "Referer": "https://www.sse.com.cn/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        r = requests.get(url, params=params, headers=headers, timeout=AKSHARE_TIMEOUT)
        rows = r.json().get("result") or []
        if not rows:
            return {}
        result = {}
        for row in rows:
            code = str(row.get("FUND_CODE", ""))
            vol = row.get("INTERNAL_VOL")
            if code and vol is not None:
                result[code] = float(str(vol).replace(",", "")) / 1e4
        _SSE_LOF_CACHE[key] = result
        return result
    except Exception as e:
        print(f"[FETCH] SSE LOF shares failed: {e}")
        return {}


def share_data_source(code: str, date_str: str) -> str:
    """该标的份额数据源类型(供回填前置判断, 避免对无数据标的逐日空转重试):

    - "etf": ETF 逐日规模接口可用, 可完整回填历史;
    - "lof": 仅存在于上交所 LOF 快照名单(无历史日期参数), 无法回填序列;
    - "none": ETF/LOF 两个名单都没有;
    - "unknown": 数据源异常, 无法判断(调用方按正常流程尝试)。
    """
    sse = fetch_shares_sse(date_str)
    szse = fetch_shares_szse(date_str)
    if not sse and not szse:
        return "unknown"
    if code in sse or code in szse:
        return "etf"
    if code in fetch_shares_sse_lof():
        return "lof"
    return "none"


def fetch_shares_for_date(date_str: str) -> dict[str, float]:
    """拉取某日全部监控 ETF 份额; 沪深分市场独立重试(单市场失败不掩盖另一市场)。

    某市场连续空/失败时按 SHARES_RETRY 退避重试(限流容错), 成功后不再重拉;
    SSE 被封锁时走冷却快速跳过, 不拖慢整体回填。
    """
    sse: dict[str, float] = {}
    szse: dict[str, float] = {}
    for attempt in range(SHARES_RETRY + 1):
        if not sse:
            sse = fetch_shares_sse(date_str)
        if not szse:
            szse = fetch_shares_szse(date_str)
        if sse and szse:
            break
        # SSE 源被封锁时重试无意义, 直接返回已拿到的部分
        if not sse and sse_source_blocked():
            break
        if attempt < SHARES_RETRY:
            wait = SHARES_RETRY_BACKOFF_SEC * (attempt + 1)
            missing = "SSE" if not sse else "SZSE"
            print(f"[FETCH] shares {date_str} {missing} 空结果, {wait}s 后重试 ({attempt + 1}/{SHARES_RETRY})")
            time.sleep(wait)
    merged = {**sse, **szse}
    return {code: merged[code] for code in ETFS if code in merged}


def _recent_weekdays(date_str: str, max_lookback: int) -> list[str]:
    """date_str 往前回溯的工作日序列(跳过周末, 含最多 max_lookback+1 天)。"""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    out: list[str] = []
    cur = dt
    while len(out) <= max_lookback:
        if cur.weekday() < 5:
            out.append(cur.strftime("%Y-%m-%d"))
        cur -= timedelta(days=1)
    return out


def fetch_latest_shares(date_str: str, max_lookback: int = 5) -> dict[str, dict]:
    """返回 {code: {"shares_yi": float, "date": str}} 使用最近可用数据(跳过周末)"""
    for check in _recent_weekdays(date_str, max_lookback):
        shares = fetch_shares_for_date(check)
        if shares:
            return {code: {"shares_yi": v, "date": check} for code, v in shares.items()}
    return {}


def calc_share_delta(date_str: str, max_lookback: int = 7) -> dict[str, dict]:
    """计算份额日变化: {code: {"shares_yi", "delta_yi", "delta_pct", "date"}}

    份额 T+1 发布: 拉当日(未发布)会得到空结果, 自动回溯到最近已发布日。
    返回空 dict 表示 max_lookback 内均无数据(远端异常或全部未发布)。
    """
    today_shares = None
    today_date = None

    for check in _recent_weekdays(date_str, max_lookback):
        shares = fetch_shares_for_date(check)
        if shares:
            today_shares = shares
            today_date = check
            break

    if not today_shares:
        return {}
    assert today_date is not None

    prev_shares = None
    for check in _recent_weekdays(today_date, max_lookback)[1:]:
        shares = fetch_shares_for_date(check)
        if shares:
            prev_shares = shares
            break

    result = {}
    for code, shares_yi in today_shares.items():
        delta_yi = None
        delta_pct = None
        if prev_shares and code in prev_shares and prev_shares[code] > 0:
            delta_yi = round(shares_yi - prev_shares[code], 4)
            delta_pct = round(delta_yi / prev_shares[code] * 100, 3)
        result[code] = {
            "shares_yi": round(shares_yi, 4),
            "delta_yi": delta_yi,
            "delta_pct": delta_pct,
            "date": today_date,
        }
    return result
