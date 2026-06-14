<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

const props = defineProps({
  phaseEvents: { type: Array, required: true },
  replayMode: { type: Boolean, default: false },
  replaySourceName: { type: String, default: '' },
})

const emit = defineEmits(['clear', 'import-replay', 'exit-replay', 'open-phase-dialog'])

const SCROLL_BOTTOM_EPS = 24

const phaseFilterExclude = ref(new Set())
const severityFilterExclude = ref(new Set())
const phaseStatsExpanded = ref(false)
const phaseStatsSortBy = ref('count')
const timelineFiltersExpanded = ref(false)
const timelineAutoScroll = ref(true)
const timelineRef = ref(null)
const replayInputRef = ref(null)

const phaseDialogVisible = ref(false)
const selectedPhaseEvent = ref(null)
const _CHIP_DBLCLICK_WINDOW_MS = 220
let _chipClickTimer = null

const filteredPhaseEvents = computed(() => {
  const exP = phaseFilterExclude.value
  const exS = severityFilterExclude.value
  if (exP.size === 0 && exS.size === 0) return props.phaseEvents
  return props.phaseEvents.filter((e) => {
    const p = String(e.phase || 'unknown')
    const s = String(e.severity || 'info')
    return !exP.has(p) && !exS.has(s)
  })
})

const completionEvidence = computed(() => {
  for (let i = props.phaseEvents.length - 1; i >= 0; i -= 1) {
    const evt = props.phaseEvents[i]
    if (evt && String(evt.phase || '') === 'completion_guard') {
      const evaluation = evt.evaluation && typeof evt.evaluation === 'object'
        ? evt.evaluation : {}
      const state = evt.state && typeof evt.state === 'object' ? evt.state : {}
      return {
        guard: String(evt.guard || 'completion'),
        message: String(evt.message || ''),
        status: String(evaluation.status || ''),
        confidence: evaluation.confidence,
        evidence: Array.isArray(evaluation.evidence) ? evaluation.evidence : [],
        reasons: Array.isArray(evaluation.reasons) ? evaluation.reasons : [],
        checks: Array.isArray(evaluation.checks) ? evaluation.checks : [],
        streak: state.streak ?? evaluation.subgoal_exit?.streak,
        step: evt.step,
      }
    }
  }
  return null
})

const phaseTimelineGroups = computed(() => {
  const noStepKey = '__no_step__'
  const buckets = new Map()
  for (const e of filteredPhaseEvents.value) {
    const key = Number.isFinite(e.step) ? `s${e.step}` : noStepKey
    if (!buckets.has(key)) buckets.set(key, { step: e.step, events: [] })
    buckets.get(key).events.push(e)
  }
  const groups = Array.from(buckets.values())
  groups.sort((a, b) => {
    if (Number.isFinite(a.step) && !Number.isFinite(b.step)) return -1
    if (!Number.isFinite(a.step) && Number.isFinite(b.step)) return 1
    if (Number.isFinite(a.step) && Number.isFinite(b.step)) return a.step - b.step
    return 0
  })
  return groups
})

const phaseSummary = computed(() => {
  const out = { total: filteredPhaseEvents.value.length, warn: 0, error: 0, byPhase: {} }
  for (const e of filteredPhaseEvents.value) {
    const sev = String(e.severity || 'info')
    if (sev === 'warn') out.warn += 1
    else if (sev === 'error') out.error += 1
    const p = String(e.phase || 'unknown')
    out.byPhase[p] = (out.byPhase[p] || 0) + 1
  }
  return out
})

const phaseFilterOptions = computed(() => {
  const acc = new Map()
  for (const e of props.phaseEvents) {
    const p = String(e.phase || 'unknown')
    let bucket = acc.get(p)
    if (!bucket) {
      bucket = { count: 0, durs: [], sev: { info: 0, warn: 0, error: 0 } }
      acc.set(p, bucket)
    }
    bucket.count += 1
    if (Number.isFinite(e.duration_ms)) bucket.durs.push(Number(e.duration_ms))
    const sev = String(e.severity || 'info')
    if (bucket.sev[sev] != null) bucket.sev[sev] += 1
  }
  const pickPercentile = (sorted, p) => {
    if (!sorted.length) return null
    if (sorted.length === 1) return sorted[0]
    const idx = (sorted.length - 1) * p
    const lo = Math.floor(idx)
    const hi = Math.ceil(idx)
    if (lo === hi) return sorted[lo]
    return sorted[lo] + (sorted[hi] - sorted[lo]) * (idx - lo)
  }
  const out = []
  for (const [phase, b] of acc.entries()) {
    const sorted = b.durs.slice().sort((a, c) => a - c)
    const sum = sorted.reduce((s, x) => s + x, 0)
    out.push({
      phase,
      count: b.count,
      mean: sorted.length ? sum / sorted.length : null,
      p50: pickPercentile(sorted, 0.5),
      p95: pickPercentile(sorted, 0.95),
      max: sorted.length ? sorted[sorted.length - 1] : null,
      sevCounts: { ...b.sev },
      durations: sorted.length > 60
        ? sorted.filter((_, i) => i % Math.ceil(sorted.length / 60) === 0)
        : sorted,
    })
  }
  return out.sort((a, b) => b.count - a.count)
})

