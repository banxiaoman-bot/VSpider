<script setup>
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'

const url = ref('')
const prompt = ref('')
const selectedFile = ref(null)
const isRunning = ref(false)
const logs = ref([])
const currentImageBase64 = ref('')
const terminalRef = ref(null)
const wsStatus = ref('connecting')

let socket = null
let reconnectTimer = null
let isUnmounted = false

const appendLog = async (message) => {
  logs.value.push(message)
  await scrollToBottom()
}

const scrollToBottom = async () => {
  await nextTick()
  const el = terminalRef.value
  if (!el) return
  el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
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
        const text = payload.message || (payload.success ? '任务执行完成' : '任务执行结束')
        await appendLog(`[DONE] ${text}`)
      }
    } catch (err) {
      await appendLog(`[WARN] 无法解析消息: ${String(err)}`)
    }
  }

  socket.onclose = () => {
    wsStatus.value = 'disconnected'
    appendLog('[SYSTEM] WebSocket disconnected')
    if (!isUnmounted) {
      if (reconnectTimer) {
        clearTimeout(reconnectTimer)
      }
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
  connectWebSocket()
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
  <main class="min-h-screen h-screen overflow-hidden bg-slate-950 text-white p-6">
    <div class="grid h-full grid-cols-[40%_60%] gap-4">
      <section class="h-full min-h-0">
        <div class="flex h-full flex-col gap-5 rounded-xl bg-white p-6 shadow-md">
          <div class="flex items-center gap-3">
            <div class="grid h-10 w-10 place-items-center rounded-lg bg-slate-900 text-cyan-300">
              <svg viewBox="0 0 24 24" class="h-6 w-6" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M4 6h16M4 12h10M4 18h7" />
                <path d="m15 14 5 4-5 4" />
              </svg>
            </div>
            <div>
              <h1 class="text-lg font-semibold text-slate-900">VSpider Control Center</h1>
              <p class="text-xs text-slate-500">任务编排与执行入口</p>
            </div>
          </div>

          <div class="flex flex-col gap-2">
            <label class="text-sm font-medium text-slate-700">目标 URL</label>
            <el-input v-model="url" clearable placeholder="https://example.com" />
          </div>

          <div class="flex flex-col gap-2">
            <label class="text-sm font-medium text-slate-700">业务指令 (Prompt)</label>
            <el-input
              v-model="prompt"
              type="textarea"
              :rows="5"
              resize="none"
              placeholder="描述你希望 VSpider 执行的业务任务"
            />
          </div>

          <div class="flex min-h-0 flex-1 flex-col gap-2">
            <label class="text-sm font-medium text-slate-700">文件挂载（可选）</label>
            <el-upload
              drag
              class="w-full"
              :auto-upload="false"
              :limit="1"
              accept=".xlsx,.xls,.csv"
              :on-change="handleUploadChange"
              :on-remove="handleUploadRemove"
            >
              <div class="py-4 text-sm text-slate-600">拖拽 Excel/CSV 到此处，或点击选择文件（非批量任务可留空）</div>
            </el-upload>
            <p class="text-xs text-slate-500">
              当前文件：{{ selectedFile ? selectedFile.name : '未选择文件，当前为单任务模式' }}
            </p>
          </div>

          <div class="mt-auto flex flex-col gap-3">
            <el-button type="primary" class="!w-full" :loading="isRunning" @click="submitTask">
              🚀 开始执行
            </el-button>
            <el-button type="danger" class="!ml-0 !w-full" @click="forceStop">
              ⏹️ 强制终止
            </el-button>
          </div>
        </div>
      </section>

      <section class="h-full min-h-0">
        <div class="flex h-full flex-col gap-4">
          <div class="h-1/2 overflow-hidden rounded-xl border border-green-500/30 bg-black/80 p-4 shadow-md">
            <div class="mb-2 flex items-center justify-between">
              <h2 class="text-sm font-semibold text-green-300">Live Terminal</h2>
              <span class="text-xs text-green-400/70">{{ isRunning ? 'RUNNING' : 'IDLE' }} · {{ wsStatus }}</span>
            </div>
            <div
              ref="terminalRef"
              class="h-[calc(100%-1.5rem)] overflow-auto rounded-md border border-green-500/20 bg-black/40 p-3 text-sm font-mono text-green-400"
            >
              <template v-if="logs.length">
                <p v-for="(line, idx) in logs" :key="idx" class="leading-6">{{ line }}</p>
              </template>
              <p v-else class="text-gray-500">等待日志流...</p>
            </div>
          </div>

          <div class="relative h-1/2 overflow-hidden rounded-xl bg-black p-2 shadow-md">
            <h2 class="absolute left-4 top-3 z-10 text-sm font-semibold text-gray-300">Visual Preview</h2>
            <div class="flex h-full w-full items-center justify-center">
              <img
                v-if="currentImageBase64"
                :src="currentImageBase64"
                alt="实时画面"
                class="h-full w-full object-contain"
              />
              <div v-else class="h-[88%] w-[92%] animate-pulse rounded-lg border border-gray-800 bg-gray-900" />
            </div>
          </div>
        </div>
      </section>
    </div>
  </main>
</template>
