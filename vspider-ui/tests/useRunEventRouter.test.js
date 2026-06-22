// Behavior lock for composables/useRunEventRouter.js (方向D · 从 App.vue 抽离)
//
// Pins the WS message dispatch previously inline in App.vue handleSocketMessage:
//   - log → appendLog([LEVEL] content); image/screenshot → pushScreenshotFrame
//   - done → stop running, refresh runtime/runs badge, [DONE], applyDoneAnswer;
//     success=false also refetches failed runs
//   - phase → appendLog([PHASE]), push+ring-trim phaseEvents, capability/phase badges;
//     replayMode drops the event
//   - status → new_artifact/human_intervention/hitl_form/human_resumed sub-branches
//   - malformed JSON → [WARN]
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { ref } from 'vue'
import { useRunEventRouter, PHASE_LIMIT } from '../src/composables/useRunEventRouter.js'

function mk (over = {}) {
  const fetchFailedRuns = vi.fn()
  const scrollToBottom = vi.fn(async () => {})
  const deps = {
    appendLog: vi.fn(async () => {}),
    pushScreenshotFrame: vi.fn(),
    isRunning: ref(true),
    isHumanInterventionRequired: ref(false),
    humanInterventionReason: ref(''),
    hitlScreenshot: ref(''),
    hitlFormFields: ref([]),
    hitlFormReason: ref(''),
    hitlFormScreenshot: ref(''),
    hitlFormLoading: ref(false),
    hitlFormVisible: ref(false),
    fetchBrowserRuntimeStatus: vi.fn(),
    runHistoryRefreshToken: ref(0),
    activeBottomTab: ref('terminal'),
    hasNewRuns: ref(false),
    hasNewCapability: ref(false),
    hasNewPhase: ref(false),
    timelineAutoScroll: ref(true),
    failedRunsPaneRef: ref({ fetchFailedRuns }),
    applyDoneAnswer: vi.fn(),
    replayMode: ref(false),
    phaseEvents: ref([]),
    fetchArtifacts: vi.fn(async () => {}),
    hasNewArtifacts: ref(false),
    currentImageBase64: ref(''),
    timelinePanelRef: ref({ scrollToBottom }),
    ...over,
  }
  const { handleSocketMessage } = useRunEventRouter(deps)
  return { handleSocketMessage, deps, fetchFailedRuns, scrollToBottom }
}

const ev = (obj) => ({ data: JSON.stringify(obj) })

beforeEach(() => vi.clearAllMocks())

describe('useRunEventRouter log/image', () => {
  it('log → appendLog with uppercased level', async () => {
    const { handleSocketMessage, deps } = mk()
    await handleSocketMessage(ev({ type: 'log', level: 'warn', content: 'hi' }))
    expect(deps.appendLog).toHaveBeenCalledWith('[WARN] hi')
  })

  it('image → pushScreenshotFrame', async () => {
    const { handleSocketMessage, deps } = mk()
    await handleSocketMessage(ev({ type: 'image', data: 'b64' }))
    expect(deps.pushScreenshotFrame).toHaveBeenCalledWith('b64')
  })
})

describe('useRunEventRouter done', () => {
  it('stops running, bumps refresh token, pulses runs badge, applies answer', async () => {
    const { handleSocketMessage, deps } = mk()
    await handleSocketMessage(ev({ type: 'done', success: true, message: 'ok' }))
    expect(deps.isRunning.value).toBe(false)
    expect(deps.fetchBrowserRuntimeStatus).toHaveBeenCalled()
    expect(deps.runHistoryRefreshToken.value).toBe(1)
    expect(deps.hasNewRuns.value).toBe(true)
    expect(deps.appendLog).toHaveBeenCalledWith('[DONE] ok')
    expect(deps.applyDoneAnswer).toHaveBeenCalledTimes(1)
  })

  it('success=false also refetches failed runs', async () => {
    const { handleSocketMessage, fetchFailedRuns } = mk()
    await handleSocketMessage(ev({ type: 'done', success: false }))
    expect(fetchFailedRuns).toHaveBeenCalledTimes(1)
  })

  it('does not pulse runs badge when already on runs tab', async () => {
    const { handleSocketMessage, deps } = mk({ activeBottomTab: ref('runs') })
    await handleSocketMessage(ev({ type: 'done', success: true }))
    expect(deps.hasNewRuns.value).toBe(false)
  })
})

describe('useRunEventRouter phase', () => {
  it('appends event + pulses phase/capability badges', async () => {
    const { handleSocketMessage, deps } = mk()
    await handleSocketMessage(ev({ type: 'phase', phase: 'capability_route', severity: 'info' }))
    expect(deps.phaseEvents.value).toHaveLength(1)
    expect(deps.hasNewCapability.value).toBe(true)
    expect(deps.hasNewPhase.value).toBe(true)
  })

  it('replayMode drops the phase event', async () => {
    const { handleSocketMessage, deps } = mk({ replayMode: ref(true) })
    await handleSocketMessage(ev({ type: 'phase', phase: 'x' }))
    expect(deps.phaseEvents.value).toHaveLength(0)
  })

  it('ring-trims phaseEvents to PHASE_LIMIT', async () => {
    const phaseEvents = ref(Array.from({ length: PHASE_LIMIT }, (_, i) => ({ phase: 'x', _ts: i })))
    const { handleSocketMessage } = mk({ phaseEvents })
    await handleSocketMessage(ev({ type: 'phase', phase: 'y' }))
    expect(phaseEvents.value).toHaveLength(PHASE_LIMIT)
    expect(phaseEvents.value[phaseEvents.value.length - 1].phase).toBe('y')
  })
})

describe('useRunEventRouter status', () => {
  it('new_artifact fetches + pulses artifacts badge', async () => {
    const { handleSocketMessage, deps } = mk()
    await handleSocketMessage(ev({ type: 'status', status: 'new_artifact', filename: 'a.xlsx' }))
    expect(deps.fetchArtifacts).toHaveBeenCalledTimes(1)
    expect(deps.hasNewArtifacts.value).toBe(true)
  })

  it('human_intervention sets reason + screenshot fallback', async () => {
    const { handleSocketMessage, deps } = mk({ currentImageBase64: ref('cur') })
    await handleSocketMessage(ev({ type: 'status', status: 'human_intervention', reason: 'blocked' }))
    expect(deps.isHumanInterventionRequired.value).toBe(true)
    expect(deps.humanInterventionReason.value).toBe('blocked')
    expect(deps.hitlScreenshot.value).toBe('cur')
  })

  it('hitl_form opens the form with fields', async () => {
    const { handleSocketMessage, deps } = mk()
    await handleSocketMessage(ev({ type: 'status', status: 'hitl_form', fields: [{ name: 'u' }] }))
    expect(deps.hitlFormVisible.value).toBe(true)
    expect(deps.hitlFormFields.value).toEqual([{ name: 'u' }])
  })

  it('human_resumed clears intervention + form', async () => {
    const { handleSocketMessage, deps } = mk({
      isHumanInterventionRequired: ref(true),
      hitlFormVisible: ref(true),
    })
    await handleSocketMessage(ev({ type: 'status', status: 'human_resumed' }))
    expect(deps.isHumanInterventionRequired.value).toBe(false)
    expect(deps.hitlFormVisible.value).toBe(false)
  })
})

describe('useRunEventRouter malformed', () => {
  it('warns on unparseable data', async () => {
    const { handleSocketMessage, deps } = mk()
    await handleSocketMessage({ data: 'not json{' })
    expect(String(deps.appendLog.mock.calls.at(-1)[0])).toContain('[WARN]')
  })
})
