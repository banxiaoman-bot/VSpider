<script setup>
import { ref } from 'vue'

const props = defineProps({
  running: { type: Boolean, default: false },
})

const panelOpen = ref(false)
const fabRef = ref(null)
let isDragging = false
let hasMoved = false
let dragStartX = 0, dragStartY = 0, fabStartX = 0, fabStartY = 0

function onDragStart(e) {
  e.preventDefault()
  isDragging = true
  hasMoved = false
  const point = e.touches ? e.touches[0] : e
  const rect = fabRef.value.getBoundingClientRect()
  dragStartX = point.clientX
  dragStartY = point.clientY
  fabStartX = rect.left
  fabStartY = rect.top
  document.addEventListener('mousemove', onDragMove)
  document.addEventListener('mouseup', onDragEnd)
  document.addEventListener('touchmove', onDragMove, { passive: false })
  document.addEventListener('touchend', onDragEnd)
}

function onDragMove(e) {
  if (!isDragging) return
  e.preventDefault()
  const point = e.touches ? e.touches[0] : e
  const dx = point.clientX - dragStartX
  const dy = point.clientY - dragStartY
  if (Math.abs(dx) > 3 || Math.abs(dy) > 3) hasMoved = true
  const fab = fabRef.value
  if (!fab) return
  const maxX = window.innerWidth - fab.offsetWidth
  const maxY = window.innerHeight - fab.offsetHeight
  fab.style.left = Math.max(0, Math.min(fabStartX + dx, maxX)) + 'px'
  fab.style.top = Math.max(0, Math.min(fabStartY + dy, maxY)) + 'px'
  fab.style.right = 'auto'
  fab.style.bottom = 'auto'
}

function onDragEnd() {
  isDragging = false
  document.removeEventListener('mousemove', onDragMove)
  document.removeEventListener('mouseup', onDragEnd)
  document.removeEventListener('touchmove', onDragMove)
  document.removeEventListener('touchend', onDragEnd)
  if (!hasMoved) panelOpen.value = !panelOpen.value
}
</script>

