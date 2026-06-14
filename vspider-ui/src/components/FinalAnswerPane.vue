<script setup>
import { computed, onUnmounted, ref } from 'vue'

const props = defineProps({
  status: { type: String, default: 'idle' },
  text: { type: String, default: '' },
  html: { type: String, default: '' },
  domain: { type: String, default: '' },
})

const emit = defineEmits(['jump-to-artifacts'])

const COLLAPSE_THRESHOLD = 600
const COLLAPSED_PREVIEW = 480
const finalAnswerExpanded = ref(false)
const finalAnswerCopyState = ref('idle')
let finalAnswerCopyTimer = null

const charCount = computed(() => (props.text || '').length)
const lineCount = computed(() => {
  const t = props.text || ''
  return t ? t.split(/\r?\n/).length : 0
})
const isLong = computed(() => charCount.value > COLLAPSE_THRESHOLD)

const DOMAIN_META = {
  weather: { icon: '\u{1F324}\uFE0F', label: '\u5929\u6C14', accent: '#7ec8ff' },
  stock:   { icon: '\u{1F4C8}', label: '\u80A1\u7968', accent: '#7ce0a2' },
  recipe:  { icon: '\u{1F373}', label: '\u83DC\u8C31', accent: '#ffb877' },
  flight:  { icon: '\u2708\uFE0F', label: '\u822A\u73ED', accent: '#c89bff' },
}
const domainMeta = computed(() => DOMAIN_META[props.domain] || null)

const renderMarkdown = (text) => {
  if (!text) return ''
  let t = text
  t = t.replace(/^### (.+)$/gm, '<h3>$1</h3>')
  t = t.replace(/^## (.+)$/gm, '<h2>$1</h2>')
  t = t.replace(/^# (.+)$/gm, '<h1>$1</h1>')
  t = t.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
  t = t.replace(/\*(.+?)\*/g, '<em>$1</em>')
  t = t.replace(/`([^`]+)`/g, '<code>$1</code>')
  t = t.replace(/^- (.+)$/gm, '<li>$1</li>')
  t = t.replace(/(<li>.*<\/li>\n?)+/g, '<ul>$&</ul>')
  t = t.replace(/\n{2,}/g, '</p><p>')
  t = `<p>${t}</p>`
  return t
}

const displayedHtml = computed(() => {
  if (!isLong.value || finalAnswerExpanded.value) return props.html || renderMarkdown(props.text)
  const t = props.text || ''
  let cut = COLLAPSED_PREVIEW
  const slack = Math.min(120, t.length - COLLAPSED_PREVIEW)
  if (slack > 0) {
    const w = t.slice(COLLAPSED_PREVIEW, COLLAPSED_PREVIEW + slack)
    const para = w.search(/\n\s*\n/)
    if (para !== -1) cut = COLLAPSED_PREVIEW + para
    else { const p = w.search(/[。.!?！？]\s/); if (p !== -1) cut = COLLAPSED_PREVIEW + p + 1 }
  }
  return renderMarkdown(t.slice(0, cut))
})

const copyToClipboard = async () => {
  const t = props.text || ''
  if (!t) return
  try {
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(t)
    else {
      const ta = document.createElement('textarea'); ta.value = t; ta.setAttribute('readonly', ''); ta.style.cssText = 'position:absolute;left:-9999px'
      document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta)
    }
    finalAnswerCopyState.value = 'ok'
  } catch (_) { finalAnswerCopyState.value = 'err' } finally {
    if (finalAnswerCopyTimer) clearTimeout(finalAnswerCopyTimer)
    finalAnswerCopyTimer = setTimeout(() => { finalAnswerCopyTimer = null; finalAnswerCopyState.value = 'idle' }, 1600)
  }
}

const exportAsMarkdown = () => {
  const t = props.text || ''; if (!t) return
  const ts = new Date()
  const stamp = `${ts.getFullYear()}${String(ts.getMonth() + 1).padStart(2, '0')}${String(ts.getDate()).padStart(2, '0')}_${String(ts.getHours()).padStart(2, '0')}${String(ts.getMinutes()).padStart(2, '0')}${String(ts.getSeconds()).padStart(2, '0')}`
  const blob = new Blob([t], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = `final_answer_${stamp}.md`
  document.body.appendChild(a); a.click(); document.body.removeChild(a); setTimeout(() => URL.revokeObjectURL(url), 1000)
}

onUnmounted(() => { if (finalAnswerCopyTimer) clearTimeout(finalAnswerCopyTimer) })

defineExpose({ resetExpanded: () => { finalAnswerExpanded.value = false } })
</script>

<template>
  <el-scrollbar class="final-scroll">
    <div v-if="status === 'pending'" class="final-pending">
      <div class="typing-dots" aria-hidden="true"><span /><span /><span /></div>
      <p class="final-pending-text">Agent 正在思考并提取结论...</p>
    </div>

    <div v-else-if="status === 'file'" class="final-fallback">
      <div v-if="text && text.trim()" class="final-md final-file-summary" v-html="html || renderMarkdown(text)" />
      <p class="final-fallback-text">本次任务产出为结构化数据，请前往<a href="#" class="tab-jump" @click.prevent="emit('jump-to-artifacts')">产物</a>面板下载</p>
    </div>

    <div v-else-if="status === 'text'" class="final-text-wrap">
      <div v-if="domainMeta" class="final-domain-card" :style="{ borderColor: domainMeta.accent, background: 'linear-gradient(135deg, ' + domainMeta.accent + '14, transparent 65%)' }">
        <span class="final-domain-icon" aria-hidden="true">{{ domainMeta.icon }}</span>
        <div class="final-domain-meta">
          <span class="final-domain-label" :style="{ color: domainMeta.accent }">{{ domainMeta.label }}</span>
          <span class="final-domain-hint">由 Agent 自动识别为该领域，答案已按领域格式精简</span>
        </div>
      </div>
      <div class="final-toolbar">
        <span class="final-meta">{{ charCount }} 字 · {{ lineCount }} 行</span>
        <div class="final-toolbar-spacer" />
        <el-button size="small" plain class="final-toolbar-btn" :type="finalAnswerCopyState === 'ok' ? 'success' : finalAnswerCopyState === 'err' ? 'danger' : 'default'" @click="copyToClipboard">
          <span v-if="finalAnswerCopyState === 'ok'">已复制 ✓</span>
          <span v-else-if="finalAnswerCopyState === 'err'">复制失败</span>
          <span v-else>复制 Markdown</span>
        </el-button>
        <el-button size="small" plain class="final-toolbar-btn" @click="exportAsMarkdown">导出 .md</el-button>
      </div>
      <div class="final-md" v-html="displayedHtml" />
      <div v-if="isLong" class="final-toggle-wrap">
        <button class="final-toggle-btn" type="button" @click="finalAnswerExpanded = !finalAnswerExpanded">{{ finalAnswerExpanded ? '收起' : `展开全部 (${charCount} 字)` }}</button>
      </div>
    </div>

    <p v-else class="final-empty">提交任务后，Agent 的最终结论会显示在这里</p>
  </el-scrollbar>
</template>

<style src="../styles/final-answer-pane.css" scoped></style>
