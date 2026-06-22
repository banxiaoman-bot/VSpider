// Behavior lock for composables/useTimelineReplay.js (方向D · 从 App.vue 抽离)
//
// Pins the offline-replay contract previously inline in App.vue:
//   - parsePhaseReplayJsonl tolerates blank / malformed / non-dict lines,
//     extracts detail.phase_event, and recognizes a capability_execute_trace doc
//   - handleTimelineImportReplay swaps phaseEvents into replay mode + switches tab
//   - exitReplayMode clears the replay buffer and flags
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { ref } from 'vue'

vi.mock('element-plus', () => ({
  ElMessage: { info: vi.fn(), warning: vi.fn(), success: vi.fn(), error: vi.fn() },
}))

import { useTimelineReplay, parsePhaseReplayJsonl } from '../src/composables/useTimelineReplay.js'
import { ElMessage } from 'element-plus'

beforeEach(() => vi.clearAllMocks())

describe('parsePhaseReplayJsonl', () => {
  it('parses valid jsonl lines', () => {
    const r = parsePhaseReplayJsonl('{"phase":"a"}\n{"phase":"b"}')
    expect(r.events.map((e) => e.phase)).toEqual(['a', 'b'])
    expect(r.total).toBe(2)
    expect(r.bad).toBe(0)
  })

  it('skips blank lines (not counted) and counts malformed lines', () => {
    const r = parsePhaseReplayJsonl('{"phase":"a"}\n\n   \nNOTJSON\n[1,2]')
    expect(r.events.map((e) => e.phase)).toEqual(['a'])
    expect(r.total).toBe(3) // a, NOTJSON, [1,2]
    expect(r.bad).toBe(2)   // NOTJSON + array(non-dict)
  })

  it('unwraps detail.phase_event', () => {
    const r = parsePhaseReplayJsonl('{"detail":{"phase_event":{"phase":"unwrapped"}}}')
    expect(r.events[0].phase).toBe('unwrapped')
  })

  it('recognizes a single capability_execute_trace document', () => {
    const r = parsePhaseReplayJsonl(JSON.stringify({
      type: 'capability_execute_trace',
      result: { capability: 'search', completed: true, status: 'ok' },
    }))
    expect(r.events).toHaveLength(1)
    expect(r.events[0].phase).toBe('capability_execute')
    expect(r.events[0].capability).toBe('search')
    expect(r.events[0].severity).toBe('info')
  })

  it('empty text yields nothing', () => {
    const r = parsePhaseReplayJsonl('   ')
    expect(r.events).toEqual([])
    expect(r.total).toBe(0)
  })
})

function harness () {
  const phaseEvents = ref([{ phase: 'old' }])
  const hasNewCapability = ref(true)
  const setActiveBottomTab = vi.fn()
  const r = useTimelineReplay({ phaseEvents, hasNewCapability, setActiveBottomTab })
  return { phaseEvents, hasNewCapability, setActiveBottomTab, r }
}

describe('useTimelineReplay import', () => {
  it('swaps phaseEvents into replay mode and switches to timeline tab', () => {
    const { phaseEvents, setActiveBottomTab, r } = harness()
    r.handleTimelineImportReplay('{"phase":"a"}\n{"phase":"b"}', 'run.jsonl')
    expect(phaseEvents.value).toHaveLength(2)
    expect(r.replayMode.value).toBe(true)
    expect(r.replaySourceName.value).toBe('run.jsonl')
    expect(setActiveBottomTab).toHaveBeenCalledWith('timeline')
    expect(typeof phaseEvents.value[0]._ts).toBe('number') // _ts decorated
  })

  it('routes to capability tab when triggerReplayImport target is capability', () => {
    const { setActiveBottomTab, r } = harness()
    r.triggerReplayImport('capability')
    r.handleTimelineImportReplay('{"phase":"x"}', 'f.jsonl')
    expect(setActiveBottomTab).toHaveBeenCalledWith('capability')
  })

  it('no recognizable events → warns, leaves state untouched', () => {
    const { phaseEvents, r } = harness()
    r.handleTimelineImportReplay('   ', 'f.jsonl')
    expect(r.replayMode.value).toBe(false)
    expect(phaseEvents.value).toEqual([{ phase: 'old' }])
    expect(ElMessage.warning).toHaveBeenCalled()
  })
})

describe('useTimelineReplay exit', () => {
  it('clears replay buffer + flags', () => {
    const { phaseEvents, hasNewCapability, r } = harness()
    r.handleTimelineImportReplay('{"phase":"a"}', 'f.jsonl')
    r.exitReplayMode()
    expect(r.replayMode.value).toBe(false)
    expect(r.replaySourceName.value).toBe('')
    expect(phaseEvents.value).toEqual([])
    expect(hasNewCapability.value).toBe(false)
    expect(ElMessage.info).toHaveBeenCalled()
  })
})
