<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { Document, Refresh, View } from '@element-plus/icons-vue'
import { API_BASE, apiFetch } from '../api/client.js'

const props = defineProps({
  refreshToken: { type: Number, default: 0 },
})

const emit = defineEmits(['loaded', 'open-detail'])

const runs = ref([])
const loading = ref(false)
const detailLoading = ref(false)
const detailError = ref('')
const dialogVisible = ref(false)
const selectedRun = ref(null)
const selectedDetail = ref(null)

const fetchRuns = async () => {
  if (loading.value) return
  loading.value = true
  try {
    const response = await apiFetch('/api/runs?limit=50')
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.message || result.detail || 'load runs failed')
    }
    runs.value = Array.isArray(result.runs) ? result.runs : []
    emit('loaded', runs.value)
  } catch (err) {
    // Keep the panel quiet; the empty state and refresh button are enough.
    // eslint-disable-next-line no-console
    console.warn('[runs] fetch failed:', err)
  } finally {
    loading.value = false
  }
}

const openRunDetail = async (row) => {
  if (!row || !row.run_id) return
  selectedRun.value = row
  selectedDetail.value = null
  detailError.value = ''
  dialogVisible.value = true
  detailLoading.value = true
  emit('open-detail', row)
  try {
    const response = await apiFetch(`/api/runs/${encodeURIComponent(row.run_id)}`)
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.message || result.detail || 'load run detail failed')
    }
    selectedDetail.value = result
  } catch (err) {
    detailError.value = String(err)
  } finally {
    detailLoading.value = false
  }
}

const contracts = computed(() => selectedDetail.value?.contracts || {})
const contractSummary = computed(() => contracts.value.summary || {})
const inputContract = computed(() => contracts.value.input_contract || {})
const outputContract = computed(() => contracts.value.output_contract || {})
const manifest = computed(() => contracts.value.manifest || {})
const manifestItems = computed(() =>
  Array.isArray(manifest.value.items) ? manifest.value.items : [],
)
const visibleManifestItems = computed(() => manifestItems.value.slice(0, 80))
const childRuns = computed(() =>
  manifestItems.value.filter((item) => item?.extra?.entry_type === 'child_run'),
)
const runRecord = computed(() => selectedDetail.value?.run || selectedRun.value || {})
const inputUrls = computed(() =>
  Array.isArray(inputContract.value.urls) ? inputContract.value.urls : [],
)
const attachments = computed(() =>
  Array.isArray(inputContract.value.attachments) ? inputContract.value.attachments : [],
)
const detailJson = computed(() => {
  try {
    return JSON.stringify(selectedDetail.value || selectedRun.value || {}, null, 2)
  } catch (err) {
    return String(err)
  }
})

