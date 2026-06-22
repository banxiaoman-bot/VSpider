# App.vue 二期收尾拆分（P2）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 `executing-plans` 逐任务实现此计划，`test-driven-development` 先写测试。每个 Slice 独立 commit，三绿（vitest / npm run build / ReadLints）+ 结构化 pytest 回归后再进入下一刀。

**目标：** 把 `vspider-ui/src/App.vue` `<script setup>`（当前 590 行）内残留的「聚合 / 接线胶水群」继续抽成 composable，使 App.vue 进一步退化为薄接线层。
**架构：** 沿用 D-UI-17~24 已验证范式——新建 `composables/use*.js`，把内联 ref/函数迁入，App.vue 用**同名 destructure** 接线（模板/handler 调用点逐字零改动）；可测纯逻辑抽成具名导出函数（参考 `useKeyboardCommand` 的 `resolveKeyboardAction` 拆法），生命周期包装（`onMounted`/`onUnmounted`）留 composable 内并用 `getCurrentInstance()` 守卫以便 node 测试环境零警告。
**技术栈：** Vue 3 `<script setup>`、Element Plus、Vitest（node 环境，无 jsdom）、纯函数优先。

---

## 现状基线（baa0afe / D-UI-24 后实测）

- `App.vue` = 1010 行：`<script setup>` 1–591、`<template>` 593–1008、`<style src>` 1010。
- 已抽 24 个 composable（`vspider-ui/src/composables/`），20 个 `tests/*.test.js`。
- 残留内联「聚合 / 接线」群（行号为当前实测，**抽刀后会顺移，执行时以实测为准**）：

  | 区块 | 行范围 | 体量 | 归属 Slice |
  |---|---|---|---|
  | output-contract 预览（3 ref + refresh + watch + 清理） | 61–63 / 499–522 / 564–567 | ~30 | D-UI-25 |
  | 键盘接线（2 ref + focus + actions 表 + context + 调用） | 179–180 / 453–497 | ~50 | D-UI-26 |
  | capability-trace 聚合（trace + fixtureReplay + clear + export + more 分发） | 345–413 / 575–589 | ~90 | D-UI-27 |
  | run 流接线（scrollToBottom + RunEventRouter + WebSocket） | 304–343 | ~40 | D-UI-28 |
  | onMounted 启动序列 + finalAnswerCopy 反馈（2 ref + 清理） | 236–237 / 524–554 / 568–573 | ~40 | D-UI-29 |

## 文件结构（将创建/修改）

- **新建** `composables/useOutputContractPreview.js` — output_contract 实时预览（debounce + fetch + 清理）。
- **新建** `composables/useAppKeyboard.js` — 键盘动作表 + 上下文 + `useKeyboardCommand` 接线。
- **新建** `composables/useCapabilityTracePanel.js` — capability-trace 三件套聚合 + clear + more 分发。
- **新建** `composables/useRunStream.js` — WS 连接 + 事件路由接线。
- **新建** `composables/useAppBootstrap.js` — onMounted 启动序列；附带 `composables/useCopyFeedback.js`（copy 反馈态）。
- **修改** `App.vue` — 每刀删除对应内联块，改 destructure 接线。
- **新建** `tests/` 下对应 5（+1）个 `*.test.js`（vitest）。
- **追加** `docs/vspider_architecture_backlog.md` — 每刀一行 Slice 记录。

## 排序原则

自包含叶子（output-contract）→ 接线胶水（键盘）→ 大聚合块（capability-trace）→ 流接线（WS）→ 启动序列收尾。每刀后 App.vue 仍可独立编译、模板/handler 行为零变更。

---

## Slice D-UI-25 — useOutputContractPreview（自包含叶子，最低风险）

