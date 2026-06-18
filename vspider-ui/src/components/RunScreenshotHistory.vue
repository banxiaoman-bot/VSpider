<script setup>
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'

const props = defineProps({
  frames: { type: Array, default: () => [] },
  running: { type: Boolean, default: false },
})

const selected = ref(0)
const following = ref(true)
const playing = ref(false)
const lightbox = ref(false)
const stripRef = ref(null)
let playTimer = null

const speeds = [
  { label: '0.5x', ms: 480 },
  { label: '1x', ms: 280 },
  { label: '2x', ms: 140 },
]
const speedIdx = ref(1)
const intervalMs = computed(() => speeds[speedIdx.value].ms)

const count = computed(() => props.frames.length)
const current = computed(() => props.frames[selected.value] || null)
const atLatest = computed(() => selected.value >= count.value - 1)

watch(() => props.frames.length, (len) => {
  if (len === 0) {
    selected.value = 0
    following.value = true
    stopPlay()
    return
  }
  if (selected.value > len - 1) selected.value = len - 1
  if (following.value && !playing.value) {
    selected.value = len - 1
    scrollThumbIntoView()
  }
})

function select(i) {
  selected.value = i
  following.value = i >= count.value - 1
  stopPlay()
  scrollThumbIntoView()
}

function prev() {
  if (selected.value > 0) {
    selected.value--
    following.value = false
    scrollThumbIntoView()
  }
}

function next() {
  if (selected.value < count.value - 1) {
    selected.value++
    following.value = atLatest.value
    scrollThumbIntoView()
  }
}

function goLatest() {
  selected.value = count.value - 1
  following.value = true
  stopPlay()
  scrollThumbIntoView()
}

function tick() {
  if (selected.value >= count.value - 1) {
    stopPlay()
    return
  }
  selected.value++
  scrollThumbIntoView()
}

function togglePlay() {
  if (playing.value) {
    stopPlay()
    return
  }
  if (count.value === 0) return
  if (atLatest.value) selected.value = 0
  following.value = false
  playing.value = true
  playTimer = setInterval(tick, intervalMs.value)
}

function stopPlay() {
  playing.value = false
  if (playTimer) {
    clearInterval(playTimer)
    playTimer = null
  }
}

function setSpeed(i) {
  speedIdx.value = i
  if (playing.value) {
    if (playTimer) clearInterval(playTimer)
    playTimer = setInterval(tick, intervalMs.value)
  }
}

function downloadCurrent() {
  if (!current.value) return
  const a = document.createElement('a')
  a.href = current.value.src
  a.download = `screenshot-${String(selected.value + 1).padStart(3, '0')}.png`
  document.body.appendChild(a)
  a.click()
  a.remove()
}

function onScrub(e) {
  selected.value = Number(e.target.value)
  following.value = atLatest.value
  stopPlay()
  scrollThumbIntoView()
}

async function scrollThumbIntoView() {
  await nextTick()
  const strip = stripRef.value
  if (!strip) return
  const el = strip.querySelector('.rsh-thumb.active')
  if (el && el.scrollIntoView) {
    el.scrollIntoView({ inline: 'center', block: 'nearest', behavior: 'smooth' })
  }
}

function openLightbox() {
  if (current.value) lightbox.value = true
}

function closeLightbox() {
  lightbox.value = false
}

function onKeydown(e) {
  if (!count.value) return
  const t = e.target
  const tag = t && t.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (t && t.isContentEditable)) return
  if (e.key === 'ArrowLeft') {
    e.preventDefault()
    prev()
  } else if (e.key === 'ArrowRight') {
    e.preventDefault()
    next()
  } else if (e.key === 'Escape') {
    if (lightbox.value) closeLightbox()
  } else if (e.key === ' ' || e.key === 'Spacebar') {
    if (tag === 'BUTTON') return
    e.preventDefault()
    togglePlay()
  }
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  stopPlay()
})
</script>

