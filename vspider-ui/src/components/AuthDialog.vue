<script setup>
import { ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { apiFetch } from '../api/client.js'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  profileOptions: { type: Array, default: () => [] },
  fallbackUrl: { type: String, default: '' },
})

const emit = defineEmits([
  'update:modelValue',
  'use-profile',
  'reload-profiles',
  'log',
])

const authLoginUrl = ref('')
const authProfileName = ref('')
const isAuthRecording = ref(false)

const startManualAuth = async () => {
  const target = (authLoginUrl.value || props.fallbackUrl).trim()
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
    const response = await apiFetch('/api/auth/manual/start', {
      method: 'POST',
      body: formData,
    })
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.message || '打开人工登录窗口失败')
    }
    isAuthRecording.value = true
    authProfileName.value = result.profile || authProfileName.value
    emit('use-profile', result.profile)
    ElMessage.success('登录窗口已打开')
    emit('log', `[AUTH] 登录窗口已打开，完成登录后点击保存: ${result.profile}`)
  } catch (err) {
    ElMessage.error(`打开登录窗口失败: ${String(err)}`)
    emit('log', `[ERROR] 打开登录窗口失败: ${String(err)}`)
  }
}

const saveManualAuth = async () => {
  try {
    const response = await apiFetch('/api/auth/manual/save', {
      method: 'POST',
    })
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.message || '保存登录态失败')
    }
    isAuthRecording.value = false
    emit('use-profile', result.profile)
    emit('reload-profiles')
    ElMessage.success('登录态已保存')
    emit('log', `[AUTH] ${result.message}`)
  } catch (err) {
    ElMessage.error(`保存登录态失败: ${String(err)}`)
    emit('log', `[ERROR] 保存登录态失败: ${String(err)}`)
  }
}

const cancelManualAuth = async () => {
  try {
    const response = await apiFetch('/api/auth/manual/cancel', {
      method: 'POST',
    })
    const result = await response.json()
    if (!response.ok || result.status !== 'success') {
      throw new Error(result.message || '取消登录态录制失败')
    }
    isAuthRecording.value = false
    ElMessage.info('已取消登录态录制')
  } catch (err) {
    ElMessage.error(`取消失败: ${String(err)}`)
  }
}

const useProfile = (name) => {
  emit('use-profile', name)
}

const authProfileOptionLabel = (item) => {
  if (!item) return ''
  let label = item.name || ''
  if (item.cookies) label += ` (${item.cookies} cookies)`
  return label
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="身份管理"
    width="560px"
    class="auth-dialog"
    destroy-on-close
    @update:model-value="emit('update:modelValue', $event)"
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
        <label>保存为 Profile</label>
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
          <el-button text size="small" :icon="Refresh" @click="emit('reload-profiles')">
            刷新
          </el-button>
        </div>
        <div v-if="profileOptions.length" class="profile-tags">
          <button
            v-for="profileItem in profileOptions"
            :key="profileItem.name"
            type="button"
            @click="useProfile(profileItem.name)"
          >
            <span>{{ profileItem.name }}</span>
            <small>
              {{ profileItem.cookies }} cookies
              <template v-if="profileItem.cf_clearance">
                · CF
                <template v-if="profileItem.cf_clearance_expires_in_hours">
                  ~{{ Math.round(profileItem.cf_clearance_expires_in_hours) }}h
                </template>
              </template>
            </small>
          </button>
        </div>
        <p v-else class="empty-profile">暂无 profile，先打开登录窗口并保存。</p>
      </div>
    </div>
  </el-dialog>
</template>

<style scoped>
@import '../styles/auth-dialog.css';
</style>
