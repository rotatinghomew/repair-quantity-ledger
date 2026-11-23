"""验工事实仓业务规则测试。

覆盖：复测重划转段、局部修订、撤回签认、重叠争议与独立通过、
不同工作项分别计量、待批变更待认与获批转付、签认期/生效期双轴、
锁定表不可变、冲补分列不轧差、复跑幂等、证据双向追查。
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from facthouse import FactHouse  # noqa: E402
from facthouse.loader import build_facts, load_facts  # noqa: E402

DATA = ROOT / "data"


def scenario_house(name: str, prior_ledger=None) -> FactHouse:
    bundle = json.loads((DATA / "scenarios.json").read_text(encoding="utf-8"))
    return FactHouse(build_facts(bundle[name]), prior_ledger)


def by_key(house: FactHouse):
    return {p.key: p for p in house.ledger + house.held}


class MainFixtureTests(unittest.TestCase):
    def setUp(self):
        self.house = FactHouse(load_facts(DATA / "facts.json"))

    def test_locked_table_is_immutable_snapshot(self):
        tables = {t["period"]: t for t in self.house.payment_view()["payment_tables"]}
        locked = tables["2026-10"]
        self.assertTrue(locked["locked"])
        self.assertEqual(locked["total_amount"], 7500.0)
        self.assertEqual(
            {(r["item_id"], r["meters"], r["amount"]) for r in locked["rows"]},
            {("甲-屋脊-一期", 5.0, 6000.0), ("乙-屋脊-一期", 1.0, 1500.0)},
        )

    def test_reallocation_clawback_and_hold_are_split_lines(self):
        ledger = by_key(self.house)
        claw = next(p for p in self.house.ledger if p.direction == "clawback")
        self.assertEqual((claw.rng, claw.meters, claw.amount), ((4.0, 5.0), 1.0, -1200.0))
        self.assertEqual(claw.period, "2026-11")
        self.assertEqual(claw.source_item_id, "甲-屋脊-一期")
        held = next(p for p in self.house.held)
        self.assertEqual((held.meters, held.target_item_id), (1.0, "乙-屋脊-二期变更"))
        self.assertIsNone(held.amount)
        # 冲减与待认分列，未以两家分包轧差。
        self.assertEqual(
            sum(p.amount for p in self.house.ledger), -1200.0
        )

    def test_pending_change_does_not_pay_or_consume_future_quantity(self):
        view = self.house.payment_view()
        self.assertEqual(view["held_quantities"][0]["meters"], 1.0)
        self.assertNotIn(1680, [r.get("amount") for t in view["payment_tables"] for r in t["rows"]])

    def test_evidence_chain_links_report_cert_and_both_measurements(self):
        claw = next(p for p in self.house.ledger if p.direction == "clawback")
        ev = claw.evidence
        self.assertEqual(ev["certification_id"], "CERT-81")
        self.assertEqual(ev["report_id"], "R-81")
        self.assertEqual(ev["measurement_at_signing"], "V1")
        self.assertEqual(ev["measurement_current"], "V2")
        trace = self.house.trace(claw.key)
        self.assertTrue(trace["postings"])
        self.assertTrue(all(p["evidence"]["certification_id"] == "CERT-81" for p in trace["postings"]))

    def test_rerun_is_idempotent_and_keeps_original_booking_period(self):
        first = self.house.export()
        rerun = FactHouse(load_facts(DATA / "facts.json"), first["correction_ledger"])
        self.assertEqual(
            [p.key for p in rerun.ledger], [p.key for p in self.house.ledger]
        )
        self.assertEqual(
            [p.amount for p in rerun.ledger], [p.amount for p in self.house.ledger]
        )
        self.assertEqual(
            [p.period for p in rerun.ledger], [p.period for p in self.house.ledger]
        )

    def test_hint_reconciliation_has_no_issues(self):
        self.assertEqual(self.house.issues, [])


class PartialRevisionAndWithdrawalTests(unittest.TestCase):
    def test_partial_revision_keeps_unaffected_basis_and_withdrawal_claws_back(self):
        house = scenario_house("scenario_partial_revision_and_withdrawal")
        # 局部修订只动 10-14，甲 0-10 的签认不受影响，不产生更正。
        self.assertFalse(
            any(p.source_item_id == "甲-墙面-一期" for p in house.ledger)
        )
        claw = next(p for p in house.ledger if p.direction == "clawback")
        self.assertEqual(claw.source_item_id, "乙-墙面-一期")
        self.assertEqual(claw.meters, 4.0)
        self.assertEqual(claw.amount, -3600.0)
        self.assertIn("撤回", claw.reason)
        # 后补/撤回资料关联原始报量与签认。
        self.assertEqual(claw.evidence["report_id"], "RP-2")
        self.assertEqual(claw.evidence["certification_id"], "C-撤")


class OverlapDisputeTests(unittest.TestCase):
    def test_disputed_range_held_while_independent_ranges_pass(self):
        house = scenario_house("scenario_overlap_dispute_with_independent_pass")
        held_m = round(sum(p.meters for p in house.held), 6)
        self.assertEqual(held_m, 4.0)  # 6-8 两家重叠
        passing = house.export()["independent_passing_ranges_m"]
        self.assertEqual(
            passing,
            [{"start_m": 0.0, "end_m": 6.0}, {"start_m": 8.0, "end_m": 14.0}],
        )
        # 独立范围照常计价：甲 6m×500 + 乙 6m×520
        booked = sorted(p.amount for p in house.ledger)
        self.assertEqual(booked, [3000.0, 3120.0])
        self.assertTrue(
            all(
                "subcontractor_boundary_overlap" in p.evidence["reasons"]
                for p in house.held
            )
        )


class SeparableWorkItemTests(unittest.TestCase):
    def test_different_work_items_on_same_entity_are_separately_measurable(self):
        house = scenario_house("scenario_different_work_items_separately_measurable")
        self.assertEqual(house.held, [])
        amounts = sorted(p.amount for p in house.ledger)
        self.assertEqual(amounts, [6000.0, 7000.0])
        self.assertEqual(len(house.export()["disputes"]), 0)


class ApprovalTimelineTests(unittest.TestCase):
    def test_held_quantity_becomes_payable_after_approval_without_double_count(self):
        bundle = json.loads((DATA / "scenarios.json").read_text(encoding="utf-8"))
        name = "scenario_pending_change_later_approved"
        before = FactHouse(build_facts({**bundle[name], "as_of": "2026-11-20"}))
        held_before = next(p for p in before.held)
        self.assertEqual(held_before.meters, 1.0)
        # 用同一幂等键复跑：获批后待认转补入，按新价 1680 入账。
        prior = before.export()["correction_ledger"] + before.export()["held_quantities"]
        after = FactHouse(build_facts(bundle[name]), prior)
        add = next(
            p
            for p in after.ledger
            if p.direction == "add" and p.target_item_id == "乙-屋脊-二期变更"
        )
        self.assertEqual((add.meters, add.price_per_m, add.amount), (1.0, 1680.0, 1680.0))
        # 冲减只发生一次，复跑不重复冲。
        claws = [p for p in after.ledger if p.direction == "clawback"]
        self.assertEqual(len(claws), 1)
        self.assertEqual(claws[0].amount, -1200.0)
        # 待认量已转出，不再占用。
        self.assertFalse(any(p.key == held_before.key for p in after.held))


class EffectiveVsSigningDateTests(unittest.TestCase):
    def test_price_follows_signing_date_not_payment_period_alone(self):
        house = scenario_house("scenario_effective_date_vs_signing_date")
        priced = {(p.target_item_id, p.price_per_m, p.amount) for p in house.ledger}
        self.assertIn(("丙-地坪-旧价", 300.0, 1800.0), priced)  # 10-25 签认，新价未生效
        self.assertIn(("丙-地坪-新价", 330.0, 1320.0), priced)  # 11-08 签认，新价已生效


class LoaderGuardTests(unittest.TestCase):
    def test_signed_quantity_beyond_measurement_support_is_rejected(self):
        from facthouse.loader import FactValidationError

        payload = json.loads((DATA / "scenarios.json").read_text(encoding="utf-8"))[
            "scenario_effective_date_vs_signing_date"
        ]
        payload["certifications"][0]["signed_m"] = 99
        with self.assertRaises(ValueError):
            FactHouse(build_facts(payload))

    def test_dangling_reference_is_rejected(self):
        from facthouse.loader import FactValidationError

        payload = json.loads((DATA / "facts.json").read_text())
        payload["certifications"][0]["measurement"] = "NOPE"
        with self.assertRaises(FactValidationError):
            build_facts(payload)


if __name__ == "__main__":
    unittest.main()
