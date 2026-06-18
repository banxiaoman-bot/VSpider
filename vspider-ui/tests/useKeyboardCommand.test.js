// Behavior lock for composables/useKeyboardCommand.js (方向D · 从 App.vue 抽离)
//
// Pins the global keyboard dispatch contract that was previously inline in
// App.vue handleGlobalKeydown:
//   - primary = ctrl OR meta
//   - Ctrl+Enter / terminal Ctrl+F fire even while typing; everything else is
//     suppressed while typing
//   - tab digits respect tabCount bounds (out-of-range = no match)
//   - dialog / terminal-search / timeline / capability context branches
import { describe, it, expect } from 'vitest'
import { resolveKeyboardAction, isTypingTarget } from '../src/composables/useKeyboardCommand.js'

const ev = (key, opts = {}) => ({
  key,
  ctrlKey: false,
  metaKey: false,
  shiftKey: false,
  target: null,
  ...opts,
})

describe('isTypingTarget', () => {
  it('is true for text-bearing / editable targets', () => {
    expect(isTypingTarget({ tagName: 'INPUT' })).toBe(true)
    expect(isTypingTarget({ tagName: 'textarea' })).toBe(true)
    expect(isTypingTarget({ tagName: 'SELECT' })).toBe(true)
    expect(isTypingTarget({ tagName: 'DIV', isContentEditable: true })).toBe(true)
  })
  it('is false for non-editable targets / null', () => {
    expect(isTypingTarget({ tagName: 'DIV' })).toBe(false)
    expect(isTypingTarget(null)).toBe(false)
  })
})

describe('resolveKeyboardAction · typing-resistant shortcuts', () => {
  it('Ctrl+Enter submits even while typing, via ctrl or meta', () => {
    expect(resolveKeyboardAction(ev('Enter', { ctrlKey: true }))).toEqual({ action: 'submitIfIdle' })
    expect(resolveKeyboardAction(ev('Enter', { metaKey: true, target: { tagName: 'TEXTAREA' } })))
      .toEqual({ action: 'submitIfIdle' })
  })
  it('terminal Ctrl+F opens search even while typing', () => {
    const ctx = { activeTab: 'terminal' }
    expect(resolveKeyboardAction(ev('f', { ctrlKey: true, target: { tagName: 'INPUT' } }), ctx))
      .toEqual({ action: 'terminalSearchOpen' })
  })
})

describe('resolveKeyboardAction · typing suppression', () => {
  it('suppresses non-exempt shortcuts while typing', () => {
    const t = { target: { tagName: 'TEXTAREA' }, ctrlKey: true }
    expect(resolveKeyboardAction(ev('k', t))).toBeNull()
    expect(resolveKeyboardAction(ev('/', t))).toBeNull()
  })
})

describe('resolveKeyboardAction · global shortcuts', () => {
  it('Ctrl+/ and Ctrl+? toggle help', () => {
    expect(resolveKeyboardAction(ev('/', { ctrlKey: true }))).toEqual({ action: 'toggleHelp' })
    expect(resolveKeyboardAction(ev('?', { ctrlKey: true }))).toEqual({ action: 'toggleHelp' })
  })
  it('Ctrl+K focuses prompt', () => {
    expect(resolveKeyboardAction(ev('k', { ctrlKey: true }))).toEqual({ action: 'focusPrompt' })
    expect(resolveKeyboardAction(ev('K', { ctrlKey: true }))).toEqual({ action: 'focusPrompt' })
  })
  it('Ctrl+digit selects tab within bounds, ignores out-of-range', () => {
    const ctx = { tabCount: 6 }
    expect(resolveKeyboardAction(ev('1', { ctrlKey: true }), ctx)).toEqual({ action: 'selectTab', arg: 0 })
    expect(resolveKeyboardAction(ev('6', { ctrlKey: true }), ctx)).toEqual({ action: 'selectTab', arg: 5 })
    expect(resolveKeyboardAction(ev('7', { ctrlKey: true }), ctx)).toBeNull()
  })
})

describe('resolveKeyboardAction · dialog navigation', () => {
  it('phase dialog arrows', () => {
    const ctx = { phaseDialogOpen: true }
    expect(resolveKeyboardAction(ev('ArrowLeft'), ctx)).toEqual({ action: 'phasePrev' })
    expect(resolveKeyboardAction(ev('ArrowRight'), ctx)).toEqual({ action: 'phaseNext' })
  })
  it('failed-run dialog arrows', () => {
    const ctx = { failedDialogOpen: true }
    expect(resolveKeyboardAction(ev('ArrowLeft'), ctx)).toEqual({ action: 'failedPrev' })
    expect(resolveKeyboardAction(ev('ArrowRight'), ctx)).toEqual({ action: 'failedNext' })
  })
  it('no dialog open → arrows do nothing', () => {
    expect(resolveKeyboardAction(ev('ArrowLeft'), {})).toBeNull()
  })
})

describe('resolveKeyboardAction · terminal search', () => {
  const ctx = { activeTab: 'terminal', terminalSearchVisible: true }
  it('Escape closes, Enter navigates (shift = prev)', () => {
    expect(resolveKeyboardAction(ev('Escape'), ctx)).toEqual({ action: 'terminalSearchClose' })
    expect(resolveKeyboardAction(ev('Enter', { shiftKey: true }), ctx)).toEqual({ action: 'terminalSearchPrev' })
    expect(resolveKeyboardAction(ev('Enter'), ctx)).toEqual({ action: 'terminalSearchNext' })
  })
})

describe('resolveKeyboardAction · tab-scoped exports & scroll', () => {
  it('timeline tab: Ctrl+E export, End/Home scroll', () => {
    const ctx = { activeTab: 'timeline' }
    expect(resolveKeyboardAction(ev('e', { ctrlKey: true }), ctx)).toEqual({ action: 'exportTimeline' })
    expect(resolveKeyboardAction(ev('End'), ctx)).toEqual({ action: 'timelineBottom' })
    expect(resolveKeyboardAction(ev('Home'), ctx)).toEqual({ action: 'timelineTop' })
  })
  it('capability tab: Ctrl+E export', () => {
    expect(resolveKeyboardAction(ev('e', { ctrlKey: true }), { activeTab: 'capability' }))
      .toEqual({ action: 'exportCapability' })
  })
  it('unbound keys return null', () => {
    expect(resolveKeyboardAction(ev('a'), { activeTab: 'timeline' })).toBeNull()
  })
})
