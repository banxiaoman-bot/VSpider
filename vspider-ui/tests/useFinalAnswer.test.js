// Behavior lock for composables/useFinalAnswer.js (方向D · 从 App.vue 抽离)
//
// Pins the Final Answer panel subsystem previously inline in App.vue:
//   - applyDoneAnswer: explicit answer_type wins; else artifactsGrew heuristic (file/text);
//     dedupe early-return on existing text answer + no explicit fields; domain whitelist (text only)
//   - resetForNewRun: clears to 'pending' baseline (copyState stays in App.vue)
//   - watch(taskResult): switches activeBottomTab on a real type change only
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { ref, nextTick } from 'vue'

vi.mock('../src/composables/markdownRender.js', () => ({
  renderMarkdown: (t) => `<md>${t}</md>`,
}))

import { useFinalAnswer } from '../src/composables/useFinalAnswer.js'

function mk (over = {}) {
  const activeBottomTab = over.activeBottomTab || ref('terminal')
  const artifactsGrewSinceSubmit = over.artifactsGrewSinceSubmit || (() => false)
  const fa = useFinalAnswer({ activeBottomTab, artifactsGrewSinceSubmit })
  return { fa, activeBottomTab }
}

beforeEach(() => vi.clearAllMocks())

describe('useFinalAnswer.applyDoneAnswer', () => {
  it('explicit answer_type=file sets file status + taskResult', () => {
    const { fa } = mk()
    fa.applyDoneAnswer({ answer_type: 'file', answer: 'out.xlsx' }, 'done')
    expect(fa.finalAnswerStatus.value).toBe('file')
    expect(fa.taskResult.value).toEqual({ type: 'file', answer: 'out.xlsx' })
  })

  it('no explicit type falls back to artifactsGrew heuristic', () => {
    const grew = mk({ artifactsGrewSinceSubmit: () => true })
    grew.fa.applyDoneAnswer({ message: 'm' }, 'the text')
    expect(grew.fa.finalAnswerStatus.value).toBe('file')

    const flat = mk({ artifactsGrewSinceSubmit: () => false })
    flat.fa.applyDoneAnswer({}, 'the text')
    expect(flat.fa.finalAnswerStatus.value).toBe('text')
    expect(flat.fa.finalAnswerText.value).toBe('the text')
    expect(flat.fa.taskResult.value).toEqual({ type: 'text', answer: 'the text' })
  })

  it('dedupes: existing text answer + no explicit fields → no-op', () => {
    const { fa } = mk()
    fa.applyDoneAnswer({ answer: 'first answer' }, 'first answer')
    const before = fa.taskResult.value
    fa.applyDoneAnswer({}, 'late done')
    expect(fa.taskResult.value).toBe(before)
    expect(fa.finalAnswerText.value).toBe('first answer')
  })

  it('answer_domain whitelist only applies when type is text', () => {
    const a = mk()
    a.fa.applyDoneAnswer({ answer_type: 'text', answer: 'x', answer_domain: 'weather' }, 'd')
    expect(a.fa.finalAnswerDomain.value).toBe('weather')

    const b = mk()
    b.fa.applyDoneAnswer({ answer_type: 'text', answer: 'x', answer_domain: 'bogus' }, 'd')
    expect(b.fa.finalAnswerDomain.value).toBe('')

    const c = mk()
    c.fa.applyDoneAnswer({ answer_type: 'file', answer: 'x', answer_domain: 'weather' }, 'd')
    expect(c.fa.finalAnswerDomain.value).toBe('')
  })

  it('finalAnswerHtml renders finalAnswerText via renderMarkdown', () => {
    const { fa } = mk()
    fa.applyDoneAnswer({ answer_type: 'text', answer: 'hello' }, 'd')
    expect(fa.finalAnswerHtml.value).toBe('<md>hello</md>')
  })
})

describe('useFinalAnswer.resetForNewRun', () => {
  it('resets panel to pending baseline', () => {
    const { fa } = mk()
    fa.applyDoneAnswer({ answer_type: 'text', answer: 'old', answer_domain: 'stock' }, 'd')
    fa.hasNewFinalAnswer.value = true
    fa.resetForNewRun()
    expect(fa.taskResult.value).toBe(null)
    expect(fa.finalAnswerText.value).toBe('')
    expect(fa.finalAnswerStatus.value).toBe('pending')
    expect(fa.hasNewFinalAnswer.value).toBe(false)
    expect(fa.finalAnswerDomain.value).toBe('')
  })
})

describe('useFinalAnswer watch(taskResult)', () => {
  it('text result switches to final tab + pulses badge when not on final', async () => {
    const activeBottomTab = ref('terminal')
    const { fa } = mk({ activeBottomTab })
    fa.applyDoneAnswer({ answer_type: 'text', answer: 'x' }, 'd')
    await nextTick()
    expect(activeBottomTab.value).toBe('final')
    expect(fa.hasNewFinalAnswer.value).toBe(true)
  })

  it('file result switches to artifacts tab', async () => {
    const activeBottomTab = ref('terminal')
    const { fa } = mk({ activeBottomTab })
    fa.applyDoneAnswer({ answer_type: 'file', answer: 'x' }, 'd')
    await nextTick()
    expect(activeBottomTab.value).toBe('artifacts')
  })

  it('same type twice does not re-grab the tab', async () => {
    const activeBottomTab = ref('terminal')
    const { fa } = mk({ activeBottomTab })
    fa.applyDoneAnswer({ answer_type: 'text', answer: 'a' }, 'd')
    await nextTick()
    activeBottomTab.value = 'runs'
    fa.hasNewFinalAnswer.value = false
    fa.applyDoneAnswer({ answer_type: 'text', answer: 'b' }, 'd')
    await nextTick()
    expect(activeBottomTab.value).toBe('runs')
    expect(fa.hasNewFinalAnswer.value).toBe(false)
  })
})
