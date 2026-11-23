"""计价：合同/变更生效期与现场签认期分开裁决。

规则：
- 计价只采用签认时点已批准且已生效的合同项；待批变更挂待认（held），
  既不按新价付款，也不占用将来获批后可支付的量；
- 同一米程有多条已批价格线时，取生效日最新者（变更优先）；
- 无任何已批价格线覆盖的范围挂待认，等待合同裁决。
"""
from __future__ import annotations

from dataclasses import dataclass

from .intervals import Range, intersect, split_at
from .model import ContractItem, SourceFacts
from .coverage import CertSlice

PRICED = "priced"
HELD_PENDING_CHANGE = "held_pending_change"
HELD_NO_APPROVED_PRICE = "held_no_approved_price"


@dataclass
class PricedSlice:
    rng: Range
    length_m: float
    slice_ref: CertSlice
    item_id: str
    subcontractor: str
    work_item: str
    price_per_m: float | None
    amount: float | None
    status: str
    reason: str = ""
    pending_item_id: str = ""


def _is_effective_approved(item: ContractItem, signing_date: str, facts: SourceFacts) -> bool:
    if item.effective_from > signing_date:
        return False
    if item.approval == "approved":
        return True
    # 显式审批事件可把 fixture 里的 pending 项在审批日之后（且已生效）转为可用。
    return any(
        event.item_id == item.id and event.approved_at <= signing_date
        for event in facts.approvals
    )


def price_slice(
    sl: CertSlice, signing_date: str, facts: SourceFacts
) -> list[PricedSlice]:
    return _price(sl, signing_date, facts, reallocation=False, backing_ranges=set())


def price_reallocation(
    sl: CertSlice,
    decision_date: str,
    facts: SourceFacts,
    backing_ranges: set[Range],
) -> list[PricedSlice]:
    """转段去向计价：只有完整覆盖当前背书实测段的合同项才有归属资格。"""
    return _price(
        sl, decision_date, facts, reallocation=True, backing_ranges=backing_ranges
    )


def _price(
    sl: CertSlice,
    decision_date: str,
    facts: SourceFacts,
    reallocation: bool,
    backing_ranges: set[Range],
) -> list[PricedSlice]:
    boundaries = sorted({p for item in facts.contract_items for p in item.rng})
    pieces = split_at(sl.rng, boundaries)
    priced: list[PricedSlice] = []

    for piece in pieces:
        covering = [
            item
            for item in facts.contract_items
            if intersect(item.rng, piece) is not None
        ]
        if reallocation:
            # 转段场景下，归属候选必须完整包含背书该切片的实测段。
            covering = [
                item
                for item in covering
                if all(
                    item.start_m <= br[0] + 1e-9 and item.end_m >= br[1] - 1e-9
                    for br in backing_ranges
                )
            ]
        effective = [
            item for item in covering if _is_effective_approved(item, decision_date, facts)
        ]
        pending = [item for item in covering if item not in effective]
        length_m = round(piece[1] - piece[0], 6)

        if effective:
            # 优先采用签认/报量指定的合同项；同一范围有多条价格线时才取最新生效者。
            designated = [item for item in effective if item.id == sl.item_id]
            chosen = (
                designated[0]
                if designated
                else max(effective, key=lambda item: item.effective_from)
            )
            priced.append(
                PricedSlice(
                    rng=piece,
                    length_m=length_m,
                    slice_ref=sl,
                    item_id=chosen.id,
                    subcontractor=chosen.subcontractor,
                    work_item=chosen.work_item,
                    price_per_m=chosen.price_per_m,
                    amount=round(chosen.price_per_m * length_m, 2),
                    status=PRICED,
                )
            )
        elif pending:
            candidate = max(pending, key=lambda item: item.effective_from)
            priced.append(
                PricedSlice(
                    rng=piece,
                    length_m=length_m,
                    slice_ref=sl,
                    item_id="",
                    subcontractor=candidate.subcontractor,
                    work_item=candidate.work_item,
                    price_per_m=None,
                    amount=None,
                    status=HELD_PENDING_CHANGE,
                    reason=f"变更 {candidate.id} 尚未获批，保留待认量 {length_m}m",
                    pending_item_id=candidate.id,
                )
            )
        else:
            priced.append(
                PricedSlice(
                    rng=piece,
                    length_m=length_m,
                    slice_ref=sl,
                    item_id="",
                    subcontractor=sl.subcontractor,
                    work_item=sl.work_item,
                    price_per_m=None,
                    amount=None,
                    status=HELD_NO_APPROVED_PRICE,
                    reason="该范围无已批合同价格线",
                )
            )
    return priced
