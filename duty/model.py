"""领域模型：时间源、观测、席位、操作。

演练中存在两种时间基准：
- 到达时刻 ``at`` / ``action_time`` 是值班侧演练秒；
- 观测 ``bounds`` 与席位 ``expires`` 使用同一可信时间基准（机器自校时间）。
同一观测有效期内两种基准秒长相同，因此可以按经过秒数平移区间。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Action(str, Enum):
    """续用、回收、新领取。"""

    RENEW = "renew"
    RECLAIM = "reclaim"
    CLAIM = "claim"


class Outcome(str, Enum):
    """执行回执的三种确认状态。"""

    UNKNOWN = "unknown"
    COMMITTED = "committed"
    VOID = "void"


class Verdict(str, Enum):
    """值班判定：可通过、暂缓（证据不足/冲突）、拒绝（确定不允许）、留复核。"""

    APPROVE = "approve"
    SUSPEND = "suspend"
    DENY = "deny"
    REVIEW = "review"


class OperationStatus(str, Enum):
    """操作生命周期状态。"""

    PENDING = "pending"          # 申请已到，尚未判定
    APPROVED = "approved"        # 判定可通过，等待值班推进
    ADVANCED = "advanced"        # 已派发，等待执行回执
    SUSPENDED = "suspended"      # 证据不足，暂缓
    DENIED = "denied"            # 确定不允许（如已证到期的续用、过期代际）
    COMMITTED = "committed"      # 已确认提交
    VOID = "void"                # 已确认未生效
    REVIEW = "review"            # 恢复后仍无法判定，留人工复核


# 终态：之后不再变更
TERMINAL_STATUSES = frozenset(
    {OperationStatus.COMMITTED, OperationStatus.VOID, OperationStatus.REVIEW,
     OperationStatus.DENIED}
)


@dataclass
class Decision:
    """一次纯计算的判定结果，不修改操作状态。"""

    verdict: Verdict
    reasons: list[str]
    position: str
    trusted: bool
    window_low: int | None = None
    window_high: int | None = None


@dataclass
class Source:
    source: str
    domain: str


@dataclass
class PartitionPolicy:
    partition: str
    source_domains_min: int
    freshness: int


@dataclass
class Observation:
    """一条时间观测证据。

    ``action_time`` 是观测产生时的值班侧演练秒；``bounds`` 是当时对可信时间的
    闭区间估计。决策时刻 now 距 action_time 的经过秒数同时用于平移区间，
    并判断该证据是否仍在 freshness 有效期内。
    """

    source: str
    partition: str
    host: str
    boot: int
    ticks: int
    action_time: int
    bounds: tuple[int, int]
    received_at: int

    def shifted_bounds(self, now: int) -> tuple[int, int]:
        elapsed = now - self.action_time
        low, high = self.bounds
        return low + elapsed, high + elapsed


@dataclass
class Seat:
    seat: str
    partition: str
    holder: str
    generation: int
    expires: int


@dataclass
class Operation:
    """一次续用/回收/领取申请及其全生命周期信息。"""

    operation: str
    seat: str
    generation: int
    action: Action
    holder: str
    requested_expiry: int | None
    received_at: int
    status: OperationStatus = OperationStatus.PENDING
    verdict: Verdict | None = None
    reasons: list[str] = field(default_factory=list)
    decided_at: int | None = None
    advanced_at: int | None = None
    outcome: Outcome | None = None
    effect_at: int | None = None
    applied: bool = False          # 提交结果是否已落实到席位状态
    superseded: bool = False       # 迟到回执针对的代际已被新归属取代
    recovery_note: str | None = None

    def to_dict(self) -> dict:
        return {
            "operation": self.operation,
            "seat": self.seat,
            "generation": self.generation,
            "action": self.action.value,
            "holder": self.holder,
            "requested_expiry": self.requested_expiry,
            "received_at": self.received_at,
            "status": self.status.value,
            "verdict": self.verdict.value if self.verdict else None,
            "reasons": list(self.reasons),
            "decided_at": self.decided_at,
            "advanced_at": self.advanced_at,
            "outcome": self.outcome.value if self.outcome else None,
            "effect_at": self.effect_at,
            "applied": self.applied,
            "superseded": self.superseded,
            "recovery_note": self.recovery_note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Operation":
        return cls(
            operation=data["operation"],
            seat=data["seat"],
            generation=data["generation"],
            action=Action(data["action"]),
            holder=data["holder"],
            requested_expiry=data.get("requested_expiry"),
            received_at=data["received_at"],
            status=OperationStatus(data["status"]),
            verdict=Verdict(data["verdict"]) if data.get("verdict") else None,
            reasons=list(data.get("reasons", [])),
            decided_at=data.get("decided_at"),
            advanced_at=data.get("advanced_at"),
            outcome=Outcome(data["outcome"]) if data.get("outcome") else None,
            effect_at=data.get("effect_at"),
            applied=bool(data.get("applied", False)),
            superseded=bool(data.get("superseded", False)),
            recovery_note=data.get("recovery_note"),
        )
