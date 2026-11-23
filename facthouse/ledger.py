"""验工事实仓引擎：付款表、更正台账与证据追查。

输出三类结果：
1. payment_tables：按付款期的付款表。锁定表是不可变快照，
   重算只做核对不覆盖；未锁期由当前事实重新生成。
2. correction_ledger：更正造成的转段/转分包/价差，在第一个可入账期
   按实际受影响合同项逐行列冲减与补入，禁止两家分包轧差，
   并以稳定幂等键保证复跑不会重复冲。
3. held_quantities：待认量（待批变更/争议归属），不付款也不占用未来量。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from dataclasses import replace

from .coverage import (
    DISPUTED,
    REALLOCATED,
    SUPPORTED,
    UNSUPPORTED,
    WITHDRAWN,
    CertSlice,
    build_coverage,
    independent_passing_ranges,
    report_certification_state,
)
from .intervals import Range
from .loader import SourceFacts, load_facts
from .periods import Period, first_bookable_period
from .pricing import price_reallocation, price_slice
from .support import MeasurementEvolution

MONEY = Decimal("0.01")


def money(value: float) -> float:
    return float(Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP))


def period_of_date(date_text: str) -> Period:
    year_s, month_s, _ = date_text.split("-")
    return Period(int(year_s), int(month_s))


def _idem_key(parts: tuple[str, ...]) -> str:
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]
    return f"ADJ-{digest}"


@dataclass
class Posting:
    key: str
    period: str
    direction: str          # original / clawback / add / held
    status: str             # booked / held
    source_item_id: str
    target_item_id: str
    subcontractor: str
    work_item: str
    rng: Range
    meters: float
    price_per_m: float | None
    amount: float | None
    reason: str
    evidence: dict
    first_seen_version: str

    def to_dict(self) -> dict:
        data = asdict(self)
        data["range_m"] = {"start_m": self.rng[0], "end_m": self.rng[1]}
        data.pop("rng")
        return data


@dataclass
class ReconciliationIssue:
    kind: str
    detail: str
    expected: object | None = None
    actual: object | None = None


def _point_in_time(facts: SourceFacts) -> SourceFacts:
    """按 as_of 截止日过滤实测版本与审批事件，支持同一资料跑两个时点。"""
    if not facts.as_of:
        return facts
    cutoff = facts.as_of
    return replace(
        facts,
        measurements=tuple(mv for mv in facts.measurements if mv.surveyed_at <= cutoff),
        certifications=tuple(
            cert
            for cert in facts.certifications
            if cert.signing_date(cert.period.end_date()) <= cutoff
        ),
        approvals=tuple(event for event in facts.approvals if event.approved_at <= cutoff),
        corrections_hint=tuple(
            hint for hint in facts.corrections_hint if hint.get("measurement", "")
        ),
    )


class FactHouse:
    def __init__(self, facts: SourceFacts, prior_ledger: list[dict] | None = None):
        self.facts = _point_in_time(facts)
        self.evolution = MeasurementEvolution(self.facts.measurements)
        self.slices = build_coverage(facts, self.evolution)
        self._prior = {row["key"]: row for row in (prior_ledger or [])}
        self._prior_keys = set(self._prior)
        self.issues: list[ReconciliationIssue] = []
        self.ledger: list[Posting] = []
        self.held: list[Posting] = []
        self.payment_tables: dict[str, dict] = {}
        self._build()

    # ------------------------------------------------------------------ #
    @classmethod
    def from_paths(
        cls, facts_path: str | Path, ledger_path: str | Path | None = None
    ) -> "FactHouse":
        prior = None
        if ledger_path and Path(ledger_path).exists():
            prior = json.loads(Path(ledger_path).read_text(encoding="utf-8")).get(
                "correction_ledger", []
            )
        return cls(load_facts(facts_path), prior)

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        self._build_locked_tables()
        self._build_current_lines()
        self._build_corrections()
        self._reconcile_hints()

    def _signing_date(self, sl: CertSlice) -> str:
        return sl.cert.signing_date(sl.cert.period.end_date())

    def _build_locked_tables(self) -> None:
        for row in self.facts.locked_payment:
            period = str(row.period)
            table = self.payment_tables.setdefault(
                period,
                {"period": period, "locked": True, "rows": [], "total_amount": 0.0},
            )
            table["rows"].append(
                {
                    "item_id": row.item_id,
                    "subcontractor": self.facts.item(row.item_id).subcontractor,
                    "meters": row.m,
                    "price_per_m": row.price_per_m,
                    "amount": row.amount,
                    "certification_ids": list(row.certification_ids),
                    "evidence": {"snapshot": "locked_payment", "period": period},
                }
            )
            table["total_amount"] = money(table["total_amount"] + row.amount)
        self._check_locked_reconciliation()

    def _check_locked_reconciliation(self) -> None:
        for row in self.facts.locked_payment:
            item = self.facts.item(row.item_id)
            expected_meters = round(
                sum(
                    sl.length_m
                    for sl in self.slices
                    if sl.cert.item_id == row.item_id
                    and str(sl.cert.period) == str(row.period)
                ),
                6,
            )
            if expected_meters and abs(expected_meters - row.m) > 1e-6:
                self.issues.append(
                    ReconciliationIssue(
                        kind="locked_meter_drift",
                        detail=f"锁定表 {row.period}/{row.item_id} 米数与签认切片不符（锁定值仍保留）",
                        expected=expected_meters,
                        actual=row.m,
                    )
                )
            if row.price_per_m is not None and abs(
                money(row.price_per_m * row.m) - row.amount
            ) > 0.011:
                self.issues.append(
                    ReconciliationIssue(
                        kind="locked_amount_mismatch",
                        detail=f"锁定表 {row.period}/{row.item_id} 金额≠米数×单价",
                        expected=money(row.price_per_m * row.m),
                        actual=row.amount,
                    )
                )

    def _build_current_lines(self) -> None:
        """未锁付款期的原始签认行，按当前事实重新计价。"""
        for sl in self.slices:
            if str(sl.cert.period) in {str(p) for p in self.facts.locked_periods}:
                continue
            if sl.status in {WITHDRAWN, UNSUPPORTED, REALLOCATED}:
                continue
            if sl.status == DISPUTED:
                self._emit_dispute_hold(sl)
                continue
            for priced in price_slice(sl, self._signing_date(sl), self.facts):
                self._emit_original(priced)

    def _emit_dispute_hold(self, sl: CertSlice) -> None:
        """归属无裁决的重叠范围保留争议，不付款；独立范围不受影响照常通过。"""
        key = _idem_key(
            ("dispute", sl.cert.id, sl.item_id, f"{sl.rng[0]}", f"{sl.rng[1]}")
        )
        posting = Posting(
            key=key,
            period=str(sl.cert.period),
            direction="held",
            status="held",
            source_item_id=sl.item_id,
            target_item_id=sl.item_id,
            subcontractor=sl.subcontractor,
            work_item=sl.work_item,
            rng=sl.rng,
            meters=sl.length_m,
            price_per_m=None,
            amount=None,
            reason=f"争议范围保留，待归属裁决：{','.join(sl.reasons)}",
            evidence=self._evidence(sl),
            first_seen_version=sl.current_backing or sl.backing_at_signing,
        )
        self._register(posting)

    def _emit_original(self, priced) -> None:
        key = _idem_key(
            (
                "original",
                priced.slice_ref.cert.id,
                f"{priced.rng[0]}",
                f"{priced.rng[1]}",
                priced.item_id or "held",
            )
        )
        posting = Posting(
            key=key,
            period=str(priced.slice_ref.cert.period),
            direction="original",
            status="booked" if priced.status == "priced" else "held",
            source_item_id=priced.item_id,
            target_item_id=priced.item_id,
            subcontractor=priced.subcontractor,
            work_item=priced.work_item,
            rng=priced.rng,
            meters=priced.length_m,
            price_per_m=priced.price_per_m,
            amount=priced.amount,
            reason=priced.reason or "签认覆盖且实测支持，按生效合同计价",
            evidence=self._evidence(priced.slice_ref),
            first_seen_version=priced.slice_ref.cert.measurement_id,
        )
        (self.ledger if posting.status == "booked" else self.held).append(posting)

    # ------------------------------------------------------------------ #
    def _build_corrections(self) -> None:
        latest = self.evolution.latest_version
        book_period = str(
            first_bookable_period(period_of_date(latest.surveyed_at), self.facts.locked_periods)
        )
        for sl in self.slices:
            if str(sl.cert.period) not in {str(p) for p in self.facts.locked_periods}:
                continue
            if sl.status == SUPPORTED:
                continue
            if sl.status in {UNSUPPORTED, DISPUTED, REALLOCATED}:
                self._emit_clawback(sl, book_period, latest.id)
                self._emit_destination(sl, book_period, latest.id)
            elif sl.status == WITHDRAWN:
                self._emit_clawback(sl, book_period, latest.id, reason="签认被监理撤回")

    def _emit_clawback(
        self, sl: CertSlice, book_period: str, version_id: str, reason: str | None = None
    ) -> None:
        item = self.facts.item(sl.item_id)
        amount = money(item.price_per_m * sl.length_m)
        key = _idem_key(
            ("clawback", sl.cert.id, sl.item_id, f"{sl.rng[0]}", f"{sl.rng[1]}")
        )
        posting = Posting(
            key=key,
            period=book_period,
            direction="clawback",
            status="booked",
            source_item_id=item.id,
            target_item_id="",
            subcontractor=item.subcontractor,
            work_item=item.work_item,
            rng=sl.rng,
            meters=sl.length_m,
            price_per_m=item.price_per_m,
            amount=-amount,
            reason=reason or (
                "复测重划边界，原签认范围失去实测支持"
                if sl.status == UNSUPPORTED
                else (
                    "复测重划分界线，背书实测段已移至原合同部位之外，原依据转段冲减"
                    if sl.status == REALLOCATED
                    else f"范围归属争议挂起，先按原依据冲减：{','.join(sl.reasons)}"
                )
            ),
            evidence=self._evidence(sl),
            first_seen_version=version_id,
        )
        self._register(posting)

    def _emit_destination(self, sl: CertSlice, book_period: str, version_id: str) -> None:
        """受影响量的去向：按复测确认时点选生效合同价，有则补入，否则待认挂起。

        注意计价时点用复测日而非原签认日：转段后的归属合同在复测时才确定，
        待批变更即便空间覆盖也不得提前按新价付款。
        """
        survey_date = self.evolution.latest_version.surveyed_at
        # 去向计价在"复测确认"与"归属合同获批"两件事都成立后才可入账，
        # 因此决策时点取两者较晚者；未获批前自然落入待认。
        approval_dates = [
            event.approved_at
            for event in self.facts.approvals
        ]
        decision_date = max([survey_date, *approval_dates])
        backing_ranges = {br for _, (_, br) in self.evolution.backing_at(sl.rng).items()}
        priced_pieces = price_reallocation(sl, decision_date, self.facts, backing_ranges)
        for priced in priced_pieces:
            key = _idem_key(
                (
                    "add" if priced.status == "priced" else "held",
                    sl.cert.id,
                    priced.pending_item_id or priced.item_id or "noprice",
                    f"{priced.rng[0]}",
                    f"{priced.rng[1]}",
                )
            )
            if priced.status == "priced":
                posting = Posting(
                    key=key,
                    period=book_period,
                    direction="add",
                    status="booked",
                    source_item_id=sl.item_id,
                    target_item_id=priced.item_id,
                    subcontractor=priced.subcontractor,
                    work_item=priced.work_item,
                    rng=priced.rng,
                    meters=priced.length_m,
                    price_per_m=priced.price_per_m,
                    amount=priced.amount,
                    reason="复测确认归属后按生效合同项补入（与冲减分列，不轧差）",
                    evidence=self._evidence(sl),
                    first_seen_version=version_id,
                )
            else:
                posting = Posting(
                    key=key,
                    period=book_period,
                    direction="held",
                    status="held",
                    source_item_id=sl.item_id,
                    target_item_id=priced.pending_item_id,
                    subcontractor=priced.subcontractor,
                    work_item=priced.work_item,
                    rng=priced.rng,
                    meters=priced.length_m,
                    price_per_m=None,
                    amount=None,
                    reason=priced.reason,
                    evidence=self._evidence(sl),
                    first_seen_version=version_id,
                )
            self._register(posting)

    def _register(self, posting: Posting) -> None:
        """幂等注册：已入账键复跑不产生新冲补；待认可随审批转为入账。"""
        prior = self._prior.get(posting.key)
        if prior is not None:
            # 保持首次入账期，绝不二次冲减。
            posting.period = prior["period"]
            if prior["status"] == "booked":
                posting.status = "booked"
                posting.amount = prior["amount"]
        # 同一键此前为待认、现已满足计价条件：待认量转出，按实际受影响合同项补入。
        target = self.ledger if posting.status == "booked" else self.held
        target.append(posting)

    # ------------------------------------------------------------------ #
    def _evidence(self, sl: CertSlice) -> dict:
        return {
            "certification_id": sl.cert.id,
            "report_id": sl.cert.report_id,
            "measurement_at_signing": sl.backing_at_signing,
            "measurement_current": sl.current_backing,
            "superseded_lineage": list(sl.lineage),
            "contract_item": sl.item_id,
            "range_m": {"start_m": sl.rng[0], "end_m": sl.rng[1]},
            "reasons": list(sl.reasons),
        }

    def _reconcile_hints(self) -> None:
        """与资料中的更正提示逐字段对账，防止引擎口径漂移。"""
        for hint in self.facts.corrections_hint:
            claw = next(
                (
                    p
                    for p in self.ledger
                    if p.direction == "clawback"
                    and p.meters == abs(float(hint["pending_change_m"]))
                ),
                None,
            )
            if claw is None or abs(claw.amount - float(hint["pending_change_amount"])) > 0.011:
                self.issues.append(
                    ReconciliationIssue(
                        kind="hint_clawback_mismatch",
                        detail=f"更正提示冲减量未复现: {hint}",
                        expected=hint["pending_change_amount"],
                        actual=None if claw is None else claw.amount,
                    )
                )
            held = next(
                (p for p in self.held if p.meters == float(hint["pending_add_m"])), None
            )
            if held is None:
                self.issues.append(
                    ReconciliationIssue(
                        kind="hint_hold_mismatch",
                        detail=f"更正提示待认量未复现: {hint}",
                        expected=hint["pending_add_m"],
                        actual=None,
                    )
                )

    # ------------------------------------------------------------------ #
    def payment_view(self) -> dict:
        """支付岗视图：准备付款金额、争议原因、历次调整。"""
        tables: dict[str, dict] = {}
        for period, table in self.payment_tables.items():
            tables[period] = {
                "period": period,
                "locked": table["locked"],
                "total_amount": table["total_amount"],
                "rows": table["rows"],
            }
        open_periods = sorted({p.period for p in self.ledger if p.status == "booked"})
        for period in open_periods:
            rows = [p.to_dict() for p in self.ledger if p.period == period and p.status == "booked"]
            tables.setdefault(
                period, {"period": period, "locked": False, "rows": [], "total_amount": 0.0}
            )
            tables[period]["rows"].extend(rows)
            tables[period]["total_amount"] = money(
                tables[period]["total_amount"] + sum(r["amount"] for r in rows)
            )
        return {
            "payment_tables": [tables[p] for p in sorted(tables)],
            "disputes": [
                {
                    "range_m": {"start_m": sl.rng[0], "end_m": sl.rng[1]},
                    "certification_id": sl.cert.id,
                    "item_id": sl.item_id,
                    "reasons": list(sl.reasons),
                }
                for sl in self.slices
                if sl.status == DISPUTED
            ],
            "held_quantities": [p.to_dict() for p in self.held],
            "reconciliation_issues": [asdict(issue) for issue in self.issues],
        }

    def trace(self, key_or_cert: str) -> dict:
        """从任一笔差额或签认双向追查两边各自使用的验工依据。"""
        postings = [
            p
            for p in self.ledger + self.held
            if p.key == key_or_cert
            or p.evidence["certification_id"] == key_or_cert
        ]
        related_certs = {p.evidence["certification_id"] for p in postings}
        return {
            "query": key_or_cert,
            "postings": [p.to_dict() for p in postings],
            "cert_slices": [
                {
                    "certification_id": sl.cert.id,
                    "range_m": {"start_m": sl.rng[0], "end_m": sl.rng[1]},
                    "status": sl.status,
                    "evidence": self._evidence(sl),
                }
                for sl in self.slices
                if sl.cert.id in related_certs or sl.cert.id == key_or_cert
            ],
        }

    def export(self) -> dict:
        return {
            "schema_version": 1,
            "reports": report_certification_state(self.facts, self.slices),
            "independent_passing_ranges_m": [
                {"start_m": a, "end_m": b} for a, b in independent_passing_ranges(self.slices)
            ],
            "correction_ledger": [p.to_dict() for p in self.ledger],
            "held_quantities": [p.to_dict() for p in self.held],
            **self.payment_view(),
        }

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.export(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
