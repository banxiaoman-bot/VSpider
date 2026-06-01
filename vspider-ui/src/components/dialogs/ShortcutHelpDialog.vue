<script setup>
import { computed } from 'vue'

const props = defineProps({
  visible: { type: Boolean, default: false },
})
const emit = defineEmits(['update:visible'])

const dialogVisible = computed({
  get: () => props.visible,
  set: (value) => emit('update:visible', value),
})
</script>

<template>
  <el-dialog
    v-model="dialogVisible"
    title="键盘快捷键 (Ctrl+/)"
    width="560px"
    class="shortcut-dialog"
    destroy-on-close
  >
    <div class="shortcut-dialog-body">
      <section class="shortcut-section">
        <h4>全局</h4>
        <table class="shortcut-table">
          <tbody>
            <tr><td><kbd>Ctrl</kbd>+<kbd>Enter</kbd></td><td>提交任务 (在输入框内也生效)</td></tr>
            <tr><td><kbd>Ctrl</kbd>+<kbd>K</kbd></td><td>聚焦业务指令输入框</td></tr>
            <tr><td><kbd>Ctrl</kbd>+<kbd>1</kbd>..<kbd>6</kbd></td><td>切换底部 tab (Terminal/Timeline/Capability/Final/Artifacts/失败记录)</td></tr>
            <tr><td><kbd>Ctrl</kbd>+<kbd>/</kbd></td><td>打开 / 关闭本对话框</td></tr>
          </tbody>
        </table>
      </section>

      <section class="shortcut-section">
        <h4>Live Terminal 搜索 (X)</h4>
        <table class="shortcut-table">
          <tbody>
            <tr><td><kbd>Ctrl</kbd>+<kbd>F</kbd></td><td>在 Terminal tab 内打开日志搜索</td></tr>
            <tr><td><kbd>Enter</kbd></td><td>下一个匹配</td></tr>
            <tr><td><kbd>Shift</kbd>+<kbd>Enter</kbd></td><td>上一个匹配</td></tr>
            <tr><td><kbd>Esc</kbd></td><td>关闭搜索栏</td></tr>
          </tbody>
        </table>
      </section>

      <section class="shortcut-section">
        <h4>Timeline tab</h4>
        <table class="shortcut-table">
          <tbody>
            <tr><td><kbd>Ctrl</kbd>+<kbd>E</kbd></td><td>导出当前过滤后的 phase 事件为 JSONL</td></tr>
            <tr><td>统计</td><td>展开 phase 耗时分布 / 严重度统计 (V)</td></tr>
            <tr><td>导入回放</td><td>导入 phase JSONL 离线重建 Timeline (W)</td></tr>
            <tr><td><kbd>End</kbd></td><td>跳到底部 + 恢复自动跟随</td></tr>
            <tr><td><kbd>Home</kbd></td><td>跳到顶部 + 暂停自动跟随</td></tr>
          </tbody>
        </table>
      </section>

      <section class="shortcut-section">
        <h4>Capability tab</h4>
        <table class="shortcut-table">
          <tbody>
            <tr><td><kbd>Ctrl</kbd>+<kbd>E</kbd></td><td>导出 capability_route / capability_execute 为 JSONL</td></tr>
          </tbody>
        </table>
      </section>

      <section class="shortcut-section">
        <h4>事件详情对话框</h4>
        <table class="shortcut-table">
          <tbody>
            <tr><td><kbd>←</kbd> / <kbd>→</kbd></td><td>切换上一条 / 下一条事件 (走过滤后的列表)</td></tr>
            <tr><td><kbd>Esc</kbd></td><td>关闭对话框</td></tr>
          </tbody>
        </table>
      </section>

      <section class="shortcut-section">
        <h4>Timeline chip</h4>
        <table class="shortcut-table">
          <tbody>
            <tr><td>单击 chip</td><td>打开 JSON 详情对话框</td></tr>
            <tr><td>双击 chip</td><td>直接复制完整 JSON 到剪贴板 (跳过对话框)</td></tr>
          </tbody>
        </table>
      </section>

      <section class="shortcut-section">
        <h4>失败记录 (K6)</h4>
        <table class="shortcut-table">
          <tbody>
            <tr><td>单击行</td><td>打开失败 run 详情对话框 (含 JSON + phase 事件预览)</td></tr>
            <tr><td><kbd>←</kbd> / <kbd>→</kbd></td><td>详情对话框打开时，切换上一条 / 下一条 run</td></tr>
          </tbody>
        </table>
      </section>

      <p class="shortcut-footnote">
        macOS 用户：<kbd>Ctrl</kbd> 可替换为 <kbd>⌘</kbd> Cmd。
      </p>
    </div>
    <template #footer>
      <el-button size="small" @click="dialogVisible = false">关闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.shortcut-dialog-body {
  display: flex;
  flex-direction: column;
  gap: 14px;
  color: var(--vsp-text-2);
}

.shortcut-section h4 {
  margin: 0 0 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--vsp-text-strong);
  letter-spacing: 0.02em;
}

.shortcut-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
}

.shortcut-table td {
  padding: 4px 8px;
  vertical-align: middle;
}

.shortcut-table td:first-child {
  width: 200px;
  white-space: nowrap;
}

.shortcut-table kbd {
  display: inline-block;
  padding: 1px 6px;
  margin: 0 1px;
  border-radius: 4px;
  background: var(--vsp-gray-800);
  border: 1px solid var(--vsp-slate-700);
  border-bottom-width: 2px;
  color: var(--vsp-text-strong);
  font-family: Consolas, 'JetBrains Mono', monospace;
  font-size: 11.5px;
  line-height: 1.4;
}

.shortcut-footnote {
  margin: 0;
  padding-top: 4px;
  font-size: 12px;
  color: var(--vsp-text-muted);
  border-top: 1px dashed rgb(var(--rgb-slate) / 0.18);
}
</style>
