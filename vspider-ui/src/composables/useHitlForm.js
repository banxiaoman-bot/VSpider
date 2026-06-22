// 方向D · 从 App.vue 抽离：human-in-the-loop（HITL）介入/表单子系统。
//
// 持有 HITL 全部状态（人工介入标志/原因/截图 + 前端补填表单可见性/字段/原因/
// 截图/loading）、isBotChallengeHitl 判定，以及三个动作：恢复执行、提交表单、
// 跳过表单。WS 消息处理器（App.vue handleSocketMessage）直接写这些返回的 ref
// （destructure 后同名，零改动）。网络经 apiFetch，反馈经 ElMessage，日志经注入
// 的 appendLog。行为与原 App.vue 内联实现逐字一致。
import { ref, computed } from 'vue'
import { apiFetch } from '../api/client.js'
import { ElMessage } from 'element-plus'
import { isBotChallengeReason } from './useTaskSubmit'

export function useHitlForm ({ appendLog }) {
  const isHumanInterventionRequired = ref(false)
  const humanInterventionReason = ref('')
  const hitlFormVisible = ref(false)
  const hitlFormFields = ref([])
  const hitlFormReason = ref('')
  const hitlFormScreenshot = ref('')
  const hitlFormLoading = ref(false)
  const hitlScreenshot = ref('')

  const isBotChallengeHitl = computed(() =>
    isBotChallengeReason(humanInterventionReason.value),
  )

  const resumeAgentExecution = async () => {
    try {
      const response = await apiFetch('/api/human/resume', {
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

  const submitHitlForm = async (formData) => {
    hitlFormLoading.value = true
    try {
      const response = await apiFetch('/api/human/form_submit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fields: formData }),
      })
      const result = await response.json()
      if (!response.ok || result.status !== 'success') {
        throw new Error(result.message || '表单提交失败')
      }
      hitlFormVisible.value = false
      hitlFormLoading.value = false
      ElMessage.success('已提交表单数据，Agent 继续执行')
      await appendLog(`[HITL] 表单数据已提交，${Object.keys(formData).length} 个字段`)
    } catch (err) {
      hitlFormLoading.value = false
      ElMessage.error(`表单提交失败: ${String(err)}`)
      await appendLog(`[ERROR] HITL 表单提交失败: ${String(err)}`)
    }
  }

  const skipHitlForm = async () => {
    hitlFormVisible.value = false
    hitlFormLoading.value = false
    await appendLog('[HITL] 用户选择跳过前端表单，请去浏览器窗口操作')
  }

  return {
    isHumanInterventionRequired,
    humanInterventionReason,
    hitlFormVisible,
    hitlFormFields,
    hitlFormReason,
    hitlFormScreenshot,
    hitlFormLoading,
    hitlScreenshot,
    isBotChallengeHitl,
    resumeAgentExecution,
    submitHitlForm,
    skipHitlForm,
  }
}
