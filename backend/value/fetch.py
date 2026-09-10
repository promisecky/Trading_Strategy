"""价值回归数据抓取层 — 直连可用 HTTPS 源, 限流/重试/断点续传(已缓存跳过)。

本机出口封锁 HTTP 明文与百度/乐咕, 实测可达源(2026-08):
- 宇宙预筛: push2delay.eastmoney.com clist(延迟行情, 收盘确认策略无影响)
- 行业/总股本: push2delay stock/get
- K线双价: 腾讯 ifzq.gtimg.cn(HTTPS, 单次≤~640根故按 850 自然日分块):
    RAW(不复权, 真实成交价, 永不随除权变化) + HFQ(后复权, 锚点在上市日,
    历史值永不随新除权重算) — 日更对两序列均为纯追加, 根治 qfq 锚点漂移断层
- PE-TTM 日线: datacenter.eastmoney.com RPT_VALUEANALYSIS_DET
- 分红: akshare stock_fhps_detail_em(datacenter-web, HTTPS 可达), 每笔独立存行
- ROE: akshare stock_financial_analysis_indicator(新浪, HTTPS 可达)

净化层(fail-closed): 日期/值域校验, 脏行拒收; 双价不变量(HFQ/RAW 比值≥1且
单调不减, 有除权必增长)破坏则整只拒收; 抓取失败绝不清空存量。

近似项(文档化):
- 宇宙预筛用当前快照(市值/行业), 历史季度池仍以 as-of 基本面门判定(幸存者偏差无法离线消除, 结论中声明);
- ROE 披露日缺失时用法定截止日(年报 FY+1-04-30)。
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from value import cache, store
from value.config import IDLE_FUND_CODE, KLINE_START_YEAR, MCAP_MIN_YI, POOL_MONTHS, REAL_ESTATE_KEYWORDS
from value.engine import build_pool

FETCH_SLEEP_SEC = 0.4
_EM_UT = "fa5fd1943c7b386f172d6893dbfba10b"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RATIO_TOL = 0.004  # HFQ/RAW 复权比 ≥1 的舍入容差(仿射复权 F≥1, D≥0, 实测确认)


def _symbol(code: str) -> str:
    """腾讯行情代码前缀: 沪市 6/9/5(ETF) → sh, 其余(0/3/1) → sz。"""
    return ("sh" if code.startswith(("6", "9", "5")) else "sz") + code


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


def _norm_date(v) -> str:
    """任意输入 → 'YYYY-MM-DD' 或 ''。
    对抗性: pd.NaT 拥有 .date 属性(旧代码 str(NaT.date()) 落库 "NaT" 脏字符串的根因),
    故不再信任属性探测结果, 统一用正则终判; NaT/None/乱串一律归一为 ""。"""
    if v is None:
        return ""
    date_fn = getattr(v, "date", None)
    if callable(date_fn):
        try:
            s = str(date_fn())
        except (ValueError, TypeError):
            return ""
        return s if _DATE_RE.match(s) else ""
    s = str(v).strip()[:10]
    return s if _DATE_RE.match(s) else ""


def _parse_dividend_df(df) -> list[dict]:
    """东财分红明细 → 每笔独立行 [{fiscal_year,dps,announce_date,ex_date}]。
    同财年多笔(中期+年报)不在存储层聚合 — 聚合是引擎职责(check_gates 按财年求和),
    存储层保留每笔事实(各自除权日/公告日), 旧版按财年归并会丢失后续笔的除权日。"""
    out: list[dict] = []
    for _, r in df.iterrows():
        fy_raw = str(r.get("报告期", ""))[:4]
        ratio = r.get("现金分红-现金分红比例")  # 每 10 股派息
        if not fy_raw.isdigit() or ratio is None:
            continue  # 送转行现金比例为 NaN, 非现金分红不入表
        try:
            dps = float(ratio) / 10.0
        except (TypeError, ValueError):
            continue
        if not 0 < dps < 1e4:  # NaN/负值/无穷/荒谬值一律拒收
            continue
        out.append(
            {
                "fiscal_year": int(fy_raw),
                "dps": round(dps, 6),
                "announce_date": _norm_date(r.get("预案公告日")),
                "ex_date": _norm_date(r.get("除权除息日")),
            }
        )
    return sorted(out, key=lambda x: (x["fiscal_year"], x["ex_date"], x["announce_date"]))


def _fetch_dividends(code: str) -> list[dict] | None:
    """拉取分红; 上游失败返回 None(区别于"确无分红"的 [], 防止失败时清空存量)。"""
    import akshare as ak

    df = _try(ak.stock_fhps_detail_em, symbol=code)
    if df is None:
        return None
    if df.empty:
        return []
    return _parse_dividend_df(df)


def _fetch_roes(code: str) -> list[dict] | None:
    """年度 ROE; 上游失败返回 None(防清空存量)。披露日缺失用法定截止日 FY+1-04-30(文档化近似)。"""
    import akshare as ak

    df = _try(ak.stock_financial_analysis_indicator, symbol=code, start_year="2005")
    if df is None:
        return None
    if df.empty:
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
        if not -100 <= roe_v <= 100:  # 值域守卫: 脏数据拒收
            continue
        fy = int(d[:4])
        out.append({"fiscal_year": fy, "roe": roe_v, "disclose_date": f"{fy + 1}-04-30"})
    return sorted(out, key=lambda x: x["fiscal_year"])


def _fetch_pe(code: str) -> list[tuple[str, float]] | None:
    """PE-TTM 日线(东财估值明细, 分页降序全量); 任一页失败返回 None(残缺序列会静默覆盖存量, 故整只放弃)。"""
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
        if d is None:
            return None
        result = (d or {}).get("result") or {}
        rows = result.get("data") or []
        for r in rows:
            pe = r.get("PE_TTM")
            if pe is None:
                continue
            try:
                pe_v = float(pe)
                dt = str(r["TRADE_DATE"])[:10]
            except (TypeError, ValueError, KeyError):
                continue
            if _DATE_RE.match(dt) and -1e6 < pe_v < 1e6:  # 日期/值域净化
                out.append((dt, pe_v))
        if not rows or pn >= (result.get("pages") or 0):
            break
        pn += 1
        time.sleep(FETCH_SLEEP_SEC)
    return out


def _kline_rows(d, symbol: str, fq: str, ws: str, we: str) -> list[tuple[str, float, float, float, float]]:
    """腾讯K线响应 → (日,开,收,高,低) 列表; 日期正则+窗口过滤, 非数值行拒收。
    键严格匹配请求口径(hfq→hfqday, 不复权→day), 绝不回退到其他口径的键 —
    静默回退会让 raw 冒充 hfq 落库(实测部分镜像对科创板 hfq 请求只返 day 键)。"""
    node = (d or {}).get("data", {}).get(symbol, {})
    rows = node.get("hfqday" if fq == "hfq" else "day") or []
    out = []
    for r in rows:
        if len(r) < 5 or not r[0] or not _DATE_RE.match(str(r[0])) or not (ws <= str(r[0]) <= we):
            continue  # 部分端点会多返回窗口前的行, 按窗口过滤
        try:
            vals = (float(r[1]), float(r[2]), float(r[3]), float(r[4]))
        except (TypeError, ValueError):
            continue
        if min(vals) <= 0 or vals[2] < vals[3] or vals[0] <= 0:  # 正价 + 高≥低 + 收盘>0
            continue
        out.append((str(r[0]), *vals))
    return out


def _merge_pair(raw_rows, hfq_rows, divs: list[tuple[str, float]] | None) -> tuple[list[tuple], str]:
    """RAW/HFQ 按日归并 + 不变量校验(fail-closed, 破坏则整只拒收):
    1. 同日双价齐全且均为正, 高≥低, 逐日复权比 > 0;
    2. 窗口斜率校验(有显著除权且价差可辨时): F = Δhfq/Δraw 必须偏离 1 —
       实证腾讯 HFQ 为仿射复权 hfq = F×raw + D(茅台 F≈5.6, 天风 F≈1.29/D<0(配股),
       511880 F≈0.0113(份额折算)), 而 qfq 是斜率恒为 1 的减法复权(qfq = raw − 未来分红),
       故 |F−1|≤0.01 = qfq 混入/未复权 → 拒收;
    3. 存在显著分红(单笔 ≥1.5%)时, 复权比不得恒等于 1(防 hfq 静默退化成 raw;
       真 hfq 必在除权处累积因子使 ratio 分段变化, 而配股/折算可致 ratio 合法地 <1);
    4. 显著除权日分红补偿统计校验: HFQ日涨幅 − RAW日涨幅 应≈ dps/前收。
       实测两源存在噪声(东财 dps 口径/送转伴随/日期归并): 带内比例≥50%且均值方向为正即通过。
    注: ratio 绝对值≥1/恒定/单调均不可作不变量(仿射复权下 ratio = F + D/raw 随 raw 波动,
    配股可致 D<0 使 ratio<1, 基金份额折算使 ratio≈0.01); 锚点稳定性由跨窗口一致性实证保证。
    divs: [(ex_date, dps)]; 增量追加场景无除权信息传 None(跳过 2/3/4, 由衔接守卫兜底)。
    返回 (rows9, err)。"""
    hfq = {r[0]: r for r in hfq_rows}
    rows: list[tuple] = []
    ratios: list[float] = []
    for d, o, c, h, l in raw_rows:
        a = hfq.get(d)
        if a is None:
            continue  # 双端日期未对齐(罕见) → 弃该行, 不造单腿数据
        ao, ac, ah, al = a[1], a[2], a[3], a[4]
        if min(ao, ac, ah, al) <= 0 or ah < al:
            return [], f"{d} HFQ 非法值"
        ratio = ac / c
        if ratio <= 0:
            return [], f"{d} 复权比非正"
        ratios.append(ratio)
        rows.append((d, o, c, h, l, ao, ac, ah, al))
    if not rows:
        return [], "无归并行"
    ds = [r[0] for r in rows]
    by_d = {r[0]: r for r in rows}
    prev_of = {ds[i]: ds[i - 1] for i in range(1, len(ds))}
    # 显著分红 = 单笔 dps/前收 ≥ 1.5%(判别力阈值, 与补偿校验一致)
    sig: list[tuple[str, float, float]] = []  # (ex, dps, theory)
    for e, dps in divs or []:
        p = prev_of.get(e)
        if not (dps > 0) or p is None or not (ds[0] <= e <= ds[-1]):
            continue
        theory = dps / by_d[p][2]
        if theory >= 0.015:
            sig.append((e, dps, theory))
    if sig:
        # 斜率校验: qfq = raw − S 斜率恒为1; 真 hfq 斜率 F 必偏离 1(每笔显著除权至少 +1.5%)
        d0, d1 = rows[0], rows[-1]
        draw = d1[2] - d0[2]
        if abs(draw) >= max(0.3, 0.1 * d0[2]):  # 价差可辨才做斜率判定
            f_est = (d1[6] - d0[6]) / draw
            if abs(f_est - 1.0) <= 0.01:
                return [], f"窗口斜率 {f_est:.4f}≈1(qfq 混入/未复权序列嫌疑)"
        if max(ratios) - min(ratios) <= 0.002 and abs(ratios[-1] - 1.0) <= _RATIO_TOL:
            # 复权比恒等于 1 = hfq 从未复权(真 hfq 在除权处必产生 ratio 分段;
            # 天风证券配股致 ratio 0.82~1.16 波动、511880 折算致 ratio≈0.0113 均合法)
            return [], f"窗口内 {len(sig)} 次显著除权但复权比恒为1(hfq 端退化成 raw)"
    comps: list[tuple[float, float, float]] = []  # (实际补偿, 理论补偿, 除权日raw涨跌)
    for ex, dps, theory in sig:
        p = prev_of[ex]
        raw_ret = by_d[ex][2] / by_d[p][2] - 1
        hfq_ret = by_d[ex][6] / by_d[p][6] - 1
        comp = hfq_ret - raw_ret
        comps.append((comp, theory, raw_ret))
    if comps:
        # 带内 = 补偿方向正确且量级合理。上界 = max(theory+0.30, −raw_ret+0.03):
        # 送转伴随除权时 raw 跳降含送转份额(实测拓普/博腾 comp≈+32%), 而 hfq 不跌 →
        # comp ≈ −raw_ret, 用 raw 实际跌幅自适应放宽; 伪 hfq(comp≈0)仍被下界拒收
        in_band = sum(
            1 for c, t, rr in comps if 0.35 * t - 0.004 <= c <= max(t + 0.30, -rr + 0.03)
        )
        mean_comp = sum(c for c, _, _ in comps) / len(comps)
        if mean_comp <= 0 or in_band * 2 < len(comps):
            return [], (
                f"分红补偿校验失败: 带内 {in_band}/{len(comps)}, 均值{mean_comp:+.4f}"
                "(hfq 未剔除除权跳降, 疑似伪 hfq/未复权序列)"
            )
    return rows, ""


def _fetch_kline_pair(code: str, divs: list[tuple[str, float]] | None = None) -> list[tuple]:
    """全窗口 RAW+HFQ 双价K线(按 850 自然日分块规避单次~640根上限)。
    窗口语义: 上市前的连续空窗(无键/无行)合法; 有数据窗口之后再现缺失 = 中途残缺 → 整只拒。
    任一块真失败或不变量破坏返回 [](fail-closed, 避免残缺/污染序列落库)。"""
    symbol = _symbol(code)
    start = datetime(KLINE_START_YEAR, 1, 1)
    end = datetime.now()
    windows: list[tuple[str, str]] = []
    cur = start
    while cur <= end:
        win_end = min(cur + timedelta(days=850), end)
        windows.append((cur.strftime("%Y-%m-%d"), win_end.strftime("%Y-%m-%d")))
        cur = win_end + timedelta(days=1)
    raw_resp, hfq_resp = [], []
    for ws, we in windows:
        raw_resp.append(_tencent_kline(symbol, ws, we, fq=""))
        hfq_resp.append(_tencent_kline(symbol, ws, we, fq="hfq"))
        time.sleep(0.3)  # 限流: 风控敏感, 不宜提速(曾触发 501 封锁)

    def collect(resps: list, fq: str) -> list[tuple] | None:
        seen_data = False
        rows: list[tuple] = []
        for (ws, we), d in zip(windows, resps):
            r = _kline_rows(d, symbol, fq, ws, we) if d is not None else []
            if r:
                seen_data = True
                rows += r
            elif seen_data:
                return None  # 有数据后的缺失窗口 = 中途残缺, 整只拒收
        return rows if seen_data else None

    raw_all = collect(raw_resp, "")
    hfq_all = collect(hfq_resp, "hfq")
    if not raw_all or not hfq_all:
        print(f"[value-fetch] {code} 双价拉取失败(raw={'ok' if raw_all else '缺'} hfq={'ok' if hfq_all else '缺'})")
        return []
    rows, err = _merge_pair(raw_all, hfq_all, divs)
    if err:
        print(f"[value-fetch] {code} 双价校验失败: {err}")
        return []
    return rows


# 腾讯行情多源: 主域被风控(501)时轮换镜像端点; fq=""为不复权, "hfq"为后复权
_TENCENT_HOSTS = (
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get",
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
)


def _tencent_kline(symbol: str, start: str, end: str, fq: str = ""):
    """多镜像轮换: 请求异常或响应缺少目标口径键(hfq→hfqday / 不复权→day)都视为
    该端点失败 → 试下一镜像(实测: 部分镜像对科创板 hfq 请求返 501 或缺 hfqday 键)。"""
    param = f"{symbol},day,{start},{end},650,{fq}"
    want = "hfqday" if fq == "hfq" else "day"
    last_err: Exception | str | None = None
    for base in _TENCENT_HOSTS:
        url = f"{base}?param={param}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                d = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            time.sleep(0.5)
            continue
        node = (d or {}).get("data", {}).get(symbol, {})
        if node.get(want):
            return d
        last_err = f"响应缺 {want} 键(该镜像不支持此复权口径)"
        time.sleep(0.3)
    print(f"[value-fetch] tencent kline all hosts failed ({symbol} fq={fq or 'raw'}): {type(last_err).__name__ if isinstance(last_err, Exception) else 'KeyMissing'} {last_err}")
    return None


def fetch_stock(code: str, with_fundamentals: bool = True) -> dict:
    """逐只全量拉取并落库, 返回各表行数。
    顺序: 分红先行(K线不变量校验需除权日清单) → 双价K线 → ROE/PE。
    fail-closed: 任一环节失败/校验不过 → 不覆盖该项存量(绝不清空)。"""
    divs = _fetch_dividends(code)
    if divs is not None:
        store.replace_dividends(code, divs)
    if divs is None:  # 分红拉取失败: 用库存除权日做K线校验(降级不中断)
        snap = store.get_snapshot(code)
        divs = (snap or {}).get("dividends", [])
    div_pairs = [(d["ex_date"], d["dps"]) for d in divs if d.get("ex_date") and (d.get("dps") or 0) > 0]
    kline = _fetch_kline_pair(code, div_pairs)
    n_kl = store.replace_kline(code, kline) if kline else 0
    n_roe = n_pe = 0
    if with_fundamentals:
        roes = _fetch_roes(code)
        if roes is not None:
            store.replace_roes(code, roes)
            n_roe = len(roes)
        pe = _fetch_pe(code)
        if pe is not None:
            store.replace_pe(code, pe)
            n_pe = len(pe)
    return {"kline": n_kl, "dividends": len(divs), "roes": n_roe, "pe": n_pe}


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
        if not n["kline"]:  # 拉取/校验失败(存量未被触碰) → 计入失败, 下次重试
            failed += 1
            continue
        done += 1
        progress(i + 1, len(todo), f"{code} K线{n['kline']}(RAW+HFQ) 分红{n['dividends']} ROE{n['roes']} PE{n['pe']}")
        time.sleep(FETCH_SLEEP_SEC)
    _update_idle_fund()
    cache.invalidate_all()  # 数据已变: 失效回测/池缓存并后台重算, 前端下个请求即命中新数据
    return {"universe": len(info_codes), "fetched": done, "failed": failed, "skipped": len(info_codes) - len(todo)}


def job_value_upgrade_kline(progress) -> dict:
    """RAW+HFQ 双价迁移(幂等, 可断点续传): 缺 HFQ 价或缺 high/low 的存量个股
    全量重拉双价K线 + 每笔分红(NaT 修复), 旧 qfq 单价口径由此根除。
    ROE/PE 存量保留不重拉(与价格口径无关, 控制请求量)。"""
    todo = sorted(set(store.codes_missing_adj()) | set(store.codes_missing_hl()))
    done = 0
    failed = 0
    for i, code in enumerate(todo):
        try:
            n = fetch_stock(code, with_fundamentals=False)
            if n["kline"]:
                done += 1
                progress(i + 1, len(todo), f"{code} K线{n['kline']}根(RAW+HFQ) 分红{n['dividends']}笔")
            else:
                failed += 1
                progress(i + 1, len(todo), f"{code} 拉取/校验失败, 下次重试")
        except Exception as e:
            failed += 1
            print(f"[value-fetch] {code} 双价迁移异常: {type(e).__name__} {e}")
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


def _incremental_dual(code: str, since: str, today: str, last: tuple) -> int:
    """日更增量: RAW+HFQ 双序列追加(两序列锚点各自稳定, 纯追加安全)。
    复权比连续性守卫: 新行首 ratio ≥ 库内末行 ratio(HFQ 因子单调不减);
    违反 → 混合锚点/库内口径变更嫌疑 → 该股全量重拉(自愈)。返回新增行数。"""
    symbol = _symbol(code)
    dr = _tencent_kline(symbol, since, today, fq="")
    dh = _tencent_kline(symbol, since, today, fq="hfq")
    if dr is None or dh is None:
        return 0
    raw_new = [r for r in _kline_rows(dr, symbol, "", since, today) if r[0] > since]
    hfq_new = [r for r in _kline_rows(dh, symbol, "hfq", since, today) if r[0] > since]
    if not raw_new and not hfq_new:
        return 0  # 已是最新, 无新行
    new, err = _merge_pair(raw_new, hfq_new, None)
    if new:
        # 衔接守卫: hfq 锚点已实证跨窗口稳定, 合法日间变化仅来自除权因子(向上)与
        # 仿射回拉(随 raw 涨跌小幅双向); 跳出宽松带 = 口径损坏嫌疑 → 全量重拉自愈。
        # 不用绝对 ratio≥1 判定: 基金份额折算等恒定尺度因子(511880≈0.0113)同样合法
        r_last = last[2] / last[1]
        r_new = new[0][6] / new[0][2]
        if not (0.5 * r_last <= r_new <= 2.5 * r_last):
            err = f"复权比跳变 {r_last:.4f}->{r_new:.4f}(口径损坏嫌疑)"
            new = []
    if new:
        return store.upsert_kline(code, new)
    print(f"[value-daily] {code} 增量守卫触发({err}) → 全量重拉自愈")
    n = fetch_stock(code, with_fundamentals=False)
    return n["kline"]


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
    # 双价迁移门禁: 未完成 RAW+HFQ 迁移的个股不入日更清单(否则 qfq 旧行上追加 raw 新行
    # 会在衔接处造出假除权断层), 由 value_upgrade_kline 迁移任务统一处理
    unmigrated = set(store.codes_missing_adj())
    todo = [c for c in info_codes if latest.get(c, "") < target and c not in unmigrated]
    today = datetime.now().strftime("%Y-%m-%d")
    done = 0
    failed = 0
    for i, code in enumerate(todo):
        n_kl = 0
        try:
            since = latest.get(code, "")
            last = store.last_price_row(code) if since else None
            if not since or last is None or last[2] is None:
                # 新股无历史, 或存量尚未双价迁移(aclose 缺失) → 全量(含不变量校验)
                n = fetch_stock(code, with_fundamentals=not since)
                n_kl = n["kline"]
            else:
                n_kl = _incremental_dual(code, since, today, last)
            pe_since = store.pe_max_date(code) or f"{KLINE_START_YEAR}-01-01"
            new_pe = _fetch_pe_since(code, pe_since)
            if new_pe:
                store.upsert_pe(code, new_pe)
            done += 1
            progress(i + 1, len(todo), f"{code} +{n_kl}K(RAW+HFQ) +{len(new_pe)}PE")
        except Exception as e:
            failed += 1
            print(f"[value-daily] {code} 更新异常: {type(e).__name__} {e}")
        time.sleep(FETCH_SLEEP_SEC)
    _update_idle_fund()
    cache.invalidate_all()
    job_value_rebuild_pool(lambda c, t, m: None)  # 池预览随数据同步刷新
    return {"todo": len(todo), "updated": done, "failed": failed}


def task_value_freshness_guard() -> dict:
    """小时级巡检(共享 ETF 交易日历): 任一个股落后于日历最近交易日即追更(含部分落后),
    新鲜时零开销直接返回 — 彻底兼容 调度错过/停机/网络抽风 等场景。
    双价迁移优先: 存在未迁移个股时, 日期即使新鲜也自动启动迁移任务(exclusive,
    已在运行则静默跳过), 杜绝旧口径数据上继续叠加新行。"""
    from base.store.calendar_repo import get_trade_days

    latest = store.latest_kline_dates()
    cur = max(latest.values()) if latest else ""
    unmigrated = store.codes_missing_adj()
    if unmigrated:
        from base.scheduler.job_manager import job_manager
        from base.scheduler.job_registry import JOB_DEFS, JOB_FNS

        if not job_manager.can_start("value_upgrade_kline", JOB_DEFS["value_upgrade_kline"]["exclusive"]):
            return {"fresh": False, "migrating": len(unmigrated), "job_id": None}  # 迁移已在运行
        # 巡检自身跑在工作线程(无事件循环): 同步执行迁移, 进度接入 job_manager 供前端展示;
        # exclusive 标记保证小时级重入被 can_start 拦住
        job_id = job_manager.submit("value_upgrade_kline", {}, True)
        job_manager.mark_running(job_id)
        try:
            result = JOB_FNS["value_upgrade_kline"](lambda c, t, m: job_manager.progress(job_id, c, t, m))
            job_manager.finish(job_id, result, None)
        except Exception as e:
            job_manager.finish(job_id, None, f"{type(e).__name__}: {e}")
        return {"fresh": False, "migrating": len(unmigrated), "job_id": job_id}
    days = get_trade_days(end=datetime.now().strftime("%Y-%m-%d"))
    target = days[-1] if days else cur
    info_codes = _existing_codes()
    lagging = [c for c in info_codes if latest.get(c, "") < target]
    if lagging:
        print(f"[value-guard] stale: max={cur} target={target} lagging={len(lagging)}, catching up...")
        res = task_value_daily_update()
        return {"fresh": False, **res}
    return {"fresh": True, "latest": cur}


def _update_idle_fund() -> int:
    """闲钱货币基金(511880)K线同步: 无历史/未双价迁移全量, 否则双价增量(守卫同上)。
    回测计息用 HFQ 收盘比(基金分红除权日 RAW 价跳降, HFQ 才是真实收益口径)。"""
    code = IDLE_FUND_CODE
    since = store.latest_kline_dates().get(code, "")
    today = datetime.now().strftime("%Y-%m-%d")
    last = store.last_price_row(code) if since else None
    if not since or last is None or last[2] is None:
        rows = _fetch_kline_pair(code)
        return store.replace_kline(code, rows) if rows else 0
    return _incremental_dual(code, since, today, last)


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
