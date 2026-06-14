<script setup>
import { computed, nextTick, ref, watch } from 'vue'

const props = defineProps({
  logs: { type: Array, default: () => [] },
  logsTrimmedCount: { type: Number, default: 0 },
  wsStatus: { type: String, default: 'connecting' },
  isRunning: { type: Boolean, default: false },
})

const terminalRef = ref(null)
const terminalSearchVisible = ref(false)
const terminalSearchQuery = ref('')
const terminalSearchCurrent = ref(0)
const terminalSearchInputRef = ref(null)

function logLineClass(line) {
  if (!line) return ''
  const head = String(line).slice(0, 32)
  if (head.startsWith('[ERROR')) return 'log-line--error'
  if (head.startsWith('[PHASE/ERR')) return 'log-line--error'
  if (head.startsWith('[WARN')) return 'log-line--warn'
  if (head.startsWith('[PHASE/WARN')) return 'log-line--warn'
  if (head.startsWith('[PHASE]')) return 'log-line--phase'
  if (head.startsWith('[DONE')) return 'log-line--done'
  if (head.startsWith('[HITL')) return 'log-line--hitl'
  if (head.startsWith('[ARTIFACT')) return 'log-line--artifact'
  if (head.startsWith('[CAPTCHA')) return 'log-line--warn'
  if (head.startsWith('[SYSTEM')) return 'log-line--system'
  if (head.startsWith('[AUTH')) return 'log-line--system'
  return ''
}

