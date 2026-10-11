"""价值回归策略存储层 — value_* 表读写(与 ETF 表完全隔离)。"""

from __future__ import annotations

import json

from base.store.database import get_connection
from value.kline_view import KlineView


def upsert_stock_info(code: str, name: str, industry: str, total_shares_yi: float | None) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO value_stock_info(code, name, industry, total_shares_yi, updated_at)
               VALUES(?,?,?,?,datetime('now','localtime'))
               ON CONFLICT(code) DO UPDATE SET name=excluded.name, industry=excluded.industry,
                 total_shares_yi=excluded.total_shares_yi, updated_at=excluded.updated_at""",
            (code, name, industry, total_shares_yi),
        )
        conn.commit()
    finally:
        conn.close()


def get_stock_info(code: str) -> dict | None:
    conn = get_connection()
    try:
        r = conn.execute("SELECT * FROM value_stock_info WHERE code=?", (code,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def replace_dividends(code: str, rows: list[dict]) -> None:
    """每笔分红独立存行(同财年中期+年报多笔), seq 按入参顺序编号。"""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM value_dividend WHERE code=?", (code,))
        conn.executemany(
            "INSERT OR REPLACE INTO value_dividend(code, fiscal_year, seq, dps, announce_date, ex_date) VALUES(?,?,?,?,?,?)",
            [(code, r["fiscal_year"], i, r["dps"], r["announce_date"], r["ex_date"]) for i, r in enumerate(rows)],
        )
        conn.commit()
    finally:
        conn.close()


def replace_roes(code: str, rows: list[dict]) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM value_roe WHERE code=?", (code,))
        conn.executemany(
            "INSERT OR REPLACE INTO value_roe(code, fiscal_year, roe, disclose_date) VALUES(?,?,?,?)",
            [(code, r["fiscal_year"], r["roe"], r["disclose_date"]) for r in rows],
        )
        conn.commit()
    finally:
        conn.close()


def replace_pe(code: str, rows: list[tuple[str, float]]) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM value_pe WHERE code=?", (code,))
        conn.executemany(
            "INSERT OR REPLACE INTO value_pe(code, date, pe_ttm) VALUES(?,?,?)",
            [(code, d, v) for d, v in rows],
        )
        conn.commit()
    finally:
        conn.close()


def _valid_kline_row(row: tuple) -> bool:
    """入库守卫: 日期格式 + 八价齐全且为正 + 高≥低(RAW/HFQ 各自)。脏行拒收不落库。"""
    if len(row) != 9:
        return False
    d, o, c, h, l, ao, ac, ah, al = row
    if not (isinstance(d, str) and len(d) == 10 and d[4] == "-" and d[7] == "-"):
        return False
    for v in (o, c, h, l, ao, ac, ah, al):
        if not isinstance(v, (int, float)) or not v > 0:
            return False
    return h >= l and ah >= al


def replace_kline(code: str, rows: list[tuple]) -> int:
    """rows: (date, open, close, high, low, aopen, aclose, ahigh, alow) — RAW+HFQ 双价。
    返回实际写入行数(脏行拒收)。"""
    clean = [r for r in rows if _valid_kline_row(r)]
    conn = get_connection()
    try:
        conn.execute("DELETE FROM value_kline WHERE code=?", (code,))
        conn.executemany(
            "INSERT OR REPLACE INTO value_kline(code, date, open, close, high, low, aopen, aclose, ahigh, alow) VALUES(?,?,?,?,?,?,?,?,?,?)",
            [(code, *r) for r in clean],
        )
        conn.commit()
    finally:
        conn.close()
    return len(clean)


def get_kline(code: str) -> dict[str, dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT date, open, close, high, low, aopen, aclose, ahigh, alow FROM value_kline WHERE code=? ORDER BY date", (code,)
        ).fetchall()
        return {
            r["date"]: {
                "open": r["open"], "close": r["close"], "high": r["high"], "low": r["low"],
                "aopen": r["aopen"], "aclose": r["aclose"], "ahigh": r["ahigh"], "alow": r["alow"],
            }
            for r in rows
        }
    finally:
        conn.close()


def get_kline_view(code: str) -> KlineView:
    """get_kline 的紧凑内存版(见 kline_view.KlineView): 行为等价于
    {date: {8价格字段}}, 但常驻内存约省 5x(numpy 底层 + intern 日期)。
    供 _inputs 常驻数据集使用, 回测/接口消费方无需感知差异。
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT date, open, close, high, low, aopen, aclose, ahigh, alow FROM value_kline WHERE code=? ORDER BY date",
            (code,),
        ).fetchall()
        return KlineView.from_rows(rows)
    finally:
        conn.close()


def get_pe(code: str) -> dict[str, float]:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT date, pe_ttm FROM value_pe WHERE code=?", (code,)).fetchall()
        return {r["date"]: r["pe_ttm"] for r in rows}
    finally:
        conn.close()


def kline_row_counts() -> dict[str, int]:
    conn = get_connection()
    try:
        return {r["code"]: r["n"] for r in conn.execute("SELECT code, COUNT(*) n FROM value_kline GROUP BY code")}
    finally:
        conn.close()


