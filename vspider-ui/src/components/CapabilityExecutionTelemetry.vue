<script setup>
import { computed } from 'vue'
import {
  capabilityAttemptClass,
  capabilityCrawlEfficiencyCandidateClass,
  capabilityCrawlEfficiencyEvidence,
  capabilityEventActionIssueSummary,
  capabilityEventActionTrace,
  capabilityEventCrawlEfficiencyPlan,
  capabilityEventEfficiencyCorrelationReport,
  capabilityEventFailureBundle,
} from './capabilityTraceUtils'
import CapabilityEfficiencyPanel from './CapabilityEfficiencyPanel.vue'

const props = defineProps({
  executeEvent: { type: Object, default: () => ({}) },
  routeCrawlEfficiencyPlan: { type: Object, default: () => ({}) },
})

const exe = computed(() => props.executeEvent || {})

const attempts = computed(() =>
  Array.isArray(exe.value.attempts) ? exe.value.attempts : [],
)
const checks = computed(() =>
  Array.isArray(exe.value.verification?.checks) ? exe.value.verification.checks : [],
)
const runtimeAfter = computed(() =>
  (exe.value.runtime_summary || {}).after || {},
)
const runtimeDrift = computed(() => exe.value.runtime_drift || {})
const runtimeIssueSummary = computed(() => exe.value.runtime_issue_summary || {})
const runtimeIssues = computed(() =>
  Array.isArray(runtimeIssueSummary.value.issues) ? runtimeIssueSummary.value.issues : [],
)
const runtimeActions = computed(() =>
  Array.isArray(runtimeIssueSummary.value.recommended_actions)
    ? runtimeIssueSummary.value.recommended_actions
        .map((a) => String(a || '')).filter((a) => a && a !== 'continue')
    : [],
)
const actionTrace = computed(() => capabilityEventActionTrace(exe.value))
const actionIssueSummary = computed(() =>
  capabilityEventActionIssueSummary(exe.value, actionTrace.value),
)
const actionIssues = computed(() =>
  Array.isArray(actionIssueSummary.value.issues) ? actionIssueSummary.value.issues : [],
)
const actionIssueActions = computed(() =>
  Array.isArray(actionIssueSummary.value.recommended_actions)
    ? actionIssueSummary.value.recommended_actions
        .map((a) => String(a || '')).filter((a) => a && a !== 'continue')
    : [],
)
const actionFailureSummary = computed(() => actionTrace.value?.result_summary || {})
const actionRecoveryActions = computed(() =>
  Array.isArray(actionFailureSummary.value.recovery_actions)
    ? actionFailureSummary.value.recovery_actions
        .map((a) => String(a || '')).filter((a) => a && a !== 'continue')
    : [],
)

const executionCrawlPlan = computed(() => capabilityEventCrawlEfficiencyPlan(exe.value))
const activeCrawlPlan = computed(() =>
  executionCrawlPlan.value?.version ? executionCrawlPlan.value : props.routeCrawlEfficiencyPlan,
)
const crawlCandidates = computed(() =>
  Array.isArray(activeCrawlPlan.value?.candidates) ? activeCrawlPlan.value.candidates : [],
)
const crawlAvailablePaths = computed(() =>
  Array.isArray(activeCrawlPlan.value?.available_paths)
    ? activeCrawlPlan.value.available_paths.map((i) => String(i || '')).filter(Boolean)
    : [],
)
const crawlSummary = computed(() => activeCrawlPlan.value?.efficiency_summary || {})
const correlationReport = computed(() => capabilityEventEfficiencyCorrelationReport(exe.value))
const correlationAlignment = computed(() => correlationReport.value?.alignment || {})
const correlationRootCauses = computed(() =>
  Array.isArray(correlationReport.value?.root_causes) ? correlationReport.value.root_causes : [],
)
const correlationPlannerHints = computed(() =>
  Array.isArray(correlationReport.value?.planner_hints) ? correlationReport.value.planner_hints : [],
)
const correlationActions = computed(() =>
  Array.isArray(correlationReport.value?.recommended_actions)
    ? correlationReport.value.recommended_actions.map((a) => String(a || '')).filter(Boolean)
    : [],
)
const correlationStatusClass = computed(() => {
  const s = String(correlationReport.value?.status || 'unknown')
  if (s === 'aligned') return 'is-complete'
  if (s === 'suboptimal' || s === 'needs_repair') return 'is-warning'
  if (s === 'needs_replan') return 'is-error'
  return 'is-skip'
})

