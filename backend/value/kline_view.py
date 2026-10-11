"""value_kline 的紧凑内存表示: numpy 底层 + dict 兼容只读视图。

背景: 原 _inputs 常驻 {code: {date: {8个价格字段}}} 的 dict-of-dict, 619 股 × ~2000 日
≈ 146 万个内层 dict + 每个 8  float 对象, 常驻约 465MB; 本机仅 1.6G 内存, 是 swap
抖动(接口时快时慢)的主因。

改造: 每股只存 (升序 date 列表[sys.intern 全局共享同一字符串对象] + (n,8) float64
numpy 数组[DB NULL→NaN]), 常驻降至约 90MB。经 collections.abc.Mapping 暴露
.get/[]/in/items/keys/values/迭代 的完全 dict **只读**语义, 使 run_backtest 与 api 层
消费代码零改动、回测结果 bit-level 不变:
  - __getitem__ 仍返回含 8 字段的普通 dict(NaN→None, 值为原生 float, 可直接 JSON 序列化);
  - 缺失日期 __getitem__ 抛 KeyError, 于是 Mapping.get 返回默认(None/{})与原 dict 一致;
  - __iter__ 按 date 升序, 与原 get_kline 的 ORDER BY date 一致。

只读: 不提供写操作(底层数据加载后不变); 需要 dict 写语义的场景不适用。
"""

from __future__ import annotations

import bisect
import sys
from collections.abc import Iterator, Mapping

import numpy as np

# 列顺序与 store.get_kline 的 SELECT(date,open,close,high,low,aopen,aclose,ahigh,alow)一致
FIELDS = ("open", "close", "high", "low", "aopen", "aclose", "ahigh", "alow")


def _f(v: float) -> float | None:
    """numpy float64 → 原生 float; NaN(DB NULL) → None。v!=v 即 NaN 判定。"""
    return None if v != v else float(v)


class KlineView(Mapping):
    """单只个股 K 线的紧凑只读视图, 行为等价于 {date: {字段: float | None}}。"""

    __slots__ = ("_dates", "_arr")

    def __init__(self, dates: list[str], arr: np.ndarray) -> None:
        self._dates = dates  # 升序 date 字符串(已 intern, 跨股共享同一对象)
        self._arr = arr  # shape (len(dates), 8) float64; NaN 表示该字段 DB NULL

    @classmethod
    def from_rows(cls, rows) -> KlineView:
        """rows: 升序的 (date, open, close, high, low, aopen, aclose, ahigh, alow) 序列。

        None 值经 numpy dtype=float64 转换自动成为 NaN(已验证无 warning);
        date 用 sys.intern 令 619 股的相同日期共享同一字符串对象, 避免百万份重复。
        """
        n = len(rows)
        if n == 0:
            return cls([], np.empty((0, len(FIELDS)), dtype=np.float64))
        dates = [sys.intern(str(r[0])) for r in rows]
        arr = np.array(
            [[r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8]] for r in rows],
            dtype=np.float64,
        )
        return cls(dates, arr)

    def __getitem__(self, date: str) -> dict:
        i = bisect.bisect_left(self._dates, date)
        if i >= len(self._dates) or self._dates[i] != date:
            raise KeyError(date)
        a = self._arr[i]
        return {
            "open": _f(a[0]),
            "close": _f(a[1]),
            "high": _f(a[2]),
            "low": _f(a[3]),
            "aopen": _f(a[4]),
            "aclose": _f(a[5]),
            "ahigh": _f(a[6]),
            "alow": _f(a[7]),
        }

    def __iter__(self) -> Iterator[str]:
        return iter(self._dates)

    def __len__(self) -> int:
        return len(self._dates)

    def __reduce__(self):
        """紧凑 pickle: 仅存 dates + numpy 数组。

        interned 日期字符串由 pickle 的 memo 跨 619 股共享(同一对象只序列化一次),
        numpy 数组按原始 buffer 存储 —— 落盘体积与加载内存均远小于 dict 版。
        """
        return (self.__class__, (self._dates, self._arr))
