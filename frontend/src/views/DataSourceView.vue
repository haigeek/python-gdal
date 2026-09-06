<template>
  <div>
    <el-card shadow="never">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <b>数据源管理</b>
          <el-button type="primary" @click="openCreate">新建数据源</el-button>
        </div>
      </template>
      <el-table :data="items" v-loading="loading" empty-text="暂无数据源">
        <el-table-column prop="name" label="名称" min-width="150" />
        <el-table-column label="主机:端口" min-width="160">
          <template #default="{ row }">{{ row.config.host }}:{{ row.config.port }}</template>
        </el-table-column>
        <el-table-column label="数据库 / 用户" min-width="180">
          <template #default="{ row }">{{ row.config.dbname }}（{{ row.config.user }}）</template>
        </el-table-column>
        <el-table-column label="schema" width="110">
          <template #default="{ row }">{{ row.config.schema || 'public' }}</template>
        </el-table-column>
        <el-table-column label="更新时间" width="170">
          <template #default="{ row }">{{ (row.updated_at || row.created_at || '').replace('T', ' ').slice(0, 16) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button size="small" link style="font-size: 14px" :loading="testingId === row.id"
                       @click="testRow(row)">测试连接</el-button>
            <el-button size="small" link style="font-size: 14px"
                       @click="openEdit(row)">编辑</el-button>
            <el-button size="small" link type="danger" style="font-size: 14px"
                       @click="removeRow(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="editing ? `编辑数据源 #${editing.id}（${editing.name}）` : '新建数据源'" width="560px">
      <el-form label-width="140px" label-position="left">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" placeholder="唯一名称，如：生产 GIS 库" maxlength="100" />
        </el-form-item>
        <el-form-item label="主机" required>
          <el-input v-model="form.host" placeholder="127.0.0.1" />
        </el-form-item>
        <el-form-item label="端口">
          <el-input-number v-model="form.port" :min="1" :max="65535" controls-position="right" style="width: 100%" />
        </el-form-item>
        <el-form-item label="数据库名" required>
          <el-input v-model="form.dbname" />
        </el-form-item>
        <el-form-item label="用户" required>
          <el-input v-model="form.user" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            :placeholder="editing ? '已保存密码，重填可覆盖' : '可留空（连接不需要密码时）'"
          />
        </el-form-item>
        <el-form-item label="schema">
          <el-input v-model="form.schema" placeholder="public" />
        </el-form-item>
        <el-form-item label="SSL 模式">
          <el-select v-model="form.ssl" style="width: 100%">
            <el-option v-for="s in ['prefer', 'require', 'disable', 'allow']" :key="s" :label="s" :value="s" />
          </el-select>
        </el-form-item>
      </el-form>
      <div class="test-hint" :class="{ ok: testOk, bad: testOk === false }">{{ testHint }}</div>
      <template #footer>
        <el-button :loading="testing" @click="testForm">测试连接</el-button>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createDataSource,
  deleteDataSource,
  listDataSources,
  updateDataSource,
} from '../api/datasources'
import { testDatabase } from '../api/tasks'
import type { DataSourceItem, DatabaseTestResult } from '../types'

const items = ref<DataSourceItem[]>([])
const loading = ref(false)
const dialogVisible = ref(false)
const editing = ref<DataSourceItem | null>(null)
const saving = ref(false)
const testing = ref(false)
const testingId = ref<number | null>(null)
const testHint = ref('')
const testOk = ref<boolean | null>(null)

const form = ref<Record<string, any>>({})

function blankForm() {
  return {
    name: '',
    host: '127.0.0.1',
    port: 5432,
    dbname: '',
    user: '',
    password: '',
    schema: 'public',
    ssl: 'prefer',
  }
}

function collectConfig(): Record<string, any> {
  const cfg: Record<string, any> = {
    host: form.value.host.trim(),
    port: form.value.port ?? 5432,
    dbname: form.value.dbname.trim(),
    user: form.value.user.trim(),
    schema: form.value.schema.trim() || 'public',
    ssl: form.value.ssl || 'prefer',
  }
  if (form.value.password !== '') cfg.password = form.value.password
  return cfg
}

async function reload() {
  loading.value = true
  try {
    items.value = await listDataSources()
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editing.value = null
  form.value = blankForm()
  testHint.value = ''
  testOk.value = null
  dialogVisible.value = true
}

function openEdit(row: DataSourceItem) {
  editing.value = row
  const c = row.config as Record<string, any>
  form.value = {
    name: row.name,
    host: c.host ?? '',
    port: c.port ?? 5432,
    dbname: c.dbname ?? '',
    user: c.user ?? '',
    password: c.password ?? '', // 脱敏的 "***"：不填则保留
    schema: c.schema ?? 'public',
    ssl: c.ssl ?? 'prefer',
  }
  testHint.value = ''
  testOk.value = null
  dialogVisible.value = true
}

async function save() {
  if (!form.value.name.trim()) return ElMessage.warning('请填写名称')
  if (!form.value.host.trim() || !form.value.dbname.trim() || !form.value.user.trim()) {
    return ElMessage.warning('主机/数据库名/用户为必填')
  }
  saving.value = true
  try {
    if (editing.value) {
      await updateDataSource(editing.value.id, { config: collectConfig() })
    } else {
      await createDataSource({ name: form.value.name.trim(), config: collectConfig() })
    }
    ElMessage.success('已保存')
    dialogVisible.value = false
    await reload()
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    saving.value = false
  }
}

async function removeRow(row: DataSourceItem) {
  try {
    await ElMessageBox.confirm(`删除数据源「${row.name}」？已保存任务不受影响（任务保存的是独立配置副本）。`, '确认删除', { type: 'warning' })
  } catch {
    return
  }
  try {
    await deleteDataSource(row.id)
    ElMessage.success('已删除')
    await reload()
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

function describeResult(r: DatabaseTestResult): string {
  return `已连接 · PostgreSQL ${r.server_version}` +
    (r.postgis_version ? ` · PostGIS ${r.postgis_version}` : '') +
    ` · schema ${r.schema}${r.schema_exists ? '（存在）' : '（不存在）'} · ${r.latency_ms}ms`
}

async function testOf(database: Record<string, any>) {
  testHint.value = ''
  testOk.value = null
  try {
    testOk.value = true
    testHint.value = describeResult(await testDatabase({ database }))
  } catch (e) {
    testOk.value = false
    testHint.value = (e as Error).message
  }
}

async function testRow(row: DataSourceItem) {
  testingId.value = row.id
  try {
    // 密码是脱敏的 "***"：带 datasource_id，后端从库取真实密码检测
    const r = await testDatabase({
      database: { ...(row.config as Record<string, any>), datasource_id: row.id },
    })
    await ElMessageBox.alert(describeResult(r), '连接成功', {
      type: 'success',
      confirmButtonText: '好',
    })
  } catch (e) {
    await ElMessageBox.alert((e as Error).message, '连接失败', {
      type: 'error',
      confirmButtonText: '好',
    })
  } finally {
    testingId.value = null
  }
}

async function testForm() {
  testing.value = true
  try {
    const cfg = collectConfig()
    if (editing.value) cfg.datasource_id = editing.value.id
    await testOf(cfg)
  } finally {
    testing.value = false
  }
}

onMounted(reload)
</script>

<style scoped>
.test-hint {
  font-size: 13px;
  min-height: 20px;
  margin-bottom: 4px;
}
.test-hint.ok {
  color: #67c23a;
}
.test-hint.bad {
  color: #f56c6c;
}
</style>