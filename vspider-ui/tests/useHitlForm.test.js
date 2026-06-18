// Behavior lock for composables/useHitlForm.js (方向D · 从 App.vue 抽离)
//
// Pins the human-in-the-loop intervention/form subsystem:
//   - default state + isBotChallengeHitl reacts to humanInterventionReason
//   - resumeAgentExecution happy clears intervention + toasts; error path toasts
//   - submitHitlForm happy hides form + toasts field count; error keeps form usable
//   - skipHitlForm just hides the form and logs (no network)
import { describe, it, expect, beforeEach, vi } from 'vitest'

vi.mock('../src/api/client.js', () => ({ apiFetch: vi.fn() }))
vi.mock('element-plus', () => ({
  ElMessage: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() },
}))
vi.mock('../src/composables/useTaskSubmit', () => ({
  isBotChallengeReason: (r) => r === 'captcha',
}))

import { useHitlForm } from '../src/composables/useHitlForm.js'
import { apiFetch } from '../src/api/client.js'
import { ElMessage } from 'element-plus'

function jsonResponse (body, ok = true) {
  return { ok, json: async () => body }
}

function mk () {
  const appendLog = vi.fn(async () => {})
  const h = useHitlForm({ appendLog })
  return { h, appendLog }
}

beforeEach(() => vi.clearAllMocks())

describe('useHitlForm state + isBotChallengeHitl', () => {
  it('defaults are inert', () => {
    const { h } = mk()
    expect(h.isHumanInterventionRequired.value).toBe(false)
    expect(h.humanInterventionReason.value).toBe('')
    expect(h.hitlFormVisible.value).toBe(false)
    expect(h.hitlFormFields.value).toEqual([])
    expect(h.hitlFormLoading.value).toBe(false)
    expect(h.isBotChallengeHitl.value).toBe(false)
  })

  it('isBotChallengeHitl tracks the reason via isBotChallengeReason', () => {
    const { h } = mk()
    h.humanInterventionReason.value = 'captcha'
    expect(h.isBotChallengeHitl.value).toBe(true)
    h.humanInterventionReason.value = 'login wall'
    expect(h.isBotChallengeHitl.value).toBe(false)
  })
})

describe('useHitlForm.resumeAgentExecution', () => {
  it('happy path clears intervention + success toast', async () => {
    apiFetch.mockResolvedValueOnce(jsonResponse({ status: 'success' }))
    const { h, appendLog } = mk()
    h.isHumanInterventionRequired.value = true
    h.humanInterventionReason.value = 'blocked'
    await h.resumeAgentExecution()
    expect(apiFetch).toHaveBeenCalledWith('/api/human/resume', { method: 'POST' })
    expect(h.isHumanInterventionRequired.value).toBe(false)
    expect(h.humanInterventionReason.value).toBe('')
    expect(ElMessage.success).toHaveBeenCalledWith('已发送恢复执行信号')
    expect(appendLog).toHaveBeenCalled()
  })

  it('error path toasts + logs, leaves intervention flag', async () => {
    apiFetch.mockResolvedValueOnce(jsonResponse({ status: 'error', message: 'nope' }, false))
    const { h } = mk()
    h.isHumanInterventionRequired.value = true
    await h.resumeAgentExecution()
    expect(ElMessage.error).toHaveBeenCalledTimes(1)
    expect(h.isHumanInterventionRequired.value).toBe(true)
  })
})

describe('useHitlForm.submitHitlForm', () => {
  it('happy path hides form + toasts field count', async () => {
    apiFetch.mockResolvedValueOnce(jsonResponse({ status: 'success' }))
    const { h, appendLog } = mk()
    h.hitlFormVisible.value = true
    await h.submitHitlForm({ name: 'a', email: 'b' })
    expect(apiFetch).toHaveBeenCalledWith('/api/human/form_submit', expect.objectContaining({ method: 'POST' }))
    const body = JSON.parse(apiFetch.mock.calls[0][1].body)
    expect(body).toEqual({ fields: { name: 'a', email: 'b' } })
    expect(h.hitlFormVisible.value).toBe(false)
    expect(h.hitlFormLoading.value).toBe(false)
    expect(ElMessage.success).toHaveBeenCalledTimes(1)
    expect(String(appendLog.mock.calls.at(-1)[0])).toContain('2 个字段')
  })

  it('error path keeps form open-able (loading reset) + error toast', async () => {
    apiFetch.mockResolvedValueOnce(jsonResponse({ status: 'error', message: 'bad' }, false))
    const { h } = mk()
    h.hitlFormVisible.value = true
    await h.submitHitlForm({ x: 1 })
    expect(h.hitlFormLoading.value).toBe(false)
    expect(ElMessage.error).toHaveBeenCalledTimes(1)
  })
})

describe('useHitlForm.skipHitlForm', () => {
  it('hides form, resets loading, logs, no network', async () => {
    const { h, appendLog } = mk()
    h.hitlFormVisible.value = true
    h.hitlFormLoading.value = true
    await h.skipHitlForm()
    expect(h.hitlFormVisible.value).toBe(false)
    expect(h.hitlFormLoading.value).toBe(false)
    expect(appendLog).toHaveBeenCalledTimes(1)
    expect(apiFetch).not.toHaveBeenCalled()
  })
})
