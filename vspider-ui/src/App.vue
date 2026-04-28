<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import 'element-plus/theme-chalk/dark/css-vars.css'
import {
  Close,
  Monitor,
  Refresh,
  Setting,
  UploadFilled,
  VideoPlay,
} from '@element-plus/icons-vue'

const url = ref('')
const prompt = ref('')
const selectedAuthProfiles = ref([])
const selectedFile = ref(null)
const isRunning = ref(false)
const logs = ref([])
const currentImageBase64 = ref('')
const terminalRef = ref(null)
const wsStatus = ref('connecting')

const authDialogOpen = ref(false)
const authLoginUrl = ref('')
const authProfileName = ref('')
const authProfileOptions = ref([])
const isAuthRecording = ref(false)
const selectedModel = ref('backend-default')
const selectedSemanticModel = ref('backend-default')
const modelSettingsOpen = ref(false)
const modelTemperature = ref(0.1)
const modelMaxTokens = ref(4096)
const modelBaseUrl = ref('')
const modelApiKey = ref('')
const isHumanInterventionRequired = ref(false)
const humanInterventionReason = ref('')
const activeBottomTab = ref('terminal')
const artifactList = ref([])
const hasNewArtifacts = ref(false)

let socket = null
let reconnectTimer = null
let isUnmounted = false

const authProfileNames = computed(() =>
  authProfileOptions.value.map((item) => item.name).filter(Boolean),
)
const selectedModelType = computed(() =>
  ['deepseek-chat'].includes(selectedModel.value) ? 'text' : 'vl',
)

const appendLog = async (message) => {
  logs.value.push(message)
  await scrollToBottom()
}

const scrollToBottom = async () => {
  await nextTick()
  const el = terminalRef.value
  if (!el) return
  if (typeof el.setScrollTop === 'function') {
    el.setScrollTop(Number.MAX_SAFE_INTEGER)
    return
  }
  const wrap = el.wrapRef || el
  if (wrap) wrap.scrollTop = wrap.scrollHeight
}

const connectWebSocket = () => {
  if (isUnmounted) return
  if (socket && socket.readyState === WebSocket.OPEN) return
  if (socket && socket.readyState === WebSocket.CONNECTING) return

  wsStatus.value = 'connecting'
  socket = new WebSocket('ws://localhost:8000/ws/logs')

  socket.onopen = () => {
    wsStatus.value = 'connected'
    appendLog('[SYSTEM] WebSocket connected')
  }

  socket.onmessage = async (event) => {
    try {
      const payload = JSON.parse(event.data)
      if (payload.type === 'log') {
        const level = payload.level ? String(payload.level).toUpperCase() : 'INFO'
        const content = payload.content || ''
        await appendLog(`[${level}] ${content}`)
        return
      }

      if (payload.type === 'image' || payload.type === 'screenshot') {
        currentImageBase64.value = payload.data || ''
        return
      }

      if (payload.type === 'done') {
        isRunning.value = false
        isHumanInterventionRequired.value = false
        const text = payload.message || (payload.success ? '任务执行完成' : '任务执行结束')
        await appendLog(`[DONE] ${text}`)
        return
      }

      if (payload.type === 'status') {
        if (payload.status === 'new_artifact') {
          await fetchArtifacts()
          if (activeBottomTab.value !== 'artifacts') {
            hasNewArtifacts.value = true
          }
          await appendLog(`[ARTIFACT] ${payload.filename || 'new file'} ready`)
        }
        if (payload.status === 'human_intervention') {
          isHumanInterventionRequired.value = true
          humanInterventionReason.value = payload.reason || 'Agent 遇到需要人工处理的障碍'
          await appendLog(`[HITL] ${humanInterventionReason.value}`)
        }
        if (payload.status === 'human_resumed') {
          isHumanInterventionRequired.value = false
          humanInterventionReason.value = ''
          await appendLog('[HITL] Agent resumed')
        }
      }
    } catch (err) {
      await appendLog(`[WARN] 无法解析消息: ${String(err)}`)
    }
  }

  socket.onclose = () => {
    wsStatus.value = 'disconnected'
    appendLog('[SYSTEM] WebSocket disconnected')
    if (!isUnmounted) {
      if (reconnectTimer) clearTimeout(reconnectTimer)
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null
        connectWebSocket()
      }, 2000)
    }
  }

  socket.onerror = () => {
    wsStatus.value = 'error'
    appendLog('[ERROR] WebSocket error')
  }
}