const _escapeRegex = (s) =>
  String(s || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

const terminalSearchActive = computed(
  () => terminalSearchVisible.value
    && String(terminalSearchQuery.value || '').trim().length > 0,
)

const terminalSearchMatches = computed(() => {
  if (!terminalSearchActive.value) return []
  const q = String(terminalSearchQuery.value).toLowerCase()
  const out = []
  for (let li = 0; li < props.logs.length; li += 1) {
    const line = String(props.logs[li] || '').toLowerCase()
    if (!q.length) continue
    let from = 0
    while (from < line.length) {
      const idx = line.indexOf(q, from)
      if (idx === -1) break
      out.push({ lineIdx: li, start: idx, end: idx + q.length })
      from = idx + Math.max(1, q.length)
    }
  }
  return out
})

const terminalSearchSegments = computed(() => {
  const map = new Map()
  if (!terminalSearchActive.value) return map
  const matches = terminalSearchMatches.value
  if (!matches.length) return map
  const byLine = new Map()
  for (let i = 0; i < matches.length; i += 1) {
    const m = matches[i]
    if (!byLine.has(m.lineIdx)) byLine.set(m.lineIdx, [])
    byLine.get(m.lineIdx).push({ ...m, gIdx: i })
  }
  const cur = terminalSearchCurrent.value
  for (const [lineIdx, list] of byLine.entries()) {
    const line = String(props.logs[lineIdx] || '')
    const segs = []
    let cursor = 0
    for (const m of list) {
      if (m.start > cursor) {
        segs.push({ text: line.slice(cursor, m.start), kind: 'plain' })
      }
      segs.push({
        text: line.slice(m.start, m.end),
        kind: m.gIdx === cur ? 'current' : 'hit',
      })
      cursor = m.end
    }
    if (cursor < line.length) {
      segs.push({ text: line.slice(cursor), kind: 'plain' })
    }
    map.set(lineIdx, segs)
  }
  return map
})

const terminalSearchTotal = computed(() => terminalSearchMatches.value.length)

const openTerminalSearch = () => {
  terminalSearchVisible.value = true
  nextTick(() => {
    const inp = terminalSearchInputRef.value
    if (!inp) return
    try {
      if (typeof inp.focus === 'function') {
        inp.focus()
        if (typeof inp.select === 'function') inp.select()
      } else if (inp.$el && inp.$el.querySelector) {
        const ta = inp.$el.querySelector('input, textarea')
        if (ta && typeof ta.focus === 'function') {
          ta.focus()
          ta.select()
        }
      }
    } catch (_) { /* focus failures aren't fatal */ }
  })
}

const closeTerminalSearch = () => {
  terminalSearchVisible.value = false
  terminalSearchCurrent.value = 0
}

const terminalSearchNext = () => {
  const total = terminalSearchTotal.value
  if (total === 0) return
  terminalSearchCurrent.value = (terminalSearchCurrent.value + 1) % total
}

const terminalSearchPrev = () => {
  const total = terminalSearchTotal.value
  if (total === 0) return
  terminalSearchCurrent.value =
    (terminalSearchCurrent.value - 1 + total) % total
}

watch(terminalSearchQuery, () => {
  terminalSearchCurrent.value = 0
})

watch(() => props.logs, () => {
  if (terminalSearchCurrent.value >= terminalSearchTotal.value) {
    terminalSearchCurrent.value = 0
  }
})

const scrollToBottom = async () => {
  await nextTick()
  const el = terminalRef.value
  if (!el) return
  if (typeof el.setScrollTop === 'function') {
    el.setScrollTop(Number.MAX_SAFE_INTEGER)
    return
  }
  const wrap = el.wrapRef || el
  if (wrap) wrap.scrollTop = wrap.scrollHeight
}

defineExpose({
  scrollToBottom,
  openTerminalSearch,
  closeTerminalSearch,
  terminalSearchNext,
  terminalSearchPrev,
  searchVisible: terminalSearchVisible,
})
</script>

<template>
  <div class="terminal-log-pane">
    <div class="terminal-heading">
      <span class="status-pill" :class="wsStatus">
        {{ isRunning ? 'RUNNING' : 'IDLE' }} · {{ wsStatus }}
      </span>
      <div class="terminal-heading-spacer" />
      <div v-if="terminalSearchVisible" class="terminal-search-bar">
        <el-input
          ref="terminalSearchInputRef"
          v-model="terminalSearchQuery"
          size="small"
          clearable
          class="terminal-search-input"
          placeholder="搜索日志..."
          @keydown.enter.prevent="event => event.shiftKey
            ? terminalSearchPrev()
            : terminalSearchNext()"
          @keydown.esc.stop.prevent="closeTerminalSearch"
        />
        <span class="terminal-search-count">
          {{ terminalSearchTotal
            ? `${terminalSearchCurrent + 1}/${terminalSearchTotal}`
            : '0/0' }}
        </span>
        <el-button
          size="small"
          plain
          :disabled="terminalSearchTotal === 0"
          title="上一个匹配 (Shift+Enter)"
          @click="terminalSearchPrev"
        >↑</el-button>
        <el-button
          size="small"
          plain
          :disabled="terminalSearchTotal === 0"
          title="下一个匹配 (Enter)"
          @click="terminalSearchNext"
        >↓</el-button>
        <el-button
          size="small"
          plain
          title="关闭搜索 (Esc)"
          @click="closeTerminalSearch"
        >关闭</el-button>
      </div>
      <el-button
        v-else
        size="small"
        plain
        class="terminal-search-open-btn"
        title="搜索 Live Terminal (Ctrl+F)"
        @click="openTerminalSearch"
      >搜索</el-button>
    </div>
    <el-scrollbar ref="terminalRef" class="terminal-scroll">
      <p v-if="logsTrimmedCount > 0" class="log-line log-line--system">
        [SYSTEM] 已裁剪最早 {{ logsTrimmedCount }} 行（完整日志见后端 event_stream.jsonl）
      </p>
      <template v-if="logs.length">
        <p
          v-for="(line, idx) in logs"
          :key="idx"
          class="log-line"
          :class="logLineClass(line)"
        >
          <template v-if="terminalSearchSegments.get(idx)">
            <span
              v-for="(seg, sidx) in terminalSearchSegments.get(idx)"
              :key="`${idx}-${sidx}`"
              :class="{
                'terminal-search-hit': seg.kind === 'hit',
                'terminal-search-current': seg.kind === 'current',
              }"
            >{{ seg.text }}</span>
          </template>
          <template v-else>{{ line }}</template>
        </p>
      </template>
      <p v-else class="empty-log">等待日志流...</p>
    </el-scrollbar>
  </div>
</template>

<style scoped>
@import '../styles/terminal-log-pane.css';
</style>
