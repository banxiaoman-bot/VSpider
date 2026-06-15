<script setup>
import CapabilityStatusBadge from './CapabilityStatusBadge.vue'
import CapabilityRuntimePanel from './CapabilityRuntimePanel.vue'
import CapabilityTraceList from './CapabilityTraceList.vue'
import CapabilityAlignmentCard from './CapabilityAlignmentCard.vue'
import CapabilityExecutionTelemetry from './CapabilityExecutionTelemetry.vue'

const props = defineProps({
  traceRows: { type: Array, default: () => [] },
  filteredTraceRows: { type: Array, default: () => [] },
  traceSummary: { type: Object, default: () => ({}) },
  traceHealth: { type: Object, default: () => ({}) },
  traceFilter: { type: String, default: 'all' },
  traceSearchQuery: { type: String, default: '' },
  runtimePreflight: { type: Object, default: () => ({}) },
  runtimePreflightClass: { type: String, default: '' },
  runtimePreflightLabel: { type: String, default: '' },
  browserRuntime: { type: Object, default: () => ({}) },
  browserRuntimeClass: { type: String, default: '' },
  browserRuntimeLabel: { type: String, default: '' },
  backendSummary: { type: Object, default: () => ({}) },
  capacity: { type: Object, default: () => ({}) },
  healthLabel: { type: String, default: '' },
  healthCacheLabel: { type: String, default: '' },
  hasExecuteEvent: { type: Boolean, default: false },
  executeEvent: { type: Object, default: () => ({}) },
  executionAlignment: { type: Object, default: () => ({}) },
  routeCrawlEfficiencyPlan: { type: Object, default: () => ({}) },
})

const emit = defineEmits([
  'update:traceFilter',
  'update:traceSearchQuery',
  'open-row',
])
</script>

<template>
  <section v-if="traceRows.length" class="capability-health-strip">
    <CapabilityStatusBadge
      :status-class="traceHealth.status"
      :label="traceHealth.label"
    />
    <span>route {{ traceHealth.route }}</span>
    <span>execute {{ traceHealth.execute }}</span>
    <span>issues {{ traceHealth.issues }}</span>
    <span v-if="traceHealth.alignment">{{ traceHealth.alignment }}</span>
  </section>

  <CapabilityRuntimePanel
    :runtime-preflight="runtimePreflight"
    :runtime-preflight-class="runtimePreflightClass"
    :runtime-preflight-label="runtimePreflightLabel"
    :browser-runtime="browserRuntime"
    :browser-runtime-class="browserRuntimeClass"
    :browser-runtime-label="browserRuntimeLabel"
    :backend-summary="backendSummary"
    :capacity="capacity"
    :health-label="healthLabel"
    :health-cache-label="healthCacheLabel"
  />

  <CapabilityTraceList
    :filter="traceFilter"
    :search-query="traceSearchQuery"
    :rows="filteredTraceRows"
    :total-rows="traceRows.length"
    :summary="traceSummary"
    @update:filter="(v) => emit('update:traceFilter', v)"
    @update:search-query="(v) => emit('update:traceSearchQuery', v)"
    @open-row="(evt) => emit('open-row', evt)"
  />

  <CapabilityAlignmentCard
    :visible="hasExecuteEvent"
    :alignment="executionAlignment"
  />

  <CapabilityExecutionTelemetry
    v-if="hasExecuteEvent"
    :execute-event="executeEvent"
    :route-crawl-efficiency-plan="routeCrawlEfficiencyPlan"
  />
</template>
