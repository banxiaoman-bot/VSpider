<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import 'element-plus/theme-chalk/dark/css-vars.css'
import {
  Close,
  Monitor,
  Refresh,
  Setting,
  UploadFilled,
  VideoPlay,
} from '@element-plus/icons-vue'
import {
  capabilityAttemptClass,
  capabilityCrawlEfficiencyCandidateClass,
  capabilityCrawlEfficiencyEvidence,
  capabilityEventActionIssueSummary,
  capabilityEventActionTrace,
  capabilityEventCrawlEfficiencyPlan,
  capabilityEventEfficiencyCorrelationReport,
  capabilityEventFailureBundle,
  capabilityEventTraceArtifact,
  capabilityItemDetail,
  capabilityItemMeta,
  capabilityItemName,
} from './components/capabilityTraceUtils'
import CapabilityStatusBadge from './components/CapabilityStatusBadge.vue'
import CapabilityTraceList from './components/CapabilityTraceList.vue'
import CapabilityRuntimePanel from './components/CapabilityRuntimePanel.vue'
import CapabilityAlignmentCard from './components/CapabilityAlignmentCard.vue'
import CapabilityEfficiencyPanel from './components/CapabilityEfficiencyPanel.vue'
import RunRegistryPanel from './components/RunRegistryPanel.vue'
import ShortcutHelpDialog from './components/dialogs/ShortcutHelpDialog.vue'
import { buildFailureFixtureBatchReplaySummaryText } from './composables/failureFixtureSummary'
import { createTerminalLogBuffer } from './composables/useTerminalLog.js'
import {
  ATTACHMENT_INTENT_AUTO,
  ATTACHMENT_INTENT_OPTIONS,
  appendAttachmentIntentToFormData,
} from './composables/useAttachmentIntent.js'
import {
  ATTACHMENT_ACCEPT,
  ATTACHMENT_HINT,
  appendConstraintsToFormData,
  authProfileOptionLabel,
  buildAuthoritativeUrlsPayload,
  buildTaskConstraints,
  isBotChallengeReason,
  validateTaskInput,
  fetchOutputContractPreview,
  formatOutputContractPreview,
} from './composables/useTaskSubmit'
import { API_BASE, apiFetch, wsUrl } from './api/client.js'

const url = ref('')
const prompt = ref('')
const outputContractPreview = ref(null)
const outputContractPreviewLoading = ref(false)
let outputContractPreviewTimer = null
const extraUrls = ref('')
const proxyServer = ref('')
const proxyUsername = ref('')
const proxyPassword = ref('')
const batchMaxRuns = ref(0)
const resumeEnabled = ref(false)
const captchaSolverEnabled = ref(false)
const captchaSolverProvider = ref('')
const selectedAuthProfiles = ref([])
const selectedFile = ref(null)
// 优化 E: 附件 intent 用户覆盖（auto = 交给后端推断）
const attachmentIntent = ref(ATTACHMENT_INTENT_AUTO)
const isRunning = ref(false)
// 优化 D: batched log buffer — one reactive update + one scroll per frame
// instead of per WS line; ring-trims to LOG_LIMIT (backend event_stream
// keeps the full log). scrollToBottom is defined below; the arrow defers
// the lookup until the first async flush, after setup has finished.
const {
  logs,
  trimmedCount: logsTrimmedCount,
  appendLog,
  clear: clearTerminalLogs,
} = createTerminalLogBuffer({ onFlush: () => { scrollToBottom() } })
const currentImageBase64 = ref('')
const terminalRef = ref(null)
const wsStatus = ref('connecting')

const authDialogOpen = ref(false)
const authLoginUrl = ref('')
const authProfileName = ref('')
const authProfileOptions = ref([])
const isAuthRecording = ref(false)
const selectedModel = ref('backend-default')
const selectedSemanticModel = ref('backend-default')
const modelSettingsOpen = ref(false)
const modelTemperature = ref(0.1)
const modelMaxTokens = ref(4096)
const modelBaseUrl = ref('')
const modelApiKey = ref('')
const semanticBaseUrl = ref('')
const semanticApiKey = ref('')
const isHumanInterventionRequired = ref(false)
const humanInterventionReason = ref('')
const activeBottomTab = ref('terminal')
// A: Capability 页签内部二级子页签（概览/计划/回放/诊断）
const capabilitySubTab = ref('overview')
// B: Timeline 筛选 chips 默认收起，点「筛选」按钮展开
const timelineFiltersExpanded = ref(false)
// C2: 高级配置抽屉 — 左栏只留任务输入，配置项收进抽屉
const settingsDrawerOpen = ref(false)
const settingsActivePanels = ref(['models', 'identity', 'constraints', 'file'])
const artifactList = ref([])
const hasNewArtifacts = ref(false)
const runHistoryRefreshToken = ref(0)
const hasNewRuns = ref(false)
const browserRuntimeStatus = ref(null)
const browserRuntimeLoading = ref(false)

// ── K3: Failed runs drawer ──
// failedRunsList: array of records returned by GET /api/failed_runs.
// Each record carries schema_version/run_id/ts/reason/goal/duration_s/
// step_count/paths/paths_exist (the last is added by list_failed_runs
// based on filesystem presence; we use it to grey out the HTML log
// button when the underlying file was deleted/rotated).
const failedRunsList = ref([])
// hasNewFailures: pulse the tab badge dot when a fresh failure lands
// while the user is on a different tab, mirroring hasNewArtifacts.
const hasNewFailures = ref(false)
// failedRunsLoading: prevent overlapping refresh spinners — fetchFailedRuns
// can be triggered both by the manual Refresh button AND by the WS done
// handler (success=false), so back-to-back triggers are common.
const failedRunsLoading = ref(false)

// ── K6: Failed-run detail dialog ──────────────────────────────────
// Click any row in the 失败记录 table → open a dialog showing all
// archive fields + a pretty-printed JSON dump + a fetched preview of
// the related phase events (the historic ``phase_<run_id>.jsonl``).
//
//   selectedFailedRun:        the record dict (same shape list endpoint
//                             returns) currently displayed in the dialog
//   failedRunDialogVisible:   v-model for the dialog
//   failedRunPhaseEvents:     fetched phase events array (may be empty
//                             when the .jsonl file no longer exists)
//   failedRunPhaseStatus:     'idle' | 'loading' | 'ok' | 'missing' | 'error'
//                             — drives the preview area's UI state
//   failedRunPhaseTotal:      total events on disk (>= length of preview)
//   failedRunPhaseTruncated:  True iff backend hit its tail-only cap
const selectedFailedRun = ref(null)
const failedRunDialogVisible = ref(false)
const failedRunPhaseEvents = ref([])
const failedRunPhaseStatus = ref('idle')
const failedRunPhaseTotal = ref(0)
const failedRunPhaseTruncated = ref(false)

// ── M: Phase timeline ──
// phaseEvents: list of {type, phase, severity, message, step, duration_ms,
//   ts, ...extras} pushed from the WS phase channel (G2/H1/I/L source).
// Capped at 500 to keep the panel light; older events drop off the front.
// hasNewPhase: pulse the tab badge when an event lands while the user is
// on a different tab.
const phaseEvents = ref([])
const hasNewPhase = ref(false)
const hasNewCapability = ref(false)
const capabilityFailureFixtureLoading = ref(false)
const capabilityFailureFixtureReplayLoading = ref(false)
const capabilityFailureFixtureReplayReport = ref(null)
const capabilityFailureFixtureReplayArtifact = ref(null)
const capabilityFailureFixtureLibraryLoading = ref(false)
const capabilityFailureFixtureBatchReplayLoading = ref(false)
const capabilityFailureFixtureBatchHistoryLoading = ref(false)
const capabilityFailureFixtureLibrary = ref([])
const capabilityFailureFixtureBatchHistory = ref([])
const capabilityFailureFixtureBatchHistoryTrend = ref(null)
const capabilityFailureFixtureBatchReplayReport = ref(null)
const capabilityFailureFixtureBatchReplayArtifact = ref(null)
const capabilityEfficiencyFeedbackReplayLoading = ref(false)
const capabilityEfficiencyFeedbackReplayLibraryLoading = ref(false)
const capabilityEfficiencyFeedbackReplayReport = ref(null)
const capabilityEfficiencyFeedbackReplayArtifact = ref(null)
const capabilityEfficiencyFeedbackReplayLibrary = ref([])
const capabilityTraceFilter = ref('all')
const capabilityTraceSearchQuery = ref('')
const PHASE_LIMIT = 500

// ── N: Phase event detail dialog ──
// Click a timeline chip → open a dialog with the full event payload
// (pretty-printed JSON + key fields summary).
const phaseDialogVisible = ref(false)
const selectedPhaseEvent = ref(null)

// ── T: Keyboard shortcuts ─────────────────────────────────────────
// helpDialogVisible: toggled by Ctrl+/ — shows a cheat-sheet table.
// promptInputRef: bound to the prompt el-input via :ref so Ctrl+K can
//   programmatically focus the textarea even when it isn't visible yet.
const helpDialogVisible = ref(false)
const promptInputRef = ref(null)

// ── O: Phase timeline filtering ──
// phaseFilterExclude: set of phase names to HIDE (default empty = show all).
// We use exclude semantics so new phases added by future patches show up by
// default, instead of silently disappearing because they weren't in an
// allowlist.
// severityFilterExclude: same idea but per-severity ('info'/'warn'/'error').
const phaseFilterExclude = ref(new Set())
const severityFilterExclude = ref(new Set())

// V: Phase histogram / stats panel (toggled in the Timeline toolbar).
//   phaseStatsExpanded: drives the expand/collapse animation. Default
//     collapsed so the panel doesn't crowd the existing filter pills
//     until the user explicitly opens it.
//   phaseStatsSortBy:   one of 'count' | 'mean' | 'p95' | 'max'.
//     Tells the stats grid which column to descending-sort by. Default
//     'count' matches the existing filter row order so the toggle
//     doesn't re-shuffle the user's mental model.
const phaseStatsExpanded = ref(false)
const phaseStatsSortBy = ref('count')

// W: Offline replay mode ───────────────────────────────────────────
//   replayMode:        when true, ``phaseEvents`` is replaced by an
//                      imported ``phase_<id>.jsonl`` file and incoming
//                      WS phase events are DROPPED so they don't
//                      pollute the replay buffer. The Live Terminal
//                      keeps streaming — only Timeline is gated.
//   replaySourceName:  name of the imported file (shown in the banner)
//                      so the user can tell which run they're viewing.
//   replayInputRef:    bound to the hidden <input type="file"> so the
//                      "导入回放" button can trigger it programmatically.
const replayMode = ref(false)
const replaySourceName = ref('')
const replayInputRef = ref(null)
const replayImportTarget = ref('timeline')

// X: Live Terminal in-content search ──────────────────────────────────
//   terminalSearchVisible: shows the search bar when true.
//   terminalSearchQuery:   user input (case-insensitive substring).
//   terminalSearchCurrent: index into the filtered match list — drives
//                          the "highlight current match" rendering and
//                          n / Shift+n navigation.
//   terminalSearchInputRef: bound to the el-input so Ctrl+F can focus.
// We deliberately keep this OUTSIDE the keyboard shortcut help dialog's
// list of "global" keys: Ctrl+F is only meaningful while the Live
// Terminal tab is active, and trying to grab it globally would break
// the user's expectation of the browser's native page-search.
const terminalSearchVisible = ref(false)
const terminalSearchQuery = ref('')
const terminalSearchCurrent = ref(0)
const terminalSearchInputRef = ref(null)

// ── P: Timeline auto-scroll ──
// timelineRef: reference to the el-scrollbar of the timeline panel.
// timelineAutoScroll: pin-to-bottom flag. Starts true. Flips to false
// when the user scrolls up by hand; flips back to true when they scroll
// to the bottom OR click the "回到底部" floating button.
// SCROLL_BOTTOM_EPS: how close to the bottom counts as "at bottom" (px).
const timelineRef = ref(null)
const timelineAutoScroll = ref(true)
const SCROLL_BOTTOM_EPS = 24

// ── Final Answer 面板状态 ──
// taskResult: 后端最终结果，结构 { type: 'text' | 'file', answer: string }
//   - null：未开始 / 已重置
//   - { type: 'text', answer }：纯文本答案，自动切到 Final Answer Tab 并渲染 Markdown
//   - { type: 'file', answer? }：结构化导出，自动切到 Artifacts Tab；
//                                 Final Answer 面板显示兜底文案
// finalAnswerStatus: 'idle' | 'pending' | 'text' | 'file'，驱动面板的 3 种 UI 状态
const taskResult = ref(null)
const finalAnswerStatus = ref('idle')
const finalAnswerText = ref('')
const finalAnswerDomain = ref('') // F3: 'weather'|'stock'|'recipe'|'flight'|'' (空=无卡片)
const hasNewFinalAnswer = ref(false)
// 启动任务时记录 artifact 数，作为兜底的 type 推断依据（后端未显式标记时使用）
let artifactsCountAtSubmit = 0

let socket = null
let reconnectTimer = null
let isUnmounted = false

const MODEL_SETTINGS_STORAGE_KEY = 'vspider:model-settings:v1'

const loadModelSettings = () => {
  try {
    const raw = window.localStorage.getItem(MODEL_SETTINGS_STORAGE_KEY)
    if (!raw) return
    const data = JSON.parse(raw)
    if (typeof data.selectedModel === 'string') selectedModel.value = data.selectedModel
    if (typeof data.selectedSemanticModel === 'string') selectedSemanticModel.value = data.selectedSemanticModel
    if (typeof data.modelBaseUrl === 'string') modelBaseUrl.value = data.modelBaseUrl
    if (typeof data.semanticBaseUrl === 'string') semanticBaseUrl.value = data.semanticBaseUrl
    if (typeof data.modelTemperature === 'number') modelTemperature.value = data.modelTemperature
    if (typeof data.modelMaxTokens === 'number') modelMaxTokens.value = data.modelMaxTokens
  } catch (err) {
    console.warn('[settings] failed to load model settings', err)
  }
}

const saveModelSettings = () => {
  try {
    window.localStorage.setItem(MODEL_SETTINGS_STORAGE_KEY, JSON.stringify({
      selectedModel: selectedModel.value,
      selectedSemanticModel: selectedSemanticModel.value,
      modelBaseUrl: modelBaseUrl.value,
      semanticBaseUrl: semanticBaseUrl.value,
      modelTemperature: modelTemperature.value,
      modelMaxTokens: modelMaxTokens.value,
    }))
  } catch (err) {
    console.warn('[settings] failed to save model settings', err)
  }
}

watch(
  [selectedModel, selectedSemanticModel, modelBaseUrl, semanticBaseUrl, modelTemperature, modelMaxTokens],
  saveModelSettings,
)

// ── 自动切换 Tab：仅在 taskResult.type 真正发生变化时触发，避免无限循环 ──
//   - 只读 taskResult，只写 activeBottomTab / hasNewFinalAnswer
//   - 不会回写 taskResult，所以这个 watch 不会自激
//   - 重复的 done 事件（同 type）也不会再次抢用户已切走的 Tab
watch(taskResult, (val, oldVal) => {
  if (!val) return
  if (oldVal && oldVal.type === val.type) return
  if (val.type === 'text') {
    if (activeBottomTab.value !== 'final') {
      hasNewFinalAnswer.value = true
    }
    activeBottomTab.value = 'final'
  } else if (val.type === 'file') {
    activeBottomTab.value = 'artifacts'
  }
})

const authProfileNames = computed(() =>
  authProfileOptions.value.map((item) => item.name).filter(Boolean),
)

const isBotChallengeHitl = computed(() =>
  isBotChallengeReason(humanInterventionReason.value),
)
const selectedModelType = computed(() =>
  ['deepseek-chat', 'deepseek-reasoner', 'deepseek-v4-flash', 'deepseek-v4-pro']
    .includes(selectedModel.value) ? 'text' : 'vl',
)

// ─────────────────────────────────────────────────────────────
//  轻量 Markdown 渲染器（不引入第三方依赖）
//  支持：``` 代码块、`inline 代码`、**粗体**、*斜体*、# / ## / ###
//        标题、- / * 列表、[text](url) 链接、段落（空行分隔）。
//  安全：先把代码块抽成占位符，再把剩余文本整体 escapeHtml，
//        最后才把 Markdown 标记替换为受控的 HTML 标签；
//        链接仅放行 http/https/mailto 协议，其余统一渲染为纯文本。
// ─────────────────────────────────────────────────────────────
const escapeHtml = (s) =>
  String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')

const renderMarkdown = (src) => {
  if (src == null || src === '') return ''
  let text = String(src).replace(/\r\n/g, '\n')

  // 1. 抽取 ``` 代码块到占位符，避免内部内容被后续替换破坏
  const codeBlocks = []
  text = text.replace(/```([\w-]*)\n?([\s\S]*?)```/g, (_m, lang, code) => {
    const idx = codeBlocks.length
    const langClass = lang ? ` lang-${escapeHtml(lang)}` : ''
    codeBlocks.push(
      `<pre class="md-pre"><code class="md-code${langClass}">${escapeHtml(code.replace(/\n$/, ''))}</code></pre>`,
    )
    return `\u0000CB${idx}\u0000`
  })

  // 2. 抽取行内代码到占位符
  const inlineCodes = []
  text = text.replace(/`([^`\n]+)`/g, (_m, c) => {
    const idx = inlineCodes.length
    inlineCodes.push(`<code class="md-icode">${escapeHtml(c)}</code>`)
    return `\u0000IC${idx}\u0000`
  })

  // 3. 整体 escape HTML
  text = escapeHtml(text)

  // 4. 标题 ### / ## / # （顺序：长前缀先匹配）
  text = text.replace(/^###\s+(.+)$/gm, '<h3 class="md-h3">$1</h3>')
  text = text.replace(/^##\s+(.+)$/gm, '<h2 class="md-h2">$1</h2>')
  text = text.replace(/^#\s+(.+)$/gm, '<h1 class="md-h1">$1</h1>')

  // 5. 粗体、斜体（粗体优先以避免 ** 被 * 抢先吃掉）
  text = text.replace(/\*\*([^*\n]+?)\*\*/g, '<strong>$1</strong>')
  text = text.replace(/(^|[^*])\*([^*\n]+?)\*(?!\*)/g, '$1<em>$2</em>')

  // 6. 链接 [text](url) —— 仅允许 http/https/mailto
  text = text.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (m, label, href) => {
    if (!/^(https?:|mailto:)/i.test(href)) return m
    return `<a class="md-a" href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`
  })

  // 7. 无序列表：连续的 - / * 行 → <ul><li>
  text = text.replace(
    /(?:^|\n)((?:[-*]\s+.+(?:\n|$))+)/g,
    (_m, block) => {
      const items = block
        .split('\n')
        .filter((l) => l.trim())
        .map((l) => `<li>${l.replace(/^[-*]\s+/, '')}</li>`)
        .join('')
      return `\n<ul class="md-ul">${items}</ul>\n`
    },
  )

  // 8. 段落：空行分隔的块；已经是 <h*>/<ul>/<pre>/占位符 的块原样保留
  text = text
    .split(/\n{2,}/)
    .map((chunk) => {
      const t = chunk.trim()
      if (!t) return ''
      if (/^<(?:h\d|ul|pre|blockquote)/.test(t)) return t
      if (/^\u0000CB\d+\u0000$/.test(t)) return t
      return `<p class="md-p">${chunk.replace(/\n/g, '<br/>')}</p>`
    })
    .join('\n')

  // 9. 还原代码块占位符
  text = text.replace(/\u0000CB(\d+)\u0000/g, (_m, i) => codeBlocks[+i] || '')
  text = text.replace(/\u0000IC(\d+)\u0000/g, (_m, i) => inlineCodes[+i] || '')

  return text
}

const finalAnswerHtml = computed(() => renderMarkdown(finalAnswerText.value))

// ── F1+F2: Final Answer 工具栏 / 折叠 ─────────────────────────────────
// 每次任务重置时重新折叠；用户主动展开后保持展开直到下一次任务。
const FINAL_ANSWER_COLLAPSE_THRESHOLD = 600 // 字符数；> 阈值默认折叠
const FINAL_ANSWER_COLLAPSED_PREVIEW = 480  // 折叠时只渲染前 N 个字符
const finalAnswerExpanded = ref(false)
const finalAnswerCopyState = ref('idle') // 'idle' | 'ok' | 'err'
let finalAnswerCopyTimer = null // F1: reset-to-idle debounce; cleared on unmount

const finalAnswerCharCount = computed(() => (finalAnswerText.value || '').length)
const finalAnswerLineCount = computed(() => {
  const t = finalAnswerText.value || ''
  if (!t) return 0
  return t.split(/\r?\n/).length
})
const isFinalAnswerLong = computed(
  () => finalAnswerCharCount.value > FINAL_ANSWER_COLLAPSE_THRESHOLD,
)
// 折叠时渲染的 markdown HTML（截断到预览长度，保留段落边界）
const displayedFinalAnswerHtml = computed(() => {
  const t = finalAnswerText.value || ''
  if (!isFinalAnswerLong.value || finalAnswerExpanded.value) {
    return finalAnswerHtml.value
  }
  // 在 preview 边界附近寻找最近的段落断点（双换行 / 句号）以避免突兀截断
  const cap = FINAL_ANSWER_COLLAPSED_PREVIEW
  let cut = cap
  const slack = Math.min(120, t.length - cap)
  if (slack > 0) {
    const window = t.slice(cap, cap + slack)
    const para = window.search(/\n\s*\n/)
    if (para !== -1) cut = cap + para
    else {
      const period = window.search(/[。.!?！？]\s/)
      if (period !== -1) cut = cap + period + 1
    }
  }
  return renderMarkdown(t.slice(0, cut))
})

const copyFinalAnswerToClipboard = async () => {
  const t = finalAnswerText.value || ''
  if (!t) return
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(t)
    } else {
      // Fallback for non-secure context (e.g. http://localhost)
      const ta = document.createElement('textarea')
      ta.value = t
      ta.setAttribute('readonly', '')
      ta.style.position = 'absolute'
      ta.style.left = '-9999px'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    finalAnswerCopyState.value = 'ok'
  } catch (e) {
    finalAnswerCopyState.value = 'err'
  } finally {
    // F1: keep a handle so onUnmounted can cancel this reset; otherwise the
    // callback fires on a detached component (writes finalAnswerCopyState).
    if (finalAnswerCopyTimer) clearTimeout(finalAnswerCopyTimer)
    finalAnswerCopyTimer = setTimeout(() => {
      finalAnswerCopyTimer = null
      finalAnswerCopyState.value = 'idle'
    }, 1600)
  }
}

// F3: 域 → 卡片元数据（图标 + 标签 + 着色）
const FINAL_ANSWER_DOMAIN_META = {
  weather: { icon: '🌤️', label: '天气', accent: '#7ec8ff' },
  stock:   { icon: '📈', label: '股票', accent: '#7ce0a2' },
  recipe:  { icon: '🍳', label: '菜谱', accent: '#ffb877' },
  flight:  { icon: '✈️', label: '航班', accent: '#c89bff' },
}
const finalAnswerDomainMeta = computed(
  () => FINAL_ANSWER_DOMAIN_META[finalAnswerDomain.value] || null,
)

const exportFinalAnswerAsMarkdown = () => {
  const t = finalAnswerText.value || ''
  if (!t) return
  const ts = new Date()
  const stamp =
    `${ts.getFullYear()}${String(ts.getMonth() + 1).padStart(2, '0')}` +
    `${String(ts.getDate()).padStart(2, '0')}_${String(ts.getHours()).padStart(2, '0')}` +
    `${String(ts.getMinutes()).padStart(2, '0')}${String(ts.getSeconds()).padStart(2, '0')}`
  const blob = new Blob([t], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `final_answer_${stamp}.md`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

// appendLog now comes from createTerminalLogBuffer (see top of setup):
// synchronous push into a plain buffer, batched flush per frame.

// ── R: Live Terminal log line severity coloring ──────────────────────
// Inspect the log line's leading "[TAG]" and return a CSS modifier class.
// Only the first 32 chars are scanned (prefixes are short; a value containing
// "[ERROR" later in the message must NOT recolor a benign line).
// Matches both legacy tags ("[ERROR]", "[WARN]") and the G2/H1/I phase
// tags ("[PHASE]", "[PHASE/WARN]", "[PHASE/ERR]").
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

// ── X: Live Terminal in-content search ──────────────────────────────
//
// Three computeds form the search pipeline:
//
//   terminalSearchActive   → true when the bar is visible AND query
//                            has at least one non-whitespace char.
//                            Renders the highlight overlay only when
//                            this is true; otherwise log lines render
//                            as plain text (cheaper).
//
//   terminalSearchMatches  → flat array of {lineIdx, start, end} in
//                            the order matches appear (top-to-bottom,
//                            left-to-right). Drives the "x of y"
//                            counter and the n / Shift+n nav.
//
//   terminalSearchSegments → Map(lineIdx → list of {text, kind})
//                            consumed by the v-for renderer. ``kind``
//                            is one of 'plain' | 'hit' | 'current'.
//
// We compute matches case-insensitively because users typing "[error"
// don't want to also type the bracket-aware case. Regex is escaped so
// literal special chars (.*?+()|^$) don't accidentally match nothing.
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
  for (let li = 0; li < logs.value.length; li += 1) {
    const line = String(logs.value[li] || '').toLowerCase()
    if (!q.length) continue
    let from = 0
    while (from < line.length) {
      const idx = line.indexOf(q, from)
      if (idx === -1) break
      out.push({ lineIdx: li, start: idx, end: idx + q.length })
      // Step forward by at least 1 even when the query is empty (shouldn't
      // happen because of the active guard above, but defensive).
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
  // Group matches by line for O(N) per-line splitting.
  const byLine = new Map()
  for (let i = 0; i < matches.length; i += 1) {
    const m = matches[i]
    if (!byLine.has(m.lineIdx)) byLine.set(m.lineIdx, [])
    byLine.get(m.lineIdx).push({ ...m, gIdx: i })
  }
  const cur = terminalSearchCurrent.value
  for (const [lineIdx, list] of byLine.entries()) {
    const line = String(logs.value[lineIdx] || '')
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
  // nextTick + DOM dive: the el-input is rendered conditionally by
  // v-if, so we have to wait for Vue to mount it before .focus()
  // can hit a real element.
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
    } catch (err) {
      // Quiet — focus failures aren't fatal
    }
  })
}

const closeTerminalSearch = () => {
  terminalSearchVisible.value = false
  // Keep the query around so re-opening with Ctrl+F shows the previous
  // search (matches Chrome/VS Code behavior). Reset the cursor though
  // so a re-open with stale query lands on the first match.
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

// Watch the ref itself (not .length): the log buffer replaces the array on
// every flush, so this fires even when ring-trim keeps length constant at
// LOG_LIMIT while matched lines get trimmed away.
watch(logs, () => {
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

const connectWebSocket = () => {
  if (isUnmounted) return
  if (socket && socket.readyState === WebSocket.OPEN) return
  if (socket && socket.readyState === WebSocket.CONNECTING) return

  wsStatus.value = 'connecting'
  socket = new WebSocket(wsUrl('/ws/logs'))

  socket.onopen = () => {
    wsStatus.value = 'connected'
    appendLog('[SYSTEM] WebSocket connected')
  }

  socket.onmessage = async (event) => {
    try {
      const payload = JSON.parse(event.data)
      if (payload.type === 'log') {
        const level = payload.level ? String(payload.level).toUpperCase() : 'INFO'
        const content = payload.content || ''
        await appendLog(`[${level}] ${content}`)
        return
      }

      if (payload.type === 'image' || payload.type === 'screenshot') {
        currentImageBase64.value = payload.data || ''
        return
      }

      if (payload.type === 'done') {
        isRunning.value = false
        isHumanInterventionRequired.value = false
        fetchBrowserRuntimeStatus()
        runHistoryRefreshToken.value += 1
        if (activeBottomTab.value !== 'runs') {
          hasNewRuns.value = true
        }
        const text = payload.message || (payload.success ? '任务执行完成' : '任务执行结束')
        await appendLog(`[DONE] ${text}`)

        // K3: when the run failed, refetch the failed-runs list so the
        // drawer has the freshest entry. Pulse the badge dot if the user
        // isn't already viewing the panel. Fire-and-forget — we don't
        // await it so the rest of the done-handler stays responsive.
        if (payload.success === false) {
          fetchFailedRuns()
          if (activeBottomTab.value !== 'failed') {
            hasNewFailures.value = true
          }
        }

        // ── Final Answer：解析后端结果类型 ──
        // 优先用后端显式字段 (payload.answer_type / payload.answer)；
        // 若缺失则使用兜底启发式：本次运行有新 artifact 产生 → 视为 'file'，
        // 否则视为 'text'，并把 message 当成纯文本答案展示。
        let answerType =
          typeof payload.answer_type === 'string' && payload.answer_type
            ? payload.answer_type
            : null
        const answerText =
          typeof payload.answer === 'string' ? payload.answer : ''
        const hasExplicitAnswerType =
          typeof payload.answer_type === 'string' && payload.answer_type
        const hasExplicitAnswer = answerText.trim().length > 0
        const hasExistingTextAnswer =
          taskResult.value &&
          taskResult.value.type === 'text' &&
          typeof finalAnswerText.value === 'string' &&
          finalAnswerText.value.trim().length > 0
        if (!hasExplicitAnswerType && !hasExplicitAnswer && hasExistingTextAnswer) {
          return
        }
        if (!answerType) {
          const artifactsGrew =
            artifactList.value.length > artifactsCountAtSubmit
          answerType = artifactsGrew ? 'file' : 'text'
        }
        const fallbackText = answerText || (answerType === 'text' ? text : '')
        finalAnswerText.value = fallbackText
        finalAnswerStatus.value = answerType === 'file' ? 'file' : 'text'
        // F3: domain hint from backend (only meaningful when answerType==='text')
        const rawDomain =
          typeof payload.answer_domain === 'string' ? payload.answer_domain : ''
        const ALLOWED_DOMAINS = ['weather', 'stock', 'recipe', 'flight']
        finalAnswerDomain.value =
          answerType === 'text' && ALLOWED_DOMAINS.includes(rawDomain) ? rawDomain : ''
        taskResult.value = { type: answerType, answer: fallbackText }
        return
      }

      // G2/H1/I/L: phase events — agent timeline ticks
      if (payload.type === 'phase') {
        const phase = String(payload.phase || 'unknown')
        const sev = String(payload.severity || 'info')
        const dur = Number.isFinite(payload.duration_ms) ? `${payload.duration_ms}ms` : ''
        const stepStr = Number.isFinite(payload.step) ? `step ${payload.step}` : ''
        const msg = String(payload.message || '')
        // Compose: [PHASE/info] som_inject (step 3, 1240ms): 87 elements / 2 frames
        const tag = sev === 'warn' ? '[PHASE/WARN]' : sev === 'error' ? '[PHASE/ERR]' : '[PHASE]'
        const head = `${tag} ${phase}`
        const meta = [stepStr, dur].filter(Boolean).join(', ')
        const tail = msg ? `: ${msg}` : ''
        await appendLog(meta ? `${head} (${meta})${tail}` : `${head}${tail}`)
        // M: also push into the Timeline panel buffer. Keep the array
        // bounded so a 200-step run doesn't blow up memory.
        // W: drop incoming WS phase events while in replay mode so the
        //    imported buffer isn't contaminated. Live Terminal logs
        //    above this branch still stream normally — the user can
        //    confirm a new run is starting via the IDLE/RUNNING pill
        //    without losing their replay context.
        if (replayMode.value) {
          return
        }
        const evt = { ...payload, _ts: payload.ts || Date.now() / 1000 }
        phaseEvents.value.push(evt)
        if (phaseEvents.value.length > PHASE_LIMIT) {
          phaseEvents.value.splice(0, phaseEvents.value.length - PHASE_LIMIT)
        }
        if (
          ['capability_route', 'capability_execute'].includes(String(evt.phase || ''))
          && activeBottomTab.value !== 'capability'
        ) {
          hasNewCapability.value = true
        }
        if (activeBottomTab.value !== 'timeline') {
          hasNewPhase.value = true
        }
        // P: pin-to-bottom — only auto-scroll while the user is already
        // following the tail. If they scrolled up to read history, leave
        // them where they are and let the floating "回到底部" button
        // bring them back manually.
        if (timelineAutoScroll.value && activeBottomTab.value === 'timeline') {
          await scrollTimelineToBottom()
        }
        return
      }

      if (payload.type === 'status') {
        if (payload.status === 'new_artifact') {
          await fetchArtifacts()
          if (activeBottomTab.value !== 'artifacts') {
            hasNewArtifacts.value = true
          }
          await appendLog(`[ARTIFACT] ${payload.filename || 'new file'} ready`)
        }
        if (payload.status === 'human_intervention') {
          isHumanInterventionRequired.value = true
          humanInterventionReason.value = payload.reason || 'Agent 遇到需要人工处理的障碍'
          await appendLog(`[HITL] ${humanInterventionReason.value}`)
        }
        if (payload.status === 'human_resumed') {
          isHumanInterventionRequired.value = false
          humanInterventionReason.value = ''
          await appendLog('[HITL] Agent resumed')
        }
      }
    } catch (err) {
      await appendLog(`[WARN] 无法解析消息: ${String(err)}`)
    }
  }

  socket.onclose = () => {
    wsStatus.value = 'disconnected'
    appendLog('[SYSTEM] WebSocket disconnected')
    if (!isUnmounted) {
      if (reconnectTimer) clearTimeout(reconnectTimer)
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null
        connectWebSocket()
      }, 2000)
    }
  }

  socket.onerror = () => {
    wsStatus.value = 'error'
    appendLog('[ERROR] WebSocket error')
  }
}

const handleUploadChange = (uploadFile, uploadFiles) => {
  if (!uploadFile || !uploadFile.raw) {
    selectedFile.value = null
    attachmentIntent.value = ATTACHMENT_INTENT_AUTO
    return
  }

  selectedFile.value = uploadFile.raw
  attachmentIntent.value = ATTACHMENT_INTENT_AUTO
  if (uploadFiles.length > 1) {
    uploadFiles.splice(0, uploadFiles.length - 1)
  }
}

const handleUploadRemove = () => {
  selectedFile.value = null
  attachmentIntent.value = ATTACHMENT_INTENT_AUTO
}

const loadAuthProfiles = async () => {
  try {
    const response = await apiFetch('/api/auth/profiles')
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.message || '加载 Auth Profiles 失败')
    }
    authProfileOptions.value = result.profiles || []
  } catch (err) {
    await appendLog(`[WARN] 加载 Auth Profiles 失败: ${String(err)}`)
  }
}

const loadCaptchaSolverStatus = async () => {
  try {
    const response = await apiFetch('/api/runtime/captcha_solver')
    const result = await response.json()
    if (!response.ok || result.status !== 'success') return
    captchaSolverEnabled.value = Boolean(result.enabled)
    captchaSolverProvider.value = String(result.provider || '')
  } catch (err) {
    // non-critical
    console.warn('[captcha_solver] status fetch failed:', err)
  }
}

// ── K3: failed-runs fetch helpers ─────────────────────────────────────
// Backend returns {status, count, items}. Items already arrive newest-
// first courtesy of failure_archive.list_failed_runs.
const fetchFailedRuns = async () => {
  if (failedRunsLoading.value) return
  failedRunsLoading.value = true
  try {
    const response = await apiFetch('/api/failed_runs?limit=50')
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.message || '加载失败 run 列表失败')
    }
    failedRunsList.value = Array.isArray(result.items) ? result.items : []
    if (activeBottomTab.value === 'failed') {
      hasNewFailures.value = false
    }
  } catch (err) {
    // Quiet: this panel is non-critical and the API is brand new (K2).
    // A fetch failure shouldn't surface a toast each time the WS reconnects.
    // eslint-disable-next-line no-console
    console.warn('[failed_runs] fetch failed:', err)
  } finally {
    failedRunsLoading.value = false
  }
}

// Format a unix-epoch ts (seconds, possibly fractional) as
// "MM-DD HH:MM:SS" — short enough for the table column, still
// unambiguous within a year. Falls back to '—' on bad input.
const formatFailedRunTime = (ts) => {
  if (!Number.isFinite(ts)) return '—'
  try {
    const d = new Date(ts * 1000)
    const pad = (n) => String(n).padStart(2, '0')
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
      + `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  } catch (err) {
    return '—'
  }
}

