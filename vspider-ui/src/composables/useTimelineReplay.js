// 方向D · 从 App.vue 抽离：时间线离线回放（导入 phase_<id>.jsonl + 进入/退出回放）。
//
// parsePhaseReplayJsonl 是纯解析器（容忍空行 / 坏 JSON / 单 capability_execute_trace 文档），
// 可独立单测；useTimelineReplay 持有回放状态机，导入时替换 phaseEvents 并置 replayMode，
// 让 WS 相位事件门控保持回放缓冲纯净。行为与原 App.vue 内联实现逐字一致。
import { ref } from 'vue'
import { ElMessage } from 'element-plus'

export const parsePhaseReplayJsonl = (text) => {
  const out = []
  let bad = 0
  let total = 0
  const _capabilityExecuteArtifactPhaseEvent = (doc) => {
    if (!doc || typeof doc !== 'object' || Array.isArray(doc)) return null
    if (String(doc.type || '') !== 'capability_execute_trace') return null
    const result = doc.result && typeof doc.result === 'object' && !Array.isArray(doc.result) ? doc.result : {}
    return {
      type: 'phase',
      phase: 'capability_execute',
      severity: result.completed ? 'info' : 'warn',
      message: String(result.capability || result.fallback_reason || result.status || 'capability_execute'),
      ts: Number.isFinite(doc.created_at) ? doc.created_at : Date.now() / 1000,
      execution_status: result.status,
      completed: Boolean(result.completed),
      capability: result.capability,
      attempts: Array.isArray(result.attempts) ? result.attempts : [],
      verification: result.verification,
      fallback_reason: result.fallback_reason,
      artifact: result.artifact,
      trace_artifact: result.trace_artifact,
      runtime_summary: result.runtime_summary,
      runtime_drift: result.runtime_drift,
      runtime_issue_summary: result.runtime_issue_summary,
      action_trace: result.action_trace,
      action_issue_summary: result.action_issue_summary,
      failure_bundle: result.failure_bundle,
      route_intent: result.route?.intent,
    }
  }
  try {
    const doc = JSON.parse(String(text || '').trim())
    const artifactEvent = _capabilityExecuteArtifactPhaseEvent(doc)
    if (artifactEvent) return { events: [artifactEvent], total: 1, bad: 0 }
  } catch (err) {
    // fall through to JSONL parsing
  }
  // Normalize line endings: a phase log captured on Windows may carry
  // CRLF and we don't want a stray '\r' breaking the JSON parser.
  const lines = String(text || '').replace(/\r\n?/g, '\n').split('\n')
  for (const raw of lines) {
    const line = raw.trim()
    if (!line) continue
    total += 1
    try {
      const obj = JSON.parse(line)
      if (obj && typeof obj === 'object' && !Array.isArray(obj)) {
        const artifactEvent = _capabilityExecuteArtifactPhaseEvent(obj)
        if (artifactEvent) {
          out.push(artifactEvent)
          continue
        }
        const phaseEvent = obj?.detail?.phase_event
        out.push(phaseEvent && typeof phaseEvent === 'object' && !Array.isArray(phaseEvent) ? phaseEvent : obj)
        continue
      }
    } catch (err) {
      // fall through to bad counter
    }
    bad += 1
  }
  return { events: out, total, bad }
}


export function useTimelineReplay ({ phaseEvents, hasNewCapability, setActiveBottomTab }) {
const replayMode = ref(false)
const replaySourceName = ref('')
const replayInputRef = ref(null)
const replayImportTarget = ref('timeline')

const triggerReplayImport = (target = 'timeline') => {
  // Programmatically click the hidden <input type="file"> so the user
  // gets the native picker. We reset the value first so re-importing
  // the same file fires onchange again (browsers debounce identical
  // selections otherwise).
  replayImportTarget.value = target === 'capability' ? 'capability' : 'timeline'
  const inp = replayInputRef.value
  if (!inp) return
  try {
    inp.value = ''
    inp.click()
  } catch (err) {
    // Quiet — file picker errors are essentially "user clicked cancel"
    // and the rest of the app is unaffected.
  }
}


const exitReplayMode = () => {
  replayMode.value = false
  replaySourceName.value = ''
  phaseEvents.value = []
  hasNewCapability.value = false
  ElMessage.info('已退出回放模式')
}


const handleTimelineImportReplay = (rawText, filename) => {
  const { events, total, bad } = parsePhaseReplayJsonl(rawText)
  if (events.length === 0) {
    ElMessage.warning('文件中没有可识别的 phase 事件')
    return
  }
  for (const e of events) {
    if (typeof e._ts !== 'number') e._ts = Number.isFinite(e.ts) ? e.ts : Date.now() / 1000
  }
  phaseEvents.value = events
  replayMode.value = true
  replaySourceName.value = filename
  setActiveBottomTab(replayImportTarget.value === 'capability' ? 'capability' : 'timeline')
  if (bad > 0) {
    ElMessage.warning(`已导入 ${events.length} / ${total} 条事件（跳过 ${bad} 行损坏数据）`)
  } else {
    ElMessage.success(`已导入 ${events.length} 条事件，进入回放模式`)
  }
}


  return {
    replayMode,
    replaySourceName,
    replayInputRef,
    replayImportTarget,
    triggerReplayImport,
    exitReplayMode,
    handleTimelineImportReplay,
  }
}
