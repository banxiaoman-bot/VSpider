// Behavior lock for composables/useClipboard.js (方向D · 从 App.vue 抽离)
//
// Pins the clipboard-write contract previously inline in App.vue _writeToClipboard:
//   - empty text short-circuits to false
//   - navigator.clipboard.writeText path → true
//   - fallback to textarea + execCommand('copy') when the clipboard API is missing → true
//   - any throw → ElMessage.error('复制失败: …') and returns false
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

vi.mock('element-plus', () => ({
  ElMessage: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

import { writeToClipboard } from '../src/composables/useClipboard.js'
import { ElMessage } from 'element-plus'

beforeEach(() => vi.clearAllMocks())
afterEach(() => vi.unstubAllGlobals())

describe('writeToClipboard', () => {
  it('returns false for empty text', async () => {
    expect(await writeToClipboard('')).toBe(false)
  })

  it('uses navigator.clipboard.writeText when available', async () => {
    const writeText = vi.fn(async () => {})
    vi.stubGlobal('navigator', { clipboard: { writeText } })
    const ok = await writeToClipboard('hello')
    expect(writeText).toHaveBeenCalledWith('hello')
    expect(ok).toBe(true)
  })

  it('falls back to textarea + execCommand when clipboard API missing', async () => {
    const ta = { value: '', select: vi.fn() }
    const appendChild = vi.fn()
    const removeChild = vi.fn()
    const execCommand = vi.fn()
    vi.stubGlobal('navigator', {})
    vi.stubGlobal('document', {
      createElement: vi.fn(() => ta),
      body: { appendChild, removeChild },
      execCommand,
    })
    const ok = await writeToClipboard('copy me')
    expect(ta.value).toBe('copy me')
    expect(ta.select).toHaveBeenCalledTimes(1)
    expect(execCommand).toHaveBeenCalledWith('copy')
    expect(appendChild).toHaveBeenCalledWith(ta)
    expect(removeChild).toHaveBeenCalledWith(ta)
    expect(ok).toBe(true)
  })

  it('returns false and toasts on error', async () => {
    vi.stubGlobal('navigator', {
      clipboard: { writeText: vi.fn(async () => { throw new Error('denied') }) },
    })
    const ok = await writeToClipboard('x')
    expect(ElMessage.error).toHaveBeenCalledTimes(1)
    expect(String(ElMessage.error.mock.calls[0][0])).toContain('复制失败')
    expect(ok).toBe(false)
  })
})
