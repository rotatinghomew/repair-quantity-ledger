"""事实仓领域模型：从原始 JSON 资料构建的强类型事实。

设计要点：
- 合同项同时携带分包、可支付工作项、米程范围、单价与生效/审批信息；
- 实测是带版本的事件流，支持全套替代、局部修订、撤回三种动作；
- 报量是师傅侧事实，签认才是付款入口，两者分别建实体并保留引用链；
- 锁定付款表是不可变快照，加载后只参与核对，不参与重算覆盖。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .periods import Period


@dataclass(frozen=True)
class ContractItem:
    id: str
    subcontractor: str
    work_item: str          # 可支付工作项（同一项的重叠量不得重复结算）
    start_m: float
    end_m: float
    price_per_m: float
    effective_from: str     # 合同/变更生效日
    approval: str           # approved / pending
    replaces: str | None = None  # 变更获批后替换的原合同项

    @property
    def rng(self) -> tuple[float, float]:
        return (self.start_m, self.end_m)

    @property
    def payable_key(self) -> tuple[str, str]:
        return (self.subcontractor, self.work_item)


@dataclass(frozen=True)
class MeasurementVersion:
    id: str
    version: int
    surveyed_at: str
    mode: str               # full / partial / withdraw
    supersedes: str | None
    segments: tuple[tuple[float, float], ...]
    withdraws_cert: str | None = None
    note: str = ""


@dataclass(frozen=True)
class ForemanReport:
    id: str
    item_id: str
    work_item: str
    unit: str
    start_m: float
    end_m: float
    quantity: float
    reported_at: str
    certified_by: tuple[str, ...] = field(default_factory=tuple)

    @property
    def rng(self) -> tuple[float, float]:
        return (self.start_m, self.end_m)


@dataclass(frozen=True)
class Certification:
    id: str
    measurement_id: str
    report_id: str | None
    item_id: str
    signed_m: float
    period: Period
    signed_at: str | None = None  # 缺省取付款期期末日

    def signing_date(self, period_end: str) -> str:
        return self.signed_at or period_end


@dataclass(frozen=True)
class LockedPaymentRow:
    period: Period
    item_id: str
    m: float
    amount: float
    price_per_m: float | None = None
    certification_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ApprovalEvent:
    item_id: str
    approved_at: str
    replaces: str | None = None


@dataclass(frozen=True)
class SourceFacts:
    contract_items: tuple[ContractItem, ...]
    measurements: tuple[MeasurementVersion, ...]
    reports: tuple[ForemanReport, ...]
    certifications: tuple[Certification, ...]
    locked_payment: tuple[LockedPaymentRow, ...]
    approvals: tuple[ApprovalEvent, ...]
    corrections_hint: tuple[dict, ...]
    as_of: str | None = None

    def item(self, item_id: str) -> ContractItem:
        for item in self.contract_items:
            if item.id == item_id:
                return item
        raise KeyError(f"合同项不存在: {item_id}")

    def measurement(self, measurement_id: str) -> MeasurementVersion:
        for mv in self.measurements:
            if mv.id == measurement_id:
                return mv
        raise KeyError(f"实测版本不存在: {measurement_id}")

    @property
    def locked_periods(self) -> set[Period]:
        return {row.period for row in self.locked_payment}