- **文件：** 新建 `vspider-ui/src/composables/useOutputContractPreview.js`；改 `App.vue`；新建 `vspider-ui/tests/useOutputContractPreview.test.js`。
- **抽出：** refs `outputContractPreview`(61) `outputContractPreviewLoading`(62) + 内联 `let outputContractPreviewTimer`(63)；`refreshOutputContractPreview`(499–515)；`watch(prompt, …)`(517–522) 的 450ms debounce；`onUnmounted` 的 timer 清理(564–567)。
- **导出：**
```js
import { ref, watch, onUnmounted, getCurrentInstance } from 'vue'
import { fetchOutputContractPreview } from './useTaskSubmit'

export const OUTPUT_CONTRACT_PREVIEW_DEBOUNCE_MS = 450

export function useOutputContractPreview ({ prompt }) {
  const outputContractPreview = ref(null)
  const outputContractPreviewLoading = ref(false)
  let timer = null

  const refreshOutputContractPreview = async () => {
    const text = String(prompt.value || '').trim()
    if (!text) {
      outputContractPreview.value = null
      outputContractPreviewLoading.value = false
      return
    }
    outputContractPreviewLoading.value = true
    try {
      const result = await fetchOutputContractPreview(text)
      outputContractPreview.value = result.status === 'success' ? result.formatted : null
    } catch {
      outputContractPreview.value = null
    } finally {
      outputContractPreviewLoading.value = false
    }
  }

  watch(prompt, () => {
    if (timer) clearTimeout(timer)
    timer = setTimeout(() => { refreshOutputContractPreview() }, OUTPUT_CONTRACT_PREVIEW_DEBOUNCE_MS)
  })

  if (getCurrentInstance()) {
    onUnmounted(() => { if (timer) { clearTimeout(timer); timer = null } })
  }

  return { outputContractPreview, outputContractPreviewLoading, refreshOutputContractPreview }
}
```
- **接线：** `App.vue` 删 61–63 / 499–522 / 564–567；顶部 `import { useOutputContractPreview } from './composables/useOutputContractPreview.js'`；在 `useTaskForm` 接线后 destructure `const { outputContractPreview, outputContractPreviewLoading } = useOutputContractPreview({ prompt })`。模板 637–642 引用名不变。`fetchOutputContractPreview` 的 import（53–57 块）若仅本处使用则随之移除。
- **测试（mock `../src/composables/useTaskSubmit` 的 `fetchOutputContractPreview`）：** ① 空 prompt → preview=null、loading=false、不触网；② success → preview=formatted；③ status≠success → preview=null；④ fetch 抛错 → preview=null 且 finally loading=false；⑤ watch debounce：`vi.useFakeTimers()`，改 prompt → `await nextTick()` → `await vi.advanceTimersByTimeAsync(450)` → 触发一次 fetch；⑥ 450ms 内连改两次 → fetch 仅一次（timer 重置）。
- **验证：** `cd vspider-ui && npx vitest run tests/useOutputContractPreview.test.js`（红→绿）→ `npx vitest run`（全绿）→ `npm run build`（绿）→ ReadLints clean → 仓库根 `python scripts/validate_y.py` 不必（纯 UI），改跑结构化 pytest 抽样作回归。
- **提交：** `D-UI-25 useOutputContractPreview 抽离`（能力名 output_contract_preview_extraction / 影响层 App.vue→composables / 无新 contract 字段）。

## Slice D-UI-26 — useAppKeyboard（键盘接线胶水）

- **文件：** 新建 `composables/useAppKeyboard.js`；改 `App.vue`；新建 `tests/useAppKeyboard.test.js`。
- **抽出：** refs `helpDialogVisible`(179) `promptInputRef`(180)；`focusPromptInput`(453–468)；`keyboardActions`(470–487)；`getKeyboardContext`(489–495)；`useKeyboardCommand({ getContext, actions })`(497)。
- **可测纯逻辑（具名导出，参考 `resolveKeyboardAction` 拆法）：**
  - `buildKeyboardActions(deps)` → 返回 actions 表（`submitIfIdle` 仅在 `!isRunning.value` 调 `submitTask`；`selectTab(i)` → `setActiveBottomTab(TAB_ORDER[i])`；其余转调注入句柄）。
  - `buildKeyboardContext(deps)` → 读 refs 组 `{ activeTab, tabCount, phaseDialogOpen, failedDialogOpen, terminalSearchVisible }`。
