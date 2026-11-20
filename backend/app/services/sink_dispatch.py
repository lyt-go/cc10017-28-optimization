"""下沉分发业务规则：状态流转、字段校验与筛选口径都收在这里。

分发批次优化（替代旧的整表重试口径）：
- 读取分发规则后按规则的「批次大小」切分待推送键，逐条判定成功键/失败键（坏键）；
- 批次结果同时保留成功键、失败键与需要回退的范围，单条坏键不再整表重试、不阻塞后续批次；
- 已提交键台账（sink_committed_keys）保证每个成功键只提交一次，同批失败后重试也不会再次写入；
- 任务中断后按批次结果恢复：仅回退未完成范围（失败键界定的区间），成功键不回退、不重放；
- 冲突判定支持两种做法（坏键优先 / 新键优先），会改变分发批次结果的当前版本。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "sink_dispatch"
LIST_FIELDS = ["规则编号", "湖表名", "目标系统", "写入模式", "字段映射", "过滤条件", "批次大小", "失败回退"]
REQUIRED_FIELDS = ["规则编号", "湖表名", "目标系统"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
NEGATIVE_ACTIONS = ["停用规则"]

# 分发批次结果产物表与已提交键台账
BATCH_TABLE = "sink_dispatch_batches"
COMMITTED_TABLE = "sink_committed_keys"

# 冲突判定的两种做法：
# - retry_bad_key_first：坏键失败与新键到达同窗口相遇时，先重试坏键，再推进后续新键；
# - advance_new_key_first：先推进后续新键，再回头重试坏键。
# 两种做法下成功键都只提交一次，但提交次序不同，因此批次结果的当前版本不同。
CONFLICT_POLICIES = ("retry_bad_key_first", "advance_new_key_first")
POLICY_LABELS = {
    "retry_bad_key_first": "坏键优先重试",
    "advance_new_key_first": "新键优先推进",
}


class SinkDispatchService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("规则编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"分发规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于下沉分发可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"分发规则已{action}"

    # ───────────────────────────────
    # 分发批次：按成功键/失败键生成回退范围
    # ───────────────────────────────

    def submit_batch(
        self,
        rule_id: int,
        *,
        keys: list[str] | None = None,
        bad_keys: list[str] | None = None,
        conflict_policy: str = "retry_bad_key_first",
        batch_size: int | None = None,
        key_count: int | None = None,
        interrupted: bool = False,
    ) -> tuple[dict[str, Any] | None, str]:
        """读取一条分发规则并生成分发批次结果。

        返回的批次结果保留成功键、失败键与回退范围；interrupted=True 模拟任务中断，
        未完成范围由失败键界定并标记为待恢复，而不是整表回退。
        未显式给键时按 key_count 生成演示键（默认 5 个），真实链路由调用方传入实际键。
        """
        rule = store.find(MODULE, rule_id)
        if rule is None:
            return None, f"分发规则 {rule_id} 不存在或已归档"
        if conflict_policy not in CONFLICT_POLICIES:
            return None, f"冲突判定做法「{conflict_policy}」不受支持"

        effective_size = self._resolve_batch_size(rule, batch_size)
        demo_count = self._resolve_key_count(key_count)
        candidate_keys = self._build_candidate_keys(rule, keys, bad_keys, demo_count)
        bad_key_set = {str(k) for k in (bad_keys or [])}

        # 已提交键先剔除：成功键只能提交一次，同批失败重试也不会再次写入
        committed = self._committed_key_set(rule_id)
        skipped_keys = [key for key in candidate_keys if key in committed]
        pending_keys = [key for key in candidate_keys if key not in committed]

        success_keys: list[str] = []
        failed_keys: list[str] = []
        # 中断场景：模拟只处理到批次中段，剩余键进入未完成范围
        cutoff = len(pending_keys)
        if interrupted and len(pending_keys) > 1:
            cutoff = max(1, len(pending_keys) // 2)
        processed = pending_keys[:cutoff]
        unfinished_keys = pending_keys[cutoff:]

        for key in processed:
            if key in bad_key_set:
                failed_keys.append(key)
            else:
                success_keys.append(key)
        # 中断时尚未处理的键也并入失败/未完成口径，由回退范围界定
        failed_keys.extend(unfinished_keys)

        batch = self._new_batch_record(rule, rule_id)
        self._commit_keys(rule_id, success_keys, batch["batch_id"])
        rollback_ranges = self._build_rollback_ranges(pending_keys, failed_keys)
        outcomes = self._ordered_outcomes(pending_keys, failed_keys)
        result_version = self._result_version(1, conflict_policy, outcomes)
        batch.update({
            "conflict_policy": conflict_policy,
            "conflict_policy_label": POLICY_LABELS[conflict_policy],
            "batch_size": effective_size,
            "candidate_keys": list(candidate_keys),
            # 真正的坏键单独留存：中断批次的 failed_keys 还混有未处理的好键，
            # 恢复时只能把这里记录的坏键继续判失败，不能把未处理好键也当坏键
            "bad_keys": sorted(bad_key_set),
            "skipped_committed_keys": skipped_keys,
            "success_keys": success_keys,
            "failed_keys": failed_keys,
            "rollback_ranges": rollback_ranges,
            "full_table_rollback": False,
            "interrupted": interrupted,
            "status": "待恢复" if (failed_keys or interrupted) else "已完成",
            "attempt": 1,
            "committed_total": len(self._committed_key_set(rule_id)),
            "result_version": result_version,
            "version_history": [
                {"attempt": 1, "conflict_policy": conflict_policy, "result_version": result_version}
            ],
            "source_record": {
                "module": MODULE,
                "rule_id": rule_id,
                "rule_code": rule.get("规则编号"),
                "lake_table": rule.get("湖表名"),
                "target_system": rule.get("目标系统"),
            },
        })
        store.rows(BATCH_TABLE).append(batch)
        rule["_last_batch"] = {"batch_id": batch["batch_id"], "status": batch["status"]}
        return batch, ""

    def recover_batch(
        self,
        batch_id: str,
        *,
        new_keys: list[str] | None = None,
        conflict_policy: str | None = None,
        bad_keys: list[str] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """任务中断后恢复分发批次结果。

        仅回退并重放未完成范围（由失败键界定），已成功键依赖已提交键台账跳过；
        同窗口到达的后续新键按冲突判定做法决定先重试坏键还是先推进新键。
        """
        batch = self._find_batch(batch_id)
        if batch is None:
            return None, f"分发批次 {batch_id} 不存在"
        if batch["status"] == "已完成":
            return None, f"分发批次 {batch_id} 已完成，无需恢复"

        policy = conflict_policy or batch.get("conflict_policy", "retry_bad_key_first")
        if policy not in CONFLICT_POLICIES:
            return None, f"冲突判定做法「{policy}」不受支持"

        rule_id = int(batch["rule_id"])
        rule = store.find(MODULE, rule_id)
        if rule is None:
            return None, f"分发规则 {rule_id} 不存在或已归档"

        retry_keys = list(batch.get("failed_keys", []))
        incoming_keys = [str(k) for k in (new_keys or [])]
        # bad_keys 为 None 表示调用方未声明，沿用批次记录的真正坏键；
        # 显式传空列表表示坏键已全部修复，本次没有坏键。
        if bad_keys is None:
            recorded_bad = batch.get("bad_keys")
            bad_key_set = {str(k) for k in recorded_bad} if recorded_bad is not None else set(retry_keys)
        else:
            bad_key_set = {str(k) for k in bad_keys}

        # 冲突判定的两种做法改变处理次序，进而改变当前版本
        if policy == "retry_bad_key_first":
            ordered_keys = retry_keys + incoming_keys
        else:
            ordered_keys = incoming_keys + retry_keys

        committed = self._committed_key_set(rule_id)
        skipped_keys = [key for key in ordered_keys if key in committed]
        pending_keys = [key for key in ordered_keys if key not in committed]

        success_keys: list[str] = []
        failed_keys: list[str] = []
        for key in pending_keys:
            if key in bad_key_set:
                failed_keys.append(key)
            else:
                success_keys.append(key)
        self._commit_keys(rule_id, success_keys, batch_id)

        rollback_ranges = self._build_rollback_ranges(pending_keys, failed_keys)
        attempt = int(batch.get("attempt", 1)) + 1
        outcomes = self._ordered_outcomes(pending_keys, failed_keys)
        result_version = self._result_version(attempt, policy, outcomes)

        batch["conflict_policy"] = policy
        batch["conflict_policy_label"] = POLICY_LABELS[policy]
        batch["bad_keys"] = sorted(bad_key_set)
        batch["skipped_committed_keys"] = list(batch.get("skipped_committed_keys", [])) + skipped_keys
        # 历史成功键保留在台账里，批次结果累计本次与既往成功键（去重，保持首次提交次序）
        accumulated_success = list(dict.fromkeys([*batch.get("success_keys", []), *success_keys]))
        batch["success_keys"] = accumulated_success
        batch["failed_keys"] = failed_keys
        batch["rollback_ranges"] = rollback_ranges
        batch["full_table_rollback"] = False
        batch["interrupted"] = False
        batch["status"] = "待恢复" if failed_keys else "已完成"
        batch["attempt"] = attempt
        batch["recovered_at"] = datetime.now().isoformat()
        batch["incoming_new_keys"] = incoming_keys
        batch["committed_total"] = len(self._committed_key_set(rule_id))
        batch["result_version"] = result_version
        history = list(batch.get("version_history", []))
        history.append({"attempt": attempt, "conflict_policy": policy, "result_version": result_version})
        batch["version_history"] = history
        if rule is not None:
            rule["_last_batch"] = {"batch_id": batch_id, "status": batch["status"]}
        return batch, ""

    def list_batches(self, rule_id: int | None = None) -> list[dict[str, Any]]:
        """读取分发批次结果；可按规则过滤，切换页面后仍能取回同一批结果。"""
        rows = store.rows(BATCH_TABLE)
        if rule_id is None:
            return list(rows)
        return [row for row in rows if int(row.get("rule_id", -1)) == int(rule_id)]

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        return self._find_batch(batch_id)

    def list_committed_keys(self, rule_id: int | None = None) -> list[dict[str, Any]]:
        """已提交键台账：核对每个成功键只提交一次。"""
        rows = store.rows(COMMITTED_TABLE)
        if rule_id is None:
            return list(rows)
        return [row for row in rows if int(row.get("rule_id", -1)) == int(rule_id)]

    # ───────────────────────────────
    # 内部辅助
    # ───────────────────────────────

    def _resolve_batch_size(self, rule: dict[str, Any], override: int | None) -> int:
        if override and int(override) > 0:
            return int(override)
        try:
            size = int(rule.get("批次大小") or 100)
        except (TypeError, ValueError):
            size = 100
        return max(1, size)

    def _resolve_key_count(self, override: int | None) -> int:
        if override is None:
            return 5
        try:
            count = int(override)
        except (TypeError, ValueError):
            count = 5
        return min(100, max(1, count))

    def _build_candidate_keys(
        self,
        rule: dict[str, Any],
        keys: list[str] | None,
        bad_keys: list[str] | None,
        key_count: int,
    ) -> list[str]:
        if keys:
            return [str(key) for key in keys]
        # 未显式给键时，按规则编号生成一批演示键，并保证坏键落在候选集合内
        rule_code = str(rule.get("规则编号", "RULE"))
        generated = [f"{rule_code}-K{index:03d}" for index in range(1, key_count + 1)]
        for bad in bad_keys or []:
            if str(bad) not in generated:
                generated.append(str(bad))
        return generated

    def _committed_key_set(self, rule_id: int) -> set[str]:
        return {
            str(row.get("key"))
            for row in store.rows(COMMITTED_TABLE)
            if int(row.get("rule_id", -1)) == int(rule_id)
        }

    def _commit_keys(self, rule_id: int, keys: list[str], batch_id: str) -> None:
        """写入已提交键台账；已存在的键不会再次写入（成功键只提交一次）。"""
        ledger = store.rows(COMMITTED_TABLE)
        existing = self._committed_key_set(rule_id)
        now = datetime.now().isoformat()
        for key in keys:
            if key in existing:
                continue
            ledger.append({
                "id": max((int(row.get("id", 0)) for row in ledger), default=0) + 1,
                "rule_id": rule_id,
                "key": key,
                "first_batch_id": batch_id,
                "committed_at": now,
            })
            existing.add(key)

    def _build_rollback_ranges(
        self, ordered_keys: list[str], failed_keys: list[str]
    ) -> list[dict[str, Any]]:
        """按失败键在本批键序中的位置，把需要回退的范围压成连续区间。"""
        if not failed_keys:
            return []
        failed_set = set(failed_keys)
        indexes = sorted(index for index, key in enumerate(ordered_keys) if key in failed_set)
        ranges: list[dict[str, Any]] = []
        start = prev = indexes[0]
        for index in indexes[1:]:
            if index == prev + 1:
                prev = index
                continue
            ranges.append(self._range_record(ordered_keys, start, prev))
            start = prev = index
        ranges.append(self._range_record(ordered_keys, start, prev))
        return ranges

    def _range_record(self, ordered_keys: list[str], start: int, end: int) -> dict[str, Any]:
        return {
            "offset_start": start,
            "offset_end": end,
            "start_key": ordered_keys[start],
            "end_key": ordered_keys[end],
            "scope": "未完成范围（仅失败键界定，不含已成功键）",
        }

    def _new_batch_record(self, rule: dict[str, Any], rule_id: int) -> dict[str, Any]:
        seq = self._next_seq(rule_id)
        return {
            "batch_id": f"BATCH-{rule_id}-{seq}",
            "rule_id": rule_id,
            "rule_code": rule.get("规则编号"),
            "lake_table": rule.get("湖表名"),
            "target_system": rule.get("目标系统"),
            "write_mode": rule.get("写入模式"),
            "created_at": datetime.now().isoformat(),
        }

    def _next_seq(self, rule_id: int) -> int:
        return 1 + sum(1 for row in store.rows(BATCH_TABLE) if int(row.get("rule_id", -1)) == int(rule_id))

    def _find_batch(self, batch_id: str) -> dict[str, Any] | None:
        for row in store.rows(BATCH_TABLE):
            if row.get("batch_id") == batch_id:
                return row
        return None

    def _ordered_outcomes(
        self, ordered_keys: list[str], failed_keys: list[str]
    ) -> list[tuple[str, str]]:
        """按实际处理次序记录每个键的结果，用于生成可区分提交次序的版本号。"""
        failed_set = set(failed_keys)
        return [(key, "fail" if key in failed_set else "ok") for key in ordered_keys]

    def _result_version(
        self, attempt: int, conflict_policy: str, outcomes: list[tuple[str, str]]
    ) -> str:
        # 版本号体现冲突判定做法与提交次序：处理次序或成败序列不同，当前版本就不同。
        # 校验和纯手工计算，避免内置 hash 的随机盐导致跨进程版本号不稳定。
        checksum = 0
        for key, outcome in outcomes:
            for char in f"{key}:{outcome}|":
                checksum = (checksum * 31 + ord(char)) % 1_000_000
        return f"v{attempt}.{conflict_policy}.{checksum:06d}"
