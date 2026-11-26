"""分件验收、领货与结算领域规则的单元测试（不启动 HTTP 服务）。"""

import copy
import unittest

from cooperative import (
    ACCEPTED,
    DECLINED,
    DISPUTED,
    PENDING_VERIFICATION,
    VERIFIED,
    CooperativeOrder,
    ServiceError,
    ingest,
    load_bundle,
)
from counterparty import FakeReceiptPort
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def base_register():
    register, _ = load_bundle(FIXTURES)
    return copy.deepcopy(register)


def build_order(**overrides):
    register = base_register()
    register.update(overrides)
    return CooperativeOrder(register)


def accept_pieces(order, *piece_ids):
    for piece_id in piece_ids:
        order.submit_for_review(piece_id)
        order.apply_review(piece_id, ACCEPTED)


def verify_handover(order, piece_id, collector, credential, port, point="pickup-east"):
    handover = order.record_handover(piece_id, collector, credential, point, port)
    order._sync_received_stage()
    return handover


class SampleRevisionTest(unittest.TestCase):
    def test_revision_only_reopens_affected_unconfirmed_pieces(self):
        order = build_order()
        order.submit_for_review("frame")
        order.apply_review("frame", ACCEPTED)
        order.submit_for_review("cloth")
        order.apply_review("cloth", DECLINED, reason="sample_color")

        reopened = order.apply_sample_revision("design-2", ["frame", "cloth", "stand"])

        # frame 已确认、stand 无样稿，都不重开；只有未确认且共用该样稿的 cloth。
        self.assertEqual(reopened, ["cloth"])
        frame = order.piece("frame")
        self.assertTrue(frame.confirmed)
        self.assertEqual(frame.sample, "design-1")
        cloth = order.piece("cloth")
        self.assertFalse(cloth.confirmed)
        self.assertEqual(cloth.sample, "design-2")
        self.assertEqual(cloth.stage, "material")
        self.assertIn("sample_reopened", cloth.blocker)

    def test_revision_does_not_touch_confirmed_standalone_piece(self):
        order = build_order()
        accept_pieces(order, "stand")
        reopened = order.apply_sample_revision("design-2", ["stand"])
        self.assertEqual(reopened, [])
        self.assertTrue(order.piece("stand").confirmed)

    def test_same_sample_is_not_a_real_impact(self):
        order = build_order()
        order.submit_for_review("cloth")
        reopened = order.apply_sample_revision("design-1", ["cloth"])
        self.assertEqual(reopened, [])
        self.assertEqual(order.piece("cloth").sample_history, ["design-1"])


class ReviewAndAssemblyTest(unittest.TestCase):
    def test_independently_accepted_piece_is_kept_when_dependency_declined(self):
        order = build_order()
        accept_pieces(order, "frame", "stand")
        order.submit_for_review("cloth")
        order.apply_review("cloth", DECLINED, reason="sample_color")

        frame = order.piece("frame")
        self.assertTrue(frame.confirmed)
        self.assertFalse(frame.returned)
        # 客人允许拆领且 frame 有领取人 buyer，可独立领货。
        self.assertEqual(order.pickup_eligibility("frame"), "independent")

    def test_wait_assembly_without_split_pickup_or_delegation(self):
        order = build_order(agreed_split_pickup=False)
        accept_pieces(order, "frame")
        self.assertEqual(order.pickup_eligibility("frame"), "wait_assembly")
        with self.assertRaises(ServiceError) as caught:
            order.record_handover("frame", "buyer", "h-1", "pickup-east", FakeReceiptPort())
        self.assertEqual(caught.exception.code, "assembly_blocked")

    def test_unconfirmed_dependency_blocks_assembly_pass(self):
        order = build_order()
        accept_pieces(order, "frame")
        with self.assertRaises(ServiceError) as caught:
            order.pass_assembly("display")
        self.assertEqual(caught.exception.code, "assembly_blocked")
        self.assertEqual(caught.exception.context["unconfirmed"], ["cloth"])

    def test_assembly_pass_unblocks_standalone_waiting_piece(self):
        order = build_order(agreed_split_pickup=False)
        accept_pieces(order, "frame")
        self.assertEqual(order.pickup_eligibility("frame"), "wait_assembly")
        accept_pieces(order, "cloth")
        order.pass_assembly("display")
        self.assertEqual(order.piece("frame").blocker, None)
        # 台账仍登记了 buyer 的授权，组合通过后即可独立领取。
        self.assertEqual(order.pickup_eligibility("frame"), "independent")


