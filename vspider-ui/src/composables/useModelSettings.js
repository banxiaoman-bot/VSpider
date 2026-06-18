// 方向D · 从 App.vue 抽离：模型配置（VLM + 语义模型）状态与持久化。
//
// 拥有模型相关的全部响应式状态，并提供：
//   - fetchRemoteModels：从兼容 OpenAI 的 /models 端点拉取远程模型列表
//   - loadModelSettings / saveModelSettings：localStorage 持久化
//   - selectedModelType：纯文本 vs 多模态推断
// 行为与原 App.vue 内联实现保持一致（含模板对 ref 的自动解包语义）。
import { ref, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'

const MODEL_SETTINGS_STORAGE_KEY = 'vspider:model-settings:v1'

export function useModelSettings() {
  const selectedModel = ref('backend-default')
  const selectedSemanticModel = ref('backend-default')
  const modelTemperature = ref(0.1)
  const modelMaxTokens = ref(4096)
  const modelBaseUrl = ref('')
  const modelApiKey = ref('')
  const semanticBaseUrl = ref('')
  const semanticApiKey = ref('')
  const vlmRemoteModels = ref([])
  const vlmRemoteLoading = ref(false)
  const semanticRemoteModels = ref([])
  const semanticRemoteLoading = ref(false)

  async function fetchRemoteModels (baseUrl, apiKey, targetRef, loadingRef) {
    if (!baseUrl) { ElMessage.warning('请先填写 Base URL'); return }
    loadingRef.value = true
    try {
      const headers = { 'Content-Type': 'application/json' }
      if (apiKey) headers['Authorization'] = `Bearer ${apiKey}`
      const url = baseUrl.replace(/\/+$/, '') + '/models'
      const resp = await fetch(url, { headers })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const json = await resp.json()
      const models = (json.data || json.models || []).map(m => typeof m === 'string' ? m : m.id).filter(Boolean)
      if (!models.length) { ElMessage.warning('未返回可用模型'); return }
      targetRef.value = models
      ElMessage.success(`获取到 ${models.length} 个模型`)
    } catch (err) {
      ElMessage.error(`连接失败: ${String(err)}`)
    } finally {
      loadingRef.value = false
    }
  }

  const loadModelSettings = () => {
    try {
      const raw = window.localStorage.getItem(MODEL_SETTINGS_STORAGE_KEY)
      if (!raw) return
      const data = JSON.parse(raw)
      if (typeof data.selectedModel === 'string') selectedModel.value = data.selectedModel
      if (typeof data.selectedSemanticModel === 'string') selectedSemanticModel.value = data.selectedSemanticModel
      if (typeof data.modelBaseUrl === 'string') modelBaseUrl.value = data.modelBaseUrl
      if (typeof data.semanticBaseUrl === 'string') semanticBaseUrl.value = data.semanticBaseUrl
      if (typeof data.modelTemperature === 'number') modelTemperature.value = data.modelTemperature
      if (typeof data.modelMaxTokens === 'number') modelMaxTokens.value = data.modelMaxTokens
    } catch (err) {
      console.warn('[settings] failed to load model settings', err)
    }
  }

  const saveModelSettings = () => {
    try {
      window.localStorage.setItem(MODEL_SETTINGS_STORAGE_KEY, JSON.stringify({
        selectedModel: selectedModel.value,
        selectedSemanticModel: selectedSemanticModel.value,
        modelBaseUrl: modelBaseUrl.value,
        semanticBaseUrl: semanticBaseUrl.value,
        modelTemperature: modelTemperature.value,
        modelMaxTokens: modelMaxTokens.value,
      }))
    } catch (err) {
      console.warn('[settings] failed to save model settings', err)
    }
  }

  watch(
    [selectedModel, selectedSemanticModel, modelBaseUrl, semanticBaseUrl, modelTemperature, modelMaxTokens],
    saveModelSettings,
  )

  const selectedModelType = computed(() =>
    ['deepseek-chat', 'deepseek-reasoner', 'deepseek-v4-flash', 'deepseek-v4-pro']
      .includes(selectedModel.value) ? 'text' : 'vl',
  )

  return {
    selectedModel,
    selectedSemanticModel,
    modelTemperature,
    modelMaxTokens,
    modelBaseUrl,
    modelApiKey,
    semanticBaseUrl,
    semanticApiKey,
    vlmRemoteModels,
    vlmRemoteLoading,
    semanticRemoteModels,
    semanticRemoteLoading,
    fetchRemoteModels,
    loadModelSettings,
    saveModelSettings,
    selectedModelType,
  }
}
