// Behavior lock for composables/useModelSettings.js (方向D · 从 App.vue 抽离)
//
// Pins the model-config contract that was previously inline in App.vue:
//   - selectedModelType text/vl inference
//   - localStorage save/load round-trip (only whitelisted fields)
//   - fetchRemoteModels happy path + guard
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { ref } from 'vue'

vi.mock('element-plus', () => ({
  ElMessage: {
    warning: vi.fn(),
    success: vi.fn(),
    error: vi.fn(),
  },
}))

import { useModelSettings } from '../src/composables/useModelSettings.js'
import { ElMessage } from 'element-plus'

function makeLocalStorage() {
  const store = new Map()
  return {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  }
}

beforeEach(() => {
  vi.stubGlobal('window', { localStorage: makeLocalStorage() })
  vi.clearAllMocks()
})

describe('useModelSettings.selectedModelType', () => {
  it('infers text for deepseek text models, vl otherwise', () => {
    const ms = useModelSettings()
    expect(ms.selectedModelType.value).toBe('vl') // backend-default
    ms.selectedModel.value = 'deepseek-chat'
    expect(ms.selectedModelType.value).toBe('text')
    ms.selectedModel.value = 'deepseek-reasoner'
    expect(ms.selectedModelType.value).toBe('text')
    ms.selectedModel.value = 'qwen-vl-max'
    expect(ms.selectedModelType.value).toBe('vl')
  })
})

describe('useModelSettings persistence', () => {
  it('saves only whitelisted fields and loads them back', () => {
    const a = useModelSettings()
    a.selectedModel.value = 'qwen-vl-max'
    a.selectedSemanticModel.value = 'deepseek-chat'
    a.modelBaseUrl.value = 'https://vlm.example/v1'
    a.semanticBaseUrl.value = 'https://sem.example/v1'
    a.modelTemperature.value = 0.7
    a.modelMaxTokens.value = 8192
    a.modelApiKey.value = 'secret-should-not-persist'
    a.saveModelSettings()

    const raw = window.localStorage.getItem('vspider:model-settings:v1')
    const parsed = JSON.parse(raw)
    expect(parsed).not.toHaveProperty('modelApiKey') // api keys are never persisted
    expect(parsed.selectedModel).toBe('qwen-vl-max')

    const b = useModelSettings()
    b.loadModelSettings()
    expect(b.selectedModel.value).toBe('qwen-vl-max')
    expect(b.selectedSemanticModel.value).toBe('deepseek-chat')
    expect(b.modelBaseUrl.value).toBe('https://vlm.example/v1')
    expect(b.modelTemperature.value).toBe(0.7)
    expect(b.modelMaxTokens.value).toBe(8192)
  })

  it('loadModelSettings is a no-op when storage is empty', () => {
    const ms = useModelSettings()
    ms.loadModelSettings()
    expect(ms.selectedModel.value).toBe('backend-default')
  })
})

describe('useModelSettings.fetchRemoteModels', () => {
  it('warns and skips when baseUrl is missing', async () => {
    const ms = useModelSettings()
    const target = ref([])
    const loading = ref(false)
    await ms.fetchRemoteModels('', '', target, loading)
    expect(ElMessage.warning).toHaveBeenCalled()
    expect(target.value).toEqual([])
  })

  it('populates target ref from a compatible /models response', async () => {
    const ms = useModelSettings()
    const target = ref([])
    const loading = ref(false)
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      json: async () => ({ data: [{ id: 'm1' }, { id: 'm2' }] }),
    })))
    await ms.fetchRemoteModels('https://api.example/v1/', 'key', target, loading)
    expect(target.value).toEqual(['m1', 'm2'])
    expect(loading.value).toBe(false)
    expect(ElMessage.success).toHaveBeenCalled()
  })
})

