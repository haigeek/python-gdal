<template>
  <el-tag :type="tagType" size="small" disable-transitions>{{ label }}</el-tag>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { TaskStatus } from '../types'

const props = defineProps<{ status: TaskStatus }>()

const MAP: Record<TaskStatus, { label: string; type: 'success' | 'info' | 'warning' | 'danger' | 'primary' }> = {
  draft: { label: '草稿', type: 'info' },
  queued: { label: '排队中', type: 'warning' },
  running: { label: '运行中', type: 'primary' },
  succeeded: { label: '成功', type: 'success' },
  failed: { label: '失败', type: 'danger' },
  cancelled: { label: '已取消', type: 'info' },
}

const tagType = computed(() => MAP[props.status].type)
const label = computed(() => MAP[props.status].label)
</script>