// 优化 D · Live Terminal log buffer with batched flush + ring trim.
//
// Why this exists (App.vue 优化前的问题):
//   * appendLog pushed straight into the reactive array and awaited a
//     scroll per line → a WS message storm caused N reactive updates,
//     N nextTick waits and N forced scrolls per frame (layout thrash).
//   * logs had no cap (phaseEvents has PHASE_LIMIT=500, logs had none),
//     so a long run grew the v-for DOM without bound.
//
// Design:
//   * appendLog is synchronous: lines land in a plain `pending` array
//     (no reactivity cost), one flush per animation frame applies them
//     in a single reactive assignment and fires `onFlush` once so the
//     caller can scroll once per batch.
//   * Ring trim keeps the newest `limit` lines; `trimmedCount` is
//     exposed so the UI can tell the user lines were dropped (full log
//     still lives in the backend event_stream.jsonl).
//   * `scheduler` is injectable for deterministic tests; `generation`
//     guards stale scheduled flushes after clear().
import { ref } from 'vue'

export const LOG_LIMIT = 2000

const defaultScheduler = (cb) => {
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(cb)
  } else {
    setTimeout(cb, 16)
  }
}

export function createTerminalLogBuffer({
  limit = LOG_LIMIT,
  scheduler = defaultScheduler,
  onFlush,
} = {}) {
  const logs = ref([])
  const trimmedCount = ref(0)
  let pending = []
  let scheduled = false
  let generation = 0

  const flushNow = () => {
    scheduled = false
    if (!pending.length) return
    const next = logs.value.concat(pending)
    pending = []
    if (next.length > limit) {
      trimmedCount.value += next.length - limit
      next.splice(0, next.length - limit)
    }
    logs.value = next
    if (typeof onFlush === 'function') onFlush()
  }

  const appendLog = (message) => {
    pending.push(message)
    if (scheduled) return
    scheduled = true
    const gen = generation
    scheduler(() => {
      if (gen !== generation) return
      flushNow()
    })
  }

  const clear = () => {
    generation += 1
    pending = []
    scheduled = false
    logs.value = []
    trimmedCount.value = 0
  }

  return { logs, trimmedCount, appendLog, flushNow, clear }
}