<template>
  <div class="rsh">
    <template v-if="count > 0">
      <div class="rsh-main" title="点击放大" @click="openLightbox">
        <img :src="current.src" alt="运行画面" />
        <span class="rsh-zoom">⤢</span>
        <span v-if="running && atLatest" class="rsh-live"><i />LIVE</span>
      </div>

      <div class="rsh-controls">
        <button class="rsh-btn" :disabled="selected === 0" title="上一帧" @click="prev">‹</button>
        <button class="rsh-btn rsh-play" :title="playing ? '暂停' : '逐帧回放'" @click="togglePlay">
          {{ playing ? '⏸' : '▶' }}
        </button>
        <button class="rsh-btn" :disabled="atLatest" title="下一帧" @click="next">›</button>
        <input
          class="rsh-range" type="range" min="0" :max="count - 1"
          :value="selected" @input="onScrub"
        />
        <span class="rsh-count">{{ selected + 1 }} / {{ count }}</span>
        <button v-if="!atLatest" class="rsh-btn rsh-latest" title="回到最新画面" @click="goLatest">
          实时
        </button>
      </div>

      <div ref="stripRef" class="rsh-strip">
        <button
          v-for="(f, i) in frames" :key="i"
          class="rsh-thumb" :class="{ active: i === selected }"
          @click="select(i)"
        >
          <img :src="f.src" alt="" loading="lazy" />
          <span class="rsh-thumb-idx">{{ i + 1 }}</span>
        </button>
      </div>
    </template>

    <div v-else class="rsh-empty">启动任务后显示实时画面</div>

    <Teleport to="body">
      <transition name="rsh-fade">
        <div v-if="lightbox" class="rsh-lightbox" @click="closeLightbox">
          <button class="rsh-lb-close" @click.stop="closeLightbox">&times;</button>
          <button class="rsh-lb-nav left" :disabled="selected === 0" @click.stop="prev">‹</button>
          <img v-if="current" :src="current.src" alt="运行画面放大" @click.stop />
          <button class="rsh-lb-nav right" :disabled="atLatest" @click.stop="next">›</button>
          <div class="rsh-lb-bar" @click.stop>
            <button class="rsh-lb-btn" :title="playing ? '暂停' : '播放'" @click="togglePlay">
              {{ playing ? '⏸' : '▶' }}
            </button>
            <div class="rsh-lb-speed">
              <button
                v-for="(s, i) in speeds" :key="s.label"
                :class="{ on: i === speedIdx }" @click="setSpeed(i)"
              >{{ s.label }}</button>
            </div>
            <span class="rsh-lb-count">{{ selected + 1 }} / {{ count }}</span>
            <button class="rsh-lb-btn rsh-lb-dl" title="下载当前帧" @click="downloadCurrent">⤓ 下载</button>
          </div>
        </div>
      </transition>
    </Teleport>
  </div>
</template>

<style scoped>
.rsh { display: flex; flex-direction: column; gap: 10px; }

.rsh-main {
  position: relative;
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid var(--vsp-border, #e5e7eb);
  background: #0f172a;
  cursor: zoom-in;
  line-height: 0;
}

.rsh-main img { width: 100%; display: block; }

.rsh-zoom {
  position: absolute;
  right: 8px; bottom: 8px;
  width: 24px; height: 24px;
  display: grid; place-items: center;
  border-radius: 6px;
  background: rgba(15, 23, 42, 0.55);
  color: #fff;
  font-size: 13px;
  opacity: 0;
  transition: opacity 0.15s;
}

.rsh-main:hover .rsh-zoom { opacity: 1; }

.rsh-live {
  position: absolute;
  left: 8px; top: 8px;
  display: inline-flex; align-items: center; gap: 5px;
  padding: 3px 8px;
  border-radius: 6px;
  background: rgba(16, 185, 129, 0.9);
  color: #fff;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.3px;
}

.rsh-live i {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: #fff;
  animation: rsh-blink 1.2s ease-in-out infinite;
}

@keyframes rsh-blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }

.rsh-controls {
  display: flex;
  align-items: center;
  gap: 8px;
}

