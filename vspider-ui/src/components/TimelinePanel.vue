<script setup>
import { computed, nextTick, ref, toRef, watch } from 'vue'
import { usePhaseTrace } from '../composables/usePhaseTrace.js'

const props = defineProps({
  phaseEvents: { type: Array, required: true },
  replayMode: { type: Boolean, default: false },
  replaySourceName: { type: String, default: '' },
})

const emit = defineEmits(['clear', 'import-replay', 'exit-replay', 'open-phase-dialog'])

const SCROLL_BOTTOM_EPS = 24

const phaseEventsRef = toRef(props, 'phaseEvents')
const {
  phaseFilterExclude,
  severityFilterExclude,
  phaseStatsExpanded,
  phaseStatsSortBy,
  filteredPhaseEvents,
  completionEvidence,
  phaseTimelineGroups,
  phaseSummary,
  phaseFilterOptions,
  phaseStatsSorted,
  severityFilterOptions,
  phaseFilterActive,
  togglePhaseFilter,
  toggleSeverityFilter,
  resetPhaseFilters,
  phaseSparklinePath,
  formatPhaseStatMs,
  phaseChipStyle,
  phaseChipLabel,
  phaseChipDetail,
} = usePhaseTrace(phaseEventsRef)
const timelineFiltersExpanded = ref(false)
const timelineAutoScroll = ref(true)
const timelineRef = ref(null)
const replayInputRef = ref(null)

const phaseDialogVisible = ref(false)
const selectedPhaseEvent = ref(null)
const _CHIP_DBLCLICK_WINDOW_MS = 220
let _chipClickTimer = null

