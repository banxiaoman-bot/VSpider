<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { API_BASE, apiFetch } from '../api/client.js'

const failedRunsList = ref([])
const failedRunsLoading = ref(false)
const selectedFailedRun = ref(null)
const failedRunDialogVisible = ref(false)
const failedRunPhaseEvents = ref([])
const failedRunPhaseStatus = ref('idle')
const failedRunPhaseTotal = ref(0)
const failedRunPhaseTruncated = ref(false)

const selectedFailedRunJson = computed(() => {
  if (!selectedFailedRun.value) return ''
  try { return JSON.stringify(selectedFailedRun.value, null, 2) } catch (e) { return String(e) }
})

const fetchFailedRuns = async () => {
  if (failedRunsLoading.value) return
  failedRunsLoading.value = true
  try {
    const response = await apiFetch('/api/failed_runs?limit=50')
    const result = await response.json()
    if (!response.ok || result.status !== 'success') throw new Error(result.message || '加载失败 run 列表失败')
    failedRunsList.value = Array.isArray(result.items) ? result.items : []
  } catch (err) {
    console.warn('[failed_runs] fetch failed:', err)
  } finally {
    failedRunsLoading.value = false
  }
}

const formatFailedRunTime = (ts) => {
  if (!Number.isFinite(ts)) return '—'
  try {
    const d = new Date(ts * 1000)
    const pad = (n) => String(n).padStart(2, '0')
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  } catch (_) { return '—' }
}

const formatFailedRunDuration = (sec) => {
  if (!Number.isFinite(sec) || sec < 0) return '—'
  if (sec < 60) return `${sec.toFixed(1)}s`
  const m = Math.floor(sec / 60)
  const s = Math.round(sec - m * 60)
  return `${m}m${String(s).padStart(2, '0')}s`
}

const openFailedRunLog = (rec) => {
  if (!rec || !rec.run_id) return
  if (rec.paths_exist && rec.paths_exist.html_log === false) {
    ElMessage.warning('HTML 日志文件已被清理，无法打开')
    return
  }
  window.open(`${API_BASE}/api/failed_runs/${encodeURIComponent(rec.run_id)}/log`, '_blank', 'noopener')
}

const fetchFailedRunPhaseEvents = async (rec) => {
  if (!rec || !rec.run_id) { failedRunPhaseStatus.value = 'idle'; failedRunPhaseEvents.value = []; return }
  if (rec.paths_exist && rec.paths_exist.phase_jsonl === false) {
    failedRunPhaseStatus.value = 'missing'; failedRunPhaseEvents.value = []; failedRunPhaseTotal.value = 0; failedRunPhaseTruncated.value = false; return
  }
  failedRunPhaseStatus.value = 'loading'; failedRunPhaseEvents.value = []; failedRunPhaseTotal.value = 0; failedRunPhaseTruncated.value = false
  try {
    const url = `${API_BASE}/api/failed_runs/${encodeURIComponent(rec.run_id)}/phase_events?limit=200`
    const response = await fetch(url)
    if (response.status === 404) { failedRunPhaseStatus.value = 'missing'; return }
    const result = await response.json()
    if (!response.ok || result.status !== 'success') throw new Error(result.detail || result.message || '加载 phase 事件失败')
    failedRunPhaseEvents.value = Array.isArray(result.events) ? result.events : []
    failedRunPhaseTotal.value = Number.isFinite(result.total) ? result.total : 0
    failedRunPhaseTruncated.value = Boolean(result.truncated)
    failedRunPhaseStatus.value = 'ok'
  } catch (err) {
    console.warn('[failed_runs] phase events fetch failed:', err)
    failedRunPhaseStatus.value = 'error'
  }
}

const openFailedRunDetail = (rec) => {
  if (!rec) return
  selectedFailedRun.value = rec
  failedRunDialogVisible.value = true
  fetchFailedRunPhaseEvents(rec)
}

const closeFailedRunDetail = () => { failedRunDialogVisible.value = false }

