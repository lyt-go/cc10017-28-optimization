<template>
  <section class="page" data-module="sink_dispatch">
    <header class="page-head">
      <div>
        <h2>下沉分发管理</h2>
        <p class="page-desc">维护湖表数据向OLAP、KV或搜索引擎下沉的分发规则；按批次区分成功键、失败键与回退范围，坏键不再整表重试、不阻塞后续批次。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记分发规则</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出下沉分发清单</button>
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

    <!-- 批量选择工具栏：整组一次提交，逐条保留成功或失败结果 -->
    <div class="batch-toolbar" data-testid="batch-toolbar">
      <div class="batch-toolbar-left">
        <label class="batch-check">
          <input type="checkbox" data-testid="select-all" :checked="allSelected" :indeterminate.prop="someSelected" @change="toggleSelectAll" />
          <span>全选本页</span>
        </label>
        <span class="batch-count">已选 {{ selectedRuleIds.length }} 条规则</span>
      </div>
      <div class="batch-toolbar-right">
        <label class="batch-field">
          <span>坏键（逗号分隔，可选）</span>
          <input v-model="badKeysText" data-testid="batch-bad-keys" placeholder="如 K002,K005" />
        </label>
        <label class="batch-field">
          <span>冲突判定</span>
          <select v-model="conflictPolicy" data-testid="batch-policy">
            <option value="retry_bad_key_first">坏键优先重试</option>
            <option value="advance_new_key_first">新键优先推进</option>
          </select>
        </label>
        <label class="batch-check">
          <input type="checkbox" v-model="interrupted" data-testid="batch-interrupted" />
          <span>模拟任务中断</span>
        </label>
        <button class="btn primary" data-testid="submit-batch" type="button" :disabled="!selectedRuleIds.length || submitting" @click="submitBatch">
          {{ submitting ? '提交中…' : '整组提交分发批次' }}
        </button>
      </div>
    </div>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
          <th style="width:42px">选择</th>
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
          <td>
            <input
              type="checkbox"
              :data-testid="`select-row-${row.id}`"
              :value="Number(row.id)"
              v-model="selectedRuleIds"
              @change="syncSelection"
            />
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无下沉分发数据，可先登记分发规则</td>
        </tr>
      </tbody>
    </table>

    <!-- 分发批次结果：切换页面后仍由 Pinia 保留，关联记录同步呈现 -->
    <section class="batch-result" data-testid="batch-result-panel">
      <div class="batch-result-head">
        <h3>分发批次结果（成功键 / 失败键 / 回退范围）</h3>
        <div class="page-actions">
          <button class="btn" data-testid="refresh-batches" type="button" @click="loadBatches">刷新批次结果</button>
          <button class="btn" data-testid="load-committed" type="button" @click="loadCommittedKeys">查看已提交键台账</button>
        </div>
      </div>
      <p v-if="!batches.length" class="empty-state">还没有分发批次结果，勾选规则后整组提交即可生成。</p>
      <table v-else class="data-table">
        <thead>
          <tr>
            <th>批次编号</th><th>规则</th><th>目标系统</th><th>状态</th><th>尝试次数</th>
            <th>成功键</th><th>失败键</th><th>回退范围</th><th>整表回退</th><th>当前版本</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="batch in batches" :key="batch.batch_id" :data-testid="`batch-row-${batch.batch_id}`"
              :class="{ selected: batch.batch_id === selectedBatchId }" @click="selectBatch(batch.batch_id)">
            <td>{{ batch.batch_id }}</td>
            <td>{{ batch.rule_code }}（#{{ batch.rule_id }}）</td>
            <td>{{ batch.target_system ?? '—' }}</td>
            <td>{{ batch.status }}</td>
            <td>{{ batch.attempt }}</td>
            <td>{{ batch.success_keys.length }}</td>
            <td>{{ batch.failed_keys.length }}</td>
            <td>{{ batch.rollback_ranges.length }}</td>
            <td>{{ batch.full_table_rollback ? '是' : '否' }}</td>
            <td>{{ batch.result_version }}</td>
            <td class="row-actions" @click.stop>
              <button class="link" type="button" :data-testid="`detail-${batch.batch_id}`" @click="selectBatch(batch.batch_id)">明细</button>
              <button
                v-if="batch.status !== '已完成'"
                class="link" type="button" :data-testid="`recover-${batch.batch_id}`"
                @click="openRecover(batch)"
              >恢复分发</button>
            </td>
          </tr>
        </tbody>
      </table>

      <!-- 选中批次的关联记录：成功键、失败键、回退范围、来源记录、版本历史 -->
      <div v-if="selectedBatch" class="batch-detail" data-testid="batch-detail">
        <h4>{{ selectedBatch.batch_id }} 关联记录</h4>
        <div class="detail-grid">
          <div class="detail-block">
            <strong>成功键（仅提交一次）</strong>
            <ul class="key-list ok"><li v-for="key in selectedBatch.success_keys" :key="key">{{ key }}</li></ul>
          </div>
          <div class="detail-block">
            <strong>失败键 / 未完成键</strong>
            <ul class="key-list fail"><li v-for="key in selectedBatch.failed_keys" :key="key">{{ key }}</li></ul>
          </div>
          <div class="detail-block">
            <strong>回退范围（失败键界定，不含成功键）</strong>
            <ul class="range-list">
              <li v-for="(range, index) in selectedBatch.rollback_ranges" :key="index">
                [{{ range.offset_start }}..{{ range.offset_end }}] {{ range.start_key }} → {{ range.end_key }}
              </li>
              <li v-if="!selectedBatch.rollback_ranges.length" class="empty-state">无回退范围</li>
            </ul>
          </div>
          <div class="detail-block">
            <strong>版本历史与来源记录</strong>
            <ul class="range-list">
              <li v-for="item in selectedBatch.version_history" :key="item.attempt">
                第{{ item.attempt }}次 · {{ policyText(item.conflict_policy) }} · {{ item.result_version }}
              </li>
            </ul>
            <p class="source-record">来源：{{ selectedBatch.source_record?.module }} / {{ selectedBatch.source_record?.rule_code }} / {{ selectedBatch.source_record?.lake_table }}</p>
          </div>
        </div>
      </div>

      <!-- 已提交键台账：复查成功键不会因同批失败再次写入 -->
      <div v-if="committedKeys.length" class="committed-panel" data-testid="committed-panel">
        <h4>已提交键台账（每个成功键只提交一次）</h4>
        <table class="data-table">
          <thead><tr><th>键</th><th>规则</th><th>首次提交批次</th><th>提交时间</th></tr></thead>
          <tbody>
            <tr v-for="row in committedKeys" :key="row.id">
              <td>{{ row.key }}</td><td>#{{ row.rule_id }}</td><td>{{ row.first_batch_id }}</td><td>{{ row.committed_at }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- 恢复分发弹窗：处理同窗口到达的新键 -->
    <div v-if="recoverTarget" class="modal-backdrop" data-testid="recover-modal">
      <form class="modal" @submit.prevent="recoverBatch">
        <h3>恢复分发批次 {{ recoverTarget.batch_id }}</h3>
        <p class="page-desc">仅回退失败键界定的未完成范围，已成功键按台账跳过、不会再次写入。</p>
        <label class="create-field">
          <span>同窗口新键（逗号分隔，可选）</span>
          <input v-model="recoverNewKeys" data-testid="recover-new-keys" placeholder="如 K100,K101" />
        </label>
        <label class="create-field">
          <span>仍失败的坏键（逗号分隔，可选）</span>
          <input v-model="recoverBadKeys" data-testid="recover-bad-keys" :placeholder="recoverTarget.failed_keys.join(',')" />
        </label>
        <label class="create-field">
          <span>冲突判定做法</span>
          <select v-model="recoverPolicy" data-testid="recover-policy">
            <option value="retry_bad_key_first">坏键优先重试</option>
            <option value="advance_new_key_first">新键优先推进</option>
          </select>
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="recoverTarget = null">取消</button>
          <button class="btn primary" data-testid="confirm-recover" type="submit" :disabled="recovering">
            {{ recovering ? '恢复中…' : '确认恢复' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记分发规则</h3>
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
      <span>共 {{ total }} 条下沉分发记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'
import { useDispatchStore, type DispatchBatch } from '@/stores/dispatch'

type Row = Record<string, string | number | null>
type CommittedKey = { id: number; rule_id: number; key: string; first_batch_id: string; committed_at: string }

const ENDPOINT = '/api/sink_dispatch'
const columns = ["规则编号", "湖表名", "目标系统", "写入模式", "字段映射", "过滤条件", "批次大小", "失败回退"]
const actions = ["提交校验", "标记生效", "停用规则"]
const statuses = ["草稿", "待校验", "已生效", "已停用"]
const metricNames = ["分发规则总数", "待校验规则", "已生效规则"]

const dispatchStore = useDispatchStore()

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})

