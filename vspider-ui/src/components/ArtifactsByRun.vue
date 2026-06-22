<script setup>
import { onMounted, ref, watch } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { API_BASE, apiFetch } from '../api/client.js'

const props = defineProps({ refreshToken: { type: Number, default: 0 } })

const groups = ref([])
const loading = ref(false)
const activeNames = ref([])

const fetchGroups = async () => {
  if (loading.value) return
  loading.value = true
  try {
    const resp = await apiFetch('/api/run_artifacts?limit=50')
    const result = await resp.json()
    if (!resp.ok || result.status !== 'success') throw new Error(result.detail || 'load failed')
    groups.value = Array.isArray(result.runs) ? result.runs : []
    if (groups.value.length) activeNames.value = [groups.value[0].run_id]
  } catch (err) {
    console.warn('[run_artifacts] fetch failed:', err)
  } finally {
    loading.value = false
  }
}

const formatTime = (ts) => {
  if (!Number.isFinite(ts)) return '-'
  const d = new Date(ts * 1000)
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}
const statusTagType = (s) => {
  const v = String(s || '').toLowerCase()
  if (v === 'succeeded') return 'success'
  if (v === 'failed' || v === 'error') return 'danger'
  if (v === 'stopped' || v === 'paused') return 'warning'
  if (v === 'running' || v === 'queued') return 'primary'
  return 'info'
}

onMounted(fetchGroups)
watch(() => props.refreshToken, fetchGroups)
</script>

<template>
  <div class="artifacts-by-run">
    <div class="abr-toolbar">
      <el-button size="small" plain :icon="Refresh" :loading="loading" @click="fetchGroups">刷新</el-button>
      <span class="abr-count">{{ groups.length }} 个任务</span>
    </div>
    <el-collapse v-if="groups.length" v-model="activeNames" class="abr-collapse">
      <el-collapse-item v-for="g in groups" :key="g.run_id" :name="g.run_id">
        <template #title>
          <span class="abr-group-title" :title="`${g.goal || ''}\n${g.run_id}`">
            <el-tag size="small" :type="statusTagType(g.status)">{{ g.status || 'unknown' }}</el-tag>
            <span class="abr-label">{{ g.label || g.run_id }}</span>
            <span class="abr-meta">· {{ formatTime(g.created_at) }} · {{ g.artifacts.length }} 个产物</span>
          </span>
        </template>
        <el-table :data="g.artifacts" class="artifact-table" header-cell-class-name="dark-table-header" empty-text="无产物">
          <el-table-column prop="filename" label="文件" show-overflow-tooltip />
          <el-table-column prop="kind" label="类型" width="140" />
          <el-table-column prop="size_kb" label="KB" width="80" class-name="col-mono">
            <template #default="scope">{{ scope.row.size_kb ?? '-' }}</template>
          </el-table-column>
          <el-table-column label="" width="72">
            <template #default="scope">
              <a :href="`${API_BASE}${scope.row.download_url}`" download class="download-link">下载</a>
            </template>
          </el-table-column>
        </el-table>
      </el-collapse-item>
    </el-collapse>
    <p v-else class="abr-empty">暂无带产物的运行</p>
  </div>
</template>

<style scoped>
.artifacts-by-run { display: flex; flex-direction: column; height: 100%; min-height: 0; gap: 8px; }
.abr-toolbar { display: flex; align-items: center; gap: 8px; }
.abr-count { color: var(--vsp-text-faint); font-size: 12px; }
.abr-collapse { flex: 1; min-height: 0; overflow-y: auto; }
.abr-group-title { display: inline-flex; align-items: center; gap: 8px; min-width: 0; }
.abr-label { color: var(--vsp-text-strong); font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 360px; }
.abr-meta { color: var(--vsp-text-muted); font-size: 12px; }
.abr-empty { padding: 28px 14px; text-align: center; color: var(--vsp-text-muted); }
.download-link { color: var(--vsp-cyan-600); font-weight: 700; text-decoration: none; }
.download-link:hover { color: var(--vsp-cyan-700); }
</style>