// Format duration_s as "12.3s" / "1m24s" / "—". Keep it terse so the
// column doesn't blow out on long runs.
const formatFailedRunDuration = (sec) => {
  if (!Number.isFinite(sec) || sec < 0) return '—'
  if (sec < 60) return `${sec.toFixed(1)}s`
  const m = Math.floor(sec / 60)
  const s = Math.round(sec - m * 60)
  return `${m}m${String(s).padStart(2, '0')}s`
}

// Open the HTML trajectory log for a failed run in a new browser tab.
// FastAPI route is /api/failed_runs/{run_id}/log — it 404s if the file
// was deleted, in which case we fall back to a friendly message instead
// of the raw "Detail" JSON body.
const openFailedRunLog = (rec) => {
  if (!rec || !rec.run_id) return
  // Defensive: if the backend already told us the log is gone, don't
  // even open the tab — show a toast.
  const exists = rec.paths_exist && rec.paths_exist.html_log
  if (exists === false) {
    ElMessage.warning('HTML 日志文件已被清理，无法打开')
    return
  }
  const url = `${API_BASE}/api/failed_runs/${encodeURIComponent(rec.run_id)}/log`
  window.open(url, '_blank', 'noopener')
}

// ── K6: failed-run detail dialog helpers ──────────────────────────────

// Pretty-print the currently-selected record's JSON. Two-space indent
// matches the on-disk archive format so a copy from this dialog is a
// drop-in for ``cat runs/failed/<id>.json | jq .``.
const selectedFailedRunJson = computed(() => {
  if (!selectedFailedRun.value) return ''
  try {
    return JSON.stringify(selectedFailedRun.value, null, 2)
  } catch (err) {
    return String(err)
  }
})

// Fetch the historic phase events for this run from the K6 backend
// endpoint. Best-effort: a 404 means the .jsonl file was rotated /
// cleared, which we surface via failedRunPhaseStatus='missing' (not
// 'error') so the UI shows a friendly "已被清理" message instead of a
// red banner.
const fetchFailedRunPhaseEvents = async (rec) => {
  if (!rec || !rec.run_id) {
    failedRunPhaseStatus.value = 'idle'
    failedRunPhaseEvents.value = []
    return
  }
  // Skip the request entirely when paths_exist already says no.
  const exists = rec.paths_exist && rec.paths_exist.phase_jsonl
  if (exists === false) {
    failedRunPhaseStatus.value = 'missing'
    failedRunPhaseEvents.value = []
    failedRunPhaseTotal.value = 0
    failedRunPhaseTruncated.value = false
    return
  }
  failedRunPhaseStatus.value = 'loading'
  failedRunPhaseEvents.value = []
  failedRunPhaseTotal.value = 0
  failedRunPhaseTruncated.value = false
  try {
    const url = `${API_BASE}/api/failed_runs/${encodeURIComponent(rec.run_id)}/phase_events?limit=200`
    const response = await fetch(url)
    if (response.status === 404) {
      failedRunPhaseStatus.value = 'missing'
      return
    }
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.detail || result.message || '加载 phase 事件失败')
    }
    failedRunPhaseEvents.value = Array.isArray(result.events) ? result.events : []
    failedRunPhaseTotal.value = Number.isFinite(result.total) ? result.total : 0
    failedRunPhaseTruncated.value = Boolean(result.truncated)
    failedRunPhaseStatus.value = 'ok'
  } catch (err) {
    // Quiet the console — this panel is non-critical. Display the
    // error inside the dialog instead of bubbling a toast that would
    // distract from the JSON the user already came here to read.
    // eslint-disable-next-line no-console
    console.warn('[failed_runs] phase events fetch failed:', err)
    failedRunPhaseStatus.value = 'error'
  }
}

// Open the detail dialog for a clicked row. Idempotent — calling it
// with the same rec just refreshes the phase preview, which is what the
// "刷新" button in the dialog does.
const openFailedRunDetail = (rec) => {
  if (!rec) return
  selectedFailedRun.value = rec
  failedRunDialogVisible.value = true
  fetchFailedRunPhaseEvents(rec)
}

const closeFailedRunDetail = () => {
  failedRunDialogVisible.value = false
  // Don't clear selectedFailedRun immediately — the dialog's close
  // transition reads it while fading out. Vue handles the GC.
}

// Prev/next navigation within the dialog so the user can step through
// the failure list without closing+reopening. Uses the current order of
// failedRunsList (already newest-first per the backend).
const _findCurrentFailedRunIndex = () => {
  const cur = selectedFailedRun.value
  if (!cur) return -1
  const list = failedRunsList.value
  let idx = list.indexOf(cur)
  if (idx !== -1) return idx
  // Fall back to run_id match for cases where the list was refetched
  // (object identity changes even though it's the same logical record).
  const curRid = cur.run_id
  for (let i = 0; i < list.length; i += 1) {
    if (list[i] && list[i].run_id === curRid) return i
  }
  return -1
}

const goToPrevFailedRun = () => {
  const list = failedRunsList.value
  if (!list.length) return
  const idx = _findCurrentFailedRunIndex()
  const next = idx <= 0 ? list.length - 1 : idx - 1
  openFailedRunDetail(list[next])
}

const goToNextFailedRun = () => {
  const list = failedRunsList.value
  if (!list.length) return
  const idx = _findCurrentFailedRunIndex()
  const next = idx === -1 || idx >= list.length - 1 ? 0 : idx + 1
  openFailedRunDetail(list[next])
}

// Copy the selected record's JSON to clipboard. Same fallback chain
// as copyPhaseJson + the F1 Final Answer copy button.
const copyFailedRunJson = async () => {
  const text = selectedFailedRunJson.value
  if (!text) return
  const ok = await _writeToClipboard(text)
  if (ok) {
    ElMessage.success('已复制 JSON')
  } else {
    ElMessage.error('复制失败：浏览器拒绝了剪贴板写入')
  }
}

// Helpers shared with the phase preview list. We keep them tiny so the
// template stays declarative.
const formatPhasePreviewSeverity = (sev) => {
  const s = String(sev || 'info').toLowerCase()
  if (s === 'warn' || s === 'warning') return 'warn'
  if (s === 'error' || s === 'err') return 'error'
  return 'info'
}

const formatPhasePreviewTs = (ts) => {
  if (!Number.isFinite(ts)) return ''
  try {
    const d = new Date(ts * 1000)
    const pad = (n) => String(n).padStart(2, '0')
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  } catch (err) {
    return ''
  }
}

const fetchArtifacts = async () => {
  try {
    const response = await apiFetch('/api/artifacts')
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.message || '加载产出文件失败')
    }
    artifactList.value = result.files || []
    if (activeBottomTab.value === 'artifacts') {
      hasNewArtifacts.value = false
    }
  } catch (err) {
    await appendLog(`[WARN] 加载产出文件失败: ${String(err)}`)
  }
}

const fetchBrowserRuntimeStatus = async () => {
  if (browserRuntimeLoading.value) return
  browserRuntimeLoading.value = true
  try {
    const response = await apiFetch('/api/browser_pool')
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.message || '加载浏览器运行时状态失败')
    }
    browserRuntimeStatus.value = result.runtime || null
  } catch (err) {
    await appendLog(`[WARN] 加载浏览器运行时状态失败: ${String(err)}`)
  } finally {
    browserRuntimeLoading.value = false
  }
}

const useAuthProfile = (name) => {
  if (!name) return
  if (!selectedAuthProfiles.value.includes(name)) {
    selectedAuthProfiles.value = [...selectedAuthProfiles.value, name]
  }
}

const startManualAuth = async () => {
  const target = (authLoginUrl.value || url.value).trim()
  if (!target) {
    ElMessage.warning('请先填写登录 URL 或目标 URL')
    return
  }

  const formData = new FormData()
  formData.append('target_url', target)
  if (authProfileName.value.trim()) {
    formData.append('profile', authProfileName.value.trim())
  }

  try {
    const response = await apiFetch('/api/auth/manual/start', {
      method: 'POST',
      body: formData,
    })
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.message || '打开人工登录窗口失败')
    }
    isAuthRecording.value = true
    authProfileName.value = result.profile || authProfileName.value
    useAuthProfile(result.profile)
    ElMessage.success('登录窗口已打开')
    await appendLog(`[AUTH] 登录窗口已打开，完成登录后点击保存: ${result.profile}`)
  } catch (err) {
    ElMessage.error(`打开登录窗口失败: ${String(err)}`)
    await appendLog(`[ERROR] 打开登录窗口失败: ${String(err)}`)
  }
}

const saveManualAuth = async () => {
  try {
    const response = await apiFetch('/api/auth/manual/save', {
      method: 'POST',
    })
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.message || '保存登录态失败')
    }
    isAuthRecording.value = false
    useAuthProfile(result.profile)
    await loadAuthProfiles()
    ElMessage.success('登录态已保存')
    await appendLog(`[AUTH] ${result.message}`)
  } catch (err) {
    ElMessage.error(`保存登录态失败: ${String(err)}`)
    await appendLog(`[ERROR] 保存登录态失败: ${String(err)}`)
  }
}

const cancelManualAuth = async () => {
  try {
    const response = await apiFetch('/api/auth/manual/cancel', {
      method: 'POST',
    })
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.message || '取消登录态录制失败')
    }
    isAuthRecording.value = false
    ElMessage.info('已取消登录态录制')
    await appendLog(`[AUTH] ${result.message}`)
  } catch (err) {
    ElMessage.error(`取消失败: ${String(err)}`)
    await appendLog(`[ERROR] 取消登录态录制失败: ${String(err)}`)
  }
}

const resumeAgentExecution = async () => {
  try {
    const response = await apiFetch('/api/human/resume', {
      method: 'POST',
    })
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.message || '恢复执行失败')
    }
    isHumanInterventionRequired.value = false
    humanInterventionReason.value = ''
    ElMessage.success('已发送恢复执行信号')
    await appendLog('[HITL] Resume signal sent')
  } catch (err) {
    ElMessage.error(`恢复执行失败: ${String(err)}`)
    await appendLog(`[ERROR] 恢复执行失败: ${String(err)}`)
  }
}

// ── M: Phase timeline computeds & helpers ─────────────────────────────
// O: filteredPhaseEvents applies the exclude filters (phase + severity)
// before grouping. Everything downstream (grouping, summary stats inside
// the toolbar) operates on this view, so unchecking "vlm_call" instantly
// hides those chips.
const filteredPhaseEvents = computed(() => {
  const exP = phaseFilterExclude.value
  const exS = severityFilterExclude.value
  if (exP.size === 0 && exS.size === 0) return phaseEvents.value
  return phaseEvents.value.filter((e) => {
    const p = String(e.phase || 'unknown')
    const s = String(e.severity || 'info')
    return !exP.has(p) && !exS.has(s)
  })
})

const latestCapabilityRoute = computed(() => {
  for (let i = phaseEvents.value.length - 1; i >= 0; i -= 1) {
    const evt = phaseEvents.value[i]
    if (evt && String(evt.phase || '') === 'capability_route') return evt
  }
  return null
})

const latestCapabilityExecute = computed(() => {
  for (let i = phaseEvents.value.length - 1; i >= 0; i -= 1) {
    const evt = phaseEvents.value[i]
    if (evt && String(evt.phase || '') === 'capability_execute') return evt
  }
  return null
})

const latestCompletionGuard = computed(() => {
  for (let i = phaseEvents.value.length - 1; i >= 0; i -= 1) {
    const evt = phaseEvents.value[i]
    if (evt && String(evt.phase || '') === 'completion_guard') return evt
  }
  return null
})

const completionEvidence = computed(() => {
  const evt = latestCompletionGuard.value
  if (!evt) return null
  const evaluation = evt.evaluation && typeof evt.evaluation === 'object'
    ? evt.evaluation
    : {}
  const state = evt.state && typeof evt.state === 'object' ? evt.state : {}
  return {
    guard: String(evt.guard || 'completion'),
    message: String(evt.message || ''),
    status: String(evaluation.status || ''),
    confidence: evaluation.confidence,
    evidence: Array.isArray(evaluation.evidence) ? evaluation.evidence : [],
    reasons: Array.isArray(evaluation.reasons) ? evaluation.reasons : [],
    checks: Array.isArray(evaluation.checks) ? evaluation.checks : [],
    streak: state.streak ?? evaluation.subgoal_exit?.streak,
    step: evt.step,
  }
})

const capabilityTraceEvents = computed(() =>
  phaseEvents.value.filter((evt) =>
    ['capability_route', 'capability_execute'].includes(String(evt?.phase || '')),
  ),
)

const capabilityIntent = computed(() => latestCapabilityRoute.value?.intent || {})
const capabilityBackendPlan = computed(() =>
  Array.isArray(latestCapabilityRoute.value?.backend_plan)
    ? latestCapabilityRoute.value.backend_plan
    : [],
)
const capabilityFallbackChain = computed(() =>
  Array.isArray(latestCapabilityRoute.value?.fallback_chain)
    ? latestCapabilityRoute.value.fallback_chain
    : [],
)
const capabilityExecutionPlanSteps = computed(() =>
  Array.isArray(latestCapabilityRoute.value?.execution_plan?.steps)
    ? latestCapabilityRoute.value.execution_plan.steps
    : [],
)
const capabilityWorkflowGraph = computed(() => latestCapabilityRoute.value?.workflow_graph || {})
const capabilityRuntimePreflight = computed(() => latestCapabilityRoute.value?.runtime_preflight || {})
const capabilityRouteCrawlEfficiencyPlan = computed(() =>
  capabilityEventCrawlEfficiencyPlan(latestCapabilityRoute.value),
)
const capabilityWorkflowNodes = computed(() =>
  Array.isArray(capabilityWorkflowGraph.value?.nodes)
    ? capabilityWorkflowGraph.value.nodes
    : [],
)
const capabilityActionRefSchema = computed(() => latestCapabilityRoute.value?.action_ref_schema || {})
const capabilityManifestSummary = computed(() =>
  Array.isArray(latestCapabilityRoute.value?.capability_manifest)
    ? latestCapabilityRoute.value.capability_manifest
    : [],
)
const capabilityModelRoles = computed(() => latestCapabilityRoute.value?.model_roles || {})
const capabilityAuditFindings = computed(() =>
  Array.isArray(latestCapabilityRoute.value?.audit?.findings)
    ? latestCapabilityRoute.value.audit.findings
    : [],
)
const capabilityTraceJson = computed(() =>
  latestCapabilityRoute.value ? buildPhaseEventJsonString(latestCapabilityRoute.value) : '',
)
const capabilityExecutionAttempts = computed(() =>
  Array.isArray(latestCapabilityExecute.value?.attempts)
    ? latestCapabilityExecute.value.attempts
    : [],
)
const capabilityExecutionVerification = computed(() =>
  latestCapabilityExecute.value?.verification || {},
)
const capabilityExecutionRuntimeSummary = computed(() =>
  latestCapabilityExecute.value?.runtime_summary || {},
)
const capabilityExecutionRuntimeAfter = computed(() =>
  capabilityExecutionRuntimeSummary.value?.after || {},
)
const capabilityExecutionRuntimeDrift = computed(() =>
  latestCapabilityExecute.value?.runtime_drift || {},
)
const capabilityExecutionRuntimeIssueSummary = computed(() =>
  latestCapabilityExecute.value?.runtime_issue_summary || {},
)
const capabilityExecutionRuntimeIssues = computed(() =>
  Array.isArray(capabilityExecutionRuntimeIssueSummary.value?.issues)
    ? capabilityExecutionRuntimeIssueSummary.value.issues
    : [],
)
const capabilityExecutionRuntimeActions = computed(() =>
  Array.isArray(capabilityExecutionRuntimeIssueSummary.value?.recommended_actions)
    ? capabilityExecutionRuntimeIssueSummary.value.recommended_actions
      .map((action) => String(action || ''))
      .filter((action) => action && action !== 'continue')
    : [],
)
const capabilityExecutionActionTrace = computed(() =>
  capabilityEventActionTrace(latestCapabilityExecute.value),
)
const capabilityExecutionActionIssueSummary = computed(() =>
  capabilityEventActionIssueSummary(latestCapabilityExecute.value, capabilityExecutionActionTrace.value),
)
const capabilityExecutionActionIssues = computed(() =>
  Array.isArray(capabilityExecutionActionIssueSummary.value?.issues)
    ? capabilityExecutionActionIssueSummary.value.issues
    : [],
)
const capabilityExecutionActionIssueActions = computed(() =>
  Array.isArray(capabilityExecutionActionIssueSummary.value?.recommended_actions)
    ? capabilityExecutionActionIssueSummary.value.recommended_actions
      .map((action) => String(action || ''))
      .filter((action) => action && action !== 'continue')
    : [],
)
const capabilityExecutionActionFailureSummary = computed(() =>
  capabilityExecutionActionTrace.value?.result_summary || {},
)
const capabilityExecutionActionRecoveryActions = computed(() =>
  Array.isArray(capabilityExecutionActionFailureSummary.value?.recovery_actions)
    ? capabilityExecutionActionFailureSummary.value.recovery_actions
      .map((action) => String(action || ''))
      .filter((action) => action && action !== 'continue')
    : [],
)
const capabilityExecutionFailureBundle = computed(() =>
  capabilityEventFailureBundle(latestCapabilityExecute.value),
)
const capabilityExecutionCrawlEfficiencyPlan = computed(() =>
  capabilityEventCrawlEfficiencyPlan(latestCapabilityExecute.value),
)
const capabilityActiveCrawlEfficiencyPlan = computed(() =>
  capabilityExecutionCrawlEfficiencyPlan.value?.version
    ? capabilityExecutionCrawlEfficiencyPlan.value
    : capabilityRouteCrawlEfficiencyPlan.value,
)
const capabilityExecutionCrawlEfficiencyCandidates = computed(() =>
  Array.isArray(capabilityActiveCrawlEfficiencyPlan.value?.candidates)
    ? capabilityActiveCrawlEfficiencyPlan.value.candidates
    : [],
)
const capabilityExecutionCrawlEfficiencyAvailablePaths = computed(() =>
  Array.isArray(capabilityActiveCrawlEfficiencyPlan.value?.available_paths)
    ? capabilityActiveCrawlEfficiencyPlan.value.available_paths.map((item) => String(item || '')).filter(Boolean)
    : [],
)
const capabilityExecutionCrawlEfficiencySummary = computed(() =>
  capabilityActiveCrawlEfficiencyPlan.value?.efficiency_summary || {},
)
const capabilityExecutionEfficiencyCorrelationReport = computed(() =>
  capabilityEventEfficiencyCorrelationReport(latestCapabilityExecute.value),
)
const capabilityExecutionEfficiencyCorrelationAlignment = computed(() =>
  capabilityExecutionEfficiencyCorrelationReport.value?.alignment || {},
)
const capabilityExecutionEfficiencyCorrelationRootCauses = computed(() =>
  Array.isArray(capabilityExecutionEfficiencyCorrelationReport.value?.root_causes)
    ? capabilityExecutionEfficiencyCorrelationReport.value.root_causes
    : [],
)
const capabilityExecutionEfficiencyCorrelationPlannerHints = computed(() =>
  Array.isArray(capabilityExecutionEfficiencyCorrelationReport.value?.planner_hints)
    ? capabilityExecutionEfficiencyCorrelationReport.value.planner_hints
    : [],
)
const capabilityExecutionEfficiencyCorrelationActions = computed(() =>
  Array.isArray(capabilityExecutionEfficiencyCorrelationReport.value?.recommended_actions)
    ? capabilityExecutionEfficiencyCorrelationReport.value.recommended_actions.map((action) => String(action || '')).filter(Boolean)
    : [],
)
const capabilityEfficiencyFeedbackReplayChecks = computed(() =>
  Array.isArray(capabilityEfficiencyFeedbackReplayReport.value?.checks)
    ? capabilityEfficiencyFeedbackReplayReport.value.checks
    : [],
)
const capabilityEfficiencyFeedbackReplayFailedChecks = computed(() =>
  capabilityEfficiencyFeedbackReplayChecks.value.filter((check) => !check?.passed),
)
const capabilityEfficiencyFeedbackReplayStatus = computed(() =>
  capabilityEfficiencyFeedbackReplayReport.value?.passed ? 'passed' : 'failed',
)
const capabilityEfficiencyFeedbackReplayPlannerFeedback = computed(() =>
  capabilityEfficiencyFeedbackReplayReport.value?.planner_feedback || {},
)
const capabilityEfficiencyFeedbackReplayPreferredCapabilities = computed(() =>
  Array.isArray(capabilityEfficiencyFeedbackReplayPlannerFeedback.value?.preferred_capabilities)
    ? capabilityEfficiencyFeedbackReplayPlannerFeedback.value.preferred_capabilities.map((item) => String(item || '')).filter(Boolean)
    : [],
)
const capabilityEfficiencyFeedbackReplayAvoidActions = computed(() =>
  Array.isArray(capabilityEfficiencyFeedbackReplayPlannerFeedback.value?.avoid_actions)
    ? capabilityEfficiencyFeedbackReplayPlannerFeedback.value.avoid_actions.map((item) => String(item || '')).filter(Boolean)
    : [],
)
const capabilityEfficiencyFeedbackReplayLibraryCount = computed(() =>
  capabilityEfficiencyFeedbackReplayLibrary.value.length,
)
const capabilityFailureFixtureReplayChecks = computed(() =>
  Array.isArray(capabilityFailureFixtureReplayReport.value?.checks)
    ? capabilityFailureFixtureReplayReport.value.checks
    : [],
)
const capabilityFailureFixtureReplayFailedChecks = computed(() =>
  capabilityFailureFixtureReplayChecks.value.filter((check) => !check?.passed),
)
const capabilityFailureFixtureReplayStatus = computed(() =>
  capabilityFailureFixtureReplayReport.value?.passed ? 'passed' : 'failed',
)
const capabilityFailureFixtureLibraryCount = computed(() =>
  capabilityFailureFixtureLibrary.value.length,
)
const capabilityFailureFixtureBatchHistoryCount = computed(() =>
  capabilityFailureFixtureBatchHistory.value.length,
)
const capabilityFailureFixtureLatestBatchHistory = computed(() =>
  capabilityFailureFixtureBatchHistory.value[0] || null,
)
const capabilityFailureFixtureBatchHistoryTrendDirection = computed(() =>
  capabilityFailureFixtureBatchHistoryTrend.value?.direction || 'empty',
)
const capabilityFailureFixtureBatchHistoryTrendPassRateDelta = computed(() =>
  Math.round(Number(capabilityFailureFixtureBatchHistoryTrend.value?.pass_rate_delta || 0) * 1000) / 10,
)
const capabilityFailureFixtureBatchReplayItems = computed(() =>
  Array.isArray(capabilityFailureFixtureBatchReplayReport.value?.items)
    ? capabilityFailureFixtureBatchReplayReport.value.items
    : [],
)
const capabilityFailureFixtureBatchReplayFailedItems = computed(() =>
  capabilityFailureFixtureBatchReplayItems.value.filter((item) => !item?.passed),
)
const capabilityFailureFixtureBatchReplayFailedChecks = computed(() =>
  Array.isArray(capabilityFailureFixtureBatchReplayReport.value?.failed_checks)
    ? capabilityFailureFixtureBatchReplayReport.value.failed_checks
    : [],
)
const capabilityFailureFixtureBatchReplaySummary = computed(() =>
  capabilityFailureFixtureBatchReplayReport.value?.summary || {},
)
const capabilityFailureFixtureBatchReplayTopPrimaryFailures = computed(() =>
  Array.isArray(capabilityFailureFixtureBatchReplaySummary.value?.top_primary_failures)
    ? capabilityFailureFixtureBatchReplaySummary.value.top_primary_failures
    : [],
)
const capabilityFailureFixtureBatchReplayTopFailureCategories = computed(() =>
  Array.isArray(capabilityFailureFixtureBatchReplaySummary.value?.top_failure_categories)
    ? capabilityFailureFixtureBatchReplaySummary.value.top_failure_categories
    : [],
)
const capabilityFailureFixtureBatchReplayTopFailedChecks = computed(() =>
  Array.isArray(capabilityFailureFixtureBatchReplaySummary.value?.top_failed_checks)
    ? capabilityFailureFixtureBatchReplaySummary.value.top_failed_checks
    : [],
)
const capabilityFailureFixtureBatchReplayStatus = computed(() =>
  capabilityFailureFixtureBatchReplayReport.value?.passed ? 'passed' : 'failed',
)
const capabilityExecutionChecks = computed(() =>
  Array.isArray(capabilityExecutionVerification.value?.checks)
    ? capabilityExecutionVerification.value.checks
    : [],
)
const capabilityExecuteJson = computed(() =>
  latestCapabilityExecute.value ? buildPhaseEventJsonString(latestCapabilityExecute.value) : '',
)
const capabilityTraceRows = computed(() => capabilityTraceEvents.value.map((evt, idx) => {
  const phase = String(evt?.phase || 'unknown')
  const runtimePreflight = evt?.runtime_preflight || {}
  const runtimeIssueSummary = evt?.runtime_issue_summary || {}
  const runtimeDrift = evt?.runtime_drift || {}
  const actionTrace = capabilityEventActionTrace(evt)
  const actionIssueSummary = capabilityEventActionIssueSummary(evt, actionTrace)
  const traceArtifact = capabilityEventTraceArtifact(evt)
  const crawlEfficiencyPlan = capabilityEventCrawlEfficiencyPlan(evt)
  const crawlEfficiencyCandidates = Array.isArray(crawlEfficiencyPlan?.candidates)
    ? crawlEfficiencyPlan.candidates
    : []
  const crawlEfficiencyAvailablePaths = Array.isArray(crawlEfficiencyPlan?.available_paths)
    ? crawlEfficiencyPlan.available_paths.map((item) => String(item || '')).filter(Boolean)
    : []
  const efficiencyCorrelationReport = capabilityEventEfficiencyCorrelationReport(evt)
  const efficiencyCorrelationAlignment = efficiencyCorrelationReport?.alignment || {}
  const efficiencyCorrelationRootCauses = Array.isArray(efficiencyCorrelationReport?.root_causes)
    ? efficiencyCorrelationReport.root_causes.map((item) => String(item || '')).filter(Boolean)
    : []
  const efficiencyCorrelationActions = Array.isArray(efficiencyCorrelationReport?.recommended_actions)
    ? efficiencyCorrelationReport.recommended_actions.map((item) => String(item || '')).filter(Boolean)
    : []
  const efficiencyCorrelationStatus = String(efficiencyCorrelationReport?.status || 'unknown')
  const actionResultSummary = actionTrace?.result_summary || {}
  const actionFailureCode = String(actionResultSummary.failure_code || '')
  const actionFailureCategory = String(actionResultSummary.failure_category || '')
  const actionRecoveryActions = Array.isArray(actionResultSummary.recovery_actions)
    ? actionResultSummary.recovery_actions.map((item) => String(item || '')).filter(Boolean)
    : []
  const runtimePreflightStatus = String(runtimePreflight.status || 'unknown')
  const runtimePreflightWarnings = Array.isArray(runtimePreflight.warnings)
    ? runtimePreflight.warnings.map((item) => String(item || '')).filter(Boolean)
    : []
  const runtimePreflightWarningCount = runtimePreflightWarnings.length
  const runtimeIssueStatus = String(runtimeIssueSummary.status || 'unknown')
  const runtimeDriftStatus = String(runtimeDrift.status || 'unknown')
  const runtimeIssueCount = Number(runtimeIssueSummary.issue_count || 0)
  const runtimeIssueCodes = Array.isArray(runtimeIssueSummary.issues)
    ? runtimeIssueSummary.issues.map((item) => String(item?.code || '')).filter(Boolean)
    : []
  const actionIssueStatus = String(actionIssueSummary.status || 'unknown')
  const actionIssueCount = Number(actionIssueSummary.issue_count || 0)
  const actionIssueCodes = Array.isArray(actionIssueSummary.issues)
    ? actionIssueSummary.issues.map((item) => String(item?.code || '')).filter(Boolean)
    : []
  const actionIssueActions = Array.isArray(actionIssueSummary.recommended_actions)
    ? actionIssueSummary.recommended_actions.map((item) => String(item || '')).filter(Boolean)
    : []
  const actionWarningCodes = Array.isArray(actionTrace?.warning_codes)
    ? actionTrace.warning_codes.map((item) => String(item || '')).filter(Boolean)
    : []
  const hasRouteRuntimePreflightIssue = phase === 'capability_route'
    && (
      runtimePreflightStatus === 'warn'
      || runtimePreflightWarningCount > 0
    )
  const hasRuntimeIssue = phase === 'capability_execute'
    && (
      runtimeIssueStatus === 'warn'
      || runtimeIssueCount > 0
      || runtimeDriftStatus === 'warn'
    )
  const hasActionIssue = phase === 'capability_execute'
    && (
      actionIssueStatus === 'warn'
      || actionIssueStatus === 'error'
      || actionIssueCount > 0
    )
  const hasEfficiencyIssue = phase === 'capability_execute'
    && ['suboptimal', 'needs_repair', 'needs_replan'].includes(efficiencyCorrelationStatus)
  const hasTraceRuntimeIssue = hasRuntimeIssue || hasRouteRuntimePreflightIssue || hasActionIssue || hasEfficiencyIssue
  const previewSeverity = hasTraceRuntimeIssue && formatPhasePreviewSeverity(evt?.severity) === 'info'
    ? 'warn'
    : formatPhasePreviewSeverity(evt?.severity)
  const issue = hasRouteRuntimePreflightIssue
    || (
      phase === 'capability_execute'
      && (
        evt?.completed === false
        || Boolean(evt?.fallback_reason)
        || previewSeverity !== 'info'
        || hasRuntimeIssue
        || hasActionIssue
      )
    )
  const parts = []
  if (evt?.message) parts.push(String(evt.message))
  if (evt?.capability) parts.push(`capability=${evt.capability}`)
  if (evt?.execution_status) parts.push(`status=${evt.execution_status}`)
  if (evt?.completed != null) parts.push(`completed=${Boolean(evt.completed)}`)
  if (evt?.fallback_reason) parts.push(`fallback=${evt.fallback_reason}`)
  if (Array.isArray(evt?.backend_plan)) {
    const names = evt.backend_plan
      .slice(0, 3)
      .map((item) => capabilityItemName(item))
      .filter(Boolean)
    if (names.length) parts.push(`plan=${names.join(' → ')}`)
  }
  if (Array.isArray(evt?.attempts)) parts.push(`${evt.attempts.length} attempts`)
  if (runtimePreflightStatus !== 'unknown' && runtimePreflightStatus !== 'pass') parts.push(`preflight=${runtimePreflightStatus}`)
  if (runtimePreflightWarningCount > 0) parts.push(`preflight_warnings=${runtimePreflightWarningCount}`)
  if (runtimePreflightWarnings.length) parts.push(`preflight_codes=${runtimePreflightWarnings.slice(0, 3).join(',')}`)
  if (runtimePreflight.recommended_action && runtimePreflight.recommended_action !== 'continue') {
    parts.push(`preflight_action=${runtimePreflight.recommended_action}`)
  }
  if (runtimeIssueStatus !== 'unknown' && runtimeIssueStatus !== 'ok') parts.push(`runtime=${runtimeIssueStatus}`)
  if (runtimeIssueCount > 0) parts.push(`runtime_issues=${runtimeIssueCount}`)
  if (runtimeIssueCodes.length) parts.push(`runtime_codes=${runtimeIssueCodes.slice(0, 3).join(',')}`)
  if (runtimeDriftStatus !== 'unknown' && runtimeDriftStatus !== 'stable') parts.push(`drift=${runtimeDriftStatus}`)
  if (runtimeIssueSummary.recommended_action && runtimeIssueSummary.recommended_action !== 'continue') {
    parts.push(`action=${runtimeIssueSummary.recommended_action}`)
  }
  if (actionTrace?.action) parts.push(`browser_action=${actionTrace.action}`)
  if (actionIssueStatus !== 'unknown' && actionIssueStatus !== 'ok') parts.push(`action_issue=${actionIssueStatus}`)
  if (actionIssueCount > 0) parts.push(`action_issues=${actionIssueCount}`)
  if (actionIssueCodes.length) parts.push(`action_codes=${actionIssueCodes.slice(0, 3).join(',')}`)
  if (actionIssueSummary.recommended_action && actionIssueSummary.recommended_action !== 'continue') {
    parts.push(`action_issue_action=${actionIssueSummary.recommended_action}`)
  }
  if (actionFailureCode) parts.push(`failure=${actionFailureCode}`)
  if (actionFailureCategory) parts.push(`failure_category=${actionFailureCategory}`)
  if (actionRecoveryActions.length) parts.push(`recovery=${actionRecoveryActions.slice(0, 3).join(',')}`)
  if (traceArtifact.path || traceArtifact.url) parts.push('trace_artifact=available')
  if (crawlEfficiencyPlan?.recommended_path) parts.push(`crawl_efficiency=${crawlEfficiencyPlan.recommended_path}`)
  if (crawlEfficiencyPlan?.skip_browser != null) parts.push(`skip_browser=${Boolean(crawlEfficiencyPlan.skip_browser)}`)
  if (crawlEfficiencyPlan?.skip_vlm != null) parts.push(`skip_vlm=${Boolean(crawlEfficiencyPlan.skip_vlm)}`)
  if (crawlEfficiencyAvailablePaths.length) parts.push(`available_paths=${crawlEfficiencyAvailablePaths.slice(0, 4).join(',')}`)
  if (efficiencyCorrelationReport?.status) parts.push(`efficiency_correlation=${efficiencyCorrelationReport.status}`)
  if (efficiencyCorrelationAlignment.executed_path) parts.push(`executed_path=${efficiencyCorrelationAlignment.executed_path}`)
  if (efficiencyCorrelationAlignment.recommended_path) parts.push(`recommended_path=${efficiencyCorrelationAlignment.recommended_path}`)
  if (efficiencyCorrelationRootCauses.length) parts.push(`efficiency_causes=${efficiencyCorrelationRootCauses.slice(0, 3).join(',')}`)
  if (efficiencyCorrelationActions.length) parts.push(`efficiency_actions=${efficiencyCorrelationActions.slice(0, 3).join(',')}`)
  const actionSearchText = [
    phase,
    evt?.message,
    evt?.capability,
    evt?.execution_status,
    evt?.fallback_reason,
    parts.join(' '),
    actionTrace?.action,
    actionTrace?.status,
    actionTrace?.target?.selector,
    actionTrace?.target?.ref,
    actionTrace?.action_ref?.selector,
    actionTrace?.action_ref?.ref,
    actionIssueStatus,
    actionIssueCodes.join(' '),
    actionIssueActions.join(' '),
    actionWarningCodes.join(' '),
    actionIssueSummary.recommended_action,
    actionFailureCode,
    actionFailureCategory,
    actionRecoveryActions.join(' '),
    traceArtifact.path,
    traceArtifact.url,
    crawlEfficiencyPlan?.recommended_path,
    crawlEfficiencyAvailablePaths.join(' '),
    crawlEfficiencyCandidates.map((item) => `${item?.name || ''} ${item?.reason || ''}`).join(' '),
    efficiencyCorrelationReport?.status,
    efficiencyCorrelationAlignment.recommended_path,
    efficiencyCorrelationAlignment.executed_path,
    efficiencyCorrelationRootCauses.join(' '),
    efficiencyCorrelationActions.join(' '),
  ]
    .map((item) => String(item || '').toLowerCase())
    .filter(Boolean)
    .join(' ')
  return {
    idx,
    event: evt,
    phase,
    severity: previewSeverity,
    issue,
    time: formatPhasePreviewTs(Number.isFinite(evt?._ts) ? evt._ts : evt?.ts),
    detail: parts.join(' · '),
    searchText: actionSearchText,
  }
}))

