import { defineStore } from 'pinia'

/** 分发批次结果与物化刷新版本的跨页面缓存。
 *
 * 下沉分发页与物化查询页都会拉取同一批分发批次结果；放进 Pinia 后，
 * 切换路由再切回来仍能看到之前的批次结果与关联记录，无需重新提交。
 */

export type RollbackRange = {
  offset_start: number
  offset_end: number
  start_key: string
  end_key: string
  scope: string
}

export type DispatchBatch = {
  batch_id: string
  rule_id: number
  rule_code?: string
  lake_table?: string
  target_system?: string
  conflict_policy: string
  conflict_policy_label?: string
  status: string
  attempt: number
  result_version: string
  success_keys: string[]
  failed_keys: string[]
  bad_keys?: string[]
  skipped_committed_keys?: string[]
  rollback_ranges: RollbackRange[]
  full_table_rollback: boolean
  interrupted: boolean
  committed_total?: number
  version_history?: { attempt: number; conflict_policy: string; result_version: string }[]
  source_record?: Record<string, unknown>
  created_at?: string
  recovered_at?: string
}

export type RefreshVersion = {
  id: number
  mv_id: number
  mv_code?: string
  refresh_version: string
  sink_batch_id: string
  sink_result_version?: string
  conflict_policy?: string
  success_key_count: number
  failed_key_count: number
  rollback_range_count: number
  batch_status?: string
  source_record?: Record<string, unknown>
  confirmed_at?: string
}

type DispatchState = {
  batches: DispatchBatch[]
  selectedBatchId: string | null
  refreshVersions: RefreshVersion[]
  selectedRuleIds: number[]
  lastLoadedAt: string | null
}

const STORAGE_KEY = 'dispatch-store-v1'

function loadPersisted(): Partial<DispatchState> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as Partial<DispatchState>
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

export const useDispatchStore = defineStore('dispatch', {
  state: (): DispatchState => {
    const persisted = loadPersisted()
    return {
      batches: Array.isArray(persisted.batches) ? persisted.batches : [],
      selectedBatchId: persisted.selectedBatchId ?? null,
      refreshVersions: Array.isArray(persisted.refreshVersions) ? persisted.refreshVersions : [],
      selectedRuleIds: Array.isArray(persisted.selectedRuleIds) ? persisted.selectedRuleIds : [],
      lastLoadedAt: persisted.lastLoadedAt ?? null,
    }
  },
  getters: {
    selectedBatch(state): DispatchBatch | null {
      return state.batches.find((batch) => batch.batch_id === state.selectedBatchId) ?? null
    },
    hasBatches(state): boolean {
      return state.batches.length > 0
    },
  },
  actions: {
    setBatches(batches: DispatchBatch[]) {
      // 以 batch_id 去重合并，保留最新的服务端状态
      const merged = new Map<string, DispatchBatch>(this.batches.map((b) => [b.batch_id, b]))
      for (const batch of batches) {
        merged.set(batch.batch_id, batch)
      }
      this.batches = Array.from(merged.values())
      this.lastLoadedAt = new Date().toISOString()
    },
    upsertBatch(batch: DispatchBatch) {
      const index = this.batches.findIndex((item) => item.batch_id === batch.batch_id)
      if (index >= 0) {
        this.batches.splice(index, 1, batch)
      } else {
        this.batches.unshift(batch)
      }
      this.lastLoadedAt = new Date().toISOString()
    },
    selectBatch(batchId: string | null) {
      this.selectedBatchId = batchId
    },
    setSelectedRuleIds(ruleIds: number[]) {
      this.selectedRuleIds = ruleIds
    },
    setRefreshVersions(versions: RefreshVersion[]) {
      const merged = new Map<number, RefreshVersion>(this.refreshVersions.map((v) => [v.id, v]))
      for (const version of versions) {
        merged.set(version.id, version)
      }
      this.refreshVersions = Array.from(merged.values())
    },
    reset() {
      this.batches = []
      this.selectedBatchId = null
      this.refreshVersions = []
      this.selectedRuleIds = []
      this.lastLoadedAt = null
    },
  },
})
