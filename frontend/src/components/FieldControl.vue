<template>
  <el-input
    v-if="type === 'text'"
    :model-value="modelValue as string"
    :placeholder="field.placeholder || `请输入${field.label}`"
    style="max-width: 420px; width: 100%"
    @update:model-value="emitValue"
  />
  <el-input
    v-else-if="type === 'password'"
    type="password"
    show-password
    :model-value="modelValue as string"
    :placeholder="field.placeholder || `请输入${field.label}`"
    autocomplete="new-password"
    style="max-width: 420px; width: 100%"
    @update:model-value="emitValue"
  />
  <el-input-number
    v-else-if="type === 'int'"
    :model-value="modelValue as number | null"
    :controls="false"
    :min="1"
    :placeholder="'留空 = 继承'"
    style="max-width: 420px; width: 100%"
    @update:model-value="emitValue"
  />
  <el-switch
    v-else-if="type === 'bool'"
    :model-value="!!modelValue"
    @update:model-value="emitValue"
  />
  <el-select
    v-else-if="type === 'enum'"
    :model-value="modelValue as string"
    clearable
    style="max-width: 420px; width: 100%"
    @update:model-value="emitValue"
  >
    <el-option v-for="opt in field.options || []" :key="opt" :label="opt" :value="opt" />
  </el-select>
</template>

<script setup lang="ts">
import type { FieldDef } from '../types'

const props = defineProps<{ field: FieldDef; modelValue: unknown }>()
const emit = defineEmits<{ (e: 'update:modelValue', v: unknown): void }>()

const type = props.field.type

function emitValue(v: unknown) {
  if (type === 'int' && (v === null || v === undefined || v === '')) {
    emit('update:modelValue', null)
    return
  }
  emit('update:modelValue', v)
}
</script>