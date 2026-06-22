// Behavior lock for composables/useAuthProfiles.js (方向D · 从 App.vue 抽离)
//
// Pins the auth-profile + captcha-solver subsystem previously inline in App.vue:
//   - loadAuthProfiles happy stores profiles; non-success/error warns via appendLog, options untouched
//   - loadCaptchaSolverStatus happy stores enabled/provider; non-success early-returns; error swallowed (console.warn)
//   - useAuthProfile appends a name once (dedup), ignores empty
import { describe, it, expect, beforeEach, vi } from 'vitest'

vi.mock('../src/api/client.js', () => ({ apiFetch: vi.fn() }))

import { useAuthProfiles } from '../src/composables/useAuthProfiles.js'
import { apiFetch } from '../src/api/client.js'

function mk () {
  const appendLog = vi.fn(async () => {})
  const a = useAuthProfiles({ appendLog })
  return { a, appendLog }
}

function jsonResponse (body, ok = true) {
  return { ok, json: async () => body }
}

beforeEach(() => vi.clearAllMocks())

describe('useAuthProfiles.loadAuthProfiles', () => {
  it('happy path stores profiles', async () => {
    apiFetch.mockResolvedValueOnce(jsonResponse({ status: 'success', profiles: [{ name: 'p1' }] }))
    const { a } = mk()
    await a.loadAuthProfiles()
    expect(apiFetch).toHaveBeenCalledWith('/api/auth/profiles')
    expect(a.authProfileOptions.value).toEqual([{ name: 'p1' }])
  })

  it('non-success warns via appendLog and leaves options untouched', async () => {
    apiFetch.mockResolvedValueOnce(jsonResponse({ status: 'error', message: 'boom' }, false))
    const { a, appendLog } = mk()
    a.authProfileOptions.value = [{ name: 'prev' }]
    await a.loadAuthProfiles()
    expect(appendLog).toHaveBeenCalledTimes(1)
    expect(String(appendLog.mock.calls[0][0])).toContain('[WARN]')
    expect(a.authProfileOptions.value).toEqual([{ name: 'prev' }])
  })
})

describe('useAuthProfiles.loadCaptchaSolverStatus', () => {
  it('happy path stores enabled + provider', async () => {
    apiFetch.mockResolvedValueOnce(jsonResponse({ status: 'success', enabled: true, provider: 'twocaptcha' }))
    const { a } = mk()
    await a.loadCaptchaSolverStatus()
    expect(apiFetch).toHaveBeenCalledWith('/api/runtime/captcha_solver')
    expect(a.captchaSolverEnabled.value).toBe(true)
    expect(a.captchaSolverProvider.value).toBe('twocaptcha')
  })

  it('non-success early-returns without writing', async () => {
    apiFetch.mockResolvedValueOnce(jsonResponse({ status: 'error' }, false))
    const { a } = mk()
    await a.loadCaptchaSolverStatus()
    expect(a.captchaSolverEnabled.value).toBe(false)
    expect(a.captchaSolverProvider.value).toBe('')
  })

  it('error is swallowed (console.warn, no throw)', async () => {
    apiFetch.mockRejectedValueOnce(new Error('net'))
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const { a } = mk()
    await expect(a.loadCaptchaSolverStatus()).resolves.toBeUndefined()
    expect(warn).toHaveBeenCalled()
    warn.mockRestore()
  })
})

describe('useAuthProfiles.useAuthProfile', () => {
  it('appends once, dedups, ignores empty', () => {
    const { a } = mk()
    a.useAuthProfile('')
    expect(a.selectedAuthProfiles.value).toEqual([])
    a.useAuthProfile('p1')
    expect(a.selectedAuthProfiles.value).toEqual(['p1'])
    a.useAuthProfile('p1')
    expect(a.selectedAuthProfiles.value).toEqual(['p1'])
    a.useAuthProfile('p2')
    expect(a.selectedAuthProfiles.value).toEqual(['p1', 'p2'])
  })
})
