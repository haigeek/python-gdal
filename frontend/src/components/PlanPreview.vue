<template>
  <div>
    <el-alert v-if="data.error" type="error" :title="data.error" :closable="false" style="margin-bottom: 12px" />
    <div v-if="data.db_error" class="muted" style="margin-bottom: 8px">⚠ {{ data.db_error }}</div>
    <el-descriptions v-if="data.postgis" :column="2" size="small" border style="margin-bottom: 12px">
      <el-descriptions-item label="数据源">{{ data.shp || data.gdb }}</el-descriptions-item>
      <el-descriptions-item label="目标库 PostGIS">{{ data.postgis }}</el-descriptions-item>
    </el-descriptions>
    <el-alert
      v-if="data.db_checks.length"
      type="warning"
      :closable="false"
      style="margin-bottom: 12px"
      :title="`${data.db_checks.length} 个表冲突提示`"
    >
      <div v-for="(c, i) in data.db_checks" :key="i">· {{ c }}</div>
    </el-alert>

    <el-table v-if="data.layers.length" :data="data.layers" size="small" border>
      <el-table-column prop="source" label="源图层" min-width="140" />
      <el-table-column prop="table" label="目标表" min-width="140" />
      <el-table-column prop="feature_count" label="要素数" width="90" align="right" />
      <el-table-column label="几何" min-width="140">
        <template #default="{ row }">
          <span v-if="row.geometry">{{ row.geometry }} ({{ row.srid ?? '?' }})</span>
          <span v-else>无</span>
        </template>
      </el-table-column>
      <el-table-column prop="mode" label="模式" width="90" />
      <el-table-column label="主键" min-width="170">
        <template #default="{ row }">
          <span v-if="row.pk_source === 'field'">{{ row.pk_field }}（源字段）</span>
          <span v-else-if="row.pk_source === 'fid'">{{ row.pk_column }}（原生 FID）</span>
          <span v-else>{{ row.pk_column }}（自增）</span>
        </template>
      </el-table-column>
      <el-table-column label="字段" min-width="160">
        <template #default="{ row }">
          <span v-if="row.columns.length" class="muted">
            {{ row.columns.slice(0, 4).map((c: any) => `${c.dst}:${c.pg}`).join('，') }}
            {{ row.columns.length > 4 ? `…(+${row.columns.length - 4})` : '' }}
          </span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="提示" min-width="180">
        <template #default="{ row }">
          <div v-for="(w, i) in row.issues" :key="'w' + i" class="warn">{{ w }}</div>
          <div v-for="(e, i) in row.errors" :key="'e' + i" class="err">{{ e }}</div>
        </template>
      </el-table-column>
    </el-table>
    <el-empty v-else description="未生成任何图层计划" />
  </div>
</template>

<script setup lang="ts">
import type { PreviewData } from '../types'

defineProps<{ data: PreviewData }>()
</script>

<style scoped>
.warn {
  color: #e6a23c;
  font-size: 12px;
}
.err {
  color: #f56c6c;
  font-size: 12px;
}
</style>
