// 方向D · 从 App.vue 抽离：剪贴板写入工具。
//
// 与原 App.vue 内联 _writeToClipboard 逐字一致：优先 navigator.clipboard.writeText，
// 不可用时回退到隐藏 <textarea> + document.execCommand('copy')；任何异常经 ElMessage
// 反馈并返回 false；空串直接返回 false。
import { ElMessage } from 'element-plus'

export async function writeToClipboard (text) {
  if (!text) return false
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text)
    } else {
      const ta = document.createElement('textarea')
      ta.value = text
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    return true
  } catch (err) {
    ElMessage.error(`复制失败: ${String(err)}`)
    return false
  }
}
