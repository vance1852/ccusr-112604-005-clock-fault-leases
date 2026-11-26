"""可信时间推断与分区隔离判定。

证据准入规则（与 README 约定一致）：
1. 观测必须覆盖目标分区；
2. 决策时刻距 action_time 不超过该分区 freshness（含边界秒）；
3. 主机发生 boot 变更后，其旧 boot 的观测不能再作证；
4. 准入观测按其可信域去重计数，独立域数量达到 ``source_domains_min``
   才足以作为期限依据；
5. 准入观测的平移区间取交集：交集为空即证据冲突，整体不可信；
6. 交集与期限的相对位置给出「未到期 / 已到期 / 跨过期点」三种结论，
   跨点同样保守处理。
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Observation


@dataclass(frozen=True)
class TrustWindow:
    """某分区在某决策时刻的可信时间结论。"""

    partition: str
    now: int
    eligible: tuple[Observation, ...]
    domains: frozenset[str]
    domains_min: int
    window: tuple[int, int] | None      # 准入区间交集，None 表示无法建立
    quorum: bool
    conflict: bool                       # 域数达标但区间交集为空
    rejected_stale: int = 0
    rejected_boot: int = 0
    rejected_wrong_partition: int = 0

    @property
    def trusted(self) -> bool:
        return self.quorum and not self.conflict and self.window is not None

    def position_vs(self, deadline: int) -> str:
        """可信区间相对某个期限点的位置。

        返回 before（整段在到期前）、after（整段在到期点或之后）、
        crossing（区间跨过到期点）、unknown（无可信时间）。
        """
        if not self.trusted:
            return "unknown"
        low, high = self.window  # type: ignore[misc]
        if high < deadline:
            return "before"
        if low >= deadline:
            return "after"
        return "crossing"


def evaluate_partition(
    partition: str,
    now: int,
    observations: list[Observation],
    boot_events: dict[str, tuple[int, int]],
    source_domain: dict[str, str],
    domains_min: int,
    freshness: int,
) -> TrustWindow:
    eligible: list[Observation] = []
    rejected_stale = 0
    rejected_boot = 0
    rejected_wrong_partition = 0
    for obs in observations:
        if obs.partition != partition:
            rejected_wrong_partition += 1
            continue
        current = boot_events.get(obs.host)
        if current is not None:
            current_boot, _boot_seen_at = current
            # boot 变更后该主机旧 boot 的观测不能当作新观测
            if obs.boot != current_boot:
                rejected_boot += 1
                continue
        if now - obs.action_time > freshness:
            rejected_stale += 1
            continue
        if obs.source not in source_domain:
            continue
        eligible.append(obs)

    domains = frozenset(source_domain[obs.source] for obs in eligible)
    quorum = len(domains) >= domains_min
    window: tuple[int, int] | None = None
    conflict = False
    if eligible:
        low = max(obs.shifted_bounds(now)[0] for obs in eligible)
        high = min(obs.shifted_bounds(now)[1] for obs in eligible)
        if low > high:
            conflict = True
        else:
            window = (low, high)
    return TrustWindow(
        partition=partition,
        now=now,
        eligible=tuple(eligible),
        domains=domains,
        domains_min=domains_min,
        window=window,
        quorum=quorum,
        conflict=conflict,
        rejected_stale=rejected_stale,
        rejected_boot=rejected_boot,
        rejected_wrong_partition=rejected_wrong_partition,
    )
