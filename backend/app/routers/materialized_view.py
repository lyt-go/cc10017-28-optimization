"""物化查询接口：维护物化视图，覆盖提交校验、启用刷新、停用视图等动作。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.materialized_view import MaterializedViewService

router = APIRouter(prefix="/api/materialized_view", tags=["物化查询"])

service = MaterializedViewService()

LIST_FIELDS = ["视图编号", "查询SQL", "基表名", "刷新模式", "刷新间隔", "存储位置", "数据量", "最后刷新"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按视图编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、视图编号关键字与状态过滤物化查询列表。"""
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
    output.write("\ufeff")
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
        headers={"Content-Disposition": 'attachment; filename="materialized_view.csv"'})


# ───────────────────────────────
# 分发批次结果核对与刷新版本：字面量路径声明在 /{entry_id} 之前
# ───────────────────────────────

@router.post("/refresh-versions/confirm", response_model=ActionResult)
def confirm_refresh(payload: dict[str, object]) -> ActionResult:
    """物化查询页核对分发批次结果，并确认后续批次使用的视图刷新版本。"""
    try:
        mv_id = int(payload.get("mv_id"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ActionResult(ok=False, message="缺少有效的 mv_id")
    batch_id = str(payload.get("batch_id") or "").strip()
    if not batch_id:
        return ActionResult(ok=False, message="缺少待核对的 batch_id")
    record, message = service.confirm_batch_refresh(mv_id, batch_id)
    if record is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=f"已确认刷新版本 {record['refresh_version']}", entry=record)


@router.get("/refresh-versions", response_model=PageResult[dict])
def list_refresh_versions(
    mv_id: int | None = Query(default=None, description="按物化视图过滤刷新版本"),
) -> PageResult[dict]:
    """读取视图刷新版本确认记录，关联记录与分发批次来源同步呈现。"""
    items = service.list_refresh_versions(mv_id)
    return PageResult(items=items, total=len(items), page=1, size=len(items) or 1)


@router.get("/batch-sources", response_model=PageResult[dict])
def list_batch_sources(
    mv_id: int | None = Query(default=None, description="按物化视图过滤来源记录"),
) -> PageResult[dict]:
    """物化查询侧保留的分发批次结果来源记录。"""
    items = service.list_batch_sources(mv_id)
    return PageResult(items=items, total=len(items), page=1, size=len(items) or 1)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条物化视图明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"物化视图 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条物化视图，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="物化视图已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条物化视图执行提交校验、启用刷新、停用视图；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
