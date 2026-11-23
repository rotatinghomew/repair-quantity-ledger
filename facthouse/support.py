"""实测支持范围的版本演化。

三种复测动作：
- full：全套复测，整体替代上一版的支持范围；
- partial：只修订部分实测，仅新版本覆盖到的米程被替换，其余依据原样保留；
- withdraw：撤回某条签认，不改变空间支持范围，撤回在覆盖层处理。

每个支持切片都记录当前背书版本与被替代的历史版本链，
保证后补/复测资料可以回查到原始报量与签认依据。
"""
from __future__ import annotations

from dataclasses import dataclass

from .intervals import Range, intersect, subtract_many
from .model import MeasurementVersion


@dataclass(frozen=True)
class SupportedRange:
    rng: Range
    backing_version: str
    lineage: tuple[str, ...]  # 被本版替代掉的历史实测版本


class MeasurementEvolution:
    def __init__(self, measurements: tuple[MeasurementVersion, ...]):
        self._versions = sorted(measurements, key=lambda mv: mv.version)
        self._states: dict[str, list[SupportedRange]] = {}
        self._build()

    def _build(self) -> None:
        current: list[SupportedRange] = []
        for mv in self._versions:
            if mv.mode == "withdraw":
                # 撤回签认不是重测，空间支持范围保持不变。
                self._states[mv.id] = list(current)
                continue
            if mv.mode == "full":
                lineage = tuple(sorted({sr.backing_version for sr in current}))
                # 保留各实测段边界：复测段的分界本身就是重划归属的证据，不可合并。
                current = [
                    SupportedRange(rng=rng, backing_version=mv.id, lineage=lineage)
                    for rng in mv.segments
                ]
            elif mv.mode == "partial":
                replaced = {
                    sr.backing_version
                    for sr in current
                    if any(intersect(sr.rng, new) for new in mv.segments)
                }
                kept: list[SupportedRange] = []
                for sr in current:
                    pieces = subtract_many(sr.rng, mv.segments)
                    for piece in pieces:
                        kept.append(
                            SupportedRange(
                                rng=piece,
                                backing_version=sr.backing_version,
                                lineage=sr.lineage,
                            )
                        )
                for rng in mv.segments:
                    kept.append(
                        SupportedRange(
                            rng=rng,
                            backing_version=mv.id,
                            lineage=tuple(sorted(replaced)),
                        )
                    )
                current = kept
            self._states[mv.id] = list(current)

    def support(self, version_id: str) -> list[SupportedRange]:
        if version_id not in self._states:
            raise KeyError(f"未知实测版本: {version_id}")
        return list(self._states[version_id])

    def latest(self) -> list[SupportedRange]:
        return self.support(self._versions[-1].id)

    @property
    def latest_version(self) -> MeasurementVersion:
        return self._versions[-1]

    def backing_at(self, rng: Range) -> dict[Range, tuple[str, Range]]:
        """返回某区间当前各切片的背书版本及其完整实测段。"""
        result: dict[Range, tuple[str, Range]] = {}
        for sr in self.latest():
            piece = intersect(rng, sr.rng)
            if piece is not None:
                result[piece] = (sr.backing_version, sr.rng)
        return result
