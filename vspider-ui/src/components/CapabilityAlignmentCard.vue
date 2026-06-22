<script setup>
const props = defineProps({
  alignment: {
    type: Object,
    default: () => ({}),
  },
  visible: {
    type: Boolean,
    default: false,
  },
})
</script>

<template>
  <section v-if="props.visible" class="capability-section">
    <h4>Route / Execute 对齐</h4>
    <div class="capability-alignment-card">
      <span
        class="capability-alignment-status"
        :class="props.alignment.topChoice ? 'is-complete' : 'is-fallback'"
      >
        {{ props.alignment.topChoice ? '命中首选' : '非首选执行' }}
      </span>
      <span v-if="props.alignment.capability">
        executed: {{ props.alignment.capability }}
      </span>
      <span v-if="props.alignment.rank">
        plan rank #{{ props.alignment.rank }}
      </span>
      <span v-else-if="props.alignment.capability">
        not in plan
      </span>
      <span v-if="props.alignment.planHead">
        preferred: {{ props.alignment.planHead }}
      </span>
      <span v-if="props.alignment.fallbackReason">
        fallback: {{ props.alignment.fallbackReason }}
      </span>
    </div>
  </section>
</template>

<style scoped>
.capability-section h4 {
  margin: 0 0 8px;
  color: var(--vsp-text-strong);
  font-size: 13px;
}

.capability-alignment-card {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  color: var(--vsp-text-2);
  font-size: 12px;
}

.capability-alignment-status {
  padding: 3px 8px;
  border-radius: 999px;
  border: 1px solid rgba(148, 163, 184, 0.22);
  background: rgba(148, 163, 184, 0.08);
  font-size: 11.5px;
  font-weight: 700;
}

.capability-alignment-status.is-complete {
  color: var(--vsp-success);
  background: rgba(34, 197, 94, 0.12);
  border-color: rgba(34, 197, 94, 0.35);
}

.capability-alignment-status.is-fallback {
  color: var(--vsp-warn);
  background: rgba(251, 191, 36, 0.12);
  border-color: rgba(251, 191, 36, 0.35);
}
</style>
