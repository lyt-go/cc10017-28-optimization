"""下沉分发接口：维护分发规则，覆盖提交校验、标记生效、停用规则等动作。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.sink_dispatch import SinkDispatchService

router = APIRouter(prefix="/api/sink_dispatch", tags=["下沉分发"])

service = SinkDispatchService()

LIST_FIELDS = ["规则编号", "湖表名", "目标系统", "写入模式", "字段映射", "过滤条件", "批次大小", "失败回退"]
STATUSES = ["草稿", "待校验", "已生效", "已停用"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


def _as_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已生效、已停用"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、规则编号关键字与状态过滤下沉分发列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="sink_dispatch.csv"'})


# ───────────────────────────────
# 分发批次结果：字面量路径必须声明在 /{entry_id} 之前，避免被整型参数拦截
# ───────────────────────────────

@router.post("/batches", response_model=ActionResult)
def submit_batch(payload: dict[str, object]) -> ActionResult:
    """读取分发规则并生成分发批次结果。

    批次结果保留成功键、失败键与回退范围；支持一次选择多条规则整组提交，
    逐条保留成功或失败结果。坏键只影响其所在批次，不再触发整表重试。
    """
    rule_ids = payload.get("rule_ids")
    single_id = payload.get("rule_id")
    if not rule_ids and single_id is not None:
        rule_ids = [single_id]
    if not isinstance(rule_ids, list) or not rule_ids:
        return ActionResult(ok=False, message="缺少待提交的分发规则 rule_ids")

    common_policy = str(payload.get("conflict_policy", "retry_bad_key_first"))
    per_rule_keys = payload.get("keys_by_rule") if isinstance(payload.get("keys_by_rule"), dict) else {}
    per_rule_bad = payload.get("bad_keys_by_rule") if isinstance(payload.get("bad_keys_by_rule"), dict) else {}
    interrupted = bool(payload.get("interrupted", False))
    try:
        key_count = int(payload["key_count"]) if payload.get("key_count") is not None else None
    except (TypeError, ValueError):
        key_count = None

    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for raw_id in rule_ids:
        try:
            rule_id = int(raw_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            failures.append({"rule_id": raw_id, "message": "规则编号不是有效整数"})
            continue
        keys = _as_str_list(per_rule_keys.get(str(rule_id))) or None
        bad_keys = _as_str_list(per_rule_bad.get(str(rule_id)))
        batch, message = service.submit_batch(
            rule_id,
            keys=keys,
            bad_keys=bad_keys,
            conflict_policy=common_policy,
            key_count=key_count,
            interrupted=interrupted,
        )
        if batch is None:
            failures.append({"rule_id": rule_id, "message": message})
        else:
            results.append(batch)

    ok = not failures
    message = (
        f"整组 {len(rule_ids)} 条规则已提交，生成 {len(results)} 个分发批次结果"
        if ok else f"{len(failures)} 条规则提交失败，其余 {len(results)} 条已生成批次结果"
    )
    return ActionResult(ok=ok, message=message, entry={"batches": results, "failures": failures})


@router.get("/batches", response_model=PageResult[dict])
def list_batches(
    rule_id: int | None = Query(default=None, description="按分发规则过滤批次结果"),
) -> PageResult[dict]:
    """读取分发批次结果；切换页面后仍可按同一口径取回成功键、失败键与回退范围。"""
    items = service.list_batches(rule_id)
    return PageResult(items=items, total=len(items), page=1, size=len(items) or 1)


@router.get("/batches/{batch_id}", response_model=dict)
def get_batch(batch_id: str) -> dict:
    """读取单个分发批次结果明细。"""
    batch = service.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"分发批次 {batch_id} 不存在")
    return batch


@router.post("/batches/{batch_id}/recover", response_model=ActionResult)
def recover_batch(batch_id: str, payload: dict[str, object] | None = None) -> ActionResult:
    """任务中断后恢复分发批次：仅回退失败键界定的未完成范围，成功键不重复写入。"""
    payload = payload or {}
    new_keys = _as_str_list(payload.get("new_keys"))
    # 未提供 bad_keys 时传 None，让服务层沿用批次记录的坏键；显式空列表表示已全部修复
    bad_raw = payload.get("bad_keys")
    bad_keys = _as_str_list(bad_raw) if isinstance(bad_raw, list) else None
    policy = payload.get("conflict_policy")
    batch, message = service.recover_batch(
        batch_id,
        new_keys=new_keys,
        bad_keys=bad_keys,
        conflict_policy=str(policy) if policy else None,
    )
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=f"分发批次 {batch_id} 已恢复", entry=batch)


@router.get("/committed-keys", response_model=PageResult[dict])
def list_committed_keys(
    rule_id: int | None = Query(default=None, description="按分发规则过滤已提交键台账"),
) -> PageResult[dict]:
    """已提交键台账：复查批量推送中的成功键不会因同批失败再次写入。"""
    items = service.list_committed_keys(rule_id)
    return PageResult(items=items, total=len(items), page=1, size=len(items) or 1)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条分发规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"分发规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条分发规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="分发规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条分发规则执行提交校验、标记生效、停用规则；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
