// 方向D · 从 App.vue 抽离：任务输入表单状态 + 提交链路 + slash 胶水。
//
// 持有任务输入态（url/prompt/extraUrls/proxy*/batchMaxRuns/resume/file/intent/
// isRunning + settings drawer 开关）+ 上传处理 + slash 命令输入胶水 + submitTask /
// forceStop。所有跨子系统依赖（日志、终端清屏、截图基线、phase/timeline 复位、final
// answer 复位、模型设置、auth profiles、browser-runtime、slash registry）经 deps 注入。
// 注意 output-contract preview（outputContractPreview* + refresh + watch + 其 timer）
// 仍留在 App.vue（与 onUnmounted 清理同处）。行为与原内联逐字一致。
import { ref, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { apiFetch } from '../api/client.js'
import {
  ATTACHMENT_INTENT_AUTO,
  appendAttachmentIntentToFormData,
} from './useAttachmentIntent.js'
import {
  appendConstraintsToFormData,
  buildAuthoritativeUrlsPayload,
  buildTaskConstraints,
  validateTaskInput,
} from './useTaskSubmit'

export function useTaskForm (deps) {
  const {
    appendLog,
    clearTerminalLogs,
    clearScreenshotStream,
    markArtifactsBaseline,
    fetchBrowserRuntimeStatus,
    resetForNewRun,
    finalAnswerCopyState,
    phaseEvents,
    hasNewPhase,
    hasNewCapability,
    timelineAutoScroll,
    replayMode,
    replaySourceName,
    selectedAuthProfiles,
    selectedModel,
    selectedSemanticModel,
    selectedModelType,
    modelTemperature,
    modelMaxTokens,
    modelBaseUrl,
    modelApiKey,
    semanticBaseUrl,
    semanticApiKey,
    updateCmdSuggestions,
    cmdPaletteVisible,
    cmdPaletteRef,
    dismissCmdPalette,
    promptInputRef,
    tryExecuteCmd,
  } = deps

  const url = ref('')
  const urlFieldExpanded = ref(false)
  const prompt = ref('')
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
  // C2: 高级配置抽屉 — 左栏只留任务输入，配置项收进抽屉
  const settingsDrawerOpen = ref(false)
  const settingsActivePanels = ref(['models', 'identity', 'constraints', 'file'])

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

  return {
    url,
    urlFieldExpanded,
    prompt,
    extraUrls,
    proxyServer,
    proxyUsername,
    proxyPassword,
    batchMaxRuns,
    resumeEnabled,
    selectedFile,
    attachmentIntent,
    isRunning,
    settingsDrawerOpen,
    settingsActivePanels,
    handleUploadChange,
    handleUploadRemove,
    onPromptInput,
    onPromptKeydown,
    handleCmdSelect,
    trySlashBeforeSubmit,
    submitTask,
    forceStop,
  }
}
