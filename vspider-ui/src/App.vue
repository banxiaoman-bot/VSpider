<script setup>
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  Close,
  Refresh,
  Setting,
  UploadFilled,
  VideoPlay,
} from '@element-plus/icons-vue'
import CapabilityStatusBadge from './components/CapabilityStatusBadge.vue'
import CapabilityTraceList from './components/CapabilityTraceList.vue'
import CapabilityRuntimePanel from './components/CapabilityRuntimePanel.vue'
import CapabilityAlignmentCard from './components/CapabilityAlignmentCard.vue'
import CapabilityEfficiencyPanel from './components/CapabilityEfficiencyPanel.vue'
import CapabilityPlanPane from './components/CapabilityPlanPane.vue'
import CapabilityReplayPane from './components/CapabilityReplayPane.vue'
import CapabilityDiagnosticsPane from './components/CapabilityDiagnosticsPane.vue'
import CapabilityHeroSection from './components/CapabilityHeroSection.vue'
import CapabilityOverviewPane from './components/CapabilityOverviewPane.vue'
import TimelinePanel from './components/TimelinePanel.vue'
import FailedRunsPane from './components/FailedRunsPane.vue'
import FinalAnswerPane from './components/FinalAnswerPane.vue'
import TerminalLogPane from './components/TerminalLogPane.vue'
import AuthDialog from './components/AuthDialog.vue'
import RunRegistryPanel from './components/RunRegistryPanel.vue'
import ShortcutHelpDialog from './components/dialogs/ShortcutHelpDialog.vue'
import HitlFormDialog from './components/HitlFormDialog.vue'
import CommandPalette from './components/CommandPalette.vue'
import SpiderAssistant from './components/SpiderAssistant.vue'
import RunScreenshotHistory from './components/RunScreenshotHistory.vue'
import {
  createSlashCommandRegistry,
  registerBuiltinCommands,
  useSlashCommand,
} from './composables/useSlashCommand.js'
import { createTerminalLogBuffer } from './composables/useTerminalLog.js'
import { useModelSettings } from './composables/useModelSettings.js'
import { useWebSocket } from './composables/useWebSocket.js'
import { useKeyboardCommand } from './composables/useKeyboardCommand.js'
import { useCapabilityTrace } from './composables/useCapabilityTrace.js'
import { useTimelineReplay } from './composables/useTimelineReplay.js'
import { useCapabilityFixtureReplay } from './composables/useCapabilityFixtureReplay.js'
import { useBrowserRuntimeStatus } from './composables/useBrowserRuntimeStatus.js'
import { useCapabilityTraceExport } from './composables/useCapabilityTraceExport.js'
import { useHitlForm } from './composables/useHitlForm.js'
import { useScreenshotArtifacts } from './composables/useScreenshotArtifacts.js'
import { writeToClipboard } from './composables/useClipboard.js'
import { useAuthProfiles } from './composables/useAuthProfiles.js'
import { useFinalAnswer } from './composables/useFinalAnswer.js'
import { useBottomTabs } from './composables/useBottomTabs.js'
import { useRunEventRouter } from './composables/useRunEventRouter.js'
import {
  ATTACHMENT_INTENT_AUTO,
  ATTACHMENT_INTENT_OPTIONS,
  appendAttachmentIntentToFormData,
} from './composables/useAttachmentIntent.js'
import {
  ATTACHMENT_ACCEPT,
  appendConstraintsToFormData,
  authProfileOptionLabel,
  buildAuthoritativeUrlsPayload,
  buildTaskConstraints,
  validateTaskInput,
  fetchOutputContractPreview,
} from './composables/useTaskSubmit'
import { API_BASE, apiFetch } from './api/client.js'

const url = ref('')
const urlFieldExpanded = ref(false)
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
const terminalLogPaneRef = ref(null)

const {
  authDialogOpen,
  authProfileOptions,
  selectedAuthProfiles,
  captchaSolverEnabled,
  captchaSolverProvider,
  loadAuthProfiles,
  loadCaptchaSolverStatus,
  useAuthProfile,
} = useAuthProfiles({ appendLog })
const {
  selectedModel,
  selectedSemanticModel,
  modelTemperature,
  modelMaxTokens,
  modelBaseUrl,
  modelApiKey,
  semanticBaseUrl,
  semanticApiKey,
  vlmRemoteModels,
  vlmRemoteLoading,
  semanticRemoteModels,
  semanticRemoteLoading,
  fetchRemoteModels,
  loadModelSettings,
  selectedModelType,
} = useModelSettings()