class ReworkMaterialAndPriceTest(unittest.TestCase):
    def test_rework_keeps_agreed_price(self):
        order = build_order()
        order.submit_for_review("cloth")
        order.apply_review("cloth", DECLINED, reason="sample_color")
        order.record_rework("cloth", reason="color")
        cloth = order.piece("cloth")
        self.assertEqual(cloth.agreed_cents, 12000)
        self.assertEqual(cloth.stage, "material")
        self.assertEqual(cloth.blocker, "rework:color")

    def test_material_substitution_keeps_price_and_records_material(self):
        order = build_order()
        order.substitute_material("stand", "recycled-hardwood")
        stand = order.piece("stand")
        self.assertEqual(stand.agreed_cents, 6000)
        self.assertEqual(stand.materials, ["recycled-hardwood"])

    def test_price_increase_requires_customer_consent(self):
        order = build_order()
        with self.assertRaises(ServiceError) as caught:
            order.request_price_change("stand", 7200)
        self.assertEqual(caught.exception.code, "price_change_needs_consent")
        self.assertEqual(order.piece("stand").agreed_cents, 6000)
        # 客人明确同意后可以调价；降价不设置门槛。
        order.request_price_change("stand", 7200, customer_consent=True)
        self.assertEqual(order.piece("stand").agreed_cents, 7200)
        order.request_price_change("stand", 5000)
        self.assertEqual(order.piece("stand").agreed_cents, 5000)


class SerialStockTest(unittest.TestCase):
    def test_cannot_take_base_confirmed_to_another_order(self):
        order = build_order()
        with self.assertRaises(ServiceError) as caught:
            order.allocate_serial("stand", "stand-r1")
        self.assertEqual(caught.exception.code, "serial_allocated_elsewhere")
        self.assertEqual(caught.exception.context["allocated_order"], "co-31")
        self.assertIsNone(order.piece("stand").serial)

    def test_free_serial_can_be_allocated_once(self):
        order = build_order()
        order.allocate_serial("stand", "stand-r2")
        self.assertEqual(order.piece("stand").serial, "stand-r2")
        other = build_order()
        other.allocate_serial("frame", "stand-r2")  # 另一订单的独立台账
        # 同一订单内不能重复分给两个分件。
        with self.assertRaises(ServiceError) as caught:
            order.allocate_serial("frame", "stand-r2")
        self.assertEqual(caught.exception.code, "serial_in_use")


class DelegationTest(unittest.TestCase):
    def test_register_cannot_grant_one_piece_to_two_collectors(self):
        register = base_register()
        register["pickup_delegations"].append({"collector": "proxy-9", "pieces": ["frame"]})
        with self.assertRaises(ServiceError) as caught:
            CooperativeOrder(register)
        self.assertEqual(caught.exception.code, "duplicate_delegation")

    def test_running_grant_prevents_double_handover(self):
        order = build_order()
        accept_pieces(order, "frame")
        order.record_handover("frame", "buyer", "h-1", "pickup-east", FakeReceiptPort("lost"))
        # 未确认收到前不能直接交给另一个领取人。
        with self.assertRaises(ServiceError) as caught:
            order.record_handover(
                "frame", "proxy-2", "h-2", "pickup-east", FakeReceiptPort()
            )
        self.assertEqual(caught.exception.code, "collector_not_delegated")

    def test_redelegation_does_not_reopen_pending_credential(self):
        order = build_order()
        accept_pieces(order, "frame")
        order.record_handover("frame", "buyer", "handover-31", "pickup-east", FakeReceiptPort("lost"))
        order.delegate("frame", "proxy-2")
        frame = order.piece("frame")
        # 拆换授权已留痕，但待核实凭据仍绑定原领取人，件也不会重新变成可交。
        self.assertEqual(order.effective_collector("frame"), "buyer")
        self.assertEqual(frame.handover.credential, "handover-31")
        self.assertEqual(frame.handover.state, PENDING_VERIFICATION)
        with self.assertRaises(ServiceError) as caught:
            order.record_handover(
                "frame", "proxy-2", "handover-31", "pickup-east", FakeReceiptPort()
            )
        self.assertEqual(caught.exception.code, "collector_not_delegated")
        with self.assertRaises(ServiceError) as caught:
            order.record_handover(
                "frame", "buyer", "other-credential", "pickup-east", FakeReceiptPort()
            )
        self.assertEqual(caught.exception.code, "handover_pending")

    def test_redelegation_takes_effect_once_handover_cleared(self):
        order = build_order()
        accept_pieces(order, "frame")
        order.record_handover("frame", "buyer", "h-1", "pickup-east", FakeReceiptPort())
        self.assertTrue(order.piece("frame").received)
        with self.assertRaises(ServiceError) as caught:
            order.delegate("frame", "proxy-2")
        self.assertEqual(caught.exception.code, "piece_already_received")


