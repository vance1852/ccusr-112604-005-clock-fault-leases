"""值班引擎：证据准入、动作判定、回执处理与恢复。

判定语义（保守优先）：

* 分区可信：覆盖该分区、未超 freshness、未被 boot 作废的观测，按可信域
  去重后达到 ``source_domains_min``，且平移区间交集非空。
* renew（续用）：只有可信区间整段位于到期点之前才允许；一旦某代际已被
  证明到期，之后即使观测因向后校时回到到期前也不得重新允许。
* reclaim（回收）：只有可信区间整段位于到期点或之后（确证到期）才允许；
  不能证实到期的席位绝不交给另一个任务。
* claim（新领取）：分区可核验 + 席位确证空闲（到期或已被提交的回收释放）
  + 没有同代际的在先申请，资源才可承诺。
* 证据冲突（交集为空）、可信域不足、区间跨过到期点，一律暂缓/隔离。
* 迟到回执按 generation 围栏：旧代际的通知不能改变新归属。
"""

from __future__ import annotations

from .model import (
    Action,
    Decision,
    Observation,
    Operation,
    OperationStatus,
    Outcome,
    Seat,
    TERMINAL_STATUSES,
    Verdict,
)
from .store import (
    DutyStore,
    deserialize_observation,
    deserialize_seat,
    serialize_observation,
    serialize_seat,
)
from .trust import TrustWindow, evaluate_partition
from .validation import RecordError, validate_inventory, validate_record


