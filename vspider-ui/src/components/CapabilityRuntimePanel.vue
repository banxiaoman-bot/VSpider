<script setup>
import CapabilityStatusBadge from './CapabilityStatusBadge.vue'

const props = defineProps({
  runtimePreflight: {
    type: Object,
    default: () => ({}),
  },
  runtimePreflightClass: {
    type: String,
    default: '',
  },
  runtimePreflightLabel: {
    type: String,
    default: '',
  },
  browserRuntime: {
    type: Object,
    default: () => ({}),
  },
  browserRuntimeClass: {
    type: String,
    default: '',
  },
  browserRuntimeLabel: {
    type: String,
    default: '',
  },
  backendSummary: {
    type: Object,
    default: () => ({}),
  },
  capacity: {
    type: Object,
    default: () => ({}),
  },
  healthLabel: {
    type: String,
    default: '',
  },
  healthCacheLabel: {
    type: String,
    default: '',
  },
})
</script>

<template>
  <section v-if="props.runtimePreflight.version" class="capability-section browser-runtime-section">
    <div class="capability-section-head">
      <h4>Runtime Preflight</h4>
      <CapabilityStatusBadge
        :status-class="props.runtimePreflightClass"
        :label="props.runtimePreflightLabel"
      />
    </div>
    <div class="browser-runtime-grid">
      <div class="browser-runtime-card">
        <span>action</span>
        <strong>{{ props.runtimePreflight.recommended_action || 'continue' }}</strong>
        <small>
          runtime {{ props.runtimePreflight.runtime_status || 'unknown' }}
          · blocking {{ props.runtimePreflight.blocking ? 'yes' : 'no' }}
        </small>
      </div>
      <div class="browser-runtime-card">
        <span>checks</span>
        <strong>{{ props.runtimePreflight.pool_available ? 'pool ok' : 'pool warn' }}</strong>
        <small>
          backend {{ props.runtimePreflight.backend_healthy === false ? 'unhealthy' : 'healthy/unknown' }}
          · cache {{ props.runtimePreflight.cache_stale ? 'stale' : 'fresh' }}
        </small>
      </div>
      <div class="browser-runtime-card">
        <span>warnings</span>
        <strong>{{ Array.isArray(props.runtimePreflight.warnings) ? props.runtimePreflight.warnings.length : 0 }}</strong>
        <small>
          {{ Array.isArray(props.runtimePreflight.warnings) && props.runtimePreflight.warnings.length ? props.runtimePreflight.warnings.join(' · ') : 'none' }}
        </small>
      </div>
    </div>
  </section>

  <section v-if="props.browserRuntime.version" class="capability-section browser-runtime-section">
    <div class="capability-section-head">
      <h4>Browser Runtime</h4>
      <CapabilityStatusBadge
        :status-class="props.browserRuntimeClass"
        :label="props.browserRuntimeLabel"
      />
    </div>
    <div class="browser-runtime-grid">
      <div class="browser-runtime-card">
        <span>active backend</span>
        <strong>{{ props.backendSummary.active_name || 'unknown' }}</strong>
        <small>
          {{ props.backendSummary.active_kind || 'backend' }}
          · {{ props.backendSummary.active_transport || 'transport?' }}
        </small>
      </div>
      <div class="browser-runtime-card">
        <span>health</span>
        <strong>{{ props.healthLabel }}</strong>
        <small>
          {{ props.backendSummary.health_check_kind || 'metadata' }}
          · reachable {{ props.backendSummary.health_reachable == null ? 'unknown' : (props.backendSummary.health_reachable ? 'yes' : 'no') }}
          <template v-if="props.backendSummary.health_latency_ms != null">
            · {{ props.backendSummary.health_latency_ms }}ms
          </template>
          · {{ props.healthCacheLabel }}
        </small>
      </div>
      <div class="browser-runtime-card">
        <span>capacity</span>
        <strong>
          {{ props.capacity.available_contexts ?? 0 }}
          /
          {{ props.capacity.max_contexts ?? 0 }}
        </strong>
        <small>
          active {{ props.capacity.active_contexts ?? 0 }}
          · sessions {{ props.capacity.backend_session_count ?? 0 }}
        </small>
      </div>
      <div class="browser-runtime-card">
        <span>remote</span>
        <strong>{{ props.backendSummary.remote_available ? 'available' : 'planned' }}</strong>
        <small>
          {{ props.backendSummary.supports_remote ? 'active backend supports remote' : 'local active backend' }}
        </small>
      </div>
      <div class="browser-runtime-card">
        <span>safety</span>
        <strong>{{ props.capacity.parallel_enabled ? 'parallel' : 'single' }}</strong>
        <small>
          {{ props.capacity.safety_cap_active ? 'safety cap active' : 'no safety cap' }}
        </small>
      </div>
    </div>
  </section>
</template>

<style scoped>
.capability-section h4 {
  margin: 0 0 8px;
  color: var(--vsp-text-strong);
  font-size: 13px;
}

.capability-section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin: 0 0 8px;
}

.capability-section-head h4 {
  margin: 0;
}

.browser-runtime-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 8px;
}

.browser-runtime-card {
  padding: 9px 10px;
  border-radius: 8px;
  background: var(--vsp-surface);
  border: 1px solid var(--vsp-border);
}

.browser-runtime-card span {
  display: block;
  color: var(--vsp-text-muted);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.browser-runtime-card strong {
  display: block;
  margin-top: 3px;
  color: var(--vsp-blue-500);
  font-size: 14px;
}

.browser-runtime-card small {
  display: block;
  margin-top: 3px;
  color: var(--vsp-text-2);
  font-size: 11.5px;
  line-height: 1.4;
}
</style>
