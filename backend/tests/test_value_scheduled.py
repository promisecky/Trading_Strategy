"""价值回归定时任务对抗性测试: 季度重建门条件 + 个股独立全仓回测。"""

from __future__ import annotations

import value.fetch as vf
from value.backtest import run_backtest

from tests.test_value_backtest import DATES, dip_closes, kline_from, pe_const, snap


# ---------------------------------------------------------------- 季度重建门


def test_quarterly_rebuild_skips_non_quarter_month(monkeypatch):
    monkeypatch.setattr(vf.store, "latest_kline_dates", lambda: {"600000": "2026-08-28"})
    out = vf.task_value_quarterly_rebuild()
    assert out["skipped"] and "非季度月" in out["reason"]


def test_quarterly_rebuild_skips_when_no_data(monkeypatch):
    monkeypatch.setattr(vf.store, "latest_kline_dates", lambda: {})
    out = vf.task_value_quarterly_rebuild()
    assert out["skipped"]


def test_quarterly_rebuild_skips_non_first_trading_day(monkeypatch):
    monkeypatch.setattr(vf.store, "latest_kline_dates", lambda: {"600000": "2026-07-02"})
    monkeypatch.setattr("base.store.calendar_repo.get_trade_days", lambda s, e: ["2026-07-01", "2026-07-02"])
    out = vf.task_value_quarterly_rebuild()
    assert out["skipped"] and "首个交易日" in out["reason"]


def test_quarterly_rebuild_runs_on_first_trading_day(monkeypatch):
    monkeypatch.setattr(vf.store, "latest_kline_dates", lambda: {"600000": "2026-07-01"})
    monkeypatch.setattr("base.store.calendar_repo.get_trade_days", lambda s, e: ["2026-07-01"])
    monkeypatch.setattr(vf, "job_value_rebuild_pool", lambda p: {"codes": 1, "pool": 1, "as_of": "2026-07-01"})
    out = vf.task_value_quarterly_rebuild()
    assert not out.get("skipped") and out["pool"] == 1


# ---------------------------------------------------------------- 个股独立全仓视角


def test_single_stock_full_position_round_trip():
    """slot_divisor=1: 单票独立视角全仓买入, +11.1% 往返全部落袋(对比组合 5 槽仅 +2.2%)。"""
    closes = dip_closes()
    closes[150] = 1.00  # 触发止盈
    opens = {121: 0.90}
    res = run_backtest(
        DATES, {"A": kline_from(closes, opens)}, {"A": snap("A")}, pe_const(["A"]),
        idle_yield_pct=0.0, slot_divisor=1,
    )
    buys = [t for t in res["trade_log"] if t["action"] == "BUY"]
    sells = [t for t in res["trade_log"] if t["action"] == "SELL"]
    assert len(buys) == 1 and len(sells) == 1
    assert abs(buys[0]["amount"] - 1.0) < 1e-6  # 全仓 = 全部权益
    assert sells[0]["name"] == "A"  # 交易记录带名称
    assert abs(res["metrics"]["total_return_pct"] - 11.11) < 0.15  # 1.0/0.9-1
