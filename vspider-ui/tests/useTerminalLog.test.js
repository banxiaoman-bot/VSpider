// TDD for composables/useTerminalLog.js (优化 D · WS 日志批量化)
//
// Behavior under test:
//   1. appendLog is synchronous and buffers lines; nothing hits the
//      reactive `logs` array until the scheduler fires (batching).
//   2. One flush per batch → onFlush (scroll hook) called once, not N times.
//   3. Ring-buffer trim: logs never exceed `limit`, oldest lines drop first,
//      trimmedCount accumulates so the UI can show "N 行已裁剪".
//   4. Scheduler re-arms after each flush.
//   5. Default scheduler auto-flushes without manual driving.
//   6. clear() resets logs / pending / trimmedCount for a new run.
import { describe, it, expect, vi } from 'vitest'
import { createTerminalLogBuffer, LOG_LIMIT } from '../src/composables/useTerminalLog.js'

// Scheduler stub: collects callbacks so tests control flush timing.
function manualScheduler() {
  const queue = []
  const schedule = (cb) => {
    queue.push(cb)
  }
  schedule.run = () => {
    while (queue.length) queue.shift()()
  }
  schedule.calls = queue
  return schedule
}

describe('createTerminalLogBuffer', () => {
  it('appendLog buffers lines until the scheduler fires', () => {
    const scheduler = manualScheduler()
    const buf = createTerminalLogBuffer({ scheduler })
    buf.appendLog('[SYSTEM] a')
    buf.appendLog('[SYSTEM] b')
    buf.appendLog('[SYSTEM] c')
    expect(buf.logs.value).toEqual([])
    scheduler.run()
    expect(buf.logs.value).toEqual(['[SYSTEM] a', '[SYSTEM] b', '[SYSTEM] c'])
  })

  it('calls onFlush once per batch, not once per line', () => {
    const scheduler = manualScheduler()
    const onFlush = vi.fn()
    const buf = createTerminalLogBuffer({ scheduler, onFlush })
    for (let i = 0; i < 100; i++) buf.appendLog(`line ${i}`)
    expect(onFlush).not.toHaveBeenCalled()
    scheduler.run()
    expect(onFlush).toHaveBeenCalledTimes(1)
    expect(buf.logs.value).toHaveLength(100)
  })

  it('trims to limit keeping the newest lines and counts trimmed rows', () => {
    const scheduler = manualScheduler()
    const buf = createTerminalLogBuffer({ scheduler, limit: 5 })
    for (let i = 1; i <= 8; i++) buf.appendLog(`L${i}`)
    scheduler.run()
    expect(buf.logs.value).toEqual(['L4', 'L5', 'L6', 'L7', 'L8'])
    expect(buf.trimmedCount.value).toBe(3)
  })

  it('accumulates trimmedCount across flushes', () => {
    const scheduler = manualScheduler()
    const buf = createTerminalLogBuffer({ scheduler, limit: 5 })
    for (let i = 1; i <= 8; i++) buf.appendLog(`L${i}`)
    scheduler.run()
    buf.appendLog('L9')
    buf.appendLog('L10')
    scheduler.run()
    expect(buf.logs.value).toEqual(['L6', 'L7', 'L8', 'L9', 'L10'])
    expect(buf.trimmedCount.value).toBe(5)
  })

  it('re-arms the scheduler after a flush', () => {
    const scheduler = manualScheduler()
    const schedule = vi.fn(scheduler)
    const buf = createTerminalLogBuffer({ scheduler: schedule })
    buf.appendLog('a')
    buf.appendLog('b') // same batch → no extra scheduling
    expect(schedule).toHaveBeenCalledTimes(1)
    scheduler.run()
    buf.appendLog('c')
    expect(schedule).toHaveBeenCalledTimes(2)
  })

  it('flushNow drains pending immediately and is a no-op when empty', () => {
    const scheduler = manualScheduler()
    const onFlush = vi.fn()
    const buf = createTerminalLogBuffer({ scheduler, onFlush })
    buf.appendLog('x')
    buf.flushNow()
    expect(buf.logs.value).toEqual(['x'])
    expect(onFlush).toHaveBeenCalledTimes(1)
    buf.flushNow()
    expect(onFlush).toHaveBeenCalledTimes(1)
  })

  it('auto-flushes with the default scheduler', async () => {
    const buf = createTerminalLogBuffer()
    buf.appendLog('auto')
    await new Promise((resolve) => setTimeout(resolve, 60))
    expect(buf.logs.value).toEqual(['auto'])
  })

  it('clear resets logs, pending buffer and trimmedCount', () => {
    const scheduler = manualScheduler()
    const buf = createTerminalLogBuffer({ scheduler, limit: 2 })
    buf.appendLog('a')
    buf.appendLog('b')
    buf.appendLog('c')
    scheduler.run()
    expect(buf.trimmedCount.value).toBe(1)
    buf.appendLog('pending-line')
    buf.clear()
    expect(buf.logs.value).toEqual([])
    expect(buf.trimmedCount.value).toBe(0)
    scheduler.run() // stale scheduled flush must not resurrect cleared lines
    expect(buf.logs.value).toEqual([])
  })

  it('exports a production LOG_LIMIT of 2000', () => {
    expect(LOG_LIMIT).toBe(2000)
  })

  // App.vue watches the `logs` ref itself (shallow) to re-clamp the terminal
  // search index. That only fires if every flush replaces the array reference,
  // including when the buffer is already full and length stays constant.
  it('replaces the array reference on every flush (shallow watch contract)', () => {
    const scheduler = manualScheduler()
    const buf = createTerminalLogBuffer({ scheduler, limit: 3 })
    for (let i = 1; i <= 3; i++) buf.appendLog(`L${i}`)
    scheduler.run()
    const fullRef = buf.logs.value
    expect(fullRef).toHaveLength(3)
    buf.appendLog('L4') // buffer already at limit → length stays 3
    scheduler.run()
    expect(buf.logs.value).toHaveLength(3)
    expect(buf.logs.value).not.toBe(fullRef)
    expect(buf.logs.value).toEqual(['L2', 'L3', 'L4'])
  })
})
