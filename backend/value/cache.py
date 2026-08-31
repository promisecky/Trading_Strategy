"""价值回归缓存中枢 — 跨模块缓存失效注册表。

fetch 层任务完成后需让 api 层缓存失效, 但 api↔fetch 存在间接循环依赖
(经 job_registry), 故以零依赖的注册中心解耦: api 注册失效函数,
fetch 任务完成时调用 invalidate_all()。
"""

from __future__ import annotations

from typing import Callable

_INVALIDATORS: list[Callable[[], None]] = []


def register(fn: Callable[[], None]) -> None:
    _INVALIDATORS.append(fn)


def invalidate_all() -> None:
    for fn in _INVALIDATORS:
        fn()
