"""报量/签认到米程区间的覆盖映射与重叠裁决。

每条签认沿"合同部位 ∩ 当时实测支持范围"落位，按从左到右分配签认量；
之后再用最新实测支持范围逐片复核，得到受支持/失支持/撤回/争议四种状态。

同一可支付工作项（分包+工作项）的重叠量直接标记为重复计量争议；
不同工作项或不同合同项的重叠保留各自范围，由计价层按合同分别处理。
无争议的独立范围始终照常通过。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .intervals import Range, intersect, merge, split_at, subtract_many
from .model import Certification, SourceFacts
from .support import MeasurementEvolution

SUPPORTED = "supported"
UNSUPPORTED = "unsupported"
WITHDRAWN = "withdrawn"
DISPUTED = "disputed"
REALLOCATED = "reallocated"

DISPUTE_DUPLICATE = "same_payable_item_overlap"
DISPUTE_BOUNDARY = "subcontractor_boundary_overlap"
REASON_BOUNDARY_MOVED = "survey_boundary_reallocated"


@dataclass
class CertSlice:
    rng: Range
    cert: Certification
    item_id: str
    subcontractor: str
    work_item: str
    status: str
    reasons: tuple[str, ...] = ()
    backing_at_signing: str = ""
    current_backing: str | None = None
    lineage: tuple[str, ...] = field(default_factory=tuple)

    @property
    def length_m(self) -> float:
        return round(self.rng[1] - self.rng[0], 6)

    @property
    def payable_key(self) -> tuple[str, str]:
        return (self.subcontractor, self.work_item)


def _allocate_signed_range(
    cert: Certification, item_rng: Range, supported: list[Range], signed_m: float
) -> Range:
    """签认量沿合同部位与当时实测支持范围的交集，自左向右落位。"""
    available = merge(
        piece
        for rng in supported
        if (piece := intersect(item_rng, rng)) is not None
    )
    total = round(sum(b - a for a, b in available), 6)
    if signed_m > total + 1e-6:
        raise ValueError(
            f"签认 {cert.id} 的签认量 {signed_m}m 超出当时实测可支持量 {total}m"
        )
    remaining = signed_m
    chosen: list[Range] = []
    for rng in available:
        if remaining <= 1e-9:
            break
        take = min(remaining, rng[1] - rng[0])
        chosen.append((rng[0], rng[0] + take))
        remaining -= take
    merged = merge(chosen)
    if len(merged) != 1:
        # 跨片签认保留为多片时由调用方逐片展开；此处返回总区间再切分。
        return (merged[0][0], merged[-1][1])
    return merged[0]


def build_coverage(
    facts: SourceFacts, evolution: MeasurementEvolution
) -> list[CertSlice]:
    withdrawn = {
        mv.withdraws_cert
        for mv in facts.measurements
        if mv.mode == "withdraw" and mv.withdraws_cert
    }

    slices: list[CertSlice] = []
    for cert in facts.certifications:
        item = facts.item(cert.item_id)
        support_at_signing = evolution.support(cert.measurement_id)
        supported_ranges = [sr.rng for sr in support_at_signing]
        full_rng = _allocate_signed_range(
            cert, item.rng, supported_ranges, cert.signed_m
        )
        item_boundaries = sorted(
            {p for it in facts.contract_items for p in (it.start_m, it.end_m)}
        )
        pieces = split_at(
            full_rng,
            sorted({p for r in supported_ranges for p in r} | set(item_boundaries)),
        )
        for piece in pieces:
            backing = next(
                (
                    sr.backing_version
                    for sr in support_at_signing
                    if intersect(sr.rng, piece) is not None
                ),
                cert.measurement_id,
            )
            lineage = next(
                (
                    sr.lineage
                    for sr in support_at_signing
                    if intersect(sr.rng, piece) is not None
                ),
                (),
            )
            if cert.id in withdrawn:
                slices.append(
                    CertSlice(
                        rng=piece,
                        cert=cert,
                        item_id=item.id,
                        subcontractor=item.subcontractor,
                        work_item=item.work_item,
                        status=WITHDRAWN,
                        reasons=("certification_withdrawn",),
                        backing_at_signing=backing,
                        current_backing=None,
                        lineage=lineage,
                    )
                )
                continue
            current = evolution.backing_at(piece)
            if not current:
                status, reasons, current_backing = (
                    UNSUPPORTED,
                    ("measurement_support_removed",),
                    None,
                )
            elif _is_reallocated(piece, item.rng, current):
                # 当前实测段虽仍覆盖该米程，但背书段已落在原合同部位之外：
                # 复测重划了分界线，原签认依据不再覆盖此范围。
                status, reasons, current_backing = (
                    REALLOCATED,
                    (REASON_BOUNDARY_MOVED,),
                    next(iter(current.values()))[0],
                )
            else:
                status, reasons, current_backing = (
                    SUPPORTED,
                    (),
                    next(iter(current.values()))[0],
                )
            slices.append(
                CertSlice(
                    rng=piece,
                    cert=cert,
                    item_id=item.id,
                    subcontractor=item.subcontractor,
                    work_item=item.work_item,
                    status=status,
                    reasons=reasons,
                    backing_at_signing=backing,
                    current_backing=current_backing,
                    lineage=lineage,
                )
            )

    _mark_overlap_disputes(slices, facts)
    return sorted(slices, key=lambda s: (s.rng[0], s.rng[1], s.cert.id))


def _is_reallocated(
    piece: Range,
    item_rng: Range,
    current_backing: dict[Range, tuple[str, Range]],
    threshold: float = 0.5,
) -> bool:
    """当前背书实测段若主体已移到原合同部位之外，判定为边界重划。"""
    for _, (_, backing_rng) in current_backing.items():
        inside = intersect(backing_rng, item_rng)
        inside_len = 0.0 if inside is None else inside[1] - inside[0]
        backing_len = backing_rng[1] - backing_rng[0]
        if backing_len > 0 and inside_len / backing_len < threshold:
            return True
    return False


def _mark_overlap_disputes(slices: list[CertSlice], facts: SourceFacts) -> None:
    replacements = {
        item.id for item in facts.contract_items if item.replaces is not None
    }
    replaced_targets = {
        item.replaces for item in facts.contract_items if item.replaces is not None
    }
    live = [s for s in slices if s.status in {SUPPORTED, DISPUTED, REALLOCATED}]
    for i, left in enumerate(live):
        for right in live[i + 1 :]:
            overlap = intersect(left.rng, right.rng)
            if overlap is None or left.cert.id == right.cert.id:
                continue
            if left.work_item != right.work_item:
                continue  # 不同工序按合同可分别计量
            pair = {left.item_id, right.item_id}
            if pair & replacements and pair & replaced_targets:
                continue  # 同一合同的变更替换链，归属已由合同裁决
            reason = (
                DISPUTE_DUPLICATE
                if left.payable_key == right.payable_key
                else DISPUTE_BOUNDARY
            )
            for holder in (left, right):
                if reason not in holder.reasons:
                    holder.reasons = (*holder.reasons, reason)
                holder.status = DISPUTED


def report_certification_state(facts: SourceFacts, slices: list[CertSlice]) -> list[dict]:
    """报量视角：每条报量当前被签认覆盖了多少、哪些仍未进入付款依据。"""
    by_report: dict[str, list[CertSlice]] = {}
    for sl in slices:
        if sl.cert.report_id:
            by_report.setdefault(sl.cert.report_id, []).append(sl)

    result: list[dict] = []
    for report in facts.reports:
        covered = by_report.get(report.id, [])
        certified_m = round(sum(s.length_m for s in covered if s.status != WITHDRAWN), 6)
        withdrawn_m = round(sum(s.length_m for s in covered if s.status == WITHDRAWN), 6)
        result.append(
            {
                "report_id": report.id,
                "item_id": report.item_id,
                "reported_m": report.quantity,
                "certified_m": certified_m,
                "withdrawn_m": withdrawn_m,
                "uncertified_m": round(report.quantity - certified_m, 6),
                "cert_ids": sorted({s.cert.id for s in covered}),
                "payable_eligible": report.quantity - certified_m <= 1e-9,
            }
        )
    return result


def independent_passing_ranges(slices: list[CertSlice]) -> list[Range]:
    """无争议且受支持的独立范围，允许照常通过。"""
    passing = [s.rng for s in slices if s.status == SUPPORTED]
    disputed = merge(s.rng for s in slices if s.status == DISPUTED)
    return merge(
        piece
        for rng in passing
        for piece in subtract_many(rng, disputed)
    )
