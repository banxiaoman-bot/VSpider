<script setup>
import {
  capabilityItemDetail,
  capabilityItemName,
} from './capabilityTraceUtils.js'

defineProps({
  manifestSummary: { type: Array, default: () => [] },
  fallbackChain: { type: Array, default: () => [] },
  roleRows: { type: Array, default: () => [] },
  auditFindings: { type: Array, default: () => [] },
  rawJson: { type: String, default: '' },
})
</script>

<template>
  <div class="capability-diagnostics-pane">
    <section v-if="manifestSummary.length" class="capability-section">
      <h4>能力清单摘要</h4>
      <div class="capability-fallback">
        <span
          v-for="item in manifestSummary"
          :key="`manifest-${item.name}`"
          class="capability-fallback-pill"
          :title="`${item.layer || ''} · ${item.owner || ''}`"
        >
          {{ item.name }} · {{ item.layer || item.category || 'capability' }}
        </span>
      </div>
    </section>

    <section class="capability-section">
      <h4>兜底顺序</h4>
      <div class="capability-fallback">
        <span
          v-for="(item, idx) in fallbackChain"
          :key="`fallback-${idx}-${capabilityItemName(item)}`"
          class="capability-fallback-pill"
          :title="capabilityItemDetail(item)"
        >
          {{ idx + 1 }}. {{ capabilityItemName(item) }}
        </span>
      </div>
    </section>

    <section class="capability-section">
      <h4>模型职责边界</h4>
      <div class="capability-role-grid">
        <article
          v-for="role in roleRows"
          :key="role.key"
          class="capability-role-card"
        >
          <header>
            <strong>{{ role.key }}</strong>
            <span v-if="role.recommendedUse">{{ role.recommendedUse }}</span>
          </header>
          <p v-if="role.position" class="capability-meta">{{ role.position }}</p>
          <ul v-if="role.responsibilities.length">
            <li v-for="item in role.responsibilities" :key="`${role.key}-r-${item}`">
              {{ item }}
            </li>
          </ul>
          <p v-if="role.shouldNotDo.length" class="capability-avoid">
            avoid: {{ role.shouldNotDo.join(', ') }}
          </p>
        </article>
      </div>
    </section>

    <section v-if="auditFindings.length" class="capability-section">
      <h4>审计发现</h4>
      <div class="capability-audit-list">
        <div
          v-for="(finding, idx) in auditFindings"
          :key="`finding-${idx}-${finding.area || idx}`"
          class="capability-audit-item"
        >
          <strong>{{ finding.area || 'area' }}</strong>
          <span>{{ finding.status || 'unknown' }}</span>
          <p>{{ finding.detail || '' }}</p>
        </div>
      </div>
    </section>

    <section class="capability-section">
      <h4>原始事件</h4>
      <pre class="capability-json"><code>{{ rawJson }}</code></pre>
    </section>
  </div>
</template>

<style scoped>
.capability-diagnostics-pane {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
</style>