<template>
  <div
    ref="fabRef"
    class="spider-fab"
    :class="{ running }"
    @mousedown="onDragStart"
    @touchstart.prevent="onDragStart"
  >
    <svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <radialGradient id="spBodyG" cx="38%" cy="30%" r="80%">
          <stop offset="0%" stop-color="#eef3f4" />
          <stop offset="55%" stop-color="#ccd6d8" />
          <stop offset="100%" stop-color="#aab6b9" />
        </radialGradient>
        <radialGradient id="spLensG" cx="40%" cy="35%" r="75%">
          <stop offset="0%" stop-color="#ecffff" />
          <stop offset="55%" stop-color="#a6e6ee" />
          <stop offset="100%" stop-color="#6cc3cf" />
        </radialGradient>
        <linearGradient id="spRingG" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="#67e8f9" />
          <stop offset="50%" stop-color="#22d3ee" />
          <stop offset="100%" stop-color="#2dd4bf" />
        </linearGradient>
        <linearGradient id="spLegG" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#dde4e6" />
          <stop offset="100%" stop-color="#b3bfc2" />
        </linearGradient>
        <filter id="spRingGlow" x="-30%" y="-30%" width="160%" height="160%">
          <feGaussianBlur stdDeviation="1.8" result="b" />
          <feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge>
        </filter>
        <clipPath id="spFrontClip"><rect x="0" y="49" width="100" height="51" /></clipPath>
      </defs>

      <ellipse cx="50" cy="95" rx="22" ry="3" fill="#0f172a" opacity="0.1" />

      <g class="orbit-ring orbit-ring-back" filter="url(#spRingGlow)">
        <ellipse cx="50" cy="48" rx="47" ry="19" fill="none" stroke="url(#spRingG)"
          stroke-width="3" transform="rotate(-18 50 48)" opacity="0.95" />
      </g>

      <g class="spider-bob">
        <g stroke-linecap="round" fill="none">
          <g stroke="#2b4a54" stroke-width="7">
            <path d="M40 58 Q23 61 18 73" /><path d="M60 58 Q77 61 82 73" />
            <path d="M42 64 Q28 71 24 85" /><path d="M58 64 Q72 71 76 85" />
            <path d="M46 68 Q40 79 38 91" /><path d="M54 68 Q60 79 62 91" />
          </g>
          <g stroke="url(#spLegG)" stroke-width="4">
            <path d="M40 58 Q23 61 18 73" /><path d="M60 58 Q77 61 82 73" />
            <path d="M42 64 Q28 71 24 85" /><path d="M58 64 Q72 71 76 85" />
            <path d="M46 68 Q40 79 38 91" /><path d="M54 68 Q60 79 62 91" />
          </g>
        </g>

        <ellipse cx="50" cy="46" rx="31" ry="29" fill="url(#spBodyG)" stroke="#2b4a54" stroke-width="2.6" />
        <ellipse cx="38" cy="31" rx="12" ry="7" fill="#ffffff" opacity="0.35" />

        <path d="M22 50 Q23 66 40 68 Q33 56 22 50 Z" fill="#9fe0bd" opacity="0.9" />
        <path d="M68 58 Q78 58 80 47 Q72 51 68 58 Z" fill="#9fe0bd" opacity="0.75" />
        <path d="M54 22 Q70 18 79 31 Q66 29 54 22 Z" fill="#9fe0bd" opacity="0.6" />

        <g stroke="#2e8f6a" stroke-width="1.2" fill="none" stroke-linecap="round">
          <rect x="60.5" y="25" width="7" height="7" rx="1.2" />
          <path d="M67.5 28 H73" /><path d="M64 25 V20.5" /><path d="M60.5 31.5 H55.5" /><path d="M67.5 31.5 L71 35" />
        </g>
        <g fill="#2e8f6a">
          <circle cx="73.6" cy="28" r="1.1" /><circle cx="64" cy="20" r="1.1" />
          <circle cx="55" cy="31.5" r="1.1" /><circle cx="71.4" cy="35.4" r="1.1" />
          <circle cx="64" cy="28.5" r="1.2" />
        </g>

        <circle cx="44" cy="27" r="1.5" fill="#2b4a54" />
        <circle cx="56" cy="27" r="1.5" fill="#2b4a54" />

        <path d="M48 45 H52" stroke="#2b4a54" stroke-width="3.2" stroke-linecap="round" />
        <circle cx="38" cy="46" r="12" fill="url(#spLensG)" stroke="#2b4a54" stroke-width="3.4" />
        <circle cx="62" cy="46" r="12" fill="url(#spLensG)" stroke="#2b4a54" stroke-width="3.4" />
        <circle cx="39" cy="48" r="6.5" fill="#19323b" />
        <circle cx="61" cy="48" r="6.5" fill="#19323b" />
        <circle cx="36.6" cy="45.4" r="2.4" fill="#ffffff" /><circle cx="58.6" cy="45.4" r="2.4" fill="#ffffff" />
        <circle cx="41.4" cy="50.6" r="1.3" fill="#ffffff" /><circle cx="63.4" cy="50.6" r="1.3" fill="#ffffff" />
        <circle cx="33.5" cy="41.5" r="1.4" fill="#ffffff" opacity="0.85" /><circle cx="57.5" cy="41.5" r="1.4" fill="#ffffff" opacity="0.85" />
        <circle cx="43.5" cy="42.5" r="1" fill="#ffffff" opacity="0.7" /><circle cx="65.5" cy="42.5" r="1" fill="#ffffff" opacity="0.7" />

        <path d="M45 60 Q50 65 55 60" fill="none" stroke="#2b4a54" stroke-width="2" stroke-linecap="round" />
      </g>

      <g class="orbit-ring orbit-ring-front" filter="url(#spRingGlow)" clip-path="url(#spFrontClip)">
        <ellipse cx="50" cy="48" rx="47" ry="19" fill="none" stroke="url(#spRingG)"
          stroke-width="3" transform="rotate(-18 50 48)" />
      </g>
    </svg>
  </div>

  <Teleport to="body">
    <transition name="sp-backdrop">
      <div v-if="panelOpen" class="sp-backdrop" @click="panelOpen = false" />
    </transition>
    <transition name="sp-drawer">
      <div v-if="panelOpen" class="sp-drawer">
        <div class="sp-drawer-header">
          <div class="sp-drawer-titles">
            <span class="sp-kicker">VSPIDER</span>
            <h2>实时画面</h2>
          </div>
          <button class="sp-drawer-close" @click="panelOpen = false">&times;</button>
        </div>
        <div class="sp-drawer-body">
          <div class="sp-status" :class="running ? 'running' : 'idle'">
            <span class="sp-status-dot" />
            <span>{{ running ? '运行中 · 实时画面' : '空闲 · 等待任务' }}</span>
          </div>
          <slot />
        </div>
      </div>
    </transition>
  </Teleport>
