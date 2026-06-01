<script setup>
import { computed } from 'vue'

const props = defineProps({
  statusClass: {
    type: String,
    default: '',
  },
  label: {
    type: [String, Number],
    default: '',
  },
})

const normalizedClass = computed(() => {
  const value = String(props.statusClass || '').trim()
  if (!value) return ''
  return value.startsWith('is-') ? value : `is-${value}`
})
</script>

<template>
  <span class="capability-health-status" :class="normalizedClass">
    <slot>{{ label }}</slot>
  </span>
</template>

<style scoped>
.capability-health-status {
  padding: 3px 8px;
  border-radius: 999px;
  border: 1px solid rgba(148, 163, 184, 0.22);
  font-weight: 700;
}

.capability-health-status.is-healthy,
.capability-health-status.is-executed,
.capability-health-status.is-ok {
  color: #86efac;
  background: rgba(34, 197, 94, 0.12);
  border-color: rgba(34, 197, 94, 0.35);
}

.capability-health-status.is-route-only,
.capability-health-status.is-fallback {
  color: #fde68a;
  background: rgba(251, 191, 36, 0.12);
  border-color: rgba(251, 191, 36, 0.35);
}

.capability-health-status.is-issue,
.capability-health-status.is-error {
  color: #fecaca;
  background: rgba(239, 68, 68, 0.14);
  border-color: rgba(239, 68, 68, 0.4);
}
</style>
