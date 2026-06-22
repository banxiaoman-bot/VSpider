import { useCapabilityTrace } from './useCapabilityTrace.js'
import { useCapabilityFixtureReplay } from './useCapabilityFixtureReplay.js'
import { useCapabilityTraceExport } from './useCapabilityTraceExport.js'
import { writeToClipboard } from './useClipboard.js'

/**
 * Pure logic: command→handler dispatch for the capability "more" dropdown.
 */
export function buildCapabilityMoreActionHandler (handlers) {
  return (command) => { handlers[command]?.() }
}

/**
 * Pure logic: resets phaseEvents + badge flags + autoScroll.
 */
export function makeClearPhaseEvents ({ phaseEvents, hasNewPhase, hasNewCapability, timelineAutoScroll }) {
  return () => {
    phaseEvents.value = []
    hasNewPhase.value = false
    hasNewCapability.value = false
    timelineAutoScroll.value = true
  }
}

/**
 * Composable: aggregates capability-trace + fixture-replay + export + clear + more-action dispatch.
 */
export function useCapabilityTracePanel ({
  phaseEvents, hasNewPhase, hasNewCapability, timelineAutoScroll,
  url, prompt, fetchArtifacts, triggerReplayImport,
}) {
  const capabilityTrace = useCapabilityTrace(phaseEvents)
  const {
    latestCapabilityRoute,
    latestCapabilityExecute,
    capabilityTraceEvents,
    capabilityIntent,
    capabilityBackendPlan,
    capabilityFallbackChain,
    capabilityExecutionPlanSteps,
    capabilityWorkflowGraph,
    capabilityRuntimePreflight,
    capabilityRouteCrawlEfficiencyPlan,
    capabilityWorkflowNodes,
    capabilityActionRefSchema,
    capabilityManifestSummary,
    capabilityAuditFindings,
    capabilityTraceJson,
    capabilityExecutionFailureBundle,
    capabilityExecutionEfficiencyCorrelationReport,
    capabilityExecuteJson,
    capabilityTraceRows,
    capabilityTraceSummary,
    capabilityFilteredTraceRows,
    capabilityExecutionAlignment,
    capabilityTraceHealth,
    capabilityRuntimePreflightClass,
    capabilityRuntimePreflightLabel,
    capabilityRoleRows,
    capabilityTraceFilter,
    capabilityTraceSearchQuery,
  } = capabilityTrace

  const {
    capabilityReplayPaneProps,
    copyCapabilityFailureFixtureBatchReplaySummary,
    generateCapabilityFailureFixture,
    replayCapabilityFailureFixture,
    replayCapabilityEfficiencyFeedback,
    fetchCapabilityEfficiencyFeedbackReplays,
    fetchCapabilityFailureFixtures,
    fetchCapabilityFailureFixtureBatchHistory,
    batchReplayCapabilityFailureFixtures,
  } = useCapabilityFixtureReplay({
    url,
    prompt,
    fetchArtifacts: () => fetchArtifacts(),
    writeToClipboard,
    latestCapabilityExecute,
    capabilityExecutionFailureBundle,
    capabilityExecutionEfficiencyCorrelationReport,
  })

  const clearPhaseEvents = makeClearPhaseEvents({ phaseEvents, hasNewPhase, hasNewCapability, timelineAutoScroll })

  const {
    exportCapabilityTraceAsJsonl,
    copyCapabilityTraceSummary,
  } = useCapabilityTraceExport({
    trace: capabilityTrace,
    writeToClipboard,
  })

  const handleCapabilityMoreAction = buildCapabilityMoreActionHandler({
    copySummary: copyCapabilityTraceSummary,
    generateFixture: generateCapabilityFailureFixture,
    replayFixture: replayCapabilityFailureFixture,
    refreshFixtures: fetchCapabilityFailureFixtures,
    refreshBatchHistory: fetchCapabilityFailureFixtureBatchHistory,
    batchReplay: batchReplayCapabilityFailureFixtures,
    replayEfficiency: replayCapabilityEfficiencyFeedback,
    refreshEfficiencyReplays: fetchCapabilityEfficiencyFeedbackReplays,
    importReplay: () => triggerReplayImport('capability'),
  })

  return {
    latestCapabilityRoute,
    latestCapabilityExecute,
    capabilityTraceEvents,
    capabilityIntent,
    capabilityBackendPlan,
    capabilityFallbackChain,
    capabilityExecutionPlanSteps,
    capabilityWorkflowGraph,
    capabilityRuntimePreflight,
    capabilityRouteCrawlEfficiencyPlan,
    capabilityWorkflowNodes,
    capabilityActionRefSchema,
    capabilityManifestSummary,
    capabilityAuditFindings,
    capabilityTraceJson,
    capabilityExecutionFailureBundle,
    capabilityExecutionEfficiencyCorrelationReport,
    capabilityExecuteJson,
    capabilityTraceRows,
    capabilityTraceSummary,
    capabilityFilteredTraceRows,
    capabilityExecutionAlignment,
    capabilityTraceHealth,
    capabilityRuntimePreflightClass,
    capabilityRuntimePreflightLabel,
    capabilityRoleRows,
    capabilityTraceFilter,
    capabilityTraceSearchQuery,
    capabilityReplayPaneProps,
    copyCapabilityFailureFixtureBatchReplaySummary,
    generateCapabilityFailureFixture,
    replayCapabilityFailureFixture,
    replayCapabilityEfficiencyFeedback,
    fetchCapabilityEfficiencyFeedbackReplays,
    fetchCapabilityFailureFixtures,
    fetchCapabilityFailureFixtureBatchHistory,
    batchReplayCapabilityFailureFixtures,
    clearPhaseEvents,
    exportCapabilityTraceAsJsonl,
    copyCapabilityTraceSummary,
    handleCapabilityMoreAction,
  }
}