// 批量分发相关状态
const selectedRuleIds = ref<number[]>([...dispatchStore.selectedRuleIds])
const badKeysText = ref('')
const conflictPolicy = ref('retry_bad_key_first')
const interrupted = ref(false)
const submitting = ref(false)
const committedKeys = ref<CommittedKey[]>([])

// 恢复弹窗
const recoverTarget = ref<DispatchBatch | null>(null)
const recoverNewKeys = ref('')
const recoverBadKeys = ref('')
const recoverPolicy = ref('retry_bad_key_first')
const recovering = ref(false)

const batches = computed(() => dispatchStore.batches)
const selectedBatchId = computed(() => dispatchStore.selectedBatchId)
const selectedBatch = computed(() => dispatchStore.selectedBatch)

const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

const allSelected = computed(() => rows.value.length > 0 && selectedRuleIds.value.length === rows.value.length)
const someSelected = computed(() => selectedRuleIds.value.length > 0 && !allSelected.value)

function policyText(policy: string): string {
  return policy === 'advance_new_key_first' ? '新键优先推进' : '坏键优先重试'
}

function splitKeys(text: string): string[] {
  return text.split(',').map((item) => item.trim()).filter(Boolean)
}

function syncSelection() {
  dispatchStore.setSelectedRuleIds([...selectedRuleIds.value])
}