const {
  isHumanInterventionRequired,
  humanInterventionReason,
  hitlFormVisible,
  hitlFormFields,
  hitlFormReason,
  hitlFormScreenshot,
  hitlFormLoading,
  hitlScreenshot,
  isBotChallengeHitl,
  resumeAgentExecution,
  submitHitlForm,
  skipHitlForm,
} = useHitlForm({ appendLog })
const {
  activeBottomTab,
  runsSubView,
  hasNewRuns,
  hasNewPhase,
  hasNewCapability,
  timelineAutoScroll,
  runHistoryRefreshToken,
  TAB_ORDER,
  setActiveBottomTab,
} = useBottomTabs({
  resolveExternalBadges: () => ({ hasNewArtifacts, hasNewFinalAnswer, timelinePanelRef }),
})
// C2: 高级配置抽屉 — 左栏只留任务输入，配置项收进抽屉
const settingsDrawerOpen = ref(false)
const settingsActivePanels = ref(['models', 'identity', 'constraints', 'file'])
const {
  currentImageBase64,
  screenshotHistory,
  pushScreenshotFrame,
  clearScreenshotStream,
  artifactList,
  hasNewArtifacts,
  fetchArtifacts,
  markArtifactsBaseline,
  artifactsGrewSinceSubmit,
} = useScreenshotArtifacts({
  appendLog,
  isArtifactsTabActive: () => activeBottomTab.value === 'artifacts',
})
const {
  fetchBrowserRuntimeStatus,
  browserRuntime,
  browserRuntimeCapacity,
  browserRuntimeBackendSummary,
  browserRuntimeStatusClass,
  browserRuntimeLabel,
  browserRuntimeHealthLabel,
  browserRuntimeHealthCacheLabel,
} = useBrowserRuntimeStatus({ appendLog })

// ── K3: Failed runs drawer ──
// failedRunsList: array of records returned by GET /api/failed_runs.
// Each record carries schema_version/run_id/ts/reason/goal/duration_s/
// step_count/paths/paths_exist (the last is added by list_failed_runs
// based on filesystem presence; we use it to grey out the HTML log
// button when the underlying file was deleted/rotated).

// ── M: Phase timeline ──
// phaseEvents: list of {type, phase, severity, message, step, duration_ms,
//   ts, ...extras} pushed from the WS phase channel (G2/H1/I/L source).
// Capped at 500 to keep the panel light; older events drop off the front.
// hasNewPhase: pulse the tab badge when an event lands while the user is
// on a different tab.
const phaseEvents = ref([])

// ── N: Phase event detail dialog ──
// Click a timeline chip → open a dialog with the full event payload
// (pretty-printed JSON + key fields summary).

// ── T: Keyboard shortcuts ─────────────────────────────────────────
// helpDialogVisible: toggled by Ctrl+/ — shows a cheat-sheet table.
// promptInputRef: bound to the prompt el-input via :ref so Ctrl+K can
//   programmatically focus the textarea even when it isn't visible yet.
const helpDialogVisible = ref(false)
const promptInputRef = ref(null)

const {
  replayMode,
  replaySourceName,
  triggerReplayImport,
  exitReplayMode,
  handleTimelineImportReplay,
} = useTimelineReplay({
  phaseEvents,
  hasNewCapability,
  setActiveBottomTab: (name) => setActiveBottomTab(name),
})
const timelinePanelRef = ref(null)
const failedRunsPaneRef = ref(null)
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


// ── Final Answer 面板状态 ──
// taskResult: 后端最终结果，结构 { type: 'text' | 'file', answer: string }
//   - null：未开始 / 已重置
//   - { type: 'text', answer }：纯文本答案，自动切到 Final Answer Tab 并渲染 Markdown
//   - { type: 'file', answer? }：结构化导出，自动切到 Artifacts Tab；
//                                 Final Answer 面板显示兜底文案
// finalAnswerStatus: 'idle' | 'pending' | 'text' | 'file'，驱动面板的 3 种 UI 状态
const {
  taskResult,
  finalAnswerStatus,
  finalAnswerText,
  finalAnswerDomain,
  hasNewFinalAnswer,
  finalAnswerHtml,
  applyDoneAnswer,
  resetForNewRun,
} = useFinalAnswer({ activeBottomTab, artifactsGrewSinceSubmit })

// ── Slash command system ──
const slashRegistry = createSlashCommandRegistry()
const {
  paletteVisible: cmdPaletteVisible,
  suggestions: cmdSuggestions,
  updateSuggestions: updateCmdSuggestions,
  tryExecute: tryExecuteCmd,
  dismiss: dismissCmdPalette,
} = useSlashCommand(slashRegistry)
const cmdPaletteRef = ref(null)