const capabilityTraceSummary = computed(() => {
  const rows = capabilityTraceRows.value
  return {
    all: rows.length,
    route: rows.filter((row) => row.phase === 'capability_route').length,
    execute: rows.filter((row) => row.phase === 'capability_execute').length,
    issues: rows.filter((row) => row.issue).length,
  }
})

const capabilityFilteredTraceRows = computed(() => {
  const filter = capabilityTraceFilter.value
  let rows = capabilityTraceRows.value
  if (filter === 'route') {
    rows = rows.filter((row) => row.phase === 'capability_route')
  }
  else if (filter === 'execute') {
    rows = rows.filter((row) => row.phase === 'capability_execute')
  }
  else if (filter === 'issues') {
    rows = rows.filter((row) => row.issue)
  }
  const q = String(capabilityTraceSearchQuery.value || '').trim().toLowerCase()
  if (!q) return rows
  return rows.filter((row) => String(row.searchText || row.detail || '').toLowerCase().includes(q))
})

const capabilityExecutionAlignment = computed(() => {
  const capability = String(latestCapabilityExecute.value?.capability || '')
  const planNames = capabilityBackendPlan.value.map((item) => capabilityItemName(item))
  const rank = capability ? planNames.indexOf(capability) : -1
  const completed = Boolean(latestCapabilityExecute.value?.completed)
  const fallbackReason = String(latestCapabilityExecute.value?.fallback_reason || '')
  return {
    capability,
    rank: rank >= 0 ? rank + 1 : null,
    topChoice: Boolean(capability && planNames[0] === capability),
    inPlan: rank >= 0,
    completed,
    fallback: Boolean(fallbackReason || (capability && planNames[0] && planNames[0] !== capability)),
    fallbackReason,
    planHead: planNames[0] || '',
  }
})

const capabilityTraceHealth = computed(() => {
  const summary = capabilityTraceSummary.value
  const alignment = capabilityExecutionAlignment.value
  const runtimeIssue = capabilityExecutionRuntimeIssueSummary.value || {}
  const runtimeIssueStatus = String(runtimeIssue.status || '')
  const runtimeIssueCount = Number(runtimeIssue.issue_count || 0)
  const runtimeIssueAction = String(runtimeIssue.recommended_action || '')
  const runtimeAlignment = runtimeIssueStatus === 'warn' || runtimeIssueCount > 0
    ? `runtime ${runtimeIssueStatus || 'warn'} (${runtimeIssueCount})${runtimeIssueAction && runtimeIssueAction !== 'continue' ? ` · ${runtimeIssueAction}` : ''}`
    : ''
  const routePreflight = capabilityRuntimePreflight.value || {}
  const routePreflightStatus = String(routePreflight.status || '')
  const routePreflightWarnings = Array.isArray(routePreflight.warnings) ? routePreflight.warnings : []
  const routePreflightAction = String(routePreflight.recommended_action || '')
  const routePreflightAlignment = routePreflightStatus === 'warn' || routePreflightWarnings.length > 0
    ? `preflight ${routePreflightStatus || 'warn'} (${routePreflightWarnings.length})${routePreflightAction && routePreflightAction !== 'continue' ? ` · ${routePreflightAction}` : ''}`
    : ''
  if (!summary.all) {
    return { status: 'empty', label: '暂无 trace', route: 0, execute: 0, issues: 0, alignment: '' }
  }
  if (summary.issues > 0) {
    return {
      status: 'issue',
      label: '存在问题',
      route: summary.route,
      execute: summary.execute,
      issues: summary.issues,
      alignment: alignment.fallbackReason || runtimeAlignment || routePreflightAlignment || '需要检查执行结果',
    }
  }
  if (!summary.execute) {
    return {
      status: 'route-only',
      label: '仅路由',
      route: summary.route,
      execute: summary.execute,
      issues: summary.issues,
      alignment: alignment.planHead ? `preferred ${alignment.planHead}` : '',
    }
  }
  if (alignment.topChoice && alignment.completed) {
    return {
      status: 'healthy',
      label: '健康',
      route: summary.route,
      execute: summary.execute,
      issues: summary.issues,
      alignment: `top choice ${alignment.capability}`,
    }
  }
  if (alignment.fallback) {
    return {
      status: 'fallback',
      label: 'Fallback',
      route: summary.route,
      execute: summary.execute,
      issues: summary.issues,
      alignment: alignment.fallbackReason || `preferred ${alignment.planHead || 'unknown'}`,
    }
  }
  return {
    status: 'executed',
    label: '已执行',
    route: summary.route,
    execute: summary.execute,
    issues: summary.issues,
    alignment: alignment.capability || '',
  }
})

const browserRuntime = computed(() => browserRuntimeStatus.value || {})
const browserRuntimeCapacity = computed(() => browserRuntime.value.capacity || {})
const browserRuntimeBackendSummary = computed(() => browserRuntime.value.backend_summary || {})
const browserRuntimeStatusClass = computed(() => {
  const status = String(browserRuntime.value.status || 'unknown')
  if (status === 'available') return 'healthy'
  if (status === 'pool_exhausted' || status === 'backend_unavailable' || status === 'backend_unhealthy') return 'issue'
  if (status === 'limited') return 'fallback'
  return 'route-only'
})
const browserRuntimeLabel = computed(() => {
  const status = String(browserRuntime.value.status || 'unknown')
  if (status === 'available') return '可用'
  if (status === 'pool_exhausted') return 'Pool 已满'
  if (status === 'backend_unavailable') return 'Backend 不可用'
  if (status === 'backend_unhealthy') return 'Backend 异常'
  if (status === 'limited') return '受限'
  return '未知'
})
const browserRuntimeHealthLabel = computed(() => {
  const status = String(browserRuntimeBackendSummary.value.health_status || 'unknown')
  if (status === 'healthy') return 'healthy'
  if (status === 'not_configured') return 'not configured'
  if (status === 'unhealthy') return 'unhealthy'
  return status
})
const browserRuntimeHealthCacheLabel = computed(() => {
  const summary = browserRuntimeBackendSummary.value
  if (summary.health_cache_stale) return 'stale cache'
  if (summary.health_cache_hit) {
    const age = summary.health_cache_age_s == null ? '?' : summary.health_cache_age_s
    const ttl = summary.health_cache_ttl_s == null ? '?' : summary.health_cache_ttl_s
    return `cached ${age}s/${ttl}s`
  }
  return 'fresh'
})
const capabilityRuntimePreflightClass = computed(() => {
  const status = String(capabilityRuntimePreflight.value.status || 'unknown')
  if (status === 'pass') return 'healthy'
  if (status === 'warn') return 'fallback'
  if (status === 'block') return 'issue'
  return 'route-only'
})
const capabilityRuntimePreflightLabel = computed(() => {
  const status = String(capabilityRuntimePreflight.value.status || 'unknown')
  if (status === 'pass') return '预检通过'
  if (status === 'warn') return '预检提醒'
  if (status === 'block') return '预检阻断'
  return '未预检'
})
const capabilityExecutionRuntimeLabel = computed(() => {
  const status = String(capabilityExecutionRuntimeAfter.value.preflight_status || 'unknown')
  if (status === 'pass') return 'runtime ok'
  if (status === 'warn') return 'runtime warn'
  if (status === 'block') return 'runtime block'
  return 'runtime unknown'
})
const capabilityExecutionDriftLabel = computed(() => {
  const status = String(capabilityExecutionRuntimeDrift.value.status || 'unknown')
  if (status === 'stable') return 'drift stable'
  if (status === 'changed') return 'drift changed'
  if (status === 'warn') return 'drift warn'
  return 'drift unknown'
})
const capabilityExecutionIssueLabel = computed(() => {
  const status = String(capabilityExecutionRuntimeIssueSummary.value.status || 'unknown')
  if (status === 'ok') return 'runtime ok'
  if (status === 'watch') return 'runtime watch'
  if (status === 'warn') return 'runtime issues'
  return 'runtime unknown'
})
const capabilityExecutionActionIssueLabel = computed(() => {
  const status = String(capabilityExecutionActionIssueSummary.value.status || 'unknown')
  if (status === 'ok') return 'action ok'
  if (status === 'warn') return 'action issues'
  if (status === 'error') return 'action error'
  return 'action unknown'
})

const capabilityEfficiencyCorrelationStatusClass = computed(() => {
  const status = String(capabilityExecutionEfficiencyCorrelationReport.value?.status || 'unknown')
  if (status === 'aligned') return 'is-complete'
  if (status === 'suboptimal' || status === 'needs_repair') return 'is-warning'
  if (status === 'needs_replan') return 'is-error'
  return 'is-skip'
})

const capabilityRoleRows = computed(() => Object.entries(capabilityModelRoles.value || {})
  .map(([key, value]) => ({
    key,
    position: value?.position || '',
    responsibilities: Array.isArray(value?.responsibilities) ? value.responsibilities.slice(0, 4) : [],
    shouldNotDo: Array.isArray(value?.should_not_do) ? value.should_not_do.slice(0, 4) : [],
    recommendedUse: value?.recommended_use || '',
  })))

// Group events by step (or "no step" bucket for finalize/etc.) so the panel
// can render one row per step with all phase chips for that step inline.
// Order: events without a step go to the end (typically only finalize).
const phaseTimelineGroups = computed(() => {
  const noStepKey = '__no_step__'
  const buckets = new Map()
  for (const e of filteredPhaseEvents.value) {
    const key = Number.isFinite(e.step) ? `s${e.step}` : noStepKey
    if (!buckets.has(key)) buckets.set(key, { step: e.step, events: [] })
    buckets.get(key).events.push(e)
  }
  // Stable sort: ascending step, no_step last
  const groups = Array.from(buckets.values())
  groups.sort((a, b) => {
    if (Number.isFinite(a.step) && !Number.isFinite(b.step)) return -1
    if (!Number.isFinite(a.step) && Number.isFinite(b.step)) return 1
    if (Number.isFinite(a.step) && Number.isFinite(b.step)) return a.step - b.step
    return 0
  })
  return groups
})

// Summary band over the whole run (top of the Timeline panel).
// Counts come from the FILTERED set so the user always sees how many
// events match their current filter; the toolbar separately shows
// "X / Y" so they know how many are hidden.
const phaseSummary = computed(() => {
  const out = { total: filteredPhaseEvents.value.length, warn: 0, error: 0, byPhase: {} }
  for (const e of filteredPhaseEvents.value) {
    const sev = String(e.severity || 'info')
    if (sev === 'warn') out.warn += 1
    else if (sev === 'error') out.error += 1
    const p = String(e.phase || 'unknown')
    out.byPhase[p] = (out.byPhase[p] || 0) + 1
  }
  return out
})

// O: list of distinct phase names seen in this run, with their UNFILTERED
// counts. Used to render the filter chip row. Sorted by count desc so the
// noisy phases (vlm_call/action/som_inject) always appear first.
// V: Per-phase aggregate stats. Each entry now carries:
//   {phase, count, mean, p50, p95, max, sevCounts:{info,warn,error},
//    durations:[...]}
// `durations` is the sorted ascending list of duration_ms samples; we
// keep it for the sparkline renderer (which buckets the values into
// fixed-width vertical bars). Phases with no duration_ms field still
// appear with count + sevCounts; their mean/p50/p95/max are null.
//
// Sort order: count DESC (noisy phases at the top, same as before V)
// because that's the most useful triage axis when the run produced a
// huge timeline.
//
// Helpers (defined inline so this stays a single computed expression
// for Vue's dependency tracking):
//   pickPercentile(sorted, p):  linear interpolation, returns null on
//                                empty input. We don't need the
//                                full numpy-grade nearest-rank dance —
//                                p50/p95 over <=500 samples is plenty.
const phaseFilterOptions = computed(() => {
  const acc = new Map() // phase -> {count, durs:[], sev:{info,warn,error}}
  for (const e of phaseEvents.value) {
    const p = String(e.phase || 'unknown')
    let bucket = acc.get(p)
    if (!bucket) {
      bucket = { count: 0, durs: [], sev: { info: 0, warn: 0, error: 0 } }
      acc.set(p, bucket)
    }
    bucket.count += 1
    if (Number.isFinite(e.duration_ms)) {
      bucket.durs.push(Number(e.duration_ms))
    }
    const sev = String(e.severity || 'info')
    if (bucket.sev[sev] != null) bucket.sev[sev] += 1
  }

  const pickPercentile = (sorted, p) => {
    if (!sorted.length) return null
    if (sorted.length === 1) return sorted[0]
    // Linear interpolation between the two nearest ranks.
    const idx = (sorted.length - 1) * p
    const lo = Math.floor(idx)
    const hi = Math.ceil(idx)
    if (lo === hi) return sorted[lo]
    return sorted[lo] + (sorted[hi] - sorted[lo]) * (idx - lo)
  }

  const out = []
  for (const [phase, b] of acc.entries()) {
    const sorted = b.durs.slice().sort((a, c) => a - c)
    const sum = sorted.reduce((s, x) => s + x, 0)
    out.push({
      phase,
      count: b.count,
      mean: sorted.length ? sum / sorted.length : null,
      p50: pickPercentile(sorted, 0.5),
      p95: pickPercentile(sorted, 0.95),
      max: sorted.length ? sorted[sorted.length - 1] : null,
      sevCounts: { ...b.sev },
      // Cap durations passed to the sparkline at 60 samples — we don't
      // need every point to communicate the shape, and the renderer
      // would just bucket them anyway.
      durations: sorted.length > 60
        ? sorted.filter((_, i) => i % Math.ceil(sorted.length / 60) === 0)
        : sorted,
    })
  }
  return out.sort((a, b) => b.count - a.count)
})

// V: Sorted view of phaseFilterOptions for the stats grid. The base
// computed is already count-DESC; here we re-sort when the user picks a
// different axis (mean / p95 / max). Phases with no duration data sink
// to the bottom regardless of axis (their stat is null).
const phaseStatsSorted = computed(() => {
  const key = phaseStatsSortBy.value
  if (key === 'count') return phaseFilterOptions.value
  return phaseFilterOptions.value.slice().sort((a, b) => {
    const av = a[key]
    const bv = b[key]
    const aMissing = av == null
    const bMissing = bv == null
    if (aMissing && bMissing) return 0
    if (aMissing) return 1   // missing → bottom
    if (bMissing) return -1
    return bv - av           // descending
  })
})

// V: Compute the SVG path for one phase's mini sparkline. The bars go
// from left (oldest sample) to right (newest sample) with each bar
// height proportional to that sample's duration, normalized against the
// phase's local max so cheap phases still get readable bars.
//
// The SVG uses viewBox="0 0 100 24" so the consumer can size it freely
// via CSS without having to rewrite the path.
const phaseSparklinePath = (durations) => {
  if (!durations || durations.length === 0) return ''
  const w = 100
  const h = 24
  const maxV = durations.reduce((m, x) => (x > m ? x : m), 0) || 1
  // Bar geometry: leave 1px gap between bars when possible.
  const n = durations.length
  const barW = Math.max(1, w / n - 0.6)
  const stride = w / n
  let d = ''
  for (let i = 0; i < n; i += 1) {
    const x = i * stride
    const barH = (durations[i] / maxV) * (h - 2) + 1
    const y = h - barH
    d += `M${x.toFixed(2)},${h} L${x.toFixed(2)},${y.toFixed(2)} `
        + `L${(x + barW).toFixed(2)},${y.toFixed(2)} `
        + `L${(x + barW).toFixed(2)},${h} Z `
  }
  return d.trim()
}

// V: Friendly formatter for ms values inside the stats grid.
//   12.4ms / 1.2s / —
const formatPhaseStatMs = (v) => {
  if (v == null || !Number.isFinite(v)) return '—'
  if (v < 1000) return `${v.toFixed(1)}ms`
  return `${(v / 1000).toFixed(2)}s`
}

// Same as phaseFilterOptions but per-severity.
const severityFilterOptions = computed(() => {
  const counts = { info: 0, warn: 0, error: 0 }
  for (const e of phaseEvents.value) {
    const s = String(e.severity || 'info')
    if (counts[s] != null) counts[s] += 1
  }
  // Only show severities that actually occur (don't waste space on empty)
  return ['info', 'warn', 'error']
    .filter((s) => counts[s] > 0)
    .map((s) => ({ severity: s, count: counts[s] }))
})

// True when any filter is active — drives visual state of the "重置" button
const phaseFilterActive = computed(
  () => phaseFilterExclude.value.size > 0 || severityFilterExclude.value.size > 0,
)

// Toggle a phase in/out of the exclude set. We re-assign the Set so Vue
// notices the change (Sets aren't deeply reactive otherwise).
const togglePhaseFilter = (phase) => {
  const next = new Set(phaseFilterExclude.value)
  if (next.has(phase)) next.delete(phase)
  else next.add(phase)
  phaseFilterExclude.value = next
}

const toggleSeverityFilter = (severity) => {
  const next = new Set(severityFilterExclude.value)
  if (next.has(severity)) next.delete(severity)
  else next.add(severity)
  severityFilterExclude.value = next
}

const resetPhaseFilters = () => {
  phaseFilterExclude.value = new Set()
  severityFilterExclude.value = new Set()
}

// Per-phase visual palette. Falls back to a neutral grey when phase unknown.
// Color follows severity, NOT phase name, so warn/error always pop.
//
// U: when the backend included a `notice_severity` field (the BrowserEnv
// `_last_notice_severity` snapshot at emit time), use the HIGHER of
// (severity, notice_severity). This catches the common case where the
// action itself succeeded (severity=info) but the agent-visible notice
// it produced was a warning (e.g. "TYPE NO-OP: input unchanged") —
// without notice_severity, those chips would be deceptively green.
const _SEV_RANK = { info: 0, warn: 1, error: 2 }
function _effectiveSeverity(evt) {
  const a = String(evt.severity || 'info')
  const b = String(evt.notice_severity || a)
  return (_SEV_RANK[b] ?? 0) > (_SEV_RANK[a] ?? 0) ? b : a
}
function phaseChipStyle(evt) {
  const sev = _effectiveSeverity(evt)
  if (sev === 'error') return { background: '#fee2e2', color: '#991b1b', border: '#fca5a5' }
  if (sev === 'warn')  return { background: '#fef3c7', color: '#92400e', border: '#fcd34d' }
  return { background: '#dbeafe', color: '#1e40af', border: '#93c5fd' }
}

// Short, single-line label for a chip ("vlm_call · 2.3s")
function phaseChipLabel(evt) {
  const p = String(evt.phase || 'unknown')
  const d = evt.duration_ms
  if (Number.isFinite(d)) {
    const sec = d >= 1000 ? `${(d / 1000).toFixed(1)}s` : `${Math.round(d)}ms`
    return `${p} · ${sec}`
  }
  return p
}

// Detail bubble below the chip — message + key extras (action_name,
// element_count, guard, etc.). Returns '' to suppress the bubble.
function phaseChipDetail(evt) {
  const parts = []
  if (evt.message) parts.push(String(evt.message))
  if (evt.action_name) parts.push(`action=${evt.action_name}`)
  if (evt.element_count != null) parts.push(`${evt.element_count} els`)
  if (evt.frame_count != null) parts.push(`${evt.frame_count} frames`)
  if (evt.guard) parts.push(`guard=${evt.guard}`)
  if (evt.evaluation?.status) parts.push(`status=${evt.evaluation.status}`)
  if (evt.answer_domain) parts.push(`domain=${evt.answer_domain}`)
  return parts.join(' · ')
}

const clearPhaseEvents = () => {
  phaseEvents.value = []
  hasNewPhase.value = false
  hasNewCapability.value = false
  // P: After a clear, there's nothing to scroll past — re-pin to bottom.
  timelineAutoScroll.value = true
}

// ── Q: Export timeline events as JSONL ────────────────────────────────
// Honors the active filter (severity/phase exclude) so the user gets
// exactly what they see. Strip the synthetic _ts field (added by M for
// internal use) so the file contains only over-the-wire payloads.
// Empty buffer → no-op + toast.
const exportPhaseEventsAsJsonl = () => {
  const src = filteredPhaseEvents.value
  if (!src.length) {
    ElMessage.warning('当前没有可导出的 phase 事件')
    return
  }
  const lines = []
  for (const e of src) {
    const clone = {}
    for (const k of Object.keys(e)) {
      if (k === '_ts') continue
      clone[k] = e[k]
    }
    try {
      lines.push(JSON.stringify(clone))
    } catch (err) {
      // Skip un-serializable events but don't abort the whole export
      lines.push(JSON.stringify({ phase: 'unknown', error: String(err) }))
    }
  }
  const ts = new Date()
  const stamp =
    `${ts.getFullYear()}${String(ts.getMonth() + 1).padStart(2, '0')}` +
    `${String(ts.getDate()).padStart(2, '0')}_${String(ts.getHours()).padStart(2, '0')}` +
    `${String(ts.getMinutes()).padStart(2, '0')}${String(ts.getSeconds()).padStart(2, '0')}`
  // Trailing newline keeps tools like `jq -c` and pandas.read_json(lines=True) happy
  const blob = new Blob([lines.join('\n') + '\n'], {
    type: 'application/x-ndjson;charset=utf-8',
  })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `phase_events_${stamp}.jsonl`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  setTimeout(() => URL.revokeObjectURL(url), 1000)
  ElMessage.success(`已导出 ${lines.length} 条事件`)
}

const exportCapabilityTraceAsJsonl = () => {
  const src = capabilityTraceEvents.value
  if (!src.length) {
    ElMessage.warning('当前没有可导出的 capability trace 事件')
    return
  }
  const lines = []
  for (const e of src) {
    const clone = {}
    for (const k of Object.keys(e)) {
      if (k === '_ts') continue
      clone[k] = e[k]
    }
    try {
      lines.push(JSON.stringify(clone))
    } catch (err) {
      lines.push(JSON.stringify({ phase: 'capability_unknown', error: String(err) }))
    }
  }
  const ts = new Date()
  const stamp =
    `${ts.getFullYear()}${String(ts.getMonth() + 1).padStart(2, '0')}` +
    `${String(ts.getDate()).padStart(2, '0')}_${String(ts.getHours()).padStart(2, '0')}` +
    `${String(ts.getMinutes()).padStart(2, '0')}${String(ts.getSeconds()).padStart(2, '0')}`
  const blob = new Blob([lines.join('\n') + '\n'], {
    type: 'application/x-ndjson;charset=utf-8',
  })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `capability_trace_${stamp}.jsonl`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  setTimeout(() => URL.revokeObjectURL(url), 1000)
  ElMessage.success(`已导出 ${lines.length} 条 capability trace 事件`)
}

const buildCapabilityTraceSummaryText = () => {
  const health = capabilityTraceHealth.value
  const alignment = capabilityExecutionAlignment.value
  const intent = capabilityIntent.value || {}
  const routePreflight = capabilityRuntimePreflight.value || {}
  const routePreflightWarnings = Array.isArray(routePreflight.warnings) ? routePreflight.warnings : []
  const runtimeIssue = capabilityExecutionRuntimeIssueSummary.value || {}
  const runtimeDrift = capabilityExecutionRuntimeDrift.value || {}
  const runtimeIssues = capabilityExecutionRuntimeIssues.value || []
  const runtimeActions = capabilityExecutionRuntimeActions.value || []
  const actionTrace = capabilityExecutionActionTrace.value || {}
  const actionIssue = capabilityExecutionActionIssueSummary.value || {}
  const actionIssues = capabilityExecutionActionIssues.value || []
  const actionActions = capabilityExecutionActionIssueActions.value || []
  const actionFailure = capabilityExecutionActionFailureSummary.value || {}
  const actionRecoveryActions = capabilityExecutionActionRecoveryActions.value || []
  const crawlEfficiency = capabilityActiveCrawlEfficiencyPlan.value || {}
  const crawlCandidates = capabilityExecutionCrawlEfficiencyCandidates.value || []
  const crawlAvailablePaths = capabilityExecutionCrawlEfficiencyAvailablePaths.value || []
  const efficiencyCorrelation = capabilityExecutionEfficiencyCorrelationReport.value || {}
  const efficiencyAlignment = capabilityExecutionEfficiencyCorrelationAlignment.value || {}
  const efficiencyRootCauses = capabilityExecutionEfficiencyCorrelationRootCauses.value || []
  const efficiencyActions = capabilityExecutionEfficiencyCorrelationActions.value || []
  const lines = [
    '# Capability Trace Summary',
    '',
    `Health: ${health.label || 'unknown'}`,
    `Route events: ${health.route}`,
    `Execute events: ${health.execute}`,
    `Issues: ${health.issues}`,
  ]
  if (intent.task_type) lines.push(`Intent: ${intent.task_type}`)
  if (intent.output_mode) lines.push(`Output mode: ${intent.output_mode}`)
  if (alignment.planHead) lines.push(`Preferred: ${alignment.planHead}`)
  if (alignment.capability) lines.push(`Executed: ${alignment.capability}`)
  if (alignment.rank) lines.push(`Plan rank: #${alignment.rank}`)
  else if (alignment.capability) lines.push('Plan rank: not in plan')
  lines.push(`Top choice: ${alignment.topChoice ? 'yes' : 'no'}`)
  lines.push(`Completed: ${alignment.completed ? 'yes' : 'no'}`)
  if (crawlEfficiency.version) {
    lines.push(`Crawl efficiency path: ${crawlEfficiency.recommended_path || 'unknown'}`)
    lines.push(`Crawl efficiency skip browser: ${crawlEfficiency.skip_browser ? 'yes' : 'no'}`)
    lines.push(`Crawl efficiency skip VLM: ${crawlEfficiency.skip_vlm ? 'yes' : 'no'}`)
  }
  if (crawlAvailablePaths.length) {
    lines.push(`Crawl efficiency available paths: ${crawlAvailablePaths.join(', ')}`)
  }
  if (crawlCandidates.length) {
    const topCandidates = crawlCandidates
      .slice(0, 4)
      .map((item) => `${item.name || 'unknown'}:${item.available ? 'available' : 'unavailable'}:${item.reason || ''}`)
    lines.push(`Crawl efficiency candidates: ${topCandidates.join(' | ')}`)
  }
  if (efficiencyCorrelation.version) {
    lines.push(`Efficiency correlation: ${efficiencyCorrelation.status || 'unknown'}`)
    lines.push(`Efficiency recommended path: ${efficiencyAlignment.recommended_path || 'unknown'}`)
    lines.push(`Efficiency executed path: ${efficiencyAlignment.executed_path || 'unknown'}`)
  }
  if (efficiencyRootCauses.length) {
    lines.push(`Efficiency root causes: ${efficiencyRootCauses.join(', ')}`)
  }
  if (efficiencyActions.length) {
    lines.push(`Efficiency actions: ${efficiencyActions.join(', ')}`)
  }
  if (routePreflight.version) {
    lines.push(`Route preflight: ${routePreflight.status || 'unknown'} (${routePreflightWarnings.length})`)
  }
  if (routePreflight.recommended_action && routePreflight.recommended_action !== 'continue') {
    lines.push(`Route preflight action: ${routePreflight.recommended_action}`)
  }
  if (routePreflightWarnings.length) {
    lines.push(`Route preflight warnings: ${routePreflightWarnings.slice(0, 5).join(', ')}`)
  }
  if (runtimeIssue.version) {
    lines.push(`Runtime issues: ${runtimeIssue.status || 'unknown'} (${runtimeIssue.issue_count ?? 0})`)
  }
  if (runtimeDrift.version) {
    lines.push(`Runtime drift: ${runtimeDrift.status || 'unknown'}`)
  }
  if (runtimeIssue.recommended_action && runtimeIssue.recommended_action !== 'continue') {
    lines.push(`Runtime action: ${runtimeIssue.recommended_action}`)
  }
  if (runtimeIssues.length) {
    const issueCodes = runtimeIssues
      .slice(0, 5)
      .map((issue) => `${issue.source || 'runtime'}:${issue.code || 'issue'}`)
    lines.push(`Runtime issue codes: ${issueCodes.join(', ')}`)
  }
  if (runtimeActions.length) {
    lines.push(`Runtime actions: ${runtimeActions.join(', ')}`)
  }
  if (actionIssue.version) {
    lines.push(`Browser action issues: ${actionIssue.status || 'unknown'} (${actionIssue.issue_count ?? 0})`)
  }
  if (actionTrace.action) {
    lines.push(`Browser action: ${actionTrace.action}`)
  }
  if (actionFailure.failure_code) {
    lines.push(`Browser action failure: ${actionFailure.failure_code}`)
  }
  if (actionFailure.failure_category) {
    lines.push(`Browser action failure category: ${actionFailure.failure_category}`)
  }
  if (actionIssue.recommended_action && actionIssue.recommended_action !== 'continue') {
    lines.push(`Browser action recommendation: ${actionIssue.recommended_action}`)
  }
  if (actionIssues.length) {
    const actionIssueCodes = actionIssues
      .slice(0, 5)
      .map((issue) => `${issue.source || 'action'}:${issue.code || 'issue'}`)
    lines.push(`Browser action issue codes: ${actionIssueCodes.join(', ')}`)
  }
  if (actionActions.length) {
    lines.push(`Browser action recommendations: ${actionActions.join(', ')}`)
  }
  if (actionRecoveryActions.length) {
    lines.push(`Browser action recovery actions: ${actionRecoveryActions.join(', ')}`)
  }
  if (alignment.fallbackReason) lines.push(`Fallback reason: ${alignment.fallbackReason}`)
  if (health.alignment) lines.push(`Alignment: ${health.alignment}`)
  return `${lines.join('\n')}\n`
}

const copyCapabilityTraceSummary = async () => {
  if (!capabilityTraceEvents.value.length) {
    ElMessage.warning('当前没有可复制的 capability trace 摘要')
    return
  }
  const ok = await _writeToClipboard(buildCapabilityTraceSummaryText())
  if (ok) {
    ElMessage.success('已复制 capability trace 摘要')
  }
}

