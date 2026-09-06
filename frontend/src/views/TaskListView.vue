<template>
  <div>
    <el-card shadow="never" class="page-card">
      <div class="toolbar">
        <el-select v-model="filters.status" placeholder="状态" clearable style="width: 130px">
          <el-option v-for="s in STATUS_OPTIONS" :key="s.value" :label="s.label" :value="s.value" />
        </el-select>
        <el-select v-model="filters.type" placeholder="类型" clearable style="width: 180px">
          <el-option v-for="t in types" :key="t.type" :label="t.label" :value="t.type" />
        </el-select>
        <el-input v-model="filters.q" placeholder="按名称搜索" clearable style="width: 220px"
                  @keyup.enter="reload" />
        <el-button type="primary" @click="reload">查询</el-button>
        <el-button @click="reset">重置</el-button>
        <div style="flex: 1" />
        <el-button type="primary" @click="$router.push('/tasks/new')">+ 新建任务</el-button>
      </div>
    </el-card>

    <el-card shadow="never">
      <el-table :data="items" v-loading="loading" size="default" stripe
             :default-sort="{ prop: 'created_at', order: 'descending' }"
             @sort-change="onSortChange">
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column prop="name" label="名称" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">
            <el-link type="primary" @click="$router.push(`/tasks/${row.id}`)">{{ row.name }}</el-link>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="160">
          <template #default="{ row }">
            {{ typeLabel(row.type) }}
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <status-tag :status="row.status" />
          </template>
        </el-table-column>
        <el-table-column label="进度" min-width="160">
          <template #default="{ row }">
            <span v-if="row.progress && row.progress.layer_total" class="muted">
              {{ row.progress.source || '' }} {{ row.progress.layer_index }}/{{ row.progress.layer_total }}
              {{ row.progress.rows ? `· ${row.progress.rows} 行` : '' }}
            </span>
            <span v-else-if="row.status === 'succeeded'" class="muted">
              {{ row.result?.ok.length ?? 0 }} 层成功
            </span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间" width="165" sortable
                       :sort-orders="['descending', 'ascending']">
          <template #default="{ row }">{{ fmtTs(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="230" fixed="right">
          <template #default="{ row }">
            <el-button size="small" link style="font-size: 14px"
                       @click="$router.push(`/tasks/${row.id}`)">详情</el-button>
            <el-button v-if="runnable(row.status)" size="small" link type="success"
                       style="font-size: 14px" :loading="acting === row.id"
                       @click="onRun(row)">运行</el-button>
            <el-button v-if="cancellable(row.status)" size="small" link type="warning"
                       style="font-size: 14px" :loading="acting === row.id"
                       @click="onCancel(row)">取消</el-button>
            <el-button v-if="editable(row.status)" size="small" link type="primary"
                       style="font-size: 14px"
                       @click="$router.push(`/tasks/${row.id}/edit`)">编辑</el-button>
            <el-popconfirm title="确认删除该任务？（日志随之删除）" @confirm="onDelete(row)">
              <template #reference>
                <el-button size="small" link type="danger" style="font-size: 14px">删除</el-button>
              </template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
      <div class="pager">
        <el-pagination
          background
          layout="total, prev, pager, next, sizes"
          :total="total"
          :page-size="pageSize"
          :current-page="page"
          :page-sizes="[10, 20, 50]"
          @current-change="(p: number) => { page = p; load() }"
          @size-change="(s: number) => { pageSize = s; page = 1; load() }"
        />
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import StatusTag from '../components/StatusTag.vue'
import { cancelTask, getTask, listTaskTypes, listTasks, runTask, deleteTask } from '../api/tasks'
import type { TaskItem, TaskStatus, TaskTypeInfo } from '../types'

const STATUS_OPTIONS: { value: TaskStatus; label: string }[] = [
  { value: 'draft', label: '草稿' },
  { value: 'queued', label: '排队中' },
  { value: 'running', label: '运行中' },
  { value: 'succeeded', label: '成功' },
  { value: 'failed', label: '失败' },
  { value: 'cancelled', label: '已取消' },
]

const types = ref<TaskTypeInfo[]>([])
const items = ref<TaskItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const acting = ref<number | null>(null)
const filters = ref({ status: '' as TaskStatus | '', type: '', q: '' })

const TERMINAL: TaskStatus[] = ['succeeded', 'failed', 'cancelled']
const runnable = (s: TaskStatus) => s === 'draft' || TERMINAL.includes(s)
const cancellable = (s: TaskStatus) => s === 'queued' || s === 'running'
const editable = (s: TaskStatus) => s !== 'queued' && s !== 'running'
const typeLabel = (t: string) => types.value.find((x) => x.type === t)?.label || t

function fmtTs(ts: string | null) {
  if (!ts) return '—'
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleString()
}

const sort = ref<{ prop: string; order: 'asc' | 'desc' }>({
  prop: 'created_at',
  order: 'desc',
})

async function load() {
  loading.value = true
  try {
    const data = await listTasks({
      status: filters.value.status,
      type: filters.value.type,
      q: filters.value.q,
      page: page.value,
      pageSize: pageSize.value,
      sortBy: sort.value.prop,
      order: sort.value.order,
    })
    items.value = data.items
    total.value = data.total
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

// 列头排序（服务端排序 + 分页）
function onSortChange({ prop, order }: { prop: string; order: string | null }) {
  if (!prop || !order) return
  const next: 'asc' | 'desc' = order === 'ascending' ? 'asc' : 'desc'
  if (sort.value.prop !== prop || sort.value.order !== next) {
    sort.value = { prop, order: next }
    reload()
  }
}

function reload() {
  page.value = 1
  load()
}

function reset() {
  filters.value = { status: '', type: '', q: '' }
  reload()
}

async function onRun(row: TaskItem) {
  acting.value = row.id
  try {
    await runTask(row.id)
    ElMessage.success('任务已提交运行')
    load()
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    acting.value = null
  }
}

async function onCancel(row: TaskItem) {
  acting.value = row.id
  try {
    await cancelTask(row.id)
    ElMessage.info('已请求取消，正在停止…')
    await load()
    // 运行中的任务是协作式取消（COPY 检查点处停止），短暂轮询等待终态
    if (row.status === 'running' || row.status === 'queued') {
      await pollUntilDone(row.id)
    }
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    acting.value = null
  }
}

// 每 1.5s 查一次直到任务终态（最多 12 次 ≈ 18s），然后刷新列表
async function pollUntilDone(id: number, tries = 12) {
  for (let i = 0; i < tries; i++) {
    await new Promise((r) => setTimeout(r, 1500))
    const t = await getTask(id).catch(() => null)
    if (!t || TERMINAL.includes(t.status)) break
  }
  load()
}

async function onDelete(row: TaskItem) {
  try {
    await deleteTask(row.id)
    ElMessage.success('已删除')
    load()
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

onMounted(async () => {
  try {
    types.value = await listTaskTypes()
  } catch {
    types.value = []
  }
  load()
})
</script>

<style scoped>
.toolbar {
  display: flex;
  gap: 10px;
  align-items: center;
}
.pager {
  margin-top: 14px;
  display: flex;
  justify-content: flex-end;
}
</style>