"""通过真实本地联调入口验证回执协议与领域服务的衔接。"""

import copy
import json
from threading import Thread
import unittest

from cooperative import PENDING_VERIFICATION, VERIFIED, CooperativeOrder, load_bundle
from counterparty import HttpReceiptPort
from fixture_gateway import FixtureGateway
from http.client import RemoteDisconnected
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def accept_pieces(order, *piece_ids):
    for piece_id in piece_ids:
        order.submit_for_review(piece_id)
        from cooperative import ACCEPTED

        order.apply_review(piece_id, ACCEPTED)


class GatewayIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.server = FixtureGateway(0)
        self.addCleanup(self.server.server_close)
        thread = Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)
        self.port = HttpReceiptPort("127.0.0.1", self.server.server_port)
        register, _ = load_bundle(FIXTURES)
        self.register = register

    def build_order(self):
        return CooperativeOrder(copy.deepcopy(self.register))

    def test_lost_reply_then_same_credential_reconciles_via_http(self):
        order = self.build_order()
        accept_pieces(order, "frame")
        handover = order.record_handover(
            "frame", "buyer", "handover-31", "pickup-east", self.port
        )
        self.assertEqual(handover.state, PENDING_VERIFICATION)
        self.assertFalse(order.piece("frame").received)
        # 桩里该条 delivery=lost_reply：首次 POST 已被对方记录后断线。
        self.assertEqual(self.port.lookup("handover-31")["receipt"], "handover-31")
        # 同编号同内容重试成功，且不会在对方产生第二笔记录。
        self.assertTrue(order.reconcile_handover("handover-31", self.port))
        self.assertEqual(order.piece("frame").handover.state, VERIFIED)
        self.assertTrue(order.piece("frame").received)
        self.assertEqual(order.piece("frame").stage, "settlement")
        # 同编号同内容在合作方一侧是幂等的：直接重发 POST 仍拿回同一回执。
        from counterparty import ReceiptResult

        again = self.port.request("handover-31", "take-piece", "pickup-east")
        self.assertIsInstance(again, ReceiptResult)
        self.assertEqual(again.receipt["receipt"], "handover-31")

    def test_reused_reference_with_different_content_raises(self):
        # 先在合作方占用 handover-31。
        order = self.build_order()
        accept_pieces(order, "frame")
        order.record_handover("frame", "buyer", "handover-31", "pickup-east", self.port)
        other = self.build_order()
        accept_pieces(other, "frame")
        from cooperative import ServiceError

        with self.assertRaises(ServiceError) as caught:
            other.record_handover(
                "frame", "buyer", "handover-31", "other-point", self.port
            )
        self.assertEqual(caught.exception.code, "reference_reused")

    def test_refund_receipt_flow_via_http(self):
        order = self.build_order()
        accept_pieces(order, "stand")
        order.record_handover("stand", "buyer", "h-stand", "pickup-east", self.port)
        order._sync_received_stage()
        case = order.open_change_case("change-20", ["stand"])
        self.assertEqual(case.refund_cents, 6000)
        executed = order.execute_refund("change-20", self.port)
        self.assertEqual(executed.status, "returned")
        self.assertEqual(executed.money_receipt["status"], "returned")
        self.assertEqual(self.port.lookup("change-20")["receipt"], "money-7")

    def test_full_fixture_ingestion_against_gateway(self):
        register, records = load_bundle(FIXTURES)
        order = CooperativeOrder(register)
        for event in sorted(records, key=lambda e: e["cursor"]):
            order.apply_event(event, port=self.port)
        frame = order.piece("frame")
        self.assertEqual(frame.handover.state, PENDING_VERIFICATION)
        self.assertEqual(len(order.custody_view()), 1)
        order.reconcile("handover-31", self.port)
        self.assertTrue(frame.received)
        self.assertEqual(order.release_payment("frame"), 4000)
        summary = order.statement()
        self.assertEqual(
            json.loads(json.dumps(summary, ensure_ascii=False))["order"], "co-17"
        )


if __name__ == "__main__":
    unittest.main()
