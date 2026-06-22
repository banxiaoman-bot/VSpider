// Behavior lock for composables/useCapabilityFixtureReplay.js (方向D · 从 App.vue 抽离)
//
// Pins the fixture/efficiency replay subsystem contract:
//   - capabilityReplayPaneProps bundles every replay/library/batch section (and
//     evaluating it exercises all derived computeds -> no undefined-ref regressions)
//   - generate/replay/replayEfficiency guard on an invalid bundle/report (no network)
//   - generate happy path posts, refetches artifacts + library, toasts success
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { ref } from 'vue'

vi.mock('../src/api/client.js', () => ({ apiFetch: vi.fn() }))
vi.mock('element-plus', () => ({
  ElMessage: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

import { useCapabilityFixtureReplay } from '../src/composables/useCapabilityFixtureReplay.js'
import { apiFetch } from '../src/api/client.js'
import { ElMessage } from 'element-plus'

function deps (over = {}) {
  return {
    url: ref('http://x'),
    prompt: ref('goal'),
    fetchArtifacts: vi.fn(),
    writeToClipboard: vi.fn(async () => true),
    latestCapabilityExecute: ref(null),
    capabilityExecutionFailureBundle: ref({}),
    capabilityExecutionEfficiencyCorrelationReport: ref({}),
    ...over,
  }
}

const ACTIONS = [
  'generateCapabilityFailureFixture',
  'replayCapabilityFailureFixture',
  'replayCapabilityEfficiencyFeedback',
  'fetchCapabilityEfficiencyFeedbackReplays',
  'fetchCapabilityFailureFixtures',
  'fetchCapabilityFailureFixtureBatchHistory',
  'batchReplayCapabilityFailureFixtures',
  'copyCapabilityFailureFixtureBatchReplaySummary',
]

beforeEach(() => vi.clearAllMocks())

describe('useCapabilityFixtureReplay smoke', () => {
  it('exposes capabilityReplayPaneProps + the action functions', () => {
    const fr = useCapabilityFixtureReplay(deps())
    expect(typeof fr.capabilityReplayPaneProps.value).toBe('object')
    for (const a of ACTIONS) expect(typeof fr[a]).toBe('function')
  })

  it('capabilityReplayPaneProps bundles all sections without throwing', () => {
    const fr = useCapabilityFixtureReplay(deps())
    let props
    expect(() => { props = fr.capabilityReplayPaneProps.value }).not.toThrow()
    for (const k of ['efficiencyReplay', 'efficiencyLibrary', 'fixtureReplay',
      'fixtureLibrary', 'batchHistory', 'batchReplay']) {
      expect(props).toHaveProperty(k)
    }
  })
})

describe('useCapabilityFixtureReplay guards (no network on invalid input)', () => {
  it('generate / replay warn when failure bundle is invalid', async () => {
    const fr = useCapabilityFixtureReplay(deps({ capabilityExecutionFailureBundle: ref({}) }))
    await fr.generateCapabilityFailureFixture()
    await fr.replayCapabilityFailureFixture()
    expect(ElMessage.warning).toHaveBeenCalledTimes(2)
    expect(apiFetch).not.toHaveBeenCalled()
  })

  it('replayEfficiency warns when correlation report is invalid', async () => {
    const fr = useCapabilityFixtureReplay(deps({ capabilityExecutionEfficiencyCorrelationReport: ref({}) }))
    await fr.replayCapabilityEfficiencyFeedback()
    expect(ElMessage.warning).toHaveBeenCalled()
    expect(apiFetch).not.toHaveBeenCalled()
  })
})

describe('useCapabilityFixtureReplay generate happy path', () => {
  it('posts, refetches artifacts + library, toasts success', async () => {
    apiFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        status: 'success',
        result: { artifact: { url: '/x' }, fixture: {}, report: { passed: true } },
      }),
    })
    const d = deps({
      capabilityExecutionFailureBundle: ref({
        version: 'capability_execute_failure_bundle.v1', action: 'click', primary_failure: 'x',
      }),
    })
    const fr = useCapabilityFixtureReplay(d)
    await fr.generateCapabilityFailureFixture()
    expect(apiFetch).toHaveBeenCalled()
    expect(d.fetchArtifacts).toHaveBeenCalled()
    expect(ElMessage.success).toHaveBeenCalled()
    expect(ElMessage.error).not.toHaveBeenCalled()
  })
})
