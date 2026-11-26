"""合作方回执端口：取件点交接回执与资金退款回执。

* ``FakeReceiptPort`` 供单元测试模拟“正常回执 / 断线 / 事后可查”。
* ``HttpReceiptPort`` 对接 ``fixture_gateway.py`` 提供的本地联调入口，
  遵守“相同编号和内容可重试、编号他用返回 409、断线保留首记”的协议。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from http.client import HTTPConnection, RemoteDisconnected
from typing import Optional

from cooperative import PENDING_VERIFICATION, VERIFIED, ServiceError


@dataclass
class ReceiptResult:
    reference: str
    state: str  # verified / pending_verification
    receipt: Optional[dict] = None


class FakeReceiptPort:
    """可编程的回执桩。

    * ``lost``：首次请求断线，编号仍被记录，之后可凭同一编号查清。
    * ``always_lost``：一直查不到，用于验证保管方责任不解除。
    * ``not_received``：合作方回执称未收到实物，分件进入争议状态。
    """

    def __init__(self, behavior: str = "ok"):
        if behavior not in {"ok", "lost", "always_lost", "not_received"}:
            raise ValueError(f"未知桩行为：{behavior}")
        self.behavior = behavior
        self.requests: list[dict] = []
        self._booked: dict[str, dict] = {}

    def request(self, reference: str, operation: str, recipient: str) -> ReceiptResult:
        previous = self._booked.get(reference)
        if previous is not None:
            if (previous["operation"], previous["recipient"]) != (operation, recipient):
                raise ServiceError(
                    "reference_reused",
                    f"编号 {reference} 已被 {previous['operation']} 占用",
                )
            return ReceiptResult(reference, VERIFIED, previous["receipt"])

        self.requests.append(
            {"reference": reference, "operation": operation, "recipient": recipient}
        )
        if self.behavior == "always_lost":
            return ReceiptResult(reference, PENDING_VERIFICATION)
        if self.behavior == "not_received":
            receipt = {"receipt": reference, "received": False}
            self._booked[reference] = {
                "operation": operation,
                "recipient": recipient,
                "receipt": receipt,
            }
            return ReceiptResult(reference, VERIFIED, receipt)

        receipt = {
            "receipt": reference,
            "received": operation != "return-funds",
            "status": "returned" if operation == "return-funds" else "ok",
        }
        self._booked[reference] = {
            "operation": operation,
            "recipient": recipient,
            "receipt": receipt,
        }
        if self.behavior == "lost":
            return ReceiptResult(reference, PENDING_VERIFICATION)
        return ReceiptResult(reference, VERIFIED, receipt)

    def lookup(self, reference: str) -> Optional[dict]:
        if self.behavior == "always_lost":
            return None
        entry = self._booked.get(reference)
        return None if entry is None else entry["receipt"]


class HttpReceiptPort:
    """通过本地 HTTP 联调入口请求与查询合作方回执。"""

    def __init__(self, host: str, port: int, timeout: float = 3.0):
        self.host = host
        self.port = port
        self.timeout = timeout

    def _connection(self) -> HTTPConnection:
        return HTTPConnection(self.host, self.port, timeout=self.timeout)

    def request(self, reference: str, operation: str, recipient: str) -> ReceiptResult:
        body = json.dumps(
            {"reference": reference, "operation": operation, "recipient": recipient}
        )
        conn = self._connection()
        try:
            conn.request(
                "POST",
                "/v1/receipts",
                body,
                {"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            payload = json.loads(response.read())
            if response.status == 409:
                raise ServiceError(
                    "reference_reused",
                    f"编号 {reference} 已被另一操作使用：{payload}",
                )
            if response.status != 200:
                raise ServiceError(
                    "counterparty_rejected",
                    f"合作方拒绝回执请求：{response.status} {payload}",
                )
            return ReceiptResult(reference, VERIFIED, payload["receipt"])
        except RemoteDisconnected:
            # 对方已记录但断线：首记有效，之后凭同一编号查清。
            return ReceiptResult(reference, PENDING_VERIFICATION)
        finally:
            conn.close()

    def lookup(self, reference: str) -> Optional[dict]:
        conn = self._connection()
        try:
            conn.request("GET", f"/v1/receipts/{reference}")
            response = conn.getresponse()
            if response.status == 404:
                return None
            payload = json.loads(response.read())
            if response.status != 200:
                raise ServiceError(
                    "counterparty_lookup_failed",
                    f"查询回执失败：{response.status} {payload}",
                )
            return payload["receipt"]
        finally:
            conn.close()
