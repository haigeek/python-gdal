<template>
  <div>
    <el-page-header :content="isEdit ? `编辑任务 #${id}` : '新建任务'" @back="$router.back()" style="margin-bottom: 14px" />

    <el-card shadow="never" class="page-card">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <b>{{ isEdit ? `编辑任务 #${id}` : '新建任务' }}（{{ typeLabel }}）</b>
          <div>
            <el-button v-if="currentSchema" type="primary" plain :loading="previewing" @click="doPreview">预览（dry-run）</el-button>
            <el-button v-if="currentSchema" type="primary" :loading="saving" @click="save">保存</el-button>
          </div>
        </div>
      </template>
      <el-form label-width="120px" label-position="left" style="max-width: 560px">
        <el-form-item label="任务名称" required>
          <el-input v-model="name" placeholder="便于识别的名称（默认=类型名）" maxlength="200" />
        </el-form-item>
        <el-form-item v-if="!isEdit" label="任务类型" required>
          <el-select v-model="selectedType" placeholder="选择任务类型" style="width: 100%">
            <el-option v-for="t in types" :key="t.type" :label="t.label" :value="t.type" />
          </el-select>
        </el-form-item>
      </el-form>
      <template v-if="currentSchema">
        <el-divider style="margin: 12px 0" />
        <dynamic-form
          v-model="config"
          :groups="currentSchema.groups"
          :testing-key="testingKey"
          @group-test="onGroupTest"
          @group-action="onGroupAction"
        />
        <data-source-picker-dialog v-model="dsPickerVisible" @select="applyDataSource" />
        <el-alert
          v-if="testResult"
          :type="testResult.ok ? 'success' : 'error'"
          :title="testResult.title"
          :description="testResult.detail"
          :closable="false"
          show-icon
          style="margin-top: 12px"
        />
        <div style="margin-top: 8px">
          <el-divider content-position="left">高级：完整配置 JSON（类型自述 schema 之外的字段）</el-divider>
          <el-input v-model="advancedJson" type="textarea" :rows="6" class="mono" placeholder="{}" />
        </div>
      </template>
    </el-card>

    <el-card v-if="preview" shadow="never">
      <template #header><b>导入计划预览</b></template>
      <plan-preview :data="preview" />
    </el-card>
    <el-empty v-else-if="!isEdit && !loadingMeta" description="请先选择任务类型" />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import DynamicForm from '../components/DynamicForm.vue'
import DataSourcePickerDialog from '../components/DataSourcePickerDialog.vue'
import PlanPreview from '../components/PlanPreview.vue'
import { createTask, fetchGdbLayers, getTask, listTaskTypes, previewTask, testDatabase, updateTask } from '../api/tasks'
import type { DataSourceItem, FormSchema, PreviewData, TaskTypeInfo } from '../types'

const route = useRoute()
const router = useRouter()
const id = computed(() => (route.params.id ? Number(route.params.id) : null))
const isEdit = computed(() => id.value !== null)

const types = ref<TaskTypeInfo[]>([])
const loadingMeta = ref(true)
const selectedType = ref('')
const name = ref('')
const config = ref<Record<string, any>>({})
const advancedJson = ref('')
const preview = ref<PreviewData | null>(null)
const previewing = ref(false)
const saving = ref(false)
const testingKey = ref<string | null>(null)
const testResult = ref<{ ok: boolean; title: string; detail: string } | null>(null)
const dsPickerVisible = ref(false)

// 组自定义动作：目前「目标数据库」组的「从数据源选择」
function onGroupAction(payload: { group: string; action: string }) {
  if (payload.group === 'database' && payload.action === 'from_datasource') {
    dsPickerVisible.value = true
  }
  if (payload.group === 'layers' && payload.action === 'read_gdb') {
    const gdb = config.value.gdb
    if (!gdb) {
      ElMessage.warning('请先填写 GDB 路径')
      return
    }
    if (readingLayers.value) return
    readingLayers.value = true
    loadLayers(gdb)
      .catch((e) => ElMessage.error(`读取图层失败：${e instanceof Error ? e.message : String(e)}`))
      .finally(() => {
        readingLayers.value = false
      })
  }
}

// 选用数据源：填入 database 组（密码为脱敏 "***"，携带 datasource_id 由后端
// 在保存/检测时解析真实密码；用户可直接修改任意字段微调）
function applyDataSource(ds: DataSourceItem) {
  const db: Record<string, any> = { ...(ds.config as Record<string, any>) }
  db.datasource_id = ds.id
  config.value.database = db
  testResult.value = null
  dsPickerVisible.value = false
  ElMessage.success(`已选用数据源「${ds.name}」，可在表单中微调参数`)
}

// GDB 路径变化（防抖）→ 自动读取图层并填充「图层规则」表：
// - 仅新建任务自动填充；编辑任务尊重已保存的规则，不自动增补
// - 同一 GDB 只自动填充一次（用户删行后不会被加回来）
// - 过期响应（GDB 已再次变化）丢弃
// 读取 GDB 图层并合并进「图层规则」表：保留已有行，增补缺失图层。
// 供自动（新建防抖）与手动「读取图层」按钮共用。
async function loadLayers(gdb: string) {
  const data = await fetchGdbLayers(gdb)
  if (gdb !== config.value.gdb) return
  const kept = new Map<string, Record<string, any>>()
  for (const r of config.value.layers || []) kept.set(String(r.source), r)
  for (const l of data.layers) {
    if (!kept.has(l.source)) kept.set(l.source, { source: l.source })
  }
  config.value.layers = [...kept.values()]
  ElMessage.success(
    `已自动读取 ${data.layers.length} 个图层，请在下方表格中选择要导入的图层并设置导入模式`,
  )
}

