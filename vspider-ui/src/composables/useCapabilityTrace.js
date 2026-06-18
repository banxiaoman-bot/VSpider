// 方向D · 从 App.vue 抽离：capability-trace 派生层（route/execute 事件 → intent/plan/rows/summary/health 等）。
//
// 接收 phaseEvents ref（含 capability_route / capability_execute 事件），返回时间线“能力追踪”
// 面板所需的全部纯派生数据 + 过滤态。不涉及 DOM / 网络 / fixture 重放（那些仍留在 App.vue）。
// 行为与原 App.vue 内联实现逐字一致。
import { ref, computed } from 'vue'
import {
  capabilityEventCrawlEfficiencyPlan,
  capabilityEventActionTrace,
  capabilityEventActionIssueSummary,
  capabilityEventFailureBundle,
  capabilityEventEfficiencyCorrelationReport,
  capabilityEventTraceArtifact,
  capabilityItemName,
} from '../components/capabilityTraceUtils'
import { formatPhasePreviewSeverity, formatPhasePreviewTs } from './phasePreviewFormat.js'

export function useCapabilityTrace (phaseEvents) {
const buildPhaseEventJsonString = (evt) => {
  if (!evt || typeof evt !== 'object') return ''
  const clone = {}
  for (const k of Object.keys(evt)) {
    if (k === '_ts') continue
    clone[k] = evt[k]
  }
  try {
    return JSON.stringify(clone, null, 2)
  } catch (err) {
    return String(err)
  }
}

const capabilityTraceFilter = ref('all')
const capabilityTraceSearchQuery = ref('')

const latestCapabilityRoute = computed(() => {
  for (let i = phaseEvents.value.length - 1; i >= 0; i -= 1) {
    const evt = phaseEvents.value[i]
    if (evt && String(evt.phase || '') === 'capability_route') return evt
  }
  return null
})

const latestCapabilityExecute = computed(() => {
  for (let i = phaseEvents.value.length - 1; i >= 0; i -= 1) {
    const evt = phaseEvents.value[i]
    if (evt && String(evt.phase || '') === 'capability_execute') return evt
  }
  return null
})

const capabilityTraceEvents = computed(() =>
  phaseEvents.value.filter((evt) =>
    ['capability_route', 'capability_execute'].includes(String(evt?.phase || '')),
  ),
)

const capabilityIntent = computed(() => latestCapabilityRoute.value?.intent || {})
const capabilityBackendPlan = computed(() =>
  Array.isArray(latestCapabilityRoute.value?.backend_plan)
    ? latestCapabilityRoute.value.backend_plan
    : [],
)
const capabilityFallbackChain = computed(() =>
  Array.isArray(latestCapabilityRoute.value?.fallback_chain)
    ? latestCapabilityRoute.value.fallback_chain
    : [],
)
const capabilityExecutionPlanSteps = computed(() =>
  Array.isArray(latestCapabilityRoute.value?.execution_plan?.steps)
    ? latestCapabilityRoute.value.execution_plan.steps
    : [],
)
const capabilityWorkflowGraph = computed(() => latestCapabilityRoute.value?.workflow_graph || {})
const capabilityRuntimePreflight = computed(() => latestCapabilityRoute.value?.runtime_preflight || {})
const capabilityRouteCrawlEfficiencyPlan = computed(() =>
  capabilityEventCrawlEfficiencyPlan(latestCapabilityRoute.value),
)
const capabilityWorkflowNodes = computed(() =>
  Array.isArray(capabilityWorkflowGraph.value?.nodes)
    ? capabilityWorkflowGraph.value.nodes
    : [],
)
const capabilityActionRefSchema = computed(() => latestCapabilityRoute.value?.action_ref_schema || {})
const capabilityManifestSummary = computed(() =>
  Array.isArray(latestCapabilityRoute.value?.capability_manifest)
    ? latestCapabilityRoute.value.capability_manifest
    : [],
)
const capabilityModelRoles = computed(() => latestCapabilityRoute.value?.model_roles || {})
const capabilityAuditFindings = computed(() =>
  Array.isArray(latestCapabilityRoute.value?.audit?.findings)
    ? latestCapabilityRoute.value.audit.findings
    : [],
)
const capabilityTraceJson = computed(() =>
  latestCapabilityRoute.value ? buildPhaseEventJsonString(latestCapabilityRoute.value) : '',
)
const capabilityExecutionRuntimeDrift = computed(() =>
  latestCapabilityExecute.value?.runtime_drift || {},
)
const capabilityExecutionRuntimeIssueSummary = computed(() =>
  latestCapabilityExecute.value?.runtime_issue_summary || {},
)
const capabilityExecutionRuntimeIssues = computed(() =>
  Array.isArray(capabilityExecutionRuntimeIssueSummary.value?.issues)
    ? capabilityExecutionRuntimeIssueSummary.value.issues
    : [],
)
const capabilityExecutionRuntimeActions = computed(() =>
  Array.isArray(capabilityExecutionRuntimeIssueSummary.value?.recommended_actions)
    ? capabilityExecutionRuntimeIssueSummary.value.recommended_actions
      .map((action) => String(action || ''))
      .filter((action) => action && action !== 'continue')
    : [],
)
const capabilityExecutionActionTrace = computed(() =>
  capabilityEventActionTrace(latestCapabilityExecute.value),
)
const capabilityExecutionActionIssueSummary = computed(() =>
  capabilityEventActionIssueSummary(latestCapabilityExecute.value, capabilityExecutionActionTrace.value),
)
const capabilityExecutionActionIssues = computed(() =>
  Array.isArray(capabilityExecutionActionIssueSummary.value?.issues)
    ? capabilityExecutionActionIssueSummary.value.issues
    : [],
)
const capabilityExecutionActionIssueActions = computed(() =>
  Array.isArray(capabilityExecutionActionIssueSummary.value?.recommended_actions)
    ? capabilityExecutionActionIssueSummary.value.recommended_actions
      .map((action) => String(action || ''))
      .filter((action) => action && action !== 'continue')
    : [],
)
const capabilityExecutionActionFailureSummary = computed(() =>
  capabilityExecutionActionTrace.value?.result_summary || {},
)
const capabilityExecutionActionRecoveryActions = computed(() =>
  Array.isArray(capabilityExecutionActionFailureSummary.value?.recovery_actions)
    ? capabilityExecutionActionFailureSummary.value.recovery_actions
      .map((action) => String(action || ''))
      .filter((action) => action && action !== 'continue')
    : [],
)
const capabilityExecutionFailureBundle = computed(() =>
  capabilityEventFailureBundle(latestCapabilityExecute.value),
)
const capabilityExecutionCrawlEfficiencyPlan = computed(() =>
  capabilityEventCrawlEfficiencyPlan(latestCapabilityExecute.value),
)
const capabilityActiveCrawlEfficiencyPlan = computed(() =>
  capabilityExecutionCrawlEfficiencyPlan.value?.version
    ? capabilityExecutionCrawlEfficiencyPlan.value
    : capabilityRouteCrawlEfficiencyPlan.value,
)
const capabilityExecutionCrawlEfficiencyCandidates = computed(() =>
  Array.isArray(capabilityActiveCrawlEfficiencyPlan.value?.candidates)
    ? capabilityActiveCrawlEfficiencyPlan.value.candidates
    : [],
)
const capabilityExecutionCrawlEfficiencyAvailablePaths = computed(() =>
  Array.isArray(capabilityActiveCrawlEfficiencyPlan.value?.available_paths)
    ? capabilityActiveCrawlEfficiencyPlan.value.available_paths.map((item) => String(item || '')).filter(Boolean)
    : [],
)
const capabilityExecutionEfficiencyCorrelationReport = computed(() =>
  capabilityEventEfficiencyCorrelationReport(latestCapabilityExecute.value),
)
const capabilityExecutionEfficiencyCorrelationAlignment = computed(() =>
  capabilityExecutionEfficiencyCorrelationReport.value?.alignment || {},
)
const capabilityExecutionEfficiencyCorrelationRootCauses = computed(() =>
  Array.isArray(capabilityExecutionEfficiencyCorrelationReport.value?.root_causes)
    ? capabilityExecutionEfficiencyCorrelationReport.value.root_causes
    : [],
)
const capabilityExecutionEfficiencyCorrelationActions = computed(() =>
  Array.isArray(capabilityExecutionEfficiencyCorrelationReport.value?.recommended_actions)
    ? capabilityExecutionEfficiencyCorrelationReport.value.recommended_actions.map((action) => String(action || '')).filter(Boolean)
    : [],
)
const capabilityExecuteJson = computed(() =>
  latestCapabilityExecute.value ? buildPhaseEventJsonString(latestCapabilityExecute.value) : '',
)
const capabilityTraceRows = computed(() => capabilityTraceEvents.value.map((evt, idx) => {
  const phase = String(evt?.phase || 'unknown')
  const runtimePreflight = evt?.runtime_preflight || {}
  const runtimeIssueSummary = evt?.runtime_issue_summary || {}
  const runtimeDrift = evt?.runtime_drift || {}
  const actionTrace = capabilityEventActionTrace(evt)
  const actionIssueSummary = capabilityEventActionIssueSummary(evt, actionTrace)
  const traceArtifact = capabilityEventTraceArtifact(evt)
  const crawlEfficiencyPlan = capabilityEventCrawlEfficiencyPlan(evt)
  const crawlEfficiencyCandidates = Array.isArray(crawlEfficiencyPlan?.candidates)
    ? crawlEfficiencyPlan.candidates
    : []
  const crawlEfficiencyAvailablePaths = Array.isArray(crawlEfficiencyPlan?.available_paths)
    ? crawlEfficiencyPlan.available_paths.map((item) => String(item || '')).filter(Boolean)
    : []
  const efficiencyCorrelationReport = capabilityEventEfficiencyCorrelationReport(evt)
  const efficiencyCorrelationAlignment = efficiencyCorrelationReport?.alignment || {}
  const efficiencyCorrelationRootCauses = Array.isArray(efficiencyCorrelationReport?.root_causes)
    ? efficiencyCorrelationReport.root_causes.map((item) => String(item || '')).filter(Boolean)
    : []
  const efficiencyCorrelationActions = Array.isArray(efficiencyCorrelationReport?.recommended_actions)
    ? efficiencyCorrelationReport.recommended_actions.map((item) => String(item || '')).filter(Boolean)
    : []
  const efficiencyCorrelationStatus = String(efficiencyCorrelationReport?.status || 'unknown')
  const actionResultSummary = actionTrace?.result_summary || {}
  const actionFailureCode = String(actionResultSummary.failure_code || '')
  const actionFailureCategory = String(actionResultSummary.failure_category || '')
  const actionRecoveryActions = Array.isArray(actionResultSummary.recovery_actions)
    ? actionResultSummary.recovery_actions.map((item) => String(item || '')).filter(Boolean)
    : []
  const runtimePreflightStatus = String(runtimePreflight.status || 'unknown')
  const runtimePreflightWarnings = Array.isArray(runtimePreflight.warnings)
    ? runtimePreflight.warnings.map((item) => String(item || '')).filter(Boolean)
    : []
  const runtimePreflightWarningCount = runtimePreflightWarnings.length
  const runtimeIssueStatus = String(runtimeIssueSummary.status || 'unknown')
  const runtimeDriftStatus = String(runtimeDrift.status || 'unknown')
  const runtimeIssueCount = Number(runtimeIssueSummary.issue_count || 0)
  const runtimeIssueCodes = Array.isArray(runtimeIssueSummary.issues)
    ? runtimeIssueSummary.issues.map((item) => String(item?.code || '')).filter(Boolean)
    : []
  const actionIssueStatus = String(actionIssueSummary.status || 'unknown')
  const actionIssueCount = Number(actionIssueSummary.issue_count || 0)
  const actionIssueCodes = Array.isArray(actionIssueSummary.issues)
    ? actionIssueSummary.issues.map((item) => String(item?.code || '')).filter(Boolean)
    : []
  const actionIssueActions = Array.isArray(actionIssueSummary.recommended_actions)
    ? actionIssueSummary.recommended_actions.map((item) => String(item || '')).filter(Boolean)
    : []
  const actionWarningCodes = Array.isArray(actionTrace?.warning_codes)
    ? actionTrace.warning_codes.map((item) => String(item || '')).filter(Boolean)
    : []
  const hasRouteRuntimePreflightIssue = phase === 'capability_route'
    && (
      runtimePreflightStatus === 'warn'
      || runtimePreflightWarningCount > 0
    )
  const hasRuntimeIssue = phase === 'capability_execute'
    && (
      runtimeIssueStatus === 'warn'
      || runtimeIssueCount > 0
      || runtimeDriftStatus === 'warn'
    )
  const hasActionIssue = phase === 'capability_execute'
    && (
      actionIssueStatus === 'warn'
      || actionIssueStatus === 'error'
      || actionIssueCount > 0
    )
  const hasEfficiencyIssue = phase === 'capability_execute'
    && ['suboptimal', 'needs_repair', 'needs_replan'].includes(efficiencyCorrelationStatus)
  const hasTraceRuntimeIssue = hasRuntimeIssue || hasRouteRuntimePreflightIssue || hasActionIssue || hasEfficiencyIssue
  const previewSeverity = hasTraceRuntimeIssue && formatPhasePreviewSeverity(evt?.severity) === 'info'
    ? 'warn'
    : formatPhasePreviewSeverity(evt?.severity)
  const issue = hasRouteRuntimePreflightIssue
    || (
      phase === 'capability_execute'
      && (
        evt?.completed === false
        || Boolean(evt?.fallback_reason)
        || previewSeverity !== 'info'
        || hasRuntimeIssue
        || hasActionIssue
      )
    )
  const parts = []
  if (evt?.message) parts.push(String(evt.message))
  if (evt?.capability) parts.push(`capability=${evt.capability}`)
  if (evt?.execution_status) parts.push(`status=${evt.execution_status}`)
  if (evt?.completed != null) parts.push(`completed=${Boolean(evt.completed)}`)
  if (evt?.fallback_reason) parts.push(`fallback=${evt.fallback_reason}`)
  if (Array.isArray(evt?.backend_plan)) {
    const names = evt.backend_plan
      .slice(0, 3)
      .map((item) => capabilityItemName(item))
      .filter(Boolean)
    if (names.length) parts.push(`plan=${names.join(' → ')}`)
  }
  if (Array.isArray(evt?.attempts)) parts.push(`${evt.attempts.length} attempts`)
  if (runtimePreflightStatus !== 'unknown' && runtimePreflightStatus !== 'pass') parts.push(`preflight=${runtimePreflightStatus}`)
  if (runtimePreflightWarningCount > 0) parts.push(`preflight_warnings=${runtimePreflightWarningCount}`)
  if (runtimePreflightWarnings.length) parts.push(`preflight_codes=${runtimePreflightWarnings.slice(0, 3).join(',')}`)
  if (runtimePreflight.recommended_action && runtimePreflight.recommended_action !== 'continue') {
    parts.push(`preflight_action=${runtimePreflight.recommended_action}`)
  }
  if (runtimeIssueStatus !== 'unknown' && runtimeIssueStatus !== 'ok') parts.push(`runtime=${runtimeIssueStatus}`)
  if (runtimeIssueCount > 0) parts.push(`runtime_issues=${runtimeIssueCount}`)
  if (runtimeIssueCodes.length) parts.push(`runtime_codes=${runtimeIssueCodes.slice(0, 3).join(',')}`)
  if (runtimeDriftStatus !== 'unknown' && runtimeDriftStatus !== 'stable') parts.push(`drift=${runtimeDriftStatus}`)
  if (runtimeIssueSummary.recommended_action && runtimeIssueSummary.recommended_action !== 'continue') {
    parts.push(`action=${runtimeIssueSummary.recommended_action}`)
  }
  if (actionTrace?.action) parts.push(`browser_action=${actionTrace.action}`)
  if (actionIssueStatus !== 'unknown' && actionIssueStatus !== 'ok') parts.push(`action_issue=${actionIssueStatus}`)
  if (actionIssueCount > 0) parts.push(`action_issues=${actionIssueCount}`)
  if (actionIssueCodes.length) parts.push(`action_codes=${actionIssueCodes.slice(0, 3).join(',')}`)
  if (actionIssueSummary.recommended_action && actionIssueSummary.recommended_action !== 'continue') {
    parts.push(`action_issue_action=${actionIssueSummary.recommended_action}`)
  }
  if (actionFailureCode) parts.push(`failure=${actionFailureCode}`)
  if (actionFailureCategory) parts.push(`failure_category=${actionFailureCategory}`)
  if (actionRecoveryActions.length) parts.push(`recovery=${actionRecoveryActions.slice(0, 3).join(',')}`)
  if (traceArtifact.path || traceArtifact.url) parts.push('trace_artifact=available')
  if (crawlEfficiencyPlan?.recommended_path) parts.push(`crawl_efficiency=${crawlEfficiencyPlan.recommended_path}`)
  if (crawlEfficiencyPlan?.skip_browser != null) parts.push(`skip_browser=${Boolean(crawlEfficiencyPlan.skip_browser)}`)
  if (crawlEfficiencyPlan?.skip_vlm != null) parts.push(`skip_vlm=${Boolean(crawlEfficiencyPlan.skip_vlm)}`)
  if (crawlEfficiencyAvailablePaths.length) parts.push(`available_paths=${crawlEfficiencyAvailablePaths.slice(0, 4).join(',')}`)
  if (efficiencyCorrelationReport?.status) parts.push(`efficiency_correlation=${efficiencyCorrelationReport.status}`)
  if (efficiencyCorrelationAlignment.executed_path) parts.push(`executed_path=${efficiencyCorrelationAlignment.executed_path}`)
  if (efficiencyCorrelationAlignment.recommended_path) parts.push(`recommended_path=${efficiencyCorrelationAlignment.recommended_path}`)
  if (efficiencyCorrelationRootCauses.length) parts.push(`efficiency_causes=${efficiencyCorrelationRootCauses.slice(0, 3).join(',')}`)
  if (efficiencyCorrelationActions.length) parts.push(`efficiency_actions=${efficiencyCorrelationActions.slice(0, 3).join(',')}`)
  const actionSearchText = [
    phase,
    evt?.message,
    evt?.capability,
    evt?.execution_status,
    evt?.fallback_reason,
    parts.join(' '),
    actionTrace?.action,
    actionTrace?.status,
    actionTrace?.target?.selector,
    actionTrace?.target?.ref,
    actionTrace?.action_ref?.selector,
    actionTrace?.action_ref?.ref,
    actionIssueStatus,
    actionIssueCodes.join(' '),
    actionIssueActions.join(' '),
    actionWarningCodes.join(' '),
    actionIssueSummary.recommended_action,
    actionFailureCode,
    actionFailureCategory,
    actionRecoveryActions.join(' '),
    traceArtifact.path,
    traceArtifact.url,
    crawlEfficiencyPlan?.recommended_path,
    crawlEfficiencyAvailablePaths.join(' '),
    crawlEfficiencyCandidates.map((item) => `${item?.name || ''} ${item?.reason || ''}`).join(' '),
    efficiencyCorrelationReport?.status,
    efficiencyCorrelationAlignment.recommended_path,
    efficiencyCorrelationAlignment.executed_path,
    efficiencyCorrelationRootCauses.join(' '),
    efficiencyCorrelationActions.join(' '),
  ]
    .map((item) => String(item || '').toLowerCase())
    .filter(Boolean)
    .join(' ')
  return {
    idx,
    event: evt,
    phase,
    severity: previewSeverity,
    issue,
    time: formatPhasePreviewTs(Number.isFinite(evt?._ts) ? evt._ts : evt?.ts),
    detail: parts.join(' · '),
    searchText: actionSearchText,
  }
}))

const capabilityTraceSummary = computed(() => {
  const rows = capabilityTraceRows.value
  return {
    all: rows.length,
    route: rows.filter((row) => row.phase === 'capability_route').length,
    execute: rows.filter((row) => row.phase === 'capability_execute').length,
    issues: rows.filter((row) => row.issue).length,
  }
})

const capabilityFilteredTraceRows = computed(() => {
  const filter = capabilityTraceFilter.value
  let rows = capabilityTraceRows.value
  if (filter === 'route') {
    rows = rows.filter((row) => row.phase === 'capability_route')
  }
  else if (filter === 'execute') {
    rows = rows.filter((row) => row.phase === 'capability_execute')
  }
  else if (filter === 'issues') {
    rows = rows.filter((row) => row.issue)
  }
  const q = String(capabilityTraceSearchQuery.value || '').trim().toLowerCase()
  if (!q) return rows
  return rows.filter((row) => String(row.searchText || row.detail || '').toLowerCase().includes(q))
})

const capabilityExecutionAlignment = computed(() => {
  const capability = String(latestCapabilityExecute.value?.capability || '')
  const planNames = capabilityBackendPlan.value.map((item) => capabilityItemName(item))
  const rank = capability ? planNames.indexOf(capability) : -1
  const completed = Boolean(latestCapabilityExecute.value?.completed)
  const fallbackReason = String(latestCapabilityExecute.value?.fallback_reason || '')
  return {
    capability,
    rank: rank >= 0 ? rank + 1 : null,
    topChoice: Boolean(capability && planNames[0] === capability),
    inPlan: rank >= 0,
    completed,
    fallback: Boolean(fallbackReason || (capability && planNames[0] && planNames[0] !== capability)),
    fallbackReason,
    planHead: planNames[0] || '',
  }
})

const capabilityTraceHealth = computed(() => {
  const summary = capabilityTraceSummary.value
  const alignment = capabilityExecutionAlignment.value
  const runtimeIssue = capabilityExecutionRuntimeIssueSummary.value || {}
  const runtimeIssueStatus = String(runtimeIssue.status || '')
  const runtimeIssueCount = Number(runtimeIssue.issue_count || 0)
  const runtimeIssueAction = String(runtimeIssue.recommended_action || '')
  const runtimeAlignment = runtimeIssueStatus === 'warn' || runtimeIssueCount > 0
    ? `runtime ${runtimeIssueStatus || 'warn'} (${runtimeIssueCount})${runtimeIssueAction && runtimeIssueAction !== 'continue' ? ` · ${runtimeIssueAction}` : ''}`
    : ''
  const routePreflight = capabilityRuntimePreflight.value || {}
  const routePreflightStatus = String(routePreflight.status || '')
  const routePreflightWarnings = Array.isArray(routePreflight.warnings) ? routePreflight.warnings : []
  const routePreflightAction = String(routePreflight.recommended_action || '')
  const routePreflightAlignment = routePreflightStatus === 'warn' || routePreflightWarnings.length > 0
    ? `preflight ${routePreflightStatus || 'warn'} (${routePreflightWarnings.length})${routePreflightAction && routePreflightAction !== 'continue' ? ` · ${routePreflightAction}` : ''}`
    : ''
  if (!summary.all) {
    return { status: 'empty', label: '暂无 trace', route: 0, execute: 0, issues: 0, alignment: '' }
  }
  if (summary.issues > 0) {
    return {
      status: 'issue',
      label: '存在问题',
      route: summary.route,
      execute: summary.execute,
      issues: summary.issues,
      alignment: alignment.fallbackReason || runtimeAlignment || routePreflightAlignment || '需要检查执行结果',
    }
  }
  if (!summary.execute) {
    return {
      status: 'route-only',
      label: '仅路由',
      route: summary.route,
      execute: summary.execute,
      issues: summary.issues,
      alignment: alignment.planHead ? `preferred ${alignment.planHead}` : '',
    }
  }
  if (alignment.topChoice && alignment.completed) {
    return {
      status: 'healthy',
      label: '健康',
      route: summary.route,
      execute: summary.execute,
      issues: summary.issues,
      alignment: `top choice ${alignment.capability}`,
    }
  }
  if (alignment.fallback) {
    return {
      status: 'fallback',
      label: 'Fallback',
      route: summary.route,
      execute: summary.execute,
      issues: summary.issues,
      alignment: alignment.fallbackReason || `preferred ${alignment.planHead || 'unknown'}`,
    }
  }
  return {
    status: 'executed',
    label: '已执行',
    route: summary.route,
    execute: summary.execute,
    issues: summary.issues,
    alignment: alignment.capability || '',
  }
})

const capabilityRuntimePreflightClass = computed(() => {
  const status = String(capabilityRuntimePreflight.value.status || 'unknown')
  if (status === 'pass') return 'healthy'
  if (status === 'warn') return 'fallback'
  if (status === 'block') return 'issue'
  return 'route-only'
})
const capabilityRuntimePreflightLabel = computed(() => {
  const status = String(capabilityRuntimePreflight.value.status || 'unknown')
  if (status === 'pass') return '预检通过'
  if (status === 'warn') return '预检提醒'
  if (status === 'block') return '预检阻断'
  return '未预检'
})
const capabilityRoleRows = computed(() => Object.entries(capabilityModelRoles.value || {})
  .map(([key, value]) => ({
    key,
    position: value?.position || '',
    responsibilities: Array.isArray(value?.responsibilities) ? value.responsibilities.slice(0, 4) : [],
    shouldNotDo: Array.isArray(value?.should_not_do) ? value.should_not_do.slice(0, 4) : [],
    recommendedUse: value?.recommended_use || '',
  })))


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
    capabilityModelRoles,
    capabilityAuditFindings,
    capabilityTraceJson,
    capabilityExecutionRuntimeDrift,
    capabilityExecutionRuntimeIssueSummary,
    capabilityExecutionRuntimeIssues,
    capabilityExecutionRuntimeActions,
    capabilityExecutionActionTrace,
    capabilityExecutionActionIssueSummary,
    capabilityExecutionActionIssues,
    capabilityExecutionActionIssueActions,
    capabilityExecutionActionFailureSummary,
    capabilityExecutionActionRecoveryActions,
    capabilityExecutionFailureBundle,
    capabilityExecutionCrawlEfficiencyPlan,
    capabilityActiveCrawlEfficiencyPlan,
    capabilityExecutionCrawlEfficiencyCandidates,
    capabilityExecutionCrawlEfficiencyAvailablePaths,
    capabilityExecutionEfficiencyCorrelationReport,
    capabilityExecutionEfficiencyCorrelationAlignment,
    capabilityExecutionEfficiencyCorrelationRootCauses,
    capabilityExecutionEfficiencyCorrelationActions,
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
  }
}
