<template>
  <div ref="box" class="log-box">
    <div v-if="logs.length === 0" class="muted">（暂无日志）</div>
    <div v-for="l in logs" :key="l.id">
      <span class="log-ts">[{{ fmtTs(l.ts) }}]</span> {{ l.message }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import type { LogEntry } from '../types'

const props = defineProps<{ logs: LogEntry[] }>()
const box = ref<HTMLElement | null>(null)

function fmtTs(ts: string) {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

watch(
  () => props.logs.length,
  async () => {
    await nextTick()
    if (box.value) box.value.scrollTop = box.value.scrollHeight
  },
)
</script>

<style scoped>
.log-ts {
  color: #6e7681;
  margin-right: 6px;
}
</style>