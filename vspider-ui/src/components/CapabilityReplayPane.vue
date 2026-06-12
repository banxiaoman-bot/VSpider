<script setup>
import CapabilityStatusBadge from './CapabilityStatusBadge.vue'

defineProps({
  /* { report, status, plannerFeedback, preferredCapabilities, avoidActions,
       artifact, checks, failedChecks } */
  efficiencyReplay: { type: Object, default: () => ({}) },
  /* { items, count, loading } */
  efficiencyLibrary: { type: Object, default: () => ({}) },
  /* { report, status, artifact, checks, failedChecks } */
  fixtureReplay: { type: Object, default: () => ({}) },
  /* { items, count, loading } */
  fixtureLibrary: { type: Object, default: () => ({}) },
  /* { items, count, loading, latest, trend, trendDirection, trendPassRateDelta } */
  batchHistory: { type: Object, default: () => ({}) },
  /* { report, status, summary, topPrimaryFailures, topFailureCategories,
       topFailedChecks, artifact, failedChecks, items } */
  batchReplay: { type: Object, default: () => ({}) },
})

defineEmits(['copy-batch-summary'])
</script>

<template>
  <div class="capability-replay-pane">
    <section v-if="efficiencyReplay.report" class="capability-efficiency-feedback-replay-card">
      <div class="capability-section-head">
        <h4>Efficiency Feedback Replay</h4>
        <CapabilityStatusBadge
          :status-class="efficiencyReplay.report.passed ? 'ok' : 'error'"
          :label="efficiencyReplay.status"
        />
      </div>
      <div class="capability-fixture-replay-grid">
        <span>failure</span>
        <strong>{{ efficiencyReplay.plannerFeedback?.primary_failure || 'unknown' }}</strong>
        <span>action</span>
        <strong>{{ efficiencyReplay.plannerFeedback?.recommended_action || 'review' }}</strong>
        <span>route</span>
        <strong>{{ efficiencyReplay.report.route?.passed ? 'passed' : 'failed' }}</strong>
        <span>plan</span>
        <strong>{{ efficiencyReplay.report.execution_plan?.passed ? 'passed' : 'failed' }}</strong>
        <span>step</span>
        <strong>{{ efficiencyReplay.report.execution_plan?.feedback_step?.capability || 'none' }}</strong>
      </div>
      <div v-if="efficiencyReplay.preferredCapabilities?.length" class="capability-check-list">
        <span
          v-for="capability in efficiencyReplay.preferredCapabilities"
          :key="`efficiency-feedback-prefer-${capability}`"
          class="capability-check is-complete"
        >
          prefer: {{ capability }}
        </span>
      </div>
      <div v-if="efficiencyReplay.avoidActions?.length" class="capability-check-list">
        <span
          v-for="action in efficiencyReplay.avoidActions.slice(0, 6)"
          :key="`efficiency-feedback-avoid-${action}`"
          class="capability-check is-warning"
        >
          avoid: {{ action }}
        </span>
      </div>
      <div v-if="efficiencyReplay.artifact?.url" class="capability-fixture-replay-artifact">
        artifact:
        <a :href="efficiencyReplay.artifact.url" target="_blank" rel="noreferrer">
          {{ efficiencyReplay.artifact.url }}
        </a>
      </div>
      <div class="capability-fixture-replay-checks">
        <span>
          checks {{ (efficiencyReplay.checks?.length || 0) - (efficiencyReplay.failedChecks?.length || 0) }}/{{ efficiencyReplay.checks?.length || 0 }}
        </span>
        <span v-if="efficiencyReplay.failedChecks?.length">
          failed {{ efficiencyReplay.failedChecks.length }}
        </span>
      </div>
    </section>

    <section class="capability-efficiency-feedback-library-card">
      <div class="capability-section-head">
        <h4>Efficiency Feedback Replay Library</h4>
        <CapabilityStatusBadge
          status-class="route-only"
          :label="`${efficiencyLibrary.count || 0} reports`"
        />
      </div>
      <div class="capability-fixture-library-actions">
        <span>source capability/efficiency_feedback_replays</span>
        <span v-if="efficiencyLibrary.loading">loading</span>
      </div>
      <div v-if="efficiencyLibrary.items?.length" class="capability-efficiency-feedback-library-list">
        <div
          v-for="item in efficiencyLibrary.items.slice(0, 5)"
          :key="item.path || item.name"
          class="capability-efficiency-feedback-library-item"
        >
          <strong>{{ item.primary_failure || item.name || 'efficiency feedback replay' }}</strong>
          <span>{{ item.passed ? 'passed' : 'failed' }} · {{ item.recommended_action || 'review' }}</span>
          <span v-if="Array.isArray(item.preferred_capabilities) && item.preferred_capabilities.length">
            prefer {{ item.preferred_capabilities.slice(0, 3).join(', ') }}
          </span>
          <span v-if="item.feedback_step">step {{ item.feedback_step }}</span>
          <a v-if="item.url" :href="item.url" target="_blank" rel="noreferrer">artifact</a>
        </div>
      </div>
      <p v-else class="capability-fixture-library-empty">
        暂无 efficiency feedback replay artifacts
      </p>
    </section>

    <section v-if="fixtureReplay.report" class="capability-fixture-replay-card">
      <div class="capability-section-head">
        <h4>Failure Fixture Replay</h4>
        <CapabilityStatusBadge
          :status-class="fixtureReplay.report.passed ? 'ok' : 'error'"
          :label="fixtureReplay.status"
        />
      </div>
      <div class="capability-fixture-replay-grid">
        <span>failure</span>
        <strong>{{ fixtureReplay.report.fixture?.primary_failure || 'unknown' }}</strong>
        <span>planner</span>
        <strong>{{ fixtureReplay.report.planner_feedback?.passed ? 'passed' : 'failed' }}</strong>
        <span>route</span>
        <strong>{{ fixtureReplay.report.route?.passed ? 'passed' : 'failed' }}</strong>
        <span>plan</span>
        <strong>{{ fixtureReplay.report.execution_plan?.passed ? 'passed' : 'failed' }}</strong>
      </div>
      <div v-if="fixtureReplay.artifact?.url" class="capability-fixture-replay-artifact">
        artifact:
        <a :href="fixtureReplay.artifact.url" target="_blank" rel="noreferrer">
          {{ fixtureReplay.artifact.url }}
        </a>
      </div>
      <div class="capability-fixture-replay-checks">
        <span>
          checks {{ (fixtureReplay.checks?.length || 0) - (fixtureReplay.failedChecks?.length || 0) }}/{{ fixtureReplay.checks?.length || 0 }}
        </span>
        <span v-if="fixtureReplay.failedChecks?.length">
          failed {{ fixtureReplay.failedChecks.length }}
        </span>
      </div>
      <ul class="capability-fixture-replay-check-list">
        <li
          v-for="check in (fixtureReplay.checks || []).slice(0, 6)"
          :key="check.name"
          :class="check.passed ? 'is-ok' : 'is-error'"
        >
          <span>{{ check.passed ? '✓' : '×' }}</span>
          <code>{{ check.name }}</code>
        </li>
      </ul>
    </section>

    <section class="capability-fixture-library-card">
      <div class="capability-section-head">
        <h4>Failure Fixture Library</h4>
        <CapabilityStatusBadge
          status-class="route-only"
          :label="`${fixtureLibrary.count || 0} fixtures`"
        />
      </div>
      <div class="capability-fixture-library-actions">
        <span>source capability/failure_fixtures</span>
        <span v-if="fixtureLibrary.loading">loading</span>
      </div>
      <div v-if="fixtureLibrary.items?.length" class="capability-fixture-library-list">
        <div
          v-for="item in fixtureLibrary.items.slice(0, 5)"
          :key="item.path || item.name"
          class="capability-fixture-library-item"
        >
          <strong>{{ item.name || 'fixture' }}</strong>
          <span>{{ item.primary_failure || 'unknown' }}</span>
          <span>{{ item.action || 'action' }}</span>
          <a v-if="item.url" :href="item.url" target="_blank" rel="noreferrer">artifact</a>
        </div>
      </div>
      <div v-else class="capability-fixture-library-empty">
        暂无已加载 fixture，点击“刷新 Fixture 库”读取 artifact library。
      </div>
    </section>

    <section class="capability-fixture-history-card">
      <div class="capability-section-head">
        <h4>Failure Fixture Batch History</h4>
        <CapabilityStatusBadge
          status-class="route-only"
          :label="`${batchHistory.count || 0} reports`"
        />
      </div>
      <div class="capability-fixture-library-actions">
        <span>source capability/failure_fixture_replay_batches</span>
        <span v-if="batchHistory.loading">loading</span>
        <span v-if="batchHistory.latest">
          latest {{ batchHistory.latest.status || 'unknown' }}
          · failed {{ batchHistory.latest.failed_count || 0 }}
        </span>
      </div>
      <div
        v-if="batchHistory.trend"
        class="capability-fixture-history-trend"
        :class="`is-${batchHistory.trendDirection}`"
      >
        <strong>trend {{ batchHistory.trendDirection }}</strong>
        <span>
          failed Δ {{ batchHistory.trend.failed_count_delta || 0 }}
          · pass rate Δ {{ batchHistory.trendPassRateDelta }}%
        </span>
        <small v-if="batchHistory.trend.focus_changed">
          focus changed {{ batchHistory.trend.previous_recommended_focus || 'none' }}
          → {{ batchHistory.trend.latest_recommended_focus || 'none' }}
        </small>
      </div>
      <div v-if="batchHistory.items?.length" class="capability-fixture-history-list">
        <div
          v-for="item in batchHistory.items.slice(0, 5)"
          :key="item.path || item.name"
          class="capability-fixture-history-item"
          :class="item.passed ? 'is-ok' : 'is-error'"
        >
          <strong>{{ item.name || 'batch replay' }}</strong>
          <span>{{ item.status || (item.passed ? 'passed' : 'failed') }}</span>
          <span>{{ item.passed_count || 0 }}/{{ item.fixture_count || 0 }} passed · failed {{ item.failed_count || 0 }}</span>
          <small v-if="item.recommended_focus">{{ item.recommended_focus }}</small>
          <a v-if="item.url" :href="item.url" target="_blank" rel="noreferrer">artifact</a>
        </div>
      </div>
      <div v-else class="capability-fixture-library-empty">
        暂无已加载 batch replay history，点击“刷新 Replay 历史”读取 report artifacts。
      </div>
    </section>

    <section v-if="batchReplay.report" class="capability-fixture-replay-card capability-fixture-batch-card">
      <div class="capability-section-head">
        <h4>Failure Fixture Batch Replay</h4>
        <div class="capability-fixture-batch-head-actions">
          <el-button
            size="small"
            plain
            class="capability-export-btn"
            title="复制 failure fixture batch replay Markdown 摘要"
            @click="$emit('copy-batch-summary')"
          >
            复制 Replay 摘要
          </el-button>
          <CapabilityStatusBadge
            :status-class="batchReplay.report.passed ? 'ok' : 'error'"
            :label="batchReplay.status"
          />
        </div>
      </div>
      <div class="capability-fixture-replay-grid">
        <span>fixtures</span>
        <strong>{{ batchReplay.report.fixture_count || 0 }}</strong>
        <span>passed</span>
        <strong>{{ batchReplay.report.passed_count || 0 }}</strong>
        <span>failed</span>
        <strong>{{ batchReplay.report.failed_count || 0 }}</strong>
        <span>failed checks</span>
        <strong>{{ batchReplay.failedChecks?.length || 0 }}</strong>
      </div>
      <div v-if="batchReplay.summary?.status" class="capability-fixture-triage">
        <div v-if="batchReplay.summary.recommended_focus" class="capability-fixture-triage-focus">
          focus {{ batchReplay.summary.recommended_focus }}
        </div>
        <div class="capability-fixture-triage-grid">
          <div>
            <span>primary</span>
            <strong>{{ batchReplay.topPrimaryFailures?.[0]?.name || 'none' }}</strong>
            <small>{{ batchReplay.topPrimaryFailures?.[0]?.count || 0 }}</small>
          </div>
          <div>
            <span>category</span>
            <strong>{{ batchReplay.topFailureCategories?.[0]?.name || 'none' }}</strong>
            <small>{{ batchReplay.topFailureCategories?.[0]?.count || 0 }}</small>
          </div>
          <div>
            <span>check</span>
            <strong>{{ batchReplay.topFailedChecks?.[0]?.name || 'none' }}</strong>
            <small>{{ batchReplay.topFailedChecks?.[0]?.failed_count || 0 }}</small>
          </div>
        </div>
      </div>
      <div v-if="batchReplay.artifact?.url" class="capability-fixture-replay-artifact">
        artifact:
        <a :href="batchReplay.artifact.url" target="_blank" rel="noreferrer">
          {{ batchReplay.artifact.url }}
        </a>
      </div>
      <ul v-if="batchReplay.failedChecks?.length" class="capability-fixture-replay-check-list">
        <li
          v-for="check in batchReplay.failedChecks.slice(0, 6)"
          :key="check.name"
          class="is-error"
        >
          <span>×</span>
          <code>{{ check.name }}</code>
          <span>{{ check.failed_count }}</span>
        </li>
      </ul>
      <div class="capability-fixture-batch-list">
        <div
          v-for="item in (batchReplay.items || []).slice(0, 6)"
          :key="`${item.index}-${item.name}`"
          class="capability-fixture-batch-item"
          :class="item.passed ? 'is-ok' : 'is-error'"
        >
          <span>{{ item.passed ? '✓' : '×' }}</span>
          <strong>{{ item.name || 'fixture' }}</strong>
          <small>{{ item.primary_failure || 'unknown' }} · failed {{ item.failed_check_count || 0 }}</small>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.capability-replay-pane {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
</style>
