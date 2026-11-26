"""关键保守规则的合成单元测试（不依赖 samples 事件文件）。"""

import tempfile
import unittest
from pathlib import Path

from duty.engine import DutyEngine
from duty.model import OperationStatus, Verdict
from duty.store import DutyStore

INVENTORY = {
    "sources": [
        {"source": "t1", "domain": "u0"},
        {"source": "t2", "domain": "u1"},
        {"source": "t3", "domain": "u2"},
    ],
    "partitions": [
        {"partition": "blue", "source_domains_min": 2, "freshness": 10},
    ],
    "seats": [
        {"seat": "b1", "partition": "blue", "holder": "work-a",
         "generation": 1, "expires": 100},
    ],
}


def obs(at, action_time, source, bounds, host=None, boot=1, partition="blue"):
    return {
        "at": at, "kind": "observation", "source": source, "partition": partition,
        "host": host or f"h-{source}", "boot": boot, "ticks": 1,
        "action_time": action_time, "bounds": bounds,
    }


def intent(at, op, action, generation=1, holder="work-a", expiry=None):
    return {
        "at": at, "kind": "intent", "operation": op, "seat": "b1",
        "generation": generation, "action": action, "holder": holder,
        "requested_expiry": expiry,
    }


class RenewalMonotonicityTests(unittest.TestCase):
    def test_backward_clock_cannot_reopen_expired_renewal(self):
        engine = DutyEngine(INVENTORY)
        # t=5：两域一致，可信时间 [105,108] >= 100 => 确证到期
        engine.ingest_file([
            obs(5, 5, "t1", [105, 108]),
            obs(5, 5, "t2", [106, 109]),
        ])
        self.assertEqual(
            engine.trust_window("blue", 5).position_vs(100), "after"
        )
        # t=20：旧观测已超 freshness=10；机器被向后校时，
        # 新观测的可信时间退回 [90,94]（到期前）
        engine.ingest_file([
            obs(20, 20, "t1", [90, 94]),
            obs(20, 20, "t2", [91, 95]),
            intent(20, "r1", "renew"),
        ])
        op = engine.operations["r1"]
        self.assertEqual(op.verdict, Verdict.DENY)
        self.assertTrue(any("expiry_already_proven" in r for r in op.reasons))

    def test_renewal_before_expiry_approved(self):
        engine = DutyEngine(INVENTORY)
        engine.ingest_file([
            obs(5, 5, "t1", [90, 94]),
            obs(5, 5, "t2", [91, 95]),
            intent(5, "r1", "renew", expiry=140),
        ])
        self.assertEqual(engine.operations["r1"].verdict, Verdict.APPROVE)


class CrossingAndFreshnessTests(unittest.TestCase):
    def test_window_crossing_expiry_is_suspended(self):
        engine = DutyEngine(INVENTORY)
        engine.ingest_file([
            obs(5, 5, "t1", [96, 104]),
            obs(5, 5, "t2", [97, 105]),
            intent(5, "r1", "renew"),
        ])
        self.assertEqual(engine.operations["r1"].verdict, Verdict.SUSPEND)
        self.assertTrue(any("crosses_expiry" in r for r in engine.operations["r1"].reasons))

    def test_freshness_boundary_is_inclusive(self):
        engine = DutyEngine(INVENTORY)
        # action_time=0, freshness=10：t=10 仍有效，t=11 失效
        engine.ingest_file([obs(0, 0, "t1", [80, 84]), obs(0, 0, "t2", [81, 85])])
        self.assertTrue(engine.trust_window("blue", 10).trusted)
        window = engine.trust_window("blue", 11)
        self.assertFalse(window.trusted)
        self.assertEqual(window.rejected_stale, 2)

    def test_reclaim_requires_proof_of_expiry(self):
        engine = DutyEngine(INVENTORY)
        engine.ingest_file([
            obs(5, 5, "t1", [90, 94]),       # 到期前
            obs(5, 5, "t2", [91, 95]),
            intent(5, "q1", "reclaim"),
        ])
        # 不能证实到期 => 暂缓，席位不能交给另一个任务
        self.assertEqual(engine.operations["q1"].verdict, Verdict.SUSPEND)
        self.assertIsNotNone(engine.seats["b1"].holder)

    def test_conflicting_evidence_isolates_partition(self):
        engine = DutyEngine(INVENTORY)
        engine.ingest_file([
            obs(5, 5, "t1", [90, 94]),
            obs(5, 5, "t2", [120, 124]),     # 与 t1 不相交
            intent(5, "q1", "reclaim"),
        ])
        self.assertEqual(engine.operations["q1"].verdict, Verdict.SUSPEND)
        self.assertTrue(engine.partition_status(5)["blue"]["conflict"])