let gdbTimer: ReturnType<typeof setTimeout> | null = null
const lastFetchedGdb = ref<string | null>(null)
const readingLayers = ref(false)
watch(
  () => config.value.gdb,
  (gdb) => {
    if (gdbTimer) clearTimeout(gdbTimer)
    if (!gdb || isEdit.value) return
    gdbTimer = setTimeout(async () => {
      if (lastFetchedGdb.value === gdb) return
      lastFetchedGdb.value = gdb
      try {
        readingLayers.value = true
        await loadLayers(gdb)
      } catch {
        // 打不开或路径越界：静默，交由预览/保存时报错
      } finally {
        readingLayers.value = false
      }
    }, 600)
  },
)

const currentSchema = computed<FormSchema | null>(() => {
  const t = types.value.find((x) => x.type === (isEdit.value ? editType.value : selectedType.value))
  return t?.form_schema ?? null
})
const typeLabel = computed(() => {
  const t = types.value.find((x) => x.type === (isEdit.value ? editType.value : selectedType.value))
  return t?.label ?? ''
})
const editType = ref('')

// 依据 form_schema 生成默认 config
function defaultConfig(schema: FormSchema): Record<string, any> {
  const cfg: Record<string, any> = {}
  for (const g of schema.groups) {
    if (g.single) {
      cfg[g.key] = g.fields[0]?.default ?? ''
    } else if (g.fields.some((f) => f.type === 'table')) {
      cfg[g.key] = []
    } else {
      const obj: Record<string, any> = {}
      for (const f of g.fields) {
        obj[f.key] = f.default ?? (f.type === 'bool' ? false : f.type === 'int' ? null : '')
      }
      cfg[g.key] = obj
    }
  }
  return cfg
}

// 把表单 config 与高级 JSON 合并（高级 JSON 覆盖同名键）
function mergedConfig(): Record<string, any> {
  const merged = JSON.parse(JSON.stringify(config.value))
  try {
    const extra = JSON.parse(advancedJson.value || '{}')
    if (typeof extra === 'object' && extra !== null) Object.assign(merged, extra)
  } catch {
    ElMessage.error('高级 JSON 解析失败，请检查格式')
    throw new Error('JSON 解析失败')
  }
  return merged
}

async function doPreview() {
  const type = isEdit.value ? editType.value : selectedType.value
  if (!type) return
  previewing.value = true
  preview.value = null
  try {
    preview.value = await previewTask({ type, config: mergedConfig() })
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    previewing.value = false
  }
}

// 组「检测」：目前仅目标数据库（database）连接检测
async function onGroupTest(groupKey: string) {
  if (groupKey !== 'database') {
    ElMessage.info('该组暂不支持检测')
    return
  }
  const db = (mergedConfig().database as Record<string, any>) ?? {}
  if (!db.host || !db.dbname) {
    ElMessage.warning('请先填写数据库连接信息（至少 host 与 dbname）')
    return
  }
  testingKey.value = groupKey
  testResult.value = null
  try {
    const r = await testDatabase({
      database: db,
      task_id: isEdit.value ? id.value! : undefined,
    })
    testResult.value = {
      ok: true,
      title: '数据库连接成功',
      detail: `PostgreSQL ${r.server_version}` +
        (r.postgis_version ? ` · PostGIS ${r.postgis_version}` : '') +
        ` · schema ${r.schema}${r.schema_exists ? '（存在）' : '（不存在）'} · ${r.latency_ms}ms`,
    }
  } catch (e) {
    testResult.value = { ok: false, title: '数据库检测失败', detail: (e as Error).message }
  } finally {
    testingKey.value = null
  }
}

async function save() {
  const type = isEdit.value ? editType.value : selectedType.value
  if (!type) {
    ElMessage.warning('请选择任务类型')
    return
  }
  if (!name.value.trim()) name.value = type
  saving.value = true
  try {
    let taskId: number
    if (isEdit.value) {
      await updateTask(id.value!, { name: name.value.trim(), config: mergedConfig() })
      taskId = id.value!
    } else {
      const item = await createTask({ type, name: name.value.trim(), config: mergedConfig() })
      taskId = item.id
    }
    ElMessage.success('已保存')
    router.push(`/tasks/${taskId}`)
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    saving.value = false
  }
}

watch(selectedType, (t) => {
  const schema = types.value.find((x) => x.type === t)?.form_schema
  if (schema) {
    config.value = defaultConfig(schema)
    advancedJson.value = '{}'
    preview.value = null
  }
})

onMounted(async () => {
  loadingMeta.value = true
  try {
    types.value = await listTaskTypes()
    if (isEdit.value) {
      const item = await getTask(id.value!)
      editType.value = item.type
      name.value = item.name
      config.value = JSON.parse(JSON.stringify(item.config ?? {}))
      advancedJson.value = '{}'
    } else if (types.value.length) {
      selectedType.value = types.value[0].type
    }
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    loadingMeta.value = false
  }
})
</script>