- **导出：** `useAppKeyboard(deps)` → `{ helpDialogVisible, promptInputRef, focusPromptInput }`；内部建 actions/context 并调 `useKeyboardCommand`（其 `onMounted/onUnmounted` 已自带）。`deps`：`isRunning, submitTask, terminalLogPaneRef, setActiveBottomTab, TAB_ORDER, timelinePanelRef, failedRunsPaneRef, exportCapabilityTraceAsJsonl`。
- **接线：** App.vue 删上述块，destructure `helpDialogVisible/promptInputRef/focusPromptInput`；模板/slash 注入 `helpDialogVisible`、`registerBuiltinCommands` 的 `helpDialogVisible` 不变；`promptInputRef` 仍由 useTaskForm 注入读取（保持同名）。
- **测试：** ① `buildKeyboardActions`：`isRunning=true` 时 `submitIfIdle` 不调 submitTask、`false` 时调；② `selectTab(2)` → `setActiveBottomTab` 收到 `TAB_ORDER[2]`；③ 每个 action 转调对应 stub（terminalSearchOpen/toggleHelp/phasePrev…）；④ `buildKeyboardContext` 按 refs 组出正确 ctx 形状（含 `!!` 归一）。
- **验证 / 提交：** 同 D-UI-25 节奏；commit `D-UI-26 useAppKeyboard 抽离`。

## Slice D-UI-27 — useCapabilityTracePanel（最大聚合块）

- **文件：** 新建 `composables/useCapabilityTracePanel.js`；改 `App.vue`；新建 `tests/useCapabilityTracePanel.test.js`。
- **抽出：** `useCapabilityTrace(phaseEvents)`(345–375) + 其全量 destructure；`useCapabilityFixtureReplay({…})`(376–394) + destructure；`clearPhaseEvents`(395–401)；`useCapabilityTraceExport({…})`(407–413)；`handleCapabilityMoreAction`(575–589)。
- **可测纯逻辑（具名导出）：**
  - `buildCapabilityMoreActionHandler(handlers)` → `(command) => handlers[command]?.()`，命令键集合：`copySummary/generateFixture/replayFixture/refreshFixtures/refreshBatchHistory/batchReplay/replayEfficiency/refreshEfficiencyReplays/importReplay`。
  - `makeClearPhaseEvents({ phaseEvents, hasNewPhase, hasNewCapability, timelineAutoScroll })` → 清空 phaseEvents、`hasNewPhase=false`、`hasNewCapability=false`、`timelineAutoScroll=true`。
- **导出：** `useCapabilityTracePanel({ phaseEvents, hasNewPhase, hasNewCapability, timelineAutoScroll, url, prompt, fetchArtifacts, writeToClipboard, triggerReplayImport })` → 扁平展开 `...capabilityTrace, ...fixtureReplay, clearPhaseEvents, exportCapabilityTraceAsJsonl, copyCapabilityTraceSummary, handleCapabilityMoreAction`。
- **接线：** App.vue 删上述 5 块，单条 destructure 接出全部模板绑定字段（名字逐字不变）。注意 `handleCapabilityMoreAction` 内引用的 `triggerReplayImport`（useTimelineReplay）需作为 dep 注入。
- **风险控制：** 子 composable（trace/fixtureReplay/export）已各有单测，本刀**新测只覆盖新增胶水**（more 分发表全键 + 未知键 no-op；clearPhaseEvents 四态复位）。模板绑定面广，靠同名 destructure + `npm run build`（模板编译期校验未定义变量）守回归。
- **验证 / 提交：** commit `D-UI-27 useCapabilityTracePanel 抽离`。

## Slice D-UI-28 — useRunStream（WS + 事件路由接线）

- **文件：** 新建 `composables/useRunStream.js`；改 `App.vue`；新建 `tests/useRunStream.test.js`。
- **抽出：** `scrollToBottom`(304)；`useRunEventRouter({…})`(306–333)；`useWebSocket({ onOpen/onMessage/onClose/onError })`(334–343)。
- **可测纯逻辑（具名导出）：** `buildWebSocketHandlers({ appendLog, handleSocketMessage })` → `{ onOpen, onMessage, onClose, onError }`，其中 `onMessage` 转调 `handleSocketMessage`，`onOpen/onClose/onError` 调 `appendLog` 带固定标签（`[SYSTEM] WebSocket connected` / `disconnected` / `[ERROR] WebSocket error`）。
- **导出：** `useRunStream(deps)` → `{ wsStatus, connectWebSocket, disconnectWebSocket, scrollToBottom, handleSocketMessage }`。`deps` = useRunEventRouter 全量注入 + `appendLog` + `terminalLogPaneRef`。`scrollToBottom = () => terminalLogPaneRef.value?.scrollToBottom()`。
- **接线：** App.vue 删上述块，destructure；`onMounted` 仍调 `connectWebSocket()`、`onUnmounted` 仍调 `disconnectWebSocket()`（保持 useWebSocket 既有 connect/disconnect 契约）。`scrollToBottom` 供 `createTerminalLogBuffer` 的 onFlush 闭包使用（注意：buffer 在 setup 顶部创建、scrollToBottom 现由本 composable 提供 → 用与现状一致的「延迟查找」箭头，保留 `() => { scrollToBottom() }` 包装即可）。
- **测试：** `buildWebSocketHandlers`：① onMessage(payload) → handleSocketMessage 收到同参；② onOpen/onClose/onError → appendLog 收到对应标签字符串。
- **验证 / 提交：** commit `D-UI-28 useRunStream 抽离`。