const phaseStatsSorted = computed(() => {
  const key = phaseStatsSortBy.value
  if (key === 'count') return phaseFilterOptions.value
  return phaseFilterOptions.value.slice().sort((a, b) => {
    const av = a[key]; const bv = b[key]
    if (av == null && bv == null) return 0
    if (av == null) return 1
    if (bv == null) return -1
    return bv - av
  })
})

const phaseSparklinePath = (durations) => {
  if (!durations || durations.length === 0) return ''
  const w = 100; const h = 24
  const maxV = durations.reduce((m, x) => (x > m ? x : m), 0) || 1
  const n = durations.length
  const barW = Math.max(1, w / n - 0.6)
  const stride = w / n
  let d = ''
  for (let i = 0; i < n; i += 1) {
    const x = i * stride
    const barH = (durations[i] / maxV) * (h - 2) + 1
    const y = h - barH
    d += `M${x.toFixed(2)},${h} L${x.toFixed(2)},${y.toFixed(2)} `
        + `L${(x + barW).toFixed(2)},${y.toFixed(2)} `
        + `L${(x + barW).toFixed(2)},${h} Z `
  }
  return d.trim()
}

const formatPhaseStatMs = (v) => {
  if (v == null || !Number.isFinite(v)) return '—'
  if (v < 1000) return `${v.toFixed(1)}ms`
  return `${(v / 1000).toFixed(2)}s`
}

const severityFilterOptions = computed(() => {
  const counts = { info: 0, warn: 0, error: 0 }
  for (const e of props.phaseEvents) {
    const s = String(e.severity || 'info')
    if (counts[s] != null) counts[s] += 1
  }
  return ['info', 'warn', 'error']
    .filter((s) => counts[s] > 0)
    .map((s) => ({ severity: s, count: counts[s] }))
})

const phaseFilterActive = computed(
  () => phaseFilterExclude.value.size > 0 || severityFilterExclude.value.size > 0,
)

const togglePhaseFilter = (phase) => {
  const next = new Set(phaseFilterExclude.value)
  if (next.has(phase)) next.delete(phase)
  else next.add(phase)
  phaseFilterExclude.value = next
}
const toggleSeverityFilter = (severity) => {
  const next = new Set(severityFilterExclude.value)
  if (next.has(severity)) next.delete(severity)
  else next.add(severity)
  severityFilterExclude.value = next
}
const resetPhaseFilters = () => {
  phaseFilterExclude.value = new Set()
  severityFilterExclude.value = new Set()
}

const _SEV_RANK = { info: 0, warn: 1, error: 2 }
function _effectiveSeverity(evt) {
  const a = String(evt.severity || 'info')
  const b = String(evt.notice_severity || a)
  return (_SEV_RANK[b] ?? 0) > (_SEV_RANK[a] ?? 0) ? b : a
}
function phaseChipStyle(evt) {
  const sev = _effectiveSeverity(evt)
  if (sev === 'error') return { background: '#fee2e2', color: '#991b1b', border: '#fca5a5' }
  if (sev === 'warn')  return { background: '#fef3c7', color: '#92400e', border: '#fcd34d' }
  return { background: '#dbeafe', color: '#1e40af', border: '#93c5fd' }
}
function phaseChipLabel(evt) {
  const p = String(evt.phase || 'unknown')
  const d = evt.duration_ms
  if (Number.isFinite(d)) {
    const sec = d >= 1000 ? `${(d / 1000).toFixed(1)}s` : `${Math.round(d)}ms`
    return `${p} · ${sec}`
  }
  return p
}
function phaseChipDetail(evt) {
  const parts = []
  if (evt.message) parts.push(String(evt.message))
  if (evt.action_name) parts.push(`action=${evt.action_name}`)
  if (evt.element_count != null) parts.push(`${evt.element_count} els`)
  if (evt.frame_count != null) parts.push(`${evt.frame_count} frames`)
  if (evt.guard) parts.push(`guard=${evt.guard}`)
  if (evt.evaluation?.status) parts.push(`status=${evt.evaluation.status}`)
  if (evt.answer_domain) parts.push(`domain=${evt.answer_domain}`)
  return parts.join(' · ')
}

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

defineExpose({ scrollToBottom, scrollToTop, filteredPhaseEvents, openPhaseDialog, goToPrevPhaseEvent, goToNextPhaseEvent, phaseDialogVisible })
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