const handleUploadChange = (uploadFile, uploadFiles) => {
  if (!uploadFile || !uploadFile.raw) {
    selectedFile.value = null
    return
  }

  selectedFile.value = uploadFile.raw
  if (uploadFiles.length > 1) {
    uploadFiles.splice(0, uploadFiles.length - 1)
  }
}

const handleUploadRemove = () => {
  selectedFile.value = null
}

const loadAuthProfiles = async () => {
  try {
    const response = await fetch('http://localhost:8000/api/auth/profiles')
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.message || '加载 Auth Profiles 失败')
    }
    authProfileOptions.value = result.profiles || []
  } catch (err) {
    await appendLog(`[WARN] 加载 Auth Profiles 失败: ${String(err)}`)
  }
}

const fetchArtifacts = async () => {
  try {
    const response = await fetch('http://localhost:8000/api/artifacts')
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.message || '加载产出文件失败')
    }
    artifactList.value = result.files || []
    if (activeBottomTab.value === 'artifacts') {
      hasNewArtifacts.value = false
    }
  } catch (err) {
    await appendLog(`[WARN] 加载产出文件失败: ${String(err)}`)
  }
}

const useAuthProfile = (name) => {
  if (!name) return
  if (!selectedAuthProfiles.value.includes(name)) {
    selectedAuthProfiles.value = [...selectedAuthProfiles.value, name]
  }
}

const startManualAuth = async () => {
  const target = (authLoginUrl.value || url.value).trim()
  if (!target) {
    ElMessage.warning('请先填写登录 URL 或目标 URL')
    return
  }

  const formData = new FormData()
  formData.append('target_url', target)
  if (authProfileName.value.trim()) {
    formData.append('profile', authProfileName.value.trim())
  }

  try {
    const response = await fetch('http://localhost:8000/api/auth/manual/start', {
      method: 'POST',
      body: formData,
    })
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.message || '打开人工登录窗口失败')
    }
    isAuthRecording.value = true
    authProfileName.value = result.profile || authProfileName.value
    useAuthProfile(result.profile)
    ElMessage.success('登录窗口已打开')
    await appendLog(`[AUTH] 登录窗口已打开，完成登录后点击保存: ${result.profile}`)
  } catch (err) {
    ElMessage.error(`打开登录窗口失败: ${String(err)}`)
    await appendLog(`[ERROR] 打开登录窗口失败: ${String(err)}`)
  }
}

const saveManualAuth = async () => {
  try {
    const response = await fetch('http://localhost:8000/api/auth/manual/save', {
      method: 'POST',
    })
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.message || '保存登录态失败')
    }
    isAuthRecording.value = false
    useAuthProfile(result.profile)
    await loadAuthProfiles()
    ElMessage.success('登录态已保存')
    await appendLog(`[AUTH] ${result.message}`)
  } catch (err) {
    ElMessage.error(`保存登录态失败: ${String(err)}`)
    await appendLog(`[ERROR] 保存登录态失败: ${String(err)}`)
  }
}

const cancelManualAuth = async () => {
  try {
    const response = await fetch('http://localhost:8000/api/auth/manual/cancel', {
      method: 'POST',
    })
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.message || '取消登录态录制失败')
    }
    isAuthRecording.value = false
    ElMessage.info('已取消登录态录制')
    await appendLog(`[AUTH] ${result.message}`)
  } catch (err) {
    ElMessage.error(`取消失败: ${String(err)}`)
    await appendLog(`[ERROR] 取消登录态录制失败: ${String(err)}`)
  }
}

