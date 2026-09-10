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


# ---------------------------------------------------------------- 新鲜度巡检


def test_freshness_guard_fresh_skips(monkeypatch):
    monkeypatch.setattr(vf.store, "latest_kline_dates", lambda: {"600000": "2026-09-01"})
    monkeypatch.setattr(vf.store, "codes_missing_adj", lambda: [])
    monkeypatch.setattr(vf, "_existing_codes", lambda: ["600000"])
    monkeypatch.setattr("base.store.calendar_repo.get_trade_days", lambda end=None: ["2026-09-01"])
    out = vf.task_value_freshness_guard()
    assert out["fresh"] is True


def test_freshness_guard_stale_catches_up(monkeypatch):
    monkeypatch.setattr(vf.store, "latest_kline_dates", lambda: {"600000": "2026-08-28"})
    monkeypatch.setattr(vf.store, "codes_missing_adj", lambda: [])
    monkeypatch.setattr(vf, "_existing_codes", lambda: ["600000"])
    monkeypatch.setattr(
        "base.store.calendar_repo.get_trade_days", lambda end=None: ["2026-08-28", "2026-08-31"]
    )
    called = {}

    def fake_update():
        called["x"] = 1
        return {"todo": 1, "updated": 1, "failed": 0}

    monkeypatch.setattr(vf, "task_value_daily_update", fake_update)
    out = vf.task_value_freshness_guard()
    assert out["fresh"] is False and called


def test_freshness_guard_prioritizes_dual_price_migration(monkeypatch):
    """存在未双价迁移个股时: 即使日期新鲜也不走普通追更(防止旧口径叠加新行),
    而是同步启动迁移任务并接入 job_manager; 迁移运行中则静默跳过。"""
    from base.scheduler.job_manager import job_manager

    monkeypatch.setattr(vf.store, "latest_kline_dates", lambda: {"600000": "2026-09-01"})
    monkeypatch.setattr(vf.store, "codes_missing_adj", lambda: ["600000", "601088"])
    ran = {}
    import base.scheduler.job_registry as jr

    monkeypatch.setitem(jr.JOB_FNS, "value_upgrade_kline", lambda p: ran.setdefault("x", 1) and {"todo": 2, "upgraded": 2, "failed": 0} or {"todo": 2, "upgraded": 2, "failed": 0})
    out = vf.task_value_freshness_guard()
    assert out["fresh"] is False and out["migrating"] == 2 and ran
    st = job_manager.get(out["job_id"])
    assert st is not None and st.status == "success"
    # 迁移运行中(占位 active job) → 重入被拦
    jid = job_manager.submit("value_upgrade_kline", {}, True)
    job_manager.mark_running(jid)
    out2 = vf.task_value_freshness_guard()
    assert out2["job_id"] is None and out2["migrating"] == 2
    job_manager.finish(jid, {"todo": 0}, None)


def test_quarterly_rebuild_runs_on_first_trading_day(monkeypatch):
    monkeypatch.setattr(vf.store, "latest_kline_dates", lambda: {"600000": "2026-07-01"})
    monkeypatch.setattr("base.store.calendar_repo.get_trade_days", lambda s, e: ["2026-07-01"])
    monkeypatch.setattr(vf, "job_value_rebuild_pool", lambda p: {"codes": 1, "pool": 1, "as_of": "2026-07-01"})
    out = vf.task_value_quarterly_rebuild()
    assert not out.get("skipped") and out["pool"] == 1


# ---------------------------------------------------------------- 个股独立全仓视角


def test_single_stock_full_position_round_trip():
    """slot_divisor=1: 单票独立视角全仓买入, 移动止盈回落卖往返全部落袋(含成本扣减)。"""
    closes = dip_closes()
    closes[150] = 1.05  # 激活
    closes[151] = 1.00  # 回落卖
    opens = {121: 0.90}
    res = run_backtest(
        DATES, {"A": kline_from(closes, opens)}, {"A": snap("A")}, pe_const(["A"]),
        idle_yield_pct=0.0, slot_divisor=1,
    )
    buys = [t for t in res["trade_log"] if t["action"] == "BUY"]
    sells = [t for t in res["trade_log"] if t["action"] == "SELL"]
    assert len(buys) == 1 and len(sells) == 1
    assert abs(buys[0]["amount"] - 1.0) < 1e-6  # 全仓 = 全部权益(现金支出口径)
    assert sells[0]["name"] == "A"  # 交易记录带名称
    # 精确: 1.00/0.90 × (1-佣金2.5bp) × (1-佣金-印花税10bp, 2018) - 1 ≈ 10.94%
    assert abs(res["metrics"]["total_return_pct"] - 10.94) < 0.02
