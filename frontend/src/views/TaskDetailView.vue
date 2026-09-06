<template>
  <div v-loading="!item">
    <el-page-header :content="`任务 #${id}${item ? ' · ' + item.name : ''}`" @back="$router.push('/tasks')"
                    style="margin-bottom: 14px" />
    <template v-if="item">
      <el-card shadow="never" class="page-card">
        <div class="head-row">
          <div class="head-left">
            <status-tag :status="item.status" />
            <span class="type-badge">{{ typeLabel }}</span>
            <span class="muted">创建：{{ fmtTs(item.created_at) }}</span>
            <span class="muted">开始：{{ fmtTs(item.started_at) }} · 结束：{{ fmtTs(item.finished_at) }}</span>
          </div>
          <div>
            <el-button v-if="runnable(item.status)" type="success" :loading="acting" @click="onRun">运行</el-button>
            <el-button v-if="cancellable(item.status)" type="warning" :loading="acting" @click="onCancel">取消</el-button>
            <el-button v-if="editable(item.status)" @click="$router.push(`/tasks/${id}/edit`)">编辑</el-button>
            <el-popconfirm title="确认删除该任务？" @confirm="onDelete">
              <template #reference>
                <el-button type="danger" plain>删除</el-button>
              </template>
            </el-popconfirm>
          </div>
        </div>

        <template v-if="item.status === 'running' && item.progress">
          <el-progress
            :percentage="percent"
            :stroke-width="14"
            style="margin-top: 14px"
            :format="() => percentText"
          />
          <div v-if="layerText" class="muted" style="margin-top: 6px">{{ layerText }}</div>
        </template>

        <el-alert v-if="item.status === 'failed' && item.error" type="error" :closable="false"
                  :title="item.error" style="margin-top: 14px" />
      </el-card>

      <el-card v-if="item.result" shadow="never" class="page-card">
        <template #header><b>导入结果</b></template>
        <el-table :data="resultRows" size="small" border>
          <el-table-column prop="kind" label="结果" width="90">
            <template #default="{ row }">
              <el-tag :type="row.kind === '成功' ? 'success' : row.kind === '跳过' ? 'warning' : 'danger'" size="small">
                {{ row.kind }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="source" label="图层/来源" min-width="160" />
          <el-table-column prop="detail" label="说明" min-width="200" />
          <el-table-column v-if="item.result.ok.length" prop="rows" label="行数" width="90" align="right" />
        </el-table>
      </el-card>

      <el-card shadow="never" class="page-card">
        <template #header>
          <div style="display: flex; justify-content: space-between; align-items: center">
            <b>运行日志</b>
            <span class="muted">{{ polling ? '实时刷新中（1.5s）' : '已停止刷新' }}</span>
          </div>
        </template>
        <log-viewer :logs="logs" />
      </el-card>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import StatusTag from '../components/StatusTag.vue'
import LogViewer from '../components/LogViewer.vue'
import { cancelTask, deleteTask, getLogs, getTask, listTaskTypes, runTask } from '../api/tasks'
import type { LogEntry, TaskItem, TaskStatus, TaskTypeInfo } from '../types'

const route = useRoute()
const router = useRouter()
const id = Number(route.params.id)

const item = ref<TaskItem | null>(null)
const logs = ref<LogEntry[]>([])
const afterId = ref(0)
const types = ref<TaskTypeInfo[]>([])
const acting = ref(false)
const polling = ref(false)
const TERMINAL: TaskStatus[] = ['succeeded', 'failed', 'cancelled']

const typeLabel = computed(() => types.value.find((t) => t.type === item.value?.type)?.label || item.value?.type || '')
const percent = computed(() => {
  const p = item.value?.progress
  if (!p || !p.layer_total || !p.layer_index) return 0
  return Math.round((p.layer_index / p.layer_total) * 100)
})
const percentText = computed(() => {
  const p = item.value?.progress
  return `图层 ${p?.layer_index ?? 0}/${p?.layer_total ?? '?'}`
})
const layerText = computed(() => {
  const p = item.value?.progress
  if (!p) return ''
  const parts = [`当前图层：${p.source || '—'}`]
  if (p.rows) parts.push(`已写 ${p.rows} 行`)
  return parts.join(' · ')
})
const runnable = (s: TaskStatus) => s === 'draft' || TERMINAL.includes(s)
const cancellable = (s: TaskStatus) => s === 'queued' || s === 'running'
const editable = (s: TaskStatus) => s !== 'queued' && s !== 'running'

const resultRows = computed(() => {
  const r = item.value?.result
  if (!r) return []
  return [
    ...r.ok.map((x) => ({ kind: '成功', source: x.source, detail: `导入 ${x.rows} 行`, rows: x.rows })),
    ...r.skipped.map((x) => ({ kind: '跳过', source: x.source, detail: x.message })),
    ...r.failed.map((x) => ({ kind: '失败', source: '—', detail: x.message })),
  ]
})

function fmtTs(ts: string | null) {
  if (!ts) return '—'
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleString()
}

let timer: number | undefined

async function refresh() {
  if (!isAlive) return
  try {
    item.value = await getTask(id)
    if (!isAlive) return
    const res = await getLogs(id, afterId.value)
    if (!isAlive) return
    if (res.logs.length) {
      logs.value.push(...res.logs)
      afterId.value = res.last_id
    }
    if (TERMINAL.includes(item.value.status)) {
      stopPolling()
    }
  } catch {
    stopPolling()
  }
}

let isAlive = true // 组件存活标记：卸载后不再发起任何轮询请求
let shouldPoll = false // 任务未终结且应当轮询（用于页签可见性恢复）

function startPolling() {
  shouldPoll = true
  polling.value = true
  if (timer === undefined) {
    timer = window.setInterval(refresh, 1500)
  }
}

function stopPolling() {
  shouldPoll = false
  polling.value = false
  if (timer !== undefined) {
    window.clearInterval(timer)
    timer = undefined
  }
}

// 页签切到后台时暂停轮询，回到前台且任务未结束时恢复
function onVisibility() {
  if (document.hidden) {
    polling.value = false
    if (timer !== undefined) {
      window.clearInterval(timer)
      timer = undefined
    }
  } else if (shouldPoll) {
    startPolling()
  }
}
document.addEventListener('visibilitychange', onVisibility)

onMounted(async () => {
  try {
    types.value = await listTaskTypes()
  } catch {
    types.value = []
  }
  await refresh()
  if (item.value && !TERMINAL.includes(item.value.status)) {
    startPolling()
  }
})

onBeforeUnmount(() => {
  isAlive = false
  document.removeEventListener('visibilitychange', onVisibility)
  stopPolling()
})

async function onRun() {
  acting.value = true
  try {
    await runTask(id)
    ElMessage.success('任务已提交运行')
    await refresh()
    if (item.value && !TERMINAL.includes(item.value.status)) {
      startPolling()
    }
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    acting.value = false
  }
}

async function onCancel() {
  acting.value = true
  try {
    await cancelTask(id)
    ElMessage.info('已请求取消')
    await refresh()
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    acting.value = false
  }
}

async function onDelete() {
  try {
    await deleteTask(id)
    ElMessage.success('已删除')
    router.push('/tasks')
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}
</script>

<style scoped>
.head-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.head-left {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.type-badge {
  background: #ecf5ff;
  color: #409eff;
  border-radius: 4px;
  padding: 2px 8px;
  font-size: 12px;
}
</style>