const _findCurrentFailedRunIndex = () => {
  const cur = selectedFailedRun.value
  if (!cur) return -1
  let idx = failedRunsList.value.indexOf(cur)
  if (idx !== -1) return idx
  for (let i = 0; i < failedRunsList.value.length; i += 1) {
    if (failedRunsList.value[i]?.run_id === cur.run_id) return i
  }
  return -1
}
const goToPrevFailedRun = () => {
  const list = failedRunsList.value; if (!list.length) return
  const idx = _findCurrentFailedRunIndex()
  openFailedRunDetail(list[idx <= 0 ? list.length - 1 : idx - 1])
}
const goToNextFailedRun = () => {
  const list = failedRunsList.value; if (!list.length) return
  const idx = _findCurrentFailedRunIndex()
  openFailedRunDetail(list[idx === -1 || idx >= list.length - 1 ? 0 : idx + 1])
}

const _writeToClipboard = async (text) => {
  if (!text) return false
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text)
    else { const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta) }
    return true
  } catch (err) { ElMessage.error(`复制失败: ${String(err)}`); return false }
}

const copyFailedRunJson = async () => {
  const ok = await _writeToClipboard(selectedFailedRunJson.value)
  if (ok) ElMessage.success('已复制 JSON')
  else ElMessage.error('复制失败：浏览器拒绝了剪贴板写入')
}