const buildCapabilityFailureFixtureBatchReplaySummaryText = () => {
  return buildFailureFixtureBatchReplaySummaryText({
    report: capabilityFailureFixtureBatchReplayReport.value || {},
    summary: capabilityFailureFixtureBatchReplaySummary.value || {},
    artifact: capabilityFailureFixtureBatchReplayArtifact.value || {},
    failedChecks: capabilityFailureFixtureBatchReplayFailedChecks.value,
    topPrimaryFailures: capabilityFailureFixtureBatchReplayTopPrimaryFailures.value,
    topFailureCategories: capabilityFailureFixtureBatchReplayTopFailureCategories.value,
    topFailedChecks: capabilityFailureFixtureBatchReplayTopFailedChecks.value,
    failedItems: capabilityFailureFixtureBatchReplayFailedItems.value,
  })
}

const copyCapabilityFailureFixtureBatchReplaySummary = async () => {
  if (!capabilityFailureFixtureBatchReplayReport.value) {
    ElMessage.warning('当前没有可复制的 batch replay 摘要')
    return
  }
  const ok = await _writeToClipboard(buildCapabilityFailureFixtureBatchReplaySummaryText())
  if (ok) {
    ElMessage.success('已复制 batch replay 摘要')
  }
}

const generateCapabilityFailureFixture = async () => {
  const failureBundle = capabilityExecutionFailureBundle.value || {}
  if (failureBundle.version !== 'capability_execute_failure_bundle.v1') {
    ElMessage.warning('当前没有可生成 fixture 的 capability failure bundle')
    return
  }
  if (capabilityFailureFixtureLoading.value) return
  capabilityFailureFixtureLoading.value = true
  try {
    const response = await apiFetch('/api/capabilities/failure_fixture', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source: latestCapabilityExecute.value || { failure_bundle: failureBundle },
        name: `capability_${failureBundle.action || 'failure'}_${failureBundle.primary_failure || 'failure'}`,
        tags: ['frontend', 'capability_trace'],
        save: true,
      }),
    })
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.detail || result.message || '生成 failure fixture 失败')
    }
    const artifact = result.result?.artifact || {}
    if (artifact.url) await fetchArtifacts()
    if (artifact.url) await fetchCapabilityFailureFixtures(true)
    ElMessage.success(artifact.url ? `已生成 failure fixture: ${artifact.url}` : '已生成 failure fixture')
  } catch (err) {
    ElMessage.error(`生成 failure fixture 失败: ${String(err)}`)
  } finally {
    capabilityFailureFixtureLoading.value = false
  }
}

const replayCapabilityFailureFixture = async () => {
  const failureBundle = capabilityExecutionFailureBundle.value || {}
  if (failureBundle.version !== 'capability_execute_failure_bundle.v1') {
    ElMessage.warning('当前没有可验证 replay 的 capability failure bundle')
    return
  }
  if (capabilityFailureFixtureReplayLoading.value) return
  capabilityFailureFixtureReplayLoading.value = true
  try {
    const fixtureResponse = await apiFetch('/api/capabilities/failure_fixture', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source: latestCapabilityExecute.value || { failure_bundle: failureBundle },
        name: `capability_${failureBundle.action || 'failure'}_${failureBundle.primary_failure || 'failure'}_replay`,
        tags: ['frontend', 'capability_trace', 'replay'],
      }),
    })
    const fixtureResult = await fixtureResponse.json()
    if (!fixtureResponse.ok || fixtureResult.status !== 'success') {
      throw new Error(fixtureResult.detail || fixtureResult.message || '生成 replay fixture 失败')
    }
    const replayResponse = await apiFetch('/api/capabilities/failure_fixture/replay', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        fixture: fixtureResult.result?.fixture || {},
        save: true,
      }),
    })
    const replayResult = await replayResponse.json()
    if (!replayResponse.ok || replayResult.status !== 'success') {
      throw new Error(replayResult.detail || replayResult.message || '验证 failure fixture replay 失败')
    }
    capabilityFailureFixtureReplayReport.value = replayResult.result?.report || null
    capabilityFailureFixtureReplayArtifact.value = replayResult.result?.artifact || null
    if (capabilityFailureFixtureReplayArtifact.value?.url) await fetchArtifacts()
    const passed = Boolean(capabilityFailureFixtureReplayReport.value?.passed)
    if (passed) {
      ElMessage.success('Failure fixture replay 验证通过')
    } else {
      ElMessage.warning('Failure fixture replay 验证未通过')
    }
  } catch (err) {
    ElMessage.error(`验证 failure fixture replay 失败: ${String(err)}`)
  } finally {
    capabilityFailureFixtureReplayLoading.value = false
  }
}

const replayCapabilityEfficiencyFeedback = async () => {
  const report = capabilityExecutionEfficiencyCorrelationReport.value || {}
  if (report.version !== 'efficiency_correlation_report.v1') {
    ElMessage.warning('当前没有可验证 replay 的 efficiency correlation report')
    return
  }
  if (capabilityEfficiencyFeedbackReplayLoading.value) return
  capabilityEfficiencyFeedbackReplayLoading.value = true
  try {
    const response = await apiFetch('/api/capabilities/efficiency_feedback/replay', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source: latestCapabilityExecute.value || { efficiency_correlation_report: report },
        efficiency_correlation_report: report,
        goal: prompt.value,
        url: url.value,
        name: 'frontend_efficiency_feedback_replay',
        save: true,
      }),
    })
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.detail || result.message || '验证 efficiency feedback replay 失败')
    }
    capabilityEfficiencyFeedbackReplayReport.value = result.result?.report || null
    capabilityEfficiencyFeedbackReplayArtifact.value = result.result?.artifact || null
    if (capabilityEfficiencyFeedbackReplayArtifact.value?.url) await fetchArtifacts()
    if (capabilityEfficiencyFeedbackReplayArtifact.value?.url) await fetchCapabilityEfficiencyFeedbackReplays(true)
    if (capabilityEfficiencyFeedbackReplayReport.value?.passed) {
      ElMessage.success('Efficiency feedback replay 验证通过')
    } else {
      ElMessage.warning('Efficiency feedback replay 验证未通过')
    }
  } catch (err) {
    ElMessage.error(`验证 efficiency feedback replay 失败: ${String(err)}`)
  } finally {
    capabilityEfficiencyFeedbackReplayLoading.value = false
  }
}

const fetchCapabilityEfficiencyFeedbackReplays = async (silent = false) => {
  if (capabilityEfficiencyFeedbackReplayLibraryLoading.value) return
  capabilityEfficiencyFeedbackReplayLibraryLoading.value = true
  try {
    const response = await apiFetch('/api/capabilities/efficiency_feedback/replays?limit=20')
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.detail || result.message || '读取 efficiency feedback replay library 失败')
    }
    capabilityEfficiencyFeedbackReplayLibrary.value = Array.isArray(result.result?.reports)
      ? result.result.reports
      : []
    if (silent !== true) {
      ElMessage.success(`已加载 ${capabilityEfficiencyFeedbackReplayLibrary.value.length} 条 efficiency feedback replay reports`)
    }
  } catch (err) {
    ElMessage.error(`读取 efficiency feedback replay library 失败: ${String(err)}`)
  } finally {
    capabilityEfficiencyFeedbackReplayLibraryLoading.value = false
  }
}

const fetchCapabilityFailureFixtures = async (silent = false) => {
  if (capabilityFailureFixtureLibraryLoading.value) return
  capabilityFailureFixtureLibraryLoading.value = true
  try {
    const response = await apiFetch('/api/capabilities/failure_fixtures?limit=100')
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.detail || result.message || '读取 failure fixture library 失败')
    }
    capabilityFailureFixtureLibrary.value = Array.isArray(result.result?.fixtures)
      ? result.result.fixtures
      : []
    if (silent !== true) {
      ElMessage.success(`已加载 ${capabilityFailureFixtureLibrary.value.length} 个 failure fixtures`)
    }
  } catch (err) {
    ElMessage.error(`读取 failure fixture library 失败: ${String(err)}`)
  } finally {
    capabilityFailureFixtureLibraryLoading.value = false
  }
}

const fetchCapabilityFailureFixtureBatchHistory = async (silent = false) => {
  if (capabilityFailureFixtureBatchHistoryLoading.value) return
  capabilityFailureFixtureBatchHistoryLoading.value = true
  try {
    const response = await apiFetch('/api/capabilities/failure_fixture/replay_batches?limit=20')
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.detail || result.message || '读取 batch replay history 失败')
    }
    capabilityFailureFixtureBatchHistory.value = Array.isArray(result.result?.reports)
      ? result.result.reports
      : []
    capabilityFailureFixtureBatchHistoryTrend.value = result.result?.trend || null
    if (silent !== true) {
      ElMessage.success(`已加载 ${capabilityFailureFixtureBatchHistory.value.length} 条 batch replay history`)
    }
  } catch (err) {
    ElMessage.error(`读取 batch replay history 失败: ${String(err)}`)
  } finally {
    capabilityFailureFixtureBatchHistoryLoading.value = false
  }
}

const batchReplayCapabilityFailureFixtures = async () => {
  if (capabilityFailureFixtureBatchReplayLoading.value) return
  capabilityFailureFixtureBatchReplayLoading.value = true
  try {
    const response = await apiFetch('/api/capabilities/failure_fixture/replay_batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        limit: 100,
        name: 'frontend_capability_failure_fixture_batch_replay',
        save: true,
      }),
    })
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.detail || result.message || '批量验证 failure fixtures 失败')
    }
    capabilityFailureFixtureBatchReplayReport.value = result.result?.report || null
    capabilityFailureFixtureBatchReplayArtifact.value = result.result?.artifact || null
    if (capabilityFailureFixtureBatchReplayArtifact.value?.url) await fetchArtifacts()
    await fetchCapabilityFailureFixtures(true)
    await fetchCapabilityFailureFixtureBatchHistory(true)
    const count = Number(capabilityFailureFixtureBatchReplayReport.value?.fixture_count || 0)
    const passed = Boolean(capabilityFailureFixtureBatchReplayReport.value?.passed)
    if (!count) {
      ElMessage.warning('当前没有可批量验证的 failure fixtures')
    } else if (passed) {
      ElMessage.success(`Failure fixture batch replay 全部通过 (${count})`)
    } else {
      ElMessage.warning(`Failure fixture batch replay 未通过 ${capabilityFailureFixtureBatchReplayFailedItems.value.length}/${count}`)
    }
  } catch (err) {
    ElMessage.error(`批量验证 failure fixtures 失败: ${String(err)}`)
  } finally {
    capabilityFailureFixtureBatchReplayLoading.value = false
  }
}

// ── W: Offline replay — import a phase_<id>.jsonl ─────────────────────
//
// Symmetric inverse of ``exportPhaseEventsAsJsonl``. Parses the file
// client-side (no upload to backend), replaces ``phaseEvents`` with the
// imported buffer, and flips ``replayMode`` so the WS phase-event gate
// keeps the buffer pristine.
//
// Tolerates the same shapes the backend's K6 endpoint does:
//   • blank lines (skipped)
//   • malformed JSON lines (skipped, but counted in the warning toast)
//   • non-dict JSON values (skipped)
// We DON'T cap by PHASE_LIMIT here — a user importing a giant file
// presumably wants to see all of it. The Timeline render already
// virtualizes per-step so it handles ~10k events fine.

const triggerReplayImport = (target = 'timeline') => {
  // Programmatically click the hidden <input type="file"> so the user
  // gets the native picker. We reset the value first so re-importing
  // the same file fires onchange again (browsers debounce identical
  // selections otherwise).
  replayImportTarget.value = target === 'capability' ? 'capability' : 'timeline'
  const inp = replayInputRef.value
  if (!inp) return
  try {
    inp.value = ''
    inp.click()
  } catch (err) {
    // Quiet — file picker errors are essentially "user clicked cancel"
    // and the rest of the app is unaffected.
  }
}

const _parseJsonlText = (text) => {
  const out = []
  let bad = 0
  let total = 0
  const _capabilityExecuteArtifactPhaseEvent = (doc) => {
    if (!doc || typeof doc !== 'object' || Array.isArray(doc)) return null
    if (String(doc.type || '') !== 'capability_execute_trace') return null
    const result = doc.result && typeof doc.result === 'object' && !Array.isArray(doc.result) ? doc.result : {}
    return {
      type: 'phase',
      phase: 'capability_execute',
      severity: result.completed ? 'info' : 'warn',
      message: String(result.capability || result.fallback_reason || result.status || 'capability_execute'),
      ts: Number.isFinite(doc.created_at) ? doc.created_at : Date.now() / 1000,
      execution_status: result.status,
      completed: Boolean(result.completed),
      capability: result.capability,
      attempts: Array.isArray(result.attempts) ? result.attempts : [],
      verification: result.verification,
      fallback_reason: result.fallback_reason,
      artifact: result.artifact,
      trace_artifact: result.trace_artifact,
      runtime_summary: result.runtime_summary,
      runtime_drift: result.runtime_drift,
      runtime_issue_summary: result.runtime_issue_summary,
      action_trace: result.action_trace,
      action_issue_summary: result.action_issue_summary,
      failure_bundle: result.failure_bundle,
      route_intent: result.route?.intent,
    }
  }
  try {
    const doc = JSON.parse(String(text || '').trim())
    const artifactEvent = _capabilityExecuteArtifactPhaseEvent(doc)
    if (artifactEvent) return { events: [artifactEvent], total: 1, bad: 0 }
  } catch (err) {
    // fall through to JSONL parsing
  }
  // Normalize line endings: a phase log captured on Windows may carry
  // CRLF and we don't want a stray '\r' breaking the JSON parser.
  const lines = String(text || '').replace(/\r\n?/g, '\n').split('\n')
  for (const raw of lines) {
    const line = raw.trim()
    if (!line) continue
    total += 1
    try {
      const obj = JSON.parse(line)
      if (obj && typeof obj === 'object' && !Array.isArray(obj)) {
        const artifactEvent = _capabilityExecuteArtifactPhaseEvent(obj)
        if (artifactEvent) {
          out.push(artifactEvent)
          continue
        }
        const phaseEvent = obj?.detail?.phase_event
        out.push(phaseEvent && typeof phaseEvent === 'object' && !Array.isArray(phaseEvent) ? phaseEvent : obj)
        continue
      }
    } catch (err) {
      // fall through to bad counter
    }
    bad += 1
  }
  return { events: out, total, bad }
}

const handleReplayFileChange = async (event) => {
  const target = event && event.target
  const file = target && target.files && target.files[0]
  if (!file) return

  // Sanity cap — refuse files > 16 MB. A typical 200-step run produces
  // a phase JSONL well under 1 MB; anything larger is almost certainly
  // an accidental selection (or malicious upload from a screenshot).
  const MAX_BYTES = 16 * 1024 * 1024
  if (file.size > MAX_BYTES) {
    ElMessage.error(
      `文件过大 (${(file.size / 1024 / 1024).toFixed(1)} MB)，` +
      `请选择 ≤ 16 MB 的 phase JSONL 文件`,
    )
    return
  }

  let text = ''
  try {
    text = await file.text()
  } catch (err) {
    ElMessage.error(`读取失败：${String(err)}`)
    return
  }

  const { events, total, bad } = _parseJsonlText(text)
  if (events.length === 0) {
    ElMessage.warning('文件中没有可识别的 phase 事件（请确认是 phase_<id>.jsonl 格式）')
    return
  }

  // Decorate with synthetic _ts so render code that reads it (the chip
  // sort path in M) keeps working without a special case for replay.
  for (const e of events) {
    if (typeof e._ts !== 'number') {
      e._ts = Number.isFinite(e.ts) ? e.ts : Date.now() / 1000
    }
  }

  // Replace the buffer atomically — assign a fresh array so Vue picks
  // up the change in one tick instead of N pushes (PHASE_LIMIT splice
  // would also fire on every push).
  phaseEvents.value = events
  replayMode.value = true
  replaySourceName.value = file.name || 'imported.jsonl'

  // Reset filters and pin to bottom: the new buffer's phase set may
  // not match what was excluded before.
  phaseFilterExclude.value = new Set()
  severityFilterExclude.value = new Set()
  timelineAutoScroll.value = true

  // Switch to the requested tab so the user sees the replay target immediately.
  setActiveBottomTab(replayImportTarget.value === 'capability' ? 'capability' : 'timeline')

  if (bad > 0) {
    ElMessage.warning(
      `已导入 ${events.length} / ${total} 条事件（跳过 ${bad} 行损坏数据）`,
    )
  } else {
    ElMessage.success(`已导入 ${events.length} 条事件，进入回放模式`)
  }
}

const exitReplayMode = () => {
  replayMode.value = false
  replaySourceName.value = ''
  phaseEvents.value = []
  hasNewCapability.value = false
  phaseFilterExclude.value = new Set()
  severityFilterExclude.value = new Set()
  ElMessage.info('已退出回放模式')
}

// ── P: Timeline scroll helpers ─────────────────────────────────────────
// Get the inner wrap element of el-scrollbar so we can read scrollTop /
// scrollHeight / clientHeight directly. el-scrollbar exposes wrapRef in
// modern Element Plus; older builds expose .wrap$/. wrap_.
function _timelineWrap() {
  const sb = timelineRef.value
  if (!sb) return null
  return sb.wrapRef || sb.wrap$ || sb.wrap_ || null
}

function _isAtBottom(wrap) {
  if (!wrap) return true
  const remaining = wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight
  return remaining <= SCROLL_BOTTOM_EPS
}

const scrollTimelineToBottom = async () => {
  await nextTick()
  const sb = timelineRef.value
  if (!sb) return
  if (typeof sb.setScrollTop === 'function') {
    sb.setScrollTop(Number.MAX_SAFE_INTEGER)
  } else {
    const wrap = _timelineWrap()
    if (wrap) wrap.scrollTop = wrap.scrollHeight
  }
  timelineAutoScroll.value = true
}

// T: Scroll Timeline to top. Inverse of scrollTimelineToBottom. Used by
// the Home shortcut. We DON'T touch timelineAutoScroll here — jumping to
// top is a "give me history" gesture, the user almost certainly doesn't
// want chips to keep auto-following the tail and yanking them back.
const scrollTimelineToTop = async () => {
  await nextTick()
  const sb = timelineRef.value
  if (!sb) return
  if (typeof sb.setScrollTop === 'function') {
    sb.setScrollTop(0)
  } else {
    const wrap = _timelineWrap()
    if (wrap) wrap.scrollTop = 0
  }
  timelineAutoScroll.value = false
}

// Called by el-scrollbar's @scroll. Element Plus passes
// {scrollLeft, scrollTop} but we re-read from wrapRef to also get the
// scrollHeight (not provided in the event payload).
const onTimelineScroll = () => {
  const wrap = _timelineWrap()
  if (!wrap) return
  // Pin auto-scroll iff the user is already near the bottom. This makes
  // the panel behave like a terminal: scroll up to read history → tail
  // pauses; scroll back to bottom → tail resumes.
  timelineAutoScroll.value = _isAtBottom(wrap)
}

// ── N: Phase event detail dialog helpers ─────────────────────────────
// openPhaseDialog: assign the chip's event to selectedPhaseEvent and show
// the modal. Always pass a structured-clone-friendly object (raw payload
// is already a plain dict from JSON.parse).
const openPhaseDialog = (evt) => {
  selectedPhaseEvent.value = evt || null
  phaseDialogVisible.value = !!evt
}

// ── S: Timeline chip double-click → copy JSON shortcut ────────────────
// Power-user affordance: dbl-click a chip and we copy its full event JSON
// straight to the clipboard, skipping the dialog. Single-click still opens
// the dialog. Browser's native click handler fires BEFORE dblclick, so we
// defer single-click dialog-open by 220 ms; if a second click arrives in
// that window we cancel the open and do the copy instead.
//
// The 220 ms threshold matches typical OS-level double-click windows
// (Windows default = 500 ms but most users tap much faster); a longer
// delay would make the single-click feel sluggish.
const _CHIP_DBLCLICK_WINDOW_MS = 220
let _chipClickTimer = null

// Build the same _ts-stripped JSON string the dialog body uses, but for an
// ARBITRARY event (the dialog's selectedPhaseJson computed is tied to
// selectedPhaseEvent). Returns '' on bad input.
const buildPhaseEventJsonString = (evt) => {
  if (!evt || typeof evt !== 'object') return ''
  const clone = {}
  for (const k of Object.keys(evt)) {
    if (k === '_ts') continue
    clone[k] = evt[k]
  }
  try {
    return JSON.stringify(clone, null, 2)
  } catch (err) {
    return String(err)
  }
}

// Same clipboard fallback chain as copyPhaseJson + finalAnswerText copy.
const _writeToClipboard = async (text) => {
  if (!text) return false
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text)
    } else {
      const ta = document.createElement('textarea')
      ta.value = text
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    return true
  } catch (err) {
    ElMessage.error(`复制失败: ${String(err)}`)
    return false
  }
}

const onChipClick = (evt) => {
  // Defer the dialog open; dblclick handler may cancel us.
  if (_chipClickTimer) {
    clearTimeout(_chipClickTimer)
    _chipClickTimer = null
  }
  _chipClickTimer = setTimeout(() => {
    _chipClickTimer = null
    openPhaseDialog(evt)
  }, _CHIP_DBLCLICK_WINDOW_MS)
}

const onChipDblClick = async (evt) => {
  // Suppress the pending single-click dialog-open.
  if (_chipClickTimer) {
    clearTimeout(_chipClickTimer)
    _chipClickTimer = null
  }
  const text = buildPhaseEventJsonString(evt)
  if (!text) return
  const ok = await _writeToClipboard(text)
  if (ok) {
    // Short toast — explicit "dblclick" word so the user learns the gesture
    // is intentional (vs. assuming they triggered a duplicate by accident).
    const phaseName = String(evt && evt.phase || 'phase')
    ElMessage.success(`已复制 ${phaseName} 事件 JSON (双击)`)
  }
}

// Pretty JSON for the dialog body. Strips the internal _ts field so the
// user only sees what actually came over the wire.
const selectedPhaseJson = computed(() => {
  const e = selectedPhaseEvent.value
  if (!e) return ''
  // Shallow-copy and drop the synthetic _ts key
  const clone = {}
  for (const k of Object.keys(e)) {
    if (k === '_ts') continue
    clone[k] = e[k]
  }
  try {
    return JSON.stringify(clone, null, 2)
  } catch (err) {
    return String(err)
  }
})

