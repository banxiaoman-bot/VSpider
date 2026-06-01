export const capabilityEventActionTrace = (evt) => (
  evt?.action_trace
  || evt?.result?.action_trace
  || evt?.detail?.action_trace
  || {}
)

export const capabilityEventActionIssueSummary = (evt, actionTrace = capabilityEventActionTrace(evt)) => (
  actionTrace?.issue_summary
  || evt?.action_issue_summary
  || evt?.result?.action_issue_summary
  || evt?.detail?.action_issue_summary
  || {}
)

export const capabilityEventTraceArtifact = (evt) => (
  evt?.trace_artifact
  || evt?.result?.trace_artifact
  || evt?.detail?.trace_artifact
  || {}
)

export const capabilityEventFailureBundle = (evt) => (
  evt?.failure_bundle
  || evt?.result?.failure_bundle
  || evt?.detail?.failure_bundle
  || evt?.detail?.phase_event?.failure_bundle
  || {}
)

export const capabilityEventCrawlEfficiencyPlan = (evt) => (
  evt?.crawl_efficiency_plan
  || evt?.result?.crawl_efficiency_plan
  || evt?.detail?.crawl_efficiency_plan
  || evt?.detail?.phase_event?.crawl_efficiency_plan
  || {}
)

export const capabilityEventEfficiencyCorrelationReport = (evt) => (
  evt?.efficiency_correlation_report
  || evt?.result?.efficiency_correlation_report
  || evt?.detail?.efficiency_correlation_report
  || evt?.detail?.phase_event?.efficiency_correlation_report
  || {}
)

export const capabilityCrawlEfficiencyCandidateClass = (item) => (
  item?.available ? 'is-available' : 'is-unavailable'
)

export const capabilityCrawlEfficiencyEvidence = (item) => {
  const evidence = item?.evidence || {}
  const parts = []
  for (const key of ['candidate_count', 'best_score', 'source_length', 'selector', 'interactive_count', 'runtime_status']) {
    if (evidence[key] != null && evidence[key] !== '') parts.push(`${key}=${evidence[key]}`)
  }
  return parts.join(' · ')
}

export const capabilityItemName = (item) => (
  String(item?.name || item?.capability || item?.phase || 'unknown')
)

export const capabilityItemMeta = (item) => {
  const parts = []
  if (item?.owner) parts.push(String(item.owner))
  if (item?.category) parts.push(String(item.category))
  if (item?.milestone) parts.push(String(item.milestone))
  if (item?.risk) parts.push(`risk=${item.risk}`)
  return parts.join(' · ')
}

export const capabilityItemDetail = (item) => (
  String(item?.reason || item?.description || item?.detail || item?.summary || '')
)

export const capabilityAttemptClass = (item) => {
  if (item?.completed === true || item?.status === 'completed') return 'is-complete'
  if (item?.status === 'error') return 'is-error'
  if (item?.status === 'skipped') return 'is-skip'
  return 'is-attempted'
}