const formatPhasePreviewSeverity = (sev) => {
  const s = String(sev || 'info').toLowerCase()
  if (s === 'warn' || s === 'warning') return 'warn'
  if (s === 'error' || s === 'err') return 'error'
  return 'info'
}
const formatPhasePreviewTs = (ts) => {
  if (!Number.isFinite(ts)) return ''
  try { const d = new Date(ts * 1000); const pad = (n) => String(n).padStart(2, '0'); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}` } catch (_) { return '' }
}

defineExpose({ fetchFailedRuns, failedRunsList, goToPrevFailedRun, goToNextFailedRun, failedRunDialogVisible })
</script>

<template>
  <div>
    <div class="artifact-toolbar">
      <el-button size="small" plain :icon="Refresh" :loading="failedRunsLoading" @click="fetchFailedRuns">刷新</el-button>
      <span class="failed-runs-count">{{ failedRunsList.length }} 条记录</span>
    </div>
    <el-table :data="failedRunsList" height="190" class="artifact-table failed-runs-table failed-runs-clickable" header-cell-class-name="dark-table-header" empty-text="目前还没有失败记录 🎉" @row-click="openFailedRunDetail">
      <el-table-column label="时间" width="138">
        <template #default="scope">{{ formatFailedRunTime(scope.row.ts) }}</template>
      </el-table-column>
      <el-table-column prop="run_id" label="Run ID" width="158" show-overflow-tooltip />
      <el-table-column label="目标" show-overflow-tooltip>
        <template #default="scope"><span :title="scope.row.goal || ''">{{ scope.row.goal || '—' }}</span></template>
      </el-table-column>
      <el-table-column label="原因" show-overflow-tooltip>
        <template #default="scope"><span class="failed-reason" :title="scope.row.reason || ''">{{ scope.row.reason || '—' }}</span></template>
      </el-table-column>
      <el-table-column label="步数" width="62" align="center">
        <template #default="scope">{{ Number.isFinite(scope.row.step_count) ? scope.row.step_count : '—' }}</template>
      </el-table-column>
      <el-table-column label="耗时" width="86" align="center">
        <template #default="scope">{{ formatFailedRunDuration(scope.row.duration_s) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="118">
        <template #default="scope">
          <el-button size="small" type="primary" plain :disabled="scope.row.paths_exist && scope.row.paths_exist.html_log === false" @click.stop="openFailedRunLog(scope.row)">HTML 日志</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>

  <el-dialog v-model="failedRunDialogVisible" :title="selectedFailedRun ? `失败 run · ${selectedFailedRun.run_id}` : '失败 run'" width="760px" class="failed-run-dialog" destroy-on-close @close="closeFailedRunDetail">
    <div v-if="selectedFailedRun" class="failed-run-dialog-body">
      <dl class="failed-run-meta">
        <div class="meta-row"><dt>Run ID</dt><dd>{{ selectedFailedRun.run_id || '—' }}</dd></div>
        <div class="meta-row"><dt>Time</dt><dd>{{ formatFailedRunTime(selectedFailedRun.ts) }}</dd></div>
        <div class="meta-row"><dt>Duration</dt><dd>{{ formatFailedRunDuration(selectedFailedRun.duration_s) }}</dd></div>
        <div class="meta-row"><dt>Step</dt><dd>{{ Number.isFinite(selectedFailedRun.step_count) ? selectedFailedRun.step_count : '—' }}</dd></div>
        <div v-if="selectedFailedRun.exception_type" class="meta-row"><dt>Exception</dt><dd class="meta-mono">{{ selectedFailedRun.exception_type }}</dd></div>
        <div v-if="selectedFailedRun.goal" class="meta-row meta-row-full"><dt>Goal</dt><dd>{{ selectedFailedRun.goal }}</dd></div>
        <div class="meta-row meta-row-full"><dt>Reason</dt><dd class="meta-mono failed-reason-block">{{ selectedFailedRun.reason || '—' }}</dd></div>
      </dl>
      <section class="failed-run-preview">
        <header class="preview-header">
          <h4>Phase 事件预览</h4>
          <span v-if="failedRunPhaseStatus === 'ok'" class="preview-meta">{{ failedRunPhaseEvents.length }} / {{ failedRunPhaseTotal }} 条<span v-if="failedRunPhaseTruncated" class="preview-truncated"> · 仅显示最后 {{ failedRunPhaseEvents.length }} 条</span></span>
          <el-button v-if="failedRunPhaseStatus !== 'loading'" size="small" plain :icon="Refresh" @click="fetchFailedRunPhaseEvents(selectedFailedRun)">刷新</el-button>
        </header>
        <div v-if="failedRunPhaseStatus === 'loading'" class="preview-status">正在加载 phase 事件 …</div>
        <div v-else-if="failedRunPhaseStatus === 'missing'" class="preview-status">该 run 的 phase_jsonl 已被清理，无可显示的事件。</div>
        <div v-else-if="failedRunPhaseStatus === 'error'" class="preview-status preview-status-error">加载失败 — 后端日志可能已损坏或权限不可读。</div>
        <ul v-else-if="failedRunPhaseEvents.length" class="preview-list">
          <li v-for="(evt, idx) in failedRunPhaseEvents" :key="`${evt.ts || ''}_${idx}`" class="preview-item" :class="`preview-sev-${formatPhasePreviewSeverity(evt.severity)}`">
            <span class="preview-time">{{ formatPhasePreviewTs(evt.ts) }}</span>
            <span class="preview-phase">{{ evt.phase || 'unknown' }}</span>
            <span v-if="Number.isFinite(evt.step)" class="preview-step">step {{ evt.step }}</span>
            <span v-if="Number.isFinite(evt.duration_ms)" class="preview-dur">{{ evt.duration_ms }}ms</span>
            <span v-if="evt.message" class="preview-msg">{{ evt.message }}</span>
          </li>
        </ul>
        <div v-else class="preview-status">（这条 run 没有记录 phase 事件）</div>
      </section>
      <section class="failed-run-json-section">
        <h4>完整 JSON</h4>
        <pre class="phase-dialog-json"><code>{{ selectedFailedRunJson }}</code></pre>
      </section>
    </div>
    <template #footer>
      <el-button size="small" plain :disabled="failedRunsList.length < 2" @click="goToPrevFailedRun">← 上一条</el-button>
      <el-button size="small" plain :disabled="failedRunsList.length < 2" @click="goToNextFailedRun">下一条 →</el-button>
      <el-button size="small" plain :disabled="!selectedFailedRun || (selectedFailedRun.paths_exist && selectedFailedRun.paths_exist.html_log === false)" @click="openFailedRunLog(selectedFailedRun)">打开 HTML 日志</el-button>
      <el-button size="small" plain @click="copyFailedRunJson">复制 JSON</el-button>
      <el-button size="small" @click="closeFailedRunDetail">关闭</el-button>
    </template>
  </el-dialog>
</template>

<style src="../styles/failed-runs-pane.css" scoped></style>
