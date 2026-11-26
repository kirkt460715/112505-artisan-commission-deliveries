"""古村合作社联作订单的分件验收、领货与结算服务。

本模块只包含纯领域逻辑，不发起任何网络调用；与合作取件点、资金经办的
交互通过 ``ReceiptPort`` 端口抽象完成（见 ``counterparty.py``）。

口径约定：

* 价款一律以分（cents）为单位；分件 ``agreed_cents`` 一经约定，非客人
  明确同意不得变更，手艺人的补做、换料不会改动它。
* 一笔订金按台账中分件的登记次序逐件冲抵，每件冲抵额不超过其约定价款，
  冲抵不完的部分记为订单层面的客人余款。
* 分件阶段：``material``（原料/补做/待料）、``review``（验收待确认）、
  ``pickup``（领货）、``settlement``（已收货待结算）、``settled``（已结清）、
  ``returned``（已退件）。
* 交接凭据一经建立即与分件绑定：断线只产生“待核实”状态，只有同一张凭据
  能查清该件，拆换领取授权不会改变这一点。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ---- 阶段与状态常量 -------------------------------------------------------

MATERIAL = "material"
REVIEW = "review"
PICKUP = "pickup"
SETTLEMENT = "settlement"
SETTLED = "settled"
RETURNED = "returned"

PENDING_VERIFICATION = "pending_verification"
VERIFIED = "verified"
DISPUTED = "disputed"

ACCEPTED = "accepted"
DECLINED = "declined"


class ServiceError(Exception):
    """业务规则冲突，``code`` 供调用方程序化处理。"""

    def __init__(self, code: str, message: str = "", **context: Any):
        super().__init__(message or code)
        self.code = code
        self.context = context


# ---- 领域值对象 ------------------------------------------------------------


@dataclass
class ReviewRecord:
    piece_id: str
    decision: str
    minute: Optional[int]
    reason: Optional[str] = None


@dataclass
class Handover:
    """一次实物交接及其凭据。"""

    credential: str
    collector: str
    point: str
    state: str = PENDING_VERIFICATION
    receipt: Optional[dict] = None
    minute: Optional[int] = None


@dataclass
class DelegationRecord:
    piece_id: str
    collector: str
    minute: Optional[int] = None


@dataclass
class Piece:
    id: str
    maker: str
    agreed_cents: int
    sample: Optional[str]
    stage: str = MATERIAL
    confirmed: bool = False
    blocker: Optional[str] = None
    reviews: list[ReviewRecord] = field(default_factory=list)
    sample_history: list[str] = field(default_factory=list)
    materials: list[str] = field(default_factory=list)
    price_history: list[dict] = field(default_factory=list)
    serial: Optional[str] = None
    previous_paid: int = 0
    released_cents: int = 0
    handover: Optional[Handover] = None
    returned: bool = False
    physically_returned: bool = False

    @property
    def received(self) -> bool:
        return self.handover is not None and self.handover.state == VERIFIED


@dataclass
class Assembly:
    name: str
    needs: list[str]
    passed: bool = False


@dataclass
class ChangeCase:
    """一次退改：退掉若干分件后产生的应付/退款方案。"""

    case_id: str
    returned_pieces: list[str]
    reason: Optional[str]
    lines: list[dict]
    refund_cents: int
    payable_cents: int
    status: str  # opened / refund_pending / returned / payable_due
    money_receipt: Optional[dict] = None


# ---- 订单聚合 --------------------------------------------------------------


class CooperativeOrder:
    def __init__(self, register: dict):
        self.order_id: str = register["order"]
        self.deposit_cents: int = int(register["deposit_cents"])
        self.split_pickup: bool = bool(register.get("agreed_split_pickup", False))
        self.pieces: list[Piece] = []
        self.assemblies: list[Assembly] = []
        self.delegations: dict[str, str] = {}
        self.delegation_history: list[DelegationRecord] = []
        self.serial_stock: dict[str, Optional[str]] = {}
        self.events: list[dict] = []
        self.change_cases: dict[str, ChangeCase] = {}

        seen: set[str] = set()
        for row in register["pieces"]:
            if row["id"] in seen:
                raise ServiceError("duplicate_piece", f"分件重复：{row['id']}")
            seen.add(row["id"])
            paid = int(register.get("payment", {}).get("previous_paid", {}).get(row["id"], 0))
            self.pieces.append(
                Piece(
                    id=row["id"],
                    maker=row["maker"],
                    agreed_cents=int(row["agreed_cents"]),
                    sample=row.get("sample"),
                    previous_paid=paid,
                    sample_history=[row["sample"]] if row.get("sample") else [],
                    price_history=[{"agreed_cents": int(row["agreed_cents"]), "basis": "order_register"}],
                )
            )

        for dep in register.get("dependencies", []):
            unknown = [p for p in dep["needs"] if p not in seen]
            if unknown:
                raise ServiceError("unknown_dependency", f"依赖了不存在的分件：{unknown}")
            self.assemblies.append(Assembly(dep["assembly"], list(dep["needs"])))

        granted: dict[str, str] = {}
        for d in register.get("pickup_delegations", []):
            for piece_id in d["pieces"]:
                if piece_id not in seen:
                    raise ServiceError("unknown_piece", f"授权指向不存在的分件：{piece_id}")
                if piece_id in granted:
                    raise ServiceError(
                        "duplicate_delegation",
                        f"分件 {piece_id} 同时授予了 {granted[piece_id]} 与 {d['collector']}",
                    )
                granted[piece_id] = d["collector"]
                self.delegation_history.append(DelegationRecord(piece_id, d["collector"]))
        self.delegations = dict(granted)

        for stock in register.get("serial_stock", []):
            self.serial_stock[stock["serial"]] = stock.get("allocated_order")

    # ---- 内部工具 ---------------------------------------------------------

    def piece(self, piece_id: str) -> Piece:
        for p in self.pieces:
            if p.id == piece_id:
                return p
        raise ServiceError("unknown_piece", f"订单中不存在分件：{piece_id}")

    def _blocking_assemblies(self, piece_id: str) -> list[Assembly]:
        blocked = []
        for assembly in self.assemblies:
            if piece_id not in assembly.needs:
                continue
            if assembly.passed:
                continue
            if any(not self.piece(n).confirmed for n in assembly.needs):
                blocked.append(assembly)
        return blocked

    # ---- 样稿与验收 -------------------------------------------------------

    def submit_for_review(self, piece_id: str, minute: Optional[int] = None) -> None:
        """分件制作完成，送验收待确认。"""
        p = self.piece(piece_id)
        if p.confirmed:
            raise ServiceError("piece_already_confirmed", f"{piece_id} 已验收确认，无需再送审")
        if p.stage not in (MATERIAL, REVIEW):
            raise ServiceError("invalid_transition", f"{piece_id} 当前阶段 {p.stage} 不能送审")
        p.stage = REVIEW
        p.blocker = None

    def apply_review(
        self,
        piece_id: str,
        decision: str,
        reason: Optional[str] = None,
        minute: Optional[int] = None,
    ) -> None:
        """登记验收结论。已独立验收的实物不会因别的分件被拒收而撤销。"""
        p = self.piece(piece_id)
        if decision not in (ACCEPTED, DECLINED):
            raise ServiceError("invalid_decision", f"未知验收结论：{decision}")
        if p.returned:
            raise ServiceError("piece_returned", f"{piece_id} 已退件，不能再验收")
        p.reviews.append(ReviewRecord(piece_id, decision, minute, reason))
        if decision == ACCEPTED:
            p.confirmed = True
            p.stage = PICKUP
            p.blocker = None
            eligibility = self.pickup_eligibility(piece_id)
            if eligibility == "wait_assembly":
                p.blocker = "wait_assembly"
            elif eligibility == "not_eligible":
                p.blocker = "no_delegation"
        else:
            p.confirmed = False
            p.stage = MATERIAL
            p.blocker = f"declined:{reason}" if reason else "declined"

    def apply_sample_revision(
        self, new_sample: str, affects: list[str], minute: Optional[int] = None
    ) -> list[str]:
        """样稿变更：只重开真正受影响、且尚未确认的分件，返回被重开的分件。"""
        reopened: list[str] = []
        for piece_id in affects:
            p = self.piece(piece_id)
            if p.confirmed:
                # 已确认的分件不受样稿变更影响。
                continue
            if p.sample is None or p.sample == new_sample:
                # 没有样稿约束、或已经是新样稿的分件不算“真正受影响”。
                continue
            p.sample = new_sample
            p.sample_history.append(new_sample)
            p.confirmed = False
            p.handover = None
            last_decline = next(
                (r for r in reversed(p.reviews) if r.decision == DECLINED), None
            )
            if last_decline is not None:
                p.stage = MATERIAL
                p.blocker = f"sample_reopened:{last_decline.reason or 'declined'}"
            else:
                p.stage = REVIEW
                p.blocker = "sample_reopened"
            reopened.append(piece_id)
        return reopened

    # ---- 补做、换料与价款 -------------------------------------------------

    def record_rework(self, piece_id: str, reason: Optional[str] = None) -> None:
        """手艺人接受补做：只改变制作状态，绝不改变约定价款。"""
        p = self.piece(piece_id)
        if p.confirmed:
            raise ServiceError("piece_already_confirmed", f"{piece_id} 已确认，无需补做")
        p.stage = MATERIAL
        p.blocker = f"rework:{reason}" if reason else "rework"

    def substitute_material(self, piece_id: str, material: str) -> None:
        """手艺人换料：登记用料，约定价款不变，涨价须另行取得客人同意。"""
        p = self.piece(piece_id)
        if p.confirmed:
            raise ServiceError("piece_already_confirmed", f"{piece_id} 已确认，不能换料")
        p.materials.append(material)
        p.stage = MATERIAL
        p.blocker = "material_substitution"

    def request_price_change(self, piece_id: str, new_cents: int, *, customer_consent: bool = False) -> None:
        """调价只有在客人明确同意时才成立；补做/换料本身不构成同意。"""
        p = self.piece(piece_id)
        if new_cents < 0:
            raise ServiceError("invalid_price", "价款不能为负")
        if new_cents == p.agreed_cents:
            return
        if new_cents > p.agreed_cents and not customer_consent:
            raise ServiceError(
                "price_change_needs_consent",
                f"分件 {piece_id} 涨价到 {new_cents} 缺少客人明确同意",
                piece=piece_id,
                current_cents=p.agreed_cents,
                requested_cents=new_cents,
            )
        p.price_history.append(
            {"agreed_cents": new_cents, "basis": "customer_consent" if customer_consent else "adjustment"}
        )
        p.agreed_cents = new_cents

    def allocate_serial(self, piece_id: str, serial: str) -> None:
        """把带序列号的共用底座分配给本分件；已确认给别的订单的不得挤占。"""
        p = self.piece(piece_id)
        owner = self.serial_stock.get(serial)
        if owner is not None and owner != self.order_id:
            raise ServiceError(
                "serial_allocated_elsewhere",
                f"底座 {serial} 已确认给订单 {owner}，不能挤占",
                serial=serial,
                allocated_order=owner,
            )
        holder = next((q.id for q in self.pieces if q.serial == serial), None)
        if holder is not None and holder != piece_id:
            raise ServiceError(
                "serial_in_use",
                f"底座 {serial} 已在本订单分给分件 {holder}",
                serial=serial,
                held_by=holder,
            )
        self.serial_stock[serial] = self.order_id
        p.serial = serial

    # ---- 领取授权与组合判定 -----------------------------------------------

    def delegate(self, piece_id: str, collector: str, minute: Optional[int] = None) -> None:
        """授予/拆换领取人。

        未确认收到的分件不能同时交给两个领取人：若该分件尚有断线后待核实的
        交接，授权变更只留痕，实际领取人仍以原交接凭据上的领取人为准，且
        该件不会因此被重新标为可交。
        """
        p = self.piece(piece_id)
        if p.received:
            raise ServiceError("piece_already_received", f"{piece_id} 已确认收到，无需再授权")
        self.delegations[piece_id] = collector
        self.delegation_history.append(DelegationRecord(piece_id, collector, minute))

    def effective_collector(self, piece_id: str) -> Optional[str]:
        """待核实交接存续期间，以交接凭据上的领取人为准。"""
        p = self.piece(piece_id)
        if p.handover is not None and p.handover.state == PENDING_VERIFICATION:
            return p.handover.collector
        return self.delegations.get(piece_id)

    def pickup_eligibility(self, piece_id: str) -> str:
        """返回 ``independent`` / ``wait_assembly`` / ``not_eligible`` / ``received``。"""
        p = self.piece(piece_id)
        if p.received:
            return "received"
        if not p.confirmed or p.returned:
            return "not_eligible"
        blocked = self._blocking_assemblies(piece_id)
        if not blocked:
            return "independent" if self.effective_collector(piece_id) else "not_eligible"
        if self.split_pickup and self.effective_collector(piece_id):
            # 客人允许拆领且该件有明确领取人：可独立领货，组合其余分件另行等待。
            return "independent"
        return "wait_assembly"

    def pass_assembly(self, assembly_name: str) -> None:
        for assembly in self.assemblies:
            if assembly.name == assembly_name:
                unconfirmed = [n for n in assembly.needs if not self.piece(n).confirmed]
                if unconfirmed:
                    raise ServiceError(
                        "assembly_blocked",
                        f"组合 {assembly_name} 尚有未通过验收的分件：{unconfirmed}",
                        unconfirmed=unconfirmed,
                    )
                assembly.passed = True
                for n in assembly.needs:
                    p = self.piece(n)
                    if p.stage == PICKUP and p.blocker == "wait_assembly":
                        p.blocker = None
                return
        raise ServiceError("unknown_assembly", f"未知组合：{assembly_name}")

    # ---- 实物交接与断线核对 -----------------------------------------------

    def record_handover(
        self,
        piece_id: str,
        collector: str,
        credential: str,
        point: str,
        port: Any,
        minute: Optional[int] = None,
    ) -> Handover:
        """登记一次实物交接并向合作取件点索取回执。

        取件点断线时，交接被记为 ``pending_verification``：保管方继续承担
        核对责任，分件不会被标为已收到。重复使用同一凭据是安全重试；换一
        张凭据或换一个领取人都会被拒绝。
        """
        p = self.piece(piece_id)
        if p.returned:
            raise ServiceError("piece_returned", f"{piece_id} 已退件，不能交接")
        if not p.confirmed:
            raise ServiceError("piece_not_confirmed", f"{piece_id} 尚未验收通过，不能交接")
        if p.received:
            raise ServiceError("piece_already_received", f"{piece_id} 已确认收到")
        eligibility = self.pickup_eligibility(piece_id)
        if eligibility == "wait_assembly":
            raise ServiceError(
                "assembly_blocked",
                f"{piece_id} 须等组合验收通过后方可领取",
            )
        if collector != self.effective_collector(piece_id):
            raise ServiceError(
                "collector_not_delegated",
                f"领取人 {collector} 未被授予分件 {piece_id}",
                effective_collector=self.effective_collector(piece_id),
            )

        if p.handover is not None:
            if p.handover.credential != credential:
                raise ServiceError(
                    "handover_pending",
                    f"{piece_id} 有待核实交接 {p.handover.credential}，不能另开凭据",
                    existing_credential=p.handover.credential,
                )
            # 同一张凭据：重试，沿用原领取人与取件点。
            result = port.request(credential, "take-piece", p.handover.point)
        else:
            handover = Handover(
                credential=credential, collector=collector, point=point, minute=minute
            )
            p.handover = handover
            result = port.request(credential, "take-piece", point)

        self._apply_receipt_result(p.handover, result)
        if p.handover.state == VERIFIED:
            self._sync_received_stage()
        else:
            p.stage = PICKUP
            p.blocker = "pending_verification"
        return p.handover

    @staticmethod
    def _apply_receipt_result(handover: Handover, result: Any) -> None:
        if result.state == VERIFIED:
            receipt = result.receipt or {}
            if receipt.get("received") is False:
                handover.state = DISPUTED
                handover.receipt = receipt
                return
            handover.state = VERIFIED
            handover.receipt = receipt
        else:
            handover.state = PENDING_VERIFICATION

    def reconcile_handover(self, credential: str, port: Any) -> bool:
        """用同一张交接凭据查清待核实件；返回是否已核实。

        凭据不符直接拒绝：拆换领取授权不能重置待核实状态，只有这张凭据
        能闭合保管方的核对责任。
        """
        handover = next(
            (p.handover for p in self.pieces if p.handover and p.handover.credential == credential),
            None,
        )
        if handover is None:
            raise ServiceError("unknown_credential", f"找不到交接凭据：{credential}")
        if handover.state == VERIFIED:
            return True
        receipt = port.lookup(credential)
        if receipt is None:
            # 合作方尚无可查回执，保管方继续保留核对责任。
            return False
        if receipt.get("received") is False:
            handover.state = DISPUTED
            handover.receipt = receipt
            return False
        handover.state = VERIFIED
        handover.receipt = receipt
        self._sync_received_stage()
        return True

    def _sync_received_stage(self) -> None:
        for p in self.pieces:
            if p.received and p.stage in (PICKUP, SETTLEMENT) and not p.returned:
                p.stage = SETTLEMENT
                p.blocker = None

    # ---- 订金、尾款与退改结算 ---------------------------------------------

    def deposit_allocation(self) -> tuple[dict[str, int], int]:
        """订金按台账分件次序冲抵，返回（各件冲抵额，未冲抵余款）。"""
        remaining = self.deposit_cents
        allocation: dict[str, int] = {}
        for p in self.pieces:
            share = min(remaining, p.agreed_cents)
            allocation[p.id] = share
            remaining -= share
        return allocation, remaining

    def piece_credits(self, piece_id: str) -> int:
        """客人已为某分件支付的金额：订金冲抵额加此前已付。"""
        allocation, _ = self.deposit_allocation()
        p = self.piece(piece_id)
        return allocation[piece_id] + p.previous_paid

    def releasable(self, piece_id: str) -> int:
        """已验收且已收到的分件可释放的尾款；不满足条件返回 0。"""
        p = self.piece(piece_id)
        if not p.confirmed or not p.received or p.returned:
            return 0
        return max(0, p.agreed_cents - self.piece_credits(piece_id) - p.released_cents)

    def release_payment(self, piece_id: str) -> int:
        """释放某分件的尾款；未核实交割不得释放。"""
        p = self.piece(piece_id)
        amount = self.releasable(piece_id)
        if amount <= 0:
            if not p.received and not p.returned:
                raise ServiceError(
                    "handover_unverified",
                    f"{piece_id} 的交割尚未核实，不能释放尾款",
                )
            raise ServiceError("nothing_releasable", f"{piece_id} 没有可释放的尾款")
        p.released_cents += amount
        if p.released_cents + self.piece_credits(piece_id) >= p.agreed_cents:
            p.stage = SETTLED
            p.blocker = None
        return amount

    def open_change_case(
        self, case_id: str, piece_ids: list[str], reason: Optional[str] = None
    ) -> ChangeCase:
        """退掉若干分件，按约定价款、订金冲抵与已付记录生成应付/退款方案。

        已签收且没有退回的实物仍保留其应付部分；退件名下已付的钱进入退款。
        行项按毛额列示（退件退已付、留件付尾款），订单净额另算：
        退款 = 退件已付 + 订单未冲抵订金；应付 = 留件尾款 - 已释放尾款。
        """
        if case_id in self.change_cases:
            raise ServiceError("duplicate_case", f"退改单已存在：{case_id}")
        targets = [self.piece(pid) for pid in piece_ids]
        if any(p.returned for p in targets):
            raise ServiceError("piece_returned", "退件不能重复退改")

        allocation, unallocated = self.deposit_allocation()
        lines: list[dict] = []
        refund_from_returns = 0
        payable_kept = 0
        for p in self.pieces:
            credits = allocation[p.id] + p.previous_paid
            if p in targets:
                p.returned = True
                p.physically_returned = bool(p.received)
                p.stage = RETURNED
                p.blocker = None
                refund_from_returns += credits
                lines.append(
                    {
                        "piece": p.id,
                        "agreed_cents": p.agreed_cents,
                        "credits_cents": credits,
                        "payable_cents": 0,
                        "refundable_cents": credits,
                        "physically_returned": p.physically_returned,
                    }
                )
            else:
                # 只有已签收且保留的实物才有应付部分；未交付分件的尾款暂不轧差。
                if p.received:
                    outstanding = max(0, p.agreed_cents - credits - p.released_cents)
                else:
                    outstanding = 0
                payable_kept += outstanding
                lines.append(
                    {
                        "piece": p.id,
                        "agreed_cents": p.agreed_cents,
                        "credits_cents": credits,
                        "payable_cents": outstanding,
                        "refundable_cents": 0,
                        "physically_returned": False,
                    }
                )

        refund_cents = refund_from_returns + unallocated
        payable_cents = payable_kept
        if refund_cents and payable_cents:
            # 同一客人的债权债务轧差后只走一个方向。
            if refund_cents > payable_cents:
                refund_cents -= payable_cents
                payable_cents = 0
            else:
                payable_cents -= refund_cents
                refund_cents = 0
        if refund_cents:
            status = "opened"
        elif payable_cents:
            status = "payable_due"
        else:
            status = "returned"
        case = ChangeCase(
            case_id=case_id,
            returned_pieces=list(piece_ids),
            reason=reason,
            lines=lines,
            refund_cents=refund_cents,
            payable_cents=payable_cents,
            status=status,
        )
        self.change_cases[case_id] = case
        return case

    def execute_refund(self, case_id: str, port: Any) -> ChangeCase:
        """通过资金经办端口划出退款；断线则留待同编号凭据重试。"""
        case = self.change_cases.get(case_id)
        if case is None:
            raise ServiceError("unknown_case", f"未知退改单：{case_id}")
        if case.refund_cents <= 0:
            case.status = "payable_due" if case.payable_cents else "returned"
            return case
        result = port.request(case.case_id, "return-funds", "cash-provider")
        if result.state == VERIFIED:
            case.money_receipt = result.receipt
            case.status = "returned"
        else:
            case.status = "refund_pending"
        return case

    def reconcile_refund(self, case_id: str, port: Any) -> bool:
        case = self.change_cases.get(case_id)
        if case is None:
            raise ServiceError("unknown_case", f"未知退改单：{case_id}")
        if case.status == "returned":
            return True
        receipt = port.lookup(case.case_id)
        if receipt is None:
            return False
        case.money_receipt = receipt
        case.status = "returned"
        return True

    def reconcile(self, reference: str, port: Any) -> str:
        """通用核对入口：按编号找到待核实的交接或退款并查清。"""
        if any(p.handover and p.handover.credential == reference for p in self.pieces):
            self.reconcile_handover(reference, port)
            self._sync_received_stage()
            return "handover"
        if reference in self.change_cases:
            self.reconcile_refund(reference, port)
            return "refund"
        raise ServiceError("unknown_reference", f"找不到待核实编号：{reference}")

    # ---- 各类查询视图 -----------------------------------------------------

    def piece_view(self, piece_id: str) -> dict:
        p = self.piece(piece_id)
        allocation, _ = self.deposit_allocation()
        return {
            "piece": p.id,
            "maker": p.maker,
            "stage": p.stage,
            "blocker": p.blocker,
            "sample": p.sample,
            "confirmed": p.confirmed,
            "serial": p.serial,
            "agreed_cents": p.agreed_cents,
            "deposit_share_cents": allocation[p.id],
            "previous_paid_cents": p.previous_paid,
            "released_cents": p.released_cents,
            "payable_cents": max(0, p.agreed_cents - allocation[p.id] - p.previous_paid),
            "pickup_eligibility": self.pickup_eligibility(p.id),
            "effective_collector": self.effective_collector(p.id),
            "handover": (
                {
                    "credential": p.handover.credential,
                    "collector": p.handover.collector,
                    "point": p.handover.point,
                    "state": p.handover.state,
                    "receipt": p.handover.receipt,
                }
                if p.handover
                else None
            ),
            "returned": p.returned,
        }

    def maker_view(self, maker: Optional[str] = None) -> list[dict]:
        """手艺人查看自己的分件卡在哪一道：原料、验收、领货还是结算。"""
        rows = []
        for p in self.pieces:
            if maker is not None and p.maker != maker:
                continue
            rows.append(
                {
                    "piece": p.id,
                    "maker": p.maker,
                    "stage": p.stage,
                    "blocker": p.blocker,
                    "confirmed": p.confirmed,
                    "sample": p.sample,
                    "serial": p.serial,
                }
            )
        return rows

    def custody_view(self) -> list[dict]:
        """保管方视角：所有断线后仍待核实的交接，核对责任不解除。"""
        rows = []
        for p in self.pieces:
            if p.handover and p.handover.state in (PENDING_VERIFICATION, DISPUTED):
                rows.append(
                    {
                        "piece": p.id,
                        "credential": p.handover.credential,
                        "collector": p.handover.collector,
                        "point": p.handover.point,
                        "state": p.handover.state,
                    }
                )
        return rows

    def payment_view(self) -> dict:
        """款项经办视角：可释放尾款、被阻断项与未核实交割一览。"""
        self._sync_received_stage()
        releasable, blocked, unverified = [], [], []
        for p in self.pieces:
            if p.returned:
                continue
            if p.handover and p.handover.state in (PENDING_VERIFICATION, DISPUTED):
                unverified.append(
                    {
                        "piece": p.id,
                        "credential": p.handover.credential,
                        "point": p.handover.point,
                        "state": p.handover.state,
                    }
                )
            amount = self.releasable(p.id)
            if p.received:
                releasable.append({"piece": p.id, "amount_cents": amount})
            elif p.confirmed:
                reason = p.blocker or "awaiting_handover"
                blocked.append({"piece": p.id, "reason": reason})
        pending_refunds = [
            {"case": c.case_id, "refund_cents": c.refund_cents}
            for c in self.change_cases.values()
            if c.status == "refund_pending"
        ]
        return {
            "releasable": releasable,
            "blocked": blocked,
            "unverified_deliveries": unverified,
            "pending_refunds": pending_refunds,
        }

    def statement(self) -> dict:
        allocation, unallocated = self.deposit_allocation()
        total_agreed = sum(p.agreed_cents for p in self.pieces if not p.returned)
        total_paid = self.deposit_cents + sum(p.previous_paid for p in self.pieces)
        return {
            "order": self.order_id,
            "currency": "cents",
            "deposit_cents": self.deposit_cents,
            "unallocated_deposit_cents": unallocated,
            "total_agreed_kept_cents": total_agreed,
            "total_paid_cents": total_paid,
            "pieces": [self.piece_view(p.id) for p in self.pieces],
            "change_cases": [
                {
                    "case": c.case_id,
                    "returned_pieces": c.returned_pieces,
                    "refund_cents": c.refund_cents,
                    "payable_cents": c.payable_cents,
                    "status": c.status,
                    "lines": c.lines,
                    "money_receipt": c.money_receipt,
                }
                for c in self.change_cases.values()
            ],
        }

    # ---- 事件流接入 -------------------------------------------------------

    def apply_event(
        self,
        event: dict,
        port: Any = None,
        pickup_point: str = "pickup-east",
    ) -> None:
        """按送达顺序应用一条端上消息；业务时点以消息内 minute 为准。"""
        self.events.append(event)
        minute = event.get("minute")
        payload = event["payload"]
        source = event["source"]
        if source == "review":
            self.apply_review(
                payload["piece"], payload["decision"], payload.get("reason"), minute
            )
        elif source == "design":
            self.apply_sample_revision(payload["sample"], list(payload["affects"]), minute)
        elif source == "pickup":
            if port is None:
                # 没有回执端口时按“交接待核实”留痕，由保管方之后凭凭据核对。
                p = self.piece(payload["piece"])
                p.handover = Handover(
                    credential=payload["handover"],
                    collector=payload["collector"],
                    point=pickup_point,
                    state=PENDING_VERIFICATION,
                    minute=minute,
                )
                p.stage = PICKUP
                p.blocker = "pending_verification"
            else:
                handover = self.record_handover(
                    payload["piece"],
                    payload["collector"],
                    payload["handover"],
                    pickup_point,
                    port,
                    minute,
                )
                if handover.state == VERIFIED:
                    self._sync_received_stage()
                else:
                    self.piece(payload["piece"]).stage = PICKUP
                    self.piece(payload["piece"]).blocker = "pending_verification"
        else:
            raise ServiceError("unknown_event_source", f"未知消息来源：{source}")


def load_bundle(folder: Path) -> tuple[dict, list[dict]]:
    folder = Path(folder)
    register = json.loads((folder / "registers.json").read_text(encoding="utf-8"))
    records = [
        json.loads(line)
        for line in (folder / "events.ndjson").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return register, records


def ingest(
    order: CooperativeOrder,
    records: list[dict],
    port: Any = None,
    pickup_point: str = "pickup-east",
) -> None:
    for event in sorted(records, key=lambda e: e["cursor"]):
        order.apply_event(event, port=port, pickup_point=pickup_point)


def _summary(order: CooperativeOrder) -> dict:
    order._sync_received_stage()
    return {
        "order": order.statement(),
        "makers": order.maker_view(),
        "custody": order.custody_view(),
        "payment": order.payment_view(),
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=Path(__file__).parent / "fixtures")
    parser.add_argument("--pickup-point", default="pickup-east")
    parser.add_argument(
        "--gateway-port",
        type=int,
        default=None,
        help="提供本地联调入口端口后，交接与退款回执将走真实 HTTP 核对",
    )
    options = parser.parse_args()

    register, records = load_bundle(options.fixtures)
    order = CooperativeOrder(register)
    port = None
    if options.gateway_port is not None:
        from counterparty import HttpReceiptPort

        port = HttpReceiptPort("127.0.0.1", options.gateway_port)
    ingest(order, records, port=port, pickup_point=options.pickup_point)
    print(json.dumps(_summary(order), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