class HandoverAndCustodyTest(unittest.TestCase):
    def test_lost_reply_keeps_custody_duty_and_same_credential_reconciles(self):
        order = build_order()
        accept_pieces(order, "frame")
        lost = FakeReceiptPort("lost")
        handover = order.record_handover(
            "frame", "buyer", "handover-31", "pickup-east", lost
        )
        self.assertEqual(handover.state, PENDING_VERIFICATION)
        self.assertFalse(order.piece("frame").received)
        # 保管方视角必须能看到这条未解除的核对责任。
        custody = order.custody_view()
        self.assertEqual([row["credential"] for row in custody], ["handover-31"])
        # 同一张凭据重发：幂等，最终核实成功。
        retried = order.record_handover(
            "frame", "buyer", "handover-31", "pickup-east", lost
        )
        self.assertEqual(retried.state, VERIFIED)
        self.assertTrue(order.piece("frame").received)
        self.assertEqual(order.piece("frame").stage, "settlement")
        self.assertEqual(lost.requests[0]["reference"], "handover-31")

    def test_reconcile_with_wrong_credential_is_rejected(self):
        order = build_order()
        accept_pieces(order, "frame")
        lost = FakeReceiptPort("always_lost")
        order.record_handover("frame", "buyer", "handover-31", "pickup-east", lost)
        with self.assertRaises(ServiceError) as caught:
            order.reconcile_handover("another-credential", lost)
        self.assertEqual(caught.exception.code, "unknown_credential")
        with self.assertRaises(ServiceError) as caught:
            order.reconcile("another-credential", lost)
        self.assertEqual(caught.exception.code, "unknown_reference")

    def test_lookup_without_reply_keeps_pending_state(self):
        order = build_order()
        accept_pieces(order, "frame")
        silent = FakeReceiptPort("always_lost")
        order.record_handover("frame", "buyer", "h-1", "pickup-east", silent)
        self.assertFalse(order.reconcile_handover("h-1", silent))
        self.assertEqual(order.piece("frame").handover.state, PENDING_VERIFICATION)
        self.assertEqual(len(order.custody_view()), 1)

    def test_counterparty_says_not_received_marks_disputed(self):
        order = build_order()
        accept_pieces(order, "frame")
        port = FakeReceiptPort("not_received")
        order.record_handover("frame", "buyer", "h-1", "pickup-east", port)
        self.assertEqual(order.piece("frame").handover.state, DISPUTED)
        self.assertFalse(order.piece("frame").received)

    def test_handover_requires_confirmed_piece_and_delegation(self):
        order = build_order()
        port = FakeReceiptPort()
        with self.assertRaises(ServiceError) as caught:
            order.record_handover("stand", "buyer", "h-1", "pickup-east", port)
        self.assertEqual(caught.exception.code, "piece_not_confirmed")
        accept_pieces(order, "stand")
        with self.assertRaises(ServiceError) as caught:
            order.record_handover("stand", "stranger", "h-1", "pickup-east", port)
        self.assertEqual(caught.exception.code, "collector_not_delegated")


