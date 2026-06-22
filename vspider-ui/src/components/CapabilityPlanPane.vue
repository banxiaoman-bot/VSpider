<script setup>
import {
  capabilityItemDetail,
  capabilityItemMeta,
  capabilityItemName,
} from './capabilityTraceUtils.js'

defineProps({
  planSteps: { type: Array, default: () => [] },
  workflowGraph: { type: Object, default: () => ({}) },
  workflowNodes: { type: Array, default: () => [] },
  actionRefSchema: { type: Object, default: () => ({}) },
  backendPlan: { type: Array, default: () => [] },
})
</script>

<template>
  <div class="capability-plan-pane">
    <section v-if="planSteps.length" class="capability-section">
      <h4>结构化执行计划</h4>
      <div class="capability-chain">
        <div
          v-for="step in planSteps"
          :key="step.id || `plan-step-${step.order}-${step.capability}`"
          class="capability-card"
        >
          <div class="capability-card-head">
            <span class="capability-rank">{{ step.order || '?' }}</span>
            <strong>{{ step.capability || 'unknown' }}</strong>
          </div>
          <p class="capability-meta">
            {{ step.owner || 'owner?' }} · risk={{ step.risk || 'unknown' }}
            <span v-if="step.deterministic === false"> · model</span>
            <span v-else> · deterministic</span>
          </p>
          <p v-if="step.purpose" class="capability-detail">
            {{ step.purpose }}
          </p>
        </div>
      </div>
    </section>

    <section v-if="workflowNodes.length" class="capability-section">
      <h4>跨系统工作流图</h4>
      <div class="capability-exec-summary">
        <span>{{ workflowGraph.version || 'workflow_graph' }}</span>
        <span>systems {{ workflowGraph.systems?.length || 0 }}</span>
        <span>sessions {{ workflowGraph.sessions?.length || 0 }}</span>
        <span>nodes {{ workflowNodes.length }}</span>
        <span>edges {{ workflowGraph.data_edges?.length || 0 }}</span>
      </div>
      <div class="capability-chain">
        <div
          v-for="node in workflowNodes.slice(0, 8)"
          :key="node.id || `workflow-${node.order}-${node.capability}`"
          class="capability-card"
        >
          <div class="capability-card-head">
            <span class="capability-rank">{{ node.order || '?' }}</span>
            <strong>{{ node.capability || 'unknown' }}</strong>
          </div>
          <p class="capability-meta">
            {{ node.system_id || 'system?' }} · {{ node.session_id || 'session?' }}
          </p>
          <p v-if="node.purpose" class="capability-detail">
            {{ node.purpose }}
          </p>
        </div>
      </div>
    </section>

    <section v-if="actionRefSchema.version" class="capability-section">
      <h4>Unified ActionRef</h4>
      <div class="capability-exec-summary">
        <span>{{ actionRefSchema.version }}</span>
        <span
          v-for="source in (actionRefSchema.preferred_sources || [])"
          :key="`action-ref-source-${source}`"
        >
          {{ source }}
        </span>
      </div>
      <p
        v-if="actionRefSchema.notes?.length"
        class="capability-detail"
      >
        {{ actionRefSchema.notes.join(' · ') }}
      </p>
    </section>

    <section class="capability-section">
      <h4>推荐能力链</h4>
      <div class="capability-chain">
        <div
          v-for="(item, idx) in backendPlan"
          :key="`plan-${idx}-${capabilityItemName(item)}`"
          class="capability-card"
        >
          <div class="capability-card-head">
            <span class="capability-rank">{{ idx + 1 }}</span>
            <strong>{{ capabilityItemName(item) }}</strong>
          </div>
          <p v-if="capabilityItemMeta(item)" class="capability-meta">
            {{ capabilityItemMeta(item) }}
          </p>
          <p v-if="capabilityItemDetail(item)" class="capability-detail">
            {{ capabilityItemDetail(item) }}
          </p>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.capability-plan-pane {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
</style>