// Human-readable timestamp for the dialog header. Falls back to '—'
// when ts is missing or invalid.
const selectedPhaseTime = computed(() => {
  const e = selectedPhaseEvent.value
  const ts = e && Number.isFinite(e.ts) ? e.ts : null
  if (ts == null) return '—'
  try {
    const d = new Date(ts * 1000)
    // YYYY-MM-DD HH:MM:SS.mmm
    const pad = (n, w = 2) => String(n).padStart(w, '0')
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
      + `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
      + `.${pad(d.getMilliseconds(), 3)}`
  } catch (err) {
    return '—'
  }
})

// Copy the full JSON to clipboard. Same fallback chain as the F1 Final
// Answer copy button (clipboard API → execCommand → manual textarea).
const copyPhaseJson = async () => {
  const text = selectedPhaseJson.value
  if (!text) return
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text)
    } else {
      const ta = document.createElement('textarea')
      ta.value = text
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    ElMessage.success('已复制 JSON')
  } catch (err) {
    ElMessage.error(`复制失败: ${String(err)}`)
  }
}

// ── T: Keyboard shortcuts dispatcher ──────────────────────────────────
// A single window-level keydown listener routes to handlers based on the
// current UI context (which tab / dialog is active). Design notes:
//
//   • Modifier portability: Ctrl on Windows/Linux, Cmd on macOS. We treat
//     ``event.ctrlKey || event.metaKey`` as the "primary" modifier so the
//     same bindings feel native everywhere.
//   • Input-typing guard: when focus is in <input>/<textarea>/contenteditable,
//     we skip MOST global shortcuts (otherwise Ctrl+K would steal focus
//     mid-edit). The Ctrl+Enter SUBMIT shortcut intentionally bypasses
//     this guard — submitting from inside the prompt textarea is exactly
//     the workflow we want.
//   • Dialog-active guard: when phaseDialogVisible is true, ←/→ navigate
//     between events; other shortcuts (Ctrl+Enter, tab switches, etc.)
//     still work — they're independent of the dialog state.
//   • Browser-default conflict: Ctrl+1..6 / Ctrl+K / Ctrl+/ / Ctrl+E all
//     have native browser meanings. We unconditionally preventDefault on
//     a hit so the browser doesn't open the address bar or jump to the
//     wrong window tab. The user's mental model in an SPA expects this.

// Tab name lookup for Ctrl+1..6 — kept here so the help dialog can use
// it as a single source of truth.
const TAB_ORDER = ['terminal', 'timeline', 'capability', 'final', 'artifacts', 'runs', 'failed']

// Switch active tab + apply the same badge-clearing side effects the
// el-tabs @tab-change handler does, since direct assignment to the model
// doesn't fire the event.
const setActiveBottomTab = (name) => {
  if (!TAB_ORDER.includes(name)) return
  activeBottomTab.value = name
  if (name === 'artifacts') hasNewArtifacts.value = false
  if (name === 'runs') hasNewRuns.value = false
  if (name === 'final') hasNewFinalAnswer.value = false
  if (name === 'timeline') {
    hasNewPhase.value = false
    if (timelineAutoScroll.value) scrollTimelineToBottom()
  }
  if (name === 'capability') hasNewCapability.value = false
  if (name === 'failed') hasNewFailures.value = false
}

// Focus the Prompt textarea programmatically. Element Plus el-input
// exposes a ``.focus()`` method on its component instance (NOT the
// DOM element). The :ref bound to el-input gives us that instance.
const focusPromptInput = () => {
  const inp = promptInputRef.value
  if (!inp) return
  try {
    if (typeof inp.focus === 'function') {
      inp.focus()
    } else if (inp.$el && inp.$el.querySelector) {
      // Fallback: dig into the rendered DOM for the textarea node
      const ta = inp.$el.querySelector('textarea, input')
      if (ta && typeof ta.focus === 'function') ta.focus()
    }
  } catch (err) {
    // Quiet — focus failures are non-fatal and almost always mean the
    // input was unmounted between scheduling and dispatch.
  }
}

// Phase event navigation in the JSON detail dialog.
// Uses filteredPhaseEvents (the same view the user sees) so ←/→ skips
// over events the user has hidden via O's phase/severity filters.
const _findCurrentEventIndex = () => {
  const cur = selectedPhaseEvent.value
  if (!cur) return -1
  const list = filteredPhaseEvents.value
  // Identity match first (same object ref) — fast path when the dialog
  // was opened from the visible chip list.
  let idx = list.indexOf(cur)
  if (idx !== -1) return idx
  // Fall back to ts + phase match (covers re-render cases where the
  // proxy identity changed but the underlying event is the same).
  const curTs = cur.ts
  const curPhase = cur.phase
  for (let i = 0; i < list.length; i += 1) {
    const e = list[i]
    if (e && e.ts === curTs && e.phase === curPhase) return i
  }
  return -1
}

const goToPrevPhaseEvent = () => {
  const list = filteredPhaseEvents.value
  if (!list.length) return
  const idx = _findCurrentEventIndex()
  // If current event isn't in the filtered list, idx === -1 → land on
  // the LAST event (treat ← as "show me the newest").
  const next = idx <= 0 ? list.length - 1 : idx - 1
  selectedPhaseEvent.value = list[next]
}

const goToNextPhaseEvent = () => {
  const list = filteredPhaseEvents.value
  if (!list.length) return
  const idx = _findCurrentEventIndex()
  // idx === -1 → land on the FIRST event (treat → as "show me the oldest").
  const next = idx === -1 || idx >= list.length - 1 ? 0 : idx + 1
  selectedPhaseEvent.value = list[next]
}

// Should the current focus suppress global shortcuts? True when the user
// is typing into a text-bearing element.
const _isTypingTarget = (target) => {
  if (!target) return false
  const tag = String(target.tagName || '').toUpperCase()
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true
  if (target.isContentEditable) return true
  return false
}

const handleGlobalKeydown = (event) => {
  // Use ctrl OR meta as the primary modifier so macOS users get the same
  // bindings as Windows/Linux users without re-learning anything.
  const primary = event.ctrlKey || event.metaKey
  const typing = _isTypingTarget(event.target)

  // ── Ctrl+Enter: submit task (works even from inside the textarea) ──
  if (primary && event.key === 'Enter') {
    event.preventDefault()
    if (!isRunning.value) submitTask()
    return
  }

  if (
    activeBottomTab.value === 'terminal'
    && primary
    && (event.key === 'f' || event.key === 'F')
  ) {
    event.preventDefault()
    openTerminalSearch()
    return
  }

  // Everything below is suppressed while typing (except dialog navigation,
  // which is impossible to reach while editing anyway because the dialog
  // grabs focus).
  if (typing) return

  // ── Ctrl+/: open the cheat-sheet dialog ──
  // Browser key for "/" can come through as event.key === '/' or '?'
  // depending on Shift state. We also accept Ctrl+? for laptops where
  // the slash requires Shift.
  if (primary && (event.key === '/' || event.key === '?')) {
    event.preventDefault()
    helpDialogVisible.value = !helpDialogVisible.value
    return
  }

  // ── Ctrl+K: focus the Prompt input ──
  if (primary && (event.key === 'k' || event.key === 'K')) {
    event.preventDefault()
    focusPromptInput()
    return
  }

  // ── Ctrl+1..6: switch bottom tab ──
  if (primary && event.key >= '1' && event.key <= '9') {
    const i = parseInt(event.key, 10) - 1
    if (i >= 0 && i < TAB_ORDER.length) {
      event.preventDefault()
      setActiveBottomTab(TAB_ORDER[i])
      return
    }
  }

  // ── Phase dialog navigation: ← / → between events ──
  if (phaseDialogVisible.value) {
    if (event.key === 'ArrowLeft') {
      event.preventDefault()
      goToPrevPhaseEvent()
      return
    }
    if (event.key === 'ArrowRight') {
      event.preventDefault()
      goToNextPhaseEvent()
      return
    }
  }

  // ── K6: Failed-run dialog navigation: ← / → between rows ──
  // Same pattern as the phase dialog but driven by failedRunsList. We
  // keep this branch separate (not unified with phaseDialog) because
  // the two dialogs are mutually exclusive in practice and combining
  // them would obscure which list is being navigated.
  if (failedRunDialogVisible.value) {
    if (event.key === 'ArrowLeft') {
      event.preventDefault()
      goToPrevFailedRun()
      return
    }
    if (event.key === 'ArrowRight') {
      event.preventDefault()
      goToNextFailedRun()
      return
    }
  }

  if (activeBottomTab.value === 'terminal' && terminalSearchVisible.value) {
    if (event.key === 'Escape') {
      event.preventDefault()
      closeTerminalSearch()
      return
    }
    if (event.key === 'Enter' && event.shiftKey) {
      event.preventDefault()
      terminalSearchPrev()
      return
    }
    if (event.key === 'Enter') {
      event.preventDefault()
      terminalSearchNext()
      return
    }
  }

  // ── Timeline tab-only shortcuts ──
  if (activeBottomTab.value === 'timeline') {
    // Ctrl+E: export the current filtered Timeline as JSONL
    if (primary && (event.key === 'e' || event.key === 'E')) {
      event.preventDefault()
      exportPhaseEventsAsJsonl()
      return
    }
    // End: jump to bottom + resume auto-follow
    if (event.key === 'End' && !primary) {
      event.preventDefault()
      scrollTimelineToBottom()
      return
    }
    // Home: jump to top + pause auto-follow
    if (event.key === 'Home' && !primary) {
      event.preventDefault()
      scrollTimelineToTop()
      return
    }
  }

  if (activeBottomTab.value === 'capability') {
    if (primary && (event.key === 'e' || event.key === 'E')) {
      event.preventDefault()
      exportCapabilityTraceAsJsonl()
      return
    }
  }
}

const refreshOutputContractPreview = async () => {
  const text = String(prompt.value || '').trim()
  if (!text) {
    outputContractPreview.value = null
    outputContractPreviewLoading.value = false
    return
  }
  outputContractPreviewLoading.value = true
  try {
    const result = await fetchOutputContractPreview(text)
    outputContractPreview.value = result.status === 'success' ? result.formatted : null
  } catch {
    outputContractPreview.value = null
  } finally {
    outputContractPreviewLoading.value = false
  }
}

watch(prompt, () => {
  if (outputContractPreviewTimer) clearTimeout(outputContractPreviewTimer)
  outputContractPreviewTimer = setTimeout(() => {
    refreshOutputContractPreview()
  }, 450)
})

const submitTask = async () => {
  const validation = validateTaskInput({ prompt: prompt.value })
  if (!validation.ok) {
    ElMessage.warning(validation.message)
    return
  }

  isRunning.value = true
  clearTerminalLogs()
  currentImageBase64.value = ''
  // M: clear timeline buffer at the start of every new run so phases
  // from old runs don't bleed into the new timeline view.
  phaseEvents.value = []
  hasNewPhase.value = false
  hasNewCapability.value = false
  // O: drop any filters left over from the previous run; phase set may
  // differ and an excluded phase from before would silently hide events.
  phaseFilterExclude.value = new Set()
  severityFilterExclude.value = new Set()
  // W: starting a fresh run implicitly exits replay mode — otherwise
  // the WS phase events for the new run would be silently dropped by
  // the gate in the WS handler.
  if (replayMode.value) {
    replayMode.value = false
    replaySourceName.value = ''
  }
  // P: re-pin the tail since the panel is empty again
  timelineAutoScroll.value = true

  // ── Final Answer：进入"执行中/等待"状态 ──
  taskResult.value = null
  finalAnswerText.value = ''
  finalAnswerStatus.value = 'pending'
  hasNewFinalAnswer.value = false
  finalAnswerExpanded.value = false  // F2: 新任务默认折叠
  finalAnswerCopyState.value = 'idle' // F1: 复位 copy 反馈
  finalAnswerDomain.value = ''       // F3: 清除上轮的域卡片
  artifactsCountAtSubmit = artifactList.value.length
  fetchBrowserRuntimeStatus()

  const formData = new FormData()
  const normalizedTargetUrl = url.value.trim()
  const normalizedExtraUrls = extraUrls.value.trim()
  formData.append('target_url', normalizedTargetUrl)
  formData.append('goal', prompt.value.trim())
  const authoritativeUrls = buildAuthoritativeUrlsPayload(normalizedTargetUrl, normalizedExtraUrls)
  if (normalizedExtraUrls || (!normalizedTargetUrl && authoritativeUrls.length)) {
    formData.append('urls', JSON.stringify(authoritativeUrls))
  }
  const constraints = buildTaskConstraints({
    proxyServer: proxyServer.value,
    proxyUsername: proxyUsername.value,
    proxyPassword: proxyPassword.value,
    maxRuns: batchMaxRuns.value,
    resume: resumeEnabled.value,
  })
  appendConstraintsToFormData(formData, constraints)
  if (selectedModel.value !== 'backend-default') {
    formData.append('vlm_model', selectedModel.value)
  }
  if (selectedSemanticModel.value !== 'backend-default') {
    formData.append('semantic_model', selectedSemanticModel.value)
  }
  formData.append('vlm_model_type', selectedModelType.value)
  formData.append('vlm_temperature', String(modelTemperature.value))
  formData.append('vlm_max_tokens', String(modelMaxTokens.value))
  if (modelBaseUrl.value.trim()) {
    formData.append('vlm_base_url', modelBaseUrl.value.trim())
  }
  if (modelApiKey.value.trim()) {
    formData.append('vlm_api_key', modelApiKey.value.trim())
  }
  if (semanticBaseUrl.value.trim()) {
    formData.append('semantic_base_url', semanticBaseUrl.value.trim())
  }
  if (semanticApiKey.value.trim()) {
    formData.append('semantic_api_key', semanticApiKey.value.trim())
  }
  if (selectedAuthProfiles.value.length) {
    formData.append('auth_profiles', selectedAuthProfiles.value.join(','))
  }
  if (selectedFile.value) {
    formData.append('file', selectedFile.value)
    appendAttachmentIntentToFormData(formData, attachmentIntent.value)
  }

  try {
    const response = await apiFetch('/api/start_batch', {
      method: 'POST',
      body: formData,
    })

    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.message || '任务启动失败')
    }

    await appendLog('[SYSTEM] 任务已提交，等待后端执行...')
    ElMessage.success('任务已在后台启动')
  } catch (err) {
    isRunning.value = false
    ElMessage.error(`提交失败: ${String(err)}`)
    await appendLog(`[ERROR] 提交失败: ${String(err)}`)
  }
}

const forceStop = async () => {
  try {
    const response = await apiFetch('/api/stop_batch', {
      method: 'POST',
    })
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.message || '停止请求失败')
    }
    isRunning.value = false
    ElMessage.warning('正在强制终止后台任务...')
    await appendLog('[WARN] 已发送强制终止请求')
  } catch (err) {
    ElMessage.error(`强制终止失败: ${String(err)}`)
    await appendLog(`[ERROR] 强制终止失败: ${String(err)}`)
  }
}

onMounted(() => {
  document.documentElement.classList.add('dark')
  loadModelSettings()
  connectWebSocket()
  loadAuthProfiles()
  loadCaptchaSolverStatus()
  fetchArtifacts()
  fetchBrowserRuntimeStatus()
  // K3: seed the failed-runs drawer with historic records so the
  // tab is informative even before the user runs anything in this session.
  fetchFailedRuns()
  // T: register the global keyboard shortcut dispatcher. ``window`` (not
  // document) so the listener fires regardless of which element has
  // focus and even when the page has a click-outside-the-app gesture.
  window.addEventListener('keydown', handleGlobalKeydown)
})

onUnmounted(() => {
  isUnmounted = true
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
  if (socket) {
    socket.close()
    socket = null
  }
  // S: cancel any pending Timeline chip single-click dialog-open so the
  // callback doesn't fire after the component is gone (would touch
  // selectedPhaseEvent/phaseDialogVisible refs and crash on detached state).
  if (_chipClickTimer) {
    clearTimeout(_chipClickTimer)
    _chipClickTimer = null
  }
  // U: cancel the pending output-contract preview debounce so its callback
  // can't fire after unmount (it issues a fetch and writes refs on a now
  // detached component).
  if (outputContractPreviewTimer) {
    clearTimeout(outputContractPreviewTimer)
    outputContractPreviewTimer = null
  }
  // F1: cancel the pending copy-feedback reset so it can't write
  // finalAnswerCopyState on a detached component after unmount.
  if (finalAnswerCopyTimer) {
    clearTimeout(finalAnswerCopyTimer)
    finalAnswerCopyTimer = null
  }
  // T: drop the global keyboard listener so HMR / route changes don't
  // leave a zombie listener behind (would crash trying to use closed-
  // over refs).
  window.removeEventListener('keydown', handleGlobalKeydown)
})
// B: 按钮墙收纳进下拉后的 command 分发（指向原有 handler，不改行为）
const handleCapabilityMoreAction = (command) => {
  const handlers = {
    copySummary: copyCapabilityTraceSummary,
    generateFixture: generateCapabilityFailureFixture,
    replayFixture: replayCapabilityFailureFixture,
    refreshFixtures: fetchCapabilityFailureFixtures,
    refreshBatchHistory: fetchCapabilityFailureFixtureBatchHistory,
    batchReplay: batchReplayCapabilityFailureFixtures,
    replayEfficiency: replayCapabilityEfficiencyFeedback,
    refreshEfficiencyReplays: fetchCapabilityEfficiencyFeedbackReplays,
    importReplay: () => triggerReplayImport('capability'),
  }
  handlers[command]?.()
}

const handleTimelineMoreAction = (command) => {
  const handlers = {
    exportJsonl: exportPhaseEventsAsJsonl,
    importReplay: () => triggerReplayImport('timeline'),
    clear: clearPhaseEvents,
  }
  handlers[command]?.()
}
// C2: 抽屉关闭时在左栏回显当前配置概要
const settingsSummaryText = computed(() => {
  const identity = selectedAuthProfiles.value.length
    ? selectedAuthProfiles.value.join('、')
    : '默认身份'
  const attach = selectedFile.value ? selectedFile.value.name : '无附件'
  return `${selectedModel.value} · ${selectedSemanticModel.value} · ${identity} · ${attach}`
})
</script>

<template>
  <main class="app-shell">
    <section class="control-panel vspider-panel">
      <header class="brand-header">
        <div class="brand-mark">
          <Monitor class="brand-icon" />
        </div>
        <div>
          <h1>VSpider Control Center</h1>
          <p>任务编排与执行入口</p>
        </div>
      </header>

      <div class="control-scroll">
        <div class="field-group">
          <label>目标 URL <small class="field-hint">（可选，留空时从业务指令推断）</small></label>
          <el-input
            v-model="url"
            clearable
            :disabled="isRunning"
            placeholder="https://example.com（可留空）"
          />
        </div>

        <div class="field-group">
          <label>业务指令</label>
          <el-input
            ref="promptInputRef"
            v-model="prompt"
            type="textarea"
            resize="none"
            :autosize="{ minRows: 4, maxRows: 8 }"
            :disabled="isRunning"
            placeholder="描述你希望 VSpider 执行的业务任务 (Ctrl+Enter 提交，Ctrl+K 聚焦本框，Ctrl+/ 查看快捷键)"
          />
        </div>

        <div v-if="outputContractPreviewLoading || outputContractPreview" class="field-group output-contract-preview">
          <label>推断输出形态 <small class="field-hint">（由 goal 自动推断，非固定 Excel）</small></label>
          <div v-if="outputContractPreviewLoading" class="output-contract-preview__loading">推断中…</div>
          <div v-else-if="outputContractPreview" class="output-contract-preview__card">
            <div class="output-contract-preview__row">
              <span class="output-contract-preview__label">类型</span>
              <strong>{{ outputContractPreview.kind_label }}</strong>
              <span class="output-contract-preview__sep">→</span>
              <span class="output-contract-preview__label">落盘</span>
              <strong>{{ outputContractPreview.container_label }}</strong>
              <el-tag size="small" type="info">{{ outputContractPreview.mode }}</el-tag>
            </div>
            <p v-if="outputContractPreview.reasons.length" class="output-contract-preview__reasons">
              {{ outputContractPreview.reasons.slice(0, 2).join('；') }}
            </p>
          </div>
        </div>

        <button type="button" class="settings-summary" @click="settingsDrawerOpen = true">
          <span class="settings-summary__title">
            <el-icon><Setting /></el-icon>
            高级配置
            <span class="settings-summary__open">打开 ›</span>
          </span>
          <span class="settings-summary__echo">{{ settingsSummaryText }}</span>
        </button>
      </div>

      <footer class="action-footer">
        <el-button
          type="primary"
          class="run-button"
          :icon="VideoPlay"
          :loading="isRunning"
          @click="submitTask"
        >
          开始执行
        </el-button>
        <el-button
          class="stop-button"
          :icon="Close"
          :disabled="!isRunning"
          @click="forceStop"
        >
          强制终止
        </el-button>
      </footer>

      <!-- C2: 高级配置抽屉 — 双脑调度/身份/运行约束/附件统一入口 -->
      <el-drawer
        v-model="settingsDrawerOpen"
        title="高级配置"
        direction="rtl"
        size="440px"
        class="settings-drawer"
      >
          <el-collapse v-model="settingsActivePanels" class="advanced-collapse drawer-collapse">
          <el-collapse-item name="models">
            <template #title>
              <span>双脑调度中心</span>
              <span class="collapse-title-echo">{{ selectedModel }} · {{ selectedSemanticModel }}</span>
            </template>
        <div class="field-group model-center">
          <div class="field-title-row">
            <label>模型选择</label>
            <el-popover
              v-model:visible="modelSettingsOpen"
              placement="right-start"
              width="360"
              trigger="click"
            >
              <template #reference>
                <el-button text size="small" :icon="Setting">
                  Advanced
                </el-button>
              </template>
              <div class="model-popover">
                <label>Temperature: {{ modelTemperature }}</label>
                <el-slider
                  v-model="modelTemperature"
                  :min="0"
                  :max="2"
                  :step="0.1"
                  :disabled="isRunning"
                />
                <label>Max Tokens</label>
                <el-input-number
                  v-model="modelMaxTokens"
                  :min="512"
                  :max="32768"
                  :step="512"
                  :disabled="isRunning"
                  class="full-width"
                />
                <label>Base URL Override</label>
                <el-input
                  v-model="modelBaseUrl"
                  clearable
                  :disabled="isRunning"
                  placeholder="http://localhost:8000/v1"
                />
                <label>API Key Override</label>
                <el-input
                  v-model="modelApiKey"
                  clearable
                  show-password
                  :disabled="isRunning"
                  placeholder="empty = backend default"
                />
                <label>Semantic Base URL Override</label>
                <el-input
                  v-model="semanticBaseUrl"
                  clearable
                  :disabled="isRunning"
                  placeholder="https://api.deepseek.com"
                />
                <label>Semantic API Key Override</label>
                <el-input
                  v-model="semanticApiKey"
                  clearable
                  show-password
                  :disabled="isRunning"
                  placeholder="empty = backend/default VLM key"
                />
              </div>
            </el-popover>
          </div>

          <el-select
            v-model="selectedModel"
            :disabled="isRunning"
            filterable
            allow-create
            default-first-option
            class="full-width"
            placeholder="选择或输入 Model ID"
          >
            <el-option-group label="视觉多模态大模型 (VL)">
              <el-option value="backend-default" label="Backend Default" />
              <el-option value="qwen3-vl-plus" label="Qwen-VL-Plus" />
              <el-option value="local-74b-vl" label="内网本地 74B VL 模型" />
            </el-option-group>
            <el-option-group label="纯文本逻辑模型 (Text)">
              <el-option value="deepseek-chat" label="DeepSeek-V3" />
              <el-option value="deepseek-reasoner" label="DeepSeek-R1" />
              <el-option value="deepseek-v4-flash" label="DeepSeek-V4-Flash" />
              <el-option value="deepseek-v4-pro" label="DeepSeek-V4-Pro" />
            </el-option-group>
          </el-select>

          <el-select
            v-model="selectedSemanticModel"
            :disabled="isRunning"
            filterable
            allow-create
            default-first-option
            class="full-width"
            placeholder="选择或输入 Model ID"
          >
            <el-option value="backend-default" label="Semantic Backend Default" />
            <el-option value="deepseek-chat" label="DeepSeek-V3" />
            <el-option value="deepseek-reasoner" label="DeepSeek-R1" />
            <el-option value="deepseek-v4-flash" label="DeepSeek-V4-Flash" />
            <el-option value="deepseek-v4-pro" label="DeepSeek-V4-Pro" />
            <el-option value="qwen3-vl-plus" label="Qwen-VL-Plus" />
            <el-option value="local-74b-vl" label="Local 74B VL" />
          </el-select>

          <p v-if="selectedModelType === 'text'" class="model-warning">
            当前为纯文本模型，将自动剥离图像，仅依赖 AX Tree 执行任务。
          </p>
        </div>
          </el-collapse-item>
          <el-collapse-item name="identity">
            <template #title>
              <span>身份选择</span>
              <span class="collapse-title-echo">{{ selectedAuthProfiles.length ? selectedAuthProfiles.join('、') : '未选择（可选）' }}</span>
            </template>
        <div class="field-group">
          <div class="field-title-row">
            <label>Auth Profile</label>
            <div class="field-actions">
              <el-button
                text
                size="small"
                :icon="Refresh"
                @click="loadAuthProfiles"
              >
                Refresh
              </el-button>
              <el-button
                text
                size="small"
                :icon="Setting"
                @click="authDialogOpen = true"
              >
                Manage
              </el-button>
            </div>
          </div>

          <el-select
            v-model="selectedAuthProfiles"
            multiple
            filterable
            allow-create
            collapse-tags
            collapse-tags-tooltip
            :disabled="isRunning"
            placeholder="选择或输入 Auth Profile"
            class="full-width"
          >
            <el-option
              v-for="item in authProfileOptions"
              :key="item.name"
              :label="authProfileOptionLabel(item)"
              :value="item.name"
            />
          </el-select>
        </div>
          </el-collapse-item>
          <el-collapse-item title="运行约束（可选）" name="constraints">
            <label>附加 URL 列表</label>
            <el-input
              v-model="extraUrls"
              type="textarea"
              :rows="2"
              :disabled="isRunning"
              placeholder="每行一个 URL，或逗号分隔；与目标 URL 组成多起点任务"
              class="full-width"
            />
            <label>代理服务器</label>
            <el-input
              v-model="proxyServer"
              clearable
              :disabled="isRunning"
              placeholder="http://127.0.0.1:7890"
              class="full-width"
            />
            <label>代理用户名</label>
            <el-input
              v-model="proxyUsername"
              clearable
              :disabled="isRunning"
              class="full-width"
            />
            <label>代理密码</label>
            <el-input
              v-model="proxyPassword"
              clearable
              show-password
              :disabled="isRunning"
              class="full-width"
            />
            <label>批处理最大并发（max_runs）</label>
            <el-input-number
              v-model="batchMaxRuns"
              :min="0"
              :max="16"
              :disabled="isRunning"
              class="full-width"
            />
            <label>断点续跑（resume）</label>
            <el-switch
              v-model="resumeEnabled"
              :disabled="isRunning"
              active-text="开"
              inactive-text="关"
            />
            <p class="file-status">
              开启后：跳过上次已完成的行 / 步骤 / 已下载字节（batch / run / 媒体下载统一生效）
            </p>
            <p class="file-status">
              Captcha Solver：
              <span :class="captchaSolverEnabled ? 'solver-on' : 'solver-off'">
                {{ captchaSolverEnabled ? `已配置 (${captchaSolverProvider || 'auto'})` : '未配置（仅 HITL）' }}
              </span>
            </p>
          </el-collapse-item>
          <el-collapse-item title="附件（可选）" name="file">
            <el-upload
              drag
              class="compact-upload"
              :auto-upload="false"
              :limit="1"
              :disabled="isRunning"
              :accept="ATTACHMENT_ACCEPT"
              :on-change="handleUploadChange"
              :on-remove="handleUploadRemove"
            >
              <el-icon class="upload-icon">
                <UploadFilled />
              </el-icon>
              <div class="upload-copy">拖拽文件到此处，或点击选择</div>
            </el-upload>
            <p class="file-status">
              {{ ATTACHMENT_HINT }}
            </p>
            <p class="file-status">
              当前文件：{{ selectedFile ? selectedFile.name : '未选择文件，当前为单任务模式' }}
            </p>
            <template v-if="selectedFile">
              <el-select
                v-model="attachmentIntent"
                :disabled="isRunning"
                size="small"
                class="attachment-intent-select"
                placeholder="附件用途"
              >
                <el-option
                  v-for="option in ATTACHMENT_INTENT_OPTIONS"
                  :key="option.value"
                  :label="option.label"
                  :value="option.value"
                />
              </el-select>
              <p class="file-status">
                附件用途：自动推断不合预期时可在此显式指定（写入 input_contract）
              </p>
            </template>
          </el-collapse-item>
          </el-collapse>
      </el-drawer>
    </section>

    <section class="monitor-panel">
      <div class="preview-panel vspider-panel">
        <div class="panel-title">
          <div>
            <h2>Visual Preview</h2>
            <p>Agent 实时视觉画面</p>
          </div>
          <span class="live-indicator">
            <i />
            LIVE
          </span>
        </div>
        <div class="preview-stage">
          <img
            v-if="currentImageBase64"
            :src="currentImageBase64"
            alt="实时画面"
          />
          <div v-else class="preview-placeholder">
            等待首帧画面
          </div>
          <div v-if="isHumanInterventionRequired" class="hitl-overlay">
            <div class="hitl-card">
              <div class="hitl-title">{{ isBotChallengeHitl ? '人机验证 / Cloudflare' : 'HITL REQUIRED' }}</div>
              <div class="hitl-copy">
                <template v-if="isBotChallengeHitl">
                  Agent 已暂停。请在浏览器窗口完成 Cloudflare / Turnstile 验证；
                  通过后点击 Resume。首次验证成功后会自动缓存 cf_clearance 到 Auth Profile。
                </template>
                <template v-else>
                  Agent is paused. Complete captcha, slider, QR scan, or 2FA in the browser window.
                </template>
              </div>
              <div v-if="humanInterventionReason" class="hitl-reason">
                {{ humanInterventionReason }}
              </div>
              <el-button
                type="success"
                size="large"
                class="resume-button"
                @click="resumeAgentExecution"
              >
                Resume Agent
              </el-button>
            </div>
          </div>
        </div>
      </div>

      <div class="terminal-panel vspider-panel">
        <el-tabs
          v-model="activeBottomTab"
          class="bottom-tabs"
          @tab-change="(name) => {
            if (name === 'artifacts') hasNewArtifacts = false
            if (name === 'runs') hasNewRuns = false
            if (name === 'final') hasNewFinalAnswer = false
            if (name === 'capability') hasNewCapability = false
            if (name === 'timeline') {
              hasNewPhase = false
              // P: when re-entering the Timeline tab, snap to bottom if
              // auto-scroll is still on so the user lands on the freshest
              // events instead of stale earlier-step rows.
              if (timelineAutoScroll) scrollTimelineToBottom()
            }
            // K3: clear the failures badge dot when the user actually
            // opens the panel. We don't auto-refresh here — the user can
            // hit Refresh manually; the WS done-handler already refetches
            // on the moment a new failure lands.
            if (name === 'failed') hasNewFailures = false
          }"
        >
          <el-tab-pane name="terminal">
            <template #label>
              <span>实时日志</span>
            </template>
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
          </el-tab-pane>

          <!-- M: Phase timeline panel — chip per phase event, grouped by step -->
          <el-tab-pane name="timeline">
            <template #label>
              <el-badge :is-dot="hasNewPhase" class="artifact-badge">
                <span>时间线</span>
              </el-badge>
            </template>
            <div class="timeline-panel">
              <div class="timeline-toolbar">
                <span class="timeline-summary">
                  <strong>{{ phaseSummary.total }}</strong>
                  <span v-if="phaseFilterActive" class="timeline-filter-frac">
                    / {{ phaseEvents.length }}
                  </span>
                  events
                  <span v-if="phaseSummary.warn" class="timeline-warn-pill">
                    {{ phaseSummary.warn }} warn
                  </span>
                  <span v-if="phaseSummary.error" class="timeline-err-pill">
                    {{ phaseSummary.error }} err
                  </span>
                </span>
                <div class="timeline-toolbar-spacer" />
                <el-button
                  v-if="phaseFilterActive"
                  size="small"
                  plain
                  class="timeline-clear-btn"
                  @click="resetPhaseFilters"
                >
                  重置筛选
                </el-button>
                <el-button
                  v-if="phaseEvents.length"
                  size="small"
                  plain
                  class="timeline-clear-btn"
                  :class="{ 'is-active': phaseStatsExpanded }"
                  :title="phaseStatsExpanded
                    ? '收起 phase 统计面板'
                    : '展开 phase 统计面板（耗时分布 / 严重度细分）'"
                  @click="phaseStatsExpanded = !phaseStatsExpanded"
                >
                  统计 {{ phaseStatsExpanded ? '▴' : '▾' }}
                </el-button>
                <el-button
                  v-if="phaseEvents.length"
                  size="small"
                  plain
                  class="timeline-clear-btn"
                  :class="{ 'is-active': timelineFiltersExpanded }"
                  :title="timelineFiltersExpanded ? '收起筛选条件' : '展开 severity / phase 筛选'"
                  @click="timelineFiltersExpanded = !timelineFiltersExpanded"
                >
                  筛选 {{ timelineFiltersExpanded ? '▴' : '▾' }}
                </el-button>
                <el-dropdown
                  trigger="click"
                  @command="handleTimelineMoreAction"
                >
                  <el-button size="small" plain class="timeline-clear-btn">
                    更多 ⋯
                  </el-button>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <el-dropdown-item command="exportJsonl" :disabled="!phaseEvents.length">
                        导出 JSONL
                      </el-dropdown-item>
                      <el-dropdown-item command="importReplay">
                        导入回放
                      </el-dropdown-item>
                      <el-dropdown-item command="clear" divided :disabled="!phaseEvents.length">
                        清空
                      </el-dropdown-item>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
                <input
                  ref="replayInputRef"
                  type="file"
                  accept=".jsonl,.json,application/x-ndjson,text/plain"
                  class="replay-file-input"
                  @change="handleReplayFileChange"
                />
              </div>
              <!-- O: phase + severity filter chips. Click to toggle in/out. -->
              <div
                v-if="phaseEvents.length && (timelineFiltersExpanded || phaseFilterActive)"
                class="timeline-filter-row"
              >
                <div
                  v-if="severityFilterOptions.length > 1"
                  class="timeline-filter-group"
                >
                  <span class="timeline-filter-label">severity</span>
                  <button
                    v-for="opt in severityFilterOptions"
                    :key="'sev-' + opt.severity"
                    type="button"
                    class="timeline-filter-pill"
                    :class="['sev-' + opt.severity, {
                      'is-off': severityFilterExclude.has(opt.severity),
                    }]"
                    @click="toggleSeverityFilter(opt.severity)"
                  >
                    {{ opt.severity }}
                    <span class="timeline-filter-pill-count">{{ opt.count }}</span>
                  </button>
                </div>
                <div class="timeline-filter-group">
                  <span class="timeline-filter-label">phase</span>
                  <button
                    v-for="opt in phaseFilterOptions"
                    :key="'phase-' + opt.phase"
                    type="button"
                    class="timeline-filter-pill"
                    :class="{ 'is-off': phaseFilterExclude.has(opt.phase) }"
                    @click="togglePhaseFilter(opt.phase)"
                  >
                    {{ opt.phase }}
                    <span class="timeline-filter-pill-count">{{ opt.count }}</span>
                  </button>
                </div>
              </div>

              <!-- V: Phase histogram / stats panel ─────────────────
                   Toggled by the toolbar "统计 ▾" button. Reuses the
                   same exclude-set semantics as the chip row (clicking
                   a phase row toggles its filter), so this is "the
                   filter chips with extra detail" rather than a
                   separate UI surface. -->
              <div
                v-if="phaseStatsExpanded && phaseEvents.length"
                class="timeline-stats-panel"
              >
                <header class="timeline-stats-header">
                  <span class="timeline-stats-title">Phase 耗时分布</span>
                  <span class="timeline-stats-sort-label">排序</span>
                  <button
                    v-for="key in ['count', 'mean', 'p95', 'max']"
                    :key="`sort-${key}`"
                    type="button"
                    class="timeline-stats-sort-btn"
                    :class="{ 'is-active': phaseStatsSortBy === key }"
                    @click="phaseStatsSortBy = key"
                  >
                    {{ key }}
                  </button>
                </header>
                <div class="timeline-stats-grid">
                  <div class="timeline-stats-grid-head">
                    <span>phase</span>
                    <span>分布</span>
                    <span class="num">count</span>
                    <span class="num">mean</span>
                    <span class="num">p50</span>
                    <span class="num">p95</span>
                    <span class="num">max</span>
                    <span>severity</span>
                  </div>
                  <button
                    v-for="row in phaseStatsSorted"
                    :key="`stat-${row.phase}`"
                    type="button"
                    class="timeline-stats-row"
                    :class="{ 'is-off': phaseFilterExclude.has(row.phase) }"
                    :title="phaseFilterExclude.has(row.phase)
                      ? `点击重新显示 ${row.phase}`
                      : `点击隐藏 ${row.phase}`"
                    @click="togglePhaseFilter(row.phase)"
                  >
                    <span class="stat-phase">{{ row.phase }}</span>
                    <svg
                      class="stat-spark"
                      viewBox="0 0 100 24"
                      preserveAspectRatio="none"
                      aria-hidden="true"
                    >
                      <path
                        v-if="row.durations && row.durations.length"
                        :d="phaseSparklinePath(row.durations)"
                        fill="currentColor"
                      />
                      <text
                        v-else
                        x="50"
                        y="16"
                        text-anchor="middle"
                        class="stat-spark-empty"
                      >no duration</text>
                    </svg>
                    <span class="num">{{ row.count }}</span>
                    <span class="num">{{ formatPhaseStatMs(row.mean) }}</span>
                    <span class="num">{{ formatPhaseStatMs(row.p50) }}</span>
                    <span class="num">{{ formatPhaseStatMs(row.p95) }}</span>
                    <span class="num">{{ formatPhaseStatMs(row.max) }}</span>
                    <span class="stat-sev">
                      <span
                        v-if="row.sevCounts.error"
                        class="stat-sev-pill sev-error"
                        :title="`${row.sevCounts.error} 条 error`"
                      >{{ row.sevCounts.error }}E</span>
                      <span
                        v-if="row.sevCounts.warn"
                        class="stat-sev-pill sev-warn"
                        :title="`${row.sevCounts.warn} 条 warn`"
                      >{{ row.sevCounts.warn }}W</span>
                      <span
                        v-if="row.sevCounts.info"
                        class="stat-sev-pill sev-info"
                        :title="`${row.sevCounts.info} 条 info`"
                      >{{ row.sevCounts.info }}I</span>
                    </span>
                  </button>
                </div>
                <p class="timeline-stats-footnote">
                  分布柱状图按时间顺序展示该 phase 每次执行的耗时；高度归一到本 phase 的最大值。点击行可切换该 phase 在 Timeline 的显示。
                </p>
              </div>

              <!-- W: Replay-mode banner ───────────────────────────────
                   Indicates that the Timeline is showing imported
                   events instead of live WS data, and offers a button
                   to bail out. We don't auto-hide this — staying in
                   replay should be a sticky decision the user owns. -->
              <div v-if="replayMode" class="timeline-replay-banner">
                <span class="replay-icon" aria-hidden="true">▶</span>
                <span class="replay-text">
                  回放模式
                  <span v-if="replaySourceName" class="replay-source">
                    · {{ replaySourceName }}
                  </span>
                </span>
                <el-button
                  size="small"
                  plain
                  class="replay-exit-btn"
                  title="退出回放，清空 Timeline 并重新接收实时事件"
                  @click="exitReplayMode"
                >退出回放</el-button>
              </div>

              <div v-if="completionEvidence" class="completion-evidence-panel">
                <div class="completion-evidence-panel__head">
                  <strong>完成判定</strong>
                  <el-tag
                    size="small"
                    :type="completionEvidence.status === 'complete' ? 'success' : 'warning'"
                  >
                    {{ completionEvidence.guard }}
                  </el-tag>
                </div>
                <p v-if="completionEvidence.message" class="completion-evidence-panel__msg">
                  {{ completionEvidence.message }}
                </p>
                <div v-if="completionEvidence.evidence.length" class="completion-evidence-panel__row">
                  <span class="completion-evidence-panel__label">证据</span>
                  <span>{{ completionEvidence.evidence.join(' · ') }}</span>
                </div>
                <div v-if="completionEvidence.reasons.length" class="completion-evidence-panel__row">
                  <span class="completion-evidence-panel__label">原因</span>
                  <span>{{ completionEvidence.reasons.join(' · ') }}</span>
                </div>
                <div
                  v-if="completionEvidence.streak != null"
                  class="completion-evidence-panel__row"
                >
                  <span class="completion-evidence-panel__label">无进展 streak</span>
                  <span>{{ completionEvidence.streak }}</span>
                </div>
              </div>

              <el-scrollbar
                ref="timelineRef"
                class="timeline-scroll"
                @scroll="onTimelineScroll"
              >
                <p v-if="!phaseEvents.length" class="empty-log">
                  暂无 phase 事件 — 启动任务后会在这里实时显示 VLM /
                  action / SoM / guard / finalize 的时间线
                </p>
                <p
                  v-else-if="!phaseTimelineGroups.length"
                  class="empty-log"
                >
                  当前筛选下没有匹配事件 ·
                  <a
                    href="#"
                    class="tab-jump"
                    @click.prevent="resetPhaseFilters"
                  >重置筛选</a>
                </p>
                <div
                  v-for="(group, gi) in phaseTimelineGroups"
                  :key="gi"
                  class="timeline-row"
                >
                  <div class="timeline-step-tag">
                    {{ Number.isFinite(group.step) ? 'step ' + group.step : '—' }}
                  </div>
                  <div class="timeline-chips">
                    <button
                      v-for="(evt, ei) in group.events"
                      :key="ei"
                      type="button"
                      class="timeline-chip"
                      :style="{
                        background: phaseChipStyle(evt).background,
                        color: phaseChipStyle(evt).color,
                        borderColor: phaseChipStyle(evt).border,
                      }"
                      :title="(phaseChipDetail(evt) || '') + ' (单击查看 / 双击复制 JSON)'"
                      @click="onChipClick(evt)"
                      @dblclick="onChipDblClick(evt)"
                    >
                      <span class="timeline-chip-label">{{ phaseChipLabel(evt) }}</span>
                      <span
                        v-if="phaseChipDetail(evt)"
                        class="timeline-chip-detail"
                      >
                        {{ phaseChipDetail(evt) }}
                      </span>
                    </button>
                  </div>
                </div>
              </el-scrollbar>
              <!-- P: floating "回到底部" button — visible only when the
                   user has scrolled away from the tail and there's still
                   content to follow. Click resumes auto-scroll. -->
              <button
                v-if="!timelineAutoScroll && phaseTimelineGroups.length"
                type="button"
                class="timeline-jump-bottom"
                @click="scrollTimelineToBottom"
              >
                ↓ 回到底部
              </button>
            </div>
          </el-tab-pane>

          <el-tab-pane name="capability">
            <template #label>
              <el-badge :is-dot="hasNewCapability" class="artifact-badge">
                <span>能力追踪</span>
              </el-badge>
            </template>
            <el-scrollbar class="capability-scroll">
              <div v-if="latestCapabilityRoute || latestCapabilityExecute" class="capability-panel">
                <section class="capability-hero">
                  <div>
                    <div class="capability-kicker">Route-Aware Agent Guidance</div>
                    <h3>{{ capabilityIntent.task_type || 'unknown task' }}</h3>
                    <p>
                      输出模式：{{ capabilityIntent.output_mode || 'default' }}
                      <span v-if="capabilityIntent.requires_artifact"> · 需要产物</span>
                      <span v-if="capabilityIntent.requires_visual_grounding"> · 需要视觉定位</span>
                    </p>
                  </div>
                  <div class="capability-hero-actions">
                    <span class="capability-count">
                      {{ capabilityTraceEvents.length }} events
                    </span>
                    <el-button
                      size="small"
                      plain
                      class="capability-export-btn"
                      :disabled="!capabilityTraceEvents.length"
                      title="导出 capability_route / capability_execute 为 JSONL (Ctrl+E)"
                      @click="exportCapabilityTraceAsJsonl"
                    >
                      导出 JSONL
                    </el-button>
                    <el-dropdown
                      trigger="click"
                      @command="handleCapabilityMoreAction"
                    >
                      <el-button size="small" plain class="capability-export-btn">
                        更多操作 ⋯
                      </el-button>
                      <template #dropdown>
                        <el-dropdown-menu>
                          <el-dropdown-item command="copySummary" :disabled="!capabilityTraceEvents.length">
                            复制摘要
                          </el-dropdown-item>
                          <el-dropdown-item command="importReplay">
                            导入回放
                          </el-dropdown-item>
                          <el-dropdown-item
                            command="generateFixture"
                            divided
                            :disabled="capabilityExecutionFailureBundle.version !== 'capability_execute_failure_bundle.v1'"
                          >
                            生成 Fixture
                          </el-dropdown-item>
                          <el-dropdown-item
                            command="replayFixture"
                            :disabled="capabilityExecutionFailureBundle.version !== 'capability_execute_failure_bundle.v1'"
                          >
                            验证 Fixture
                          </el-dropdown-item>
                          <el-dropdown-item command="refreshFixtures">
                            刷新 Fixture 库
                          </el-dropdown-item>
                          <el-dropdown-item command="refreshBatchHistory">
                            刷新 Replay 历史
                          </el-dropdown-item>
                          <el-dropdown-item command="batchReplay" :disabled="capabilityFailureFixtureBatchReplayLoading">
                            批量验证 Fixture
                          </el-dropdown-item>
                          <el-dropdown-item
                            command="replayEfficiency"
                            divided
                            :disabled="capabilityExecutionEfficiencyCorrelationReport.version !== 'efficiency_correlation_report.v1'"
                          >
                            验证 Efficiency
                          </el-dropdown-item>
                          <el-dropdown-item command="refreshEfficiencyReplays">
                            刷新 Efficiency Replay
                          </el-dropdown-item>
                        </el-dropdown-menu>
                      </template>
                    </el-dropdown>
                  </div>
                </section>

                <div v-if="replayMode" class="timeline-replay-banner capability-replay-banner">
                  <span class="replay-icon" aria-hidden="true">▶</span>
                  <span class="replay-text">
                    Capability 回放模式
                    <span v-if="replaySourceName" class="replay-source">
                      · {{ replaySourceName }}
                    </span>
                  </span>
                  <el-button
                    size="small"
                    plain
                    class="replay-exit-btn"
                    title="退出回放，清空导入事件并重新接收实时事件"
                    @click="exitReplayMode"
                  >退出回放</el-button>
                </div>

                <el-tabs v-model="capabilitySubTab" class="capability-sub-tabs">
                  <el-tab-pane label="概览" name="overview">

                <section v-if="capabilityTraceRows.length" class="capability-health-strip">
                  <CapabilityStatusBadge
                    :status-class="capabilityTraceHealth.status"
                    :label="capabilityTraceHealth.label"
                  />
                  <span>route {{ capabilityTraceHealth.route }}</span>
                  <span>execute {{ capabilityTraceHealth.execute }}</span>
                  <span>issues {{ capabilityTraceHealth.issues }}</span>
                  <span v-if="capabilityTraceHealth.alignment">
                    {{ capabilityTraceHealth.alignment }}
                  </span>
                </section>

                <CapabilityRuntimePanel
                  :runtime-preflight="capabilityRuntimePreflight"
                  :runtime-preflight-class="capabilityRuntimePreflightClass"
                  :runtime-preflight-label="capabilityRuntimePreflightLabel"
                  :browser-runtime="browserRuntime"
                  :browser-runtime-class="browserRuntimeStatusClass"
                  :browser-runtime-label="browserRuntimeLabel"
                  :backend-summary="browserRuntimeBackendSummary"
                  :capacity="browserRuntimeCapacity"
                  :health-label="browserRuntimeHealthLabel"
                  :health-cache-label="browserRuntimeHealthCacheLabel"
                />

                <CapabilityTraceList
                  v-model:filter="capabilityTraceFilter"
                  v-model:search-query="capabilityTraceSearchQuery"
                  :rows="capabilityFilteredTraceRows"
                  :total-rows="capabilityTraceRows.length"
                  :summary="capabilityTraceSummary"
                  @open-row="openPhaseDialog"
                />

                <CapabilityAlignmentCard
                  :visible="Boolean(latestCapabilityExecute)"
                  :alignment="capabilityExecutionAlignment"
                />

                <section v-if="latestCapabilityExecute" class="capability-section">
                  <h4>执行遥测</h4>
                  <div class="capability-exec-summary">
                    <span
                      class="capability-exec-status"
                      :class="latestCapabilityExecute.completed ? 'is-complete' : 'is-fallback'"
                    >
                      {{ latestCapabilityExecute.execution_status || 'unknown' }}
                    </span>
                    <span v-if="latestCapabilityExecute.capability">
                      capability: {{ latestCapabilityExecute.capability }}
                    </span>
                    <span v-if="Number.isFinite(latestCapabilityExecute.duration_ms)">
                      {{ latestCapabilityExecute.duration_ms }}ms
                    </span>
                    <span v-if="latestCapabilityExecute.fallback_reason">
                      fallback: {{ latestCapabilityExecute.fallback_reason }}
                    </span>
                  </div>
                  <CapabilityEfficiencyPanel
                    :crawl-plan="capabilityActiveCrawlEfficiencyPlan"
                    :available-paths="capabilityExecutionCrawlEfficiencyAvailablePaths"
                    :candidates="capabilityExecutionCrawlEfficiencyCandidates"
                    :summary="capabilityExecutionCrawlEfficiencySummary"
                    :candidate-class="capabilityCrawlEfficiencyCandidateClass"
                    :candidate-evidence="capabilityCrawlEfficiencyEvidence"
                    :correlation-report="capabilityExecutionEfficiencyCorrelationReport"
                    :correlation-status-class="capabilityEfficiencyCorrelationStatusClass"
                    :correlation-alignment="capabilityExecutionEfficiencyCorrelationAlignment"
                    :root-causes="capabilityExecutionEfficiencyCorrelationRootCauses"
                    :planner-hints="capabilityExecutionEfficiencyCorrelationPlannerHints"
                    :actions="capabilityExecutionEfficiencyCorrelationActions"
                  />
                  <div v-if="capabilityExecutionRuntimeAfter.runtime_status" class="capability-exec-summary">
                    <span>{{ capabilityExecutionRuntimeLabel }}</span>
                    <span>runtime: {{ capabilityExecutionRuntimeAfter.runtime_status || 'unknown' }}</span>
                    <span>backend: {{ capabilityExecutionRuntimeAfter.active_backend || 'unknown' }}</span>
                    <span>health: {{ capabilityExecutionRuntimeAfter.backend_health || 'unknown' }}</span>
                    <span>
                      contexts:
                      {{ capabilityExecutionRuntimeAfter.available_contexts ?? '?' }}
                      /
                      {{ capabilityExecutionRuntimeAfter.max_contexts ?? '?' }}
                    </span>
                    <span v-if="capabilityExecutionRuntimeAfter.recommended_action">
                      action: {{ capabilityExecutionRuntimeAfter.recommended_action }}
                    </span>
                  </div>
                  <div v-if="capabilityExecutionRuntimeDrift.version" class="capability-exec-summary">
                    <span>{{ capabilityExecutionDriftLabel }}</span>
                    <span>changes: {{ Array.isArray(capabilityExecutionRuntimeDrift.changes) ? capabilityExecutionRuntimeDrift.changes.length : 0 }}</span>
                    <span>warnings: {{ Array.isArray(capabilityExecutionRuntimeDrift.warnings) ? capabilityExecutionRuntimeDrift.warnings.length : 0 }}</span>
                    <span v-if="capabilityExecutionRuntimeDrift.deltas">
                      contexts Δ:
                      {{ capabilityExecutionRuntimeDrift.deltas.available_contexts ?? 0 }}
                    </span>
                    <span v-if="capabilityExecutionRuntimeDrift.recommended_action">
                      action: {{ capabilityExecutionRuntimeDrift.recommended_action }}
                    </span>
                  </div>
                  <div v-if="capabilityExecutionRuntimeIssueSummary.version" class="capability-exec-summary">
                    <span>{{ capabilityExecutionIssueLabel }}</span>
                    <span>issues: {{ capabilityExecutionRuntimeIssueSummary.issue_count ?? 0 }}</span>
                    <span v-if="capabilityExecutionRuntimeIssueSummary.sources">
                      drift: {{ capabilityExecutionRuntimeIssueSummary.sources.drift_status || 'unknown' }}
                    </span>
                    <span v-if="capabilityExecutionRuntimeIssueSummary.recommended_action">
                      action: {{ capabilityExecutionRuntimeIssueSummary.recommended_action }}
                    </span>
                  </div>
                  <div v-if="capabilityExecutionRuntimeIssues.length" class="capability-check-list">
                    <span
                      v-for="(issue, idx) in capabilityExecutionRuntimeIssues"
                      :key="`runtime-issue-${idx}-${issue.code || idx}`"
                      class="capability-check is-error"
                    >
                      {{ issue.source || 'runtime' }}: {{ issue.code || 'issue' }}
                    </span>
                  </div>
                  <div v-if="capabilityExecutionRuntimeActions.length" class="capability-check-list">
                    <span
                      v-for="action in capabilityExecutionRuntimeActions"
                      :key="`runtime-action-${action}`"
                      class="capability-check is-complete"
                    >
                      action: {{ action }}
                    </span>
                  </div>
                  <div v-if="capabilityExecutionActionIssueSummary.version" class="capability-exec-summary">
                    <span>{{ capabilityExecutionActionIssueLabel }}</span>
                    <span v-if="capabilityExecutionActionTrace.action">
                      browser action: {{ capabilityExecutionActionTrace.action }}
                    </span>
                    <span v-if="capabilityExecutionActionFailureSummary.failure_code">
                      failure: {{ capabilityExecutionActionFailureSummary.failure_code }}
                    </span>
                    <span v-if="capabilityExecutionActionFailureSummary.failure_category">
                      category: {{ capabilityExecutionActionFailureSummary.failure_category }}
                    </span>
                    <span>issues: {{ capabilityExecutionActionIssueSummary.issue_count ?? 0 }}</span>
                    <span v-if="capabilityExecutionActionIssueSummary.recommended_action">
                      action: {{ capabilityExecutionActionIssueSummary.recommended_action }}
                    </span>
                  </div>
                  <div v-if="capabilityExecutionActionIssues.length" class="capability-check-list">
                    <span
                      v-for="(issue, idx) in capabilityExecutionActionIssues"
                      :key="`browser-action-issue-${idx}-${issue.code || idx}`"
                      class="capability-check is-error"
                    >
                      {{ issue.source || 'action' }}: {{ issue.code || 'issue' }}
                    </span>
                  </div>
                  <div v-if="capabilityExecutionActionIssueActions.length" class="capability-check-list">
                    <span
                      v-for="action in capabilityExecutionActionIssueActions"
                      :key="`browser-action-recommendation-${action}`"
                      class="capability-check is-complete"
                    >
                      action: {{ action }}
                    </span>
                  </div>
                  <div v-if="capabilityExecutionActionRecoveryActions.length" class="capability-check-list">
                    <span
                      v-for="action in capabilityExecutionActionRecoveryActions"
                      :key="`browser-action-recovery-${action}`"
                      class="capability-check is-warning"
                    >
                      recovery: {{ action }}
                    </span>
                  </div>
                  <div v-if="capabilityExecutionAttempts.length" class="capability-attempt-list">
                    <div
                      v-for="(attempt, idx) in capabilityExecutionAttempts"
                      :key="`attempt-${idx}-${attempt.capability || idx}`"
                      class="capability-attempt"
                      :class="capabilityAttemptClass(attempt)"
                    >
                      <strong>{{ attempt.capability || 'unknown' }}</strong>
                      <span>{{ attempt.status || 'attempted' }}</span>
                      <small v-if="attempt.target_count != null">
                        target {{ attempt.target_count }}
                      </small>
                      <small v-if="attempt.count != null">count {{ attempt.count }}</small>
                      <small v-if="attempt.row_count != null">rows {{ attempt.row_count }}</small>
                      <small v-if="attempt.item_count != null">items {{ attempt.item_count }}</small>
                      <p v-if="attempt.reason">{{ attempt.reason }}</p>
                    </div>
                  </div>
                  <div v-if="capabilityExecutionChecks.length" class="capability-check-list">
                    <span
                      v-for="check in capabilityExecutionChecks"
                      :key="check.name"
                      class="capability-check"
                      :class="check.passed ? 'is-complete' : 'is-error'"
                    >
                      {{ check.name }}: {{ check.passed ? 'pass' : 'fail' }}
                    </span>
                  </div>
                </section>

                  </el-tab-pane>
                  <el-tab-pane label="计划 / 工作流" name="plan">

                <section v-if="capabilityExecutionPlanSteps.length" class="capability-section">
                  <h4>结构化执行计划</h4>
                  <div class="capability-chain">
                    <div
                      v-for="step in capabilityExecutionPlanSteps"
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

                <section v-if="capabilityWorkflowNodes.length" class="capability-section">
                  <h4>跨系统工作流图</h4>
                  <div class="capability-exec-summary">
                    <span>{{ capabilityWorkflowGraph.version || 'workflow_graph' }}</span>
                    <span>systems {{ capabilityWorkflowGraph.systems?.length || 0 }}</span>
                    <span>sessions {{ capabilityWorkflowGraph.sessions?.length || 0 }}</span>
                    <span>nodes {{ capabilityWorkflowNodes.length }}</span>
                    <span>edges {{ capabilityWorkflowGraph.data_edges?.length || 0 }}</span>
                  </div>
                  <div class="capability-chain">
                    <div
                      v-for="node in capabilityWorkflowNodes.slice(0, 8)"
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

                <section v-if="capabilityActionRefSchema.version" class="capability-section">
                  <h4>Unified ActionRef</h4>
                  <div class="capability-exec-summary">
                    <span>{{ capabilityActionRefSchema.version }}</span>
                    <span
                      v-for="source in (capabilityActionRefSchema.preferred_sources || [])"
                      :key="`action-ref-source-${source}`"
                    >
                      {{ source }}
                    </span>
                  </div>
                  <p
                    v-if="capabilityActionRefSchema.notes?.length"
                    class="capability-detail"
                  >
                    {{ capabilityActionRefSchema.notes.join(' · ') }}
                  </p>
                </section>

                <section class="capability-section">
                  <h4>推荐能力链</h4>
                  <div class="capability-chain">
                    <div
                      v-for="(item, idx) in capabilityBackendPlan"
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

                  </el-tab-pane>
                  <el-tab-pane label="回放与 Fixture" name="replay">

                <section v-if="capabilityEfficiencyFeedbackReplayReport" class="capability-efficiency-feedback-replay-card">
                  <div class="capability-section-head">
                    <h4>Efficiency Feedback Replay</h4>
                    <CapabilityStatusBadge
                      :status-class="capabilityEfficiencyFeedbackReplayReport.passed ? 'ok' : 'error'"
                      :label="capabilityEfficiencyFeedbackReplayStatus"
                    />
                  </div>
                  <div class="capability-fixture-replay-grid">
                    <span>failure</span>
                    <strong>{{ capabilityEfficiencyFeedbackReplayPlannerFeedback.primary_failure || 'unknown' }}</strong>
                    <span>action</span>
                    <strong>{{ capabilityEfficiencyFeedbackReplayPlannerFeedback.recommended_action || 'review' }}</strong>
                    <span>route</span>
                    <strong>{{ capabilityEfficiencyFeedbackReplayReport.route?.passed ? 'passed' : 'failed' }}</strong>
                    <span>plan</span>
                    <strong>{{ capabilityEfficiencyFeedbackReplayReport.execution_plan?.passed ? 'passed' : 'failed' }}</strong>
                    <span>step</span>
                    <strong>{{ capabilityEfficiencyFeedbackReplayReport.execution_plan?.feedback_step?.capability || 'none' }}</strong>
                  </div>
                  <div v-if="capabilityEfficiencyFeedbackReplayPreferredCapabilities.length" class="capability-check-list">
                    <span
                      v-for="capability in capabilityEfficiencyFeedbackReplayPreferredCapabilities"
                      :key="`efficiency-feedback-prefer-${capability}`"
                      class="capability-check is-complete"
                    >
                      prefer: {{ capability }}
                    </span>
                  </div>
                  <div v-if="capabilityEfficiencyFeedbackReplayAvoidActions.length" class="capability-check-list">
                    <span
                      v-for="action in capabilityEfficiencyFeedbackReplayAvoidActions.slice(0, 6)"
                      :key="`efficiency-feedback-avoid-${action}`"
                      class="capability-check is-warning"
                    >
                      avoid: {{ action }}
                    </span>
                  </div>
                  <div v-if="capabilityEfficiencyFeedbackReplayArtifact?.url" class="capability-fixture-replay-artifact">
                    artifact:
                    <a :href="capabilityEfficiencyFeedbackReplayArtifact.url" target="_blank" rel="noreferrer">
                      {{ capabilityEfficiencyFeedbackReplayArtifact.url }}
                    </a>
                  </div>
                  <div class="capability-fixture-replay-checks">
                    <span>
                      checks {{ capabilityEfficiencyFeedbackReplayChecks.length - capabilityEfficiencyFeedbackReplayFailedChecks.length }}/{{ capabilityEfficiencyFeedbackReplayChecks.length }}
                    </span>
                    <span v-if="capabilityEfficiencyFeedbackReplayFailedChecks.length">
                      failed {{ capabilityEfficiencyFeedbackReplayFailedChecks.length }}
                    </span>
                  </div>
                </section>

                <section class="capability-efficiency-feedback-library-card">
                  <div class="capability-section-head">
                    <h4>Efficiency Feedback Replay Library</h4>
                    <CapabilityStatusBadge
                      status-class="route-only"
                      :label="`${capabilityEfficiencyFeedbackReplayLibraryCount} reports`"
                    />
                  </div>
                  <div class="capability-fixture-library-actions">
                    <span>source capability/efficiency_feedback_replays</span>
                    <span v-if="capabilityEfficiencyFeedbackReplayLibraryLoading">loading</span>
                  </div>
                  <div v-if="capabilityEfficiencyFeedbackReplayLibrary.length" class="capability-efficiency-feedback-library-list">
                    <div
                      v-for="item in capabilityEfficiencyFeedbackReplayLibrary.slice(0, 5)"
                      :key="item.path || item.name"
                      class="capability-efficiency-feedback-library-item"
                    >
                      <strong>{{ item.primary_failure || item.name || 'efficiency feedback replay' }}</strong>
                      <span>{{ item.passed ? 'passed' : 'failed' }} · {{ item.recommended_action || 'review' }}</span>
                      <span v-if="Array.isArray(item.preferred_capabilities) && item.preferred_capabilities.length">
                        prefer {{ item.preferred_capabilities.slice(0, 3).join(', ') }}
                      </span>
                      <span v-if="item.feedback_step">step {{ item.feedback_step }}</span>
                      <a v-if="item.url" :href="item.url" target="_blank" rel="noreferrer">artifact</a>
                    </div>
                  </div>
                  <p v-else class="capability-fixture-library-empty">
                    暂无 efficiency feedback replay artifacts
                  </p>
                </section>

                <section v-if="capabilityFailureFixtureReplayReport" class="capability-fixture-replay-card">
                  <div class="capability-section-head">
                    <h4>Failure Fixture Replay</h4>
                    <CapabilityStatusBadge
                      :status-class="capabilityFailureFixtureReplayReport.passed ? 'ok' : 'error'"
                      :label="capabilityFailureFixtureReplayStatus"
                    />
                  </div>
                  <div class="capability-fixture-replay-grid">
                    <span>failure</span>
                    <strong>{{ capabilityFailureFixtureReplayReport.fixture?.primary_failure || 'unknown' }}</strong>
                    <span>planner</span>
                    <strong>{{ capabilityFailureFixtureReplayReport.planner_feedback?.passed ? 'passed' : 'failed' }}</strong>
                    <span>route</span>
                    <strong>{{ capabilityFailureFixtureReplayReport.route?.passed ? 'passed' : 'failed' }}</strong>
                    <span>plan</span>
                    <strong>{{ capabilityFailureFixtureReplayReport.execution_plan?.passed ? 'passed' : 'failed' }}</strong>
                  </div>
                  <div v-if="capabilityFailureFixtureReplayArtifact?.url" class="capability-fixture-replay-artifact">
                    artifact:
                    <a :href="capabilityFailureFixtureReplayArtifact.url" target="_blank" rel="noreferrer">
                      {{ capabilityFailureFixtureReplayArtifact.url }}
                    </a>
                  </div>
                  <div class="capability-fixture-replay-checks">
                    <span>
                      checks {{ capabilityFailureFixtureReplayChecks.length - capabilityFailureFixtureReplayFailedChecks.length }}/{{ capabilityFailureFixtureReplayChecks.length }}
                    </span>
                    <span v-if="capabilityFailureFixtureReplayFailedChecks.length">
                      failed {{ capabilityFailureFixtureReplayFailedChecks.length }}
                    </span>
                  </div>
                  <ul class="capability-fixture-replay-check-list">
                    <li
                      v-for="check in capabilityFailureFixtureReplayChecks.slice(0, 6)"
                      :key="check.name"
                      :class="check.passed ? 'is-ok' : 'is-error'"
                    >
                      <span>{{ check.passed ? '✓' : '×' }}</span>
                      <code>{{ check.name }}</code>
                    </li>
                  </ul>
                </section>

                <section class="capability-fixture-library-card">
                  <div class="capability-section-head">
                    <h4>Failure Fixture Library</h4>
                    <CapabilityStatusBadge
                      status-class="route-only"
                      :label="`${capabilityFailureFixtureLibraryCount} fixtures`"
                    />
                  </div>
                  <div class="capability-fixture-library-actions">
                    <span>source capability/failure_fixtures</span>
                    <span v-if="capabilityFailureFixtureLibraryLoading">loading</span>
                  </div>
                  <div v-if="capabilityFailureFixtureLibrary.length" class="capability-fixture-library-list">
                    <div
                      v-for="item in capabilityFailureFixtureLibrary.slice(0, 5)"
                      :key="item.path || item.name"
                      class="capability-fixture-library-item"
                    >
                      <strong>{{ item.name || 'fixture' }}</strong>
                      <span>{{ item.primary_failure || 'unknown' }}</span>
                      <span>{{ item.action || 'action' }}</span>
                      <a v-if="item.url" :href="item.url" target="_blank" rel="noreferrer">artifact</a>
                    </div>
                  </div>
                  <div v-else class="capability-fixture-library-empty">
                    暂无已加载 fixture，点击“刷新 Fixture 库”读取 artifact library。
                  </div>
                </section>

                <section class="capability-fixture-history-card">
                  <div class="capability-section-head">
                    <h4>Failure Fixture Batch History</h4>
                    <CapabilityStatusBadge
                      status-class="route-only"
                      :label="`${capabilityFailureFixtureBatchHistoryCount} reports`"
                    />
                  </div>
                  <div class="capability-fixture-library-actions">
                    <span>source capability/failure_fixture_replay_batches</span>
                    <span v-if="capabilityFailureFixtureBatchHistoryLoading">loading</span>
                    <span v-if="capabilityFailureFixtureLatestBatchHistory">
                      latest {{ capabilityFailureFixtureLatestBatchHistory.status || 'unknown' }}
                      · failed {{ capabilityFailureFixtureLatestBatchHistory.failed_count || 0 }}
                    </span>
                  </div>
                  <div
                    v-if="capabilityFailureFixtureBatchHistoryTrend"
                    class="capability-fixture-history-trend"
                    :class="`is-${capabilityFailureFixtureBatchHistoryTrendDirection}`"
                  >
                    <strong>trend {{ capabilityFailureFixtureBatchHistoryTrendDirection }}</strong>
                    <span>
                      failed Δ {{ capabilityFailureFixtureBatchHistoryTrend.failed_count_delta || 0 }}
                      · pass rate Δ {{ capabilityFailureFixtureBatchHistoryTrendPassRateDelta }}%
                    </span>
                    <small v-if="capabilityFailureFixtureBatchHistoryTrend.focus_changed">
                      focus changed {{ capabilityFailureFixtureBatchHistoryTrend.previous_recommended_focus || 'none' }}
                      → {{ capabilityFailureFixtureBatchHistoryTrend.latest_recommended_focus || 'none' }}
                    </small>
                  </div>
                  <div v-if="capabilityFailureFixtureBatchHistory.length" class="capability-fixture-history-list">
                    <div
                      v-for="item in capabilityFailureFixtureBatchHistory.slice(0, 5)"
                      :key="item.path || item.name"
                      class="capability-fixture-history-item"
                      :class="item.passed ? 'is-ok' : 'is-error'"
                    >
                      <strong>{{ item.name || 'batch replay' }}</strong>
                      <span>{{ item.status || (item.passed ? 'passed' : 'failed') }}</span>
                      <span>{{ item.passed_count || 0 }}/{{ item.fixture_count || 0 }} passed · failed {{ item.failed_count || 0 }}</span>
                      <small v-if="item.recommended_focus">{{ item.recommended_focus }}</small>
                      <a v-if="item.url" :href="item.url" target="_blank" rel="noreferrer">artifact</a>
                    </div>
                  </div>
                  <div v-else class="capability-fixture-library-empty">
                    暂无已加载 batch replay history，点击“刷新 Replay 历史”读取 report artifacts。
                  </div>
                </section>

                <section v-if="capabilityFailureFixtureBatchReplayReport" class="capability-fixture-replay-card capability-fixture-batch-card">
                  <div class="capability-section-head">
                    <h4>Failure Fixture Batch Replay</h4>
                    <div class="capability-fixture-batch-head-actions">
                      <el-button
                        size="small"
                        plain
                        class="capability-export-btn"
                        title="复制 failure fixture batch replay Markdown 摘要"
                        @click="copyCapabilityFailureFixtureBatchReplaySummary"
                      >
                        复制 Replay 摘要
                      </el-button>
                      <CapabilityStatusBadge
                        :status-class="capabilityFailureFixtureBatchReplayReport.passed ? 'ok' : 'error'"
                        :label="capabilityFailureFixtureBatchReplayStatus"
                      />
                    </div>
                  </div>
                  <div class="capability-fixture-replay-grid">
                    <span>fixtures</span>
                    <strong>{{ capabilityFailureFixtureBatchReplayReport.fixture_count || 0 }}</strong>
                    <span>passed</span>
                    <strong>{{ capabilityFailureFixtureBatchReplayReport.passed_count || 0 }}</strong>
                    <span>failed</span>
                    <strong>{{ capabilityFailureFixtureBatchReplayReport.failed_count || 0 }}</strong>
                    <span>failed checks</span>
                    <strong>{{ capabilityFailureFixtureBatchReplayFailedChecks.length }}</strong>
                  </div>
                  <div v-if="capabilityFailureFixtureBatchReplaySummary.status" class="capability-fixture-triage">
                    <div v-if="capabilityFailureFixtureBatchReplaySummary.recommended_focus" class="capability-fixture-triage-focus">
                      focus {{ capabilityFailureFixtureBatchReplaySummary.recommended_focus }}
                    </div>
                    <div class="capability-fixture-triage-grid">
                      <div>
                        <span>primary</span>
                        <strong>{{ capabilityFailureFixtureBatchReplayTopPrimaryFailures[0]?.name || 'none' }}</strong>
                        <small>{{ capabilityFailureFixtureBatchReplayTopPrimaryFailures[0]?.count || 0 }}</small>
                      </div>
                      <div>
                        <span>category</span>
                        <strong>{{ capabilityFailureFixtureBatchReplayTopFailureCategories[0]?.name || 'none' }}</strong>
                        <small>{{ capabilityFailureFixtureBatchReplayTopFailureCategories[0]?.count || 0 }}</small>
                      </div>
                      <div>
                        <span>check</span>
                        <strong>{{ capabilityFailureFixtureBatchReplayTopFailedChecks[0]?.name || 'none' }}</strong>
                        <small>{{ capabilityFailureFixtureBatchReplayTopFailedChecks[0]?.failed_count || 0 }}</small>
                      </div>
                    </div>
                  </div>
                  <div v-if="capabilityFailureFixtureBatchReplayArtifact?.url" class="capability-fixture-replay-artifact">
                    artifact:
                    <a :href="capabilityFailureFixtureBatchReplayArtifact.url" target="_blank" rel="noreferrer">
                      {{ capabilityFailureFixtureBatchReplayArtifact.url }}
                    </a>
                  </div>
                  <ul v-if="capabilityFailureFixtureBatchReplayFailedChecks.length" class="capability-fixture-replay-check-list">
                    <li
                      v-for="check in capabilityFailureFixtureBatchReplayFailedChecks.slice(0, 6)"
                      :key="check.name"
                      class="is-error"
                    >
                      <span>×</span>
                      <code>{{ check.name }}</code>
                      <span>{{ check.failed_count }}</span>
                    </li>
                  </ul>
                  <div class="capability-fixture-batch-list">
                    <div
                      v-for="item in capabilityFailureFixtureBatchReplayItems.slice(0, 6)"
                      :key="`${item.index}-${item.name}`"
                      class="capability-fixture-batch-item"
                      :class="item.passed ? 'is-ok' : 'is-error'"
                    >
                      <span>{{ item.passed ? '✓' : '×' }}</span>
                      <strong>{{ item.name || 'fixture' }}</strong>
                      <small>{{ item.primary_failure || 'unknown' }} · failed {{ item.failed_check_count || 0 }}</small>
                    </div>
                  </div>
                </section>

                  </el-tab-pane>
                  <el-tab-pane label="诊断" name="diagnostics">

                <section v-if="capabilityManifestSummary.length" class="capability-section">
                  <h4>能力清单摘要</h4>
                  <div class="capability-fallback">
                    <span
                      v-for="item in capabilityManifestSummary"
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
                      v-for="(item, idx) in capabilityFallbackChain"
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
                      v-for="role in capabilityRoleRows"
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

                <section v-if="capabilityAuditFindings.length" class="capability-section">
                  <h4>审计发现</h4>
                  <div class="capability-audit-list">
                    <div
                      v-for="(finding, idx) in capabilityAuditFindings"
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
                  <pre class="capability-json"><code>{{ capabilityTraceJson || capabilityExecuteJson }}</code></pre>
                </section>

                  </el-tab-pane>
                </el-tabs>
              </div>
              <p v-else class="empty-log">
                暂无 capability_route / capability_execute 事件 — 启动任务后会显示推荐能力链、执行尝试和模型职责边界。
              </p>
            </el-scrollbar>
          </el-tab-pane>

          <el-tab-pane name="final">
            <template #label>
              <el-badge :is-dot="hasNewFinalAnswer" class="artifact-badge">
                <span>最终答案</span>
              </el-badge>
            </template>
            <el-scrollbar class="final-scroll">
              <!-- 状态 A：执行中 / 等待中 -->
              <div
                v-if="finalAnswerStatus === 'pending'"
                class="final-pending"
              >
                <div class="typing-dots" aria-hidden="true">
                  <span /><span /><span />
                </div>
                <p class="final-pending-text">
                  Agent 正在思考并提取结论...
                </p>
              </div>

              <!-- 状态 C：结构化数据导出，提示去 Artifacts 下载 -->
              <div
                v-else-if="finalAnswerStatus === 'file'"
                class="final-fallback"
              >
                <!-- 后端如果带回了文件摘要（_synthesize_file_mode_summary 合成），
                     优先以 Markdown 渲染；否则只显示固定 CTA -->
                <div
                  v-if="finalAnswerText && finalAnswerText.trim()"
                  class="final-md final-file-summary"
                  v-html="finalAnswerHtml"
                />
                <p class="final-fallback-text">
                  本次任务产出为结构化数据，请前往
                  <a
                    href="#"
                    class="tab-jump"
                    @click.prevent="activeBottomTab = 'artifacts'"
                  >Artifacts</a>
                  面板下载
                </p>
              </div>

              <!-- 状态 B：纯文本答案，Markdown 渲染 + 工具栏 + 折叠 -->
              <div v-else-if="finalAnswerStatus === 'text'" class="final-text-wrap">
                <!-- F3: 域卡片 — 后端识别为 weather/stock/recipe/flight 时显示 -->
                <div
                  v-if="finalAnswerDomainMeta"
                  class="final-domain-card"
                  :style="{
                    borderColor: finalAnswerDomainMeta.accent,
                    background: 'linear-gradient(135deg, ' +
                      finalAnswerDomainMeta.accent + '14, transparent 65%)',
                  }"
                >
                  <span class="final-domain-icon" aria-hidden="true">
                    {{ finalAnswerDomainMeta.icon }}
                  </span>
                  <div class="final-domain-meta">
                    <span
                      class="final-domain-label"
                      :style="{ color: finalAnswerDomainMeta.accent }"
                    >
                      {{ finalAnswerDomainMeta.label }}
                    </span>
                    <span class="final-domain-hint">
                      由 Agent 自动识别为该领域，答案已按领域格式精简
                    </span>
                  </div>
                </div>

                <!-- F1: 工具栏（复制 / 导出 / 字数统计） -->
                <div class="final-toolbar">
                  <span class="final-meta">
                    {{ finalAnswerCharCount }} 字 · {{ finalAnswerLineCount }} 行
                  </span>
                  <div class="final-toolbar-spacer" />
                  <el-button
                    size="small"
                    plain
                    class="final-toolbar-btn"
                    :type="
                      finalAnswerCopyState === 'ok' ? 'success'
                      : finalAnswerCopyState === 'err' ? 'danger' : 'default'
                    "
                    @click="copyFinalAnswerToClipboard"
                  >
                    <span v-if="finalAnswerCopyState === 'ok'">已复制 ✓</span>
                    <span v-else-if="finalAnswerCopyState === 'err'">复制失败</span>
                    <span v-else>复制 Markdown</span>
                  </el-button>
                  <el-button
                    size="small"
                    plain
                    class="final-toolbar-btn"
                    @click="exportFinalAnswerAsMarkdown"
                  >
                    导出 .md
                  </el-button>
                </div>

                <!-- 正文（折叠/展开） -->
                <div class="final-md" v-html="displayedFinalAnswerHtml" />

                <!-- F2: 长答案展开/收起按钮 -->
                <div v-if="isFinalAnswerLong" class="final-toggle-wrap">
                  <button
                    class="final-toggle-btn"
                    type="button"
                    @click="finalAnswerExpanded = !finalAnswerExpanded"
                  >
                    {{ finalAnswerExpanded ? '收起' : `展开全部 (${finalAnswerCharCount} 字)` }}
                  </button>
                </div>
              </div>

              <!-- 初始 idle -->
              <p v-else class="final-empty">
                提交任务后，Agent 的最终结论会显示在这里
              </p>
            </el-scrollbar>
          </el-tab-pane>

          <el-tab-pane name="artifacts">
            <template #label>
              <el-badge :is-dot="hasNewArtifacts" class="artifact-badge">
                <span>产物</span>
              </el-badge>
            </template>
            <div class="artifact-toolbar">
              <el-button size="small" plain :icon="Refresh" @click="fetchArtifacts">
                Refresh
              </el-button>
            </div>
            <el-table
              :data="artifactList"
              height="190"
              class="artifact-table"
              header-cell-class-name="dark-table-header"
              empty-text="No artifacts yet"
            >
              <el-table-column prop="name" label="File" show-overflow-tooltip />
              <el-table-column prop="size_kb" label="KB" width="84" />
              <el-table-column prop="created_at" label="Created" width="168" />
              <el-table-column label="Action" width="92">
                <template #default="scope">
                  <a
                    :href="`http://localhost:8000${scope.row.url}`"
                    download
                    class="download-link"
                  >
                    Download
                  </a>
                </template>
              </el-table-column>
            </el-table>
          </el-tab-pane>

          <!-- K3: Failed runs drawer — post-mortem list with HTML log jump -->
          <el-tab-pane name="runs">
            <template #label>
              <el-badge :is-dot="hasNewRuns" class="artifact-badge">
                <span>运行记录</span>
              </el-badge>
            </template>
            <RunRegistryPanel
              :refresh-token="runHistoryRefreshToken"
              @loaded="() => { if (activeBottomTab === 'runs') hasNewRuns = false }"
              @open-detail="hasNewRuns = false"
            />
          </el-tab-pane>

          <el-tab-pane name="failed">
            <template #label>
              <el-badge :is-dot="hasNewFailures" class="artifact-badge">
                <span>失败记录</span>
              </el-badge>
            </template>
            <div class="artifact-toolbar">
              <el-button
                size="small"
                plain
                :icon="Refresh"
                :loading="failedRunsLoading"
                @click="fetchFailedRuns"
              >
                Refresh
              </el-button>
              <span class="failed-runs-count">
                {{ failedRunsList.length }} 条记录
              </span>
            </div>
            <el-table
              :data="failedRunsList"
              height="190"
              class="artifact-table failed-runs-table failed-runs-clickable"
              header-cell-class-name="dark-table-header"
              empty-text="目前还没有失败记录 🎉"
              @row-click="openFailedRunDetail"
            >
              <el-table-column label="Time" width="138">
                <template #default="scope">
                  {{ formatFailedRunTime(scope.row.ts) }}
                </template>
              </el-table-column>
              <el-table-column prop="run_id" label="Run ID" width="158" show-overflow-tooltip />
              <el-table-column label="Goal" show-overflow-tooltip>
                <template #default="scope">
                  <span :title="scope.row.goal || ''">
                    {{ scope.row.goal || '—' }}
                  </span>
                </template>
              </el-table-column>
              <el-table-column label="Reason" show-overflow-tooltip>
                <template #default="scope">
                  <span class="failed-reason" :title="scope.row.reason || ''">
                    {{ scope.row.reason || '—' }}
                  </span>
                </template>
              </el-table-column>
              <el-table-column label="Step" width="62" align="center">
                <template #default="scope">
                  {{ Number.isFinite(scope.row.step_count) ? scope.row.step_count : '—' }}
                </template>
              </el-table-column>
              <el-table-column label="Duration" width="86" align="center">
                <template #default="scope">
                  {{ formatFailedRunDuration(scope.row.duration_s) }}
                </template>
              </el-table-column>
              <el-table-column label="Action" width="118">
                <template #default="scope">
                  <el-button
                    size="small"
                    type="primary"
                    plain
                    :disabled="scope.row.paths_exist && scope.row.paths_exist.html_log === false"
                    :title="scope.row.paths_exist && scope.row.paths_exist.html_log === false
                      ? 'HTML 日志文件已被清理'
                      : '在新标签页打开完整 HTML 轨迹日志'"
                    @click.stop="openFailedRunLog(scope.row)"
                  >
                    HTML 日志
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
          </el-tab-pane>
        </el-tabs>
      </div>
    </section>

    <el-dialog
      v-model="authDialogOpen"
      title="Auth Manager"
      width="560px"
      class="auth-dialog"
      destroy-on-close
    >
      <div class="auth-dialog-body">
        <div class="field-group">
          <label>Login URL</label>
          <el-input
            v-model="authLoginUrl"
            clearable
            placeholder="留空则使用目标 URL"
          />
        </div>

        <div class="field-group">
          <label>Save as Profile</label>
          <el-input
            v-model="authProfileName"
            clearable
            placeholder="例如 zhihu_default / oa_test01"
          />
        </div>

        <div class="auth-actions">
          <el-button
            type="primary"
            plain
            :loading="isAuthRecording"
            @click="startManualAuth"
          >
            打开登录窗口
          </el-button>
          <el-button
            type="success"
            :disabled="!isAuthRecording"
            @click="saveManualAuth"
          >
            保存 Profile
          </el-button>
          <el-button
            type="warning"
            plain
            :disabled="!isAuthRecording"
            @click="cancelManualAuth"
          >
            取消
          </el-button>
        </div>

        <div class="profile-list">
          <div class="field-title-row">
            <label>已保存 Profiles</label>
            <el-button text size="small" :icon="Refresh" @click="loadAuthProfiles">
              Refresh
            </el-button>
          </div>
          <div v-if="authProfileOptions.length" class="profile-tags">
            <button
              v-for="profileItem in authProfileOptions"
              :key="profileItem.name"
              type="button"
              @click="useAuthProfile(profileItem.name)"
            >
              <span>{{ profileItem.name }}</span>
              <small>
                {{ profileItem.cookies }} cookies
                <template v-if="profileItem.cf_clearance">
                  · CF
                  <template v-if="profileItem.cf_clearance_expires_in_hours">
                    ~{{ Math.round(profileItem.cf_clearance_expires_in_hours) }}h
                  </template>
                </template>
              </small>
            </button>
          </div>
          <p v-else class="empty-profile">暂无 profile，先打开登录窗口并保存。</p>
        </div>
      </div>
    </el-dialog>

    <!-- N: Phase event detail dialog — opens on chip click -->
    <el-dialog
      v-model="phaseDialogVisible"
      title="Phase Event"
      width="640px"
      class="phase-dialog"
      destroy-on-close
    >
      <div v-if="selectedPhaseEvent" class="phase-dialog-body">
        <div class="phase-dialog-header">
          <span
            class="phase-dialog-pill"
            :style="{
              background: phaseChipStyle(selectedPhaseEvent).background,
              color: phaseChipStyle(selectedPhaseEvent).color,
              borderColor: phaseChipStyle(selectedPhaseEvent).border,
            }"
          >
            {{ String(selectedPhaseEvent.phase || 'unknown') }}
          </span>
          <span class="phase-dialog-sev">
            severity: <strong>{{ String(selectedPhaseEvent.severity || 'info') }}</strong>
          </span>
          <!-- U: surface notice_severity when present + different from severity. -->
          <span
            v-if="selectedPhaseEvent.notice_severity &&
                  selectedPhaseEvent.notice_severity !== selectedPhaseEvent.severity"
            class="phase-dialog-sev"
            title="BrowserEnv _last_notice_severity at emit time — drives chip color when higher than the phase severity."
          >
            notice: <strong>{{ String(selectedPhaseEvent.notice_severity) }}</strong>
          </span>
          <span
            v-if="Number.isFinite(selectedPhaseEvent.step)"
            class="phase-dialog-step"
          >
            step <strong>{{ selectedPhaseEvent.step }}</strong>
          </span>
          <span
            v-if="Number.isFinite(selectedPhaseEvent.duration_ms)"
            class="phase-dialog-dur"
          >
            duration <strong>{{ selectedPhaseEvent.duration_ms }}ms</strong>
          </span>
        </div>
        <div class="phase-dialog-time">
          {{ selectedPhaseTime }}
        </div>
        <p
          v-if="selectedPhaseEvent.message"
          class="phase-dialog-message"
        >
          {{ selectedPhaseEvent.message }}
        </p>
        <pre class="phase-dialog-json"><code>{{ selectedPhaseJson }}</code></pre>
      </div>
      <template #footer>
        <!-- T: prev/next nav so the user can step through events without
             closing the dialog (also bound to ← / → keys). The buttons
             are always rendered so the keyboard hint is discoverable
             even on first open. -->
        <el-button
          size="small"
          plain
          :disabled="filteredPhaseEvents.length < 2"
          title="上一条事件 (←)"
          @click="goToPrevPhaseEvent"
        >← 上一条</el-button>
        <el-button
          size="small"
          plain
          :disabled="filteredPhaseEvents.length < 2"
          title="下一条事件 (→)"
          @click="goToNextPhaseEvent"
        >下一条 →</el-button>
        <el-button size="small" plain @click="copyPhaseJson">复制 JSON</el-button>
        <el-button size="small" @click="phaseDialogVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- T: Keyboard shortcuts cheat-sheet (Ctrl+/) ───────────────────── -->
    <ShortcutHelpDialog v-model:visible="helpDialogVisible" />

    <!-- K6: Failed-run detail dialog ─────────────────────────────────
         Opened by clicking a row in 失败记录. Two columns:
           Left  → structured fields + JSON pretty-print
           Right → historic phase events preview (fetched on open)
         Wider than other dialogs (760px) so the JSON doesn't wrap.
    -->
    <el-dialog
      v-model="failedRunDialogVisible"
      :title="selectedFailedRun ? `失败 run · ${selectedFailedRun.run_id}` : '失败 run'"
      width="760px"
      class="failed-run-dialog"
      destroy-on-close
      @close="closeFailedRunDetail"
    >
      <div v-if="selectedFailedRun" class="failed-run-dialog-body">
        <!-- Structured key-value summary (top section) -->
        <dl class="failed-run-meta">
          <div class="meta-row">
            <dt>Run ID</dt>
            <dd>{{ selectedFailedRun.run_id || '—' }}</dd>
          </div>
          <div class="meta-row">
            <dt>Time</dt>
            <dd>{{ formatFailedRunTime(selectedFailedRun.ts) }}</dd>
          </div>
          <div class="meta-row">
            <dt>Duration</dt>
            <dd>{{ formatFailedRunDuration(selectedFailedRun.duration_s) }}</dd>
          </div>
          <div class="meta-row">
            <dt>Step</dt>
            <dd>
              {{ Number.isFinite(selectedFailedRun.step_count)
                ? selectedFailedRun.step_count
                : '—' }}
            </dd>
          </div>
          <div v-if="selectedFailedRun.exception_type" class="meta-row">
            <dt>Exception</dt>
            <dd class="meta-mono">{{ selectedFailedRun.exception_type }}</dd>
          </div>
          <div v-if="selectedFailedRun.goal" class="meta-row meta-row-full">
            <dt>Goal</dt>
            <dd>{{ selectedFailedRun.goal }}</dd>
          </div>
          <div class="meta-row meta-row-full">
            <dt>Reason</dt>
            <dd class="meta-mono failed-reason-block">
              {{ selectedFailedRun.reason || '—' }}
            </dd>
          </div>
        </dl>

        <!-- Phase events preview ------------------------------------ -->
        <section class="failed-run-preview">
          <header class="preview-header">
            <h4>Phase 事件预览</h4>
            <span v-if="failedRunPhaseStatus === 'ok'" class="preview-meta">
              {{ failedRunPhaseEvents.length }} / {{ failedRunPhaseTotal }} 条
              <span v-if="failedRunPhaseTruncated" class="preview-truncated">
                · 仅显示最后 {{ failedRunPhaseEvents.length }} 条
              </span>
            </span>
            <el-button
              v-if="failedRunPhaseStatus !== 'loading'"
              size="small"
              plain
              :icon="Refresh"
              @click="fetchFailedRunPhaseEvents(selectedFailedRun)"
            >
              刷新
            </el-button>
          </header>

          <div v-if="failedRunPhaseStatus === 'loading'" class="preview-status">
            正在加载 phase 事件 …
          </div>
          <div v-else-if="failedRunPhaseStatus === 'missing'" class="preview-status">
            该 run 的 phase_jsonl 已被清理，无可显示的事件。
          </div>
          <div v-else-if="failedRunPhaseStatus === 'error'" class="preview-status preview-status-error">
            加载失败 — 后端日志可能已损坏或权限不可读。
          </div>
          <ul
            v-else-if="failedRunPhaseEvents.length"
            class="preview-list"
          >
            <li
              v-for="(evt, idx) in failedRunPhaseEvents"
              :key="`${evt.ts || ''}_${idx}`"
              class="preview-item"
              :class="`preview-sev-${formatPhasePreviewSeverity(evt.severity)}`"
            >
              <span class="preview-time">{{ formatPhasePreviewTs(evt.ts) }}</span>
              <span class="preview-phase">{{ evt.phase || 'unknown' }}</span>
              <span v-if="Number.isFinite(evt.step)" class="preview-step">
                step {{ evt.step }}
              </span>
              <span v-if="Number.isFinite(evt.duration_ms)" class="preview-dur">
                {{ evt.duration_ms }}ms
              </span>
              <span v-if="evt.message" class="preview-msg">{{ evt.message }}</span>
            </li>
          </ul>
          <div v-else class="preview-status">
            （这条 run 没有记录 phase 事件）
          </div>
        </section>

        <!-- JSON dump -->
        <section class="failed-run-json-section">
          <h4>完整 JSON</h4>
          <pre class="phase-dialog-json"><code>{{ selectedFailedRunJson }}</code></pre>
        </section>
      </div>

      <template #footer>
        <el-button
          size="small"
          plain
          :disabled="failedRunsList.length < 2"
          title="上一条 (←)"
          @click="goToPrevFailedRun"
        >← 上一条</el-button>
        <el-button
          size="small"
          plain
          :disabled="failedRunsList.length < 2"
          title="下一条 (→)"
          @click="goToNextFailedRun"
        >下一条 →</el-button>
        <el-button
          size="small"
          plain
          :disabled="!selectedFailedRun
            || (selectedFailedRun.paths_exist
              && selectedFailedRun.paths_exist.html_log === false)"
          @click="openFailedRunLog(selectedFailedRun)"
        >打开 HTML 日志</el-button>
        <el-button size="small" plain @click="copyFailedRunJson">复制 JSON</el-button>
        <el-button size="small" @click="closeFailedRunDetail">关闭</el-button>
      </template>
    </el-dialog>
  </main>
</template>

<style scoped>
:global(html.dark),
:global(body) {
  min-height: 100%;
  background: var(--vsp-bg);
}

:global(body) {
  margin: 0;
}

.app-shell {
  display: grid;
  grid-template-columns: minmax(360px, 35%) minmax(0, 1fr);
  gap: 18px;
  height: 100vh;
  overflow: hidden;
  padding: 22px;
  background:
    linear-gradient(180deg, rgb(var(--rgb-info) / 0.08), transparent 32%),
    var(--vsp-bg);
  color: var(--vsp-text);
}

.vspider-panel {
  background: var(--vsp-surface);
  border: 1px solid var(--vsp-border);
  border-radius: 8px;
  box-shadow: 0 8px 22px rgb(var(--rgb-black) / 0.18);
}

/* D: 主次层级 — 主画面保留重投影，其余面板轻量化 */
.preview-panel.vspider-panel {
  box-shadow: 0 18px 42px rgb(var(--rgb-black) / 0.32);
}

.control-panel {
  display: flex;
  height: 100%;
  min-height: 0;
  overflow: hidden;
  flex-direction: column;
}

.brand-header {
  display: flex;
  gap: 14px;
  align-items: center;
  padding: 22px 22px 16px;
  border-bottom: 1px solid var(--vsp-border);
}

.brand-mark {
  display: grid;
  width: 42px;
  height: 42px;
  place-items: center;
  border-radius: 8px;
  color: var(--vsp-accent);
  background: var(--vsp-bg);
  border: 1px solid rgb(var(--rgb-accent) / 0.28);
}

.brand-icon {
  width: 22px;
  height: 22px;
}

.brand-header h1,
.panel-title h2 {
  margin: 0;
  font-size: 18px;
  line-height: 1.25;
  letter-spacing: 0;
}

.brand-header p,
.panel-title p {
  margin: 4px 0 0;
  color: var(--vsp-text-faint);
  font-size: 12px;
}

.control-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 18px 22px 20px;
}

.field-group {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 16px;
}

.field-group label,
.field-title-row label {
  color: var(--vsp-text-label);
  font-size: 13px;
  font-weight: 600;
}

.field-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.field-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}