const resumeAgentExecution = async () => {
  try {
    const response = await fetch('http://localhost:8000/api/human/resume', {
      method: 'POST',
    })
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.message || '恢复执行失败')
    }
    isHumanInterventionRequired.value = false
    humanInterventionReason.value = ''
    ElMessage.success('已发送恢复执行信号')
    await appendLog('[HITL] Resume signal sent')
  } catch (err) {
    ElMessage.error(`恢复执行失败: ${String(err)}`)
    await appendLog(`[ERROR] 恢复执行失败: ${String(err)}`)
  }
}

const submitTask = async () => {
  if (!url.value.trim() || !prompt.value.trim()) {
    ElMessage.warning('请先填写 URL 和 Prompt')
    return
  }

  isRunning.value = true
  logs.value = []
  currentImageBase64.value = ''

  const formData = new FormData()
  formData.append('target_url', url.value.trim())
  formData.append('goal', prompt.value.trim())
  if (selectedModel.value !== 'backend-default') {
    formData.append('vlm_model', selectedModel.value)
  }
  if (selectedSemanticModel.value !== 'backend-default') {
    formData.append('semantic_model', selectedSemanticModel.value)
  }
  formData.append('vlm_model_type', selectedModelType.value)
  formData.append('vlm_temperature', String(modelTemperature.value))
  formData.append('vlm_max_tokens', String(modelMaxTokens.value))
  if (modelBaseUrl.value.trim()) {
    formData.append('vlm_base_url', modelBaseUrl.value.trim())
  }
  if (modelApiKey.value.trim()) {
    formData.append('vlm_api_key', modelApiKey.value.trim())
  }
  if (selectedAuthProfiles.value.length) {
    formData.append('auth_profiles', selectedAuthProfiles.value.join(','))
  }
  if (selectedFile.value) {
    formData.append('file', selectedFile.value)
  }

  try {
    const response = await fetch('http://localhost:8000/api/start_batch', {
      method: 'POST',
      body: formData,
    })

    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.message || '任务启动失败')
    }

    await appendLog('[SYSTEM] 任务已提交，等待后端执行...')
    ElMessage.success('任务已在后台启动')
  } catch (err) {
    isRunning.value = false
    ElMessage.error(`提交失败: ${String(err)}`)
    await appendLog(`[ERROR] 提交失败: ${String(err)}`)
  }
}

const forceStop = async () => {
  try {
    const response = await fetch('http://localhost:8000/api/stop_batch', {
      method: 'POST',
    })
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.message || '停止请求失败')
    }
    isRunning.value = false
    ElMessage.warning('正在强制终止后台任务...')
    await appendLog('[WARN] 已发送强制终止请求')
  } catch (err) {
    ElMessage.error(`强制终止失败: ${String(err)}`)
    await appendLog(`[ERROR] 强制终止失败: ${String(err)}`)
  }
}

onMounted(() => {
  document.documentElement.classList.add('dark')
  connectWebSocket()
  loadAuthProfiles()
  fetchArtifacts()
})

onUnmounted(() => {
  isUnmounted = true
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
  if (socket) {
    socket.close()
    socket = null
  }
})
</script>

