// Behavior lock for composables/useCapabilityTracePanel.js (D-UI-27)
//
// Pins the capability-trace aggregation glue previously inline in App.vue:
//   - buildCapabilityMoreActionHandler: command→handler dispatch table
//   - makeClearPhaseEvents: resets phaseEvents + badge flags + autoScroll
import { describe, it, expect, vi } from 'vitest'
import { ref } from 'vue'

import {
  buildCapabilityMoreActionHandler,
  makeClearPhaseEvents,
} from '../src/composables/useCapabilityTracePanel.js'

describe('buildCapabilityMoreActionHandler', () => {
  it('dispatches known command to its handler', () => {
    const copySummary = vi.fn()
    const generateFixture = vi.fn()
    const handler = buildCapabilityMoreActionHandler({ copySummary, generateFixture })
    handler('copySummary')
    expect(copySummary).toHaveBeenCalledOnce()
    expect(generateFixture).not.toHaveBeenCalled()
  })

  it('unknown command is a no-op (no throw)', () => {
    const handler = buildCapabilityMoreActionHandler({ copySummary: vi.fn() })
    expect(() => handler('nonexistent')).not.toThrow()
  })

  it('dispatches all 9 known commands', () => {
    const keys = [
      'copySummary', 'generateFixture', 'replayFixture',
      'refreshFixtures', 'refreshBatchHistory', 'batchReplay',
      'replayEfficiency', 'refreshEfficiencyReplays', 'importReplay',
    ]
    const handlers = Object.fromEntries(keys.map(k => [k, vi.fn()]))
    const dispatch = buildCapabilityMoreActionHandler(handlers)
    for (const k of keys) {
      dispatch(k)
      expect(handlers[k]).toHaveBeenCalledOnce()
    }
  })
})

describe('makeClearPhaseEvents', () => {
  it('resets phaseEvents to empty and flags to defaults', () => {
    const phaseEvents = ref([{ type: 'phase' }, { type: 'cap' }])
    const hasNewPhase = ref(true)
    const hasNewCapability = ref(true)
    const timelineAutoScroll = ref(false)

    const clear = makeClearPhaseEvents({ phaseEvents, hasNewPhase, hasNewCapability, timelineAutoScroll })
    clear()

    expect(phaseEvents.value).toEqual([])
    expect(hasNewPhase.value).toBe(false)
    expect(hasNewCapability.value).toBe(false)
    expect(timelineAutoScroll.value).toBe(true)
  })

  it('is idempotent (calling twice is safe)', () => {
    const phaseEvents = ref([])
    const hasNewPhase = ref(false)
    const hasNewCapability = ref(false)
    const timelineAutoScroll = ref(true)

    const clear = makeClearPhaseEvents({ phaseEvents, hasNewPhase, hasNewCapability, timelineAutoScroll })
    clear()
    clear()

    expect(phaseEvents.value).toEqual([])
    expect(timelineAutoScroll.value).toBe(true)
  })
})
