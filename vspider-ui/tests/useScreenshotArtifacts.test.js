// Behavior lock for composables/useScreenshotArtifacts.js (方向D · 从 App.vue 抽离)
//
// Pins the screenshot-stream + artifacts subsystem contract:
//   - pushScreenshotFrame always sets currentImageBase64 (empty clears it),
//     appends {src,ts} only when data present, ring-trims to SCREENSHOT_HISTORY_MAX
//   - clearScreenshotStream resets current frame + history
//   - fetchArtifacts happy path stores files; clears hasNewArtifacts ONLY when the
//     artifacts tab is active; error path warns via appendLog, leaves list untouched
//   - markArtifactsBaseline / artifactsGrewSinceSubmit capture + compare the
//     submit-time artifact count exactly like the original done-handler heuristic
import { describe, it, expect, beforeEach, vi } from 'vitest'

vi.mock('../src/api/client.js', () => ({ apiFetch: vi.fn() }))

import {
  useScreenshotArtifacts,
  SCREENSHOT_HISTORY_MAX,
} from '../src/composables/useScreenshotArtifacts.js'
import { apiFetch } from '../src/api/client.js'

function mk (over = {}) {
  const appendLog = vi.fn(async () => {})
  const isArtifactsTabActive = over.isArtifactsTabActive || vi.fn(() => false)
  const sa = useScreenshotArtifacts({ appendLog, isArtifactsTabActive })
  return { sa, appendLog, isArtifactsTabActive }
}

function jsonResponse (body, ok = true) {
  return { ok, json: async () => body }
}

beforeEach(() => vi.clearAllMocks())

describe('useScreenshotArtifacts screenshot stream', () => {
  it('pushScreenshotFrame stores current frame + appends history entry', () => {
    const { sa } = mk()
    sa.pushScreenshotFrame('data:image/png;base64,AAAA')
    expect(sa.currentImageBase64.value).toBe('data:image/png;base64,AAAA')
    expect(sa.screenshotHistory.value).toHaveLength(1)
    expect(sa.screenshotHistory.value[0].src).toBe('data:image/png;base64,AAAA')
    expect(typeof sa.screenshotHistory.value[0].ts).toBe('number')
  })

  it('pushScreenshotFrame with empty data clears current frame and does not append', () => {
    const { sa } = mk()
    sa.pushScreenshotFrame('first')
    sa.pushScreenshotFrame('')
    expect(sa.currentImageBase64.value).toBe('')
    expect(sa.screenshotHistory.value).toHaveLength(1)
  })

  it('ring-trims history to SCREENSHOT_HISTORY_MAX, dropping the oldest', () => {
    const { sa } = mk()
    for (let i = 0; i < SCREENSHOT_HISTORY_MAX + 5; i += 1) {
      sa.pushScreenshotFrame(`frame-${i}`)
    }
    expect(sa.screenshotHistory.value).toHaveLength(SCREENSHOT_HISTORY_MAX)
    // first 5 pushed got shifted out; oldest remaining is frame-5
    expect(sa.screenshotHistory.value[0].src).toBe('frame-5')
    expect(sa.currentImageBase64.value).toBe(`frame-${SCREENSHOT_HISTORY_MAX + 4}`)
  })

  it('clearScreenshotStream resets current frame + history', () => {
    const { sa } = mk()
    sa.pushScreenshotFrame('keep?')
    sa.clearScreenshotStream()
    expect(sa.currentImageBase64.value).toBe('')
    expect(sa.screenshotHistory.value).toEqual([])
  })
})

describe('useScreenshotArtifacts.fetchArtifacts', () => {
  it('happy path stores files; clears red-dot when artifacts tab active', async () => {
    apiFetch.mockResolvedValueOnce(jsonResponse({ status: 'success', files: [{ name: 'a.xlsx' }] }))
    const { sa } = mk({ isArtifactsTabActive: vi.fn(() => true) })
    sa.hasNewArtifacts.value = true
    await sa.fetchArtifacts()
    expect(apiFetch).toHaveBeenCalledWith('/api/artifacts')
    expect(sa.artifactList.value).toEqual([{ name: 'a.xlsx' }])
    expect(sa.hasNewArtifacts.value).toBe(false)
  })

  it('happy path leaves red-dot untouched when artifacts tab inactive', async () => {
    apiFetch.mockResolvedValueOnce(jsonResponse({ status: 'success', files: [] }))
    const { sa } = mk({ isArtifactsTabActive: vi.fn(() => false) })
    sa.hasNewArtifacts.value = true
    await sa.fetchArtifacts()
    expect(sa.artifactList.value).toEqual([])
    expect(sa.hasNewArtifacts.value).toBe(true)
  })

  it('error path warns via appendLog and leaves artifactList untouched', async () => {
    apiFetch.mockResolvedValueOnce(jsonResponse({ status: 'error', message: 'boom' }, false))
    const { sa, appendLog } = mk()
    sa.artifactList.value = [{ name: 'prev' }]
    await sa.fetchArtifacts()
    expect(appendLog).toHaveBeenCalledTimes(1)
    expect(String(appendLog.mock.calls[0][0])).toContain('[WARN]')
    expect(sa.artifactList.value).toEqual([{ name: 'prev' }])
  })
})

describe('useScreenshotArtifacts artifact baseline heuristic', () => {
  it('markArtifactsBaseline captures current count; grew=false right after', () => {
    const { sa } = mk()
    sa.artifactList.value = [{ name: '1' }, { name: '2' }]
    sa.markArtifactsBaseline()
    expect(sa.artifactsCountAtSubmit.value).toBe(2)
    expect(sa.artifactsGrewSinceSubmit()).toBe(false)
  })

  it('artifactsGrewSinceSubmit becomes true once the list outgrows the baseline', () => {
    const { sa } = mk()
    sa.markArtifactsBaseline() // baseline 0
    expect(sa.artifactsGrewSinceSubmit()).toBe(false)
    sa.artifactList.value = [{ name: 'new' }]
    expect(sa.artifactsGrewSinceSubmit()).toBe(true)
  })
})
