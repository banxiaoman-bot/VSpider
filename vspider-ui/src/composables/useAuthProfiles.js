// 方向D · 从 App.vue 抽离：Auth Profile 选择 + Captcha Solver 状态子系统。
//
// 持有 auth 对话框开关 / profile 选项与已选列表 / captcha solver 启用态与 provider；
// 网络经 apiFetch，告警经注入的 appendLog；captcha 状态拉取失败为非关键，仅 console.warn。
// 行为与原 App.vue 内联实现逐字一致。
import { ref } from 'vue'
import { apiFetch } from '../api/client.js'

export function useAuthProfiles ({ appendLog }) {
  const authDialogOpen = ref(false)
  const authProfileOptions = ref([])
  const selectedAuthProfiles = ref([])
  const captchaSolverEnabled = ref(false)
  const captchaSolverProvider = ref('')

  const loadAuthProfiles = async () => {
    try {
      const response = await apiFetch('/api/auth/profiles')
      const result = await response.json()
      if (!response.ok || result.status !== 'success') {
        throw new Error(result.message || '加载 Auth Profiles 失败')
      }
      authProfileOptions.value = result.profiles || []
    } catch (err) {
      await appendLog(`[WARN] 加载 Auth Profiles 失败: ${String(err)}`)
    }
  }

  const loadCaptchaSolverStatus = async () => {
    try {
      const response = await apiFetch('/api/runtime/captcha_solver')
      const result = await response.json()
      if (!response.ok || result.status !== 'success') return
      captchaSolverEnabled.value = Boolean(result.enabled)
      captchaSolverProvider.value = String(result.provider || '')
    } catch (err) {
      // non-critical
      console.warn('[captcha_solver] status fetch failed:', err)
    }
  }

  const useAuthProfile = (name) => {
    if (!name) return
    if (!selectedAuthProfiles.value.includes(name)) {
      selectedAuthProfiles.value = [...selectedAuthProfiles.value, name]
    }
  }

  return {
    authDialogOpen,
    authProfileOptions,
    selectedAuthProfiles,
    captchaSolverEnabled,
    captchaSolverProvider,
    loadAuthProfiles,
    loadCaptchaSolverStatus,
    useAuthProfile,
  }
}