const exportPhaseEventsAsJsonl = () => {
  const src = filteredPhaseEvents.value
  if (!src.length) { ElMessage.warning('当前没有可导出的 phase 事件'); return }
  const lines = []
  for (const e of src) {
    const clone = {}
    for (const k of Object.keys(e)) { if (k === '_ts') continue; clone[k] = e[k] }
    try { lines.push(JSON.stringify(clone)) } catch (err) {
      lines.push(JSON.stringify({ phase: 'unknown', error: String(err) }))
    }
  }
  const ts = new Date()
  const stamp = `${ts.getFullYear()}${String(ts.getMonth() + 1).padStart(2, '0')}`
    + `${String(ts.getDate()).padStart(2, '0')}_${String(ts.getHours()).padStart(2, '0')}`
    + `${String(ts.getMinutes()).padStart(2, '0')}${String(ts.getSeconds()).padStart(2, '0')}`
  const blob = new Blob([lines.join('\n') + '\n'], { type: 'application/x-ndjson;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url; a.download = `phase_events_${stamp}.jsonl`
  document.body.appendChild(a); a.click(); document.body.removeChild(a)
  setTimeout(() => URL.revokeObjectURL(url), 1000)
  ElMessage.success(`已导出 ${lines.length} 条事件`)
}

const handleTimelineMoreAction = (command) => {
  const handlers = {
    exportJsonl: exportPhaseEventsAsJsonl,
    importReplay: () => {
      const inp = replayInputRef.value
      if (!inp) return
      try { inp.value = ''; inp.click() } catch (_) {}
    },
    clear: () => emit('clear'),
  }
  handlers[command]?.()
}

const handleReplayFileChange = async (event) => {
  const target = event && event.target
  const file = target && target.files && target.files[0]
  if (!file) return
  if (file.size > 16 * 1024 * 1024) {
    ElMessage.error(`文件过大 (${(file.size / 1024 / 1024).toFixed(1)} MB)，请选择 ≤ 16 MB 的 phase JSONL 文件`)
    return
  }
  let text = ''
  try { text = await file.text() } catch (err) { ElMessage.error(`读取失败：${String(err)}`); return }
  emit('import-replay', text, file.name || 'imported.jsonl')
}

// --- Phase dialog ---
const buildPhaseEventJsonString = (evt) => {
  if (!evt || typeof evt !== 'object') return ''
  const clone = {}
  for (const k of Object.keys(evt)) { if (k === '_ts') continue; clone[k] = evt[k] }
  try { return JSON.stringify(clone, null, 2) } catch (err) { return String(err) }
}
const _writeToClipboard = async (text) => {
  if (!text) return false
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text)
    } else {
      const ta = document.createElement('textarea')
      ta.value = text; document.body.appendChild(ta); ta.select()
      document.execCommand('copy'); document.body.removeChild(ta)
    }
    return true
  } catch (err) { ElMessage.error(`复制失败: ${String(err)}`); return false }
}
const openPhaseDialog = (evt) => {
  selectedPhaseEvent.value = evt || null
  phaseDialogVisible.value = !!evt
}
const onChipClick = (evt) => {
  if (_chipClickTimer) { clearTimeout(_chipClickTimer); _chipClickTimer = null }
  _chipClickTimer = setTimeout(() => { _chipClickTimer = null; openPhaseDialog(evt) }, _CHIP_DBLCLICK_WINDOW_MS)
}
const onChipDblClick = async (evt) => {
  if (_chipClickTimer) { clearTimeout(_chipClickTimer); _chipClickTimer = null }
  const text = buildPhaseEventJsonString(evt)
  if (!text) return
  const ok = await _writeToClipboard(text)
  if (ok) ElMessage.success(`已复制 ${String(evt && evt.phase || 'phase')} 事件 JSON (双击)`)
}
const selectedPhaseJson = computed(() => buildPhaseEventJsonString(selectedPhaseEvent.value))
const selectedPhaseTime = computed(() => {
  const e = selectedPhaseEvent.value
  const ts = e && Number.isFinite(e.ts) ? e.ts : null
  if (ts == null) return '—'
  try {
    const d = new Date(ts * 1000)
    const pad = (n, w = 2) => String(n).padStart(w, '0')
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
      + `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`
  } catch (_) { return '—' }
})
const copyPhaseJson = async () => {
  const text = selectedPhaseJson.value
  if (!text) return
  const ok = await _writeToClipboard(text)
  if (ok) ElMessage.success('已复制 JSON')
}
const _findCurrentEventIndex = () => {
  const cur = selectedPhaseEvent.value
  if (!cur) return -1
  const list = filteredPhaseEvents.value
  let idx = list.indexOf(cur)
  if (idx !== -1) return idx
  const curTs = cur.ts; const curPhase = cur.phase
  for (let i = 0; i < list.length; i += 1) {
    if (list[i] && list[i].ts === curTs && list[i].phase === curPhase) return i
  }
  return -1
}
const goToPrevPhaseEvent = () => {
  const list = filteredPhaseEvents.value
  if (!list.length) return
  const idx = _findCurrentEventIndex()
  selectedPhaseEvent.value = list[idx <= 0 ? list.length - 1 : idx - 1]
}
const goToNextPhaseEvent = () => {
  const list = filteredPhaseEvents.value
  if (!list.length) return
  const idx = _findCurrentEventIndex()
  selectedPhaseEvent.value = list[idx === -1 || idx >= list.length - 1 ? 0 : idx + 1]
}

// --- Scroll helpers ---
function _timelineWrap() {
  const sb = timelineRef.value
  if (!sb) return null
  return sb.wrapRef || sb.wrap$ || sb.wrap_ || null
}
function _isAtBottom(wrap) {
  if (!wrap) return true
  return wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight <= SCROLL_BOTTOM_EPS
}
const scrollToBottom = async () => {
  await nextTick()
  const sb = timelineRef.value
  if (!sb) return
  if (typeof sb.setScrollTop === 'function') sb.setScrollTop(Number.MAX_SAFE_INTEGER)
  else { const wrap = _timelineWrap(); if (wrap) wrap.scrollTop = wrap.scrollHeight }
  timelineAutoScroll.value = true
}
const scrollToTop = async () => {
  await nextTick()
  const sb = timelineRef.value
  if (!sb) return
  if (typeof sb.setScrollTop === 'function') sb.setScrollTop(0)
  else { const wrap = _timelineWrap(); if (wrap) wrap.scrollTop = 0 }
  timelineAutoScroll.value = false
}
const onTimelineScroll = () => {
  const wrap = _timelineWrap()
  if (wrap) timelineAutoScroll.value = _isAtBottom(wrap)
}

