"""下沉分发批次结果测试：成功键/失败键/回退范围、恢复、单次提交与跨模块刷新版本。"""
from __future__ import annotations

import unittest

from app.services.materialized_view import MaterializedViewService
from app.services.sink_dispatch import (
    BATCH_TABLE,
    COMMITTED_TABLE,
    SinkDispatchService,
)
from app.store import store


class SinkDispatchBatchTest(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例从干净的产物表开始，避免用例间串数据
        store.rows(BATCH_TABLE).clear()
        store.rows(COMMITTED_TABLE).clear()
        store.rows("materialized_refresh_versions").clear()
        store.rows("materialized_batch_sources").clear()
        self.svc = SinkDispatchService()
        self.mv_svc = MaterializedViewService()

    def test_batch_keeps_success_failed_and_rollback_ranges(self) -> None:
        """批次结果同时保留成功键、失败键与回退范围，且不再整表回退。"""
        keys = ["k1", "k2", "k3", "k4", "k5"]
        batch, message = self.svc.submit_batch(1, keys=keys, bad_keys=["k2", "k4"])
        self.assertEqual(message, "")
        self.assertIsNotNone(batch)
        self.assertEqual(batch["success_keys"], ["k1", "k3", "k5"])
        self.assertEqual(batch["failed_keys"], ["k2", "k4"])
        # k2、k4 在键序中不连续，应压成两个回退范围
        ranges = batch["rollback_ranges"]
        self.assertEqual(len(ranges), 2)
        self.assertEqual(ranges[0]["start_key"], "k2")
        self.assertEqual(ranges[0]["end_key"], "k2")
        self.assertEqual(ranges[1]["start_key"], "k4")
        self.assertEqual(ranges[1]["end_key"], "k4")
        self.assertFalse(batch["full_table_rollback"])
        self.assertEqual(batch["status"], "待恢复")

    def test_contiguous_bad_keys_compress_into_one_range(self) -> None:
        """连续失败键界定为同一个回退范围。"""
        keys = ["a", "b", "c", "d"]
        batch, _ = self.svc.submit_batch(1, keys=keys, bad_keys=["b", "c"])
        self.assertEqual(len(batch["rollback_ranges"]), 1)
        rng = batch["rollback_ranges"][0]
        self.assertEqual((rng["offset_start"], rng["offset_end"]), (1, 2))
        self.assertEqual(rng["start_key"], "b")
        self.assertEqual(rng["end_key"], "c")

    def test_success_key_committed_only_once(self) -> None:
        """成功键写入已提交键台账；同批失败后重试，成功键不会再次写入。"""
        keys = ["k1", "k2", "k3"]
        first, _ = self.svc.submit_batch(1, keys=keys, bad_keys=["k2"])
        self.assertEqual(first["success_keys"], ["k1", "k3"])
        ledger = self.svc.list_committed_keys(1)
        self.assertEqual(sorted(row["key"] for row in ledger), ["k1", "k3"])

        # 再次提交同一批键（含已成功键与坏键）：成功键应被跳过而不是重新写入
        second, _ = self.svc.submit_batch(1, keys=keys, bad_keys=["k2"])
        self.assertEqual(second["success_keys"], [])
        self.assertEqual(second["skipped_committed_keys"], ["k1", "k3"])
        self.assertEqual(second["failed_keys"], ["k2"])
        # 台账仍然只有 k1、k3 各一条
        ledger_after = self.svc.list_committed_keys(1)
        self.assertEqual(sorted(row["key"] for row in ledger_after), ["k1", "k3"])
        self.assertEqual(len(ledger_after), 2)

    def test_recover_only_replays_unfinished_range(self) -> None:
        """中断恢复：仅回退失败键界定的未完成范围，已成功键不重放。"""
        keys = ["k1", "k2", "k3", "k4"]
        interrupted, _ = self.svc.submit_batch(
            1, keys=keys, bad_keys=["k2"], interrupted=True)
        self.assertTrue(interrupted["interrupted"])
        self.assertEqual(interrupted["status"], "待恢复")
        committed_after_interrupt = set(
            row["key"] for row in self.svc.list_committed_keys(1))

        recovered, _ = self.svc.recover_batch(interrupted["batch_id"], bad_keys=["k2"])
        self.assertEqual(recovered["status"], "待恢复")
        self.assertEqual(recovered["attempt"], 2)
        # 中断前已成功的键仍然只在台账里出现一次，未被回退重放
        for key in committed_after_interrupt:
            rows = [row for row in self.svc.list_committed_keys(1) if row["key"] == key]
            self.assertEqual(len(rows), 1, f"成功键 {key} 被重复写入")
        # 累计成功键仍包含中断前的成功键
        for key in committed_after_interrupt:
            self.assertIn(key, recovered["success_keys"])

    def test_conflict_policies_produce_different_versions(self) -> None:
        """两种冲突判定做法改变处理次序，进而改变分发批次结果的当前版本。"""
        keys = ["k1", "k2", "k3"]
        bad_first, _ = self.svc.submit_batch(
            1, keys=keys, bad_keys=["k2"], conflict_policy="retry_bad_key_first")
        new_first, _ = self.svc.submit_batch(
            2, keys=keys, bad_keys=["k2"], conflict_policy="advance_new_key_first")
        self.assertNotEqual(
            bad_first["result_version"], new_first["result_version"])
        # 两种做法都保证成功键只提交一次：各自台账里成功键不重复
        self.assertEqual(len(self.svc.list_committed_keys(1)), 2)
        self.assertEqual(len(self.svc.list_committed_keys(2)), 2)

    def test_recover_with_incoming_new_keys_respects_policy_order(self) -> None:
        """恢复时同窗口新键到达：坏键优先与新键优先给出不同的可见提交次序。

        用两条独立规则模拟同一时间窗口的两种做法，避免同一批次被连续恢复后
        成功键已进台账而掩盖次序差异。
        """
        base_retry, _ = self.svc.submit_batch(
            1, keys=["k1", "k2"], bad_keys=["k2"], interrupted=True)
        base_advance, _ = self.svc.submit_batch(
            2, keys=["k1", "k2"], bad_keys=["k2"], interrupted=True)

        retry_first, _ = self.svc.recover_batch(
            base_retry["batch_id"], new_keys=["n1"], bad_keys=["k2"],
            conflict_policy="retry_bad_key_first")
        advance_first, _ = self.svc.recover_batch(
            base_advance["batch_id"], new_keys=["n1"], bad_keys=["k2"],
            conflict_policy="advance_new_key_first")
        # 坏键仍失败、新键成功；两种做法版本不同，但 n1 在各自规则下只提交一次
        self.assertEqual(retry_first["failed_keys"], ["k2"])
        self.assertEqual(advance_first["failed_keys"], ["k2"])
        self.assertIn("n1", retry_first["success_keys"])
        self.assertIn("n1", advance_first["success_keys"])
        self.assertNotEqual(
            retry_first["result_version"], advance_first["result_version"])
        n1_retry = [row for row in self.svc.list_committed_keys(1) if row["key"] == "n1"]
        n1_advance = [row for row in self.svc.list_committed_keys(2) if row["key"] == "n1"]
        self.assertEqual(len(n1_retry), 1)
        self.assertEqual(len(n1_advance), 1)
        # 版本历史保留了中断批次与恢复批次两个版本
        self.assertGreaterEqual(len(retry_first["version_history"]), 2)

    def test_recover_completed_batch_is_rejected(self) -> None:
        """已完成批次无需恢复。"""
        batch, _ = self.svc.submit_batch(1, keys=["k1", "k2"], bad_keys=[])
        self.assertEqual(batch["status"], "已完成")
        again, message = self.svc.recover_batch(batch["batch_id"])
        self.assertIsNone(again)
        self.assertIn("已完成", message)

    def test_interrupted_recovery_does_not_refail_unfinished_good_keys(self) -> None:
        """中断恢复：未处理的好键重试时成功，只有真正的坏键继续失败。"""
        keys = ["k1", "k2", "k3", "k4"]
        interrupted, _ = self.svc.submit_batch(
            1, keys=keys, bad_keys=["k2"], interrupted=True)
        # 中断批次：k2 是坏键，其余失败键是尚未处理的好键
        self.assertEqual(interrupted["bad_keys"], ["k2"])
        self.assertIn("k2", interrupted["failed_keys"])
        self.assertTrue(any(k != "k2" for k in interrupted["failed_keys"]))

        # 恢复时不显式传坏键：沿用批次记录的坏键，好键应当全部成功
        recovered, _ = self.svc.recover_batch(interrupted["batch_id"])
        self.assertEqual(recovered["failed_keys"], ["k2"])
        # 之前未处理的好键现在都进入成功键
        for key in keys:
            if key != "k2":
                self.assertIn(key, recovered["success_keys"])

    def test_full_recovery_completes_batch(self) -> None:
        """坏键被修复后再次恢复，批次走到已完成且回退范围清空。"""
        keys = ["k1", "k2", "k3"]
        interrupted, _ = self.svc.submit_batch(
            1, keys=keys, bad_keys=["k2"], interrupted=True)
        # 本次恢复声明 k2 已修复（不再是坏键）
        recovered, _ = self.svc.recover_batch(interrupted["batch_id"], bad_keys=[])
        self.assertEqual(recovered["status"], "已完成")
        self.assertEqual(recovered["failed_keys"], [])
        self.assertEqual(recovered["rollback_ranges"], [])
        # 三个键都只提交一次
        self.assertEqual(sorted(row["key"] for row in self.svc.list_committed_keys(1)),
                         ["k1", "k2", "k3"])

    def test_invalid_conflict_policy_rejected(self) -> None:
        batch, message = self.svc.submit_batch(1, keys=["k1"], conflict_policy="nope")
        self.assertIsNone(batch)
        self.assertIn("冲突判定做法", message)

    def test_mv_confirms_refresh_version_from_batch(self) -> None:
        """物化查询核对分发批次结果并确认刷新版本，两侧各留一份来源记录。"""
        # 种子中 MATE-0001 基表 order_agg_hourly，与 SINK-0001 湖表一致
        batch, _ = self.svc.submit_batch(
            1, keys=["k1", "k2"], bad_keys=["k2"])
        version, message = self.mv_svc.confirm_batch_refresh(1, batch["batch_id"])
        self.assertEqual(message, "")
        self.assertTrue(version["refresh_version"].startswith("MV-MATE-0001-R"))
        self.assertEqual(version["sink_batch_id"], batch["batch_id"])
        self.assertEqual(version["sink_result_version"], batch["result_version"])

        # 物化查询侧来源记录
        sources = self.mv_svc.list_batch_sources(1)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["sink_batch_id"], batch["batch_id"])
        # 下沉分发侧批次结果也保留自己的来源记录
        stored_batch = self.svc.get_batch(batch["batch_id"])
        self.assertEqual(stored_batch["source_record"]["module"], "sink_dispatch")
        self.assertEqual(stored_batch["source_record"]["rule_id"], 1)

    def test_mv_rejects_cross_table_batch(self) -> None:
        """基表与批次湖表不一致时不能确认刷新版本。"""
        # MATE-0002 基表 user_funnel_daily，而规则 1 的湖表是 order_agg_hourly
        batch, _ = self.svc.submit_batch(1, keys=["k1"])
        version, message = self.mv_svc.confirm_batch_refresh(2, batch["batch_id"])
        self.assertIsNone(version)
        self.assertIn("不一致", message)

    def test_batches_survive_reload_like_page_switch(self) -> None:
        """批次结果写入共享仓库，切换页面（重新读取）后仍能取回同一结果。"""
        batch, _ = self.svc.submit_batch(1, keys=["k1", "k2"], bad_keys=["k2"])
        # 用一个新的 service 实例模拟页面重新挂载
        fresh_svc = SinkDispatchService()
        listed = fresh_svc.list_batches(1)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["batch_id"], batch["batch_id"])
        self.assertEqual(listed[0]["failed_keys"], ["k2"])


if __name__ == "__main__":
    unittest.main()