<template>
  <main class="app-shell">
    <section class="control-panel vspider-panel">
      <header class="brand-header">
        <div class="brand-mark">
          <Monitor class="brand-icon" />
        </div>
        <div>
          <h1>VSpider Control Center</h1>
          <p>任务编排与执行入口</p>
        </div>
      </header>

      <div class="control-scroll">
        <div class="field-group">
          <label>目标 URL</label>
          <el-input
            v-model="url"
            clearable
            :disabled="isRunning"
            placeholder="https://example.com"
          />
        </div>

        <div class="field-group">
          <label>业务指令</label>
          <el-input
            v-model="prompt"
            type="textarea"
            resize="none"
            :autosize="{ minRows: 4, maxRows: 8 }"
            :disabled="isRunning"
            placeholder="描述你希望 VSpider 执行的业务任务"
          />
        </div>

        <div class="field-group model-center">
          <div class="field-title-row">
            <label>双脑调度中心</label>
            <el-popover
              v-model:visible="modelSettingsOpen"
              placement="right-start"
              width="360"
              trigger="click"
            >
              <template #reference>
                <el-button text size="small" :icon="Setting">
                  Advanced
                </el-button>
              </template>
              <div class="model-popover">
                <label>Temperature: {{ modelTemperature }}</label>
                <el-slider
                  v-model="modelTemperature"
                  :min="0"
                  :max="2"
                  :step="0.1"
                  :disabled="isRunning"
                />
                <label>Max Tokens</label>
                <el-input-number
                  v-model="modelMaxTokens"
                  :min="512"
                  :max="32768"
                  :step="512"
                  :disabled="isRunning"
                  class="full-width"
                />
                <label>Base URL Override</label>
                <el-input
                  v-model="modelBaseUrl"
                  clearable
                  :disabled="isRunning"
                  placeholder="http://localhost:8000/v1"
                />
                <label>API Key Override</label>
                <el-input
                  v-model="modelApiKey"
                  clearable
                  show-password
                  :disabled="isRunning"
                  placeholder="empty = backend default"
                />
              </div>
            </el-popover>
          </div>

          <el-select
            v-model="selectedModel"
            :disabled="isRunning"
            class="full-width"
            placeholder="选择调度模型"
          >
            <el-option-group label="视觉多模态大模型 (VL)">
              <el-option value="backend-default" label="Backend Default" />
              <el-option value="qwen3-vl-plus" label="Qwen-VL-Plus" />
              <el-option value="local-74b-vl" label="内网本地 74B VL 模型" />
            </el-option-group>
            <el-option-group label="纯文本逻辑模型 (Text)">
              <el-option value="deepseek-chat" label="DeepSeek-V3" />
            </el-option-group>
          </el-select>

          <el-select
            v-model="selectedSemanticModel"
            :disabled="isRunning"
            class="full-width"
            placeholder="Semantic model for Planner / Extract / Reflector"
          >
            <el-option value="backend-default" label="Semantic Backend Default" />
            <el-option value="deepseek-chat" label="DeepSeek-V3" />
            <el-option value="qwen3-vl-plus" label="Qwen-VL-Plus" />
            <el-option value="local-74b-vl" label="Local 74B VL" />
          </el-select>

          <p v-if="selectedModelType === 'text'" class="model-warning">
            当前为纯文本模型，将自动剥离图像，仅依赖 AX Tree 执行任务。
          </p>
        </div>

        <div class="field-group">
          <div class="field-title-row">
            <label>身份选择</label>
            <div class="field-actions">
              <el-button
                text
                size="small"
                :icon="Refresh"
                @click="loadAuthProfiles"
              >
                Refresh
              </el-button>
              <el-button
                text
                size="small"
                :icon="Setting"
                @click="authDialogOpen = true"
              >
                Manage
              </el-button>
            </div>
          </div>

          <el-select
            v-model="selectedAuthProfiles"
            multiple
            filterable
            allow-create
            collapse-tags
            collapse-tags-tooltip
            :disabled="isRunning"
            placeholder="选择或输入 Auth Profile"
            class="full-width"
          >
            <el-option
              v-for="name in authProfileNames"
              :key="name"
              :label="name"
              :value="name"
            />
          </el-select>
        </div>

        <el-collapse class="advanced-collapse">
          <el-collapse-item title="批量文件（可选）" name="file">
            <el-upload
              drag
              class="compact-upload"
              :auto-upload="false"
              :limit="1"
              :disabled="isRunning"
              accept=".xlsx,.xls,.csv"
              :on-change="handleUploadChange"
              :on-remove="handleUploadRemove"
            >
              <el-icon class="upload-icon">
                <UploadFilled />
              </el-icon>
              <div class="upload-copy">拖拽 Excel/CSV 到此处，或点击选择文件</div>
            </el-upload>
            <p class="file-status">
              当前文件：{{ selectedFile ? selectedFile.name : '未选择文件，当前为单任务模式' }}
            </p>
          </el-collapse-item>
        </el-collapse>
      </div>

      <footer class="action-footer">
        <el-button
          type="primary"
          class="run-button"
          :icon="VideoPlay"
          :loading="isRunning"
          @click="submitTask"
        >
          开始执行
        </el-button>
        <el-button
          class="stop-button"
          :icon="Close"
          :disabled="!isRunning"
          @click="forceStop"
        >
          强制终止
        </el-button>
      </footer>
    </section>

    <section class="monitor-panel">
      <div class="preview-panel vspider-panel">
        <div class="panel-title">
          <div>
            <h2>Visual Preview</h2>
            <p>Agent 实时视觉画面</p>
          </div>
          <span class="live-indicator">
            <i />
            LIVE
          </span>
        </div>
        <div class="preview-stage">
          <img
            v-if="currentImageBase64"
            :src="currentImageBase64"
            alt="实时画面"
          />
          <div v-else class="preview-placeholder">
            等待首帧画面
          </div>
          <div v-if="isHumanInterventionRequired" class="hitl-overlay">
            <div class="hitl-card">
              <div class="hitl-title">HITL REQUIRED</div>
              <div class="hitl-copy">
                Agent is paused. Complete captcha, slider, QR scan, or 2FA in the browser window.
              </div>
              <div v-if="humanInterventionReason" class="hitl-reason">
                {{ humanInterventionReason }}
              </div>
              <el-button
                type="success"
                size="large"
                class="resume-button"
                @click="resumeAgentExecution"
              >
                Resume Agent
              </el-button>
            </div>
          </div>
        </div>
      </div>

      <div class="terminal-panel vspider-panel">
        <el-tabs
          v-model="activeBottomTab"
          class="bottom-tabs"
          @tab-change="(name) => { if (name === 'artifacts') hasNewArtifacts = false }"
        >
          <el-tab-pane name="terminal">
            <template #label>
              <span>Live Terminal</span>
            </template>
            <div class="terminal-heading">
              <span class="status-pill" :class="wsStatus">
                {{ isRunning ? 'RUNNING' : 'IDLE' }} · {{ wsStatus }}
              </span>
            </div>
            <el-scrollbar ref="terminalRef" class="terminal-scroll">
              <template v-if="logs.length">
                <p v-for="(line, idx) in logs" :key="idx" class="log-line">
                  {{ line }}
                </p>
              </template>
              <p v-else class="empty-log">等待日志流...</p>
            </el-scrollbar>
          </el-tab-pane>

          <el-tab-pane name="artifacts">
            <template #label>
              <el-badge :is-dot="hasNewArtifacts" class="artifact-badge">
                <span>Artifacts</span>
              </el-badge>
            </template>
            <div class="artifact-toolbar">
              <el-button size="small" plain :icon="Refresh" @click="fetchArtifacts">
                Refresh
              </el-button>
            </div>
            <el-table
              :data="artifactList"
              height="190"
              class="artifact-table"
              header-cell-class-name="dark-table-header"
              empty-text="No artifacts yet"
            >
              <el-table-column prop="name" label="File" show-overflow-tooltip />
              <el-table-column prop="size_kb" label="KB" width="84" />
              <el-table-column prop="created_at" label="Created" width="168" />
              <el-table-column label="Action" width="92">
                <template #default="scope">
                  <a
                    :href="`http://localhost:8000${scope.row.url}`"
                    download
                    class="download-link"
                  >
                    Download
                  </a>
                </template>
              </el-table-column>
            </el-table>
          </el-tab-pane>
        </el-tabs>
      </div>
    </section>

    <el-dialog
      v-model="authDialogOpen"
      title="Auth Manager"
      width="560px"
      class="auth-dialog"
      destroy-on-close
    >
      <div class="auth-dialog-body">
        <div class="field-group">
          <label>Login URL</label>
          <el-input
            v-model="authLoginUrl"
            clearable
            placeholder="留空则使用目标 URL"
          />
        </div>

        <div class="field-group">
          <label>Save as Profile</label>
          <el-input
            v-model="authProfileName"
            clearable
            placeholder="例如 zhihu_default / oa_test01"
          />
        </div>

        <div class="auth-actions">
          <el-button
            type="primary"
            plain
            :loading="isAuthRecording"
            @click="startManualAuth"
          >
            打开登录窗口
          </el-button>
          <el-button
            type="success"
            :disabled="!isAuthRecording"
            @click="saveManualAuth"
          >
            保存 Profile
          </el-button>
          <el-button
            type="warning"
            plain
            :disabled="!isAuthRecording"
            @click="cancelManualAuth"
          >
            取消
          </el-button>
        </div>

        <div class="profile-list">
          <div class="field-title-row">
            <label>已保存 Profiles</label>
            <el-button text size="small" :icon="Refresh" @click="loadAuthProfiles">
              Refresh
            </el-button>
          </div>
          <div v-if="authProfileOptions.length" class="profile-tags">
            <button
              v-for="profileItem in authProfileOptions"
              :key="profileItem.name"
              type="button"
              @click="useAuthProfile(profileItem.name)"
            >
              <span>{{ profileItem.name }}</span>
              <small>{{ profileItem.cookies }} cookies</small>
            </button>
          </div>
          <p v-else class="empty-profile">暂无 profile，先打开登录窗口并保存。</p>
        </div>
      </div>
    </el-dialog>
  </main>
