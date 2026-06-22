// Behavior lock for composables/useRunStream.js (D-UI-28)
//
// Pins the WebSocket handler wiring previously inline in App.vue:
//   - buildWebSocketHandlers: onOpen/onClose/onError → appendLog;
//     onMessage → handleSocketMessage
import { describe, it, expect, vi } from 'vitest'

import { buildWebSocketHandlers } from '../src/composables/useRunStream.js'

describe('buildWebSocketHandlers', () => {
  it('onMessage delegates to handleSocketMessage with same payload', () => {
    const handleSocketMessage = vi.fn()
    const appendLog = vi.fn()
    const handlers = buildWebSocketHandlers({ appendLog, handleSocketMessage })
    const payload = { type: 'phase', data: {} }
    handlers.onMessage(payload)
    expect(handleSocketMessage).toHaveBeenCalledWith(payload)
  })

  it('onOpen appends connected log', () => {
    const appendLog = vi.fn()
    const handlers = buildWebSocketHandlers({ appendLog, handleSocketMessage: vi.fn() })
    handlers.onOpen()
    expect(appendLog).toHaveBeenCalledWith('[SYSTEM] WebSocket connected')
  })

  it('onClose appends disconnected log', () => {
    const appendLog = vi.fn()
    const handlers = buildWebSocketHandlers({ appendLog, handleSocketMessage: vi.fn() })
    handlers.onClose()
    expect(appendLog).toHaveBeenCalledWith('[SYSTEM] WebSocket disconnected')
  })

  it('onError appends error log', () => {
    const appendLog = vi.fn()
    const handlers = buildWebSocketHandlers({ appendLog, handleSocketMessage: vi.fn() })
    handlers.onError()
    expect(appendLog).toHaveBeenCalledWith('[ERROR] WebSocket error')
  })
})
