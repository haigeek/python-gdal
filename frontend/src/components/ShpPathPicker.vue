<template>
  <div class="shp-picker">
    <div class="picker-row">
      <el-input
        :model-value="localPath"
        placeholder="服务器上的 SHP 文件路径"
        @update:model-value="(v: string) => commit(v)"
        clearable
        style="flex: 1; min-width: 0"
      />
      <el-button @click="openBrowse">浏览</el-button>
      <el-button @click="openUpload">上传 zip</el-button>
    </div>

    <el-dialog v-model="browseVisible" title="浏览服务器目录（选择 .shp）" width="720px">
      <div class="crumbs mono">
        <el-link v-if="browse?.parent" type="primary" :underline="false" @click="goParent">
          ⬆ {{ browse?.parent }}
        </el-link>
        <span v-else>当前目录：{{ browse?.path }}</span>
      </div>
      <el-table :data="browse?.entries || []" height="360" size="small">
        <el-table-column label="名称">
          <template #default="{ row }">
            <span style="margin-right: 6px">{{ row.is_dir ? '📁' : '📄' }}</span>{{ row.name }}
          </template>
        </el-table-column>
        <el-table-column label="类型" width="120">
          <template #default="{ row }">
            <el-tag v-if="row.is_shp" size="small" type="success">SHP</el-tag>
            <el-tag v-else-if="row.is_dir" size="small">目录</el-tag>
            <span v-else class="muted">{{ row.size }} B</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150">
          <template #default="{ row }">
            <el-button v-if="row.is_dir" size="small" link type="primary" @click="enterDir(row)">进入</el-button>
            <el-button v-if="row.is_shp" size="small" link type="success" @click="usePath(row)">使用此 SHP</el-button>
          </template>
        </el-table-column>
      </el-table>
      <template #footer><el-button @click="browseVisible = false">关闭</el-button></template>
    </el-dialog>

    <el-dialog v-model="uploadVisible" title="上传 SHP 压缩包（.zip）" width="520px">
      <el-upload drag :auto-upload="false" :show-file-list="false" :on-change="onFileChange" accept=".zip">
        <div style="font-size: 40px; line-height: 1">📦</div>
        <div class="el-upload__text">拖拽 .zip 到这里或<em>点击选择</em></div>
        <template #tip>
          <div class="el-upload__tip">请将 .shp 及同名的 .shx/.dbf/.prj/.cpg 一起打包；一个压缩包只能包含一个 .shp 数据集。</div>
        </template>
      </el-upload>
      <el-progress v-if="uploading" :percentage="uploadPct" :stroke-width="12" style="margin-top: 12px" />
      <div v-if="uploadNote" class="mono muted" style="margin-top: 8px">{{ uploadNote }}</div>
      <template #footer><el-button @click="uploadVisible = false">关闭</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { browseDir, uploadShpZip } from '../api/tasks'
import type { BrowseData, BrowseEntry, GdbLayerInfo } from '../types'

const props = defineProps<{ path: string }>()
const emit = defineEmits<{ (e: 'update:path', value: string): void }>()
const localPath = ref(props.path)
watch(() => props.path, (v) => { localPath.value = v || '' })
function commit(value: string) { localPath.value = value; emit('update:path', value) }

const browseVisible = ref(false)
const browse = ref<BrowseData | null>(null)
async function openBrowse() {
  browseVisible.value = true
  try { browse.value = await browseDir(localPath.value || undefined) }
  catch (e) { ElMessage.error((e as Error).message); browse.value = null }
}
async function enterDir(row: BrowseEntry) {
  try { browse.value = await browseDir(`${browse.value?.path}/${row.name}`) }
  catch (e) { ElMessage.error((e as Error).message) }
}
function goParent() {
  if (!browse.value?.parent) return
  browseDir(browse.value.parent).then((d) => (browse.value = d)).catch((e) => ElMessage.error((e as Error).message))
}
function usePath(row: BrowseEntry) {
  commit(`${browse.value?.path}/${row.name}`)
  browseVisible.value = false
  ElMessage.success('已选用 SHP 路径')
}

const uploadVisible = ref(false)
const uploading = ref(false)
const uploadPct = ref(0)
const uploadNote = ref('')
function openUpload() { uploadVisible.value = true; uploadNote.value = '' }
async function onFileChange(file: { raw?: File }) {
  if (!file.raw) return
  uploading.value = true; uploadPct.value = 0; uploadNote.value = ''
  try {
    const data = await uploadShpZip(file.raw, (pct) => (uploadPct.value = pct))
    if (!data.shp_path) throw new Error('服务端未返回 SHP 路径')
    commit(data.shp_path)
    const layers = (data.layers as (string | GdbLayerInfo)[]).map((x) => typeof x === 'string' ? x : x.source)
    uploadNote.value = `✅ ${data.cached ? '已复用历史上传' : '已解压'}：${data.shp_path}；图层 ${layers.length} 个：${layers.slice(0, 6).join('、')}${layers.length > 6 ? '…' : ''}`
    ElMessage.success(data.cached ? '已复用历史上的相同上传' : '上传成功，SHP 路径已填入')
  } catch (e) { ElMessage.error((e as Error).message); uploadNote.value = '' }
  finally { uploading.value = false }
}
</script>

<style scoped>
.picker-row { display: flex; gap: 8px; width: 100%; }
.crumbs { margin-bottom: 10px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
