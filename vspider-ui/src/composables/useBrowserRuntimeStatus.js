// 方向D · 从 App.vue 抽离：浏览器运行时（browser pool）状态子系统。
//
// 持有 runtime 状态 + loading 标志、拉取动作（/api/browser_pool）以及 7 个派生
// 展示 computed（status class / 中文标签 / health / health-cache 标签等）。网络经
// apiFetch，告警经注入的 appendLog。行为与原 App.vue 内联实现逐字一致。
import { ref, computed } from 'vue'
import { apiFetch } from '../api/client.js'

export function useBrowserRuntimeStatus ({ appendLog }) {
  const browserRuntimeStatus = ref(null)
  const browserRuntimeLoading = ref(false)

  const fetchBrowserRuntimeStatus = async () => {
    if (browserRuntimeLoading.value) return
    browserRuntimeLoading.value = true
    try {
      const response = await apiFetch('/api/browser_pool')
      const result = await response.json()
      if (!response.ok || result.status !== 'success') {
        throw new Error(result.message || '加载浏览器运行时状态失败')
      }
      browserRuntimeStatus.value = result.runtime || null
    } catch (err) {
      await appendLog(`[WARN] 加载浏览器运行时状态失败: ${String(err)}`)
    } finally {
      browserRuntimeLoading.value = false
    }
  }

  const browserRuntime = computed(() => browserRuntimeStatus.value || {})
  const browserRuntimeCapacity = computed(() => browserRuntime.value.capacity || {})
  const browserRuntimeBackendSummary = computed(() => browserRuntime.value.backend_summary || {})
  const browserRuntimeStatusClass = computed(() => {
    const status = String(browserRuntime.value.status || 'unknown')
    if (status === 'available') return 'healthy'
    if (status === 'pool_exhausted' || status === 'backend_unavailable' || status === 'backend_unhealthy') return 'issue'
    if (status === 'limited') return 'fallback'
    return 'route-only'
  })
  const browserRuntimeLabel = computed(() => {
    const status = String(browserRuntime.value.status || 'unknown')
    if (status === 'available') return '可用'
    if (status === 'pool_exhausted') return 'Pool 已满'
    if (status === 'backend_unavailable') return 'Backend 不可用'
    if (status === 'backend_unhealthy') return 'Backend 异常'
    if (status === 'limited') return '受限'
    return '未知'
  })
  const browserRuntimeHealthLabel = computed(() => {
    const status = String(browserRuntimeBackendSummary.value.health_status || 'unknown')
    if (status === 'healthy') return 'healthy'
    if (status === 'not_configured') return 'not configured'
    if (status === 'unhealthy') return 'unhealthy'
    return status
  })
  const browserRuntimeHealthCacheLabel = computed(() => {
    const summary = browserRuntimeBackendSummary.value
    if (summary.health_cache_stale) return 'stale cache'
    if (summary.health_cache_hit) {
      const age = summary.health_cache_age_s == null ? '?' : summary.health_cache_age_s
      const ttl = summary.health_cache_ttl_s == null ? '?' : summary.health_cache_ttl_s
      return `cached ${age}s/${ttl}s`
    }
    return 'fresh'
  })

  return {
    browserRuntimeStatus,
    browserRuntimeLoading,
    fetchBrowserRuntimeStatus,
    browserRuntime,
    browserRuntimeCapacity,
    browserRuntimeBackendSummary,
    browserRuntimeStatusClass,
    browserRuntimeLabel,
    browserRuntimeHealthLabel,
    browserRuntimeHealthCacheLabel,
  }
}
