<script setup>
const props = defineProps({
  crawlPlan: {
    type: Object,
    default: () => ({}),
  },
  availablePaths: {
    type: Array,
    default: () => [],
  },
  candidates: {
    type: Array,
    default: () => [],
  },
  summary: {
    type: Object,
    default: () => ({}),
  },
  candidateClass: {
    type: Function,
    default: (item) => (item?.available ? 'is-available' : 'is-unavailable'),
  },
  candidateEvidence: {
    type: Function,
    default: () => '',
  },
  correlationReport: {
    type: Object,
    default: () => ({}),
  },
  correlationStatusClass: {
    type: String,
    default: '',
  },
  correlationAlignment: {
    type: Object,
    default: () => ({}),
  },
  rootCauses: {
    type: Array,
    default: () => [],
  },
  plannerHints: {
    type: Array,
    default: () => [],
  },
  actions: {
    type: Array,
    default: () => [],
  },
})
</script>

<template>
  <div v-if="props.crawlPlan.version" class="capability-crawl-efficiency-card">
    <div class="capability-crawl-efficiency-head">
      <strong>Crawl Efficiency</strong>
      <span>path: {{ props.crawlPlan.recommended_path || 'unknown' }}</span>
      <span>skip browser: {{ props.crawlPlan.skip_browser ? 'yes' : 'no' }}</span>
      <span>skip VLM: {{ props.crawlPlan.skip_vlm ? 'yes' : 'no' }}</span>
    </div>
    <div v-if="props.availablePaths.length" class="capability-crawl-efficiency-paths">
      <span
        v-for="path in props.availablePaths"
        :key="`crawl-efficiency-path-${path}`"
        class="capability-check is-complete"
      >
        {{ path }}
      </span>
    </div>
    <div class="capability-crawl-efficiency-candidates">
      <div
        v-for="candidate in props.candidates"
        :key="`crawl-efficiency-candidate-${candidate.name}`"
        class="capability-crawl-efficiency-candidate"
        :class="props.candidateClass(candidate)"
      >
        <div class="capability-card-head">
          <strong>{{ candidate.name || 'unknown' }}</strong>
          <span>{{ candidate.available ? 'available' : 'unavailable' }}</span>
          <span>score {{ candidate.score ?? 0 }}</span>
        </div>
        <p v-if="candidate.reason" class="capability-detail">
          {{ candidate.reason }}
        </p>
        <p v-if="props.candidateEvidence(candidate)" class="capability-meta">
          {{ props.candidateEvidence(candidate) }}
        </p>
      </div>
    </div>
    <p v-if="props.summary.runtime_status" class="capability-meta">
      runtime={{ props.summary.runtime_status }}
      ? interactive={{ props.summary.interactive_count ?? 0 }}
      ? network={{ props.summary.network_candidate_count ?? 0 }}
    </p>
  </div>

  <div v-if="props.correlationReport.version" class="capability-efficiency-correlation-card">
    <div class="capability-efficiency-correlation-head">
      <strong>Efficiency Correlation</strong>
      <span
        class="capability-check"
        :class="props.correlationStatusClass"
      >
        {{ props.correlationReport.status || 'unknown' }}
      </span>
      <span>recommended: {{ props.correlationAlignment.recommended_path || 'unknown' }}</span>
      <span>executed: {{ props.correlationAlignment.executed_path || 'unknown' }}</span>
      <span v-if="props.correlationAlignment.rank_gap != null">
        rank delta {{ props.correlationAlignment.rank_gap }}
      </span>
    </div>
    <div v-if="props.rootCauses.length" class="capability-check-list">
      <span
        v-for="cause in props.rootCauses"
        :key="`efficiency-cause-${cause}`"
        class="capability-check is-warning"
      >
        cause: {{ cause }}
      </span>
    </div>
    <div v-if="props.plannerHints.length" class="capability-efficiency-hints">
      <p
        v-for="(hint, idx) in props.plannerHints.slice(0, 4)"
        :key="`efficiency-hint-${idx}-${hint.kind || idx}`"
        class="capability-detail"
      >
        {{ hint.kind || 'hint' }}: {{ hint.message || '' }}
      </p>
    </div>
    <div v-if="props.actions.length" class="capability-check-list">
      <span
        v-for="action in props.actions"
        :key="`efficiency-action-${action}`"
        class="capability-check is-complete"
      >
        action: {{ action }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.capability-efficiency-correlation-card,
.capability-crawl-efficiency-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 9px 10px;
  border-radius: 8px;
  background: rgba(15, 23, 42, 0.72);
  border: 1px solid rgba(129, 140, 248, 0.24);
}

.capability-efficiency-correlation-head,
.capability-crawl-efficiency-head,
.capability-crawl-efficiency-paths {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  color: #cbd5e1;
  font-size: 12px;
}

.capability-efficiency-correlation-head strong,
.capability-crawl-efficiency-head strong {
  color: #c7d2fe;
}

.capability-efficiency-hints,
.capability-crawl-efficiency-candidates {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 8px;
}

.capability-crawl-efficiency-candidate {
  padding: 8px 9px;
  border-radius: 8px;
  background: rgba(2, 6, 23, 0.42);
  border: 1px solid rgba(148, 163, 184, 0.14);
}

.capability-crawl-efficiency-candidate.is-available {
  border-color: rgba(34, 197, 94, 0.28);
}

.capability-crawl-efficiency-candidate.is-unavailable {
  opacity: 0.72;
}

.capability-card-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.capability-card-head strong {
  color: #93c5fd;
  font-size: 13px;
}

.capability-meta,
.capability-detail {
  margin: 5px 0 0;
  color: #94a3b8;
  font-size: 12px;
  line-height: 1.45;
}

.capability-detail {
  color: #cbd5e1;
}

.capability-check-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.capability-check {
  padding: 3px 8px;
  border-radius: 999px;
  border: 1px solid rgba(148, 163, 184, 0.22);
  background: rgba(148, 163, 184, 0.08);
  font-size: 11.5px;
  font-weight: 700;
}

.capability-check.is-complete {
  color: #86efac;
  background: rgba(34, 197, 94, 0.12);
  border-color: rgba(34, 197, 94, 0.35);
}

.capability-check.is-warning,
.capability-check.is-skip {
  color: #fde68a;
  background: rgba(251, 191, 36, 0.12);
  border-color: rgba(251, 191, 36, 0.35);
}

.capability-check.is-error {
  color: #fecaca;
  background: rgba(239, 68, 68, 0.14);
  border-color: rgba(239, 68, 68, 0.4);
}
</style>
