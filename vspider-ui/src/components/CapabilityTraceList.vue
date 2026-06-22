<script setup>
const props = defineProps({
  rows: {
    type: Array,
    default: () => [],
  },
  totalRows: {
    type: Number,
    default: 0,
  },
  summary: {
    type: Object,
    default: () => ({}),
  },
  filter: {
    type: String,
    default: 'all',
  },
  searchQuery: {
    type: String,
    default: '',
  },
})

const emit = defineEmits(['update:filter', 'update:searchQuery', 'open-row'])

const filters = ['all', 'route', 'execute', 'issues']

const setFilter = (filter) => {
  emit('update:filter', filter)
}

const setSearchQuery = (value) => {
  emit('update:searchQuery', value)
}

const openRow = (row) => {
  emit('open-row', row?.event)
}
</script>

<template>
  <section v-if="props.totalRows" class="capability-section">
    <div class="capability-section-head">
      <h4>Trace 历史</h4>
      <div class="capability-trace-filters">
        <button
          v-for="item in filters"
          :key="item"
          type="button"
          class="capability-trace-filter"
          :class="{ 'is-active': props.filter === item }"
          @click="setFilter(item)"
        >
          {{ item }} {{ props.summary[item] || 0 }}
        </button>
        <el-input
          :model-value="props.searchQuery"
          clearable
          size="small"
          class="capability-trace-search"
          placeholder="搜索 action / selector / issue code"
          @update:model-value="setSearchQuery"
        />
        <span
          v-if="props.searchQuery"
          class="capability-trace-search-count"
        >
          {{ props.rows.length }}/{{ props.totalRows }}
        </span>
      </div>
    </div>
    <div class="capability-trace-list">
      <button
        v-for="row in props.rows"
        :key="`cap-trace-${row.idx}-${row.phase}`"
        type="button"
        class="capability-trace-row"
        :class="[`sev-${row.severity}`, { 'is-issue': row.issue }]"
        :title="row.detail || '单击查看完整 JSON'"
        @click="openRow(row)"
      >
        <span class="capability-trace-index">#{{ row.idx + 1 }}</span>
        <span class="capability-trace-phase">{{ row.phase }}</span>
        <span v-if="row.time" class="capability-trace-time">{{ row.time }}</span>
        <span v-if="row.detail" class="capability-trace-detail">{{ row.detail }}</span>
      </button>
      <p v-if="!props.rows.length" class="capability-trace-empty">
        当前过滤条件下没有 trace 事件
      </p>
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

.capability-trace-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.capability-trace-filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
}

.capability-trace-search {
  width: 260px;
}

.capability-trace-search-count {
  color: var(--vsp-text-muted);
  font-size: 11px;
}

.capability-trace-filter {
  padding: 3px 8px;
  border-radius: 999px;
  color: var(--vsp-text-muted);
  background: var(--vsp-surface);
  border: 1px solid var(--vsp-border);
  font-size: 11px;
  cursor: pointer;
}

.capability-trace-filter.is-active {
  color: var(--vsp-indigo-500);
  background: rgba(99, 102, 241, 0.18);
  border-color: rgba(129, 140, 248, 0.42);
}

.capability-trace-row {
  display: grid;
  grid-template-columns: auto minmax(150px, 0.8fr) auto minmax(220px, 2fr);
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 7px 9px;
  border-radius: 8px;
  border: 1px solid var(--vsp-border);
  color: var(--vsp-text-2);
  background: var(--vsp-surface);
  text-align: left;
  cursor: pointer;
}

.capability-trace-row:hover {
  border-color: rgba(147, 197, 253, 0.42);
  background: var(--vsp-surface-mint);
}

.capability-trace-row.sev-warn {
  border-color: rgba(251, 191, 36, 0.32);
}

.capability-trace-row.sev-error {
  border-color: rgba(248, 113, 113, 0.42);
}

.capability-trace-row.is-issue {
  background: rgba(251, 191, 36, 0.08);
}

.capability-trace-index,
.capability-trace-time {
  color: var(--vsp-text-muted);
  font-family: Consolas, 'JetBrains Mono', monospace;
  font-size: 11.5px;
}

.capability-trace-phase {
  color: var(--vsp-blue-500);
  font-weight: 700;
  font-size: 12px;
}

.capability-trace-detail {
  overflow: hidden;
  color: var(--vsp-text-2);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.capability-trace-empty {
  margin: 2px 0 0;
  color: var(--vsp-text-muted);
  font-size: 12px;
}
</style>