def pe_row_counts() -> dict[str, int]:
    conn = get_connection()
    try:
        return {r["code"]: r["n"] for r in conn.execute("SELECT code, COUNT(*) n FROM value_pe GROUP BY code")}
    finally:
        conn.close()


def codes_missing_hl() -> list[str]:
    """缺 high/low 的个股(升级前抓取的存量), 供 K线升级任务重拉。"""
    conn = get_connection()
    try:
        return [
            r["code"]
            for r in conn.execute(
                "SELECT code FROM value_kline WHERE high IS NULL OR low IS NULL GROUP BY code ORDER BY code"
            )
        ]
    finally:
        conn.close()


def codes_missing_adj() -> list[str]:
    """任一行缺 HFQ 价的个股(RAW+HFQ 双价迁移待办清单, 断点续传基准)。"""
    conn = get_connection()
    try:
        return [
            r["code"]
            for r in conn.execute(
                """SELECT code FROM value_kline
                   WHERE aopen IS NULL OR aclose IS NULL OR ahigh IS NULL OR alow IS NULL
                   GROUP BY code ORDER BY code"""
            )
        ]
    finally:
        conn.close()


def last_price_row(code: str) -> tuple | None:
    """最新一行的 (date, close, aclose): 日更追加前的复权比连续性守卫基准。"""
    conn = get_connection()
    try:
        r = conn.execute(
            "SELECT date, close, aclose FROM value_kline WHERE code=? ORDER BY date DESC LIMIT 1", (code,)
        ).fetchone()
        return (r["date"], r["close"], r["aclose"]) if r else None
    finally:
        conn.close()


def latest_kline_dates() -> dict[str, str]:
    """每只股票的最新K线日(增量抓取的落后判定基准)。"""
    conn = get_connection()
    try:
        return {
            r["code"]: r["d"] for r in conn.execute("SELECT code, MAX(date) d FROM value_kline GROUP BY code")
        }
    finally:
        conn.close()


def upsert_kline(code: str, rows: list[tuple]) -> int:
    """增量写入K线(已存在日期覆盖, 新日期追加); 脏行拒收, 返回写入行数。"""
    clean = [r for r in rows if _valid_kline_row(r)]
    conn = get_connection()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO value_kline(code, date, open, close, high, low, aopen, aclose, ahigh, alow) VALUES(?,?,?,?,?,?,?,?,?,?)",
            [(code, *r) for r in clean],
        )
        conn.commit()
    finally:
        conn.close()
    return len(clean)


def pe_max_date(code: str) -> str | None:
    conn = get_connection()
    try:
        r = conn.execute("SELECT MAX(date) d FROM value_pe WHERE code=?", (code,)).fetchone()
        return r["d"] if r else None
    finally:
        conn.close()


def upsert_pe(code: str, rows: list[tuple[str, float]]) -> None:
    """增量写入PE(已存在日期覆盖, 新日期追加)。"""
    conn = get_connection()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO value_pe(code, date, pe_ttm) VALUES(?,?,?)",
            [(code, d, v) for d, v in rows],
        )
        conn.commit()
    finally:
        conn.close()


def get_snapshot(code: str) -> dict | None:
    """组装引擎所需快照(静态部分; price/mcap/pe 由调用方按 as-of 注入)。"""
    info = get_stock_info(code)
    if not info:
        return None
    conn = get_connection()
    try:
        divs = [
            {"fiscal_year": r["fiscal_year"], "dps": r["dps"], "announce_date": r["announce_date"], "ex_date": r["ex_date"]}
            for r in conn.execute("SELECT * FROM value_dividend WHERE code=? ORDER BY fiscal_year", (code,))
        ]
        roes = [
            {"fiscal_year": r["fiscal_year"], "roe": r["roe"], "disclose_date": r["disclose_date"]}
            for r in conn.execute("SELECT * FROM value_roe WHERE code=? ORDER BY fiscal_year", (code,))
        ]
    finally:
        conn.close()
    return {
        "code": code,
        "name": info.get("name"),
        "industry": info.get("industry"),
        "total_shares_yi": info.get("total_shares_yi"),
        "dividends": divs,
        "roes": roes,
    }


def save_pool(rebuild_date: str, pool: list[dict]) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM value_pool WHERE rebuild_date=?", (rebuild_date,))
        conn.executemany(
            "INSERT OR REPLACE INTO value_pool(rebuild_date, code, gates_json) VALUES(?,?,?)",
            [(rebuild_date, p["code"], json.dumps(p.get("gates", []), ensure_ascii=False)) for p in pool],
        )
        conn.commit()
    finally:
        conn.close()


def get_pool(rebuild_date: str) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM value_pool WHERE rebuild_date=? ORDER BY code", (rebuild_date,)).fetchall()
        return [{"code": r["code"], "gates": json.loads(r["gates_json"] or "[]")} for r in rows]
    finally:
        conn.close()


def latest_pool() -> tuple[str | None, list[dict]]:
    conn = get_connection()
    try:
        r = conn.execute("SELECT MAX(rebuild_date) d FROM value_pool").fetchone()
        d = r["d"] if r else None
        if not d:
            return None, []
        return d, get_pool(d)
    finally:
        conn.close()