</template>

<style scoped>
:global(html.dark),
:global(body) {
  min-height: 100%;
  background: #101014;
}

:global(body) {
  margin: 0;
}

.app-shell {
  display: grid;
  grid-template-columns: minmax(360px, 35%) minmax(0, 1fr);
  gap: 18px;
  height: 100vh;
  overflow: hidden;
  padding: 22px;
  background:
    linear-gradient(180deg, rgba(112, 192, 255, 0.08), transparent 32%),
    #101014;
  color: #f4f7fb;
}

.vspider-panel {
  background: #18181c;
  border: 1px solid #2d2d30;
  border-radius: 8px;
  box-shadow: 0 18px 42px rgba(0, 0, 0, 0.32);
}

.control-panel {
  display: flex;
  min-height: 0;
  overflow: hidden;
  flex-direction: column;
}

.brand-header {
  display: flex;
  gap: 14px;
  align-items: center;
  padding: 22px 22px 16px;
  border-bottom: 1px solid #2d2d30;
}

.brand-mark {
  display: grid;
  width: 42px;
  height: 42px;
  place-items: center;
  border-radius: 8px;
  color: #63e2b7;
  background: #101014;
  border: 1px solid rgba(99, 226, 183, 0.28);
}

.brand-icon {
  width: 22px;
  height: 22px;
}

