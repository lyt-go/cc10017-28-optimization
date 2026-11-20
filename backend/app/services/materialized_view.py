"""物化查询业务规则：状态流转、字段校验与筛选口径都收在这里。

物化查询侧对接下沉分发批次结果：
- 读取分发批次结果并确认后续批次使用的视图刷新版本（refresh_version）；
- 跨模块传递时物化查询与下沉分发分别保留分发批次结果的来源记录（source_record），
  两侧都能回溯到同一条 sink 批次，避免链路断链。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "materialized_view"
LIST_FIELDS = ["视图编号", "查询SQL", "基表名", "刷新模式", "刷新间隔", "存储位置", "数据量", "最后刷新"]
REQUIRED_FIELDS = ["视图编号", "查询SQL", "基表名"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用刷新": "已启用", "停用视图": "已停用"}
NEGATIVE_ACTIONS = ["停用视图"]

# 物化查询侧保留的分发批次来源记录表（与下沉分发的 sink_dispatch_batches 分别留存）
MV_BATCH_SOURCE_TABLE = "materialized_batch_sources"
MV_REFRESH_VERSION_TABLE = "materialized_refresh_versions"


class MaterializedViewService:
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
            rows = [row for row in rows if keyword.casefold() in str(row.get("视图编号", "")).casefold()]
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
            return None, f"物化视图 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于物化查询可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"物化视图已{action}"

    # ───────────────────────────────
    # 分发批次结果接入与刷新版本确认
    # ───────────────────────────────

    def confirm_batch_refresh(self, mv_id: int, batch_id: str) -> tuple[dict[str, Any] | None, str]:
        """物化查询模块核对一条下沉分发批次结果，并确认后续批次使用的视图刷新版本。"""
        view = store.find(MODULE, mv_id)
        if view is None:
            return None, f"物化视图 {mv_id} 不存在或已归档"

        batch = self._find_sink_batch(batch_id)
        if batch is None:
            return None, f"分发批次 {batch_id} 不存在，无法核对刷新版本"

        # 仅当视图基表与批次湖表一致时才允许确认，防止跨表串版本
        if view.get("基表名") and batch.get("lake_table") and view.get("基表名") != batch.get("lake_table"):
            return None, (
                f"视图基表 {view.get('基表名')} 与分发批次湖表 {batch.get('lake_table')} 不一致，"
                "不能确认刷新版本"
            )

        source_record = {
            "module": MODULE,
            "mv_id": mv_id,
            "mv_code": view.get("视图编号"),
            "base_table": view.get("基表名"),
            "sink_batch_id": batch_id,
            "sink_rule_id": batch.get("rule_id"),
            "sink_result_version": batch.get("result_version"),
            "confirmed_at": datetime.now().isoformat(),
        }
        store.rows(MV_BATCH_SOURCE_TABLE).append(dict(source_record))

        refresh_seq = 1 + sum(
            1 for row in store.rows(MV_REFRESH_VERSION_TABLE) if int(row.get("mv_id", -1)) == int(mv_id)
        )
        refresh_version = f"MV-{view.get('视图编号')}-R{refresh_seq:03d}"
        version_record = {
            "id": max((int(row.get("id", 0)) for row in store.rows(MV_REFRESH_VERSION_TABLE)), default=0) + 1,
            "mv_id": mv_id,
            "mv_code": view.get("视图编号"),
            "base_table": view.get("基表名"),
            "refresh_version": refresh_version,
            "sink_batch_id": batch_id,
            "sink_result_version": batch.get("result_version"),
            "conflict_policy": batch.get("conflict_policy"),
            "success_key_count": len(batch.get("success_keys", [])),
            "failed_key_count": len(batch.get("failed_keys", [])),
            "rollback_range_count": len(batch.get("rollback_ranges", [])),
            "batch_status": batch.get("status"),
            "source_record": source_record,
            "confirmed_at": datetime.now().isoformat(),
        }
        store.rows(MV_REFRESH_VERSION_TABLE).append(version_record)

        # 回写到视图条目，方便页面直接看到当前刷新版本与来源批次
        view["最后刷新"] = version_record["confirmed_at"]
        view["_refresh_version"] = refresh_version
        view["_source_batch_id"] = batch_id
        return version_record, ""

    def list_refresh_versions(self, mv_id: int | None = None) -> list[dict[str, Any]]:
        """读取视图刷新版本确认记录（含分发批次来源）。"""
        rows = store.rows(MV_REFRESH_VERSION_TABLE)
        if mv_id is None:
            return list(rows)
        return [row for row in rows if int(row.get("mv_id", -1)) == int(mv_id)]

    def list_batch_sources(self, mv_id: int | None = None) -> list[dict[str, Any]]:
        """物化查询侧留存的分发批次来源记录。"""
        rows = store.rows(MV_BATCH_SOURCE_TABLE)
        if mv_id is None:
            return list(rows)
        return [row for row in rows if int(row.get("mv_id", -1)) == int(mv_id)]

    def _find_sink_batch(self, batch_id: str) -> dict[str, Any] | None:
        for row in store.rows("sink_dispatch_batches"):
            if row.get("batch_id") == batch_id:
                return row
        return None
