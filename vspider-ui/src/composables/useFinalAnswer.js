// 方向D · 从 App.vue 抽离：Final Answer 面板状态子系统。
//
// 持有 taskResult / finalAnswerStatus / finalAnswerText / finalAnswerDomain /
// hasNewFinalAnswer / finalAnswerExpanded 与 finalAnswerHtml(computed)；done 事件
// 的「答案类型解析」收进 applyDoneAnswer，新任务复位收进 resetForNewRun，自动切 Tab
// 的 watch 内置（写注入的 activeBottomTab）。注意 finalAnswerCopyState/Timer 仍留在
// App.vue（与 onUnmounted 清理同处），不在本 composable 内复位。行为与原内联逐字一致。
import { ref, computed, watch } from 'vue'
import { renderMarkdown } from './markdownRender.js'

export function useFinalAnswer ({ activeBottomTab, artifactsGrewSinceSubmit }) {
  // taskResult: 后端最终结果，结构 { type: 'text' | 'file', answer: string }
  //   - null：未开始 / 已重置
  //   - { type: 'text', answer }：纯文本答案，自动切到 Final Answer Tab 并渲染 Markdown
  //   - { type: 'file', answer? }：结构化导出，自动切到 Artifacts Tab
  // finalAnswerStatus: 'idle' | 'pending' | 'text' | 'file'，驱动面板的 3 种 UI 状态
  const taskResult = ref(null)
  const finalAnswerStatus = ref('idle')
  const finalAnswerText = ref('')
  const finalAnswerDomain = ref('') // F3: 'weather'|'stock'|'recipe'|'flight'|'' (空=无卡片)
  const hasNewFinalAnswer = ref(false)
  const finalAnswerExpanded = ref(false)
  const finalAnswerHtml = computed(() => renderMarkdown(finalAnswerText.value))

  // ── 自动切换 Tab：仅在 taskResult.type 真正发生变化时触发，避免无限循环 ──
  //   - 只读 taskResult，只写 activeBottomTab / hasNewFinalAnswer，不回写 taskResult
  //   - 重复的 done 事件（同 type）不会再次抢用户已切走的 Tab
  watch(taskResult, (val, oldVal) => {
    if (!val) return
    if (oldVal && oldVal.type === val.type) return
    if (val.type === 'text') {
      if (activeBottomTab.value !== 'final') {
        hasNewFinalAnswer.value = true
      }
      activeBottomTab.value = 'final'
    } else if (val.type === 'file') {
      activeBottomTab.value = 'artifacts'
    }
  })

  // done 事件：解析后端结果类型。优先用显式字段 (answer_type / answer)；缺失则用兜底
  // 启发式（本次运行有新 artifact → 'file'，否则 'text'，把 message 当纯文本答案）。
  const applyDoneAnswer = (payload, text) => {
    let answerType =
      typeof payload.answer_type === 'string' && payload.answer_type
        ? payload.answer_type
        : null
    const answerText =
      typeof payload.answer === 'string' ? payload.answer : ''
    const hasExplicitAnswerType =
      typeof payload.answer_type === 'string' && payload.answer_type
    const hasExplicitAnswer = answerText.trim().length > 0
    const hasExistingTextAnswer =
      taskResult.value &&
      taskResult.value.type === 'text' &&
      typeof finalAnswerText.value === 'string' &&
      finalAnswerText.value.trim().length > 0
    if (!hasExplicitAnswerType && !hasExplicitAnswer && hasExistingTextAnswer) {
      return
    }
    if (!answerType) {
      answerType = artifactsGrewSinceSubmit() ? 'file' : 'text'
    }
    const fallbackText = answerText || (answerType === 'text' ? text : '')
    finalAnswerText.value = fallbackText
    finalAnswerStatus.value = answerType === 'file' ? 'file' : 'text'
    // F3: domain hint from backend (only meaningful when answerType==='text')
    const rawDomain =
      typeof payload.answer_domain === 'string' ? payload.answer_domain : ''
    const ALLOWED_DOMAINS = ['weather', 'stock', 'recipe', 'flight']
    finalAnswerDomain.value =
      answerType === 'text' && ALLOWED_DOMAINS.includes(rawDomain) ? rawDomain : ''
    taskResult.value = { type: answerType, answer: fallbackText }
  }

  // 新任务启动时复位（finalAnswerCopyState 留在 App.vue，不在此复位）。
  const resetForNewRun = () => {
    taskResult.value = null
    finalAnswerText.value = ''
    finalAnswerStatus.value = 'pending'
    hasNewFinalAnswer.value = false
    finalAnswerExpanded.value = false // F2: 新任务默认折叠
    finalAnswerDomain.value = '' // F3: 清除上轮的域卡片
  }

  return {
    taskResult,
    finalAnswerStatus,
    finalAnswerText,
    finalAnswerDomain,
    hasNewFinalAnswer,
    finalAnswerExpanded,
    finalAnswerHtml,
    applyDoneAnswer,
    resetForNewRun,
  }
}
