"""资料加载与引用完整性校验。

加载阶段只做结构与引用校验，不做业务裁决。任何断引用、量纲不符、
区间非法都会抛 FactValidationError，避免脏数据进入事实仓。
"""
from __future__ import annotations

import json
from pathlib import Path

from .intervals import is_valid, length
from .model import (
    ApprovalEvent,
    Certification,
    ContractItem,
    ForemanReport,
    LockedPaymentRow,
    MeasurementVersion,
    SourceFacts,
)
from .periods import Period


class FactValidationError(ValueError):
    pass


def _segments(raw_segments: list[dict], where: str) -> tuple[tuple[float, float], ...]:
    parsed: list[tuple[float, float]] = []
    for index, seg in enumerate(raw_segments):
        start_m = float(seg["start_m"])
        end_m = float(seg["end_m"])
        rng = (start_m, end_m)
        if not is_valid(rng):
            raise FactValidationError(f"{where} 第 {index} 段区间非法: {rng}")
        if "length_m" in seg and abs(float(seg["length_m"]) - length(rng)) > 1e-6:
            raise FactValidationError(
                f"{where} 第 {index} 段长度与起终点不符: {seg['length_m']} != {length(rng)}"
            )
        parsed.append(rng)
    return tuple(parsed)


def load_facts(path: str | Path) -> SourceFacts:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return build_facts(payload)


def build_facts(payload: dict) -> SourceFacts:

    items: list[ContractItem] = []
    for raw in payload.get("contract_items", []):
        subcontractor = raw.get("subcontractor") or raw["id"].split("-", 1)[0]
        work_item = raw.get("work_item") or raw["id"].split("-")[1]
        items.append(
            ContractItem(
                id=raw["id"],
                subcontractor=subcontractor,
                work_item=work_item,
                start_m=float(raw["start_m"]),
                end_m=float(raw["end_m"]),
                price_per_m=float(raw["price_per_m"]),
                effective_from=raw["effective_from"],
                approval=raw.get("approval", "approved"),
                replaces=raw.get("replaces"),
            )
        )
    item_ids = {item.id for item in items}

    measurements: list[MeasurementVersion] = []
    measurement_ids: set[str] = set()
    for raw in payload.get("measurements", []):
        mv = MeasurementVersion(
            id=raw["id"],
            version=int(raw["version"]),
            surveyed_at=raw["surveyed_at"],
            mode=raw.get("mode", "full"),
            supersedes=raw.get("supersedes"),
            segments=_segments(raw.get("segments", []), f"实测 {raw['id']}"),
            withdraws_cert=raw.get("withdraws_cert"),
            note=raw.get("note", ""),
        )
        if mv.mode not in {"full", "partial", "withdraw"}:
            raise FactValidationError(f"实测 {mv.id} 的 mode 非法: {mv.mode}")
        measurements.append(mv)
        measurement_ids.add(mv.id)
    for mv in measurements:
        if mv.supersedes and mv.supersedes not in measurement_ids:
            raise FactValidationError(f"实测 {mv.id} 指向不存在的替代对象 {mv.supersedes}")

    reports: list[ForemanReport] = []
    report_ids: set[str] = set()
    for raw in payload.get("reports", []):
        if raw["item"] not in item_ids:
            raise FactValidationError(f"报量 {raw['id']} 引用了不存在的合同项 {raw['item']}")
        rng = (float(raw["start_m"]), float(raw["end_m"]))
        if not is_valid(rng):
            raise FactValidationError(f"报量 {raw['id']} 区间非法: {rng}")
        if abs(float(raw.get("quantity", length(rng))) - length(rng)) > 1e-6:
            raise FactValidationError(f"报量 {raw['id']} 数量与区间长度不符")
        report = ForemanReport(
            id=raw["id"],
            item_id=raw["item"],
            work_item=raw.get("work_item") or raw["item"].split("-")[1],
            unit=raw.get("unit", "m"),
            start_m=rng[0],
            end_m=rng[1],
            quantity=float(raw.get("quantity", length(rng))),
            reported_at=raw["reported_at"],
            certified_by=tuple(raw.get("certified_by", [])),
        )
        reports.append(report)
        report_ids.add(report.id)

    certifications: list[Certification] = []
    cert_ids: set[str] = set()
    for raw in payload.get("certifications", []):
        if raw["measurement"] not in measurement_ids:
            raise FactValidationError(f"签认 {raw['id']} 引用了不存在的实测 {raw['measurement']}")
        if raw["item"] not in item_ids:
            raise FactValidationError(f"签认 {raw['id']} 引用了不存在的合同项 {raw['item']}")
        report_id = raw.get("report")
        if report_id and report_id not in report_ids:
            raise FactValidationError(f"签认 {raw['id']} 引用了不存在的报量 {report_id}")
        if raw["signed_m"] <= 0:
            raise FactValidationError(f"签认 {raw['id']} 数量必须为正")
        cert = Certification(
            id=raw["id"],
            measurement_id=raw["measurement"],
            report_id=report_id,
            item_id=raw["item"],
            signed_m=float(raw["signed_m"]),
            period=Period.parse(raw["period"]),
            signed_at=raw.get("signed_at"),
        )
        certifications.append(cert)
        cert_ids.add(cert.id)

    locked: list[LockedPaymentRow] = []
    for raw in payload.get("locked_payment", []):
        if raw["item"] not in item_ids:
            raise FactValidationError(f"锁定付款表引用了不存在的合同项 {raw['item']}")
        locked.append(
            LockedPaymentRow(
                period=Period.parse(raw["period"]),
                item_id=raw["item"],
                m=float(raw["m"]),
                amount=float(raw["amount"]),
                price_per_m=float(raw["price_per_m"]) if "price_per_m" in raw else None,
                certification_ids=tuple(raw.get("certifications", [])),
            )
        )
    for row in locked:
        for cert_id in row.certification_ids:
            if cert_id not in cert_ids:
                raise FactValidationError(f"锁定表 {row.period}/{row.item_id} 引用了不存在的签认 {cert_id}")

    approvals = [
        ApprovalEvent(
            item_id=raw["item"],
            approved_at=raw["approved_at"],
            replaces=raw.get("replaces"),
        )
        for raw in payload.get("approvals", [])
    ]
    for event in approvals:
        if event.item_id not in item_ids:
            raise FactValidationError(f"审批事件引用了不存在的合同项 {event.item_id}")

    return SourceFacts(
        contract_items=tuple(items),
        measurements=tuple(measurements),
        reports=tuple(reports),
        certifications=tuple(certifications),
        locked_payment=tuple(locked),
        approvals=tuple(approvals),
        corrections_hint=tuple(payload.get("corrections", [])),
        as_of=payload.get("as_of"),
    )
