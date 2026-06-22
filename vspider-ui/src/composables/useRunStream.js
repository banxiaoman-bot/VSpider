import { useRunEventRouter } from './useRunEventRouter.js'
import { useWebSocket } from './useWebSocket.js'

/**
 * Pure logic: builds the four WebSocket lifecycle callbacks.
 */
export function buildWebSocketHandlers ({ appendLog, handleSocketMessage }) {
  return {
    onOpen: () => appendLog('[SYSTEM] WebSocket connected'),
    onMessage: handleSocketMessage,
    onClose: () => appendLog('[SYSTEM] WebSocket disconnected'),
    onError: () => appendLog('[ERROR] WebSocket error'),
  }
}

/**
 * Composable: wires useRunEventRouter + useWebSocket + scrollToBottom.
 */
export function useRunStream (deps) {
  const {
    appendLog, terminalLogPaneRef,
    pushScreenshotFrame, isRunning,
    isHumanInterventionRequired, humanInterventionReason,
    hitlScreenshot, hitlFormFields, hitlFormReason,
    hitlFormScreenshot, hitlFormLoading, hitlFormVisible,
    fetchBrowserRuntimeStatus, runHistoryRefreshToken,
    activeBottomTab, hasNewRuns, hasNewCapability, hasNewPhase,
    timelineAutoScroll, failedRunsPaneRef, applyDoneAnswer,
    replayMode, phaseEvents, fetchArtifacts, hasNewArtifacts,
    currentImageBase64, timelinePanelRef,
  } = deps

  const scrollToBottom = () => terminalLogPaneRef.value?.scrollToBottom()

  const { handleSocketMessage } = useRunEventRouter({
    appendLog, pushScreenshotFrame, isRunning,
    isHumanInterventionRequired, humanInterventionReason,
    hitlScreenshot, hitlFormFields, hitlFormReason,
    hitlFormScreenshot, hitlFormLoading, hitlFormVisible,
    fetchBrowserRuntimeStatus, runHistoryRefreshToken,
    activeBottomTab, hasNewRuns, hasNewCapability, hasNewPhase,
    timelineAutoScroll, failedRunsPaneRef, applyDoneAnswer,
    replayMode, phaseEvents, fetchArtifacts, hasNewArtifacts,
    currentImageBase64, timelinePanelRef,
  })

  const wsHandlers = buildWebSocketHandlers({ appendLog, handleSocketMessage })
  const {
    status: wsStatus,
    connect: connectWebSocket,
    disconnect: disconnectWebSocket,
  } = useWebSocket(wsHandlers)

  return {
    wsStatus,
    connectWebSocket,
    disconnectWebSocket,
    handleSocketMessage,
    scrollToBottom,
  }
}
