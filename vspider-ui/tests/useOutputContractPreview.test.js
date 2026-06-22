// Behavior lock for composables/useOutputContractPreview.js (方向D · 从 App.vue 抽离)
//
// Pins the output_contract live-preview subsystem previously inline in App.vue:
//   - refreshOutputContractPreview: empty/blank goal clears preview + loading
//     without hitting the network; success → result.formatted; non-success → null;
//     fetch throw → null; loading is toggled and always reset in finally
//   - watch(prompt): 450ms debounce; rapid edits inside the window collapse to a
//     single fetch (timer reset)
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { ref, nextTick } from 'vue'

vi.mock('../src/composables/useTaskSubmit', () => ({
  fetchOutputContractPreview: vi.fn(),
}))

import { fetchOutputContractPreview } from '../src/composables/useTaskSubmit'
import {
  useOutputContractPreview,
  OUTPUT_CONTRACT_PREVIEW_DEBOUNCE_MS,
} from '../src/composables/useOutputContractPreview.js'

beforeEach(() => {
  vi.clearAllMocks()
})

describe('useOutputContractPreview.refreshOutputContractPreview', () => {
  it('blank prompt clears preview + loading without hitting network', async () => {
    const prompt = ref('   ')
    const { outputContractPreview, outputContractPreviewLoading, refreshOutputContractPreview } =
      useOutputContractPreview({ prompt })
    outputContractPreview.value = { kind_label: 'stale' }
    await refreshOutputContractPreview()
    expect(outputContractPreview.value).toBe(null)
    expect(outputContractPreviewLoading.value).toBe(false)
    expect(fetchOutputContractPreview).not.toHaveBeenCalled()
  })

  it('success status stores result.formatted', async () => {
    fetchOutputContractPreview.mockResolvedValue({
      status: 'success',
      formatted: { kind_label: 'K', container_label: 'C' },
    })
    const prompt = ref('grab prices')
    const { outputContractPreview, outputContractPreviewLoading, refreshOutputContractPreview } =
      useOutputContractPreview({ prompt })
    await refreshOutputContractPreview()
    expect(fetchOutputContractPreview).toHaveBeenCalledWith('grab prices')
    expect(outputContractPreview.value).toEqual({ kind_label: 'K', container_label: 'C' })
    expect(outputContractPreviewLoading.value).toBe(false)
  })

  it('non-success status nulls the preview', async () => {
    fetchOutputContractPreview.mockResolvedValue({ status: 'error', message: 'nope' })
    const prompt = ref('grab prices')
    const { outputContractPreview, refreshOutputContractPreview } =
      useOutputContractPreview({ prompt })
    await refreshOutputContractPreview()
    expect(outputContractPreview.value).toBe(null)
  })

  it('fetch throw nulls preview and resets loading in finally', async () => {
    fetchOutputContractPreview.mockRejectedValue(new Error('boom'))
    const prompt = ref('grab prices')
    const { outputContractPreview, outputContractPreviewLoading, refreshOutputContractPreview } =
      useOutputContractPreview({ prompt })
    await refreshOutputContractPreview()
    expect(outputContractPreview.value).toBe(null)
    expect(outputContractPreviewLoading.value).toBe(false)
  })
})

describe('useOutputContractPreview watch(prompt) debounce', () => {
  it('schedules exactly one fetch after the debounce window', async () => {
    vi.useFakeTimers()
    try {
      fetchOutputContractPreview.mockResolvedValue({
        status: 'success',
        formatted: { kind_label: 'K' },
      })
      const prompt = ref('')
      const { outputContractPreview } = useOutputContractPreview({ prompt })
      prompt.value = 'hello'
      await nextTick()
      expect(fetchOutputContractPreview).not.toHaveBeenCalled()
      await vi.advanceTimersByTimeAsync(OUTPUT_CONTRACT_PREVIEW_DEBOUNCE_MS)
      expect(fetchOutputContractPreview).toHaveBeenCalledTimes(1)
      expect(outputContractPreview.value).toEqual({ kind_label: 'K' })
    } finally {
      vi.useRealTimers()
    }
  })

  it('rapid edits within the window collapse to a single trailing fetch', async () => {
    vi.useFakeTimers()
    try {
      fetchOutputContractPreview.mockResolvedValue({ status: 'success', formatted: {} })
      const prompt = ref('')
      useOutputContractPreview({ prompt })
      prompt.value = 'a'
      await nextTick()
      await vi.advanceTimersByTimeAsync(200)
      prompt.value = 'ab'
      await nextTick()
      await vi.advanceTimersByTimeAsync(200)
      prompt.value = 'abc'
      await nextTick()
      await vi.advanceTimersByTimeAsync(OUTPUT_CONTRACT_PREVIEW_DEBOUNCE_MS)
      expect(fetchOutputContractPreview).toHaveBeenCalledTimes(1)
      expect(fetchOutputContractPreview).toHaveBeenCalledWith('abc')
    } finally {
      vi.useRealTimers()
    }
  })
})
