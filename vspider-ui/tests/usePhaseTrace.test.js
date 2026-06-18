// Behavior lock for composables/usePhaseTrace.js (方向D · 从 TimelinePanel.vue 抽离的共享逻辑)
//
// Pins the phase-trace filter/stats/render contract:
//   - filteredPhaseEvents honors phase + severity exclude sets
//   - toggle/reset filter actions + phaseFilterActive
//   - phaseSummary / phaseFilterOptions / phaseStatsSorted / severityFilterOptions
//   - phaseTimelineGroups grouping (step asc, no-step last)
//   - completionEvidence picks latest completion_guard
//   - pure helpers: phaseChipStyle (notice_severity escalation), chip label/detail,
//     phaseSparklinePath, formatPhaseStatMs
import { describe, it, expect } from 'vitest'
import { ref } from 'vue'

import {
  usePhaseTrace,
  phaseChipStyle,
  phaseChipLabel,
  phaseChipDetail,
  phaseSparklinePath,
  formatPhaseStatMs,
} from '../src/composables/usePhaseTrace.js'

const sample = () => [
  { phase: 'vlm_call', severity: 'info', step: 1, duration_ms: 100 },
  { phase: 'vlm_call', severity: 'warn', step: 1, duration_ms: 300 },
  { phase: 'action', severity: 'error', step: 2, duration_ms: 50 },
  { phase: 'finalize', severity: 'info' }, // no step, no duration
]

describe('usePhaseTrace filtering', () => {
  it('returns all events when no filter is active', () => {
    const pt = usePhaseTrace(ref(sample()))
    expect(pt.filteredPhaseEvents.value).toHaveLength(4)
    expect(pt.phaseFilterActive.value).toBe(false)
  })

  it('excludes by phase and by severity, and resets', () => {
    const pt = usePhaseTrace(ref(sample()))
    pt.togglePhaseFilter('vlm_call')
    expect(pt.filteredPhaseEvents.value.map((e) => e.phase)).toEqual(['action', 'finalize'])
    expect(pt.phaseFilterActive.value).toBe(true)
    pt.toggleSeverityFilter('error')
    expect(pt.filteredPhaseEvents.value.map((e) => e.phase)).toEqual(['finalize'])
    pt.resetPhaseFilters()
    expect(pt.filteredPhaseEvents.value).toHaveLength(4)
    expect(pt.phaseFilterActive.value).toBe(false)
  })

  it('togglePhaseFilter is idempotent on second click (re-includes)', () => {
    const pt = usePhaseTrace(ref(sample()))
    pt.togglePhaseFilter('action')
    expect(pt.filteredPhaseEvents.value.some((e) => e.phase === 'action')).toBe(false)
    pt.togglePhaseFilter('action')
    expect(pt.filteredPhaseEvents.value.some((e) => e.phase === 'action')).toBe(true)
  })

  it('reacts to the source ref changing', () => {
    const src = ref([])
    const pt = usePhaseTrace(src)
    expect(pt.phaseSummary.value.total).toBe(0)
    src.value = sample()
    expect(pt.phaseSummary.value.total).toBe(4)
  })
})

