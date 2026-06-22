// 方向D · 从 App.vue 抽离：WebSocket 连接生命周期（连接 / 自动重连 / 卸载清理）。
//
// 仅负责连接的生命周期与状态机，不解析业务消息——消息路由通过 onMessage 回调
// 交还调用方（App.vue 的 handleSocketMessage 仍持有全部组件状态）。
// 行为与原 App.vue 内联实现保持一致：
//   - connect 前若已 OPEN / CONNECTING 则跳过（防重复连接）
//   - onclose 且未卸载时，reconnectDelay 之后自动重连
//   - disconnect() 置卸载标志、清重连定时器、关闭并丢弃 socket
import { ref } from 'vue'
import { wsUrl } from '../api/client.js'

export function useWebSocket ({
  path = '/ws/logs',
  reconnectDelay = 2000,
  onOpen,
  onMessage,
  onClose,
  onError,
} = {}) {
  const status = ref('connecting')
  let socket = null
  let reconnectTimer = null
  let isUnmounted = false

  const connect = () => {
    if (isUnmounted) return
    if (socket && socket.readyState === WebSocket.OPEN) return
    if (socket && socket.readyState === WebSocket.CONNECTING) return

    status.value = 'connecting'
    socket = new WebSocket(wsUrl(path))

    socket.onopen = () => {
      status.value = 'connected'
      if (onOpen) onOpen()
    }

    socket.onmessage = (event) => {
      if (onMessage) onMessage(event)
    }

    socket.onclose = () => {
      status.value = 'disconnected'
      if (onClose) onClose()
      if (!isUnmounted) {
        if (reconnectTimer) clearTimeout(reconnectTimer)
        reconnectTimer = setTimeout(() => {
          reconnectTimer = null
          connect()
        }, reconnectDelay)
      }
    }

    socket.onerror = () => {
      status.value = 'error'
      if (onError) onError()
    }
  }

  const disconnect = () => {
    isUnmounted = true
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    if (socket) {
      socket.close()
      socket = null
    }
  }

  return { status, connect, disconnect }
}