## Slice D-UI-29 — useAppBootstrap + useCopyFeedback（启动序列收尾）

- **文件：** 新建 `composables/useAppBootstrap.js`、`composables/useCopyFeedback.js`；改 `App.vue`；新建 `tests/useAppBootstrap.test.js`、`tests/useCopyFeedback.test.js`。
- **抽出 A（copy 反馈）：** `finalAnswerCopyState`(236) + `let finalAnswerCopyTimer`(237) + onUnmounted 清理(568–573) → `useCopyFeedback({ resetMs = 1500 })` → `{ finalAnswerCopyState, flashCopyState(state) }`，内部 timer + `getCurrentInstance()` 守卫 onUnmounted。
- **抽出 B（启动序列）：** `onMounted`(524–554) 序列 → `useAppBootstrap(deps)`；可测纯逻辑 `runBootstrap(deps)` 顺序调用：`loadModelSettings → registerBuiltinCommands(slashRegistry, cmdDeps) → connectWebSocket → loadAuthProfiles → loadCaptchaSolverStatus → fetchArtifacts → fetchBrowserRuntimeStatus → failedRunsPaneRef.value?.fetchFailedRuns()`。composable 用 `onMounted(() => runBootstrap(deps))` 包装。
- **接线：** App.vue destructure `finalAnswerCopyState`（来自 useCopyFeedback）；onMounted 整块替换为 `useAppBootstrap({...})`；copy 按钮处把原 `finalAnswerCopyState.value=...＋setTimeout` 改调 `flashCopyState('ok'|'err')`。
- **测试：** `useCopyFeedback`（node 直调）：`flashCopyState('ok')` → state='ok'，`await vi.advanceTimersByTimeAsync(1500)` → 回 'idle'；`runBootstrap`：注入全 stub，断言**按序**全部被调用一次、`failedRunsPaneRef.value` 为空时不抛。
- **验证 / 提交：** commit `D-UI-29 useAppBootstrap+useCopyFeedback 抽离`。

---

## 每刀统一节奏（executing-plans + TDD）

1. 写新测（`tests/<name>.test.js`，mock 网络/模块依赖）→ 2. `npx vitest run tests/<name>.test.js` 跑确认红 → 3. 新建 composable + App.vue destructure 接线（CRLF：跨空行大块用一次性 `_patch_*.py` 按字节改，patch 完即删）→ 4. `npx vitest run` 全绿 → 5. `npm run build` 绿 → 6. ReadLints clean → 7. 结构化 pytest 抽样回归（纯 UI 不应影响）→ 8. 追加 backlog 一行 → 9. 单刀 commit（不 push，集中末尾或按需）。

## 自检（规格覆盖）

- 残留群表 5 项 → 全部映射到 D-UI-25~29，无遗漏。
- 类型一致性：`refreshOutputContractPreview` / `focusPromptInput` / `clearPhaseEvents` / `handleCapabilityMoreAction` / `scrollToBottom` / `flashCopyState` 命名跨刀一致；`OUTPUT_CONTRACT_PREVIEW_DEBOUNCE_MS=450` 与原内联一致。
- 无占位符：每刀给出确切文件、行号、导出签名、测试清单、验证命令；D-UI-25 含完整代码。
- 风险：D-UI-27 模板绑定面最大 → 靠同名 destructure + `npm run build` 模板编译校验 + 仅测新增胶水；node 测试环境无 jsdom → 生命周期包装用 `getCurrentInstance()` 守卫，单测只覆盖纯逻辑（与 `useKeyboardCommand` 既有做法一致）。
- 预期总收益：`<script setup>` 590 → ~340 行；App.vue 1010 → ~760 行。