function statusLabel(kind, statusMap) {
  const s = String(statusMap.value || 'unknown')
  const map = {
    runtime: { pass: 'runtime ok', warn: 'runtime warn', block: 'runtime block' },
    drift: { stable: 'drift stable', changed: 'drift changed', warn: 'drift warn' },
    issue: { ok: 'runtime ok', watch: 'runtime watch', warn: 'runtime issues' },
    action: { ok: 'action ok', warn: 'action issues', error: 'action error' },
  }
  return (map[kind] || {})[s] || `${kind} unknown`
}

const runtimeLabel = computed(() =>
  statusLabel('runtime', computed(() => runtimeAfter.value.preflight_status)),
)
const driftLabel = computed(() =>
  statusLabel('drift', computed(() => runtimeDrift.value.status)),
)
const issueLabel = computed(() =>
  statusLabel('issue', computed(() => runtimeIssueSummary.value.status)),
)
const actionIssueLabel = computed(() =>
  statusLabel('action', computed(() => actionIssueSummary.value.status)),
)
</script>

<template>
  <section v-if="exe.capability || exe.execution_status" class="capability-section">
    <h4>执行遥测</h4>

    <div class="capability-exec-summary">
      <span
        class="capability-exec-status"
        :class="exe.completed ? 'is-complete' : 'is-fallback'"
      >
        {{ exe.execution_status || 'unknown' }}
      </span>
      <span v-if="exe.capability">capability: {{ exe.capability }}</span>
      <span v-if="Number.isFinite(exe.duration_ms)">{{ exe.duration_ms }}ms</span>
      <span v-if="exe.fallback_reason">fallback: {{ exe.fallback_reason }}</span>
    </div>

    <CapabilityEfficiencyPanel
      :crawl-plan="activeCrawlPlan"
      :available-paths="crawlAvailablePaths"
      :candidates="crawlCandidates"
      :summary="crawlSummary"
      :candidate-class="capabilityCrawlEfficiencyCandidateClass"
      :candidate-evidence="capabilityCrawlEfficiencyEvidence"
      :correlation-report="correlationReport"
      :correlation-status-class="correlationStatusClass"
      :correlation-alignment="correlationAlignment"
      :root-causes="correlationRootCauses"
      :planner-hints="correlationPlannerHints"
      :actions="correlationActions"
    />

    <div v-if="runtimeAfter.runtime_status" class="capability-exec-summary">
      <span>{{ runtimeLabel }}</span>
      <span>runtime: {{ runtimeAfter.runtime_status || 'unknown' }}</span>
      <span>backend: {{ runtimeAfter.active_backend || 'unknown' }}</span>
      <span>health: {{ runtimeAfter.backend_health || 'unknown' }}</span>
      <span>
        contexts: {{ runtimeAfter.available_contexts ?? '?' }}
        / {{ runtimeAfter.max_contexts ?? '?' }}
      </span>
      <span v-if="runtimeAfter.recommended_action">
        action: {{ runtimeAfter.recommended_action }}
      </span>
    </div>

    <div v-if="runtimeDrift.version" class="capability-exec-summary">
      <span>{{ driftLabel }}</span>
      <span>changes: {{ Array.isArray(runtimeDrift.changes) ? runtimeDrift.changes.length : 0 }}</span>
      <span>warnings: {{ Array.isArray(runtimeDrift.warnings) ? runtimeDrift.warnings.length : 0 }}</span>
      <span v-if="runtimeDrift.deltas">
        contexts &Delta;: {{ runtimeDrift.deltas.available_contexts ?? 0 }}
      </span>
      <span v-if="runtimeDrift.recommended_action">
        action: {{ runtimeDrift.recommended_action }}
      </span>
    </div>

    <div v-if="runtimeIssueSummary.version" class="capability-exec-summary">
      <span>{{ issueLabel }}</span>
      <span>issues: {{ runtimeIssueSummary.issue_count ?? 0 }}</span>
      <span v-if="runtimeIssueSummary.sources">
        drift: {{ runtimeIssueSummary.sources.drift_status || 'unknown' }}
      </span>
      <span v-if="runtimeIssueSummary.recommended_action">
        action: {{ runtimeIssueSummary.recommended_action }}
      </span>
    </div>

    <div v-if="runtimeIssues.length" class="capability-check-list">
      <span
        v-for="(issue, idx) in runtimeIssues"
        :key="`runtime-issue-${idx}-${issue.code || idx}`"
        class="capability-check is-error"
      >
        {{ issue.source || 'runtime' }}: {{ issue.code || 'issue' }}
      </span>
    </div>

    <div v-if="runtimeActions.length" class="capability-check-list">
      <span
        v-for="action in runtimeActions"
        :key="`runtime-action-${action}`"
        class="capability-check is-complete"
      >
        action: {{ action }}
      </span>
    </div>

    <div v-if="actionIssueSummary.version" class="capability-exec-summary">
      <span>{{ actionIssueLabel }}</span>
      <span v-if="actionTrace.action">
        browser action: {{ actionTrace.action }}
      </span>
      <span v-if="actionFailureSummary.failure_code">
        failure: {{ actionFailureSummary.failure_code }}
      </span>
      <span v-if="actionFailureSummary.failure_category">
        category: {{ actionFailureSummary.failure_category }}
      </span>
      <span>issues: {{ actionIssueSummary.issue_count ?? 0 }}</span>
      <span v-if="actionIssueSummary.recommended_action">
        action: {{ actionIssueSummary.recommended_action }}
      </span>
    </div>

    <div v-if="actionIssues.length" class="capability-check-list">
      <span
        v-for="(issue, idx) in actionIssues"
        :key="`browser-action-issue-${idx}-${issue.code || idx}`"
        class="capability-check is-error"
      >
        {{ issue.source || 'action' }}: {{ issue.code || 'issue' }}
      </span>
    </div>

    <div v-if="actionIssueActions.length" class="capability-check-list">
      <span
        v-for="action in actionIssueActions"
        :key="`browser-action-recommendation-${action}`"
        class="capability-check is-complete"
      >
        action: {{ action }}
      </span>
    </div>

    <div v-if="actionRecoveryActions.length" class="capability-check-list">
      <span
        v-for="action in actionRecoveryActions"
        :key="`browser-action-recovery-${action}`"
        class="capability-check is-warning"
      >
        recovery: {{ action }}
      </span>
    </div>

    <div v-if="attempts.length" class="capability-attempt-list">
      <div
        v-for="(attempt, idx) in attempts"
        :key="`attempt-${idx}-${attempt.capability || idx}`"
        class="capability-attempt"
        :class="capabilityAttemptClass(attempt)"
      >
        <strong>{{ attempt.capability || 'unknown' }}</strong>
        <span>{{ attempt.status || 'attempted' }}</span>
        <small v-if="attempt.target_count != null">target {{ attempt.target_count }}</small>
        <small v-if="attempt.count != null">count {{ attempt.count }}</small>
        <small v-if="attempt.row_count != null">rows {{ attempt.row_count }}</small>
        <small v-if="attempt.item_count != null">items {{ attempt.item_count }}</small>
        <p v-if="attempt.reason">{{ attempt.reason }}</p>
      </div>
    </div>

    <div v-if="checks.length" class="capability-check-list">
      <span
        v-for="check in checks"
        :key="check.name"
        class="capability-check"
        :class="check.passed ? 'is-complete' : 'is-error'"
      >
        {{ check.name }}: {{ check.passed ? 'pass' : 'fail' }}
      </span>
    </div>
  </section>
</template>
