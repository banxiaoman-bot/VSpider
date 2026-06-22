// 方向D · 从 App.vue 抽离：output_contract 实时预览子系统。
//
// 持有 outputContractPreview / outputContractPreviewLoading 两个 ref 与内部
// debounce timer；refreshOutputContractPreview 按当前 prompt 拉取预览（空串清空、
// 不触网；success 取 result.formatted；非 success / 抛错置 null；loading 在 finally
// 复位）；watch(prompt) 以 OUTPUT_CONTRACT_PREVIEW_DEBOUNCE_MS(450ms) 去抖触发刷新；
// onUnmounted 清理 timer（用 getCurrentInstance 守卫，便于 node 测试环境直调零警告）。
// 行为与原 App.vue 内联逐字一致。
import { ref, watch, onUnmounted, getCurrentInstance } from 'vue'
import { fetchOutputContractPreview } from './useTaskSubmit'

export const OUTPUT_CONTRACT_PREVIEW_DEBOUNCE_MS = 450

export function useOutputContractPreview ({ prompt }) {
  const outputContractPreview = ref(null)
  const outputContractPreviewLoading = ref(false)
  let outputContractPreviewTimer = null

  const refreshOutputContractPreview = async () => {
    const text = String(prompt.value || '').trim()
    if (!text) {
      outputContractPreview.value = null
      outputContractPreviewLoading.value = false
      return
    }
    outputContractPreviewLoading.value = true
    try {
      const result = await fetchOutputContractPreview(text)
      outputContractPreview.value = result.status === 'success' ? result.formatted : null
    } catch {
      outputContractPreview.value = null
    } finally {
      outputContractPreviewLoading.value = false
    }
  }

  watch(prompt, () => {
    if (outputContractPreviewTimer) clearTimeout(outputContractPreviewTimer)
    outputContractPreviewTimer = setTimeout(() => {
      refreshOutputContractPreview()
    }, OUTPUT_CONTRACT_PREVIEW_DEBOUNCE_MS)
  })

  if (getCurrentInstance()) {
    onUnmounted(() => {
      if (outputContractPreviewTimer) {
        clearTimeout(outputContractPreviewTimer)
        outputContractPreviewTimer = null
      }
    })
  }

  return {
    outputContractPreview,
    outputContractPreviewLoading,
    refreshOutputContractPreview,
  }
}