.brand-header h1,
.panel-title h2 {
  margin: 0;
  font-size: 18px;
  line-height: 1.25;
  letter-spacing: 0;
}

.brand-header p,
.panel-title p {
  margin: 4px 0 0;
  color: #8f96a3;
  font-size: 12px;
}

.control-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 18px 22px;
}

.field-group {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 16px;
}

.field-group label,
.field-title-row label {
  color: #d8dee9;
  font-size: 13px;
  font-weight: 600;
}

.field-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.field-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}

.full-width {
  width: 100%;
}

.model-center {
  padding: 12px;
  border: 1px solid rgba(99, 226, 183, 0.16);
  border-radius: 8px;
  background: rgba(99, 226, 183, 0.035);
}

.model-popover {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.model-popover label {
  color: #d8dee9;
  font-size: 12px;
  font-weight: 600;
}

.model-warning {
  margin: 0;
  color: #f6c177;
  font-size: 12px;
  line-height: 1.45;
}

.advanced-collapse {
  --el-collapse-border-color: #2d2d30;
  --el-collapse-header-bg-color: transparent;
  --el-collapse-content-bg-color: transparent;
  --el-collapse-header-text-color: #d8dee9;
  --el-collapse-content-text-color: #d8dee9;
  margin-top: 2px;
}

.upload-icon {
  color: #63e2b7;
  font-size: 26px;
}

.upload-copy,
.file-status {
  color: #9aa4b2;
  font-size: 13px;
}

.file-status {
  margin: 8px 0 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.action-footer {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  padding: 16px 22px 20px;
  border-top: 1px solid #2d2d30;
  background: #18181c;
}

.run-button {
  --el-button-bg-color: #63e2b7;
  --el-button-border-color: #63e2b7;
  --el-button-hover-bg-color: #78edc6;
  --el-button-hover-border-color: #78edc6;
  --el-button-text-color: #06110d;
  font-weight: 700;
}

.stop-button {
  --el-button-bg-color: rgba(232, 128, 128, 0.1);
  --el-button-border-color: #e88080;
  --el-button-text-color: #e88080;
  --el-button-hover-bg-color: #e88080;
  --el-button-hover-border-color: #e88080;
  --el-button-hover-text-color: #101014;
}

.stop-button:hover {
  box-shadow: 0 0 15px rgba(239, 68, 68, 0.45);
}

.monitor-panel {
  display: grid;
  min-height: 0;
  grid-template-rows: minmax(0, 2fr) minmax(180px, 1fr);
  gap: 18px;
}

.preview-panel,
.terminal-panel {
  display: flex;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
}

.panel-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 18px 12px;
}

.panel-title.compact {
  padding-bottom: 10px;
}

.live-indicator,
.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  color: #63e2b7;
  font-size: 12px;
  font-weight: 700;
}

