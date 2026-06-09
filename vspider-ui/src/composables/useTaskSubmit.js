import { API_BASE } from '../api/client.js'

export const ATTACHMENT_ACCEPT =
  '.xlsx,.xls,.csv,.tsv,.json,.jsonl,.txt,.md,.html,.pdf,.docx,.pptx,.eml,.png,.jpg,.jpeg,.webp,.gif,.mp4,.mp3,.wav,.zip,.7z,.rar,.parquet,.ods'

export const ATTACHMENT_HINT =
  '支持表格/文档/图片/音视频/压缩包等；intent 由后端按文件名 + goal 自动推断'

export function validateTaskInput({ prompt }) {
  if (!String(prompt || '').trim()) {
    return { ok: false, message: '请先填写业务指令（goal 为唯一必填）' }
  }
  return { ok: true }
}

export function buildTaskConstraints({
  proxyServer = '',
  proxyUsername = '',
  proxyPassword = '',
  maxRuns = 0,
  resume = false,
} = {}) {
  const constraints = {}
  const server = String(proxyServer || '').trim()
  if (server) {
    constraints.proxy_server = server
    const user = String(proxyUsername || '').trim()
    const pass = String(proxyPassword || '').trim()
    if (user) constraints.proxy_username = user
    if (pass) constraints.proxy_password = pass
  }
  const runs = Number.parseInt(String(maxRuns || ''), 10)
  if (Number.isFinite(runs) && runs > 0) {
    constraints.max_runs = runs
  }
  if (resume) {
    constraints.resume = true
  }
  return Object.keys(constraints).length ? constraints : null
}

export function parseUrlList(value = '') {
  if (Array.isArray(value)) {
    return value.map((item) => String(item || '').trim()).filter(Boolean)
  }
  const text = String(value || '').trim()
  if (!text) return []
  if (text.startsWith('[')) {
    try {
      const parsed = JSON.parse(text)
      if (Array.isArray(parsed)) return parseUrlList(parsed)
    } catch {
      // Fall back to delimiter parsing for user-entered text.
    }
  }
  return text
    .split(/[\n,;]+/)
    .map((item) => item.trim())
    .filter(Boolean)
}

export function buildAuthoritativeUrlsPayload(targetUrl = '', extraUrls = '') {
  const urls = []
  const seen = new Set()
  for (const item of [targetUrl, ...parseUrlList(extraUrls)]) {
    const value = String(item || '').trim()
    if (!value) continue
    const key = value.replace(/\/+$/, '')
    if (seen.has(key)) continue
    seen.add(key)
    urls.push(value)
  }
  return urls
}

export function authProfileOptionLabel(profile) {
  if (!profile || !profile.name) return ''
  const parts = [profile.name]
  if (profile.cf_clearance) {
    const hours = profile.cf_clearance_expires_in_hours
    if (Number.isFinite(hours) && hours > 0) {
      parts.push(`CF ~${Math.round(hours)}h`)
    } else {
      parts.push('CF')
    }
  }
  if (Number.isFinite(profile.cookies) && profile.cookies > 0) {
    parts.push(`${profile.cookies}c`)
  }
  return parts.join(' · ')
}

export function isBotChallengeReason(reason) {
  const text = String(reason || '').toLowerCase()
  return /cloudflare|turnstile|cf_clearance|bot.?challenge|人机验证|验证码/.test(text)
}

export function appendConstraintsToFormData(formData, constraints) {
  if (!formData || !constraints || typeof constraints !== 'object') return
  formData.append('constraints', JSON.stringify(constraints))
}

const OUTPUT_KIND_LABELS = {
  answer_text: '纯文本回答',
  dataset_rows: '表格数据集',
  dataset_records: '结构化记录',
  media_image: '图片',
  media_video: '视频',
  media_audio: '音频',
  media_pdf: 'PDF',
  media_archive: '压缩包',
  file_generic: '通用文件',
  html_snapshot: 'HTML 快照',
  screenshot: '截图',
  code_or_text: '代码/文本',
  mixed: '混合产物',
}

const CONTAINER_LABELS = {
  inline_text: '仅聊天流',
  xlsx: 'Excel',
  csv: 'CSV',
  jsonl: 'JSONL',
  json: 'JSON',
  files_folder: '文件目录',
  zip: 'ZIP',
  html: 'HTML',
  markdown: 'Markdown',
}

export function formatOutputContractPreview(contract) {
  if (!contract || typeof contract !== 'object') return null
  const kind = String(contract.output_kind || '')
  const container = String(contract.container || '')
  const mode = String(contract.mode || 'default')
  const reasons = Array.isArray(contract.reasons) ? contract.reasons.filter(Boolean) : []
  return {
    mode,
    output_kind: kind,
    container,
    kind_label: OUTPUT_KIND_LABELS[kind] || kind || '未推断',
    container_label: CONTAINER_LABELS[container] || container || '未指定',
    reasons,
  }
}

export async function fetchOutputContractPreview(goal, apiBase = API_BASE) {
  const text = String(goal || '').trim()
  if (!text) {
    return { status: 'empty', output_contract: null }
  }
  const url = `${apiBase}/api/output_contract/preview?goal=${encodeURIComponent(text)}`
  const response = await fetch(url)
  const payload = await response.json()
  if (payload?.status !== 'success') {
    return {
      status: 'error',
      message: payload?.message || 'preview failed',
      output_contract: null,
    }
  }
  return {
    status: 'success',
    output_contract: payload.output_contract,
    formatted: formatOutputContractPreview(payload.output_contract),
  }
}
