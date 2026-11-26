"""基于两份合成事件的端到端判定验证。"""

import unittest
from pathlib import Path

from sample_reader import load_inventory, load_timeline

from duty.engine import DutyEngine
from duty.model import OperationStatus, Verdict

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def build(timeline_name: str) -> DutyEngine:
    inventory = load_inventory(SAMPLES)
    engine = DutyEngine(inventory)
    records = list(load_timeline(SAMPLES / timeline_name))
    engine.ingest_file(records)
    return engine


class ShiftLogTests(unittest.TestCase):
    """shift-log：blue 分区经历 quorum 不足、证据冲突、重启与恢复。"""

    def setUp(self):
        self.engine = build("shift-log.jsonl")
        self.ops = self.engine.operations

    def test_early_renew_passes_with_two_domain_quorum(self):
        # t=12：u0(t1)+u1(t3) 两域，交集 [113,116] 整体早于到期点 120
        op = self.ops["a9"]
        self.assertEqual(op.verdict, Verdict.APPROVE)
        # 初始状态为 approved（最终 void 由后续回执决定）
        self.assertEqual(op.decided_at, 12)

    def test_conflict_and_boot_invalidate_renew_then_isolate(self):
        # t=15：t1 与 t2 同域且区间不相交 => 冲突隔离
        # 同一时刻 t2 观测的平移区间 [123,135] 已跨/过期点 120
        status = self.engine.partition_status(15)
        self.assertTrue(status["blue"]["isolated"])
        self.assertTrue(status["blue"]["conflict"])

    def test_boot_event_voids_old_observations(self):
        # 只接入到 boot 事件（前 6 条）：b1 重启到 boot=10 后，
        # 其 boot=9 的 t1/t2 观测全部作废，blue 仅剩 t3 一个域
        engine = DutyEngine(load_inventory(SAMPLES))
        records = list(load_timeline(SAMPLES / "shift-log.jsonl"))
        engine.ingest_file(records[:6])
        window = engine.trust_window("blue", 19)
        hosts = {obs.host for obs in window.eligible}
        self.assertNotIn("b1", hosts)
        self.assertFalse(window.quorum)
        self.assertEqual(window.rejected_boot, 2)

    def test_reclaim_suspended_when_expiry_unprovable(self):
        # t=19 b3 回收：仅剩 t3 一域，不能证实到期，席位不得交给别人
        op = self.ops["b3"]
        self.assertEqual(op.verdict, Verdict.SUSPEND)
        self.assertTrue(any("quorum_unmet" in r for r in op.reasons))

    def test_recovery_establishes_expiry_and_proves_seat_dead(self):
        # t=23：t3(u1)+t4(u2) 两域，交集 [139,141] 整体 >= 120 => 确证到期
        status = self.engine.partition_status(23)
        self.assertFalse(status["blue"]["isolated"])
        seat = self.engine.seat_status()[0]
        self.assertEqual(seat["seat"], "b11")
        self.assertEqual(seat["position_now"], "after")
        self.assertIn(4, seat["proven_expiry_generations"])

    def test_void_receipt_keeps_lease_but_renewal_stays_deniable(self):
        # a9 最终 void：续用从未生效；且 gen4 到期已被证明
        self.assertEqual(self.ops["a9"].status, OperationStatus.VOID)
        self.assertEqual(self.engine.seats["b11"].expires, 120)

    def test_late_committed_reclaim_does_not_change_new_ownership(self):
        # 样本时序里 b3 的 committed 先于 b5 推进到达，因此另构造时序：
        # 先完成 gen5 领取，旧主机 gen4 回收的 committed 再迟到
        from sample_reader import load_inventory
        engine = DutyEngine(load_inventory(SAMPLES))
        engine.ingest_file([
            {"at": 23, "kind": "observation", "source": "t3", "partition": "blue",
             "host": "b2", "boot": 4, "ticks": 50, "action_time": 22, "bounds": [138, 141]},
            {"at": 23, "kind": "observation", "source": "t4", "partition": "blue",
             "host": "b3", "boot": 6, "ticks": 71, "action_time": 22, "bounds": [139, 142]},
            {"at": 23, "kind": "intent", "operation": "b3", "seat": "b11",
             "generation": 4, "action": "reclaim", "holder": "work-a",
             "requested_expiry": None},
            {"at": 24, "kind": "intent", "operation": "b5", "seat": "b11",
             "generation": 5, "action": "claim", "holder": "work-c",
             "requested_expiry": 175},
        ])
        self.assertEqual(engine.operations["b5"].verdict, Verdict.APPROVE)
        engine.advance("b5")
        engine.ingest_record({
            "at": 25, "kind": "effect", "operation": "b5", "seat": "b11",
            "generation": 5, "outcome": "committed",
        })
        # 旧主机迟到的 gen4 回收提交通知
        engine.ingest_record({
            "at": 26, "kind": "effect", "operation": "b3", "seat": "b11",
            "generation": 4, "outcome": "committed",
        })
        seat = engine.seats["b11"]
        self.assertEqual(seat.holder, "work-c")
        self.assertEqual(seat.generation, 5)
        self.assertTrue(engine.operations["b3"].superseded)
        self.assertTrue(engine.operations["b5"].applied)

    def test_sample_timeline_committed_reclaim_releases_gen4(self):
        # 样本原时序：b3(gen4) 的 committed 在 b5 推进前到达，正常释放 gen4
        self.assertEqual(self.ops["b3"].status, OperationStatus.COMMITTED)
        self.assertTrue(self.ops["b3"].applied)
        self.assertIsNone(self.engine.seats["b11"].holder)
        # b5 仍可通过，推进后取得 gen5
        self.assertEqual(self.ops["b5"].verdict, Verdict.APPROVE)

    def test_recovery_buckets_match_two_kinds_of_backlog(self):
        # 全程结束时：a9 已 void（终态）；b3 的迟到 committed 被围栏（终态）；
        # b5 已通过待推进 => approve 桶；欠账里不含任何终态操作
        plan = self.engine.recover(apply=False)
        approve_ids = {item["operation"] for item in plan["approve"]}
        all_ids = {item["operation"] for bucket in plan.values() for item in bucket}
        self.assertIn("b5", approve_ids)
        self.assertNotIn("a9", all_ids)
        self.assertNotIn("b3", all_ids)
        # dry-run 不改变状态
        self.assertEqual(self.ops["b5"].status, OperationStatus.APPROVED)


