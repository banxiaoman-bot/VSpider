// 方向D · 从 App.vue 抽离：capability trace 导出/摘要子系统。
//
// 三个动作：把 capability trace 事件导出为 .jsonl、把多维度遥测拼成纯文本
// 摘要、复制摘要到剪贴板。所有派生数据来自 useCapabilityTrace 的返回对象
// （整体经 trace 注入，函数体逐字保留），剪贴板写入经注入的 writeToClipboard，
// 反馈经 ElMessage。行为与原 App.vue 内联实现逐字一致。
import { ElMessage } from 'element-plus'

export function useCapabilityTraceExport ({ trace, writeToClipboard }) {
  const {
    capabilityTraceEvents,
    capabilityTraceHealth,
    capabilityExecutionAlignment,
    capabilityIntent,
    capabilityRuntimePreflight,
    capabilityExecutionRuntimeIssueSummary,
    capabilityExecutionRuntimeDrift,
    capabilityExecutionRuntimeIssues,
    capabilityExecutionRuntimeActions,
    capabilityExecutionActionTrace,
    capabilityExecutionActionIssueSummary,
    capabilityExecutionActionIssues,
    capabilityExecutionActionIssueActions,
    capabilityExecutionActionFailureSummary,
    capabilityExecutionActionRecoveryActions,
    capabilityActiveCrawlEfficiencyPlan,
    capabilityExecutionCrawlEfficiencyCandidates,
    capabilityExecutionCrawlEfficiencyAvailablePaths,
    capabilityExecutionEfficiencyCorrelationReport,
    capabilityExecutionEfficiencyCorrelationAlignment,
    capabilityExecutionEfficiencyCorrelationRootCauses,
    capabilityExecutionEfficiencyCorrelationActions,
  } = trace

  const exportCapabilityTraceAsJsonl = () => {
    const src = capabilityTraceEvents.value
    if (!src.length) {
      ElMessage.warning('当前没有可导出的 capability trace 事件')
      return
    }
    const lines = []
    for (const e of src) {
      const clone = {}
      for (const k of Object.keys(e)) {
        if (k === '_ts') continue
        clone[k] = e[k]
      }
      try {
        lines.push(JSON.stringify(clone))
      } catch (err) {
        lines.push(JSON.stringify({ phase: 'capability_unknown', error: String(err) }))
      }
    }
    const ts = new Date()
    const stamp =
      `${ts.getFullYear()}${String(ts.getMonth() + 1).padStart(2, '0')}` +
      `${String(ts.getDate()).padStart(2, '0')}_${String(ts.getHours()).padStart(2, '0')}` +
      `${String(ts.getMinutes()).padStart(2, '0')}${String(ts.getSeconds()).padStart(2, '0')}`
    const blob = new Blob([lines.join('\n') + '\n'], {
      type: 'application/x-ndjson;charset=utf-8',
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `capability_trace_${stamp}.jsonl`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    ElMessage.success(`已导出 ${lines.length} 条 capability trace 事件`)
  }

  const buildCapabilityTraceSummaryText = () => {
    const health = capabilityTraceHealth.value
    const alignment = capabilityExecutionAlignment.value
    const intent = capabilityIntent.value || {}
    const routePreflight = capabilityRuntimePreflight.value || {}
    const routePreflightWarnings = Array.isArray(routePreflight.warnings) ? routePreflight.warnings : []
    const runtimeIssue = capabilityExecutionRuntimeIssueSummary.value || {}
    const runtimeDrift = capabilityExecutionRuntimeDrift.value || {}
    const runtimeIssues = capabilityExecutionRuntimeIssues.value || []
    const runtimeActions = capabilityExecutionRuntimeActions.value || []
    const actionTrace = capabilityExecutionActionTrace.value || {}
    const actionIssue = capabilityExecutionActionIssueSummary.value || {}
    const actionIssues = capabilityExecutionActionIssues.value || []
    const actionActions = capabilityExecutionActionIssueActions.value || []
    const actionFailure = capabilityExecutionActionFailureSummary.value || {}
    const actionRecoveryActions = capabilityExecutionActionRecoveryActions.value || []
    const crawlEfficiency = capabilityActiveCrawlEfficiencyPlan.value || {}
    const crawlCandidates = capabilityExecutionCrawlEfficiencyCandidates.value || []
    const crawlAvailablePaths = capabilityExecutionCrawlEfficiencyAvailablePaths.value || []
    const efficiencyCorrelation = capabilityExecutionEfficiencyCorrelationReport.value || {}
    const efficiencyAlignment = capabilityExecutionEfficiencyCorrelationAlignment.value || {}
    const efficiencyRootCauses = capabilityExecutionEfficiencyCorrelationRootCauses.value || []
    const efficiencyActions = capabilityExecutionEfficiencyCorrelationActions.value || []
    const lines = [
      '# Capability Trace Summary',
      '',
      `Health: ${health.label || 'unknown'}`,
      `Route events: ${health.route}`,
      `Execute events: ${health.execute}`,
      `Issues: ${health.issues}`,
    ]
    if (intent.task_type) lines.push(`Intent: ${intent.task_type}`)
    if (intent.output_mode) lines.push(`Output mode: ${intent.output_mode}`)
    if (alignment.planHead) lines.push(`Preferred: ${alignment.planHead}`)
    if (alignment.capability) lines.push(`Executed: ${alignment.capability}`)
    if (alignment.rank) lines.push(`Plan rank: #${alignment.rank}`)
    else if (alignment.capability) lines.push('Plan rank: not in plan')
    lines.push(`Top choice: ${alignment.topChoice ? 'yes' : 'no'}`)
    lines.push(`Completed: ${alignment.completed ? 'yes' : 'no'}`)
    if (crawlEfficiency.version) {
      lines.push(`Crawl efficiency path: ${crawlEfficiency.recommended_path || 'unknown'}`)
      lines.push(`Crawl efficiency skip browser: ${crawlEfficiency.skip_browser ? 'yes' : 'no'}`)
      lines.push(`Crawl efficiency skip VLM: ${crawlEfficiency.skip_vlm ? 'yes' : 'no'}`)
    }
    if (crawlAvailablePaths.length) {
      lines.push(`Crawl efficiency available paths: ${crawlAvailablePaths.join(', ')}`)
    }
    if (crawlCandidates.length) {
      const topCandidates = crawlCandidates
        .slice(0, 4)
        .map((item) => `${item.name || 'unknown'}:${item.available ? 'available' : 'unavailable'}:${item.reason || ''}`)
      lines.push(`Crawl efficiency candidates: ${topCandidates.join(' | ')}`)
    }
    if (efficiencyCorrelation.version) {
      lines.push(`Efficiency correlation: ${efficiencyCorrelation.status || 'unknown'}`)
      lines.push(`Efficiency recommended path: ${efficiencyAlignment.recommended_path || 'unknown'}`)
      lines.push(`Efficiency executed path: ${efficiencyAlignment.executed_path || 'unknown'}`)
    }
    if (efficiencyRootCauses.length) {
      lines.push(`Efficiency root causes: ${efficiencyRootCauses.join(', ')}`)
    }
    if (efficiencyActions.length) {
      lines.push(`Efficiency actions: ${efficiencyActions.join(', ')}`)
    }
    if (routePreflight.version) {
      lines.push(`Route preflight: ${routePreflight.status || 'unknown'} (${routePreflightWarnings.length})`)
    }
    if (routePreflight.recommended_action && routePreflight.recommended_action !== 'continue') {
      lines.push(`Route preflight action: ${routePreflight.recommended_action}`)
    }
    if (routePreflightWarnings.length) {
      lines.push(`Route preflight warnings: ${routePreflightWarnings.slice(0, 5).join(', ')}`)
    }
    if (runtimeIssue.version) {
      lines.push(`Runtime issues: ${runtimeIssue.status || 'unknown'} (${runtimeIssue.issue_count ?? 0})`)
    }
    if (runtimeDrift.version) {
      lines.push(`Runtime drift: ${runtimeDrift.status || 'unknown'}`)
    }
    if (runtimeIssue.recommended_action && runtimeIssue.recommended_action !== 'continue') {
      lines.push(`Runtime action: ${runtimeIssue.recommended_action}`)
    }
    if (runtimeIssues.length) {
      const issueCodes = runtimeIssues
        .slice(0, 5)
        .map((issue) => `${issue.source || 'runtime'}:${issue.code || 'issue'}`)
      lines.push(`Runtime issue codes: ${issueCodes.join(', ')}`)
    }
    if (runtimeActions.length) {
      lines.push(`Runtime actions: ${runtimeActions.join(', ')}`)
    }
    if (actionIssue.version) {
      lines.push(`Browser action issues: ${actionIssue.status || 'unknown'} (${actionIssue.issue_count ?? 0})`)
    }
    if (actionTrace.action) {
      lines.push(`Browser action: ${actionTrace.action}`)
    }
    if (actionFailure.failure_code) {
      lines.push(`Browser action failure: ${actionFailure.failure_code}`)
    }
    if (actionFailure.failure_category) {
      lines.push(`Browser action failure category: ${actionFailure.failure_category}`)
    }
    if (actionIssue.recommended_action && actionIssue.recommended_action !== 'continue') {
      lines.push(`Browser action recommendation: ${actionIssue.recommended_action}`)
    }
    if (actionIssues.length) {
      const actionIssueCodes = actionIssues
        .slice(0, 5)
        .map((issue) => `${issue.source || 'action'}:${issue.code || 'issue'}`)
      lines.push(`Browser action issue codes: ${actionIssueCodes.join(', ')}`)
    }
    if (actionActions.length) {
      lines.push(`Browser action recommendations: ${actionActions.join(', ')}`)
    }
    if (actionRecoveryActions.length) {
      lines.push(`Browser action recovery actions: ${actionRecoveryActions.join(', ')}`)
    }
    if (alignment.fallbackReason) lines.push(`Fallback reason: ${alignment.fallbackReason}`)
    if (health.alignment) lines.push(`Alignment: ${health.alignment}`)
    return `${lines.join('\n')}\n`
  }

  const copyCapabilityTraceSummary = async () => {
    if (!capabilityTraceEvents.value.length) {
      ElMessage.warning('当前没有可复制的 capability trace 摘要')
      return
    }
    const ok = await writeToClipboard(buildCapabilityTraceSummaryText())
    if (ok) {
      ElMessage.success('已复制 capability trace 摘要')
    }
  }

  return {
    exportCapabilityTraceAsJsonl,
    buildCapabilityTraceSummaryText,
    copyCapabilityTraceSummary,
  }
}