describe('useModelSettings server persistence', () => {
  it('auto-saves to server once after a successful connect (section provided)', async () => {
    const ms = useModelSettings()
    const target = ref([])
    const loading = ref(false)
    const calls = []
    vi.stubGlobal('fetch', vi.fn(async (url, options) => {
      const u = String(url)
      const method = (options && options.method) || 'GET'
      calls.push({ u, method })
      if (u.includes('/api/model_config')) {
        return { ok: true, json: async () => ({ status: 'success', result: { vlm: { has_api_key: true }, semantic: { has_api_key: false } } }) }
      }
      return { ok: true, json: async () => ({ data: [{ id: 'm1' }] }) }
    }))
    ms.modelBaseUrl.value = 'https://vlm.example/v1'
    await ms.fetchRemoteModels('https://vlm.example/v1', 'sk-key', target, loading, 'vlm')
    const posts = calls.filter(c => c.u.includes('/api/model_config') && c.method === 'POST')
    expect(posts.length).toBe(1)
    expect(ms.vlmHasSavedKey.value).toBe(true)
  })

  it('loadServerModelConfig prefills base_url/model + has-key flag, never fills plaintext key', async () => {
    const ms = useModelSettings()
    vi.stubGlobal('fetch', vi.fn(async (url) => {
      if (String(url).includes('/api/model_config')) {
        return { ok: true, json: async () => ({ status: 'success', result: {
          vlm: { base_url: 'https://saved.example/v1', model: 'qwen-vl-max', temperature: 0.3, max_tokens: 2048, api_key: 'sk-3****abcd', has_api_key: true },
          semantic: { base_url: '', model: '', api_key: '', has_api_key: false },
        } }) }
      }
      return { ok: false, json: async () => ({}) }
    }))
    await ms.loadServerModelConfig()
    expect(ms.modelBaseUrl.value).toBe('https://saved.example/v1')
    expect(ms.selectedModel.value).toBe('qwen-vl-max')
    expect(ms.modelTemperature.value).toBe(0.3)
    expect(ms.modelMaxTokens.value).toBe(2048)
    expect(ms.vlmHasSavedKey.value).toBe(true)
    expect(ms.modelApiKey.value).toBe('')  // masked key never written into the input
  })
})

describe('useModelSettings connection status', () => {
  it('idle by default', () => {
    const ms = useModelSettings()
    expect(ms.vlmConnStatus.value.state).toBe('idle')
    expect(ms.semanticConnStatus.value.state).toBe('idle')
  })

  it('sets ok status with model count on a successful connect', async () => {
    const ms = useModelSettings()
    const target = ref([])
    const loading = ref(false)
    vi.stubGlobal('fetch', vi.fn(async (url) => {
      if (String(url).includes('/api/model_config')) {
        return { ok: true, json: async () => ({ status: 'success', result: {} }) }
      }
      return { ok: true, json: async () => ({ data: [{ id: 'a' }, { id: 'b' }] }) }
    }))
    ms.modelBaseUrl.value = 'https://vlm.example/v1'
    await ms.fetchRemoteModels('https://vlm.example/v1', 'sk-key', target, loading, 'vlm')
    expect(ms.vlmConnStatus.value.state).toBe('ok')
    expect(ms.vlmConnStatus.value.text).toContain('2')
  })

  it('sets error status with /v1 hint on HTTP 404', async () => {
    const ms = useModelSettings()
    const target = ref([])
    const loading = ref(false)
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 404, json: async () => ({}) })))
    await ms.fetchRemoteModels('https://vlm.example', 'sk-key', target, loading, 'vlm')
    expect(ms.vlmConnStatus.value.state).toBe('error')
    expect(ms.vlmConnStatus.value.text).toContain('/v1')
  })

  it('auto-clears a terminal status back to idle after a few seconds', async () => {
    vi.useFakeTimers()
    try {
      const ms = useModelSettings()
      const target = ref([])
      const loading = ref(false)
      vi.stubGlobal('fetch', vi.fn(async (url) => {
        if (String(url).includes('/api/model_config')) {
          return { ok: true, json: async () => ({ status: 'success', result: {} }) }
        }
        return { ok: true, json: async () => ({ data: [{ id: 'a' }] }) }
      }))
      ms.modelBaseUrl.value = 'https://vlm.example/v1'
      await ms.fetchRemoteModels('https://vlm.example/v1', 'sk-key', target, loading, 'vlm')
      expect(ms.vlmConnStatus.value.state).toBe('ok')
      vi.advanceTimersByTime(5000)
      expect(ms.vlmConnStatus.value.state).toBe('idle')
    } finally {
      vi.useRealTimers()
    }
  })
})
