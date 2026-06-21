// Behavior lock for composables/useTaskForm.js (方向D · 从 App.vue 抽离)
//
// Pins the task input form + submit chain previously inline in App.vue:
//   - upload change/remove; slash glue (trySlashBeforeSubmit / onPromptInput)
//   - submitTask: validation gate, run reset (terminal/screenshot/phase/final/replay),
//     /api/start_batch POST, success toast; error path flips isRunning back off
//   - forceStop: /api/stop_batch POST
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { ref } from 'vue'

vi.mock('../src/api/client.js', () => ({ apiFetch: vi.fn() }))
vi.mock('element-plus', () => ({
  ElMessage: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() },
}))
vi.mock('../src/composables/useAttachmentIntent.js', () => ({
  ATTACHMENT_INTENT_AUTO: 'auto',
  appendAttachmentIntentToFormData: vi.fn(),
}))
vi.mock('../src/composables/useTaskSubmit', () => ({
  appendConstraintsToFormData: vi.fn(),
  buildAuthoritativeUrlsPayload: vi.fn(() => ['u1']),
  buildTaskConstraints: vi.fn(() => ({})),
  validateTaskInput: vi.fn(() => ({ ok: true })),
}))

import { useTaskForm } from '../src/composables/useTaskForm.js'
import { apiFetch } from '../src/api/client.js'
import { ElMessage } from 'element-plus'
import { validateTaskInput } from '../src/composables/useTaskSubmit'

function jsonResponse (body, ok = true) {
  return { ok, json: async () => body }
}

function mk (over = {}) {
  const deps = {
    appendLog: vi.fn(async () => {}),
    clearTerminalLogs: vi.fn(),
    clearScreenshotStream: vi.fn(),
    markArtifactsBaseline: vi.fn(),
    fetchBrowserRuntimeStatus: vi.fn(),
    resetForNewRun: vi.fn(),
    finalAnswerCopyState: ref('ok'),
    phaseEvents: ref([{ phase: 'old' }]),
    hasNewPhase: ref(true),
    hasNewCapability: ref(true),
    timelineAutoScroll: ref(false),
    replayMode: ref(true),
    replaySourceName: ref('src.jsonl'),
    selectedAuthProfiles: ref([]),
    selectedModel: ref('backend-default'),
    selectedSemanticModel: ref('backend-default'),
    selectedModelType: ref('vlm'),
    modelTemperature: ref(0.7),
    modelMaxTokens: ref(1000),
    modelBaseUrl: ref(''),
    modelApiKey: ref(''),
    semanticBaseUrl: ref(''),
    semanticApiKey: ref(''),
    updateCmdSuggestions: vi.fn(),
    cmdPaletteVisible: ref(false),
    cmdPaletteRef: ref(null),
    dismissCmdPalette: vi.fn(),
    promptInputRef: ref(null),
    tryExecuteCmd: vi.fn(() => false),
    ...over,
  }
  const f = useTaskForm(deps)
  return { f, deps }
}

beforeEach(() => vi.clearAllMocks())

describe('useTaskForm upload', () => {
  it('handleUploadChange stores raw file + trims to last when multiple', () => {
    const { f } = mk()
    const files = [{}, {}]
    f.handleUploadChange({ raw: 'FILE' }, files)
    expect(f.selectedFile.value).toBe('FILE')
    expect(files).toHaveLength(1)
  })

  it('handleUploadChange with no raw clears file', () => {
    const { f } = mk()
    f.selectedFile.value = 'x'
    f.handleUploadChange(null, [])
    expect(f.selectedFile.value).toBe(null)
  })

  it('handleUploadRemove clears file', () => {
    const { f } = mk()
    f.selectedFile.value = 'x'
    f.handleUploadRemove()
    expect(f.selectedFile.value).toBe(null)
  })
})

describe('useTaskForm slash glue', () => {
  it('onPromptInput forwards the first line to updateCmdSuggestions', () => {
    const { f, deps } = mk()
    f.onPromptInput('/run now\nsecond')
    expect(deps.updateCmdSuggestions).toHaveBeenCalledWith('/run now')
  })

  it('trySlashBeforeSubmit executes a slash command and clears prompt', () => {
    const { f, deps } = mk({ tryExecuteCmd: vi.fn(() => true) })
    f.prompt.value = '/help'
    expect(f.trySlashBeforeSubmit()).toBe(true)
    expect(deps.tryExecuteCmd).toHaveBeenCalledWith('/help')
    expect(f.prompt.value).toBe('')
  })

  it('trySlashBeforeSubmit returns false for a normal prompt', () => {
    const { f } = mk()
    f.prompt.value = 'crawl this'
    expect(f.trySlashBeforeSubmit()).toBe(false)
  })
})

describe('useTaskForm.submitTask', () => {
  it('aborts with a warning when validation fails', async () => {
    validateTaskInput.mockReturnValueOnce({ ok: false, message: 'empty' })
    const { f } = mk()
    await f.submitTask()
    expect(ElMessage.warning).toHaveBeenCalledWith('empty')
    expect(apiFetch).not.toHaveBeenCalled()
  })

  it('happy path resets run state, exits replay, POSTs start_batch', async () => {
    apiFetch.mockResolvedValueOnce(jsonResponse({ status: 'success' }))
    const { f, deps } = mk()
    f.prompt.value = 'do it'
    await f.submitTask()
    expect(deps.clearTerminalLogs).toHaveBeenCalled()
    expect(deps.clearScreenshotStream).toHaveBeenCalled()
    expect(deps.resetForNewRun).toHaveBeenCalled()
    expect(deps.phaseEvents.value).toEqual([])
    expect(deps.replayMode.value).toBe(false)
    expect(deps.replaySourceName.value).toBe('')
    expect(deps.timelineAutoScroll.value).toBe(true)
    expect(apiFetch).toHaveBeenCalledWith('/api/start_batch', expect.objectContaining({ method: 'POST' }))
    expect(ElMessage.success).toHaveBeenCalled()
    expect(f.isRunning.value).toBe(true)
  })

  it('error path flips isRunning back off and toasts', async () => {
    apiFetch.mockResolvedValueOnce(jsonResponse({ status: 'error', message: 'boom' }, false))
    const { f } = mk()
    f.prompt.value = 'do it'
    await f.submitTask()
    expect(f.isRunning.value).toBe(false)
    expect(ElMessage.error).toHaveBeenCalled()
  })
})

describe('useTaskForm.forceStop', () => {
  it('POSTs stop_batch and flips isRunning off', async () => {
    apiFetch.mockResolvedValueOnce(jsonResponse({ status: 'success' }))
    const { f } = mk()
    f.isRunning.value = true
    await f.forceStop()
    expect(apiFetch).toHaveBeenCalledWith('/api/stop_batch', expect.objectContaining({ method: 'POST' }))
    expect(f.isRunning.value).toBe(false)
    expect(ElMessage.warning).toHaveBeenCalled()
  })

  it('error path toasts an error', async () => {
    apiFetch.mockResolvedValueOnce(jsonResponse({ status: 'error', message: 'nope' }, false))
    const { f } = mk()
    await f.forceStop()
    expect(ElMessage.error).toHaveBeenCalled()
  })
})
