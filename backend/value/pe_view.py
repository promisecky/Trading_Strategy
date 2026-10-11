"""value_pe 的紧凑内存表示: numpy 底层 + dict 兼容只读视图(值为 PE float)。

背景: 原 _inputs 的 pe 为 {code: {date: pe_ttm}}, 619 股共约 114 万个 {date: float}
entry, 常驻约 124MB(dict 槽 + 每个 float 对象)。与 kline 同属 1.6G 小内存机的负担。

改造: 每股只存 (升序 date 列表[sys.intern 全局共享] + (n,) float64 numpy 数组[NULL→NaN]),
常驻降至约 18MB。经 collections.abc.Mapping 暴露 .get/[]/in/迭代 的完全 dict **只读**
语义, 消费方零改动:
  - __getitem__(date) → 原生 float(NaN→None); 缺失日期抛 KeyError, 于是 Mapping.get
    返回默认(None/{})与原 dict 一致;
  - 支持 pe_map[key] 直接下标(fetch._pe_at、api.value_pool 用到);
  - __iter__ 按 date 升序; api.value_pool 的 `[k for k in pe_map if k<=last]` + max(prior)
    只取值不依赖迭代顺序, 故升序安全。

只读: 不提供写操作(底层数据加载后不变)。
"""

from __future__ import annotations

import bisect
import sys
from collections.abc import Iterator, Mapping

import numpy as np


class PeView(Mapping):
    """单只个股 PE(TTM) 时序的紧凑只读视图, 行为等价于 {date: float | None}。"""

    __slots__ = ("_dates", "_arr")

    def __init__(self, dates: list[str], arr: np.ndarray) -> None:
        self._dates = dates  # 升序 date 字符串(已 intern, 跨股共享同一对象)
        self._arr = arr  # shape (len(dates),) float64; NaN 表示 pe_ttm 为 DB NULL

    @classmethod
    def from_rows(cls, rows) -> PeView:
        """rows: 升序的 (date, pe_ttm) 序列。None 经 dtype=float64 自动成为 NaN。"""
        n = len(rows)
        if n == 0:
            return cls([], np.empty((0,), dtype=np.float64))
        dates = [sys.intern(str(r[0])) for r in rows]
        arr = np.array([r[1] for r in rows], dtype=np.float64)
        return cls(dates, arr)

    def __getitem__(self, date: str) -> float | None:
        i = bisect.bisect_left(self._dates, date)
        if i >= len(self._dates) or self._dates[i] != date:
            raise KeyError(date)
        v = self._arr[i]
        return None if v != v else float(v)  # NaN(DB NULL) → None

    def __iter__(self) -> Iterator[str]:
        return iter(self._dates)

    def __len__(self) -> int:
        return len(self._dates)

    def __reduce__(self):
        """紧凑 pickle: 仅存 dates + numpy 数组(interned 日期由 memo 跨股共享)。"""
        return (self.__class__, (self._dates, self._arr))
