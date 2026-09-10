<template>
  <div class="gdb-picker">
    <div class="picker-row">
    <el-input
      v-model="localPath"
      placeholder="服务器上的 GDB 路径"
      clearable
      style="flex: 1; min-width: 0"
    />
    <el-button @click="openBrowse">浏览</el-button>
    <el-button @click="openUpload">上传 zip</el-button>
  </div>

    <!-- 目录浏览 -->
    <el-dialog v-model="browseVisible" title="浏览服务器目录" width="720px">
      <div class="crumbs mono">
        <el-link v-if="browse?.parent" type="primary" :underline="false" @click="goParent">
          ⬆ {{ browse?.parent }}
        </el-link>
        <span v-else>当前目录：{{ browse?.path }}</span>
      </div>
      <el-table :data="browse?.entries || []" height="360" size="small"
                highlight-current-row @current-change="selectEntry">
        <el-table-column label="名称">
          <template #default="{ row }">
            <span style="margin-right: 6px">{{ row.is_dir ? '📁' : '📄' }}</span>
            {{ row.name }}
          </template>
        </el-table-column>
        <el-table-column label="类型" width="120">
          <template #default="{ row }">
            <el-tag v-if="row.is_gdb" size="small" type="success">GDB</el-tag>
            <el-tag v-else-if="row.is_dir" size="small">目录</el-tag>
            <span v-else class="muted">{{ row.size }} B</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150">
          <template #default="{ row }">
            <el-button v-if="row.is_dir" size="small" link type="primary" @click="enterDir(row)">进入</el-button>
            <el-button v-if="row.is_gdb" size="small" link type="success" @click="usePath(row)">使用此 GDB</el-button>
          </template>
        </el-table-column>
      </el-table>
      <template #footer>
        <el-button @click="browseVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- zip 上传 -->
    <el-dialog v-model="uploadVisible" title="上传 GDB 压缩包（.zip）" width="520px">
      <el-upload drag :auto-upload="false" :show-file-list="false" :on-change="onFileChange"
                 accept=".zip">
        <div style="font-size: 40px; line-height: 1">📦</div>
        <div class="el-upload__text">拖拽 .zip 到这里或<em>点击选择</em></div>
        <template #tip>
          <div class="el-upload__tip">请打包【整个 .gdb 文件夹】为 zip；上传后自动解压并定位 GDB。</div>
        </template>
      </el-upload>
      <el-progress v-if="uploading" :percentage="uploadPct" :stroke-width="12" style="margin-top: 12px" />
      <div v-if="uploadNote" class="mono muted" style="margin-top: 8px">{{ uploadNote }}</div>
      <template #footer>
        <el-button @click="uploadVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { browseDir, uploadZip } from '../api/tasks'
import type { BrowseData, BrowseEntry } from '../types'

const props = defineProps<{ path: string }>()
const emit = defineEmits<{ (e: 'update:path', value: string): void }>()

const localPath = ref(props.path)
watch(
  () => props.path,
  (v) => {
    localPath.value = v || ''
  },
)

function commit(value: string) {
  localPath.value = value
  emit('update:path', value)
}

// ---------------- 目录浏览 ----------------
const browseVisible = ref(false)
const browse = ref<BrowseData | null>(null)
const currentEntry = ref<BrowseEntry | null>(null)

async function openBrowse() {
  browseVisible.value = true
  currentEntry.value = null
  try {
    browse.value = await browseDir(localPath.value || undefined)
  } catch (e) {
    ElMessage.error((e as Error).message)
    browse.value = null
  }
}

async function enterDir(row: BrowseEntry) {
  try {
    const next = `${browse.value?.path}/${row.name}`
    browse.value = await browseDir(next)
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

function goParent() {
  if (!browse.value?.parent) return
  browseDir(browse.value.parent)
    .then((d) => (browse.value = d))
    .catch((e) => ElMessage.error((e as Error).message))
}

function selectEntry(row: BrowseEntry) {
  currentEntry.value = row
}

function usePath(row: BrowseEntry) {
  commit(`${browse.value?.path}/${row.name}`)
  browseVisible.value = false
  ElMessage.success('已选用 GDB 路径')
}

// ---------------- zip 上传 ----------------
const uploadVisible = ref(false)
const uploading = ref(false)
const uploadPct = ref(0)
const uploadNote = ref('')

function openUpload() {
  uploadVisible.value = true
  uploadNote.value = ''
}

async function onFileChange(file: { raw?: File }) {
  const raw = file.raw
  if (!raw) return
  uploading.value = true
  uploadPct.value = 0
  uploadNote.value = ''
  try {
    const data = await uploadZip(raw, (pct) => (uploadPct.value = pct))
    if (!data.gdb_path) throw new Error('服务端未返回 GDB 路径')
    commit(data.gdb_path)
    if (data.cached) {
      uploadNote.value = `✅ 相同文件已上传过，直接复用：${data.gdb_path}`
    } else {
      const shown = data.layers.slice(0, 6).join('、')
      uploadNote.value = `✅ 已解压：${data.gdb_path}；图层 ${data.layers.length} 个：${shown}${data.layers.length > 6 ? '…' : ''}`
    }
    ElMessage.success(data.cached ? '已复用历史上的相同上传' : '上传成功，GDB 路径已填入')
  } catch (e) {
    ElMessage.error((e as Error).message)
    uploadNote.value = ''
  } finally {
    uploading.value = false
  }
}
</script>

<style scoped>
.picker-row {
  display: flex;
  gap: 8px;
  width: 100%;
}
.crumbs {
  margin-bottom: 10px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>