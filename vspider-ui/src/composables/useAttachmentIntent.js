// 附件 intent 用户覆盖（输入契约 §一-B：intent 必推断、用户可覆盖）。
// "auto" 表示交给后端 infer_attachment_intent 推断；其余值与
// visual_web_agent/io_contract/input_contract.py ATTACHMENT_INTENTS 对齐
// （"unknown" 是推断结果而非用户选项，不在列表中）。

export const ATTACHMENT_INTENT_AUTO = 'auto'

export const ATTACHMENT_INTENT_OPTIONS = [
  { value: ATTACHMENT_INTENT_AUTO, label: '自动推断（默认）' },
  { value: 'batch_rows', label: '逐行批量任务 — 表格每行展开为一个子任务' },
  { value: 'upload_to_page', label: '上传到页面 — 作为网页表单的附件提交' },
  { value: 'prompt_context', label: '任务上下文 — 读取内容辅助理解，不上传' },
  { value: 'media_source', label: '媒体素材源 — 作为音视频素材引用' },
]

const VALID_INTENTS = new Set(
  ATTACHMENT_INTENT_OPTIONS
    .map((option) => option.value)
    .filter((value) => value !== ATTACHMENT_INTENT_AUTO),
)

export function isValidAttachmentIntent(value) {
  return VALID_INTENTS.has(value)
}

// 仅当用户做出真实覆盖（非 auto 且合法）时才附加字段，保持请求向后兼容。
// 返回是否附加，方便调用方做日志/断言。
export function appendAttachmentIntentToFormData(formData, intent) {
  if (!isValidAttachmentIntent(intent)) {
    return false
  }
  formData.append('attachment_intent', intent)
  return true
}
