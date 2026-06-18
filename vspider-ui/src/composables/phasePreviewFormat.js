// 方向D · 相位事件预览的轻量格式化助手（severity 归一 + 时:分:秒）。
//
// 原本分别内联在 FailedRunsPane.vue 与 App.vue 的 capabilityTraceRows 里；
// 后者引用的是一份「从未在 App.vue 定义」的同名函数（既有 ReferenceError 隐患）。
// 抽成共享纯函数，供 useCapabilityTrace 与 FailedRunsPane 复用。
export function formatPhasePreviewSeverity (sev) {
  const s = String(sev || 'info').toLowerCase()
  if (s === 'warn' || s === 'warning') return 'warn'
  if (s === 'error' || s === 'err') return 'error'
  return 'info'
}

export function formatPhasePreviewTs (ts) {
  if (!Number.isFinite(ts)) return ''
  try {
    const d = new Date(ts * 1000)
    const pad = (n) => String(n).padStart(2, '0')
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  } catch (_) {
    return ''
  }
}
