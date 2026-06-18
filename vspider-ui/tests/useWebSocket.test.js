// Behavior lock for composables/useWebSocket.js (方向D · 从 App.vue 抽离)
//
// Pins the WebSocket lifecycle contract that was previously inline in App.vue:
//   - connect() opens a socket at wsUrl(path); status connecting -> connected
//   - onmessage forwards the raw event to the onMessage callback
//   - onclose schedules a reconnect after reconnectDelay (unless unmounted)
//   - connect() is a no-op while a socket is OPEN / CONNECTING
//   - disconnect() closes the socket and prevents any further reconnect
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

vi.mock('../src/api/client.js', () => ({
  wsUrl: (p) => 'ws://test.local' + p,
}))

import { useWebSocket } from '../src/composables/useWebSocket.js'

class MockWebSocket {
  constructor (url) {
    this.url = url
    this.readyState = MockWebSocket.CONNECTING
    this.onopen = null
    this.onmessage = null
    this.onclose = null
    this.onerror = null
    this.close = vi.fn(() => { this.readyState = MockWebSocket.CLOSED })
    MockWebSocket.instances.push(this)
  }

  _open () { this.readyState = MockWebSocket.OPEN; if (this.onopen) this.onopen() }
  _message (data) { if (this.onmessage) this.onmessage({ data }) }
  _close () { this.readyState = MockWebSocket.CLOSED; if (this.onclose) this.onclose() }
  _error () { if (this.onerror) this.onerror() }
}
MockWebSocket.CONNECTING = 0
MockWebSocket.OPEN = 1
MockWebSocket.CLOSING = 2
MockWebSocket.CLOSED = 3
MockWebSocket.instances = []

const last = () => MockWebSocket.instances[MockWebSocket.instances.length - 1]

beforeEach(() => {
  MockWebSocket.instances = []
  vi.stubGlobal('WebSocket', MockWebSocket)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('useWebSocket connect/open', () => {
  it('opens a socket at wsUrl(path) and tracks status', () => {
    const ws = useWebSocket({ path: '/ws/logs' })
    expect(ws.status.value).toBe('connecting')
    ws.connect()
    expect(MockWebSocket.instances).toHaveLength(1)
    expect(last().url).toBe('ws://test.local/ws/logs')
    expect(ws.status.value).toBe('connecting')
    last()._open()
    expect(ws.status.value).toBe('connected')
  })

  it('fires onOpen / onMessage / onError callbacks', () => {
    const onOpen = vi.fn()
    const onMessage = vi.fn()
    const onError = vi.fn()
    const ws = useWebSocket({ onOpen, onMessage, onError })
    ws.connect()
    last()._open()
    expect(onOpen).toHaveBeenCalledTimes(1)
    last()._message('{"type":"log"}')
    expect(onMessage).toHaveBeenCalledWith({ data: '{"type":"log"}' })
    last()._error()
    expect(onError).toHaveBeenCalledTimes(1)
    expect(ws.status.value).toBe('error')
  })

  it('does not open a second socket while CONNECTING or OPEN', () => {
    const ws = useWebSocket()
    ws.connect()
    expect(MockWebSocket.instances).toHaveLength(1)
    ws.connect() // still CONNECTING
    expect(MockWebSocket.instances).toHaveLength(1)
    last()._open()
    ws.connect() // now OPEN
    expect(MockWebSocket.instances).toHaveLength(1)
  })
})

describe('useWebSocket reconnect', () => {
  it('reconnects after reconnectDelay on close', () => {
    vi.useFakeTimers()
    const onClose = vi.fn()
    const ws = useWebSocket({ reconnectDelay: 2000, onClose })
    ws.connect()
    last()._open()
    last()._close()
    expect(ws.status.value).toBe('disconnected')
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(MockWebSocket.instances).toHaveLength(1)
    vi.advanceTimersByTime(2000)
    expect(MockWebSocket.instances).toHaveLength(2)
  })
})

describe('useWebSocket disconnect', () => {
  it('closes the socket and blocks any reconnect / future connect', () => {
    vi.useFakeTimers()
    const ws = useWebSocket({ reconnectDelay: 2000 })
    ws.connect()
    last()._open()
    const sock = last()
    ws.disconnect()
    expect(sock.close).toHaveBeenCalledTimes(1)
    // a late onclose (browser fires after close) must not schedule a reconnect
    sock._close()
    vi.advanceTimersByTime(5000)
    expect(MockWebSocket.instances).toHaveLength(1)
    // connect() after disconnect is a no-op
    ws.connect()
    expect(MockWebSocket.instances).toHaveLength(1)
  })
})
