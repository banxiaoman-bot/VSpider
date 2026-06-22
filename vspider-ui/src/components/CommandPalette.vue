<script setup>
import { computed, ref, watch } from 'vue'

const props = defineProps({
  visible: { type: Boolean, default: false },
  suggestions: { type: Array, default: () => [] },
})
const emit = defineEmits(['select', 'dismiss'])

const activeIndex = ref(0)

watch(() => props.suggestions, () => { activeIndex.value = 0 })

function select (cmd) {
  emit('select', cmd)
}

function onKeydown (e) {
  if (!props.visible) return
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    activeIndex.value = (activeIndex.value + 1) % props.suggestions.length
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    activeIndex.value = (activeIndex.value - 1 + props.suggestions.length) % props.suggestions.length
  } else if (e.key === 'Tab' || (e.key === 'Enter' && !e.ctrlKey)) {
    if (props.suggestions.length) {
      e.preventDefault()
      select(props.suggestions[activeIndex.value])
    }
  } else if (e.key === 'Escape') {
    emit('dismiss')
  }
}

defineExpose({ onKeydown })
</script>

<template>
  <Transition name="palette-fade">
    <div v-if="visible && suggestions.length" class="command-palette">
      <div
        v-for="(cmd, i) in suggestions"
        :key="cmd.name"
        class="command-palette__item"
        :class="{ 'is-active': i === activeIndex }"
        @mouseenter="activeIndex = i"
        @click="select(cmd)"
      >
        <span class="command-palette__name">/{{ cmd.name }}</span>
        <span v-if="cmd.args" class="command-palette__args">{{ cmd.args }}</span>
        <span class="command-palette__desc">{{ cmd.description }}</span>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.command-palette {
  position: absolute;
  top: 100%;
  left: 0;
  right: 0;
  z-index: 100;
  background: var(--vspider-panel-bg, #1a1a2e);
  border: 1px solid var(--vspider-border, rgba(255, 255, 255, 0.08));
  border-radius: 8px;
  margin-top: 4px;
  padding: 4px 0;
  box-shadow: 0 4px 24px rgba(0, 0, 0, 0.4);
  max-height: 280px;
  overflow-y: auto;
}

.command-palette__item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  cursor: pointer;
  transition: background 0.12s;
  font-size: 13px;
}

.command-palette__item.is-active {
  background: rgba(100, 108, 255, 0.15);
}

.command-palette__name {
  color: #a78bfa;
  font-weight: 600;
  font-family: 'Fira Code', 'Cascadia Code', monospace;
  min-width: 90px;
}

.command-palette__args {
  color: rgba(255, 255, 255, 0.35);
  font-family: 'Fira Code', 'Cascadia Code', monospace;
  font-size: 12px;
}

.command-palette__desc {
  color: rgba(255, 255, 255, 0.55);
  margin-left: auto;
  font-size: 12px;
}

.palette-fade-enter-active,
.palette-fade-leave-active {
  transition: opacity 0.15s, transform 0.15s;
}

.palette-fade-enter-from,
.palette-fade-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
</style>