.live-indicator i {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: #63e2b7;
  box-shadow: 0 0 0 0 rgba(99, 226, 183, 0.58);
  animation: pulse-live 1.6s infinite;
}

.status-pill {
  padding: 4px 8px;
  border-radius: 999px;
  background: rgba(99, 226, 183, 0.08);
  border: 1px solid rgba(99, 226, 183, 0.2);
}

.status-pill.error,
.status-pill.disconnected {
  color: #e88080;
  background: rgba(232, 128, 128, 0.08);
  border-color: rgba(232, 128, 128, 0.24);
}

.preview-stage {
  display: grid;
  min-height: 0;
  flex: 1;
  margin: 0 18px 18px;
  overflow: hidden;
  place-items: center;
  border-radius: 8px;
  background: #0d1118;
  border: 1px solid #2d2d30;
  position: relative;
}

.preview-stage img {
  width: 100%;
  height: 100%;
  object-fit: contain;
}

.preview-placeholder {
  display: grid;
  width: 100%;
  height: 100%;
  place-items: center;
  color: #626b79;
  font-size: 14px;
}

.hitl-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 28px;
  background: rgba(127, 29, 29, 0.82);
  backdrop-filter: blur(6px);
}

.hitl-card {
  max-width: 560px;
  text-align: center;
  color: #fff;
}

.hitl-title {
  margin-bottom: 14px;
  color: #fff;
  font-size: 26px;
  font-weight: 800;
  animation: pulse-live 1.2s infinite;
}

.hitl-copy {
  margin-bottom: 18px;
  color: #f5d0d0;
  font-size: 15px;
  line-height: 1.7;
}

.hitl-reason {
  margin: 0 auto 22px;
  padding: 10px 12px;
  color: #fecaca;
  background: rgba(0, 0, 0, 0.24);
  border: 1px solid rgba(254, 202, 202, 0.24);
  border-radius: 8px;
  font-size: 13px;
}

.resume-button {
  box-shadow: 0 0 24px rgba(34, 197, 94, 0.38);
}