function toggleSelectAll(event: Event) {
  const checked = (event.target as HTMLInputElement).checked
  selectedRuleIds.value = checked ? rows.value.map((row) => Number(row.id)) : []
  syncSelection()
}

function selectBatch(batchId: string) {
  dispatchStore.selectBatch(batchId)
}

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
    link.download = 'sink_dispatch.csv'
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
      throw new Error('下沉分发动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '下沉分发操作失败'
  }
}

async function submitBatch() {
  if (!selectedRuleIds.value.length) return
  submitting.value = true
  errorMessage.value = ''
  try {
    const badKeys = splitKeys(badKeysText.value)
    const badKeysByRule: Record<string, string[]> = {}
    for (const ruleId of selectedRuleIds.value) {
      if (badKeys.length) badKeysByRule[String(ruleId)] = badKeys
    }
    const response = await request(`${ENDPOINT}/batches`, {
      method: 'POST',
      body: JSON.stringify({
        rule_ids: selectedRuleIds.value,
        bad_keys_by_rule: badKeysByRule,
        conflict_policy: conflictPolicy.value,
        key_count: 6,
        interrupted: interrupted.value,
      }),
    })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.message || '批次提交失败')
    const produced: DispatchBatch[] = payload.entry?.batches ?? []
    dispatchStore.setBatches(produced)
    if (produced.length) dispatchStore.selectBatch(produced[0].batch_id)
    errorMessage.value = payload.ok ? '' : payload.message
    await loadBatches()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分发批次提交失败'
  } finally {
    submitting.value = false
  }
}

async function loadBatches() {
  try {
    const response = await request(`${ENDPOINT}/batches`)
    if (!response.ok) throw new Error('批次结果读取失败')
    const payload = await response.json()
    dispatchStore.setBatches(payload.items ?? [])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分发批次结果读取失败'
  }
}

async function loadCommittedKeys() {
  try {
    const response = await request(`${ENDPOINT}/committed-keys`)
    if (!response.ok) throw new Error('已提交键台账读取失败')
    const payload = await response.json()
    committedKeys.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '已提交键台账读取失败'
  }
}

function openRecover(batch: DispatchBatch) {
  recoverTarget.value = batch
  recoverNewKeys.value = ''
  // 预填真正的坏键（中断批次的 failed_keys 里还混有未处理好键，不能当坏键）
  recoverBadKeys.value = (batch.bad_keys ?? batch.failed_keys).join(',')
  recoverPolicy.value = batch.conflict_policy
}

async function recoverBatch() {
  if (!recoverTarget.value) return
  recovering.value = true
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/batches/${recoverTarget.value.batch_id}/recover`, {
      method: 'POST',
      body: JSON.stringify({
        new_keys: splitKeys(recoverNewKeys.value),
        bad_keys: splitKeys(recoverBadKeys.value),
        conflict_policy: recoverPolicy.value,
      }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '恢复失败')
    dispatchStore.upsertBatch(payload.entry as DispatchBatch)
    dispatchStore.selectBatch(payload.entry.batch_id as string)
    recoverTarget.value = null
    await loadBatches()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分发批次恢复失败'
  } finally {
    recovering.value = false
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('分发规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '下沉分发列表读取失败'
  }
}

onMounted(async () => {
  await reload()
  // 切换页面回来后仍展示之前的分发批次结果
  await loadBatches()
})
</script>

<style scoped>
.batch-toolbar {
  display: flex;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 12px;
}
.batch-toolbar-left, .batch-toolbar-right {
  display: flex;
  align-items: flex-end;
  gap: 12px;
  flex-wrap: wrap;
}
.batch-check {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  white-space: nowrap;
}
.batch-count { font-size: 12px; color: var(--muted); }
.batch-field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.batch-field input, .batch-field select {
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 13px;
  min-width: 150px;
}
.batch-result {
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
tr.selected { background: #eff6ff; }
.batch-detail {
  margin-top: 14px;
  border-top: 1px solid var(--border);
  padding-top: 12px;
}
.batch-detail h4 { margin: 0 0 8px; font-size: 14px; }
.detail-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 12px;
}
.detail-block {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px;
  background: #fafbfc;
}
.detail-block strong { font-size: 12px; color: var(--muted); display: block; margin-bottom: 6px; }
.key-list, .range-list { margin: 0; padding-left: 16px; font-size: 12px; max-height: 160px; overflow: auto; }
.key-list.ok li { color: #15803d; }
.key-list.fail li { color: #b42318; }
.source-record { font-size: 11px; color: var(--muted); margin: 6px 0 0; }
.committed-panel { margin-top: 14px; }
.committed-panel h4 { margin: 0 0 8px; font-size: 14px; }
</style>
