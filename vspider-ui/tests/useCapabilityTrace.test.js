// Behavior lock for composables/useCapabilityTrace.js (方向D · 从 App.vue 抽离)
//
// Pins the capability-trace derivation contract previously inline in App.vue:
//   - latestCapabilityRoute/Execute pick the LAST matching phase event
//   - capabilityTraceEvents keeps only capability_route/capability_execute
//   - capabilityIntent/BackendPlan derive from the latest route
//   - capabilityTraceRows + Summary count by phase; filter/search narrow rows
//   - reactivity follows the source phaseEvents ref
import { describe, it, expect } from 'vitest'
import { ref } from 'vue'
import { useCapabilityTrace } from '../src/composables/useCapabilityTrace.js'

const route = (over = {}) => ({
  phase: 'capability_route',
  intent: { capability: 'search' },
  backend_plan: ['search', 'browse'],
  ...over,
})
const exec = (over = {}) => ({
  phase: 'capability_execute',
  capability: 'search',
  completed: true,
  ...over,
})
const other = () => ({ phase: 'vlm_call', severity: 'info' })

describe('useCapabilityTrace selection', () => {
  it('latestCapabilityRoute/Execute pick the last matching event', () => {
    const ev = ref([route({ intent: { capability: 'a' } }), exec(), route({ intent: { capability: 'b' } })])
    const ct = useCapabilityTrace(ev)
    expect(ct.latestCapabilityRoute.value.intent.capability).toBe('b')
    expect(ct.latestCapabilityExecute.value.capability).toBe('search')
  })

  it('capabilityTraceEvents keeps only route + execute', () => {
    const ev = ref([route(), other(), exec(), other()])
    const ct = useCapabilityTrace(ev)
    expect(ct.capabilityTraceEvents.value).toHaveLength(2)
  })

  it('capabilityIntent / capabilityBackendPlan derive from latest route', () => {
    const ct = useCapabilityTrace(ref([route({ intent: { capability: 'search' } })]))
    expect(ct.capabilityIntent.value).toEqual({ capability: 'search' })
    expect(ct.capabilityBackendPlan.value).toEqual(['search', 'browse'])
  })

  it('returns null route/execute when none present', () => {
    const ct = useCapabilityTrace(ref([other()]))
    expect(ct.latestCapabilityRoute.value).toBeNull()
    expect(ct.latestCapabilityExecute.value).toBeNull()
    expect(ct.capabilityIntent.value).toEqual({})
  })
})

describe('useCapabilityTrace rows / summary / filter', () => {
  it('rows + summary count by phase', () => {
    const ct = useCapabilityTrace(ref([route(), exec(), exec()]))
    expect(ct.capabilityTraceRows.value).toHaveLength(3)
    const s = ct.capabilityTraceSummary.value
    expect(s.all).toBe(3)
    expect(s.route).toBe(1)
    expect(s.execute).toBe(2)
  })

  it('filter narrows rows by phase', () => {
    const ct = useCapabilityTrace(ref([route(), exec(), exec()]))
    ct.capabilityTraceFilter.value = 'route'
    expect(ct.capabilityFilteredTraceRows.value).toHaveLength(1)
    expect(ct.capabilityFilteredTraceRows.value.every((r) => r.phase === 'capability_route')).toBe(true)
    ct.capabilityTraceFilter.value = 'execute'
    expect(ct.capabilityFilteredTraceRows.value).toHaveLength(2)
    ct.capabilityTraceFilter.value = 'all'
    expect(ct.capabilityFilteredTraceRows.value).toHaveLength(3)
  })

  it('reacts to phaseEvents ref changes', () => {
    const ev = ref([])
    const ct = useCapabilityTrace(ev)
    expect(ct.capabilityTraceSummary.value.all).toBe(0)
    ev.value = [route(), exec()]
    expect(ct.capabilityTraceSummary.value.all).toBe(2)
  })
})

describe('useCapabilityTrace smoke (no undefined-ref in any exposed computed)', () => {
  it('evaluating every exposed value does not throw', () => {
    const ct = useCapabilityTrace(ref([route(), exec(), { phase: 'completion_guard' }]))
    for (const [key, val] of Object.entries(ct)) {
      expect(() => {
        if (val && typeof val === 'object' && 'value' in val) return val.value
        return val
      }, `accessing ${key}`).not.toThrow()
    }
  })

  it('evaluating every exposed value is safe on empty events too', () => {
    const ct = useCapabilityTrace(ref([]))
    for (const [key, val] of Object.entries(ct)) {
      expect(() => (val && typeof val === 'object' && 'value' in val ? val.value : val),
        `accessing ${key}`).not.toThrow()
    }
  })
})
