import { ref, onUnmounted, getCurrentInstance } from 'vue'

const DEFAULT_RESET_MS = 1500

/**
 * Composable: manages a copy-button feedback state ('idle' → 'ok'/'err' → auto-reset).
 */
export function useCopyFeedback ({ resetMs = DEFAULT_RESET_MS } = {}) {
  const finalAnswerCopyState = ref('idle')
  let timer = null

  const flashCopyState = (state) => {
    finalAnswerCopyState.value = state
    if (timer) clearTimeout(timer)
    timer = setTimeout(() => {
      timer = null
      finalAnswerCopyState.value = 'idle'
    }, resetMs)
  }

  if (getCurrentInstance()) {
    onUnmounted(() => { if (timer) { clearTimeout(timer); timer = null } })
  }

  return { finalAnswerCopyState, flashCopyState }
}