class IndependentTimeTests(unittest.TestCase):
    """independent-time：green 分区始终证据冲突，全部动作被隔离。"""

    def setUp(self):
        self.engine = build("independent-time.jsonl")
        self.ops = self.engine.operations

    def test_single_domain_cannot_meet_quorum(self):
        # 只接入前两条观测（t<=1）：仅 u0 一个域
        engine = DutyEngine(load_inventory(SAMPLES))
        records = list(load_timeline(SAMPLES / "independent-time.jsonl"))
        engine.ingest_file(records[:2])
        status = engine.partition_status(2)
        self.assertTrue(status["green"]["isolated"])
        self.assertFalse(status["green"]["quorum"])

    def test_conflicting_windows_keep_partition_isolated(self):
        # t>=4：四个域都在，但 t1/t2 与 t3/t4 的区间互相不相交
        status = self.engine.partition_status(10)
        self.assertTrue(status["green"]["conflict"])
        self.assertTrue(status["green"]["isolated"])

    def test_all_intents_suspended_under_conflict(self):
        for op_id in ("c1", "c2", "c3"):
            self.assertEqual(self.ops[op_id].verdict, Verdict.SUSPEND)

    def test_committed_reclaim_releases_seat_but_claim_still_blocked(self):
        # c2 的 committed 回执是事实：gen2 席位被释放；
        # 但 c3 新领取仍因分区隔离而暂缓——权限不可核验，资源不能承诺
        self.assertTrue(self.ops["c2"].applied)
        self.assertIsNone(self.engine.seats["g18"].holder)
        self.assertEqual(self.ops["c3"].status, OperationStatus.SUSPENDED)

    def test_recovery_leaves_undecidable_work_for_human_review(self):
        plan = self.engine.recover(apply=False)
        # c1 从未发出且仍无可信时间 => waiting；无任何可自动补做项
        self.assertEqual(plan["redrive"], [])
        waiting_ids = {item["operation"] for item in plan["waiting"]}
        self.assertIn("c1", waiting_ids)
        self.assertIn("c3", waiting_ids)


if __name__ == "__main__":
    unittest.main()
