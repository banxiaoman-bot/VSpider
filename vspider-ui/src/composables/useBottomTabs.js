// 方向D · 从 App.vue 抽离：底部 Tab 激活态 + 各 Tab「有新内容」红点 + setActiveBottomTab。
//
// 持有 activeBottomTab / runsSubView / hasNewRuns / hasNewPhase / hasNewCapability /
// timelineAutoScroll / runHistoryRefreshToken + TAB_ORDER + setActiveBottomTab。
// 切 Tab 时清对应红点：artifacts / final 的红点分别由 useScreenshotArtifacts /
// useFinalAnswer 持有，timeline 滚动由 TimelinePanel 暴露 —— 三者经 resolveExternalBadges()
// 延迟解析注入（与 createTerminalLogBuffer 引用 scrollToBottom 的前向引用同模式：
// setActiveBottomTab 仅在 setup 完成后被调用，故安全）。行为与原内联逐字一致。
import { ref } from 'vue'

export function useBottomTabs ({ resolveExternalBadges }) {
  const activeBottomTab = ref('terminal')
  const runsSubView = ref('all')
  const hasNewRuns = ref(false)
  const hasNewPhase = ref(false)
  const hasNewCapability = ref(false)
  const timelineAutoScroll = ref(true)
  const runHistoryRefreshToken = ref(0)
  // Tab name lookup for Ctrl+1..6 — single source of truth shared with the help dialog.
  const TAB_ORDER = ['terminal', 'timeline', 'capability', 'final', 'artifacts', 'runs']

  // Switch active tab + apply the same badge-clearing side effects the el-tabs
  // @tab-change handler does (direct model assignment doesn't fire the event).
  const setActiveBottomTab = (name) => {
    if (!TAB_ORDER.includes(name)) return
    activeBottomTab.value = name
    const ext = resolveExternalBadges()
    if (name === 'artifacts') ext.hasNewArtifacts.value = false
    if (name === 'runs') hasNewRuns.value = false
    if (name === 'final') ext.hasNewFinalAnswer.value = false
    if (name === 'timeline') {
      hasNewPhase.value = false
      if (timelineAutoScroll.value) ext.timelinePanelRef.value?.scrollToBottom()
    }
    if (name === 'capability') hasNewCapability.value = false
  }

  return {
    activeBottomTab,
    runsSubView,
    hasNewRuns,
    hasNewPhase,
    hasNewCapability,
    timelineAutoScroll,
    runHistoryRefreshToken,
    TAB_ORDER,
    setActiveBottomTab,
  }
}