watch(() => props.phaseEvents.length, async () => {
  if (timelineAutoScroll.value) await scrollToBottom()
})

defineExpose({ scrollToBottom, scrollToTop, filteredPhaseEvents, openPhaseDialog, goToPrevPhaseEvent, goToNextPhaseEvent, phaseDialogVisible, exportPhaseEventsAsJsonl })
</script>

<template>
  <div class="timeline-panel">
    <div class="timeline-toolbar">
      <span class="timeline-summary">
        <strong>{{ phaseSummary.total }}</strong>
        <span v-if="phaseFilterActive" class="timeline-filter-frac">
          / {{ phaseEvents.length }}
        </span>
        events
        <span v-if="phaseSummary.warn" class="timeline-warn-pill">{{ phaseSummary.warn }} warn</span>
        <span v-if="phaseSummary.error" class="timeline-err-pill">{{ phaseSummary.error }} err</span>
      </span>
      <div class="timeline-toolbar-spacer" />
      <el-button v-if="phaseFilterActive" size="small" plain class="timeline-clear-btn" @click="resetPhaseFilters">重置筛选</el-button>
      <el-button v-if="phaseEvents.length" size="small" plain class="timeline-clear-btn" :class="{ 'is-active': phaseStatsExpanded }" @click="phaseStatsExpanded = !phaseStatsExpanded">统计 {{ phaseStatsExpanded ? '▴' : '▾' }}</el-button>
      <el-button v-if="phaseEvents.length" size="small" plain class="timeline-clear-btn" :class="{ 'is-active': timelineFiltersExpanded }" @click="timelineFiltersExpanded = !timelineFiltersExpanded">筛选 {{ timelineFiltersExpanded ? '▴' : '▾' }}</el-button>
      <el-dropdown trigger="click" @command="handleTimelineMoreAction">
        <el-button size="small" plain class="timeline-clear-btn">更多 ⋯</el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="exportJsonl" :disabled="!phaseEvents.length">导出 JSONL</el-dropdown-item>
            <el-dropdown-item command="importReplay">导入回放</el-dropdown-item>
            <el-dropdown-item command="clear" divided :disabled="!phaseEvents.length">清空</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <input ref="replayInputRef" type="file" accept=".jsonl,.json,application/x-ndjson,text/plain" class="replay-file-input" @change="handleReplayFileChange" />
    </div>

    <div v-if="phaseEvents.length && (timelineFiltersExpanded || phaseFilterActive)" class="timeline-filter-row">
      <div v-if="severityFilterOptions.length > 1" class="timeline-filter-group">
        <span class="timeline-filter-label">severity</span>
        <button v-for="opt in severityFilterOptions" :key="'sev-' + opt.severity" type="button" class="timeline-filter-pill" :class="['sev-' + opt.severity, { 'is-off': severityFilterExclude.has(opt.severity) }]" @click="toggleSeverityFilter(opt.severity)">{{ opt.severity }}<span class="timeline-filter-pill-count">{{ opt.count }}</span></button>
      </div>
      <div class="timeline-filter-group">
        <span class="timeline-filter-label">phase</span>
        <button v-for="opt in phaseFilterOptions" :key="'phase-' + opt.phase" type="button" class="timeline-filter-pill" :class="{ 'is-off': phaseFilterExclude.has(opt.phase) }" @click="togglePhaseFilter(opt.phase)">{{ opt.phase }}<span class="timeline-filter-pill-count">{{ opt.count }}</span></button>
      </div>
    </div>

    <div v-if="phaseStatsExpanded && phaseEvents.length" class="timeline-stats-panel">
      <header class="timeline-stats-header">
        <span class="timeline-stats-title">Phase 耗时分布</span>
        <span class="timeline-stats-sort-label">排序</span>
        <button v-for="key in ['count', 'mean', 'p95', 'max']" :key="`sort-${key}`" type="button" class="timeline-stats-sort-btn" :class="{ 'is-active': phaseStatsSortBy === key }" @click="phaseStatsSortBy = key">{{ key }}</button>
      </header>
      <div class="timeline-stats-grid">
        <div class="timeline-stats-grid-head">
          <span>phase</span><span>分布</span><span class="num">count</span><span class="num">mean</span><span class="num">p50</span><span class="num">p95</span><span class="num">max</span><span>severity</span>
        </div>
        <button v-for="row in phaseStatsSorted" :key="`stat-${row.phase}`" type="button" class="timeline-stats-row" :class="{ 'is-off': phaseFilterExclude.has(row.phase) }" @click="togglePhaseFilter(row.phase)">
          <span class="stat-phase">{{ row.phase }}</span>
          <svg class="stat-spark" viewBox="0 0 100 24" preserveAspectRatio="none" aria-hidden="true">
            <path v-if="row.durations && row.durations.length" :d="phaseSparklinePath(row.durations)" fill="currentColor" />
            <text v-else x="50" y="16" text-anchor="middle" class="stat-spark-empty">no duration</text>
          </svg>
          <span class="num">{{ row.count }}</span>
          <span class="num">{{ formatPhaseStatMs(row.mean) }}</span>
          <span class="num">{{ formatPhaseStatMs(row.p50) }}</span>
          <span class="num">{{ formatPhaseStatMs(row.p95) }}</span>
          <span class="num">{{ formatPhaseStatMs(row.max) }}</span>
          <span class="stat-sev">
            <span v-if="row.sevCounts.error" class="stat-sev-pill sev-error">{{ row.sevCounts.error }}E</span>
            <span v-if="row.sevCounts.warn" class="stat-sev-pill sev-warn">{{ row.sevCounts.warn }}W</span>
            <span v-if="row.sevCounts.info" class="stat-sev-pill sev-info">{{ row.sevCounts.info }}I</span>
          </span>
        </button>
      </div>
      <p class="timeline-stats-footnote">分布柱状图按时间顺序展示该 phase 每次执行的耗时；高度归一到本 phase 的最大值。点击行可切换该 phase 在 Timeline 的显示。</p>
    </div>

    <div v-if="replayMode" class="timeline-replay-banner">
      <span class="replay-icon" aria-hidden="true">▶</span>
      <span class="replay-text">回放模式<span v-if="replaySourceName" class="replay-source"> · {{ replaySourceName }}</span></span>
      <el-button size="small" plain class="replay-exit-btn" @click="emit('exit-replay')">退出回放</el-button>
    </div>

    <div v-if="completionEvidence" class="completion-evidence-panel">
      <div class="completion-evidence-panel__head">
        <strong>完成判定</strong>
        <el-tag size="small" :type="completionEvidence.status === 'complete' ? 'success' : 'warning'">{{ completionEvidence.guard }}</el-tag>
      </div>
      <p v-if="completionEvidence.message" class="completion-evidence-panel__msg">{{ completionEvidence.message }}</p>
      <div v-if="completionEvidence.evidence.length" class="completion-evidence-panel__row">
        <span class="completion-evidence-panel__label">证据</span><span>{{ completionEvidence.evidence.join(' · ') }}</span>
      </div>
      <div v-if="completionEvidence.reasons.length" class="completion-evidence-panel__row">
        <span class="completion-evidence-panel__label">原因</span><span>{{ completionEvidence.reasons.join(' · ') }}</span>
      </div>
      <div v-if="completionEvidence.streak != null" class="completion-evidence-panel__row">
        <span class="completion-evidence-panel__label">无进展 streak</span><span>{{ completionEvidence.streak }}</span>
      </div>
    </div>

    <el-scrollbar ref="timelineRef" class="timeline-scroll" @scroll="onTimelineScroll">
      <p v-if="!phaseEvents.length" class="empty-log">暂无 phase 事件 — 启动任务后会在这里实时显示 VLM / action / SoM / guard / finalize 的时间线</p>
      <p v-else-if="!phaseTimelineGroups.length" class="empty-log">当前筛选下没有匹配事件 · <a href="#" class="tab-jump" @click.prevent="resetPhaseFilters">重置筛选</a></p>
      <div v-for="(group, gi) in phaseTimelineGroups" :key="gi" class="timeline-row">
        <div class="timeline-step-tag">{{ Number.isFinite(group.step) ? 'step ' + group.step : '—' }}</div>
        <div class="timeline-chips">
          <button v-for="(evt, ei) in group.events" :key="ei" type="button" class="timeline-chip" :style="{ background: phaseChipStyle(evt).background, color: phaseChipStyle(evt).color, borderColor: phaseChipStyle(evt).border }" :title="(phaseChipDetail(evt) || '') + ' (单击查看 / 双击复制 JSON)'" @click="onChipClick(evt)" @dblclick="onChipDblClick(evt)">
            <span class="timeline-chip-label">{{ phaseChipLabel(evt) }}</span>
            <span v-if="phaseChipDetail(evt)" class="timeline-chip-detail">{{ phaseChipDetail(evt) }}</span>
          </button>
        </div>
      </div>
    </el-scrollbar>

    <button v-if="!timelineAutoScroll && phaseTimelineGroups.length" type="button" class="timeline-jump-bottom" @click="scrollToBottom">↓ 回到底部</button>
  </div>

  <el-dialog v-model="phaseDialogVisible" title="Phase Event" width="640px" class="phase-dialog" destroy-on-close>
    <div v-if="selectedPhaseEvent" class="phase-dialog-body">
      <div class="phase-dialog-header">
        <span class="phase-dialog-pill" :style="{ background: phaseChipStyle(selectedPhaseEvent).background, color: phaseChipStyle(selectedPhaseEvent).color, borderColor: phaseChipStyle(selectedPhaseEvent).border }">{{ String(selectedPhaseEvent.phase || 'unknown') }}</span>
        <span class="phase-dialog-sev">severity: <strong>{{ String(selectedPhaseEvent.severity || 'info') }}</strong></span>
        <span v-if="selectedPhaseEvent.notice_severity && selectedPhaseEvent.notice_severity !== selectedPhaseEvent.severity" class="phase-dialog-sev">notice: <strong>{{ String(selectedPhaseEvent.notice_severity) }}</strong></span>
        <span v-if="Number.isFinite(selectedPhaseEvent.step)" class="phase-dialog-step">step <strong>{{ selectedPhaseEvent.step }}</strong></span>
        <span v-if="Number.isFinite(selectedPhaseEvent.duration_ms)" class="phase-dialog-dur">duration <strong>{{ selectedPhaseEvent.duration_ms }}ms</strong></span>
      </div>
      <div class="phase-dialog-time">{{ selectedPhaseTime }}</div>
      <p v-if="selectedPhaseEvent.message" class="phase-dialog-message">{{ selectedPhaseEvent.message }}</p>
      <pre class="phase-dialog-json"><code>{{ selectedPhaseJson }}</code></pre>
    </div>
    <template #footer>
      <el-button size="small" plain :disabled="filteredPhaseEvents.length < 2" @click="goToPrevPhaseEvent">← 上一条</el-button>
      <el-button size="small" plain :disabled="filteredPhaseEvents.length < 2" @click="goToNextPhaseEvent">下一条 →</el-button>
      <el-button size="small" plain @click="copyPhaseJson">复制 JSON</el-button>
      <el-button size="small" @click="phaseDialogVisible = false">关闭</el-button>
    </template>
  </el-dialog>
</template>

<style src="../styles/timeline-panel.css" scoped></style>
