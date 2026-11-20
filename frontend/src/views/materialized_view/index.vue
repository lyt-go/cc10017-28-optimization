<template>
  <section class="page" data-module="materialized_view">
    <header class="page-head">
      <div>
        <h2>物化查询管理</h2>
        <p class="page-desc">维护湖上物化视图的查询定义、增量刷新与存储配置；核对下沉分发批次结果并确认后续批次使用的视图刷新版本。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记物化视图</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出物化查询清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无物化查询数据，可先登记物化视图</td>
        </tr>
      </tbody>
    </table>

    <!-- 刷新版本核对：物化查询页核对下沉分发批次结果，确认后续批次使用的视图刷新版本 -->
    <section class="refresh-panel" data-testid="refresh-panel">
      <div class="batch-result-head">
        <h3>视图刷新版本核对</h3>
        <div class="page-actions">
          <button class="btn" data-testid="refresh-versions-reload" type="button" @click="loadAll">刷新关联记录</button>
        </div>
      </div>

      <div class="confirm-bar">
        <label class="batch-field">
          <span>物化视图</span>
          <select v-model="confirmMvId" data-testid="confirm-mv">
            <option v-for="row in rows" :key="String(row.id)" :value="Number(row.id)">
              {{ row['视图编号'] }}（{{ row['基表名'] }}）
            </option>
          </select>
        </label>
        <label class="batch-field">
          <span>分发批次结果</span>
          <select v-model="confirmBatchId" data-testid="confirm-batch">
            <option value="" disabled>请选择批次</option>
            <option v-for="batch in dispatchBatches" :key="batch.batch_id" :value="batch.batch_id">
              {{ batch.batch_id }} · {{ batch.rule_code }} · {{ batch.result_version }}
            </option>
          </select>
        </label>
        <button class="btn primary" data-testid="confirm-refresh-version" type="button"
                :disabled="!confirmMvId || !confirmBatchId || confirming" @click="confirmVersion">
          {{ confirming ? '核对中…' : '核对并确认刷新版本' }}
        </button>
      </div>

      <table v-if="refreshVersions.length" class="data-table" data-testid="refresh-version-table">
        <thead>
          <tr>
            <th>刷新版本</th><th>物化视图</th><th>来源批次</th><th>批次版本</th>
            <th>成功键</th><th>失败键</th><th>回退范围</th><th>批次状态</th><th>来源记录</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="version in refreshVersions" :key="version.id" :data-testid="`version-row-${version.id}`">
            <td>{{ version.refresh_version }}</td>
            <td>{{ version.mv_code }}（#{{ version.mv_id }}）</td>
            <td>{{ version.sink_batch_id }}</td>
            <td>{{ version.sink_result_version }}</td>
            <td>{{ version.success_key_count }}</td>
            <td>{{ version.failed_key_count }}</td>
            <td>{{ version.rollback_range_count }}</td>
            <td>{{ version.batch_status }}</td>
            <td>{{ version.source_record?.module }} / {{ version.source_record?.mv_code }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="empty-state">暂无刷新版本确认记录，请选择物化视图与分发批次后核对确认。</p>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记物化视图</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条物化查询记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'
import { useDispatchStore, type RefreshVersion } from '@/stores/dispatch'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/materialized_view'
const SINK_ENDPOINT = '/api/sink_dispatch'
const columns = ["视图编号", "查询SQL", "基表名", "刷新模式", "刷新间隔", "存储位置", "数据量", "最后刷新"]
const actions = ["提交校验", "启用刷新", "停用视图"]
const statuses = ["草稿", "待校验", "已启用", "已停用"]
const metricNames = ["物化视图总数", "待校验视图", "已启用视图"]

const dispatchStore = useDispatchStore()

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})

const confirmMvId = ref<number | null>(null)
const confirmBatchId = ref('')
const confirming = ref(false)
const localVersions = ref<RefreshVersion[]>([])

const dispatchBatches = computed(() => dispatchStore.batches)
const refreshVersions = computed(() =>
  localVersions.value.length ? localVersions.value : dispatchStore.refreshVersions)

const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'materialized_view.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('物化查询动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '物化查询操作失败'
  }
}

async function confirmVersion() {
  if (!confirmMvId.value || !confirmBatchId.value) return
  confirming.value = true
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/refresh-versions/confirm`, {
      method: 'POST',
      body: JSON.stringify({ mv_id: confirmMvId.value, batch_id: confirmBatchId.value }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '刷新版本确认失败')
    dispatchStore.setRefreshVersions([payload.entry as RefreshVersion])
    await loadRefreshVersions()
    confirmBatchId.value = ''
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '刷新版本确认失败'
  } finally {
    confirming.value = false
  }
}

async function loadBatches() {
  try {
    const response = await request(`${SINK_ENDPOINT}/batches`)
    if (!response.ok) return
    const payload = await response.json()
    dispatchStore.setBatches(payload.items ?? [])
  } catch {
    // 批次读取失败不阻断物化视图主表
  }
}

async function loadRefreshVersions() {
  try {
    const response = await request(`${ENDPOINT}/refresh-versions`)
    if (!response.ok) throw new Error('刷新版本读取失败')
    const payload = await response.json()
    localVersions.value = payload.items ?? []
    dispatchStore.setRefreshVersions(localVersions.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '刷新版本读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('物化视图列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    if (confirmMvId.value === null && rows.value.length) {
      confirmMvId.value = Number(rows.value[0].id)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '物化视图列表读取失败'
  }
}

async function loadAll() {
  await loadBatches()
  await loadRefreshVersions()
}

onMounted(async () => {
  await reload()
  // 关联记录同步呈现：拉取下沉分发批次结果与已确认的刷新版本
  await loadAll()
})
</script>

<style scoped>
.refresh-panel {
  margin-top: 18px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px;
}
.batch-result-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
}
.batch-result-head h3 { margin: 0; font-size: 15px; }
.confirm-bar {
  display: flex;
  align-items: flex-end;
  gap: 12px;
  flex-wrap: wrap;
  background: #fafbfc;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 12px;
  margin-bottom: 12px;
}
.batch-field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.batch-field select {
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 13px;
  min-width: 220px;
}
</style>