.full-width {
  width: 100%;
}

.model-center {
  padding: 12px;
  border: 1px solid rgb(var(--rgb-accent) / 0.16);
  border-radius: 8px;
  background: rgb(var(--rgb-accent) / 0.035);
}

.model-popover {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.model-popover label {
  color: var(--vsp-text-label);
  font-size: 12px;
  font-weight: 600;
}

.model-warning {
  margin: 0;
  color: var(--vsp-warn-muted);
  font-size: 12px;
  line-height: 1.45;
}

.advanced-collapse {
  --el-collapse-border-color: var(--vsp-border);
  --el-collapse-header-bg-color: transparent;
  --el-collapse-content-bg-color: transparent;
  --el-collapse-header-text-color: var(--vsp-text-label);
  --el-collapse-content-text-color: var(--vsp-text-label);
  margin-top: 2px;
}

.upload-icon {
  color: var(--vsp-accent);
  font-size: 26px;
}

.upload-copy,
.file-status {
  color: var(--vsp-text-muted-alt);
  font-size: 13px;
}

.file-status {
  margin: 8px 0 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.attachment-intent-select {
  width: 100%;
  margin-top: 8px;
}

.field-hint {
  color: var(--vsp-text-muted-alt);
  font-weight: 400;
  font-size: 12px;
}

.output-contract-preview__card {
  padding: 10px 12px;
  border: 1px solid rgb(var(--rgb-accent) / 0.25);
  border-radius: 8px;
  background: rgb(var(--rgb-accent) / 0.06);
  font-size: 13px;
}

.output-contract-preview__row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

.output-contract-preview__label {
  color: var(--vsp-text-muted-alt);
}

.output-contract-preview__sep {
  color: var(--vsp-gray-500);
}

.output-contract-preview__reasons {
  margin: 8px 0 0;
  color: var(--vsp-text-muted-alt);
  font-size: 12px;
  line-height: 1.4;
}

.output-contract-preview__loading {
  color: var(--vsp-text-muted-alt);
  font-size: 13px;
}

.completion-evidence-panel {
  margin: 0 12px 10px;
  padding: 10px 12px;
  border: 1px solid var(--vsp-slate-700);
  border-radius: 8px;
  background: rgb(var(--rgb-ink) / 0.65);
}

.completion-evidence-panel__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 6px;
}

.completion-evidence-panel__msg {
  margin: 0 0 6px;
  color: var(--vsp-text-2);
  font-size: 12px;
}

.completion-evidence-panel__row {
  display: flex;
  gap: 8px;
  font-size: 12px;
  color: var(--vsp-text-muted);
  line-height: 1.5;
}

.completion-evidence-panel__label {
  flex: 0 0 auto;
  color: var(--vsp-text-dim);
}

.solver-on {
  color: var(--vsp-accent);
}

.solver-off {
  color: var(--vsp-warn-brown);
}

.advanced-collapse label {
  display: block;
  margin: 10px 0 6px;
  color: var(--vsp-text-2-alt2);
  font-size: 13px;
}

.action-footer {
  position: sticky;
  bottom: 0;
  z-index: 10;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  padding: 16px 22px 20px;
  border-top: 1px solid var(--vsp-border);
  background: var(--vsp-surface);
}

.run-button {
  --el-button-bg-color: var(--vsp-accent);
  --el-button-border-color: var(--vsp-accent);
  --el-button-hover-bg-color: var(--vsp-accent-bright);
  --el-button-hover-border-color: var(--vsp-accent-bright);
  --el-button-text-color: var(--vsp-surface-mint);
  font-weight: 700;
}

.stop-button {
  --el-button-bg-color: rgb(var(--rgb-rose) / 0.1);
  --el-button-border-color: var(--vsp-danger-rose);
  --el-button-text-color: var(--vsp-danger-rose);
  --el-button-hover-bg-color: var(--vsp-danger-rose);
  --el-button-hover-border-color: var(--vsp-danger-rose);
  --el-button-hover-text-color: var(--vsp-bg);
}

.stop-button:hover {
  box-shadow: 0 0 15px rgb(var(--rgb-danger) / 0.45);
}

.monitor-panel {
  display: grid;
  min-height: 0;
  grid-template-rows: minmax(0, 2fr) minmax(180px, 1fr);
  gap: 18px;
}

.preview-panel,
.terminal-panel {
  display: flex;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
}

.panel-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 18px 12px;
}

