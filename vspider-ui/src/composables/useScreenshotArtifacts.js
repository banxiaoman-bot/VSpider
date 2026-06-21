// 方向D · 从 App.vue 抽离：实时截图流 + 产出文件（artifacts）子系统。
//
// 持有截图历史 ring buffer（currentImageBase64 / screenshotHistory，上限
// SCREENSHOT_HISTORY_MAX）、artifact 列表与"有新文件"红点（artifactList /
// hasNewArtifacts），以及提交时刻的 artifact 基线计数（artifactsCountAtSubmit）。
// 拉取动作走 /api/artifacts（apiFetch），告警经注入的 appendLog；artifacts Tab
// 是否激活由注入的 isArtifactsTabActive() 判定。行为与原 App.vue 内联实现逐字一致。
import { ref } from 'vue'
import { apiFetch } from '../api/client.js'

export const SCREENSHOT_HISTORY_MAX = 60

export function useScreenshotArtifacts ({ appendLog, isArtifactsTabActive }) {
  // ── 实时截图流 ──
  const currentImageBase64 = ref('')
  const screenshotHistory = ref([])

  // 推入一帧截图：始终更新 currentImageBase64（无数据则清空），有数据时再
  // 追加进历史并按 SCREENSHOT_HISTORY_MAX 环形裁剪掉最旧的一帧。
  const pushScreenshotFrame = (data) => {
    currentImageBase64.value = data || ''
    if (data) {
      screenshotHistory.value.push({ src: data, ts: Date.now() })
      if (screenshotHistory.value.length > SCREENSHOT_HISTORY_MAX) {
        screenshotHistory.value.shift()
      }
    }
  }

  // 新任务启动时清空截图流（当前帧 + 历史）。
  const clearScreenshotStream = () => {
    currentImageBase64.value = ''
    screenshotHistory.value = []
  }

  // ── 产出文件（artifacts）──
  const artifactList = ref([])
  const hasNewArtifacts = ref(false)
  // 启动任务时记录 artifact 数，作为兜底的 type 推断依据（后端未显式标记时使用）。
  const artifactsCountAtSubmit = ref(0)

  const fetchArtifacts = async () => {
    try {
      const response = await apiFetch('/api/artifacts')
      const result = await response.json()
      if (!response.ok || result.status !== 'success') {
        throw new Error(result.message || '加载产出文件失败')
      }
      artifactList.value = result.files || []
      if (isArtifactsTabActive()) {
        hasNewArtifacts.value = false
      }
    } catch (err) {
      await appendLog(`[WARN] 加载产出文件失败: ${String(err)}`)
    }
  }

  // 记录提交时刻的 artifact 基线；done 事件用 artifactsGrewSinceSubmit() 判断
  // 本次运行是否新增了 artifact，从而兜底推断答案类型（file vs text）。
  const markArtifactsBaseline = () => {
    artifactsCountAtSubmit.value = artifactList.value.length
  }
  const artifactsGrewSinceSubmit = () =>
    artifactList.value.length > artifactsCountAtSubmit.value

  return {
    SCREENSHOT_HISTORY_MAX,
    currentImageBase64,
    screenshotHistory,
    pushScreenshotFrame,
    clearScreenshotStream,
    artifactList,
    hasNewArtifacts,
    artifactsCountAtSubmit,
    fetchArtifacts,
    markArtifactsBaseline,
    artifactsGrewSinceSubmit,
  }
}