class DutyEngine:
    def __init__(self, inventory: dict):
        validate_inventory(inventory)
        self.inventory = inventory
        self.source_domain: dict[str, str] = {
            s["source"]: s["domain"] for s in inventory["sources"]
        }
        self.partitions: dict[str, dict] = {
            p["partition"]: p for p in inventory["partitions"]
        }
        self.seats: dict[str, Seat] = {
            s["seat"]: Seat(**s) for s in inventory["seats"]
        }
        self.observations: list[Observation] = []
        # host -> (最新 boot 号, boot 记录到达时刻)
        self.boot_events: dict[str, tuple[int, int]] = {}
        self.operations: dict[str, Operation] = {}
        self.proven_expiry: dict[str, dict[int, dict]] = {}
        self.anomalies: list[dict] = []
        self.last_at: int = 0
        self.store: DutyStore | None = None

    # ================================================================
    # 持久化与重启
    # ================================================================
    @classmethod
    def open(cls, store: DutyStore) -> "DutyEngine":
        """从状态目录打开：优先快照，缺失时由日志重放。"""
        inventory = store.load_inventory()
        engine = cls(inventory)
        engine.store = store
        snapshot = store.load_snapshot()
        if snapshot is not None:
            engine._load_snapshot(snapshot)
        else:
            engine._replay(store.read_journal())
        return engine

    def attach_store(self, store: DutyStore) -> None:
        store.save_inventory(self.inventory)
        self.store = store
        self._persist()

    def _persist(self) -> None:
        if self.store is None:
            return
        self.store.save_snapshot(self._snapshot())

    def _snapshot(self) -> dict:
        return {
            "observations": [serialize_observation(o) for o in self.observations],
            "boot_events": dict(self.boot_events),
            "operations": {k: v.to_dict() for k, v in self.operations.items()},
            "seats": {k: serialize_seat(v) for k, v in self.seats.items()},
            "proven_expiry": {
                seat: {str(gen): fact for gen, fact in gens.items()}
                for seat, gens in self.proven_expiry.items()
            },
            "anomalies": list(self.anomalies),
            "last_at": self.last_at,
        }

    def _load_snapshot(self, snapshot: dict) -> None:
        self.observations = [
            deserialize_observation(d) for d in snapshot.get("observations", [])
        ]
        self.boot_events = dict(snapshot.get("boot_events", {}))
        self.operations = {
            k: Operation.from_dict(d) for k, d in snapshot.get("operations", {}).items()
        }
        self.seats = {
            k: deserialize_seat(d) for k, d in snapshot.get("seats", {}).items()
        }
        self.proven_expiry = {
            seat: {int(gen): fact for gen, fact in gens.items()}
            for seat, gens in snapshot.get("proven_expiry", {}).items()
        }
        self.anomalies = list(snapshot.get("anomalies", []))
        self.last_at = snapshot.get("last_at", 0)

    def _replay(self, journal: list[dict]) -> None:
        """由追加日志逐条重建，用于重启后交叉查证。"""
        for entry in journal:
            etype = entry.get("type")
            if etype == "record":
                self._ingest_record(entry["record"], journaling=False)
            elif etype == "advance":
                self.operations[entry["operation"]].status = OperationStatus.ADVANCED
                self.operations[entry["operation"]].advanced_at = entry["at"]
            elif etype == "redrive":
                # 重派本身只留痕，状态保持 ADVANCED 等待新回执
                continue
            elif etype == "recovery":
                for op_id in entry.get("review", []):
                    op = self.operations.get(op_id)
                    if op is not None and op.status not in TERMINAL_STATUSES:
                        op.status = OperationStatus.REVIEW
                for op_id in entry.get("deny", []):
                    op = self.operations.get(op_id)
                    if op is not None and op.status not in TERMINAL_STATUSES:
                        op.status = OperationStatus.DENIED

    def verify_journal(self) -> dict:
        """重放日志并与快照比对，返回不一致项（空字典表示一致）。"""
        if self.store is None:
            raise RecordError("引擎未关联状态目录，无日志可核对")
        replayed = DutyEngine(self.inventory)
        replayed._replay(self.store.read_journal())
        diffs: dict[str, object] = {}
        if replayed.last_at != self.last_at:
            diffs["last_at"] = {"journal": replayed.last_at, "snapshot": self.last_at}
        if len(replayed.observations) != len(self.observations):
            diffs["observations"] = {
                "journal": len(replayed.observations),
                "snapshot": len(self.observations),
            }
        for op_id, op in self.operations.items():
            other = replayed.operations.get(op_id)
            if other is None or other.status != op.status or other.verdict != op.verdict:
                diffs[f"operation:{op_id}"] = {
                    "journal": None if other is None else other.status.value,
                    "snapshot": op.status.value,
                }
        for seat_id, seat in self.seats.items():
            other = replayed.seats.get(seat_id)
            if other is None or (other.holder, other.generation, other.expires) != (
                seat.holder,
                seat.generation,
                seat.expires,
            ):
                diffs[f"seat:{seat_id}"] = {
                    "journal": None if other is None else serialize_seat(other),
                    "snapshot": serialize_seat(seat),
                }
        return diffs

    # ================================================================
    # 资料接入
    # ================================================================
    def ingest_file(self, records: list[dict]) -> list[dict]:
        results = []
        for index, record in enumerate(records, start=1):
            try:
                results.append(self.ingest_record(record))
            except RecordError as error:
                raise RecordError(f"第 {index} 条记录：{error}") from error
        return results

    def ingest_record(self, record: dict) -> dict:
        validate_record(record)
        if record["at"] < self.last_at:
            raise RecordError(
                f"接收时刻 {record['at']} 早于已处理时刻 {self.last_at}，"
                "资料必须按到达顺序接入"
            )
        return self._ingest_record(record, journaling=True)

    def _ingest_record(self, record: dict, *, journaling: bool) -> dict:
        if journaling and self.store is not None:
            self.store.append_journal({"type": "record", "at": record["at"], "record": record})
        kind = record["kind"]
        at = record["at"]
        self.last_at = max(self.last_at, at)
        result: dict = {"at": at, "kind": kind}

        if kind == "observation":
            obs = self._add_observation(record)
            result["observation"] = serialize_observation(obs)
            # 新证据可能直接确证某席位到期，立即登记单调事实
            for seat in self.seats.values():
                if seat.partition == obs.partition:
                    self.note_proven_expiry_from_window(seat, at)
        elif kind == "boot":
            self.boot_events[record["host"]] = (record["boot"], record["at"])
            result["host"] = record["host"]
            result["boot"] = record["boot"]
        elif kind == "intent":
            op = self._add_intent(record)
            result["operation"] = op.operation
            result["decision"] = self._decision_summary(op)
        else:
            result.update(self._add_effect(record))

        if journaling and self.store is not None:
            self._persist()
        return result

    def _add_observation(self, record: dict) -> Observation:
        if record["source"] not in self.source_domain:
            raise RecordError(f"观测引用未知时间源 {record['source']}")
        if record["partition"] not in self.partitions:
            raise RecordError(f"观测引用未知分区 {record['partition']}")
        obs = Observation(
            source=record["source"],
            partition=record["partition"],
            host=record["host"],
            boot=record["boot"],
            ticks=record["ticks"],
            action_time=record["action_time"],
            bounds=(record["bounds"][0], record["bounds"][1]),
            received_at=record["at"],
        )
        self.observations.append(obs)
        return obs

    def _add_intent(self, record: dict) -> Operation:
        op_id = record["operation"]
        if op_id in self.operations:
            raise RecordError(f"操作 {op_id} 重复申请")
        seat_id = record["seat"]
        if seat_id not in self.seats:
            raise RecordError(f"申请引用未知席位 {seat_id}")
        op = Operation(
            operation=op_id,
            seat=seat_id,
            generation=record["generation"],
            action=Action(record["action"]),
            holder=record["holder"],
            requested_expiry=record.get("requested_expiry"),
            received_at=record["at"],
        )
        self.operations[op_id] = op
        self._decide(op, record["at"])
        if self.store is not None:
            self.store.append_journal(
                {
                    "type": "decision",
                    "at": record["at"],
                    "operation": op_id,
                    "verdict": op.verdict.value if op.verdict else None,
                    "reasons": list(op.reasons),
                }
            )
        return op

    def _add_effect(self, record: dict) -> dict:
        op_id = record["operation"]
        outcome = Outcome(record["outcome"])
        op = self.operations.get(op_id)
        if op is None:
            note = {
                "at": record["at"],
                "operation": op_id,
                "problem": "回执引用未知操作，已登记异常",
            }
            self.anomalies.append(note)
            return {"operation": op_id, "anomaly": note["problem"]}
        # unknown 之后的 committed/void 属于迟到澄清，必须接受；
        # 已有确定结论后再来的回执才是重复。
        if op.outcome in (Outcome.COMMITTED, Outcome.VOID):
            note = {
                "at": record["at"],
                "operation": op_id,
                "problem": f"重复回执（已有 {op.outcome.value}），后到的 {outcome.value} 被忽略",
            }
            self.anomalies.append(note)
            return {"operation": op_id, "anomaly": note["problem"]}
        op.outcome = outcome
        op.effect_at = record["at"]
        if record["generation"] != op.generation:
            self.anomalies.append(
                {
                    "at": record["at"],
                    "operation": op_id,
                    "problem": (
                        f"回执代际 {record['generation']} 与申请代际 {op.generation} 不一致"
                    ),
                }
            )

        if outcome is Outcome.UNKNOWN:
            # 执行机已接收但没有提交结论：进入不确定桶，等待恢复流程
            if op.status not in TERMINAL_STATUSES:
                op.status = OperationStatus.ADVANCED
        elif outcome is Outcome.VOID:
            op.status = OperationStatus.VOID
        else:
            op.status = OperationStatus.COMMITTED
            self._apply_committed(op)
        return {"operation": op_id, "outcome": outcome.value, "applied": op.applied,
                "superseded": op.superseded}

    def _apply_committed(self, op: Operation) -> None:
        """把已确认提交的结果落实到席位，generation 围栏在此生效。"""
        seat = self.seats.get(op.seat)
        if seat is None:
            op.superseded = True
            return
        if op.action is Action.RENEW:
            if op.generation != seat.generation:
                op.superseded = True
                return
            if op.requested_expiry is not None:
                seat.expires = op.requested_expiry
            op.applied = True
        elif op.action is Action.RECLAIM:
            if op.generation != seat.generation:
                # 席位已有新归属：旧主机的迟到通知不能改变归属
                op.superseded = True
                return
            seat.holder = None  # type: ignore[assignment]
            op.applied = True
        else:  # CLAIM
            if seat.generation > op.generation:
                op.superseded = True
                return
            if seat.generation == op.generation and seat.holder == op.holder:
                op.applied = True
                return
            if seat.generation != op.generation - 1:
                op.superseded = True
                return
            seat.holder = op.holder
            seat.generation = op.generation
            seat.expires = op.requested_expiry if op.requested_expiry is not None else seat.expires
            op.applied = True

    # ================================================================
    # 判定
    # ================================================================
    def trust_window(self, partition: str, now: int) -> TrustWindow:
        policy = self.partitions[partition]
        return evaluate_partition(
            partition=partition,
            now=now,
            observations=self.observations,
            boot_events=self.boot_events,
            source_domain=self.source_domain,
            domains_min=policy["source_domains_min"],
            freshness=policy["freshness"],
        )

    def _record_proven_expiry(self, seat: Seat, now: int, window: TrustWindow) -> None:
        low, high = window.window  # type: ignore[misc]
        self.proven_expiry.setdefault(seat.seat, {})[seat.generation] = {
            "at": now,
            "window": [low, high],
            "expires": seat.expires,
        }

    def _decide(self, op: Operation, now: int) -> None:
        """计算判定并写入操作状态。"""
        seat = self.seats[op.seat]
        op.decided_at = now
        decision = self.compute_decision(op, seat, now)
        if decision.position == "after":
            self._record_proven_expiry(seat, now, decision)
        self._set_decision(op, decision.verdict, decision.reasons)

    def note_proven_expiry_from_window(self, seat: Seat, now: int) -> None:
        """观测接入/恢复时主动登记确证到期，不依赖某条申请触发。"""
        window = self.trust_window(seat.partition, now)
        if window.position_vs(seat.expires) == "after":
            low, high = window.window  # type: ignore[misc]
            self.proven_expiry.setdefault(seat.seat, {}).setdefault(
                seat.generation,
                {"at": now, "window": [low, high], "expires": seat.expires},
            )

    def compute_decision(self, op: Operation, seat: Seat, now: int) -> Decision:
        """纯计算：依据当前全部证据判定操作，不改变任何状态。"""
        window = self.trust_window(seat.partition, now)

        # 代际围栏
        fence = self._generation_fence(op, seat)
        if fence is not None:
            return Decision(Verdict.DENY, [fence], "unknown", window.trusted)

        # 单调事实优先：代际一旦被证明到期，即使后续观测因向后校时
        # 回到到期前、甚至制造证据冲突，续用也不得重新允许。
        if op.action is Action.RENEW:
            proven = self.proven_expiry.get(seat.seat, {}).get(seat.generation)
            if proven is not None:
                low, high = window.window if window.window else (None, None)
                return Decision(
                    Verdict.DENY,
                    [f"expiry_already_proven: 代际 {seat.generation} 已于 t={proven['at']} 证到期"],
                    window.position_vs(seat.expires),
                    window.trusted,
                    low,
                    high,
                )

        # 分区是否具备可核验权限
        if not window.quorum:
            return Decision(
                Verdict.SUSPEND,
                [f"quorum_unmet: 仅 {len(window.domains)}/{window.domains_min} 个可信域"],
                "unknown",
                False,
            )
        if window.conflict:
            low, high = window.window if window.window else (None, None)
            return Decision(
                Verdict.SUSPEND,
                ["evidence_conflict: 可信区间交集为空，隔离分区"],
                "unknown",
                False,
                low,
                high,
            )

        position = window.position_vs(seat.expires)
        low, high = window.window  # type: ignore[misc]
        if op.action is Action.RENEW:
            verdict, reasons = self._verdict_renew(seat, now, position)
        elif op.action is Action.RECLAIM:
            verdict, reasons = self._verdict_reclaim(position)
        else:
            verdict, reasons = self._verdict_claim(op, position)
        return Decision(verdict, reasons, position, True, low, high)

    def _record_proven_expiry(self, seat: Seat, now: int, decision: Decision) -> None:
        self.proven_expiry.setdefault(seat.seat, {})[seat.generation] = {
            "at": now,
            "window": [decision.window_low, decision.window_high],
            "expires": seat.expires,
        }

    def _generation_fence(self, op: Operation, seat: Seat) -> str | None:
        if op.action in (Action.RENEW, Action.RECLAIM):
            if op.generation != seat.generation:
                return f"stale_generation: 申请针对代际 {op.generation}，当前为 {seat.generation}"
            return None
        # claim 必须正好领取下一代
        if op.generation <= seat.generation:
            return f"stale_generation: 领取代际 {op.generation} 不新于当前代际 {seat.generation}"
        if op.generation != seat.generation + 1:
            return f"skipped_generation: 只能领取代际 {seat.generation + 1}，不能跳过"
        return None

    def _verdict_renew(
        self, seat: Seat, now: int, position: str
    ) -> tuple[Verdict, list[str]]:
        # 已证到期的单调拦截在 compute_decision 前置完成
        if position == "before":
            return Verdict.APPROVE, ["renew_within_lease: 可信区间整段位于到期前"]
        if position == "after":
            return Verdict.DENY, ["expired: 可信区间整段位于到期点之后"]
        return Verdict.SUSPEND, ["crosses_expiry: 可信区间跨过到期点，保守隔离"]

    def _verdict_reclaim(self, position: str) -> tuple[Verdict, list[str]]:
        if position == "after":
            return Verdict.APPROVE, ["expiry_proven: 已确证到期，允许回收席位"]
        if position == "before":
            return Verdict.SUSPEND, ["not_yet_expired: 席位仍在可信租期内，暂缓回收"]
        return Verdict.SUSPEND, ["crosses_expiry: 可信区间跨过到期点，保守隔离"]

    def _verdict_claim(self, op: Operation, position: str) -> tuple[Verdict, list[str]]:
        if position == "before":
            return Verdict.SUSPEND, [
                "seat_leased: 席位仍在可信租期内，不能承诺给新任务"
            ]
        if position == "crossing":
            return Verdict.SUSPEND, ["crosses_expiry: 可信区间跨过到期点，保守隔离"]
        # position == after：确证到期，还要看资源是否可承诺
        blocker = self._claim_blocker(op)
        if blocker is not None:
            return Verdict.SUSPEND, [blocker]
        return Verdict.APPROVE, [
            "claimable: 分区可核验、席位确证空闲且无在先申请"
        ]

    def _claim_blocker(self, op: Operation) -> str | None:
        for other in self.operations.values():
            if other.operation == op.operation or other.seat != op.seat:
                continue
            if other.generation != op.generation:
                continue
            if other.status in (
                OperationStatus.APPROVED,
                OperationStatus.ADVANCED,
            ):
                return (
                    f"resource_uncommittable: 同代际申请 {other.operation} "
                    f"已在处理中（{other.status.value}）"
                )
        return None

    def _set_decision(self, op: Operation, verdict: Verdict, reasons: list[str]) -> None:
        op.verdict = verdict
        op.reasons = reasons
        if verdict is Verdict.APPROVE:
            op.status = OperationStatus.APPROVED
        elif verdict is Verdict.SUSPEND:
            op.status = OperationStatus.SUSPENDED
        elif verdict is Verdict.DENY:
            op.status = OperationStatus.DENIED
        else:
            op.status = OperationStatus.REVIEW

    # ================================================================
    # 值班推进
    # ================================================================
    def advance(self, operation_id: str) -> Operation:
        op = self.operations.get(operation_id)
        if op is None:
            raise RecordError(f"未知操作 {operation_id}")
        if op.status is not OperationStatus.APPROVED:
            raise RecordError(
                f"操作 {operation_id} 当前状态 {op.status.value}，仅 approved 可推进"
            )
        op.status = OperationStatus.ADVANCED
        op.advanced_at = self.last_at
        if self.store is not None:
            self.store.append_journal(
                {"type": "advance", "at": self.last_at, "operation": operation_id}
            )
            self._persist()
        return op

    def redrive(self, operation_id: str) -> Operation:
        """恢复后把不确定但已证明安全的操作重新派发给恢复后的执行机。"""
        op = self.operations.get(operation_id)
        if op is None:
            raise RecordError(f"未知操作 {operation_id}")
        if op.outcome is not Outcome.UNKNOWN:
            raise RecordError(f"操作 {operation_id} 不属于「已接收未报提交」桶")
        if self.store is not None:
            self.store.append_journal(
                {"type": "redrive", "at": self.last_at, "operation": operation_id}
            )
            self._persist()
        return op

    # ================================================================
    # 恢复欠账
    # ================================================================
    def recover(self, *, apply: bool = False) -> dict:
        """按当前证据重新处理所有未终结操作。

        返回四组：
        - redrive：执行机已接收但未报提交，现已证明可安全补做/重派；
        - approve：从未发出的申请，当前证据已满足条件，可推进；
        - review：恢复后仍无法判定，留值班复核；
        - waiting：仍在等待回执或新证据，保持现状。

        仅当 apply=True 时才实际改状态并写日志，否则只给出方案（dry-run）。
        """
        now = self.last_at
        plan: dict[str, list[dict]] = {
            "redrive": [], "approve": [], "review": [], "waiting": []
        }
        review_ids: list[str] = []
        denied_ids: list[str] = []
        approve_ops: list[Operation] = []
        for op in list(self.operations.values()):
            if op.status in TERMINAL_STATUSES:
                continue
            if op.outcome is Outcome.UNKNOWN:
                self._recover_unknown(op, now, plan)
            elif op.status is OperationStatus.ADVANCED:
                plan["waiting"].append(
                    {"operation": op.operation,
                     "reason": "awaiting_receipt: 已派发，等待执行回执"}
                )
            else:
                self._recover_never_sent(op, now, plan)
        for item in plan["review"]:
            op = self.operations[item["operation"]]
            if op.status in TERMINAL_STATUSES:
                continue
            if item.get("reason", "").startswith("denied:"):
                denied_ids.append(item["operation"])
            else:
                review_ids.append(item["operation"])
        for item in plan["approve"]:
            approve_ops.append(self.operations[item["operation"]])

        if apply:
            # 先把新判定落状态，再推进，避免 approve 停留在旧的 suspended
            for op in approve_ops:
                op.verdict = Verdict.APPROVE
                op.reasons = [
                    item["reason"]
                    for item in plan["approve"]
                    if item["operation"] == op.operation
                ]
                op.status = OperationStatus.APPROVED
                op.decided_at = now
            if self.store is not None:
                self.store.append_journal(
                    {
                        "type": "recovery",
                        "at": now,
                        "redrive": [item["operation"] for item in plan["redrive"]],
                        "approve": [item["operation"] for item in plan["approve"]],
                        "review": review_ids,
                        "deny": denied_ids,
                    }
                )
            for item in plan["redrive"]:
                self.redrive(item["operation"])
            for op in approve_ops:
                self.advance(op.operation)
            for op_id in denied_ids:
                self.operations[op_id].status = OperationStatus.DENIED
            for op_id in review_ids:
                op = self.operations[op_id]
                op.status = OperationStatus.REVIEW
                note = next(
                    (i["reason"] for i in plan["review"] if i["operation"] == op_id),
                    None,
                )
                op.recovery_note = note
            if self.store is not None:
                self._persist()
        return plan

    def _recover_unknown(self, op: Operation, now: int, plan: dict) -> None:
        seat = self.seats[op.seat]
        entry = {"operation": op.operation, "action": op.action.value, "seat": op.seat}
        if op.generation != seat.generation and op.action is not Action.CLAIM:
            entry["reason"] = "fenced_late_notice: 席位已被新代际领取，旧通知不能改变归属，留复核"
            plan["review"].append(entry)
            return
        if op.action is Action.CLAIM and seat.generation > op.generation:
            entry["reason"] = "fenced_late_notice: 席位已有更新归属，留复核"
            plan["review"].append(entry)
            return
        window = self.trust_window(seat.partition, now)
        if not window.trusted:
            entry["reason"] = "no_trusted_time: 恢复后仍无可信时间，不能补做，留复核"
            plan["review"].append(entry)
            return
        position = window.position_vs(seat.expires)
        if op.action is Action.RENEW:
            proven = self.proven_expiry.get(seat.seat, {}).get(seat.generation)
            if proven is not None or position in ("after", "crossing"):
                entry["reason"] = "renew_uncertain_at_or_after_expiry: 续用是否生效已无法安全确认，留复核"
                plan["review"].append(entry)
            else:
                entry["reason"] = "renew_still_within_lease: 仍在到期前，可按操作号幂等重派"
                plan["redrive"].append(entry)
        elif op.action is Action.RECLAIM:
            if position == "after":
                entry["reason"] = "reclaim_safe_after_expiry: 已确证到期，回收可安全重做"
                plan["redrive"].append(entry)
            elif position == "before":
                entry["reason"] = "seat_still_held: 席位仍在租期内，保持等待"
                plan["waiting"].append(entry)
            else:
                entry["reason"] = "reclaim_crosses_expiry: 到期状态未能确证，留复核"
                plan["review"].append(entry)
        else:  # CLAIM
            if position == "after" and self._claim_blocker(op) is None:
                entry["reason"] = "claim_safe: 分区可核验且席位空闲，可安全重派"
                plan["redrive"].append(entry)
            else:
                entry["reason"] = "claim_unverifiable: 资源或权限不可承诺，留复核"
                plan["review"].append(entry)

    def _recover_never_sent(self, op: Operation, now: int, plan: dict) -> None:
        seat = self.seats[op.seat]
        decision = self.compute_decision(op, seat, now)
        entry = {
            "operation": op.operation,
            "action": op.action.value,
            "seat": op.seat,
            "verdict": decision.verdict.value,
            "reason": decision.reasons[0] if decision.reasons else "",
        }
        if decision.verdict is Verdict.APPROVE:
            plan["approve"].append(entry)
        elif decision.verdict is Verdict.DENY:
            # 确定性拒绝直接落终态，不混入人工复核队列
            plan["review"].append({**entry, "reason": f"denied: {entry['reason']}"})
        else:
            # 仍暂缓：继续等待新证据，保持现状
            plan["waiting"].append(entry)

    # ================================================================
    # 查询入口
    # ================================================================
    def list_operations(self, status: OperationStatus | None = None) -> list[Operation]:
        ops = list(self.operations.values())
        if status is not None:
            ops = [op for op in ops if op.status is status]
        return sorted(ops, key=lambda o: (o.received_at, o.operation))

    def get_operation(self, operation_id: str) -> Operation:
        if operation_id not in self.operations:
            raise RecordError(f"未知操作 {operation_id}")
        return self.operations[operation_id]

    def explain(self, operation_id: str) -> dict:
        op = self.get_operation(operation_id)
        seat = self.seats[op.seat]
        now = op.decided_at if op.decided_at is not None else self.last_at
        window = self.trust_window(seat.partition, now)
        eligible, rejected = [], []
        policy = self.partitions[seat.partition]
        for obs in self.observations:
            if obs.partition != seat.partition:
                continue
            item = {
                "source": obs.source,
                "domain": self.source_domain.get(obs.source),
                "host": obs.host,
                "boot": obs.boot,
                "action_time": obs.action_time,
                "shifted_bounds": list(obs.shifted_bounds(now)),
            }
            current = self.boot_events.get(obs.host)
            if current is not None and obs.boot != current[0]:
                item["rejected"] = f"boot 已变更为 {current[0]}"
                rejected.append(item)
            elif now - obs.action_time > policy["freshness"]:
                item["rejected"] = (
                    f"超出 freshness={policy['freshness']}（经过 {now - obs.action_time} 秒）"
                )
                rejected.append(item)
            else:
                eligible.append(item)
        return {
            "operation": op.to_dict(),
            "seat": serialize_seat(seat),
            "now": now,
            "partition": seat.partition,
            "isolated": not window.trusted,
            "domains": sorted(window.domains),
            "domains_min": window.domains_min,
            "trusted_window": list(window.window) if window.window else None,
            "conflict": window.conflict,
            "eligible_evidence": eligible,
            "rejected_evidence": rejected,
            "proven_expiry": self.proven_expiry.get(seat.seat, {}).get(seat.generation),
        }

    def partition_status(self, now: int | None = None) -> dict:
        now = self.last_at if now is None else now
        result = {}
        for name, policy in self.partitions.items():
            window = self.trust_window(name, now)
            result[name] = {
                "now": now,
                "freshness": policy["freshness"],
                "domains_min": policy["source_domains_min"],
                "domains": sorted(window.domains),
                "quorum": window.quorum,
                "conflict": window.conflict,
                "trusted_window": list(window.window) if window.window else None,
                "isolated": not window.trusted,
                "eligible_count": len(window.eligible),
                "rejected_stale": window.rejected_stale,
                "rejected_boot": window.rejected_boot,
            }
        return result

    def seat_status(self) -> list[dict]:
        rows = []
        for seat in self.seats.values():
            window = self.trust_window(seat.partition, self.last_at)
            rows.append(
                {
                    "seat": seat.seat,
                    "partition": seat.partition,
                    "holder": seat.holder,
                    "released": seat.holder is None,
                    "generation": seat.generation,
                    "expires": seat.expires,
                    "position_now": window.position_vs(seat.expires),
                    "partition_isolated": not window.trusted,
                    "proven_expiry_generations": sorted(
                        self.proven_expiry.get(seat.seat, {}).keys()
                    ),
                }
            )
        return rows

    def _decision_summary(self, op: Operation) -> dict:
        return {
            "operation": op.operation,
            "action": op.action.value,
            "status": op.status.value,
            "verdict": op.verdict.value if op.verdict else None,
            "reasons": list(op.reasons),
        }
