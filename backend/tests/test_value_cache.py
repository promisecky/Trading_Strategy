"""价值回归缓存层测试: 失效注册中心 + 回测状态落盘往返与指纹失效。"""

from __future__ import annotations

import os

os.environ["VALUE_NO_PREWARM"] = "1"  # 禁用后台预热线程, 避免测试内重跑全量回测

import value.api as vapi
from value import cache


def test_register_and_invalidate():
    calls = []
    cache.register(lambda: calls.append(1))
    before = len(calls)
    cache.invalidate_all()
    assert len(calls) == before + 1


def test_state_persist_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(vapi, "STATE_FILE", tmp_path / "value_state.json")
    st = {"ready": True, "metrics": {"annual_return_pct": 1.0}, "history": [], "trade_log": [], "final": {}, "signals": {}}
    vapi._persist_state(("2018-01-01", 2.0), st)
    hit = vapi._load_persisted()
    assert hit is not None
    key, loaded = hit
    assert key == ("2018-01-01", 2.0)
    assert loaded["metrics"]["annual_return_pct"] == 1.0


def test_state_not_persisted_when_not_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(vapi, "STATE_FILE", tmp_path / "value_state.json")
    vapi._persist_state(("2018-01-01", 2.0), {"ready": False})
    assert vapi._load_persisted() is None


def test_state_fingerprint_mismatch_invalidates(tmp_path, monkeypatch):
    monkeypatch.setattr(vapi, "STATE_FILE", tmp_path / "value_state.json")
    st = {"ready": True, "metrics": {}, "history": [], "trade_log": [], "final": {}, "signals": {}}
    vapi._persist_state(("2018-01-01", 2.0), st)
    monkeypatch.setattr(vapi, "_fingerprint", lambda: "changed|0|")
    assert vapi._load_persisted() is None


def test_inputs_disk_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(vapi, "INPUTS_FILE", tmp_path / "inputs.pkl")
    data = {"codes": ["600000"], "kline": {}, "dates": [], "snapshots": {}, "pe": {}}
    vapi._save_inputs_disk(data)
    assert vapi._load_inputs_disk() == data
    monkeypatch.setattr(vapi, "_fingerprint", lambda: "changed|0|")
    assert vapi._load_inputs_disk() is None