class GenerationFenceTests(unittest.TestCase):
    def _expired_blue(self, engine, at=5):
        engine.ingest_file([
            obs(at, at, "t1", [105, 108]),
            obs(at, at, "t2", [106, 109]),
        ])

    def test_stale_generation_denied(self):
        engine = DutyEngine(INVENTORY)
        self._expired_blue(engine)
        engine.ingest_file([intent(6, "old", "reclaim", generation=99)])
        self.assertEqual(engine.operations["old"].verdict, Verdict.DENY)
        self.assertTrue(any("stale_generation" in r for r in engine.operations["old"].reasons))

    def test_claim_cannot_skip_generation(self):
        engine = DutyEngine(INVENTORY)
        self._expired_blue(engine)
        engine.ingest_file([intent(6, "c1", "claim", generation=3, holder="work-b")])
        self.assertEqual(engine.operations["c1"].verdict, Verdict.DENY)

    def test_claim_then_late_old_generation_notice_keeps_new_owner(self):
        engine = DutyEngine(INVENTORY)
        self._expired_blue(engine)
        # gen1 回收与 gen2 领取先后通过
        engine.ingest_file([intent(6, "q1", "reclaim", generation=1)])
        self.assertEqual(engine.operations["q1"].verdict, Verdict.APPROVE)
        engine.ingest_file([intent(7, "c1", "claim", generation=2, holder="work-b")])
        self.assertEqual(engine.operations["c1"].verdict, Verdict.APPROVE)
        engine.advance("c1")
        engine.ingest_record({
            "at": 8, "kind": "effect", "operation": "c1", "seat": "b1",
            "generation": 2, "outcome": "committed",
        })
        self.assertEqual(engine.seats["b1"].holder, "work-b")
        # 旧主机迟到的 gen1 unknown/committed 通知不能改变新归属
        engine.ingest_record({
            "at": 9, "kind": "effect", "operation": "q1", "seat": "b1",
            "generation": 1, "outcome": "committed",
        })
        self.assertEqual(engine.seats["b1"].holder, "work-b")
        self.assertEqual(engine.seats["b1"].generation, 2)
        self.assertTrue(engine.operations["q1"].superseded)

    def test_claim_blocked_while_same_generation_claim_in_flight(self):
        engine = DutyEngine(INVENTORY)
        self._expired_blue(engine)
        engine.ingest_file([
            intent(6, "c1", "claim", generation=2, holder="work-b"),
            intent(7, "c2", "claim", generation=2, holder="work-c"),
        ])
        self.assertEqual(engine.operations["c1"].verdict, Verdict.APPROVE)
        self.assertEqual(engine.operations["c2"].verdict, Verdict.SUSPEND)
        self.assertTrue(any("resource_uncommittable" in r for r in engine.operations["c2"].reasons))


