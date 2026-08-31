"""价值回归策略存储层 — value_* 表读写(与 ETF 表完全隔离)。"""

from __future__ import annotations

import json

from base.store.database import get_connection


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
    conn = get_connection()
    try:
        conn.execute("DELETE FROM value_dividend WHERE code=?", (code,))
        conn.executemany(
            "INSERT OR REPLACE INTO value_dividend(code, fiscal_year, dps, announce_date, ex_date) VALUES(?,?,?,?,?)",
            [(code, r["fiscal_year"], r["dps"], r["announce_date"], r["ex_date"]) for r in rows],
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


def replace_kline(code: str, rows: list[tuple[str, float, float, float, float]]) -> None:
    """rows: (date, open, close, high, low)。"""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM value_kline WHERE code=?", (code,))
        conn.executemany(
            "INSERT OR REPLACE INTO value_kline(code, date, open, close, high, low) VALUES(?,?,?,?,?,?)",
            [(code, d, o, c, h, l) for d, o, c, h, l in rows],
        )
        conn.commit()
    finally:
        conn.close()


def get_kline(code: str) -> dict[str, dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT date, open, close, high, low FROM value_kline WHERE code=? ORDER BY date", (code,)
        ).fetchall()
        return {
            r["date"]: {"open": r["open"], "close": r["close"], "high": r["high"], "low": r["low"]}
            for r in rows
        }
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


def latest_kline_dates() -> dict[str, str]:
    """每只股票的最新K线日(增量抓取的落后判定基准)。"""
    conn = get_connection()
    try:
        return {
            r["code"]: r["d"] for r in conn.execute("SELECT code, MAX(date) d FROM value_kline GROUP BY code")
        }
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
