// Behavior lock for composables/useBrowserRuntimeStatus.js (方向D · 从 App.vue 抽离)
//
// Pins the browser-pool runtime status subsystem contract:
//   - derived computeds map status/health/cache into the same labels/classes
//     the original App.vue inline implementation produced
//   - fetch happy path stores runtime, toggles loading, hits /api/browser_pool
//   - fetch re-entrancy guard (loading=true) makes no network call
//   - fetch error path warns via injected appendLog and leaves status untouched
import { describe, it, expect, beforeEach, vi } from 'vitest'

vi.mock('../src/api/client.js', () => ({ apiFetch: vi.fn() }))

import { useBrowserRuntimeStatus } from '../src/composables/useBrowserRuntimeStatus.js'
import { apiFetch } from '../src/api/client.js'

function mk (over = {}) {
  const appendLog = vi.fn(async () => {})
  const rt = useBrowserRuntimeStatus({ appendLog, ...over })
  return { rt, appendLog }
}

function jsonResponse (body, ok = true) {
  return { ok, json: async () => body }
}

beforeEach(() => vi.clearAllMocks())

describe('useBrowserRuntimeStatus derived computeds', () => {
  it('defaults to empty objects + route-only/unknown labels with no runtime', () => {
    const { rt } = mk()
    expect(rt.browserRuntime.value).toEqual({})
    expect(rt.browserRuntimeCapacity.value).toEqual({})
    expect(rt.browserRuntimeBackendSummary.value).toEqual({})
    expect(rt.browserRuntimeStatusClass.value).toBe('route-only')
    expect(rt.browserRuntimeLabel.value).toBe('未知')
    expect(rt.browserRuntimeHealthLabel.value).toBe('unknown')
    expect(rt.browserRuntimeHealthCacheLabel.value).toBe('fresh')
  })

  it('maps status -> class + 中文标签', () => {
    const { rt } = mk()
    const cases = [
      ['available', 'healthy', '可用'],
      ['pool_exhausted', 'issue', 'Pool 已满'],
      ['backend_unavailable', 'issue', 'Backend 不可用'],
      ['backend_unhealthy', 'issue', 'Backend 异常'],
      ['limited', 'fallback', '受限'],
      ['weird', 'route-only', '未知'],
    ]
    for (const [status, cls, label] of cases) {
      rt.browserRuntimeStatus.value = { status }
      expect(rt.browserRuntimeStatusClass.value).toBe(cls)
      expect(rt.browserRuntimeLabel.value).toBe(label)
    }
  })

  it('maps backend health + cache label variants', () => {
    const { rt } = mk()
    rt.browserRuntimeStatus.value = { backend_summary: { health_status: 'not_configured' } }
    expect(rt.browserRuntimeHealthLabel.value).toBe('not configured')

    rt.browserRuntimeStatus.value = { backend_summary: { health_cache_stale: true } }
    expect(rt.browserRuntimeHealthCacheLabel.value).toBe('stale cache')

    rt.browserRuntimeStatus.value = {
      backend_summary: { health_cache_hit: true, health_cache_age_s: 3, health_cache_ttl_s: 30 },
    }
    expect(rt.browserRuntimeHealthCacheLabel.value).toBe('cached 3s/30s')

    rt.browserRuntimeStatus.value = { backend_summary: { health_cache_hit: true } }
    expect(rt.browserRuntimeHealthCacheLabel.value).toBe('cached ?s/?s')

    rt.browserRuntimeCapacity // exists
    expect(rt.browserRuntimeCapacity.value).toEqual({})
  })
})

describe('useBrowserRuntimeStatus.fetchBrowserRuntimeStatus', () => {
  it('happy path stores runtime + toggles loading off', async () => {
    apiFetch.mockResolvedValueOnce(jsonResponse({ status: 'success', runtime: { status: 'available' } }))
    const { rt } = mk()
    await rt.fetchBrowserRuntimeStatus()
    expect(apiFetch).toHaveBeenCalledWith('/api/browser_pool')
    expect(rt.browserRuntimeStatus.value).toEqual({ status: 'available' })
    expect(rt.browserRuntimeLoading.value).toBe(false)
  })

  it('re-entrancy guard: no network while already loading', async () => {
    const { rt } = mk()
    rt.browserRuntimeLoading.value = true
    await rt.fetchBrowserRuntimeStatus()
    expect(apiFetch).not.toHaveBeenCalled()
  })

  it('error path warns via appendLog and leaves status untouched', async () => {
    apiFetch.mockResolvedValueOnce(jsonResponse({ status: 'error', message: 'boom' }, false))
    const { rt, appendLog } = mk()
    await rt.fetchBrowserRuntimeStatus()
    expect(appendLog).toHaveBeenCalledTimes(1)
    expect(String(appendLog.mock.calls[0][0])).toContain('[WARN]')
    expect(rt.browserRuntimeStatus.value).toBe(null)
    expect(rt.browserRuntimeLoading.value).toBe(false)
  })
})