.rsh-btn {
  min-width: 28px; height: 28px;
  padding: 0 6px;
  border: 1px solid var(--vsp-border, #e5e7eb);
  background: #fff;
  border-radius: 6px;
  color: #374151;
  font-size: 14px;
  cursor: pointer;
  display: grid; place-items: center;
  transition: all 0.15s;
}

.rsh-btn:hover:not(:disabled) { border-color: #10b981; color: #10b981; }
.rsh-btn:disabled { opacity: 0.4; cursor: not-allowed; }

.rsh-play { background: #10b981; border-color: #10b981; color: #fff; }
.rsh-play:hover:not(:disabled) { background: #0ea372; color: #fff; }

.rsh-latest {
  width: auto;
  font-size: 12px;
  color: #10b981;
  border-color: #bbf7d0;
  background: #f0fdf4;
}

.rsh-range { flex: 1; accent-color: #10b981; cursor: pointer; }

.rsh-count {
  font-size: 12px;
  color: var(--vsp-text-faint, #9ca3af);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.rsh-strip {
  display: flex;
  gap: 6px;
  overflow-x: auto;
  padding-bottom: 4px;
  scrollbar-width: thin;
}

.rsh-thumb {
  position: relative;
  flex: 0 0 auto;
  width: 64px; height: 42px;
  padding: 0;
  border: 2px solid transparent;
  border-radius: 6px;
  overflow: hidden;
  cursor: pointer;
  background: #0f172a;
  line-height: 0;
  transition: border-color 0.15s, transform 0.15s;
}

.rsh-thumb img { width: 100%; height: 100%; object-fit: cover; }
.rsh-thumb:hover { transform: translateY(-1px); }
.rsh-thumb.active { border-color: #10b981; }

.rsh-thumb-idx {
  position: absolute;
  right: 2px; bottom: 1px;
  padding: 0 3px;
  border-radius: 3px;
  background: rgba(15, 23, 42, 0.6);
  color: #fff;
  font-size: 9px;
  line-height: 14px;
}

.rsh-empty {
  text-align: center;
  color: var(--vsp-text-faint, #9ca3af);
  font-size: 13px;
  padding: 32px 0;
}

.rsh-lightbox {
  position: fixed;
  inset: 0;
  z-index: 2000;
  background: rgba(15, 23, 42, 0.82);
  display: grid;
  place-items: center;
  cursor: zoom-out;
}

.rsh-lightbox img {
  max-width: 92vw;
  max-height: 86vh;
  border-radius: 8px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
  cursor: default;
}

.rsh-lb-close {
  position: absolute;
  top: 20px; right: 24px;
  width: 40px; height: 40px;
  border: none;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.14);
  color: #fff;
  font-size: 22px;
  cursor: pointer;
}

.rsh-lb-close:hover { background: rgba(255, 255, 255, 0.26); }

.rsh-lb-nav {
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  width: 46px; height: 46px;
  border: none;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.14);
  color: #fff;
  font-size: 26px;
  cursor: pointer;
}

.rsh-lb-nav:hover:not(:disabled) { background: rgba(255, 255, 255, 0.26); }
.rsh-lb-nav:disabled { opacity: 0.3; cursor: not-allowed; }
.rsh-lb-nav.left { left: 24px; }
.rsh-lb-nav.right { right: 24px; }

.rsh-lb-bar {
  position: absolute;
  bottom: 22px; left: 50%;
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.14);
  backdrop-filter: blur(4px);
  cursor: default;
}

.rsh-lb-btn {
  height: 30px;
  padding: 0 12px;
  border: none;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.16);
  color: #fff;
  font-size: 13px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.rsh-lb-btn:hover { background: rgba(255, 255, 255, 0.3); }
.rsh-lb-dl { background: rgba(16, 185, 129, 0.85); }
.rsh-lb-dl:hover { background: rgba(16, 185, 129, 1); }

.rsh-lb-speed {
  display: flex;
  gap: 2px;
  background: rgba(255, 255, 255, 0.1);
  border-radius: 999px;
  padding: 2px;
}

.rsh-lb-speed button {
  border: none;
  background: transparent;
  color: rgba(255, 255, 255, 0.7);
  font-size: 12px;
  padding: 4px 9px;
  border-radius: 999px;
  cursor: pointer;
}

.rsh-lb-speed button.on {
  background: #10b981;
  color: #fff;
}

.rsh-lb-count {
  color: #fff;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.rsh-fade-enter-active, .rsh-fade-leave-active { transition: opacity 0.2s; }
.rsh-fade-enter-from, .rsh-fade-leave-to { opacity: 0; }
</style>
