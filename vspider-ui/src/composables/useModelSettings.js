// 方向D · 从 App.vue 抽离：模型配置（VLM + 语义模型）状态与持久化。
//
// 拥有模型相关的全部响应式状态，并提供：
//   - fetchRemoteModels：从兼容 OpenAI 的 /models 端点拉取远程模型列表
//   - loadModelSettings / saveModelSettings：localStorage 持久化
//   - selectedModelType：纯文本 vs 多模态推断
// 行为与原 App.vue 内联实现保持一致（含模板对 ref 的自动解包语义）。
import { ref, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { apiFetch } from '../api/client.js'

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
  const vlmHasSavedKey = ref(false)
  const semanticHasSavedKey = ref(false)
  // 持久连接状态徽标：state ∈ idle | connecting | ok | warning | error
  const vlmConnStatus = ref({ state: 'idle', text: '' })
  const semanticConnStatus = ref({ state: 'idle', text: '' })

  const CONN_STATUS_TTL_MS = 5000
  const _connTimers = { vlm: null, semantic: null }

  function _connStatusRefFor (section) {
    if (section === 'vlm') return vlmConnStatus
    if (section === 'semantic') return semanticConnStatus
    return null
  }

  function _setConnStatus (section, state, text) {
    const statusRef = _connStatusRefFor(section)
    if (!statusRef) return
    if (_connTimers[section]) { clearTimeout(_connTimers[section]); _connTimers[section] = null }
    statusRef.value = { state, text }
    // 终态（ok/warning/error）几秒后自动清回 idle；'connecting' 保持到结果返回
    if (state === 'ok' || state === 'warning' || state === 'error') {
      const handle = setTimeout(() => {
        statusRef.value = { state: 'idle', text: '' }
        _connTimers[section] = null
      }, CONN_STATUS_TTL_MS)
      if (handle && typeof handle.unref === 'function') handle.unref()
      _connTimers[section] = handle
    }
  }

  function _friendlyConnError (err) {
    const s = String(err).replace(/^Error:\s*/, '')
    if (s.includes('HTTP 404')) return 'HTTP 404（路径不对，请确认 base_url 是否需要 /v1 后缀）'
    if (s.includes('HTTP 401') || s.includes('HTTP 403')) return `${s}（API Key 无效或缺失）`
    if (/Failed to fetch|NetworkError|TypeError/.test(s)) return '网络不可达或 CORS 限制'
    return s
  }

  // 注意：模板传参会被 Vue 自动解包（ref → 原始值），因此不能从模板接收 targetRef/loadingRef。
  // 改为按 section 使用本 composable 自己持有的内部 ref（vlm / semantic 各一套）。
  async function fetchRemoteModels (baseUrl, apiKey, section = 'vlm') {
    const targetRef = section === 'semantic' ? semanticRemoteModels : vlmRemoteModels
    const loadingRef = section === 'semantic' ? semanticRemoteLoading : vlmRemoteLoading
    if (!baseUrl) {
      ElMessage.warning('请先填写 Base URL')
      _setConnStatus(section, 'warning', '请先填写 Base URL')
      return
    }
    loadingRef.value = true
    _setConnStatus(section, 'connecting', '连接中…')
    try {
      const headers = { 'Content-Type': 'application/json' }
      if (apiKey) headers['Authorization'] = `Bearer ${apiKey}`
      const url = baseUrl.replace(/\/+$/, '') + '/models'
      const resp = await fetch(url, { headers })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const json = await resp.json()
      const models = (json.data || json.models || []).map(m => typeof m === 'string' ? m : m.id).filter(Boolean)
      if (!models.length) {
        ElMessage.warning('未返回可用模型')
        _setConnStatus(section, 'warning', '已连接，但未返回模型')
        return
      }
      targetRef.value = models
      ElMessage.success(`获取到 ${models.length} 个模型`)
      _setConnStatus(section, 'ok', `已连通 · ${models.length} 个模型`)
      await saveServerModelConfig(section)
    } catch (err) {
      const msg = _friendlyConnError(err)
      ElMessage.error(`连接失败: ${msg}`)
      _setConnStatus(section, 'error', `连接失败: ${msg}`)
    } finally {
      loadingRef.value = false
    }
  }

  function applyServerMaskFlags (result) {
    if (!result) return
    if (result.vlm) vlmHasSavedKey.value = !!result.vlm.has_api_key
    if (result.semantic) semanticHasSavedKey.value = !!result.semantic.has_api_key
  }

  async function saveServerModelConfig (section) {
    try {
      const body = section === 'semantic'
        ? { semantic: { base_url: semanticBaseUrl.value, api_key: semanticApiKey.value, model: selectedSemanticModel.value } }
        : { vlm: { base_url: modelBaseUrl.value, api_key: modelApiKey.value, model: selectedModel.value, model_type: selectedModelType.value, temperature: modelTemperature.value, max_tokens: modelMaxTokens.value } }
      const resp = await apiFetch('/api/model_config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (resp.ok) applyServerMaskFlags((await resp.json()).result)
    } catch (err) {
      console.warn('[settings] failed to save server model config', err)
    }
  }

  async function loadServerModelConfig () {
    try {
      const resp = await apiFetch('/api/model_config')
      if (!resp.ok) return
      const r = (await resp.json()).result || {}
      if (r.vlm) {
        if (typeof r.vlm.base_url === 'string' && r.vlm.base_url) modelBaseUrl.value = r.vlm.base_url
        if (typeof r.vlm.model === 'string' && r.vlm.model) selectedModel.value = r.vlm.model
        if (typeof r.vlm.temperature === 'number') modelTemperature.value = r.vlm.temperature
        if (typeof r.vlm.max_tokens === 'number') modelMaxTokens.value = r.vlm.max_tokens
      }
      if (r.semantic) {
        if (typeof r.semantic.base_url === 'string' && r.semantic.base_url) semanticBaseUrl.value = r.semantic.base_url
        if (typeof r.semantic.model === 'string' && r.semantic.model) selectedSemanticModel.value = r.semantic.model
      }
      applyServerMaskFlags(r)
    } catch (err) {
      console.warn('[settings] failed to load server model config', err)
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

  watch(selectedModel, () => { if (modelBaseUrl.value) saveServerModelConfig('vlm') })
  watch(selectedSemanticModel, () => { if (semanticBaseUrl.value) saveServerModelConfig('semantic') })

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
    loadServerModelConfig,
    saveServerModelConfig,
    vlmHasSavedKey,
    semanticHasSavedKey,
    vlmConnStatus,
    semanticConnStatus,
  }
}
