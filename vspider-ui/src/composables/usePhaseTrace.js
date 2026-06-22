// 方向D · 相位轨迹的过滤 / 统计 / 渲染派生层（从 TimelinePanel.vue 抽离的共享逻辑）。
//
// 接收一个「phase 事件数组」的 ref（或 computed），返回时间线面板所需的
// 全部纯派生数据与格式化助手：过滤集、分组、汇总、每-phase 统计直方图、
// severity 统计、以及 chip 的颜色 / 标签 / 详情。
// 不涉及任何 DOM / 文件 IO / 对话框 / 滚动——那些组件相关逻辑仍留在
// TimelinePanel.vue。行为与原 TimelinePanel 内联实现逐字一致。
import { ref, computed } from 'vue'

const _SEV_RANK = { info: 0, warn: 1, error: 2 }

function effectiveSeverity (evt) {
  const a = String(evt.severity || 'info')
  const b = String(evt.notice_severity || a)
  return (_SEV_RANK[b] ?? 0) > (_SEV_RANK[a] ?? 0) ? b : a
}

export function phaseChipStyle (evt) {
  const sev = effectiveSeverity(evt)
  if (sev === 'error') return { background: '#fee2e2', color: '#991b1b', border: '#fca5a5' }
  if (sev === 'warn') return { background: '#fef3c7', color: '#92400e', border: '#fcd34d' }
  return { background: '#dbeafe', color: '#1e40af', border: '#93c5fd' }
}

export function phaseChipLabel (evt) {
  const p = String(evt.phase || 'unknown')
  const d = evt.duration_ms
  if (Number.isFinite(d)) {
    const sec = d >= 1000 ? `${(d / 1000).toFixed(1)}s` : `${Math.round(d)}ms`
    return `${p} · ${sec}`
  }
  return p
}

export function phaseChipDetail (evt) {
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

export function phaseSparklinePath (durations) {
  if (!durations || durations.length === 0) return ''
  const w = 100
  const h = 24
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

export function formatPhaseStatMs (v) {
  if (v == null || !Number.isFinite(v)) return '—'
  if (v < 1000) return `${v.toFixed(1)}ms`
  return `${(v / 1000).toFixed(2)}s`
}

export function usePhaseTrace (phaseEventsRef) {
  const events = () => phaseEventsRef.value || []

  const phaseFilterExclude = ref(new Set())
  const severityFilterExclude = ref(new Set())
  const phaseStatsExpanded = ref(false)
  const phaseStatsSortBy = ref('count')

  const filteredPhaseEvents = computed(() => {
    const exP = phaseFilterExclude.value
    const exS = severityFilterExclude.value
    if (exP.size === 0 && exS.size === 0) return events()
    return events().filter((e) => {
      const p = String(e.phase || 'unknown')
      const s = String(e.severity || 'info')
      return !exP.has(p) && !exS.has(s)
    })
  })

  const completionEvidence = computed(() => {
    const list = events()
    for (let i = list.length - 1; i >= 0; i -= 1) {
      const evt = list[i]
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
    for (const e of events()) {
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

  const severityFilterOptions = computed(() => {
    const counts = { info: 0, warn: 0, error: 0 }
    for (const e of events()) {
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

  return {
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
  }
}