.panel-title.compact {
  padding-bottom: 10px;
}

.live-indicator,
.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  color: var(--vsp-accent);
  font-size: 12px;
  font-weight: 700;
}

.live-indicator i {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: var(--vsp-accent);
  box-shadow: 0 0 0 0 rgb(var(--rgb-accent) / 0.58);
  animation: pulse-live 1.6s infinite;
}

.status-pill {
  padding: 4px 8px;
  border-radius: 999px;
  background: rgb(var(--rgb-accent) / 0.08);
  border: 1px solid rgb(var(--rgb-accent) / 0.2);
}

.status-pill.error,
.status-pill.disconnected {
  color: var(--vsp-danger-rose);
  background: rgb(var(--rgb-rose) / 0.08);
  border-color: rgb(var(--rgb-rose) / 0.24);
}

.preview-stage {
  display: grid;
  min-height: 0;
  flex: 1;
  margin: 0 18px 18px;
  overflow: hidden;
  place-items: center;
  border-radius: 8px;
  background: var(--vsp-surface-2);
  border: 1px solid var(--vsp-border);
  position: relative;
}

.preview-stage img {
  width: 100%;
  height: 100%;
  object-fit: contain;
}

.preview-placeholder {
  display: grid;
  width: 100%;
  height: 100%;
  place-items: center;
  color: var(--vsp-text-dim-alt);
  font-size: 14px;
}

.hitl-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 28px;
  background: rgb(var(--rgb-danger-deep) / 0.82);
  backdrop-filter: blur(6px);
}

.hitl-card {
  max-width: 560px;
  text-align: center;
  color: var(--vsp-white);
}

.hitl-title {
  margin-bottom: 14px;
  color: var(--vsp-white);
  font-size: 26px;
  font-weight: 800;
  animation: pulse-live 1.2s infinite;
}

.hitl-copy {
  margin-bottom: 18px;
  color: var(--vsp-danger-pale);
  font-size: 15px;
  line-height: 1.7;
}

.hitl-reason {
  margin: 0 auto 22px;
  padding: 10px 12px;
  color: var(--vsp-danger-soft);
  background: rgb(var(--rgb-black) / 0.24);
  border: 1px solid rgb(var(--rgb-danger-soft) / 0.24);
  border-radius: 8px;
  font-size: 13px;
}

.resume-button {
  box-shadow: 0 0 24px rgb(var(--rgb-success) / 0.38);
}

.terminal-scroll {
  flex: 1;
  min-height: 0;
  height: 190px;
  margin: 0;
  padding: 12px;
  border-radius: 8px;
  background: var(--vsp-surface-sunken);
  border: 1px solid rgb(var(--rgb-accent) / 0.18);
  font-family: Consolas, 'JetBrains Mono', 'SFMono-Regular', monospace;
}

/* ── M: Phase timeline panel ───────────────────────────────────────── */
.timeline-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  position: relative; /* P: anchor for the floating jump-to-bottom button */
}

.timeline-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 0 8px;
  font-size: 13px;
  color: var(--vsp-text-2);
}

.timeline-summary {
  display: flex;
  align-items: center;
  gap: 8px;
}

.timeline-summary strong {
  color: var(--vsp-slate-200);
  font-weight: 600;
}

.timeline-warn-pill,
.timeline-err-pill {
  display: inline-flex;
  align-items: center;
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.02em;
}

.timeline-warn-pill {
  background: rgb(var(--rgb-warn) / 0.15);
  color: var(--vsp-warn);
  border: 1px solid rgb(var(--rgb-warn) / 0.4);
}

.timeline-err-pill {
  background: rgb(var(--rgb-danger) / 0.18);
  color: var(--vsp-danger);
  border: 1px solid rgb(var(--rgb-danger) / 0.45);
}

.timeline-toolbar-spacer {
  flex: 1;
}

.timeline-clear-btn {
  font-size: 12px;
}

.timeline-filter-frac {
  color: var(--vsp-text-muted);
  font-size: 12px;
  margin: 0 4px 0 -2px;
}

/* V: phase stats / histogram panel ───────────────────────────────── */
.timeline-stats-panel {
  margin: 6px 0 8px;
  padding: 10px 12px;
  border-radius: 6px;
  background: var(--vsp-bg-deep);
  border: 1px solid rgb(var(--rgb-slate) / 0.18);
}

.timeline-stats-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 12px;
  color: var(--vsp-text-muted);
}

.timeline-stats-title {
  font-weight: 600;
  color: var(--vsp-text-strong);
  flex: 1;
}

.timeline-stats-sort-label {
  letter-spacing: 0.04em;
}

.timeline-stats-sort-btn {
  padding: 1px 7px;
  border-radius: 4px;
  background: transparent;
  border: 1px solid rgb(var(--rgb-slate) / 0.25);
  color: var(--vsp-text-2);
  font-family: Consolas, 'JetBrains Mono', monospace;
  font-size: 11px;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
}

.timeline-stats-sort-btn:hover {
  border-color: rgb(var(--rgb-indigo) / 0.6);
  color: var(--vsp-indigo-300);
}

.timeline-stats-sort-btn.is-active {
  background: rgb(var(--rgb-indigo) / 0.18);
  border-color: rgb(var(--rgb-indigo) / 0.7);
  color: var(--vsp-indigo-200);
}

.timeline-stats-grid {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-family: Consolas, 'JetBrains Mono', monospace;
  font-size: 11.5px;
}

/* Shared layout: 7 explicit columns. Sparkline gets the remaining
 * space via 1fr so wider panels show longer histograms.
 */
.timeline-stats-grid-head,
.timeline-stats-row {
  display: grid;
  grid-template-columns:
    minmax(110px, 1.2fr)   /* phase */
    minmax(80px, 1fr)      /* sparkline */
    44px                   /* count */
    52px                   /* mean */
    52px                   /* p50 */
    52px                   /* p95 */
    52px                   /* max */
    minmax(80px, 0.8fr);   /* sev pills */
  align-items: center;
  gap: 8px;
  padding: 2px 4px;
}

.timeline-stats-grid-head {
  color: var(--vsp-text-dim);
  font-size: 10.5px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.timeline-stats-grid-head .num,
.timeline-stats-row .num {
  text-align: right;
  color: var(--vsp-text-2);
}

.timeline-stats-row {
  background: transparent;
  border: 1px solid transparent;
  border-radius: 4px;
  cursor: pointer;
  color: var(--vsp-text-2);
  text-align: left;
  transition: background 0.15s, border-color 0.15s, opacity 0.15s;
}

.timeline-stats-row:hover {
  background: rgb(var(--rgb-indigo) / 0.08);
  border-color: rgb(var(--rgb-indigo) / 0.25);
}

.timeline-stats-row.is-off {
  opacity: 0.45;
  text-decoration: line-through;
}

.stat-phase {
  color: var(--vsp-info-200);
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.stat-spark {
  width: 100%;
  height: 22px;
  color: var(--vsp-indigo-500); /* fill = currentColor */
  display: block;
}

.stat-spark-empty {
  fill: var(--vsp-slate-600);
  font-size: 8px;
  font-family: Consolas, monospace;
}

.stat-sev {
  display: flex;
  gap: 3px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.stat-sev-pill {
  display: inline-block;
  padding: 0 5px;
  border-radius: 3px;
  font-size: 10.5px;
  font-weight: 600;
  line-height: 1.5;
  border: 1px solid transparent;
}

.stat-sev-pill.sev-info {
  background: rgb(var(--rgb-indigo) / 0.15);
  border-color: rgb(var(--rgb-indigo) / 0.35);
  color: var(--vsp-indigo-200);
}

.stat-sev-pill.sev-warn {
  background: rgb(var(--rgb-warn) / 0.15);
  border-color: rgb(var(--rgb-warn) / 0.45);
  color: var(--vsp-warn-soft);
}

.stat-sev-pill.sev-error {
  background: rgb(var(--rgb-danger) / 0.18);
  border-color: rgb(var(--rgb-danger) / 0.55);
  color: var(--vsp-danger-soft);
}

.timeline-stats-footnote {
  margin: 8px 0 0;
  font-size: 11px;
  color: var(--vsp-text-dim);
  line-height: 1.5;
}

/* Highlight the toolbar toggle when the panel is open */
.timeline-clear-btn.is-active {
  border-color: rgb(var(--rgb-indigo) / 0.6) !important;
  color: var(--vsp-indigo-200) !important;
}

/* W: Replay file input is invisible — only triggered programmatically */
.replay-file-input {
  display: none;
}

/* W: Replay-mode banner */
.timeline-replay-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0 0 8px;
  padding: 6px 12px;
  border-radius: 6px;
  background: linear-gradient(
    90deg,
    rgb(var(--rgb-violet) / 0.12),
    rgb(var(--rgb-violet) / 0.04)
  );
  border: 1px solid rgb(var(--rgb-violet) / 0.45);
  color: var(--vsp-purple-200);
  font-size: 12.5px;
}

.timeline-replay-banner .replay-icon {
  color: var(--vsp-purple-400);
  font-size: 11px;
}

.timeline-replay-banner .replay-text {
  flex: 1;
  font-weight: 600;
  letter-spacing: 0.02em;
}

.timeline-replay-banner .replay-source {
  font-weight: 400;
  color: var(--vsp-purple-300);
  font-family: Consolas, 'JetBrains Mono', monospace;
  font-size: 12px;
}

.timeline-replay-banner .replay-exit-btn {
  border-color: rgb(var(--rgb-violet) / 0.55) !important;
  color: var(--vsp-purple-200) !important;
}

.timeline-replay-banner .replay-exit-btn:hover {
  background: rgb(var(--rgb-violet) / 0.18) !important;
}

.capability-replay-banner {
  margin: 0;
}

/* O: filter chip row */
.timeline-filter-row {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 16px;
  padding: 0 0 8px;
  border-bottom: 1px solid rgb(var(--rgb-slate) / 0.12);
  margin-bottom: 6px;
}

.timeline-filter-group {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

.timeline-filter-label {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--vsp-text-dim);
  margin-right: 2px;
}

.timeline-filter-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid rgb(var(--rgb-slate) / 0.35);
  background: rgb(var(--rgb-slate) / 0.08);
  color: var(--vsp-text-2);
  font-size: 11.5px;
  font-family: Consolas, 'JetBrains Mono', 'SFMono-Regular', monospace;
  cursor: pointer;
  transition: opacity 0.1s ease, background 0.1s ease, border-color 0.1s ease;
}

.timeline-filter-pill:hover {
  background: rgb(var(--rgb-slate) / 0.18);
}

.timeline-filter-pill:focus-visible {
  outline: 2px solid var(--vsp-blue-500);
  outline-offset: 2px;
}

/* "off" = excluded from view; show muted/strikethrough */
.timeline-filter-pill.is-off {
  opacity: 0.45;
  text-decoration: line-through;
}

/* Severity-tinted pills (only when ON) */
.timeline-filter-pill.sev-warn:not(.is-off) {
  background: rgb(var(--rgb-warn) / 0.15);
  border-color: rgb(var(--rgb-warn) / 0.4);
  color: var(--vsp-warn);
}
.timeline-filter-pill.sev-error:not(.is-off) {
  background: rgb(var(--rgb-danger) / 0.18);
  border-color: rgb(var(--rgb-danger) / 0.45);
  color: var(--vsp-danger);
}
.timeline-filter-pill.sev-info:not(.is-off) {
  background: rgb(var(--rgb-blue) / 0.15);
  border-color: rgb(var(--rgb-blue) / 0.4);
  color: var(--vsp-blue-400);
}

.timeline-filter-pill-count {
  font-size: 10.5px;
  font-weight: 600;
  padding: 0 5px;
  border-radius: 999px;
  background: rgb(var(--rgb-ink) / 0.55);
  color: inherit;
  opacity: 0.85;
}

.timeline-scroll {
  flex: 1;
  min-height: 0;
  height: 190px;
  padding: 8px 4px 8px 0;
  border-radius: 8px;
  background: var(--vsp-bg-deep);
  border: 1px solid rgb(var(--rgb-accent) / 0.18);
}

.timeline-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 6px 12px;
  border-bottom: 1px dashed rgb(var(--rgb-slate) / 0.12);
}

.timeline-row:last-child {
  border-bottom: none;
}

.timeline-step-tag {
  flex: 0 0 60px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.02em;
  text-transform: uppercase;
  color: var(--vsp-text-muted);
  padding-top: 4px;
}

.timeline-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  flex: 1;
  min-width: 0;
}

.timeline-chip {
  display: inline-flex;
  flex-direction: column;
  gap: 2px;
  max-width: 100%;
  padding: 4px 10px;
  border-radius: 6px;
  border: 1px solid;
  font-size: 12px;
  line-height: 1.35;
  text-align: left;
  cursor: pointer;
  transition: transform 0.08s ease, box-shadow 0.08s ease, opacity 0.08s ease;
  font-family: Consolas, 'JetBrains Mono', 'SFMono-Regular', monospace;
}

.timeline-chip:hover {
  transform: translateY(-1px);
  box-shadow: 0 2px 6px rgb(var(--rgb-black) / 0.25);
}

.timeline-chip:active {
  transform: translateY(0);
  opacity: 0.85;
}

.timeline-chip:focus-visible {
  outline: 2px solid var(--vsp-blue-500);
  outline-offset: 2px;
}

