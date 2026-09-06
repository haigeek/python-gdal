<template>
  <el-dialog v-model="visible" title="从数据源选择（选用后填入表单，可微调）" width="640px">
    <el-table :data="items" v-loading="loading" empty-text="暂无数据源，请先在「数据源」页创建">
      <el-table-column prop="name" label="名称" min-width="140" />
      <el-table-column label="主机:端口" min-width="150">
        <template #default="{ row }">{{ row.config.host }}:{{ row.config.port }}</template>
      </el-table-column>
      <el-table-column label="数据库 / schema" min-width="160">
        <template #default="{ row }">{{ row.config.dbname }} · {{ row.config.schema || 'public' }}</template>
      </el-table-column>
      <el-table-column label="操作" width="160" fixed="right">
        <template #default="{ row }">
          <el-button type="primary" size="small" plain @click="emit('select', row)">选用</el-button>
          <el-button size="small" link @click="$router.push('/datasources')">管理</el-button>
        </template>
      </el-table-column>
    </el-table>
    <template #footer>
      <el-button type="primary" link @click="$router.push('/datasources')">
        去「数据源管理」新建 →
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { listDataSources } from '../api/datasources'
import type { DataSourceItem } from '../types'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'select', item: DataSourceItem): void
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

const items = ref<DataSourceItem[]>([])
const loading = ref(false)

watch(
  () => props.modelValue,
  (v) => {
    if (v) reload()
  },
)

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

onMounted(() => {
  if (props.modelValue) reload()
})
</script>