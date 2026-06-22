// Behavior lock for composables/useBottomTabs.js (方向D · 从 App.vue 抽离)
//
// Pins setActiveBottomTab's tab switch + per-tab badge clearing, with the
// external badges (artifacts/final/timeline scroll) resolved lazily via
// resolveExternalBadges() exactly like the original inline implementation.
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { ref } from 'vue'
import { useBottomTabs } from '../src/composables/useBottomTabs.js'

function mk () {
  const hasNewArtifacts = ref(true)
  const hasNewFinalAnswer = ref(true)
  const scrollToBottom = vi.fn()
  const timelinePanelRef = ref({ scrollToBottom })
  const resolveExternalBadges = () => ({ hasNewArtifacts, hasNewFinalAnswer, timelinePanelRef })
  const t = useBottomTabs({ resolveExternalBadges })
  return { t, hasNewArtifacts, hasNewFinalAnswer, scrollToBottom }
}

beforeEach(() => vi.clearAllMocks())

describe('useBottomTabs.setActiveBottomTab', () => {
  it('ignores names not in TAB_ORDER', () => {
    const { t } = mk()
    t.setActiveBottomTab('bogus')
    expect(t.activeBottomTab.value).toBe('terminal')
  })

  it('artifacts tab switches + clears external hasNewArtifacts', () => {
    const { t, hasNewArtifacts } = mk()
    t.setActiveBottomTab('artifacts')
    expect(t.activeBottomTab.value).toBe('artifacts')
    expect(hasNewArtifacts.value).toBe(false)
  })

  it('runs tab clears hasNewRuns', () => {
    const { t } = mk()
    t.hasNewRuns.value = true
    t.setActiveBottomTab('runs')
    expect(t.hasNewRuns.value).toBe(false)
  })

  it('final tab clears external hasNewFinalAnswer', () => {
    const { t, hasNewFinalAnswer } = mk()
    t.setActiveBottomTab('final')
    expect(hasNewFinalAnswer.value).toBe(false)
  })

  it('capability tab clears hasNewCapability', () => {
    const { t } = mk()
    t.hasNewCapability.value = true
    t.setActiveBottomTab('capability')
    expect(t.hasNewCapability.value).toBe(false)
  })

  it('timeline tab clears hasNewPhase and scrolls when autoScroll on', () => {
    const { t, scrollToBottom } = mk()
    t.hasNewPhase.value = true
    t.setActiveBottomTab('timeline')
    expect(t.hasNewPhase.value).toBe(false)
    expect(scrollToBottom).toHaveBeenCalledTimes(1)
  })

  it('timeline tab does not scroll when autoScroll off', () => {
    const { t, scrollToBottom } = mk()
    t.timelineAutoScroll.value = false
    t.setActiveBottomTab('timeline')
    expect(scrollToBottom).not.toHaveBeenCalled()
  })
})
