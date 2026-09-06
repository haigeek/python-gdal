<template>
  <div class="dynamic-form">
    <el-card v-for="group in groups" :key="group.key" shadow="never" class="form-group">
      <template #header>
        <div class="group-header">
          <b>{{ group.label }}</b>
          <div style="display: flex; gap: 8px">
            <el-button
              v-for="a in group.actions || []"
              :key="a.key"
              size="small"
              @click="emit('group-action', { group: group.key, action: a.key })"
            >{{ a.label }}</el-button>
            <el-button
              v-if="group.test"
              size="small"
              type="primary"
              plain
              :loading="testingKey === group.key"
              @click="emit('group-test', group.key)"
            >检测</el-button>
          </div>
        </div>
      </template>
      <el-form label-width="auto" label-position="left" class="dform-label">
        <!-- 单值组（如 gdb） -->
        <el-form-item v-if="group.single" v-for="f in fieldsOf(group)" :key="f.key" :label="f.label">
          <gdb-path-picker
            v-if="f.type === 'gdb_path'"
            :path="(local[group.key] as string) || ''"
            @update:path="(v: string) => { local[group.key] = v; emitChange() }"
          />
          <field-control
            v-else
            :field="f"
            :model-value="local[group.key]"
            @update:model-value="(v: unknown) => { local[group.key] = v; emitChange() }"
          />
          <div v-if="f.help" class="muted field-help">{{ f.help }}</div>
        </el-form-item>

        <!-- 对象组（database / default）：字段单列 -->
        <el-form-item v-else v-for="f in fieldsOf(group)" :key="f.key" :label="f.label">
          <field-control
            :field="f"
            :model-value="nested(group.key, f.key)"
            @update:model-value="(v: unknown) => setField(group, f, v)"
          />
          <div v-if="f.help" class="muted field-help">{{ f.help }}</div>
        </el-form-item>

        <!-- 表格组（layers） -->
        <el-form-item v-if="tableField(group)" :label="tableField(group)!.label">
          <div style="width: 100%">
            <div v-for="(row, idx) in tableRows(group)" :key="idx" class="table-row">
              <el-input
                v-for="col in textCols(group)"
                :key="col.key"
                :model-value="row[col.key] as string"
                :placeholder="col.label"
                style="flex: 1"
                @update:model-value="(v: string) => setCell(row, col.key, v)"
              />
              <el-input-number
                v-for="col in intCols(group)"
                :key="col.key"
                :model-value="row[col.key] as number | null"
                :controls="false"
                :min="1"
                :placeholder="col.label"
                style="width: 130px"
                @update:model-value="(v: number | null) => setCell(row, col.key, v)"
              />
              <el-select
                v-for="col in enumCols(group)"
                :key="col.key"
                :model-value="row[col.key] as string"
                :placeholder="col.label"
                clearable
                style="width: 120px"
                @update:model-value="(v: string) => setCell(row, col.key, v)"
              >
                <el-option v-for="opt in col.options || []" :key="opt" :label="opt" :value="opt" />
              </el-select>
              <el-button size="small" type="danger" link @click="removeRow(group, idx)">删除</el-button>
            </div>
            <el-button size="small" type="primary" plain @click="addRow(group)">+ 添加规则</el-button>
          </div>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { reactive, watch } from 'vue'
import FieldControl from './FieldControl.vue'
import GdbPathPicker from './GdbPathPicker.vue'
import type { FormGroup, TableColumnDef } from '../types'

const props = defineProps<{
  groups: FormGroup[]
  modelValue: Record<string, any>
  testingKey?: string | null
}>()
const emit = defineEmits<{
  (e: 'update:modelValue', value: Record<string, any>): void
  (e: 'group-test', key: string): void
  (e: 'group-action', payload: { group: string; action: string }): void
}>()

const local = reactive<Record<string, any>>(deepClone(props.modelValue))

function deepClone(v: unknown): any {
  return JSON.parse(JSON.stringify(v ?? {}))
}

// 外部整体替换 modelValue（如「选用数据源」填充）时同步到本地表单；
// 与本地内容一致（含用户打字中 emit 回来的值）则不动，避免打断编辑。
watch(
  () => props.modelValue,
  (v) => {
    const next = deepClone(v)
    if (JSON.stringify(next) === JSON.stringify(local)) return
    for (const k of Object.keys(local)) delete local[k]
    Object.assign(local, next)
  },
  { deep: true },
)

function emitChange() {
  emit('update:modelValue', deepClone(local))
}

function fieldsOf(group: FormGroup) {
  return group.fields.filter((f) => f.type !== 'table')
}

function tableField(group: FormGroup) {
  return group.fields.find((f) => f.type === 'table')
}

function nested(groupKey: string, fieldKey: string) {
  const obj = (local[groupKey] ??= {})
  return obj[fieldKey]
}

function setField(group: FormGroup, f: { key: string }, v: unknown) {
  const obj = (local[group.key] ??= {})
  obj[f.key] = v
  emitChange()
}

function tableRows(group: FormGroup) {
  if (!Array.isArray(local[group.key])) local[group.key] = []
  return local[group.key] as Record<string, unknown>[]
}

function colsOf(group: FormGroup, t: TableColumnDef['type']) {
  return (tableField(group)?.columns || []).filter((c) => c.type === t)
}
const textCols = (g: FormGroup) => colsOf(g, 'text')
const intCols = (g: FormGroup) => colsOf(g, 'int')
const enumCols = (g: FormGroup) => colsOf(g, 'enum')

function setCell(row: Record<string, unknown>, key: string, v: unknown) {
  row[key] = v
  emitChange()
}

function addRow(group: FormGroup) {
  const row: Record<string, unknown> = {}
  for (const col of tableField(group)?.columns || []) {
    row[col.key] = col.type === 'int' ? null : ''
  }
  tableRows(group).push(row)
  emitChange()
}

function removeRow(group: FormGroup, idx: number) {
  tableRows(group).splice(idx, 1)
  emitChange()
}
</script>

<style scoped>
/* 分组卡片单栏纵向堆叠，卡片内字段单列 */
.form-group {
  margin-bottom: 16px;
}
.group-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
/* 表单标签不换行（过长 label 已精简，140px 内单行显示） */
.dform-label :deep(.el-form-item__label) {
  white-space: nowrap;
}
.field-help {
  flex-basis: 100%;
  width: 100%;
  line-height: 1.5;
}
.table-row {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
</style>