class SettlementTest(unittest.TestCase):
    def test_deposit_allocated_fifo_across_registered_pieces(self):
        order = build_order()
        allocation, unallocated = order.deposit_allocation()
        self.assertEqual(allocation, {"frame": 14000, "cloth": 0, "stand": 0})
        self.assertEqual(unallocated, 0)
        # stand 另有此前已付 6000，自身尾款为 0。
        self.assertEqual(order.piece_credits("stand"), 6000)
        self.assertEqual(order.releasable("stand"), 0)

    def test_unverified_delivery_blocks_payment_release(self):
        order = build_order()
        accept_pieces(order, "frame")
        order.record_handover(
            "frame", "buyer", "h-1", "pickup-east", FakeReceiptPort("always_lost")
        )
        self.assertEqual(order.releasable("frame"), 0)
        with self.assertRaises(ServiceError) as caught:
            order.release_payment("frame")
        self.assertEqual(caught.exception.code, "handover_unverified")
        payment = order.payment_view()
        self.assertEqual(payment["unverified_deliveries"][0]["credential"], "h-1")

    def test_release_tail_after_received(self):
        order = build_order()
        accept_pieces(order, "frame")
        verify_handover(order, "frame", "buyer", "h-1", FakeReceiptPort())
        # frame 约定 18000，订金冲抵 14000，尾款 4000。
        self.assertEqual(order.releasable("frame"), 4000)
        released = order.release_payment("frame")
        self.assertEqual(released, 4000)
        self.assertEqual(order.piece("frame").stage, "settled")
        self.assertEqual(order.releasable("frame"), 0)

    def test_change_case_refunds_unreceived_returned_piece(self):
        order = build_order()
        accept_pieces(order, "frame", "cloth", "stand")
        verify_handover(order, "frame", "buyer", "h-1", FakeReceiptPort())
        verify_handover(order, "stand", "buyer", "h-2", FakeReceiptPort())
        # cloth 未领回，退件：订金全在 frame 上，cloth 无已付款，不产生退款。
        case = order.open_change_case("change-1", ["cloth"], reason="color")
        cloth_line = next(line for line in case.lines if line["piece"] == "cloth")
        self.assertEqual(cloth_line["refundable_cents"], 0)
        self.assertFalse(cloth_line["physically_returned"])
        self.assertEqual(case.refund_cents, 0)
        self.assertEqual(case.payable_cents, 4000)  # frame 尾款仍欠
        self.assertTrue(order.piece("cloth").returned)

    def test_change_case_on_paid_piece_refunds_and_keeps_others_payable(self):
        order = build_order()
        accept_pieces(order, "frame", "stand")
        verify_handover(order, "frame", "buyer", "h-1", FakeReceiptPort())
        verify_handover(order, "stand", "buyer", "h-2", FakeReceiptPort())
        # 退 stand：其名下此前已付 6000 应退；frame 已签收保留，尾款 4000 轧差后净退 2000。
        case = order.open_change_case("change-2", ["stand"])
        stand_line = next(line for line in case.lines if line["piece"] == "stand")
        self.assertTrue(stand_line["physically_returned"])
        self.assertEqual(stand_line["refundable_cents"], 6000)
        frame_line = next(line for line in case.lines if line["piece"] == "frame")
        self.assertEqual(frame_line["payable_cents"], 4000)
        self.assertEqual(case.refund_cents, 2000)
        self.assertEqual(case.payable_cents, 0)

    def test_signed_and_unreturned_piece_keeps_payable_share(self):
        order = build_order()
        accept_pieces(order, "frame", "cloth", "stand")
        verify_handover(order, "frame", "buyer", "h-1", FakeReceiptPort())
        verify_handover(order, "cloth", "proxy-2", "h-2", FakeReceiptPort())
        # 退未签收的 stand；两件已签收实物的应付部分保留。
        case = order.open_change_case("change-3", ["stand"])
        payable = {line["piece"]: line["payable_cents"] for line in case.lines}
        self.assertEqual(payable["frame"], 4000)
        self.assertEqual(payable["cloth"], 12000)
        self.assertEqual(payable["stand"], 0)

    def test_released_tail_reduces_later_payable(self):
        order = build_order()
        accept_pieces(order, "frame", "stand")
        verify_handover(order, "frame", "buyer", "h-1", FakeReceiptPort())
        order.release_payment("frame")
        case = order.open_change_case("change-4", ["stand"])
        # stand 已付 6000 全退；frame 尾款已释放，无应付。
        self.assertEqual(case.refund_cents, 6000)
        self.assertEqual(case.payable_cents, 0)

    def test_refund_execution_and_pending_retry(self):
        class FlakyRefundPort(FakeReceiptPort):
            """首次请求断线，但编号已登记；随后可凭同一编号查清。"""

            def __init__(self):
                super().__init__("lost")
                self.lookup_enabled = False

            def request(self, reference, operation, recipient):
                result = super().request(reference, operation, recipient)
                if not self.lookup_enabled:
                    result.state = PENDING_VERIFICATION
                return result

            def lookup(self, reference):
                if not self.lookup_enabled:
                    return None
                return super().lookup(reference)

        order = build_order()
        accept_pieces(order, "stand")
        verify_handover(order, "stand", "buyer", "h-1", FakeReceiptPort())
        case = order.open_change_case("change-20", ["stand"])
        self.assertEqual(case.refund_cents, 6000)
        flaky = FlakyRefundPort()
        order.execute_refund("change-20", flaky)
        self.assertEqual(case.status, "refund_pending")
        self.assertIn(
            "change-20",
            [row["case"] for row in order.payment_view()["pending_refunds"]],
        )
        flaky.lookup_enabled = True
        self.assertTrue(order.reconcile_refund("change-20", flaky))
        self.assertEqual(case.status, "returned")
        self.assertIsNotNone(case.money_receipt)