describe('usePhaseTrace summary & stats', () => {
  it('phaseSummary counts warn/error/byPhase over the FILTERED set', () => {
    const pt = usePhaseTrace(ref(sample()))
    expect(pt.phaseSummary.value).toMatchObject({ total: 4, warn: 1, error: 1 })
    expect(pt.phaseSummary.value.byPhase.vlm_call).toBe(2)
    pt.togglePhaseFilter('vlm_call')
    expect(pt.phaseSummary.value.total).toBe(2)
    expect(pt.phaseSummary.value.warn).toBe(0)
  })

  it('phaseFilterOptions aggregates count/mean/max + sevCounts, sorted count desc', () => {
    const pt = usePhaseTrace(ref(sample()))
    const opts = pt.phaseFilterOptions.value
    expect(opts[0].phase).toBe('vlm_call') // count 2, first
    const vlm = opts.find((o) => o.phase === 'vlm_call')
    expect(vlm.count).toBe(2)
    expect(vlm.mean).toBe(200) // (100 + 300) / 2
    expect(vlm.max).toBe(300)
    expect(vlm.sevCounts).toEqual({ info: 1, warn: 1, error: 0 })
    const fin = opts.find((o) => o.phase === 'finalize')
    expect(fin.mean).toBeNull() // no durations
  })

  it('phaseStatsSorted sinks null-stat phases to the bottom when sorting by mean', () => {
    const pt = usePhaseTrace(ref(sample()))
    pt.phaseStatsSortBy.value = 'mean'
    const sorted = pt.phaseStatsSorted.value
    expect(sorted[0].phase).toBe('vlm_call') // mean 200 highest
    expect(sorted[sorted.length - 1].phase).toBe('finalize') // null mean -> bottom
  })

  it('severityFilterOptions only lists severities that occur', () => {
    const pt = usePhaseTrace(ref(sample()))
    const sevs = pt.severityFilterOptions.value.map((s) => s.severity)
    expect(sevs).toEqual(['info', 'warn', 'error'])
    expect(pt.severityFilterOptions.value.find((s) => s.severity === 'info').count).toBe(2)
  })
})

describe('usePhaseTrace grouping & completion', () => {
  it('phaseTimelineGroups groups by step ascending with no-step bucket last', () => {
    const pt = usePhaseTrace(ref(sample()))
    const groups = pt.phaseTimelineGroups.value
    expect(groups.map((g) => g.step)).toEqual([1, 2, undefined])
    expect(groups[0].events).toHaveLength(2)
  })

  it('completionEvidence returns the latest completion_guard payload', () => {
    const events = ref([
      { phase: 'completion_guard', guard: 'g1', evaluation: { status: 'incomplete' } },
      { phase: 'action' },
      { phase: 'completion_guard', guard: 'g2', message: 'done', evaluation: { status: 'complete', evidence: ['x'] } },
    ])
    const pt = usePhaseTrace(events)
    expect(pt.completionEvidence.value.guard).toBe('g2')
    expect(pt.completionEvidence.value.status).toBe('complete')
    expect(pt.completionEvidence.value.evidence).toEqual(['x'])
  })

  it('completionEvidence is null when no guard event exists', () => {
    const pt = usePhaseTrace(ref(sample()))
    expect(pt.completionEvidence.value).toBeNull()
  })
})

describe('usePhaseTrace pure helpers', () => {
  it('phaseChipStyle follows severity and escalates via notice_severity', () => {
    expect(phaseChipStyle({ severity: 'error' }).color).toBe('#991b1b')
    expect(phaseChipStyle({ severity: 'warn' }).color).toBe('#92400e')
    expect(phaseChipStyle({ severity: 'info' }).color).toBe('#1e40af')
    // notice_severity higher than severity wins
    expect(phaseChipStyle({ severity: 'info', notice_severity: 'warn' }).color).toBe('#92400e')
    // notice_severity lower than severity does NOT downgrade
    expect(phaseChipStyle({ severity: 'error', notice_severity: 'info' }).color).toBe('#991b1b')
  })

  it('phaseChipLabel appends formatted duration', () => {
    expect(phaseChipLabel({ phase: 'vlm_call', duration_ms: 1500 })).toBe('vlm_call · 1.5s')
    expect(phaseChipLabel({ phase: 'action', duration_ms: 250 })).toBe('action · 250ms')
    expect(phaseChipLabel({ phase: 'finalize' })).toBe('finalize')
  })

  it('phaseChipDetail joins message + key extras', () => {
    expect(phaseChipDetail({ message: 'hi', action_name: 'click', element_count: 3 }))
      .toBe('hi · action=click · 3 els')
    expect(phaseChipDetail({})).toBe('')
  })

  it('formatPhaseStatMs formats ms / s / em-dash', () => {
    expect(formatPhaseStatMs(null)).toBe('—')
    expect(formatPhaseStatMs(12.34)).toBe('12.3ms')
    expect(formatPhaseStatMs(1234)).toBe('1.23s')
  })

  it('phaseSparklinePath returns empty for no samples and a path otherwise', () => {
    expect(phaseSparklinePath([])).toBe('')
    expect(phaseSparklinePath([10, 20, 5]).startsWith('M')).toBe(true)
  })
})
