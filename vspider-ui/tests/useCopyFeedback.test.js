// Behavior lock for composables/useCopyFeedback.js (D-UI-29 part A)
//
// Pins the copy-feedback timer state previously inline in App.vue.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

import { useCopyFeedback } from '../src/composables/useCopyFeedback.js'

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers() })

describe('useCopyFeedback', () => {
  it('starts idle', () => {
    const { finalAnswerCopyState } = useCopyFeedback()
    expect(finalAnswerCopyState.value).toBe('idle')
  })

  it('flashCopyState sets state then resets to idle after resetMs', async () => {
    const { finalAnswerCopyState, flashCopyState } = useCopyFeedback({ resetMs: 1500 })
    flashCopyState('ok')
    expect(finalAnswerCopyState.value).toBe('ok')
    await vi.advanceTimersByTimeAsync(1499)
    expect(finalAnswerCopyState.value).toBe('ok')
    await vi.advanceTimersByTimeAsync(1)
    expect(finalAnswerCopyState.value).toBe('idle')
  })

  it('flashCopyState err → resets after resetMs', async () => {
    const { finalAnswerCopyState, flashCopyState } = useCopyFeedback({ resetMs: 500 })
    flashCopyState('err')
    expect(finalAnswerCopyState.value).toBe('err')
    await vi.advanceTimersByTimeAsync(500)
    expect(finalAnswerCopyState.value).toBe('idle')
  })

  it('rapid flashes reset the timer (only one pending)', async () => {
    const { finalAnswerCopyState, flashCopyState } = useCopyFeedback({ resetMs: 1000 })
    flashCopyState('ok')
    await vi.advanceTimersByTimeAsync(500)
    flashCopyState('err')
    await vi.advanceTimersByTimeAsync(500)
    expect(finalAnswerCopyState.value).toBe('err')
    await vi.advanceTimersByTimeAsync(500)
    expect(finalAnswerCopyState.value).toBe('idle')
  })
})