const formatTime = (ts) => {
  if (!Number.isFinite(ts)) return '-'
  try {
    const d = new Date(ts * 1000)
    const pad = (n) => String(n).padStart(2, '0')
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
      + `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  } catch (err) {
    return '-'
  }
}

const formatDuration = (sec) => {
  if (!Number.isFinite(sec) || sec < 0) return '-'
  if (sec < 60) return `${sec.toFixed(1)}s`
  const m = Math.floor(sec / 60)
  const s = Math.round(sec - m * 60)
  return `${m}m${String(s).padStart(2, '0')}s`
}

const statusTagType = (status) => {
  const s = String(status || '').toLowerCase()
  if (s === 'succeeded') return 'success'
  if (s === 'failed' || s === 'error') return 'danger'
  if (s === 'stopped' || s === 'paused') return 'warning'
  if (s === 'running' || s === 'queued') return 'primary'
  return 'info'
}

const manifestItemLabel = (item) => {
  const kind = String(item?.kind || 'other')
  const path = String(item?.path || '')
  if (!path) return kind
  const parts = path.split(/[\\/]/).filter(Boolean)
  return parts[parts.length - 1] || kind
}

const artifactHref = (item) => {
  const path = String(item?.path || '')
  const runId = String(manifest.value.run_id || runRecord.value.run_id || '')
  const prefix = `runs/${runId}/artifacts/`
  if (!path.startsWith(prefix)) return ''
  const rel = path.slice(prefix.length)
  if (!rel) return ''
  return `${API_BASE}/download/runs/${encodeURIComponent(runId)}/artifacts/${encodeURI(rel)}`
}

const FILE_BUNDLE_THRESHOLD = 5
const downloadableKinds = new Set([
  'media_image', 'media_video', 'media_audio', 'media_pdf',
  'media_archive', 'file_generic', 'screenshot',
])
const downloadableFileCount = computed(() =>
  manifestItems.value.filter(
    (item) =>
      downloadableKinds.has(String(item?.kind || '')) &&
      String(item?.path || '') &&
      !item?.inline &&
      !(item?.extra && item.extra.bundle),
  ).length,
)
const bundleHref = computed(() => {
  const runId = String(manifest.value.run_id || runRecord.value.run_id || '')
  if (!runId) return ''
  return `${API_BASE}/download/runs/${encodeURIComponent(runId)}/bundle.zip`
})

const closeDialog = () => {
  dialogVisible.value = false
}

onMounted(fetchRuns)
watch(() => props.refreshToken, () => fetchRuns())
</script>

<template>
  <section class="run-registry-panel">
    <div class="run-registry-toolbar">
      <el-button
        size="small"
        plain
        :icon="Refresh"
        :loading="loading"
        @click="fetchRuns"
      >
        刷新
      </el-button>
      <span class="run-registry-count">{{ runs.length }} 条</span>
      <span class="toolbar-spacer" />
      <slot name="toolbar-extra" />
    </div>

    <el-table
      :data="runs"
      height="190"
      class="artifact-table run-registry-table"
      header-cell-class-name="dark-table-header"
      empty-text="暂无运行记录"
      @row-click="openRunDetail"
    >
      <el-table-column label="状态" width="112">
        <template #default="scope">
          <el-tag size="small" :type="statusTagType(scope.row.status)">
            {{ scope.row.status || 'unknown' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="run_id" label="Run ID" width="168" show-overflow-tooltip />
      <el-table-column label="创建时间" width="138">
        <template #default="scope">
          {{ formatTime(scope.row.created_at) }}
        </template>
      </el-table-column>
      <el-table-column prop="target_url" label="目标 URL" show-overflow-tooltip />
      <el-table-column label="模式" width="86">
        <template #default="scope">
          {{ scope.row.mode || '-' }}
        </template>
      </el-table-column>
      <el-table-column label="耗时" width="92">
        <template #default="scope">
          {{ formatDuration(scope.row.duration_s) }}
        </template>
      </el-table-column>
      <el-table-column label="操作" width="92">
        <template #default="scope">
          <el-button
            size="small"
            plain
            :icon="View"
            @click.stop="openRunDetail(scope.row)"
          >
            详情
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog
      v-model="dialogVisible"
      :title="runRecord.run_id ? `Run ${runRecord.run_id}` : '运行详情'"
      width="860px"
      class="run-detail-dialog"
      destroy-on-close
      @close="closeDialog"
    >
      <div v-if="detailLoading" class="run-detail-status">正在加载运行详情...</div>
      <div v-else-if="detailError" class="run-detail-status run-detail-error">
        {{ detailError }}
      </div>
      <div v-else class="run-detail-body">
        <section class="run-detail-section">
          <div class="run-detail-summary">
            <div class="run-summary-item">
              <span>状态</span>
              <strong>{{ runRecord.status || 'unknown' }}</strong>
            </div>
            <div class="run-summary-item">
              <span>输入契约</span>
              <strong>{{ contractSummary.has_input_contract ? 'ready' : 'missing' }}</strong>
            </div>
            <div class="run-summary-item">
              <span>输出契约</span>
              <strong>{{ contractSummary.has_output_contract ? 'ready' : 'missing' }}</strong>
            </div>
            <div class="run-summary-item">
              <span>清单条目</span>
              <strong>{{ contractSummary.manifest_items ?? 0 }}</strong>
            </div>
            <div class="run-summary-item">
              <span>子任务</span>
              <strong>{{ contractSummary.child_runs ?? childRuns.length }}</strong>
            </div>
            <div class="run-summary-item">
              <span>产物数</span>
              <strong>{{ contractSummary.artifacts ?? 0 }}</strong>
            </div>
          </div>
        </section>

        <section class="run-detail-section">
          <header>
            <Document class="run-section-icon" />
            <h4>输入契约</h4>
          </header>
          <dl class="run-detail-meta">
            <div class="meta-row meta-row-full">
              <dt>目标</dt>
              <dd>{{ inputContract.goal || runRecord.prompt || '-' }}</dd>
            </div>
            <div class="meta-row meta-row-full">
              <dt>URLs</dt>
              <dd>
                <span v-if="!inputUrls.length">-</span>
                <span
                  v-for="(item, idx) in inputUrls"
                  v-else
                  :key="`${item.url || idx}`"
                  class="run-inline-chip"
                >
                  {{ item.role || 'start' }}: {{ item.url || '-' }}
                </span>
              </dd>
            </div>
            <div class="meta-row meta-row-full">
              <dt>附件</dt>
              <dd>
                <span v-if="!attachments.length">-</span>
                <span
                  v-for="(item, idx) in attachments"
                  v-else
                  :key="`${item.path || item.filename || idx}`"
                  class="run-inline-chip"
                >
                  {{ item.intent || 'unknown' }}: {{ item.filename || item.path || '-' }}
                </span>
              </dd>
            </div>
          </dl>
        </section>

        <section class="run-detail-section">
          <header>
            <Document class="run-section-icon" />
            <h4>输出契约</h4>
          </header>
          <div class="run-output-grid">
            <span>mode: <strong>{{ outputContract.mode || '-' }}</strong></span>
            <span>kind: <strong>{{ outputContract.output_kind || '-' }}</strong></span>
            <span>container: <strong>{{ outputContract.container || '-' }}</strong></span>
            <span>explicit: <strong>{{ outputContract.user_explicit ? 'yes' : 'no' }}</strong></span>
          </div>
        </section>

        <section class="run-detail-section">
          <header>
            <Document class="run-section-icon" />
            <h4>产物清单 Manifest</h4>
            <a
              v-if="downloadableFileCount > FILE_BUNDLE_THRESHOLD && bundleHref"
              :href="bundleHref"
              class="download-link bundle-download-link"
              target="_blank"
              rel="noreferrer"
            >
              打包下载（{{ downloadableFileCount }} 个文件）
            </a>
          </header>
          <el-table
            :data="visibleManifestItems"
            max-height="220"
            class="artifact-table run-manifest-table"
            header-cell-class-name="dark-table-header"
            empty-text="清单为空"
          >
            <el-table-column label="类型" width="128">
              <template #default="scope">
                {{ scope.row.kind || 'other' }}
              </template>
            </el-table-column>
            <el-table-column label="文件" show-overflow-tooltip>
              <template #default="scope">
                {{ manifestItemLabel(scope.row) }}
              </template>
            </el-table-column>
            <el-table-column label="步骤" width="118" show-overflow-tooltip>
              <template #default="scope">
                {{ scope.row.step_id || '-' }}
              </template>
            </el-table-column>
            <el-table-column label="产出方" width="156" show-overflow-tooltip>
              <template #default="scope">
                {{ scope.row.produced_by || '-' }}
              </template>
            </el-table-column>
            <el-table-column label="操作" width="104">
              <template #default="scope">
                <a
                  v-if="artifactHref(scope.row)"
                  :href="artifactHref(scope.row)"
                  class="download-link"
                  target="_blank"
                  rel="noreferrer"
                >
                  下载
                </a>
                <span v-else>-</span>
              </template>
            </el-table-column>
          </el-table>
        </section>

        <section class="run-detail-section">
          <details class="run-detail-raw">
            <summary>
              <Document class="run-section-icon" />
              <span>原始 JSON（点击展开）</span>
            </summary>
            <pre class="run-detail-json"><code>{{ detailJson }}</code></pre>
          </details>
        </section>
      </div>

      <template #footer>
        <el-button
          size="small"
          plain
          :icon="Refresh"
          :disabled="!runRecord.run_id"
          @click="openRunDetail(runRecord)"
        >
          刷新
        </el-button>
        <el-button size="small" @click="closeDialog">关闭</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.run-registry-panel {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.run-registry-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
}

.toolbar-spacer {
  flex: 1;
}

.run-registry-count {
  color: #9ca3af;
  font-size: 12px;
}

.run-registry-table :deep(.el-table__row) {
  cursor: pointer;
}

.run-registry-table :deep(.el-table__row:hover) > td {
  background: rgba(96, 165, 250, 0.10) !important;
}

.run-detail-status {
  color: #d1d5db;
  padding: 16px 0;
}

.run-detail-error {
  color: #fca5a5;
}

.run-detail-body {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.run-detail-section {
  border: 1px solid rgba(148, 163, 184, 0.18);
  border-radius: 8px;
  padding: 12px;
  background: rgba(15, 23, 42, 0.50);
}

.run-detail-section header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.run-detail-section h4 {
  margin: 0;
  font-size: 13px;
  color: #e5e7eb;
}

.run-section-icon {
  width: 16px;
  height: 16px;
  color: #93c5fd;
}

.run-detail-summary,
.run-output-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 8px;
}

.run-summary-item,
.run-output-grid span {
  min-width: 0;
  border: 1px solid rgba(148, 163, 184, 0.16);
  border-radius: 6px;
  padding: 8px 10px;
  background: rgba(2, 6, 23, 0.34);
}

.run-summary-item span,
.run-output-grid span {
  color: #94a3b8;
  font-size: 11px;
}

.run-summary-item strong,
.run-output-grid strong {
  display: block;
  margin-top: 4px;
  color: #f8fafc;
  font-size: 13px;
  overflow-wrap: anywhere;
}

.run-detail-meta {
  display: grid;
  grid-template-columns: 1fr;
  gap: 8px;
  margin: 0;
}

.run-detail-meta .meta-row {
  display: grid;
  grid-template-columns: 96px minmax(0, 1fr);
  gap: 10px;
  align-items: start;
}

.run-detail-meta dt {
  color: #94a3b8;
  font-size: 12px;
}

.run-detail-meta dd {
  margin: 0;
  color: #e5e7eb;
  font-size: 12px;
  overflow-wrap: anywhere;
}

.run-inline-chip {
  display: inline-flex;
  max-width: 100%;
  margin: 0 6px 6px 0;
  border: 1px solid rgba(59, 130, 246, 0.28);
  border-radius: 999px;
  padding: 3px 8px;
  color: #bfdbfe;
  background: rgba(30, 64, 175, 0.18);
  overflow-wrap: anywhere;
}

.run-detail-raw summary {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  color: #e5e7eb;
  font-size: 13px;
  font-weight: 600;
  list-style: none;
}

.run-detail-raw summary::-webkit-details-marker {
  display: none;
}

.run-detail-raw[open] summary {
  margin-bottom: 10px;
}

.run-detail-json {
  max-height: 220px;
  overflow: auto;
  margin: 0;
  padding: 10px;
  border-radius: 6px;
  background: rgba(2, 6, 23, 0.74);
  color: #d1d5db;
  font-size: 12px;
  line-height: 1.5;
}
</style>