const finalAnswerCopyState = ref('idle') // 'idle' | 'ok' | 'err'
let finalAnswerCopyTimer = null // F1: reset-to-idle debounce; cleared on unmount

// appendLog now comes from createTerminalLogBuffer (see top of setup):
// synchronous push into a plain buffer, batched flush per frame.

// ── R: Live Terminal log line severity coloring ──────────────────────
// Inspect the log line's leading "[TAG]" and return a CSS modifier class.
// Only the first 32 chars are scanned (prefixes are short; a value containing
// "[ERROR" later in the message must NOT recolor a benign line).
// Matches both legacy tags ("[ERROR]", "[WARN]") and the G2/H1/I phase
// tags ("[PHASE]", "[PHASE/WARN]", "[PHASE/ERR]").


const scrollToBottom = () => terminalLogPaneRef.value?.scrollToBottom()

const { handleSocketMessage } = useRunEventRouter({
  appendLog,
  pushScreenshotFrame,
  isRunning,
  isHumanInterventionRequired,
  humanInterventionReason,
  hitlScreenshot,
  hitlFormFields,
  hitlFormReason,
  hitlFormScreenshot,
  hitlFormLoading,
  hitlFormVisible,
  fetchBrowserRuntimeStatus,
  runHistoryRefreshToken,
  activeBottomTab,
  hasNewRuns,
  hasNewCapability,
  hasNewPhase,
  timelineAutoScroll,
  failedRunsPaneRef,
  applyDoneAnswer,
  replayMode,
  phaseEvents,
  fetchArtifacts,
  hasNewArtifacts,
  currentImageBase64,
  timelinePanelRef,
})
const {
  status: wsStatus,
  connect: connectWebSocket,
  disconnect: disconnectWebSocket,
} = useWebSocket({
  onOpen: () => appendLog('[SYSTEM] WebSocket connected'),
  onMessage: handleSocketMessage,
  onClose: () => appendLog('[SYSTEM] WebSocket disconnected'),
  onError: () => appendLog('[ERROR] WebSocket error'),
})

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

const capabilityTrace = useCapabilityTrace(phaseEvents)
const {
  latestCapabilityRoute,
  latestCapabilityExecute,
  capabilityTraceEvents,
  capabilityIntent,
  capabilityBackendPlan,
  capabilityFallbackChain,
  capabilityExecutionPlanSteps,
  capabilityWorkflowGraph,
  capabilityRuntimePreflight,
  capabilityRouteCrawlEfficiencyPlan,
  capabilityWorkflowNodes,
  capabilityActionRefSchema,
  capabilityManifestSummary,
  capabilityAuditFindings,
  capabilityTraceJson,
  capabilityExecutionFailureBundle,
  capabilityExecutionEfficiencyCorrelationReport,
  capabilityExecuteJson,
  capabilityTraceRows,
  capabilityTraceSummary,
  capabilityFilteredTraceRows,
  capabilityExecutionAlignment,
  capabilityTraceHealth,
  capabilityRuntimePreflightClass,
  capabilityRuntimePreflightLabel,
  capabilityRoleRows,
  capabilityTraceFilter,
  capabilityTraceSearchQuery,
} = capabilityTrace
const {
  capabilityReplayPaneProps,
  copyCapabilityFailureFixtureBatchReplaySummary,
  generateCapabilityFailureFixture,
  replayCapabilityFailureFixture,
  replayCapabilityEfficiencyFeedback,
  fetchCapabilityEfficiencyFeedbackReplays,
  fetchCapabilityFailureFixtures,
  fetchCapabilityFailureFixtureBatchHistory,
  batchReplayCapabilityFailureFixtures,
} = useCapabilityFixtureReplay({
  url,
  prompt,
  fetchArtifacts: () => fetchArtifacts(),
  writeToClipboard,
  latestCapabilityExecute,
  capabilityExecutionFailureBundle,
  capabilityExecutionEfficiencyCorrelationReport,
})
const clearPhaseEvents = () => {
  phaseEvents.value = []
  hasNewPhase.value = false
  hasNewCapability.value = false
  // P: After a clear, there's nothing to scroll past — re-pin to bottom.
  timelineAutoScroll.value = true
}

// ── Q: capability trace 导出/摘要/复制（→ useCapabilityTraceExport）─────
// Honors the active filter (severity/phase exclude) so the user gets
// exactly what they see. Strip the synthetic _ts field (added by M for
// internal use) so the file contains only over-the-wire payloads.
const {
  exportCapabilityTraceAsJsonl,
  copyCapabilityTraceSummary,
} = useCapabilityTraceExport({
  trace: capabilityTrace,
  writeToClipboard,
})

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

