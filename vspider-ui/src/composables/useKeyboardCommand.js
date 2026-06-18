// 方向D · 从 App.vue 抽离：全局键盘快捷键派发器（窗口 keydown → 动作）。
//
// 拆成两层，让分派逻辑可单测、动作保持在 App.vue：
//   - resolveKeyboardAction(event, ctx)：纯函数，把按键 + 上下文映射成
//     { action, arg } 或 null（无副作用，便于穷举测试）。
//   - useKeyboardCommand({ getContext, actions })：组合式函数，挂载窗口
//     keydown 监听（卸载时移除），命中后 preventDefault 并调用 actions[action]。
// 行为与原 App.vue handleGlobalKeydown 逐字一致（判定顺序、修饰键、typing 抑制、
// 越界 Ctrl+数字不拦截、Ctrl+Enter / Ctrl+F 在输入态下仍生效）。
import { onMounted, onUnmounted } from 'vue'

// 用户是否正在文本控件里输入（输入时抑制大多数全局快捷键）。
export function isTypingTarget (target) {
  if (!target) return false
  const tag = String(target.tagName || '').toUpperCase()
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true
  if (target.isContentEditable) return true
  return false
}

// 纯分派：返回 { action, arg } 或 null。ctx 字段：
//   activeTab / tabCount / phaseDialogOpen / failedDialogOpen / terminalSearchVisible
export function resolveKeyboardAction (event, ctx = {}) {
  const primary = event.ctrlKey || event.metaKey
  const typing = isTypingTarget(event.target)
  const key = event.key

  // Ctrl+Enter：提交任务（即使在输入框内也生效）
  if (primary && key === 'Enter') return { action: 'submitIfIdle' }

  // 终端标签 + Ctrl+F：打开终端内搜索（输入态也生效）
  if (ctx.activeTab === 'terminal' && primary && (key === 'f' || key === 'F')) {
    return { action: 'terminalSearchOpen' }
  }

  // 其余快捷键在输入态下一律抑制
  if (typing) return null

  if (primary && (key === '/' || key === '?')) return { action: 'toggleHelp' }
  if (primary && (key === 'k' || key === 'K')) return { action: 'focusPrompt' }

  if (primary && key >= '1' && key <= '9') {
    const i = parseInt(key, 10) - 1
    if (i >= 0 && i < (ctx.tabCount || 0)) return { action: 'selectTab', arg: i }
    return null
  }

  if (ctx.phaseDialogOpen) {
    if (key === 'ArrowLeft') return { action: 'phasePrev' }
    if (key === 'ArrowRight') return { action: 'phaseNext' }
  }

  if (ctx.failedDialogOpen) {
    if (key === 'ArrowLeft') return { action: 'failedPrev' }
    if (key === 'ArrowRight') return { action: 'failedNext' }
  }

  if (ctx.activeTab === 'terminal' && ctx.terminalSearchVisible) {
    if (key === 'Escape') return { action: 'terminalSearchClose' }
    if (key === 'Enter' && event.shiftKey) return { action: 'terminalSearchPrev' }
    if (key === 'Enter') return { action: 'terminalSearchNext' }
  }

  if (ctx.activeTab === 'timeline') {
    if (primary && (key === 'e' || key === 'E')) return { action: 'exportTimeline' }
    if (key === 'End' && !primary) return { action: 'timelineBottom' }
    if (key === 'Home' && !primary) return { action: 'timelineTop' }
  }

  if (ctx.activeTab === 'capability') {
    if (primary && (key === 'e' || key === 'E')) return { action: 'exportCapability' }
  }

  return null
}

export function useKeyboardCommand ({ getContext, actions } = {}) {
  const handleGlobalKeydown = (event) => {
    const match = resolveKeyboardAction(event, getContext ? getContext() : {})
    if (!match) return
    event.preventDefault()
    const fn = actions && actions[match.action]
    if (typeof fn === 'function') fn(match.arg)
  }

  // window（而非 document）以便无论焦点在哪都能触发。
  onMounted(() => window.addEventListener('keydown', handleGlobalKeydown))
  onUnmounted(() => window.removeEventListener('keydown', handleGlobalKeydown))

  return { handleGlobalKeydown }
}
