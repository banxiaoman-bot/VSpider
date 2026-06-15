<script setup>
import { Setting } from '@element-plus/icons-vue'

defineProps({
  intent: { type: Object, default: () => ({}) },
  eventCount: { type: Number, default: 0 },
  exportDisabled: { type: Boolean, default: true },
  replayMode: { type: Boolean, default: false },
  replaySourceName: { type: String, default: '' },
  batchReplayLoading: { type: Boolean, default: false },
  failureBundleValid: { type: Boolean, default: false },
  correlationReportValid: { type: Boolean, default: false },
})

const emit = defineEmits([
  'export-jsonl',
  'more-action',
  'exit-replay',
])
</script>

<template>
  <section class="capability-hero">
    <div>
      <div class="capability-kicker">Route-Aware Agent Guidance</div>
      <h3>{{ intent.task_type || 'unknown task' }}</h3>
      <p>
        输出模式：{{ intent.output_mode || 'default' }}
        <span v-if="intent.requires_artifact"> &middot; 需要产物</span>
        <span v-if="intent.requires_visual_grounding"> &middot; 需要视觉定位</span>
      </p>
    </div>
    <div class="capability-hero-actions">
      <span class="capability-count">{{ eventCount }} events</span>
      <el-button
        size="small"
        plain
        class="capability-export-btn"
        :disabled="exportDisabled"
        title="导出 capability_route / capability_execute 为 JSONL (Ctrl+E)"
        @click="emit('export-jsonl')"
      >
        导出 JSONL
      </el-button>
      <el-dropdown trigger="click" @command="(cmd) => emit('more-action', cmd)">
        <el-button size="small" plain class="capability-export-btn">
          更多操作 ⋯
        </el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="copySummary" :disabled="!eventCount">
              复制摘要
            </el-dropdown-item>
            <el-dropdown-item command="importReplay">
              导入回放
            </el-dropdown-item>
            <el-dropdown-item
              command="generateFixture"
              divided
              :disabled="!failureBundleValid"
            >
              生成 Fixture
            </el-dropdown-item>
            <el-dropdown-item
              command="replayFixture"
              :disabled="!failureBundleValid"
            >
              验证 Fixture
            </el-dropdown-item>
            <el-dropdown-item command="refreshFixtures">
              刷新 Fixture 库
            </el-dropdown-item>
            <el-dropdown-item command="refreshBatchHistory">
              刷新 Replay 历史
            </el-dropdown-item>
            <el-dropdown-item command="batchReplay" :disabled="batchReplayLoading">
              批量验证 Fixture
            </el-dropdown-item>
            <el-dropdown-item
              command="replayEfficiency"
              divided
              :disabled="!correlationReportValid"
            >
              验证 Efficiency
            </el-dropdown-item>
            <el-dropdown-item command="refreshEfficiencyReplays">
              刷新 Efficiency Replay
            </el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </div>
  </section>

  <div v-if="replayMode" class="timeline-replay-banner capability-replay-banner">
    <span class="replay-icon" aria-hidden="true">&#x25B6;</span>
    <span class="replay-text">
      Capability 回放模式
      <span v-if="replaySourceName" class="replay-source">
        &middot; {{ replaySourceName }}
      </span>
    </span>
    <el-button
      size="small"
      plain
      class="replay-exit-btn"
      title="退出回放，清空导入事件并重新接收实时事件"
      @click="emit('exit-replay')"
    >退出回放</el-button>
  </div>
</template>