</template>

<style scoped>
.spider-fab {
  position: fixed;
  bottom: 32px;
  right: 32px;
  width: 56px;
  height: 56px;
  cursor: grab;
  z-index: 1000;
  transition: filter 0.3s;
  user-select: none;
}

.spider-fab:hover { filter: drop-shadow(0 0 10px rgba(34,211,238,0.4)); }
.spider-fab svg { width: 100%; height: 100%; overflow: visible; }

.spider-fab.running .orbit-ring {
  animation: ring-shimmer 2.4s ease-in-out infinite;
}

@keyframes ring-shimmer {
  0%, 100% { opacity: 0.78; }
  50% { opacity: 1; }
}

.spider-bob { transform-origin: 50% 56%; }
.spider-fab.running .spider-bob {
  animation: spider-bob 1.8s ease-in-out infinite;
}

@keyframes spider-bob {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-2px); }
}

.spider-fab.running::after {
  content: '';
  position: absolute;
  inset: -5px;
  border-radius: 50%;
  background: radial-gradient(circle, rgba(34,211,238,0.14) 0%, transparent 70%);
  animation: glow-pulse 2s ease-in-out infinite;
  pointer-events: none;
}

@keyframes glow-pulse {
  0%, 100% { opacity: 0.4; transform: scale(1); }
  50% { opacity: 1; transform: scale(1.06); }
}

.sp-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.06);
  z-index: 999;
}

.sp-drawer {
  position: fixed;
  top: 0; right: 0; bottom: 0;
  width: 420px;
  max-width: 92vw;
  background: var(--vsp-surface);
  z-index: 1001;
  box-shadow: -8px 0 30px rgb(var(--rgb-teal-deep) / 0.14);
  display: flex;
  flex-direction: column;
}

.sp-drawer-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 20px;
  border-bottom: 1px solid var(--vsp-border);
  /* faint brand wash + a 56x2 emerald→cyan hairline pinned bottom-left */
  background:
    linear-gradient(90deg, var(--vsp-accent), var(--vsp-cyan-bright)) 0 100% / 56px 2px no-repeat,
    linear-gradient(90deg, rgb(var(--rgb-accent) / 0.06), transparent 55%);
}

.sp-drawer-titles {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.sp-kicker {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--vsp-cyan-700);
}

.sp-drawer-header h2 {
  font-size: 16px;
  font-weight: 700;
  color: var(--vsp-teal-deep);
}

.sp-drawer-close {
  width: 30px; height: 30px;
  border: none; background: var(--vsp-surface-sunken);
  border-radius: 8px;
  font-size: 16px;
  cursor: pointer;
  color: var(--vsp-text-muted);
  display: grid;
  place-items: center;
  transition: all 0.15s;
}

.sp-drawer-close:hover { background: var(--vsp-surface-mint); color: var(--vsp-accent); }

.sp-drawer-body {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
}

.sp-status {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 13px;
  margin-bottom: 14px;
}

.sp-status {
  font-weight: 600;
  letter-spacing: 0.01em;
}

.sp-status.idle {
  background: var(--vsp-bg-deep);
  border: 1px solid var(--vsp-border);
  color: var(--vsp-text-muted);
}

.sp-status.running {
  background: var(--vsp-surface-mint);
  border: 1px solid rgb(var(--rgb-accent) / 0.35);
  color: var(--vsp-cyan-700);
}

.sp-status-dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.sp-status.idle .sp-status-dot { background: var(--vsp-text-faint); }
.sp-status.running .sp-status-dot {
  background: var(--vsp-accent);
  box-shadow: 0 0 0 3px rgb(var(--rgb-accent) / 0.18);
  animation: sp-dot-pulse 1.5s ease-in-out infinite;
}

@keyframes sp-dot-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

/* transitions */
.sp-backdrop-enter-active,
.sp-backdrop-leave-active { transition: opacity 0.25s; }
.sp-backdrop-enter-from,
.sp-backdrop-leave-to { opacity: 0; }

.sp-drawer-enter-active { transition: transform 0.3s cubic-bezier(0.22,1,0.36,1); }
.sp-drawer-leave-active { transition: transform 0.25s ease-in; }
.sp-drawer-enter-from,
.sp-drawer-leave-to { transform: translateX(100%); }
</style>
