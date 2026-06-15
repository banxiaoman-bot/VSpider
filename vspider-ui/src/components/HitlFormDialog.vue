<script setup>
import { ref, watch, computed } from 'vue'

const props = defineProps({
  visible: { type: Boolean, default: false },
  reason: { type: String, default: '' },
  fields: { type: Array, default: () => [] },
  screenshotUrl: { type: String, default: '' },
  loading: { type: Boolean, default: false },
})

const emit = defineEmits(['update:visible', 'submit', 'skip'])

const formValues = ref({})
const screenshotZoomed = ref(false)

watch(
  () => props.fields,
  (fields) => {
    const vals = {}
    for (const f of fields) {
      vals[f.id] = f.value || ''
    }
    formValues.value = vals
  },
  { immediate: true },
)

const fieldLabel = (field) =>
  field.label || field.placeholder || field.name || field.id

const fieldIcon = (field) => {
  const type = String(field.type || '').toLowerCase()
  if (type === 'password') return '🔒'
  if (type === 'email') return '📧'
  if (type === 'tel') return '📞'
  if (type === 'url') return '🔗'
  return '📝'
}

const canSubmit = computed(() =>
  props.fields.some((f) => f.required)
    ? props.fields.filter((f) => f.required).every((f) => String(formValues.value[f.id] || '').trim())
    : Object.values(formValues.value).some((v) => String(v || '').trim()),
)

function handleSubmit() {
  emit('submit', { ...formValues.value })
}

function handleSkip() {
  emit('skip')
}
</script>

<template>
  <el-dialog
    :model-value="visible"
    title="需要人工输入"
    width="520px"
    class="hitl-form-dialog"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    :show-close="false"
    align-center
    @update:model-value="(v) => emit('update:visible', v)"
  >
    <div class="hitl-form-body">
      <div v-if="reason" class="hitl-form-reason">
        <span class="hitl-form-reason-icon">⏸️</span>
        <span>{{ reason }}</span>
      </div>

      <div v-if="screenshotUrl" class="hitl-form-screenshot">
        <img
          :src="screenshotUrl"
          alt="当前页面"
          :class="{ 'is-zoomed': screenshotZoomed }"
          @click="screenshotZoomed = !screenshotZoomed"
        />
        <span class="hitl-form-screenshot-hint">
          {{ screenshotZoomed ? '点击缩小' : '点击放大查看页面详情' }}
        </span>
      </div>

      <div class="hitl-form-fields">
        <div
          v-for="field in fields"
          :key="field.id"
          class="hitl-form-field"
        >
          <label :for="`hitl-${field.id}`">
            <span class="hitl-field-icon">{{ fieldIcon(field) }}</span>
            {{ fieldLabel(field) }}
            <span v-if="field.required" class="hitl-field-required">*</span>
          </label>
          <el-input
            v-if="field.type === 'textarea'"
            :id="`hitl-${field.id}`"
            v-model="formValues[field.id]"
            type="textarea"
            :rows="3"
            :placeholder="field.placeholder || ''"
            :disabled="loading"
          />
          <el-input
            v-else
            :id="`hitl-${field.id}`"
            v-model="formValues[field.id]"
            :type="field.type === 'password' ? 'password' : 'text'"
            :show-password="field.type === 'password'"
            :placeholder="field.placeholder || ''"
            :disabled="loading"
            clearable
          />
          <p v-if="field.hint" class="hitl-field-hint">{{ field.hint }}</p>
        </div>
      </div>
    </div>

    <template #footer>
      <div class="hitl-form-actions">
        <el-button @click="handleSkip" :disabled="loading">
          跳过，去浏览器操作
        </el-button>
        <el-button
          type="primary"
          :loading="loading"
          :disabled="!canSubmit"
          @click="handleSubmit"
        >
          提交并继续
        </el-button>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.hitl-form-body {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.hitl-form-reason {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 12px;
  border-radius: 8px;
  background: rgb(var(--rgb-warn) / 0.08);
  border: 1px solid rgb(var(--rgb-warn) / 0.25);
  color: var(--vsp-warn-soft);
  font-size: 13px;
  line-height: 1.5;
}

.hitl-form-reason-icon {
  flex-shrink: 0;
  font-size: 16px;
}

.hitl-form-screenshot {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid var(--vsp-border);
}

.hitl-form-screenshot img {
  width: 100%;
  max-height: 160px;
  display: block;
  object-fit: contain;
  cursor: pointer;
  transition: max-height 0.3s ease;
}

.hitl-form-screenshot img.is-zoomed {
  max-height: 400px;
}

.hitl-form-screenshot img:hover {
  opacity: 0.92;
}

.hitl-form-screenshot-hint {
  padding: 4px 0 6px;
  color: var(--vsp-text-dim-alt);
  font-size: 11px;
}

.hitl-form-fields {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.hitl-form-field label {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
  color: var(--vsp-text-strong);
  font-size: 13px;
  font-weight: 600;
}

.hitl-field-icon {
  font-size: 14px;
}

.hitl-field-required {
  color: var(--vsp-danger-rose);
  font-weight: 700;
}

.hitl-field-hint {
  margin: 4px 0 0;
  color: var(--vsp-text-muted);
  font-size: 12px;
}

.hitl-form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

:deep(.hitl-form-dialog .el-dialog) {
  background: var(--vsp-surface);
  border: 1px solid var(--vsp-border);
  border-radius: 12px;
}

:deep(.hitl-form-dialog .el-dialog__header) {
  padding: 16px 20px 0;
}

:deep(.hitl-form-dialog .el-dialog__title) {
  color: var(--vsp-text-strong);
  font-size: 16px;
  font-weight: 700;
}

:deep(.hitl-form-dialog .el-dialog__body) {
  padding: 16px 20px;
}

:deep(.hitl-form-dialog .el-dialog__footer) {
  padding: 0 20px 16px;
}
</style>
