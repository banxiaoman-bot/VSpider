import { ref } from 'vue'
import { useKeyboardCommand } from './useKeyboardCommand.js'

/**
 * Pure logic: builds the keyboard-action dispatch table.
 * Exported separately so node-level tests can verify routing without
 * needing a Vue component or DOM.
 */
export function buildKeyboardActions (deps) {
  const {
    isRunning, submitTask,
    terminalLogPaneRef, setActiveBottomTab, TAB_ORDER,
    timelinePanelRef, failedRunsPaneRef,
    exportCapabilityTraceAsJsonl,
    helpDialogVisible, promptInputRef,
  } = deps

  const focusPromptInput = () => {
    const inp = promptInputRef.value
    if (!inp) return
    try {
      if (typeof inp.focus === 'function') {
        inp.focus()
      } else if (inp.$el && inp.$el.querySelector) {
        const ta = inp.$el.querySelector('textarea, input')
        if (ta && typeof ta.focus === 'function') ta.focus()
      }
    } catch (_) { /* non-fatal */ }
  }

  return {
    submitIfIdle: () => { if (!isRunning.value) submitTask() },
    terminalSearchOpen: () => terminalLogPaneRef.value?.openTerminalSearch(),
    toggleHelp: () => { helpDialogVisible.value = !helpDialogVisible.value },
    focusPrompt: () => focusPromptInput(),
    selectTab: (i) => setActiveBottomTab(TAB_ORDER[i]),
    phasePrev: () => timelinePanelRef.value?.goToPrevPhaseEvent(),
    phaseNext: () => timelinePanelRef.value?.goToNextPhaseEvent(),
    failedPrev: () => failedRunsPaneRef.value?.goToPrevFailedRun(),
    failedNext: () => failedRunsPaneRef.value?.goToNextFailedRun(),
    terminalSearchClose: () => terminalLogPaneRef.value?.closeTerminalSearch(),
    terminalSearchPrev: () => terminalLogPaneRef.value?.terminalSearchPrev(),
    terminalSearchNext: () => terminalLogPaneRef.value?.terminalSearchNext(),
    exportTimeline: () => timelinePanelRef.value?.exportPhaseEventsAsJsonl(),
    timelineBottom: () => timelinePanelRef.value?.scrollToBottom(),
    timelineTop: () => timelinePanelRef.value?.scrollToTop(),
    exportCapability: () => exportCapabilityTraceAsJsonl(),
    _focusPromptInput: focusPromptInput,
  }
}

/**
 * Pure logic: reads refs to build the keyboard-context snapshot.
 */
export function buildKeyboardContext (deps) {
  const { activeBottomTab, TAB_ORDER, timelinePanelRef, failedRunsPaneRef, terminalLogPaneRef } = deps
  return {
    activeTab: activeBottomTab.value,
    tabCount: TAB_ORDER.length,
    phaseDialogOpen: !!timelinePanelRef.value?.phaseDialogVisible,
    failedDialogOpen: !!failedRunsPaneRef.value?.failedRunDialogVisible,
    terminalSearchVisible: !!terminalLogPaneRef.value?.searchVisible,
  }
}

/**
 * Composable: wires keyboard shortcuts into the app.
 * Owns helpDialogVisible ref; promptInputRef is a template ref passed in.
 * Delegates to useKeyboardCommand for the window-level keydown listener.
 */
export function useAppKeyboard (deps) {
  const helpDialogVisible = ref(false)

  const allDeps = { ...deps, helpDialogVisible }
  const actions = buildKeyboardActions(allDeps)
  const getContext = () => buildKeyboardContext(allDeps)

  useKeyboardCommand({ getContext, actions })

  return {
    helpDialogVisible,
    focusPromptInput: actions._focusPromptInput,
  }
}
