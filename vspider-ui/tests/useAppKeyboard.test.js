// Behavior lock for composables/useAppKeyboard.js (D-UI-26 · keyboard wiring)
//
// Pins buildKeyboardActions / buildKeyboardContext pure logic that was
// previously inline in App.vue's <script setup>.
import { describe, it, expect, vi } from 'vitest'
import { ref } from 'vue'

import {
  buildKeyboardActions,
  buildKeyboardContext,
} from '../src/composables/useAppKeyboard.js'

describe('buildKeyboardActions', () => {
  function makeDeps (overrides = {}) {
    return {
      isRunning: ref(false),
      submitTask: vi.fn(),
      terminalLogPaneRef: ref(null),
      setActiveBottomTab: vi.fn(),
      TAB_ORDER: ['terminal', 'timeline', 'capability', 'artifacts', 'final', 'runs'],
      timelinePanelRef: ref(null),
      failedRunsPaneRef: ref(null),
      exportCapabilityTraceAsJsonl: vi.fn(),
      helpDialogVisible: ref(false),
      promptInputRef: ref(null),
      ...overrides,
    }
  }

  it('submitIfIdle calls submitTask when not running', () => {
    const submitTask = vi.fn()
    const actions = buildKeyboardActions(makeDeps({ submitTask, isRunning: ref(false) }))
    actions.submitIfIdle()
    expect(submitTask).toHaveBeenCalledOnce()
  })

  it('submitIfIdle does NOT call submitTask when running', () => {
    const submitTask = vi.fn()
    const actions = buildKeyboardActions(makeDeps({ submitTask, isRunning: ref(true) }))
    actions.submitIfIdle()
    expect(submitTask).not.toHaveBeenCalled()
  })

  it('selectTab(2) calls setActiveBottomTab with TAB_ORDER[2]', () => {
    const setActiveBottomTab = vi.fn()
    const actions = buildKeyboardActions(makeDeps({ setActiveBottomTab }))
    actions.selectTab(2)
    expect(setActiveBottomTab).toHaveBeenCalledWith('capability')
  })

  it('toggleHelp flips helpDialogVisible', () => {
    const helpDialogVisible = ref(false)
    const actions = buildKeyboardActions(makeDeps({ helpDialogVisible }))
    actions.toggleHelp()
    expect(helpDialogVisible.value).toBe(true)
    actions.toggleHelp()
    expect(helpDialogVisible.value).toBe(false)
  })

  it('terminalSearchOpen delegates to terminalLogPaneRef', () => {
    const openFn = vi.fn()
    const terminalLogPaneRef = ref({ openTerminalSearch: openFn })
    const actions = buildKeyboardActions(makeDeps({ terminalLogPaneRef }))
    actions.terminalSearchOpen()
    expect(openFn).toHaveBeenCalledOnce()
  })

  it('exportCapability delegates to exportCapabilityTraceAsJsonl', () => {
    const exportFn = vi.fn()
    const actions = buildKeyboardActions(makeDeps({ exportCapabilityTraceAsJsonl: exportFn }))
    actions.exportCapability()
    expect(exportFn).toHaveBeenCalledOnce()
  })

  it('phasePrev/phaseNext delegate to timelinePanelRef', () => {
    const prev = vi.fn()
    const next = vi.fn()
    const timelinePanelRef = ref({ goToPrevPhaseEvent: prev, goToNextPhaseEvent: next })
    const actions = buildKeyboardActions(makeDeps({ timelinePanelRef }))
    actions.phasePrev()
    actions.phaseNext()
    expect(prev).toHaveBeenCalledOnce()
    expect(next).toHaveBeenCalledOnce()
  })

  it('failedPrev/failedNext delegate to failedRunsPaneRef', () => {
    const prev = vi.fn()
    const next = vi.fn()
    const failedRunsPaneRef = ref({ goToPrevFailedRun: prev, goToNextFailedRun: next })
    const actions = buildKeyboardActions(makeDeps({ failedRunsPaneRef }))
    actions.failedPrev()
    actions.failedNext()
    expect(prev).toHaveBeenCalledOnce()
    expect(next).toHaveBeenCalledOnce()
  })
})

describe('buildKeyboardContext', () => {
  it('returns correct context shape', () => {
    const deps = {
      activeBottomTab: ref('terminal'),
      TAB_ORDER: ['terminal', 'timeline', 'capability'],
      timelinePanelRef: ref({ phaseDialogVisible: true }),
      failedRunsPaneRef: ref({ failedRunDialogVisible: false }),
      terminalLogPaneRef: ref({ searchVisible: true }),
    }
    const ctx = buildKeyboardContext(deps)
    expect(ctx).toEqual({
      activeTab: 'terminal',
      tabCount: 3,
      phaseDialogOpen: true,
      failedDialogOpen: false,
      terminalSearchVisible: true,
    })
  })

  it('coerces nullish refs to false via !!', () => {
    const deps = {
      activeBottomTab: ref('timeline'),
      TAB_ORDER: ['timeline'],
      timelinePanelRef: ref(null),
      failedRunsPaneRef: ref(null),
      terminalLogPaneRef: ref(null),
    }
    const ctx = buildKeyboardContext(deps)
    expect(ctx.phaseDialogOpen).toBe(false)
    expect(ctx.failedDialogOpen).toBe(false)
    expect(ctx.terminalSearchVisible).toBe(false)
  })
})