const keyboardActions = {
  submitIfIdle: () => { if (!isRunning.value) submitTask() },
  terminalSearchOpen: () => terminalLogPaneRef.value?.openTerminalSearch(),
  toggleHelp: () => { helpDialogVisible.value = !helpDialogVisible.value },
  focusPrompt: () => focusPromptInput(),
  selectTab: (i) => setActiveBottomTab(TAB_ORDER[i]),
  phasePrev: () => timelinePanelRef.value?.goToPrevPhaseEvent(),
  phaseNext: () => timelinePanelRef.value?.goToNextPhaseEvent(),
  failedPrev: () => failedRunsPaneRef.value?.goToPrevFailedRun(),
  failedNext: () => failedRunsPaneRef.value?.goToNextFailedRun(),
  terminalSearchClose: () => terminalLogPaneRef.value?.closeTerminalSearch(),
  terminalSearchPrev: () => terminalLogPaneRef.value?.terminalSearchPrev(),
  terminalSearchNext: () => terminalLogPaneRef.value?.terminalSearchNext(),
  exportTimeline: () => timelinePanelRef.value?.exportPhaseEventsAsJsonl(),
  timelineBottom: () => timelinePanelRef.value?.scrollToBottom(),
  timelineTop: () => timelinePanelRef.value?.scrollToTop(),
  exportCapability: () => exportCapabilityTraceAsJsonl(),
}

const getKeyboardContext = () => ({
  activeTab: activeBottomTab.value,
  tabCount: TAB_ORDER.length,
  phaseDialogOpen: !!timelinePanelRef.value?.phaseDialogVisible,
  failedDialogOpen: !!failedRunsPaneRef.value?.failedRunDialogVisible,
  terminalSearchVisible: !!terminalLogPaneRef.value?.searchVisible,
})

useKeyboardCommand({ getContext: getKeyboardContext, actions: keyboardActions })

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

// ── Slash command input handlers ──
const onPromptInput = (val) => {
  const firstLine = (typeof val === 'string' ? val : prompt.value).split('\n')[0]
  updateCmdSuggestions(firstLine)
}
const onPromptKeydown = (e) => {
  if (cmdPaletteVisible.value) {
    cmdPaletteRef.value?.onKeydown(e)
  }
}
const handleCmdSelect = (cmd) => {
  prompt.value = '/' + cmd.name + (cmd.args ? ' ' : '')
  dismissCmdPalette()
  nextTick(() => {
    promptInputRef.value?.focus()
  })
}
const trySlashBeforeSubmit = () => {
  const firstLine = prompt.value.trim().split('\n')[0]
  if (firstLine.startsWith('/')) {
    const executed = tryExecuteCmd(firstLine)
    if (executed) {
      prompt.value = ''
      return true
    }
  }
  return false
}

const submitTask = async () => {
  if (trySlashBeforeSubmit()) return
  const validation = validateTaskInput({ prompt: prompt.value })
  if (!validation.ok) {
    ElMessage.warning(validation.message)
    return
  }

  isRunning.value = true
  clearTerminalLogs()
  clearScreenshotStream()
  // M: clear timeline buffer at the start of every new run so phases
  // from old runs don't bleed into the new timeline view.
  phaseEvents.value = []
  hasNewPhase.value = false
  hasNewCapability.value = false
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
  resetForNewRun()
  finalAnswerCopyState.value = 'idle' // F1: 复位 copy 反馈
  markArtifactsBaseline()
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
  loadModelSettings()
  registerBuiltinCommands(slashRegistry, {
    selectedModel,
    selectedSemanticModel,
    modelApiKey,
    modelBaseUrl,
    semanticBaseUrl,
    semanticApiKey,
    modelTemperature,
    modelMaxTokens,
    proxyServer,
    resumeEnabled,
    batchMaxRuns,
    settingsDrawerOpen,
    settingsActivePanels,
    authDialogOpen,
    helpDialogVisible,
    targetUrl: url,
    forceStop,
    showMessage: (type, msg) => ElMessage[type]?.(msg),
  })
  connectWebSocket()
  loadAuthProfiles()
  loadCaptchaSolverStatus()
  fetchArtifacts()
  fetchBrowserRuntimeStatus()
  // K3: seed the failed-runs drawer with historic records so the
  // tab is informative even before the user runs anything in this session.
  failedRunsPaneRef.value?.fetchFailedRuns()
})