.terminal-scroll {
  flex: 1;
  min-height: 0;
  height: 190px;
  margin: 0;
  padding: 12px;
  border-radius: 8px;
  background: #080a0d;
  border: 1px solid rgba(99, 226, 183, 0.18);
  font-family: Consolas, 'JetBrains Mono', 'SFMono-Regular', monospace;
}

.bottom-tabs {
  display: flex;
  min-height: 0;
  height: 100%;
  flex-direction: column;
  padding: 8px 14px 14px;
}

:deep(.bottom-tabs .el-tabs__header) {
  margin: 0 0 8px;
}

:deep(.bottom-tabs .el-tabs__content) {
  min-height: 0;
  flex: 1;
}

:deep(.bottom-tabs .el-tab-pane) {
  height: 100%;
}

.terminal-heading,
.artifact-toolbar {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 8px;
}

.artifact-badge {
  line-height: 1;
}

.artifact-table {
  --el-table-bg-color: transparent;
  --el-table-tr-bg-color: transparent;
  --el-table-header-bg-color: #1e293b;
  --el-table-border-color: #334155;
  --el-table-text-color: #94a3b8;
  --el-table-header-text-color: #f1f5f9;
  background: transparent;
}

:deep(.artifact-table .el-table__inner-wrapper::before) {
  display: none;
}

:deep(.artifact-table th.el-table__cell),
:deep(.artifact-table tr),
:deep(.artifact-table td.el-table__cell) {
  background: transparent;
  border-bottom-color: #334155;
}

:deep(.artifact-table .dark-table-header) {
  background: #1e293b !important;
  color: #f1f5f9;
}

.download-link {
  color: #70c0ff;
  font-weight: 700;
  text-decoration: none;
}

.download-link:hover {
  color: #9bd4ff;
}

.log-line,
.empty-log {
  margin: 0;
  color: #00e676;
  font-size: 13px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
}

.empty-log {
  color: #626b79;
}

.auth-dialog-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.auth-actions {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 10px;
  margin: 4px 0 16px;
}

.profile-list {
  padding-top: 14px;
  border-top: 1px solid #2d2d30;
}

.profile-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
}

.profile-tags button {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 9px;
  color: #d8dee9;
  background: #101014;
  border: 1px solid #2d2d30;
  border-radius: 8px;
  cursor: pointer;
}

.profile-tags button:hover {
  color: #63e2b7;
  border-color: rgba(99, 226, 183, 0.55);
}

.profile-tags small,
.empty-profile {
  color: #8f96a3;
  font-size: 12px;
}

.empty-profile {
  margin: 10px 0 0;
}

:deep(.el-input__wrapper),
:deep(.el-textarea__inner),
:deep(.el-select__wrapper) {
  background: #101014;
  border-radius: 8px;
  box-shadow: 0 0 0 1px #2d2d30 inset;
}

:deep(.el-input__wrapper.is-focus),
:deep(.el-textarea__inner:focus),
:deep(.el-select__wrapper.is-focused) {
  box-shadow:
    0 0 0 1px #63e2b7 inset,
    0 0 0 3px rgba(99, 226, 183, 0.14);
}

:deep(.compact-upload .el-upload) {
  width: 100%;
}

:deep(.compact-upload .el-upload-dragger) {
  display: flex;
  min-height: 112px;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 18px 14px;
  background: #101014;
  border-color: #2d2d30;
  border-radius: 8px;
}

:deep(.auth-dialog .el-dialog) {
  background: #18181c;
  border: 1px solid #2d2d30;
  border-radius: 8px;
}

@keyframes pulse-live {
  0% {
    box-shadow: 0 0 0 0 rgba(99, 226, 183, 0.58);
  }
  70% {
    box-shadow: 0 0 0 8px rgba(99, 226, 183, 0);
  }
  100% {
    box-shadow: 0 0 0 0 rgba(99, 226, 183, 0);
  }
}

@media (max-width: 1100px) {
  .app-shell {
    grid-template-columns: 1fr;
    overflow-y: auto;
  }

  .control-panel,
  .monitor-panel {
    min-height: 720px;
  }
}
</style>