.timeline-chip-label {
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.timeline-chip-detail {
  font-size: 10.5px;
  opacity: 0.85;
  font-weight: 400;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 100%;
}

/* P: floating jump-to-bottom button. Pinned to the bottom-right of
   .timeline-panel so it doesn't get clipped by the el-scrollbar's
   internal track. */
.timeline-jump-bottom {
  position: absolute;
  right: 12px;
  bottom: 12px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 5px 12px;
  border-radius: 999px;
  border: 1px solid rgb(var(--rgb-blue) / 0.5);
  background: rgb(var(--rgb-blue-deep) / 0.85);
  color: var(--vsp-indigo-100);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  box-shadow: 0 4px 14px rgb(var(--rgb-black) / 0.35);
  transition: transform 0.1s ease, background 0.1s ease, opacity 0.1s ease;
  backdrop-filter: blur(4px);
}

.timeline-jump-bottom:hover {
  background: rgb(var(--rgb-blue-strong) / 0.95);
  transform: translateY(-1px);
}

.timeline-jump-bottom:active {
  transform: translateY(0);
  opacity: 0.9;
}

.timeline-jump-bottom:focus-visible {
  outline: 2px solid var(--vsp-blue-500);
  outline-offset: 2px;
}

.capability-scroll {
  height: 245px;
  border-radius: 8px;
  background: var(--vsp-bg-deep);
  border: 1px solid rgb(var(--rgb-indigo-bright) / 0.22);
}

.capability-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 12px;
}

.capability-hero {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 14px;
  border-radius: 10px;
  background:
    linear-gradient(135deg, rgb(var(--rgb-indigo) / 0.2), rgb(var(--rgb-sky) / 0.06)),
    var(--vsp-slate-900);
  border: 1px solid rgb(var(--rgb-indigo-bright) / 0.35);
}

.capability-kicker {
  color: var(--vsp-indigo-300);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.capability-hero h3 {
  margin: 4px 0;
  color: var(--vsp-slate-50);
  font-size: 18px;
}

.capability-hero p {
  margin: 0;
  color: var(--vsp-text-2);
  font-size: 12.5px;
}

.capability-count {
  align-self: flex-start;
  padding: 4px 10px;
  border-radius: 999px;
  color: var(--vsp-indigo-200);
  background: rgb(var(--rgb-indigo) / 0.18);
  border: 1px solid rgb(var(--rgb-indigo-bright) / 0.45);
  font-family: Consolas, 'JetBrains Mono', monospace;
  font-size: 12px;
}

.capability-hero-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 8px;
}

.capability-export-btn {
  border-color: rgb(var(--rgb-indigo-bright) / 0.45);
  color: var(--vsp-indigo-200);
}

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

.capability-chain,
.capability-role-grid,
.capability-audit-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 8px;
}

.capability-card,
.capability-role-card,
.capability-audit-item {
  padding: 9px 10px;
  border-radius: 8px;
  background: rgb(var(--rgb-ink) / 0.72);
  border: 1px solid rgb(var(--rgb-slate) / 0.16);
}

.capability-card-head,
.capability-role-card header {
  display: flex;
  align-items: center;
  gap: 8px;
}

.capability-card-head strong,
.capability-role-card strong,
.capability-audit-item strong {
  color: var(--vsp-info-200);
  font-size: 13px;
}

.capability-rank {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  color: var(--vsp-slate-900);
  background: var(--vsp-info-200);
  font-size: 11px;
  font-weight: 800;
}

.capability-meta,
.capability-detail,
.capability-audit-item p,
.capability-avoid {
  margin: 5px 0 0;
  color: var(--vsp-text-muted);
  font-size: 12px;
  line-height: 1.45;
}

.capability-detail {
  color: var(--vsp-text-2);
}

.capability-fallback {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.capability-health-strip {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 7px;
  padding: 8px 10px;
  border-radius: 8px;
  color: var(--vsp-text-2);
  background: rgb(var(--rgb-ink) / 0.72);
  border: 1px solid rgb(var(--rgb-slate) / 0.16);
  font-size: 12px;
}

.capability-health-status {
  padding: 3px 8px;
  border-radius: 999px;
  border: 1px solid rgb(var(--rgb-slate) / 0.22);
  font-weight: 700;
}

.capability-health-status.is-healthy,
.capability-health-status.is-executed,
.capability-health-status.is-ok {
  color: var(--vsp-success-soft);
  background: rgb(var(--rgb-success) / 0.12);
  border-color: rgb(var(--rgb-success) / 0.35);
}

.capability-health-status.is-route-only,
.capability-health-status.is-fallback {
  color: var(--vsp-warn-soft);
  background: rgb(var(--rgb-warn) / 0.12);
  border-color: rgb(var(--rgb-warn) / 0.35);
}

.capability-health-status.is-issue,
.capability-health-status.is-error {
  color: var(--vsp-danger-soft);
  background: rgb(var(--rgb-danger) / 0.14);
  border-color: rgb(var(--rgb-danger) / 0.4);
}

.capability-fixture-replay-card,
.capability-efficiency-feedback-replay-card {
  padding: 10px 12px;
  border-radius: 10px;
  color: var(--vsp-text-2);
  background: rgb(var(--rgb-ink) / 0.74);
  border: 1px solid rgb(var(--rgb-indigo-bright) / 0.24);
}

.capability-fixture-replay-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
  gap: 6px 10px;
  font-size: 12px;
}

.capability-fixture-replay-grid span {
  color: var(--vsp-text-muted);
}

.capability-fixture-replay-grid strong {
  color: var(--vsp-sky-100);
}

.capability-fixture-replay-artifact,
.capability-fixture-replay-checks {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
  color: var(--vsp-text-muted);
  font-size: 12px;
}

.capability-fixture-replay-artifact a {
  color: var(--vsp-info-200);
  text-decoration: none;
}

.capability-fixture-replay-check-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 8px 0 0;
  padding: 0;
  list-style: none;
}

.capability-fixture-replay-check-list li {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 7px;
  border-radius: 999px;
  border: 1px solid rgb(var(--rgb-slate) / 0.22);
  background: rgb(var(--rgb-slate) / 0.08);
  font-size: 11.5px;
}

.capability-fixture-replay-check-list li.is-ok {
  color: var(--vsp-success-soft);
  border-color: rgb(var(--rgb-success) / 0.35);
  background: rgb(var(--rgb-success) / 0.12);
}

.capability-fixture-replay-check-list li.is-error {
  color: var(--vsp-danger-soft);
  border-color: rgb(var(--rgb-danger) / 0.4);
  background: rgb(var(--rgb-danger) / 0.14);
}

.capability-fixture-library-card,
.capability-efficiency-feedback-library-card {
  padding: 10px 12px;
  border-radius: 10px;
  color: var(--vsp-text-2);
  background: rgb(var(--rgb-ink) / 0.62);
  border: 1px solid rgb(var(--rgb-slate) / 0.18);
}

.capability-fixture-history-card {
  padding: 10px 12px;
  border-radius: 10px;
  color: var(--vsp-text-2);
  background: rgb(var(--rgb-ink) / 0.58);
  border: 1px solid rgb(var(--rgb-sky-bright) / 0.18);
}

.capability-fixture-library-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  color: var(--vsp-text-muted);
  font-size: 12px;
}

.capability-fixture-history-trend {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
  padding: 7px 9px;
  border-radius: 8px;
  color: var(--vsp-text-2);
  background: rgb(var(--rgb-ink-deep) / 0.32);
  border: 1px solid rgb(var(--rgb-slate) / 0.16);
  font-size: 12px;
}

.capability-fixture-history-trend.is-improved {
  border-color: rgb(var(--rgb-success) / 0.3);
}

.capability-fixture-history-trend.is-regressed {
  border-color: rgb(var(--rgb-danger) / 0.36);
}

.capability-fixture-history-trend.is-stable,
.capability-fixture-history-trend.is-baseline {
  border-color: rgb(var(--rgb-sky-bright) / 0.26);
}

.capability-fixture-library-list,
.capability-efficiency-feedback-library-list,
.capability-fixture-history-list,
.capability-fixture-batch-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 6px;
  margin-top: 8px;
}

.capability-fixture-library-item,
.capability-efficiency-feedback-library-item,
.capability-fixture-history-item,
.capability-fixture-batch-item {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 7px 9px;
  border-radius: 8px;
  background: rgb(var(--rgb-ink-deep) / 0.32);
  border: 1px solid rgb(var(--rgb-slate) / 0.16);
  font-size: 12px;
}

.capability-fixture-library-item strong,
.capability-efficiency-feedback-library-item strong,
.capability-fixture-history-item strong,
.capability-fixture-batch-item strong {
  color: var(--vsp-sky-100);
}

.capability-fixture-library-item span,
.capability-efficiency-feedback-library-item span,
.capability-fixture-history-item span,
.capability-fixture-history-item small,
.capability-fixture-batch-item small,
.capability-fixture-library-empty {
  color: var(--vsp-text-muted);
  font-size: 12px;
}

.capability-fixture-library-item a,
.capability-efficiency-feedback-library-item a {
  color: var(--vsp-info-200);
  text-decoration: none;
}

.capability-fixture-history-item a {
  color: var(--vsp-info-200);
  text-decoration: none;
}

.capability-fixture-library-empty {
  margin-top: 8px;
}

.capability-fixture-triage {
  margin-top: 8px;
  padding: 8px;
  border-radius: 8px;
  background: rgb(var(--rgb-ink-deep) / 0.28);
  border: 1px solid rgb(var(--rgb-indigo-bright) / 0.18);
}

.capability-fixture-triage-focus {
  color: var(--vsp-indigo-200);
  font-size: 12px;
  margin-bottom: 6px;
}

.capability-fixture-triage-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 6px;
}

.capability-fixture-triage-grid div {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.capability-fixture-triage-grid span,
.capability-fixture-triage-grid small {
  color: var(--vsp-text-muted);
  font-size: 11px;
}

.capability-fixture-triage-grid strong {
  color: var(--vsp-sky-100);
  font-size: 12px;
}

.capability-fixture-batch-head-actions {
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.capability-fixture-batch-item.is-ok {
  border-color: rgb(var(--rgb-success) / 0.28);
}

.capability-fixture-history-item.is-ok {
  border-color: rgb(var(--rgb-success) / 0.24);
}

.capability-fixture-batch-item.is-error {
  border-color: rgb(var(--rgb-danger) / 0.36);
}

.capability-fixture-history-item.is-error {
  border-color: rgb(var(--rgb-danger) / 0.3);
}

.capability-exec-summary,
.capability-attempt-list,
.capability-check-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.capability-exec-summary {
  align-items: center;
  color: var(--vsp-text-2);
  font-size: 12px;
}

.capability-exec-status,
.capability-attempt,
.capability-check {
  border-radius: 999px;
  border: 1px solid rgb(var(--rgb-slate) / 0.22);
  background: rgb(var(--rgb-slate) / 0.08);
}

.capability-exec-status,
.capability-check {
  padding: 3px 8px;
  font-size: 11.5px;
  font-weight: 700;
}

.capability-attempt {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 9px;
  color: var(--vsp-text-2);
  font-size: 11.5px;
}

.capability-attempt strong {
  color: var(--vsp-sky-100);
}

.capability-attempt p {
  flex: 1 1 100%;
  margin: 0;
  color: var(--vsp-text-muted);
}

.capability-exec-status.is-complete,
.capability-attempt.is-complete,
.capability-check.is-complete {
  color: var(--vsp-success-soft);
  background: rgb(var(--rgb-success) / 0.12);
  border-color: rgb(var(--rgb-success) / 0.35);
}

.capability-exec-status.is-fallback,
.capability-attempt.is-skip,
.capability-check.is-warning,
.capability-check.is-skip {
  color: var(--vsp-warn-soft);
  background: rgb(var(--rgb-warn) / 0.12);
  border-color: rgb(var(--rgb-warn) / 0.35);
}

.capability-attempt.is-error,
.capability-check.is-error {
  color: var(--vsp-danger-soft);
  background: rgb(var(--rgb-danger) / 0.14);
  border-color: rgb(var(--rgb-danger) / 0.4);
}

.capability-fallback-pill,
.capability-role-card header span,
.capability-audit-item span {
  display: inline-flex;
  padding: 3px 8px;
  border-radius: 999px;
  color: var(--vsp-indigo-200);
  background: rgb(var(--rgb-indigo) / 0.16);
  border: 1px solid rgb(var(--rgb-indigo-bright) / 0.32);
  font-size: 11.5px;
}

.capability-role-card ul {
  margin: 7px 0 0;
  padding-left: 18px;
  color: var(--vsp-text-2);
  font-size: 12px;
  line-height: 1.5;
}

.capability-avoid {
  color: var(--vsp-warn);
}

.capability-json {
  margin: 0;
  max-height: 180px;
  overflow: auto;
  padding: 10px 12px;
  border-radius: 8px;
  color: var(--vsp-text-2);
  background: var(--vsp-slate-950);
  border: 1px solid rgb(var(--rgb-slate) / 0.16);
  font-family: Consolas, 'JetBrains Mono', monospace;
  font-size: 11.5px;
  line-height: 1.45;
}

.bottom-tabs {
  display: flex;
  min-height: 0;
  height: 100%;
  flex-direction: column;
  padding: 8px 14px 14px;
}

:deep(.bottom-tabs .el-tabs__header) {
  margin: 0 0 8px;
}

:deep(.bottom-tabs .el-tabs__content) {
  min-height: 0;
  flex: 1;
}

:deep(.bottom-tabs .el-tab-pane) {
  height: 100%;
}

.terminal-heading,
.artifact-toolbar {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  margin-bottom: 8px;
}

.terminal-heading-spacer {
  flex: 1;
}

.terminal-search-bar {
  display: flex;
  align-items: center;
  gap: 6px;
}

.terminal-search-input {
  width: 220px;
}

.terminal-search-count {
  min-width: 48px;
  text-align: center;
  color: var(--vsp-text-muted);
  font-family: Consolas, 'JetBrains Mono', monospace;
  font-size: 12px;
}

.terminal-search-open-btn {
  font-size: 12px;
}

.artifact-badge {
  line-height: 1;
}

/* K3: failed-runs panel ───────────────────────────────────────────
 * .failed-runs-count sits to the LEFT of the Refresh button.
 * Override the parent flex's `justify-content: flex-end` by pushing
 * the toolbar into space-between mode for this panel via margin.
 */
.failed-runs-count {
  margin-right: auto;
  align-self: center;
  color: var(--vsp-text-muted);
  font-size: 12px;
}

.failed-runs-table .failed-reason {
  color: var(--vsp-rose-300); /* rose-300: signals a problem, not an answer */
  font-family: 'JetBrains Mono', 'SFMono-Regular', Consolas, monospace;
  font-size: 12px;
}

/* K6: clickable rows in 失败记录 table */
.failed-runs-clickable :deep(.el-table__row) {
  cursor: pointer;
}
.failed-runs-clickable :deep(.el-table__row:hover) > td {
  background-color: rgb(var(--rgb-indigo) / 0.08);
}

/* ── K6: Failed-run detail dialog ───────────────────────────────────
 * Three stacked sections: structured meta, phase preview list,
 * full JSON dump. Width is wide enough to keep JSON unwrapped at
 * 80 columns without horizontal scroll for typical traces.
 */
.failed-run-dialog-body {
  display: flex;
  flex-direction: column;
  gap: 16px;
  color: var(--vsp-text-2);
}

.failed-run-meta {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px 16px;
  margin: 0;
  padding: 12px 14px;
  border-radius: 6px;
  background: var(--vsp-bg-deep);
  border: 1px solid rgb(var(--rgb-slate) / 0.18);
}

.failed-run-meta .meta-row {
  display: grid;
  grid-template-columns: 88px 1fr;
  gap: 8px;
  align-items: baseline;
  font-size: 12.5px;
}

.failed-run-meta .meta-row-full {
  grid-column: 1 / -1;
}

.failed-run-meta dt {
  color: var(--vsp-text-muted);
  font-weight: 500;
  letter-spacing: 0.02em;
}

.failed-run-meta dd {
  margin: 0;
  color: var(--vsp-text-strong);
  word-break: break-word;
}

.failed-run-meta .meta-mono {
  font-family: Consolas, 'JetBrains Mono', 'SFMono-Regular', monospace;
  font-size: 12px;
}

.failed-run-meta .failed-reason-block {
  color: var(--vsp-rose-300);
  white-space: pre-wrap;
  max-height: 120px;
  overflow: auto;
}

.failed-run-preview {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.failed-run-preview h4,
.failed-run-json-section h4 {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--vsp-text-strong);
}

.preview-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.preview-meta {
  flex: 1;
  font-size: 12px;
  color: var(--vsp-text-muted);
}

.preview-truncated {
  color: var(--vsp-warn); /* amber-400 — soft hint that we cut data */
}

.preview-status {
  padding: 12px 14px;
  border-radius: 6px;
  background: var(--vsp-bg-deep);
  border: 1px dashed rgb(var(--rgb-slate) / 0.18);
  color: var(--vsp-text-muted);
  font-size: 12.5px;
  text-align: center;
}

.preview-status-error {
  color: var(--vsp-rose-300);
  border-color: rgb(var(--rgb-danger-base) / 0.35);
}

.preview-list {
  list-style: none;
  margin: 0;
  padding: 6px 8px;
  border-radius: 6px;
  background: var(--vsp-bg-deep);
  border: 1px solid rgb(var(--rgb-slate) / 0.18);
  max-height: 220px;
  overflow: auto;
  font-family: Consolas, 'JetBrains Mono', 'SFMono-Regular', monospace;
  font-size: 11.5px;
  line-height: 1.5;
}

.preview-item {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px;
  padding: 2px 4px;
}

.preview-item + .preview-item {
  border-top: 1px dotted rgb(var(--rgb-slate) / 0.08);
}

.preview-time {
  color: var(--vsp-text-dim);
  min-width: 56px;
}

.preview-phase {
  color: var(--vsp-info-200); /* blue-300 */
  font-weight: 600;
}

.preview-step,
.preview-dur {
  color: var(--vsp-text-muted);
}

.preview-msg {
  flex: 1 1 100%;
  color: var(--vsp-text-2);
  padding-left: 56px;
  word-break: break-word;
}

.preview-sev-warn .preview-phase { color: var(--vsp-warn); }
.preview-sev-error .preview-phase { color: var(--vsp-rose-300); }
.preview-sev-warn .preview-msg { color: var(--vsp-warn-soft); }
.preview-sev-error .preview-msg { color: var(--vsp-danger-soft); }

.artifact-table {
  --el-table-bg-color: transparent;
  --el-table-tr-bg-color: transparent;
  --el-table-header-bg-color: var(--vsp-slate-800);
  --el-table-border-color: var(--vsp-slate-700);
  --el-table-text-color: var(--vsp-text-muted);
  --el-table-header-text-color: var(--vsp-text-strong);
  background: transparent;
}

:deep(.artifact-table .el-table__inner-wrapper::before) {
  display: none;
}

:deep(.artifact-table th.el-table__cell),
:deep(.artifact-table tr),
:deep(.artifact-table td.el-table__cell) {
  background: transparent;
  border-bottom-color: var(--vsp-slate-700);
}

:deep(.artifact-table .dark-table-header) {
  background: var(--vsp-slate-800) !important;
  color: var(--vsp-text-strong);
}

.download-link {
  color: var(--vsp-info);
  font-weight: 700;
  text-decoration: none;
}

.download-link:hover {
  color: var(--vsp-info-soft);
}

.log-line,
.empty-log {
  margin: 0;
  color: var(--vsp-success-vivid);
  font-size: 13px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
}

.empty-log {
  color: var(--vsp-text-dim-alt);
}

/* ── R: Severity-aware coloring for terminal log lines ────────────── */
/* Default green (.log-line above) is reserved for "successful action"
   lines emitted by the agent. Tag-prefixed lines override that.        */
.log-line--error {
  color: var(--vsp-danger-vivid);
}

.log-line--warn {
  color: var(--vsp-warn);
}

/* phase / info — desaturated blue, distinct from action-success green */
.log-line--phase {
  color: var(--vsp-info-200);
}

.log-line--done {
  color: var(--vsp-success);
  font-weight: 600;
}

.log-line--hitl {
  color: var(--vsp-purple-400);
  font-weight: 600;
}

.log-line--artifact {
  color: var(--vsp-cyan-300);
}

.log-line--system {
  color: var(--vsp-text-muted); /* slate */
}

.terminal-search-hit,
.terminal-search-current {
  border-radius: 2px;
  padding: 0 1px;
}

.terminal-search-hit {
  background: rgb(var(--rgb-yellow) / 0.28);
  color: var(--vsp-warn-pale);
}

.terminal-search-current {
  background: var(--vsp-warn-strong);
  color: var(--vsp-gray-900);
  box-shadow: 0 0 0 1px rgb(var(--rgb-warn-soft) / 0.7);
}

.auth-dialog-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.auth-actions {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 10px;
  margin: 4px 0 16px;
}

.profile-list {
  padding-top: 14px;
  border-top: 1px solid var(--vsp-border);
}

.profile-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
}

.profile-tags button {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 9px;
  color: var(--vsp-text-label);
  background: var(--vsp-bg);
  border: 1px solid var(--vsp-border);
  border-radius: 8px;
  cursor: pointer;
}

.profile-tags button:hover {
  color: var(--vsp-accent);
  border-color: rgb(var(--rgb-accent) / 0.55);
}

.profile-tags small,
.empty-profile {
  color: var(--vsp-text-faint);
  font-size: 12px;
}

.empty-profile {
  margin: 10px 0 0;
}

:deep(.el-input__wrapper),
:deep(.el-textarea__inner),
:deep(.el-select__wrapper) {
  background: var(--vsp-bg);
  border-radius: 8px;
  box-shadow: 0 0 0 1px var(--vsp-border) inset;
}

:deep(.el-input__wrapper.is-focus),
:deep(.el-textarea__inner:focus),
:deep(.el-select__wrapper.is-focused) {
  box-shadow:
    0 0 0 1px var(--vsp-accent) inset,
    0 0 0 3px rgb(var(--rgb-accent) / 0.14);
}

:deep(.compact-upload .el-upload) {
  width: 100%;
}

:deep(.compact-upload .el-upload-dragger) {
  display: flex;
  min-height: 112px;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 18px 14px;
  background: var(--vsp-bg);
  border-color: var(--vsp-border);
  border-radius: 8px;
}

:deep(.auth-dialog .el-dialog) {
  background: var(--vsp-surface);
  border: 1px solid var(--vsp-border);
  border-radius: 8px;
}

/* ── N: Phase event detail dialog ──────────────────────────────────── */
:deep(.phase-dialog .el-dialog) {
  background: var(--vsp-surface);
  border: 1px solid var(--vsp-border);
  border-radius: 8px;
}

.phase-dialog-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.phase-dialog-header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  font-size: 13px;
  color: var(--vsp-text-2);
}

.phase-dialog-pill {
  display: inline-flex;
  align-items: center;
  padding: 3px 10px;
  border-radius: 6px;
  border: 1px solid;
  font-size: 12.5px;
  font-weight: 600;
  font-family: Consolas, 'JetBrains Mono', 'SFMono-Regular', monospace;
}

.phase-dialog-sev strong,
.phase-dialog-step strong,
.phase-dialog-dur strong {
  color: var(--vsp-slate-200);
  font-weight: 600;
}

.phase-dialog-time {
  font-size: 12px;
  color: var(--vsp-text-muted);
  font-family: Consolas, 'JetBrains Mono', 'SFMono-Regular', monospace;
}

.phase-dialog-message {
  margin: 0;
  padding: 8px 12px;
  border-radius: 6px;
  background: rgb(var(--rgb-slate) / 0.08);
  border-left: 3px solid var(--vsp-text-dim);
  color: var(--vsp-slate-200);
  font-size: 13.5px;
  line-height: 1.5;
}

.phase-dialog-json {
  margin: 0;
  padding: 12px 14px;
  border-radius: 6px;
  background: var(--vsp-bg-deep);
  border: 1px solid rgb(var(--rgb-slate) / 0.18);
  color: var(--vsp-text-2);
  font-size: 12px;
  font-family: Consolas, 'JetBrains Mono', 'SFMono-Regular', monospace;
  max-height: 360px;
  overflow: auto;
  white-space: pre;
  -webkit-overflow-scrolling: touch;
}

/* ── Final Answer 面板 ──
 *
 * 与 Live Terminal / Artifacts 同处于 .bottom-tabs 中。
 * 字体故意比 .log-line (13px) 大一档（14.5px），优化长文本阅读。
 * 不复用 .terminal-scroll 的等宽字体，避免 Markdown 标题/列表观感生硬。
 */
.final-scroll {
  flex: 1;
  min-height: 0;
  height: 190px;
  margin: 0;
  padding: 14px 16px;
  border-radius: 8px;
  background: var(--vsp-surface-sunken);
  border: 1px solid rgb(var(--rgb-info) / 0.18);
}

.final-empty {
  margin: 0;
  padding-top: 32px;
  color: var(--vsp-text-dim-alt);
  font-size: 13.5px;
  text-align: center;
}

/* 状态 A：等待 */
.final-pending {
  display: flex;
  flex-direction: column;
  gap: 14px;
  align-items: center;
  justify-content: center;
  padding: 28px 12px;
}

.final-pending-text {
  margin: 0;
  color: var(--vsp-text-muted-alt);
  font-size: 13.5px;
  letter-spacing: 0.2px;
}

.typing-dots {
  display: inline-flex;
  gap: 6px;
}

.typing-dots span {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: var(--vsp-info);
  opacity: 0.35;
  animation: typing-bounce 1.2s infinite ease-in-out;
}

.typing-dots span:nth-child(2) {
  animation-delay: 0.18s;
}

.typing-dots span:nth-child(3) {
  animation-delay: 0.36s;
}

@keyframes typing-bounce {
  0%, 80%, 100% {
    opacity: 0.25;
    transform: translateY(0);
  }
  40% {
    opacity: 1;
    transform: translateY(-4px);
  }
}

/* 状态 C：兜底文案（可能上面叠加一段 Markdown 文件摘要） */
.final-fallback {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 14px;
  padding: 24px 16px;
}

/* 后端 _synthesize_file_mode_summary 合成的 Markdown 摘要 */
.final-file-summary {
  margin: 0;
  padding: 12px 14px;
  background: rgb(var(--rgb-info) / 0.06);
  border: 1px solid rgb(var(--rgb-info) / 0.18);
  border-radius: 8px;
  font-size: 13.5px;
}

.final-fallback-text {
  margin: 0;
  color: var(--vsp-text-label);
  font-size: 14px;
  line-height: 1.7;
  text-align: center;
}

.tab-jump {
  margin: 0 4px;
  color: var(--vsp-info);
  font-weight: 600;
  text-decoration: none;
  border-bottom: 1px dashed rgb(var(--rgb-info) / 0.5);
  cursor: pointer;
}

.tab-jump:hover {
  color: var(--vsp-info-soft);
  border-bottom-color: var(--vsp-info-soft);
}

/* F3: 域卡片 — 显示在文本答案最顶部 */
.final-domain-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 14px;
  border: 1px solid;
  border-radius: 10px;
  margin-bottom: 2px;
}

.final-domain-icon {
  font-size: 24px;
  line-height: 1;
  user-select: none;
}

.final-domain-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.final-domain-label {
  font-size: 14px;
  font-weight: 700;
  letter-spacing: 0.6px;
}

.final-domain-hint {
  font-size: 12px;
  color: var(--vsp-text-muted-alt);
}

/* F1: 工具栏 / F2: 折叠 — 容器 */
.final-text-wrap {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.final-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border: 1px solid rgb(var(--rgb-info) / 0.18);
  border-radius: 8px;
  background: rgb(var(--rgb-surface-2) / 0.5);
}

.final-meta {
  color: var(--vsp-text-muted-alt);
  font-size: 12px;
  letter-spacing: 0.2px;
  user-select: none;
}

.final-toolbar-spacer {
  flex: 1 1 auto;
}

.final-toolbar-btn {
  font-size: 12px;
}

/* F2: 展开/收起按钮 */
.final-toggle-wrap {
  display: flex;
  justify-content: center;
  margin-top: 4px;
}

.final-toggle-btn {
  padding: 6px 18px;
  font-size: 12.5px;
  color: var(--vsp-info);
  background: transparent;
  border: 1px solid rgb(var(--rgb-info) / 0.35);
  border-radius: 999px;
  cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease, border-color 0.15s ease;
}

.final-toggle-btn:hover {
  color: var(--vsp-info-soft);
  background: rgb(var(--rgb-info) / 0.08);
  border-color: var(--vsp-info-soft);
}

.final-toggle-btn:active {
  background: rgb(var(--rgb-info) / 0.16);
}

/* 状态 B：Markdown 渲染区 */
.final-md {
  color: var(--vsp-text-bright);
  font-size: 14.5px;
  line-height: 1.72;
  word-break: break-word;
}

.final-md :deep(.md-p) {
  margin: 0 0 10px;
}

.final-md :deep(.md-h1),
.final-md :deep(.md-h2),
.final-md :deep(.md-h3) {
  margin: 14px 0 8px;
  color: var(--vsp-text);
  font-weight: 700;
  line-height: 1.3;
}

.final-md :deep(.md-h1) { font-size: 19px; }
.final-md :deep(.md-h2) { font-size: 17px; }
.final-md :deep(.md-h3) { font-size: 15.5px; color: var(--vsp-text-2-alt); }

.final-md :deep(.md-ul) {
  margin: 4px 0 12px;
  padding-left: 22px;
}

.final-md :deep(.md-ul li) {
  margin-bottom: 4px;
}

.final-md :deep(strong) {
  color: var(--vsp-white);
  font-weight: 700;
}

.final-md :deep(.md-icode) {
  padding: 1px 6px;
  color: var(--vsp-warn-gold);
  background: rgb(var(--rgb-gold) / 0.08);
  border: 1px solid rgb(var(--rgb-gold) / 0.18);
  border-radius: 4px;
  font-family: Consolas, 'JetBrains Mono', 'SFMono-Regular', monospace;
  font-size: 13px;
}

.final-md :deep(.md-pre) {
  margin: 8px 0 12px;
  padding: 10px 12px;
  background: var(--vsp-surface-2);
  border: 1px solid var(--vsp-gray-800);
  border-radius: 8px;
  overflow-x: auto;
}

.final-md :deep(.md-pre .md-code) {
  color: var(--vsp-text-label);
  font-family: Consolas, 'JetBrains Mono', 'SFMono-Regular', monospace;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre;
}

.final-md :deep(.md-a) {
  color: var(--vsp-info);
  text-decoration: none;
  border-bottom: 1px solid rgb(var(--rgb-info) / 0.4);
}

.final-md :deep(.md-a:hover) {
  color: var(--vsp-info-soft);
  border-bottom-color: var(--vsp-info-soft);
}

@keyframes pulse-live {
  0% {
    box-shadow: 0 0 0 0 rgb(var(--rgb-accent) / 0.58);
  }
  70% {
    box-shadow: 0 0 0 8px rgb(var(--rgb-accent) / 0);
  }
  100% {
    box-shadow: 0 0 0 0 rgb(var(--rgb-accent) / 0);
  }
}

@media (max-width: 1100px) {
  .app-shell {
    grid-template-columns: 1fr;
    overflow-y: auto;
  }

  .control-panel,
  .monitor-panel {
    min-height: 720px;
  }
}
/* A: Capability 二级子页签 */
:deep(.capability-sub-tabs .el-tabs__header) {
  margin: 0 0 10px;
}

:deep(.capability-sub-tabs .el-tabs__nav-wrap::after) {
  height: 1px;
  background: var(--vsp-border);
}

:deep(.capability-sub-tabs .el-tabs__item) {
  font-size: 13px;
}

/* C: 折叠标题回显当前选择 */
.collapse-title-echo {
  margin-left: 8px;
  max-width: 55%;
  overflow: hidden;
  font-size: 12px;
  color: var(--vsp-text-2);
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* C2: 高级配置入口（抽屉触发器） */
.settings-summary {
  display: flex;
  flex-direction: column;
  gap: 4px;
  width: 100%;
  margin-top: 2px;
  padding: 10px 12px;
  text-align: left;
  background: transparent;
  border: 1px dashed var(--vsp-border);
  border-radius: 8px;
  color: var(--vsp-text-label);
  cursor: pointer;
  transition: border-color 0.2s ease;
}

.settings-summary:hover {
  border-color: var(--vsp-accent);
}

.settings-summary__title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
}

.settings-summary__open {
  margin-left: auto;
  font-size: 12px;
  font-weight: 400;
  color: var(--vsp-text-2);
}

.settings-summary__echo {
  overflow: hidden;
  font-size: 12px;
  color: var(--vsp-text-2);
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* C2: 抽屉视觉打磨 — 卡片化分组、与主面板同一套 token */
:global(.settings-drawer.el-drawer) {
  background: var(--vsp-surface);
  border-left: 1px solid var(--vsp-border);
  box-shadow: -18px 0 42px rgb(var(--rgb-black) / 0.45);
}

:global(.settings-drawer .el-drawer__header) {
  margin-bottom: 0;
  padding: 16px 20px;
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--vsp-text);
  border-bottom: 1px solid var(--vsp-border);
}

:global(.settings-drawer .el-drawer__body) {
  padding: 14px 20px 20px;
}

.drawer-collapse {
  --el-collapse-border-color: transparent;
  border-top: none;
  border-bottom: none;
}

.drawer-collapse :deep(.el-collapse-item) {
  margin-bottom: 12px;
  overflow: hidden;
  background: rgb(var(--rgb-surface-2) / 0.6);
  border: 1px solid var(--vsp-border);
  border-radius: 10px;
}

.drawer-collapse :deep(.el-collapse-item__header) {
  height: 44px;
  padding: 0 14px;
  font-weight: 600;
  background: transparent;
  border-bottom: none;
}

.drawer-collapse :deep(.el-collapse-item.is-active .el-collapse-item__header) {
  border-bottom: 1px solid var(--vsp-border);
}

.drawer-collapse :deep(.el-collapse-item__content) {
  padding: 12px 14px 14px;
}

.drawer-collapse :deep(.el-collapse-item__wrap) {
  background: transparent;
  border-bottom: none;
}

.drawer-collapse .field-group {
  margin-bottom: 0;
}
</style>
