"""半开米程区间 [start, end) 的纯函数运算。

事实仓里所有"部位"都落在一维米程轴上。区间运算只依赖标准库，
保证复测重划边界、部分修订、重叠争议都可以精确到米程切片。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence

EPS = 1e-9

# 区间统一表示为 (start, end) 元组，半开区间。
Range = tuple[float, float]


def length(rng: Range) -> float:
    return round(rng[1] - rng[0], 6)


def is_valid(rng: Range) -> bool:
    return rng[1] - rng[0] > EPS


def intersect(a: Range, b: Range) -> Range | None:
    lo = max(a[0], b[0])
    hi = min(a[1], b[1])
    if hi - lo > EPS:
        return (lo, hi)
    return None


def subtract(a: Range, b: Range) -> list[Range]:
    """从区间 a 中挖掉区间 b，返回剩余的规范区间列表。"""
    cut = intersect(a, b)
    if cut is None:
        return [a]
    pieces: list[Range] = []
    if cut[0] - a[0] > EPS:
        pieces.append((a[0], cut[0]))
    if a[1] - cut[1] > EPS:
        pieces.append((cut[1], a[1]))
    return pieces


def subtract_many(rng: Range, cuts: Iterable[Range]) -> list[Range]:
    remaining = [rng]
    for cut in cuts:
        nxt: list[Range] = []
        for piece in remaining:
            nxt.extend(subtract(piece, cut))
        remaining = nxt
    return [r for r in remaining if is_valid(r)]


def merge(ranges: Iterable[Range]) -> list[Range]:
    """合并相交/相接区间，输出按起点排序的规范列表。"""
    ordered = sorted((r for r in ranges if is_valid(r)), key=lambda r: r[0])
    merged: list[Range] = []
    for rng in ordered:
        if merged and rng[0] <= merged[-1][1] + EPS:
            merged[-1] = (merged[-1][0], max(merged[-1][1], rng[1]))
        else:
            merged.append((rng[0], rng[1]))
    return merged


def split_at(rng: Range, boundaries: Sequence[float]) -> list[Range]:
    """按给定分界点把区间切成小片，分界点落在区间外会被忽略。"""
    points = sorted({p for p in boundaries if rng[0] < p < rng[1]})
    pieces: list[Range] = []
    cursor = rng[0]
    for point in points:
        if point - cursor > EPS:
            pieces.append((cursor, point))
        cursor = point
    pieces.append((cursor, rng[1]))
    return [r for r in pieces if is_valid(r)]


def split_by(rng: Range, others: Iterable[Range]) -> list[Range]:
    """用另一组区间的所有端点把 rng 切片，便于逐片裁决归属。"""
    boundaries = {p for other in others for p in other}
    return split_at(rng, sorted(boundaries))


def total_length(ranges: Iterable[Range]) -> float:
    return round(sum(length(r) for r in ranges), 6)