onUnmounted(() => {
  disconnectWebSocket()
  // S: cancel any pending Timeline chip single-click dialog-open so the
  // callback doesn't fire after the component is gone (would touch
  // selectedPhaseEvent/phaseDialogVisible refs and crash on detached state).
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

</script>

<template>
  <main class="app-shell">
    <div v-if="isRunning" class="global-progress-bar" />
    <section class="control-panel vspider-panel">
      <div class="control-scroll">
        <div class="brand-row">
          <span class="brand-text">VSpider</span>
          <span v-if="!urlFieldExpanded && !url" class="url-toggle" @click="urlFieldExpanded = true">+ URL</span>
        </div>

        <div v-if="urlFieldExpanded || url" class="url-field-collapsible">
          <el-input
            v-model="url"
            clearable
            size="small"
            :disabled="isRunning"
            placeholder="目标 URL（可选）"
            @blur="urlFieldExpanded = false"
          />
        </div>

        <div class="prompt-input-wrap">
          <CommandPalette
            ref="cmdPaletteRef"
            :visible="cmdPaletteVisible"
            :suggestions="cmdSuggestions"
            @select="handleCmdSelect"
            @dismiss="dismissCmdPalette"
          />
          <el-input
            ref="promptInputRef"
            v-model="prompt"
            type="textarea"
            resize="none"
            :autosize="{ minRows: 3, maxRows: 8 }"
            :disabled="isRunning"
            placeholder="描述任务… (Ctrl+Enter 提交，/ 命令)"
            @input="onPromptInput"
            @keydown="onPromptKeydown"
            @blur="() => setTimeout(dismissCmdPalette, 150)"
          />
        </div>

        <div class="action-bar">
          <div v-if="outputContractPreviewLoading || outputContractPreview" class="output-contract-inline">
            <span v-if="outputContractPreviewLoading" class="output-contract-inline__loading">…</span>
            <template v-else-if="outputContractPreview">
              <el-tag size="small" type="info">{{ outputContractPreview.kind_label }}</el-tag>
              <span class="output-contract-inline__arrow">→</span>
              <el-tag size="small">{{ outputContractPreview.container_label }}</el-tag>
            </template>
          </div>
          <div class="action-buttons">
            <el-button
              type="primary"
              class="run-button"
              :icon="VideoPlay"
              :loading="isRunning"
              @click="submitTask"
            >
              执行
            </el-button>
            <el-button
              v-if="isRunning"
              class="stop-button"
              :icon="Close"
              @click="forceStop"
            >
              停止
            </el-button>
            <button type="button" class="settings-toggle" @click="settingsDrawerOpen = true" title="配置">
              <el-icon><Setting /></el-icon>
            </button>
          </div>
        </div>
      </div>

      <el-drawer
        v-model="settingsDrawerOpen"
        title="配置"
        direction="rtl"
        size="380px"
        class="settings-drawer"
      >
          <el-collapse v-model="settingsActivePanels" class="advanced-collapse drawer-collapse">
          <el-collapse-item name="models">
            <template #title>
              <span>模型</span>
              <span class="collapse-title-echo">{{ selectedModel }} · {{ selectedSemanticModel }}</span>
            </template>
            <div class="model-section">
              <label>VLM (视觉模型)</label>
              <el-input v-model="modelBaseUrl" clearable :disabled="isRunning" placeholder="Base URL (如 https://dashscope.aliyuncs.com/compatible-mode/v1)" size="small" />
              <div class="model-connect-row">
                <el-input v-model="modelApiKey" clearable show-password :disabled="isRunning" placeholder="API Key" size="small" />
                <el-button size="small" :loading="vlmRemoteLoading" @click="fetchRemoteModels(modelBaseUrl, modelApiKey, vlmRemoteModels, vlmRemoteLoading)">连接</el-button>
              </div>
              <el-select v-model="selectedModel" :disabled="isRunning" filterable allow-create default-first-option class="full-width" placeholder="选择模型">
                <el-option-group v-if="vlmRemoteModels.length" :label="`远程 (${vlmRemoteModels.length})`">
                  <el-option v-for="m in vlmRemoteModels" :key="m" :value="m" :label="m" />
                </el-option-group>
                <el-option-group label="预设">
                  <el-option value="backend-default" label="Backend Default" />
                  <el-option value="qwen3-vl-plus" label="Qwen-VL-Plus" />
                  <el-option value="local-74b-vl" label="内网 74B VL" />
                  <el-option value="deepseek-chat" label="DeepSeek-V3" />
                  <el-option value="deepseek-reasoner" label="DeepSeek-R1" />
                  <el-option value="deepseek-v4-flash" label="DeepSeek-V4-Flash" />
                  <el-option value="deepseek-v4-pro" label="DeepSeek-V4-Pro" />
                </el-option-group>
              </el-select>
              <p v-if="selectedModelType === 'text'" class="model-warning">纯文本模型，将剥离图像仅用 AX Tree。</p>
            </div>
            <div class="model-section">
              <label>Semantic (语义模型)</label>
              <el-input v-model="semanticBaseUrl" clearable :disabled="isRunning" placeholder="Base URL (如 https://api.deepseek.com)" size="small" />
              <div class="model-connect-row">
                <el-input v-model="semanticApiKey" clearable show-password :disabled="isRunning" placeholder="API Key" size="small" />
                <el-button size="small" :loading="semanticRemoteLoading" @click="fetchRemoteModels(semanticBaseUrl, semanticApiKey, semanticRemoteModels, semanticRemoteLoading)">连接</el-button>
              </div>
              <el-select v-model="selectedSemanticModel" :disabled="isRunning" filterable allow-create default-first-option class="full-width" placeholder="选择模型">
                <el-option-group v-if="semanticRemoteModels.length" :label="`远程 (${semanticRemoteModels.length})`">
                  <el-option v-for="m in semanticRemoteModels" :key="m" :value="m" :label="m" />
                </el-option-group>
                <el-option-group label="预设">
                  <el-option value="backend-default" label="Backend Default" />
                  <el-option value="deepseek-chat" label="DeepSeek-V3" />
                  <el-option value="deepseek-reasoner" label="DeepSeek-R1" />
                  <el-option value="deepseek-v4-flash" label="DeepSeek-V4-Flash" />
                  <el-option value="deepseek-v4-pro" label="DeepSeek-V4-Pro" />
                  <el-option value="qwen3-vl-plus" label="Qwen-VL-Plus" />
                  <el-option value="local-74b-vl" label="Local 74B VL" />
                </el-option-group>
              </el-select>
            </div>
            <el-collapse class="model-advanced-fold">
              <el-collapse-item title="高级参数" name="adv">
                <label>Temperature: {{ modelTemperature }}</label>
                <el-slider v-model="modelTemperature" :min="0" :max="2" :step="0.1" :disabled="isRunning" />
                <label>Max Tokens</label>
                <el-input-number v-model="modelMaxTokens" :min="512" :max="32768" :step="512" :disabled="isRunning" class="full-width" />
              </el-collapse-item>
            </el-collapse>
          </el-collapse-item>
          <el-collapse-item name="identity">
            <template #title>
              <span>登录</span>
              <span class="collapse-title-echo">{{ selectedAuthProfiles.length ? selectedAuthProfiles.join('、') : '可选' }}</span>
            </template>
            <div class="field-title-row">
              <el-button text size="small" :icon="Refresh" @click="loadAuthProfiles">刷新</el-button>
              <el-button text size="small" :icon="Setting" @click="authDialogOpen = true">管理</el-button>
            </div>
            <el-select v-model="selectedAuthProfiles" multiple filterable allow-create collapse-tags collapse-tags-tooltip :disabled="isRunning" placeholder="Auth Profile" class="full-width">
              <el-option v-for="item in authProfileOptions" :key="item.name" :label="authProfileOptionLabel(item)" :value="item.name" />
            </el-select>
          </el-collapse-item>
          <el-collapse-item title="运行约束" name="constraints">
            <label>附加 URL</label>
            <el-input v-model="extraUrls" type="textarea" :rows="2" :disabled="isRunning" placeholder="每行一个 URL" class="full-width" />
            <label>代理</label>
            <el-input v-model="proxyServer" clearable :disabled="isRunning" placeholder="http://127.0.0.1:7890" class="full-width" />
            <div v-if="proxyServer" class="proxy-auth-row">
              <el-input v-model="proxyUsername" clearable :disabled="isRunning" placeholder="用户名" />
              <el-input v-model="proxyPassword" clearable show-password :disabled="isRunning" placeholder="密码" />
            </div>
            <div class="constraint-inline-row">
              <span>并发</span>
              <el-input-number v-model="batchMaxRuns" :min="0" :max="16" :disabled="isRunning" size="small" />
              <span>续跑</span>
              <el-switch v-model="resumeEnabled" :disabled="isRunning" />
            </div>
            <p v-if="captchaSolverEnabled" class="file-status solver-on">Captcha Solver: {{ captchaSolverProvider || 'auto' }}</p>
          </el-collapse-item>
          <el-collapse-item title="附件" name="file">
            <el-upload drag class="compact-upload" :auto-upload="false" :limit="1" :disabled="isRunning" :accept="ATTACHMENT_ACCEPT" :on-change="handleUploadChange" :on-remove="handleUploadRemove">
              <el-icon class="upload-icon"><UploadFilled /></el-icon>
              <div class="upload-copy">拖拽或点击选择文件</div>
            </el-upload>
            <p class="file-status">{{ selectedFile ? selectedFile.name : '未选择文件' }}</p>
            <template v-if="selectedFile">
              <el-select v-model="attachmentIntent" :disabled="isRunning" size="small" class="attachment-intent-select" placeholder="附件用途">
                <el-option v-for="option in ATTACHMENT_INTENT_OPTIONS" :key="option.value" :label="option.label" :value="option.value" />
              </el-select>
            </template>
          </el-collapse-item>
          </el-collapse>
      </el-drawer>
    </section>

    <section class="monitor-panel">
      <div class="terminal-panel vspider-panel terminal-panel--full">
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
              if (timelineAutoScroll) timelinePanelRef.value?.scrollToBottom()
            }
          }"
        >
          <el-tab-pane name="terminal">
            <template #label>
              <span>实时日志</span>
            </template>
            <TerminalLogPane
              ref="terminalLogPaneRef"
              :logs="logs"
              :logs-trimmed-count="logsTrimmedCount"
              :ws-status="wsStatus"
              :is-running="isRunning"
            />
          </el-tab-pane>

          <el-tab-pane name="timeline">
            <template #label>
              <el-badge :is-dot="hasNewPhase" class="artifact-badge">
                <span>时间线</span>
              </el-badge>
            </template>
            <TimelinePanel
              ref="timelinePanelRef"
              :phase-events="phaseEvents"
              :replay-mode="replayMode"
              :replay-source-name="replaySourceName"
              @clear="clearPhaseEvents"
              @import-replay="handleTimelineImportReplay"
              @exit-replay="exitReplayMode"
            />
          </el-tab-pane>

          <el-tab-pane name="capability">
            <template #label>
              <el-badge :is-dot="hasNewCapability" class="artifact-badge">
                <span>能力追踪</span>
              </el-badge>
            </template>
            <el-scrollbar class="capability-scroll">
              <div v-if="latestCapabilityRoute || latestCapabilityExecute" class="capability-panel">
                <CapabilityHeroSection
                  :intent="capabilityIntent"
                  :event-count="capabilityTraceEvents.length"
                  :export-disabled="!capabilityTraceEvents.length"
                  :replay-mode="replayMode"
                  :replay-source-name="replaySourceName"
                  :batch-replay-loading="capabilityFailureFixtureBatchReplayLoading"
                  :failure-bundle-valid="capabilityExecutionFailureBundle.version === 'capability_execute_failure_bundle.v1'"
                  :correlation-report-valid="capabilityExecutionEfficiencyCorrelationReport.version === 'efficiency_correlation_report.v1'"
                  @export-jsonl="exportCapabilityTraceAsJsonl"
                  @more-action="handleCapabilityMoreAction"
                  @exit-replay="exitReplayMode"
                />

                <CapabilityOverviewPane
                  v-model:trace-filter="capabilityTraceFilter"
                  v-model:trace-search-query="capabilityTraceSearchQuery"
                  :trace-rows="capabilityTraceRows"
                  :filtered-trace-rows="capabilityFilteredTraceRows"
                  :trace-summary="capabilityTraceSummary"
                  :trace-health="capabilityTraceHealth"
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
                  :has-execute-event="Boolean(latestCapabilityExecute)"
                  :execute-event="latestCapabilityExecute"
                  :execution-alignment="capabilityExecutionAlignment"
                  :route-crawl-efficiency-plan="capabilityRouteCrawlEfficiencyPlan"
                  @open-row="(evt) => timelinePanelRef.value?.openPhaseDialog(evt)"
                />
                <el-collapse class="capability-fold">
                  <el-collapse-item title="计划 / 工作流" name="plan">
                    <CapabilityPlanPane
                      :plan-steps="capabilityExecutionPlanSteps"
                      :workflow-graph="capabilityWorkflowGraph"
                      :workflow-nodes="capabilityWorkflowNodes"
                      :action-ref-schema="capabilityActionRefSchema"
                      :backend-plan="capabilityBackendPlan"
                    />
                  </el-collapse-item>
                  <el-collapse-item title="回放与 Fixture" name="replay">
                    <CapabilityReplayPane
                      v-bind="capabilityReplayPaneProps"
                      @copy-batch-summary="copyCapabilityFailureFixtureBatchReplaySummary"
                    />
                  </el-collapse-item>
                  <el-collapse-item title="诊断" name="diagnostics">
                    <CapabilityDiagnosticsPane
                      :manifest-summary="capabilityManifestSummary"
                      :fallback-chain="capabilityFallbackChain"
                      :role-rows="capabilityRoleRows"
                      :audit-findings="capabilityAuditFindings"
                      :raw-json="capabilityTraceJson || capabilityExecuteJson"
                    />
                  </el-collapse-item>
                </el-collapse>
              </div>
              <div v-else class="capability-skeleton">
                <p class="skeleton-hint">启动任务后显示能力追踪</p>
              </div>
            </el-scrollbar>
          </el-tab-pane>

          <el-tab-pane name="final">
            <template #label>
              <el-badge :is-dot="hasNewFinalAnswer" class="artifact-badge">
                <span>最终答案</span>
              </el-badge>
            </template>
            <FinalAnswerPane
              :status="finalAnswerStatus"
              :text="finalAnswerText"
              :html="finalAnswerHtml"
              :domain="finalAnswerDomain"
              @jump-to-artifacts="activeBottomTab = 'artifacts'"
            />
          </el-tab-pane>

          <el-tab-pane name="artifacts">
            <template #label>
              <el-badge :is-dot="hasNewArtifacts" class="artifact-badge">
                <span>产物</span>
              </el-badge>
            </template>
            <el-table
              :data="artifactList"
              height="220"
              class="artifact-table"
              header-cell-class-name="dark-table-header"
              empty-text="暂无产物"
            >
              <el-table-column prop="name" label="文件" show-overflow-tooltip />
              <el-table-column prop="size_kb" label="KB" width="64" />
              <el-table-column label="" width="72">
                <template #header>
                  <el-button text size="small" :icon="Refresh" @click="fetchArtifacts" />
                </template>
                <template #default="scope">
                  <a :href="`${API_BASE}${scope.row.url}`" download class="download-link">下载</a>
                </template>
              </el-table-column>
            </el-table>
          </el-tab-pane>

          <el-tab-pane name="runs">
            <template #label>
              <el-badge :is-dot="hasNewRuns" class="artifact-badge">
                <span>运行记录</span>
              </el-badge>
            </template>
            <RunRegistryPanel
              v-show="runsSubView === 'all'"
              :refresh-token="runHistoryRefreshToken"
              @loaded="() => { if (activeBottomTab === 'runs') hasNewRuns = false }"
              @open-detail="hasNewRuns = false"
            >
              <template #toolbar-extra>
                <el-segmented v-model="runsSubView" :options="[{label:'全部',value:'all'},{label:'失败',value:'failed'}]" size="small" />
              </template>
            </RunRegistryPanel>
            <FailedRunsPane
              v-show="runsSubView === 'failed'"
              ref="failedRunsPaneRef"
            >
              <template #toolbar-extra>
                <el-segmented v-model="runsSubView" :options="[{label:'全部',value:'all'},{label:'失败',value:'failed'}]" size="small" />
              </template>
            </FailedRunsPane>
          </el-tab-pane>
        </el-tabs>
      </div>
    </section>

    <AuthDialog
      v-model="authDialogOpen"
      :profile-options="authProfileOptions"
      :fallback-url="url"
      @use-profile="useAuthProfile"
      @reload-profiles="loadAuthProfiles"
      @log="appendLog"
    />

    <!-- T: Keyboard shortcuts cheat-sheet (Ctrl+/) ───────────────────── -->
    <ShortcutHelpDialog v-model:visible="helpDialogVisible" />

    <!-- HITL Form Dialog: CDP proxy input for form filling -->
    <HitlFormDialog
      v-model:visible="hitlFormVisible"
      :reason="hitlFormReason"
      :fields="hitlFormFields"
      :screenshot-url="hitlFormScreenshot"
      :loading="hitlFormLoading"
      @submit="submitHitlForm"
      @skip="skipHitlForm"
    />

    <SpiderAssistant :running="isRunning">
      <div v-if="isHumanInterventionRequired" class="sp-hitl-notice">
        <p>{{ isBotChallengeHitl ? '人机验证 — 请在浏览器完成验证' : '需要人工介入' }}</p>
        <el-button type="success" size="small" @click="resumeAgentExecution">恢复执行</el-button>
      </div>
      <RunScreenshotHistory :frames="screenshotHistory" :running="isRunning" />
    </SpiderAssistant>
  </main>
</template>

<style src="./styles/app.css" scoped></style>