class ViewTest(unittest.TestCase):
    def test_maker_view_locates_the_blocking_stage(self):
        order = build_order()
        order.submit_for_review("stand")
        order.apply_review("stand", ACCEPTED)
        rows = {row["piece"]: row for row in order.maker_view("wood-team")}
        self.assertEqual(set(rows), {"frame", "stand"})
        self.assertEqual(rows["frame"]["stage"], "material")
        self.assertEqual(rows["stand"]["stage"], "pickup")
        order.submit_for_review("frame")
        order.apply_review("frame", DECLINED, reason="crack")
        self.assertEqual(order.maker_view("wood-team")[0]["blocker"], "declined:crack")

    def test_payment_view_exposes_unverified_delivery(self):
        order = build_order()
        accept_pieces(order, "frame")
        order.record_handover(
            "frame", "buyer", "h-1", "pickup-east", FakeReceiptPort("always_lost")
        )
        view = order.payment_view()
        self.assertEqual(view["unverified_deliveries"][0]["piece"], "frame")
        self.assertEqual(view["releasable"], [])
        self.assertEqual(view["blocked"][0]["reason"], "pending_verification")


class FixtureIngestionTest(unittest.TestCase):
    def test_ingest_fixture_events_offline(self):
        register, records = load_bundle(FIXTURES)
        order = CooperativeOrder(register)
        ingest(order, records)  # 无端口：断线交接留待核实
        self.assertTrue(order.piece("frame").confirmed)
        self.assertTrue(order.piece("stand").confirmed)
        cloth = order.piece("cloth")
        self.assertFalse(cloth.confirmed)
        self.assertEqual(cloth.sample, "design-2")
        self.assertEqual(cloth.stage, "material")
        frame = order.piece("frame")
        self.assertEqual(frame.handover.credential, "handover-31")
        self.assertEqual(frame.handover.state, PENDING_VERIFICATION)
        self.assertEqual(len(order.custody_view()), 1)

    def test_ingest_with_fake_port_and_full_reconcile_flow(self):
        register, records = load_bundle(FIXTURES)
        order = CooperativeOrder(register)
        port = FakeReceiptPort("lost")
        ingest(order, records, port=port)
        # 断线：frame 仍待核实，尾款不能释放。
        self.assertEqual(order.payment_view()["unverified_deliveries"][0]["piece"], "frame")
        with self.assertRaises(ServiceError) as caught:
            order.release_payment("frame")
        self.assertEqual(caught.exception.code, "handover_unverified")
        # 同凭据核对成功后，frame 进入可结算，尾款 4000。
        self.assertTrue(order.reconcile("handover-31", port))
        self.assertTrue(order.piece("frame").received)
        self.assertEqual(order.release_payment("frame"), 4000)
        # 未确认的 cloth 不能交接。
        with self.assertRaises(ServiceError) as caught:
            order.record_handover(
                "cloth", "proxy-2", "h-9", "pickup-east", FakeReceiptPort()
            )
        self.assertEqual(caught.exception.code, "piece_not_confirmed")

    def test_statement_contains_order_level_totals(self):
        register, records = load_bundle(FIXTURES)
        order = CooperativeOrder(register)
        ingest(order, records)
        statement = order.statement()
        self.assertEqual(statement["order"], "co-17")
        self.assertEqual(statement["total_paid_cents"], 20000)
        pieces = {row["piece"]: row for row in statement["pieces"]}
        self.assertEqual(pieces["frame"]["deposit_share_cents"], 14000)
        self.assertEqual(pieces["stand"]["previous_paid_cents"], 6000)


if __name__ == "__main__":
    unittest.main()