class RecoveryTests(unittest.TestCase):
    def test_unknown_outcome_renewal_after_expiry_goes_to_review(self):
        engine = DutyEngine(INVENTORY)
        # 到期前批准续用并派发，执行机只回 unknown
        engine.ingest_file([
            obs(5, 5, "t1", [90, 94]),
            obs(5, 5, "t2", [91, 95]),
            intent(5, "r1", "renew", expiry=140),
        ])
        engine.advance("r1")
        engine.ingest_record({
            "at": 6, "kind": "effect", "operation": "r1", "seat": "b1",
            "generation": 1, "outcome": "unknown",
        })
        # 恢复后可信时间显示已到期（旧观测已超 freshness，不参与交集）
        engine.ingest_file([
            obs(20, 20, "t1", [120, 124]),
            obs(20, 20, "t2", [121, 125]),
        ])
        plan = engine.recover(apply=False)
        review_ids = {item["operation"]: item["reason"] for item in plan["review"]}
        self.assertIn("r1", review_ids)
        self.assertIn("renew_uncertain", review_ids["r1"])
        # apply 后进入 review 终态
        engine.recover(apply=True)
        self.assertEqual(engine.operations["r1"].status, OperationStatus.REVIEW)

    def test_unknown_reclaim_after_proven_expiry_is_redrivable(self):
        engine = DutyEngine(INVENTORY)
        engine.ingest_file([
            obs(5, 5, "t1", [105, 108]),
            obs(5, 5, "t2", [106, 109]),
            intent(5, "q1", "reclaim"),
        ])
        engine.advance("q1")
        engine.ingest_record({
            "at": 6, "kind": "effect", "operation": "q1", "seat": "b1",
            "generation": 1, "outcome": "unknown",
        })
        plan = engine.recover(apply=False)
        self.assertIn("q1", {item["operation"] for item in plan["redrive"]})

    def test_never_sent_intent_auto_advances_after_recovery_apply(self):
        engine = DutyEngine(INVENTORY)
        # 起初只有一个域，回收暂缓（从未发出）
        engine.ingest_file([obs(5, 5, "t1", [105, 108]),
                            intent(5, "q1", "reclaim")])
        self.assertEqual(engine.operations["q1"].status, OperationStatus.SUSPENDED)
        # 第二个域到达，恢复后应自动推进（窗口取交集，整体仍在到期后）
        engine.ingest_file([obs(7, 7, "t2", [106, 107])])
        engine.recover(apply=True)
        self.assertEqual(engine.operations["q1"].status, OperationStatus.ADVANCED)


class PersistenceTests(unittest.TestCase):
    def test_restart_preserves_decisions_evidence_and_seat_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DutyStore(Path(tmp) / "state")
            engine = DutyEngine(INVENTORY)
            engine.attach_store(store)
            engine.ingest_file([
                obs(5, 5, "t1", [105, 108]),
                obs(5, 5, "t2", [106, 109]),
                intent(6, "q1", "reclaim"),
            ])
            engine.advance("q1")
            proven_before = dict(engine.proven_expiry)

            # 模拟中途停止服务后重新启动
            reopened = DutyEngine.open(DutyStore(Path(tmp) / "state"))
            self.assertEqual(reopened.operations["q1"].status, OperationStatus.ADVANCED)
            self.assertEqual(len(reopened.observations), 2)
            self.assertEqual(reopened.proven_expiry, proven_before)
            self.assertEqual(reopened.seats["b1"].expires, 100)
            # 日志重放与快照完全一致
            self.assertEqual(reopened.verify_journal(), {})

    def test_replay_from_journal_without_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "state"
            store = DutyStore(directory)
            engine = DutyEngine(INVENTORY)
            engine.attach_store(store)
            engine.ingest_file([
                obs(5, 5, "t1", [90, 94]),
                obs(5, 5, "t2", [91, 95]),
                intent(5, "r1", "renew", expiry=140),
            ])
            (directory / "snapshot.json").unlink()
            reopened = DutyEngine.open(DutyStore(directory))
            self.assertEqual(reopened.operations["r1"].verdict, Verdict.APPROVE)
            self.assertEqual(reopened.seats["b1"].expires, 100)

    def test_out_of_order_ingestion_rejected(self):
        engine = DutyEngine(INVENTORY)
        engine.ingest_file([obs(10, 10, "t1", [90, 94])])
        with self.assertRaises(ValueError):
            engine.ingest_file([obs(5, 5, "t2", [91, 95])])


if __name__ == "__main__":
    unittest.main()
