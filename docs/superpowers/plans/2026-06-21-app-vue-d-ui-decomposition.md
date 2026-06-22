# App.vue D-UI 收尾拆分 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 `executing-plans` 逐任务实现此计划。每个 Slice 独立 commit，三绿（vitest / npm run build / ReadLints）后再进入下一刀。

**目标：** 把 `vspider-ui/src/App.vue` `<script setup>` 内残留的内联逻辑（WS 事件路由 / 任务提交链路 / final answer / auth / 剪贴板 / 底部 Tab 状态）继续抽成 composable，使 App.vue 退化为「接线层」。
**架构：** 沿用 D-UI-11~16 已验证范式——新建 `composables/use*.js`，把内联 ref/函数迁入，App.vue 用**同名 destructure** 接线（模板/WS handler 调用点逐字零改动）；每个 composable 配 stub 测试。
**技术栈：** Vue 3 `<script setup>`、Element Plus、Vitest、纯函数优先。

---

## 现状基线（D-UI-16 后实测）

- `App.vue` = 1353 行：`<script setup>` 1–934、`<template>` 936–1351、`<style src>` 1353。
- 已抽 18 个 composable（`vspider-ui/src/composables/`）。
- 残留内联逻辑（行号为当前实测）：
  | 区块 | 行范围 | 体量 | 归属 Slice |
  |---|---|---|---|
  | `handleSocketMessage`（WS 路由） | 277–431 | ~155 | D-UI-21 |
  | Final Answer 状态 + watch + copy timer | 224–262 / 245–256 | ~40 | D-UI-19 |
  | Auth profiles + captcha | 463–494 | ~32 | D-UI-18 |
  | `_writeToClipboard` | 582–600 | ~19 | D-UI-17 |
  | 任务表单 + submit/forceStop + upload + output-contract preview + slash 胶水 | 64–82 / 444–461 / 692–744 / 746–865 | ~210 | D-UI-22 |
  | 底部 Tab + badges + setActiveBottomTab | 129–149 / 174–176 / 216 / 625–641 | ~50 | D-UI-20 |
  | settings drawer（2 ref） | 132–133 | 2 | 折入 D-UI-22 |
  | 键盘 actions/context/focus | 646–690 | ~45 | 可选（D-UI-25，低价值） |
  | `handleCapabilityMoreAction` | 919–932 | ~14 | 折入 D-UI-21 或保留 |

## 文件结构（将创建/修改）

- **新建** `composables/useClipboard.js` — `writeToClipboard(text)`（剪贴板写入 + execCommand 回退 + 错误 toast）。
- **新建** `composables/useAuthProfiles.js` — auth profile 选择 + captcha solver 状态。
- **新建** `composables/useFinalAnswer.js` — final answer 面板状态 + done 结果解析 + 自动切 Tab watch + copy timer 清理。
- **新建** `composables/useBottomTabs.js` — 底部 Tab 激活态 + 各 badge + `setActiveBottomTab`。
- **新建** `composables/useRunEventRouter.js` — WS `handleSocketMessage` 分发器（注入全部依赖）。
- **新建** `composables/useTaskForm.js` — 任务输入态 + 提交链路 + 上传 + output-contract preview + slash 胶水。
- **修改** `App.vue` — 每刀删除对应内联块，改 destructure 接线。
- **新建** `tests/` 下对应 6 个 `*.spec.js`（vitest）。
- **追加** `docs/vspider_architecture_backlog.md` — 每刀一行 Slice 记录。

## 排序原则

叶子工具 → 自包含 API → 被路由/表单消费的状态 owner → 路由 → 大表单。保证每刀后 App.vue 仍可独立编译、行为零变更。

---

## Slice D-UI-17 — useClipboard（叶子工具，最低风险）

- **文件：** 新建 `vspider-ui/src/composables/useClipboard.js`；改 `App.vue`；新建 `vspider-ui/tests/useClipboard.spec.js`。
- **抽出：** `App.vue` 582–600 的 `_writeToClipboard`。
- **导出：**
```js
export async function writeToClipboard(text) { /* navigator.clipboard → textarea+execCommand 回退 → 失败 ElMessage.error；返回 boolean */ }
```
- **接线：** `App.vue` 顶部 `import { writeToClipboard } from './composables/useClipboard.js'`；删除内联 `_writeToClipboard`；现有两处注入回调 `writeToClipboard: (text) => _writeToClipboard(text)`（538/563 行附近）改为 `writeToClipboard: (text) => writeToClipboard(text)`（或直接传引用）。`ElMessage` 在 composable 内 import。
- **测试：** ① `navigator.clipboard.writeText` 命中分支；② 无 clipboard → `document.execCommand('copy')` 回退分支（jsdom stub）；③ 抛错 → 返回 false 且调用 ElMessage.error；④ 空串 → 直接返回 false 不触网。
- **验证：** `cd vspider-ui && npx vitest run`（全绿，新增 4 例）→ `npm run build`（绿）→ ReadLints clean。
- **提交：** `D-UI-17 useClipboard 抽离`（能力名 clipboard_extraction / 影响层 App.vue→composables / 无新 contract 字段）。

## Slice D-UI-18 — useAuthProfiles

- **文件：** 新建 `composables/useAuthProfiles.js`；改 `App.vue`；新建 `tests/useAuthProfiles.spec.js`。
- **抽出：** refs `authDialogOpen`(95) `authProfileOptions`(96) `selectedAuthProfiles`(78) `captchaSolverEnabled`(76) `captchaSolverProvider`(77)；函数 `loadAuthProfiles`(463) `loadCaptchaSolverStatus`(476) `useAuthProfile`(489)。
- **导出：** `useAuthProfiles({ appendLog })` → `{ authDialogOpen, authProfileOptions, selectedAuthProfiles, captchaSolverEnabled, captchaSolverProvider, loadAuthProfiles, loadCaptchaSolverStatus, useAuthProfile }`。`apiFetch` 在 composable 内 import。
- **接线：** App.vue destructure；`onMounted` 的 `loadAuthProfiles()` / `loadCaptchaSolverStatus()` 调用点不变；submitTask 读 `selectedAuthProfiles` 来自 destructure；slash `registerBuiltinCommands` 注入 `authDialogOpen` 不变。
- **测试：** ① loadAuthProfiles 成功写 options；② 非 success/抛错 → appendLog WARN、options 不变；③ loadCaptchaSolverStatus 成功写 enabled/provider；④ 非 success 静默不写；⑤ useAuthProfile 去重（重复 name 不二次入列）。stub `apiFetch`（mock 模块）。
- **验证 / 提交：** 同 D-UI-17 节奏；commit `D-UI-18 useAuthProfiles 抽离`。

## Slice D-UI-19 — useFinalAnswer（路由的上游依赖，须先于 D-UI-21）

- **文件：** 新建 `composables/useFinalAnswer.js`；改 `App.vue`；新建 `tests/useFinalAnswer.spec.js`。
- **抽出：** refs `taskResult`(224) `finalAnswerStatus`(225) `finalAnswerText`(226) `finalAnswerDomain`(227) `hasNewFinalAnswer`(228) `finalAnswerExpanded`(260) `finalAnswerCopyState`(261)、`finalAnswerCopyTimer`(262)；computed `finalAnswerHtml`(258)；`watch(taskResult,…)`(245–256)；done 分支答案解析（277–348 内 final-answer 段，318–347）抽成 `applyDoneAnswer(payload, { text, artifactsGrewSinceSubmit })`；onUnmounted 的 copy timer 清理(913–916)抽成 `cleanup()`。
- **导出：** `useFinalAnswer({ renderMarkdown, activeBottomTab, setActiveBottomTab })` → 上述 ref/computed + `{ applyDoneAnswer, resetForNewRun, cleanup }`。`resetForNewRun()` = submitTask 772–779 的 final-answer 复位块。
- **接线：** App.vue destructure；watch 内对 `activeBottomTab`/自动切 Tab 的副作用通过注入的 `setActiveBottomTab` 走（D-UI-20 完成后注入其函数；若本刀先做，先注入 App.vue 现有 `activeBottomTab` ref + 临时 setter，D-UI-20 再替换）。**建议把 D-UI-20 提到本刀之前**以避免二次返工（见下方“排序微调”）。
- **测试：** ① applyDoneAnswer 显式 answer_type='file'；② 无显式 type + artifactsGrew→'file'，否则'text'；③ 已有 text 答案且本次无显式 → 早退不覆盖（332–334 逻辑）；④ answer_domain 白名单过滤（仅 text 且 ∈weather/stock/recipe/flight 才保留）；⑤ watch：text→hasNewFinalAnswer + 切 final；file→切 artifacts；同 type 重复→不动作；⑥ cleanup 清 timer。
- **验证 / 提交：** commit `D-UI-19 useFinalAnswer 抽离`（新增 contract：无）。

## Slice D-UI-20 — useBottomTabs

- **文件：** 新建 `composables/useBottomTabs.js`；改 `App.vue`；新建 `tests/useBottomTabs.spec.js`。
- **抽出：** refs `activeBottomTab`(129) `runsSubView`(130) `hasNewRuns`(149) `hasNewPhase`(175) `hasNewCapability`(176) `hasNewFinalAnswer`(注意与 D-UI-19 的归属：badge 留 useBottomTabs，final answer 内容态留 useFinalAnswer，二者通过注入互通) `timelineAutoScroll`(216) `runHistoryRefreshToken`(148)；常量 `TAB_ORDER`(625)；函数 `setActiveBottomTab`(630)。`hasNewArtifacts` 来自 useScreenshotArtifacts（注入，不搬）。
- **导出：** `useBottomTabs({ timelinePanelRef, hasNewArtifacts })` → 上述 + `setActiveBottomTab`。
- **接线：** App.vue destructure；useTimelineReplay 的 `setActiveBottomTab: (name)=>setActiveBottomTab(name)` wrapper(199) 不变；keyboardActions 的 `selectTab`(668) 不变。
- **决策：** `hasNewFinalAnswer` 只能属一处——**让 useFinalAnswer 持有 `hasNewFinalAnswer`，useBottomTabs 注入它**（`setActiveBottomTab` 里 `name==='final'` 时 `hasNewFinalAnswer.value=false`）。故 **D-UI-19 须在 D-UI-20 之前**完成，把 `hasNewFinalAnswer` 注入本刀。
- **测试：** ① 非法 name 早退；② 各 name 清对应 badge（artifacts→hasNewArtifacts、runs→hasNewRuns、final→hasNewFinalAnswer、timeline→hasNewPhase+scroll、capability→hasNewCapability）；③ timeline 且 autoScroll → 调 timelinePanelRef.scrollToBottom（stub）。
- **验证 / 提交：** commit `D-UI-20 useBottomTabs 抽离`。

## Slice D-UI-21 — useRunEventRouter（WS 路由，最高价值）

- **文件：** 新建 `composables/useRunEventRouter.js`；改 `App.vue`；新建 `tests/useRunEventRouter.spec.js`。
- **抽出：** `handleSocketMessage`(277–431)。`handleCapabilityMoreAction`(919) 可顺带留在 App.vue（与路由无关）或独立，本刀不强制。
- **导出：** `useRunEventRouter(deps)` → `{ handleSocketMessage }`。`deps` 注入（全部已由前序刀/既有 composable 提供）：`appendLog`、`pushScreenshotFrame`/`fetchArtifacts`/`hasNewArtifacts`/`currentImageBase64`/`artifactsGrewSinceSubmit`（useScreenshotArtifacts）、`isRunning`（useTaskForm，或本刀前临时由 App.vue 注入）、`fetchBrowserRuntimeStatus`（useBrowserRuntimeStatus）、`runHistoryRefreshToken`/`hasNewRuns`/`hasNewPhase`/`hasNewCapability`/`activeBottomTab`/`timelineAutoScroll`（useBottomTabs）、`failedRunsPaneRef`/`timelinePanelRef`、`phaseEvents`/`PHASE_LIMIT`/`replayMode`（useTimelineReplay + App）、HITL 系列（useHitlForm：`isHumanInterventionRequired`/`humanInterventionReason`/`hitlScreenshot`/`hitlFormFields`/`hitlFormReason`/`hitlFormScreenshot`/`hitlFormLoading`/`hitlFormVisible`）、`applyDoneAnswer`（useFinalAnswer）。
- **接线：** App.vue 删 `handleSocketMessage` 内联；`useWebSocket({ onMessage: handleSocketMessage, … })`(437) 改用 destructure 出的 `handleSocketMessage`。done 分支的 final-answer 解析改调 `applyDoneAnswer`。
- **测试（stub 全部注入）：** ① `type:'log'`→appendLog 带 level；② `type:'image'`→pushScreenshotFrame；③ `type:'done'`→isRunning=false、刷新 runtime/runs badge、调 applyDoneAnswer；done 且 success=false→failedRunsPane.fetchFailedRuns；④ `type:'phase'`→appendLog + push phaseEvents + PHASE_LIMIT 截断 + capability/timeline badge；replayMode 时丢弃；⑤ `type:'status'` 四子态（new_artifact/human_intervention/hitl_form/human_resumed）各写对应 ref；⑥ 坏 JSON→catch 写 WARN。
- **验证 / 提交：** commit `D-UI-21 useRunEventRouter 抽离`（影响层 App.vue→composables；无新 contract）。

## Slice D-UI-22 — useTaskForm（提交链路 + 表单态）

- **文件：** 新建 `composables/useTaskForm.js`；改 `App.vue`；新建 `tests/useTaskForm.spec.js`。
- **抽出：** 输入 refs `url`(64) `urlFieldExpanded`(65) `prompt`(66) `extraUrls`(70) `proxyServer/Username/Password`(71–73) `batchMaxRuns`(74) `resumeEnabled`(75) `selectedFile`(79) `attachmentIntent`(81) `isRunning`(82) `settingsDrawerOpen`(132) `settingsActivePanels`(133)；output-contract `outputContractPreview`(67)/`Loading`(68)/`Timer`(69) + `refreshOutputContractPreview`(692) + `watch(prompt)`(710) + onUnmounted timer 清理(907–910)；`handleUploadChange`(444)/`handleUploadRemove`(458)；slash 胶水 `onPromptInput`(718)/`onPromptKeydown`(722)/`handleCmdSelect`(727)/`trySlashBeforeSubmit`(734)；`submitTask`(746)/`forceStop`(849)。
- **导出：** `useTaskForm(deps)` → 上述 ref/函数。`deps` 注入：`appendLog`、`clearTerminalLogs`、screenshot（`clearScreenshotStream`/`markArtifactsBaseline`）、phase/timeline 复位（`phaseEvents`/`hasNewPhase`/`hasNewCapability`/`timelineAutoScroll`/`replayMode`/`replaySourceName`）、final answer（`resetForNewRun`）、model settings（`selectedModel`…`semanticApiKey`/`selectedModelType`）、`selectedAuthProfiles`（useAuthProfiles）、`fetchBrowserRuntimeStatus`、slash（`updateCmdSuggestions`/`tryExecuteCmd`/`dismissCmdPalette`/`cmdPaletteVisible`/`cmdPaletteRef`/`promptInputRef`）。`apiFetch`/`ElMessage`/useTaskSubmit & useAttachmentIntent 助手在 composable 内 import。
- **接线：** App.vue destructure；keyboardActions `submitIfIdle`(664)/`focusPrompt` 仍调 destructure 出的 `submitTask`/`isRunning`；`registerBuiltinCommands`(869) 注入 `settingsDrawerOpen`/`settingsActivePanels`/`forceStop`/`targetUrl:url`/各 model ref 改为 destructure 来源；onMounted/onUnmounted 调用点保留（timer 清理移入 composable 暴露的 `cleanup()`，App onUnmounted 调）。
- **测试（stub apiFetch/useTaskSubmit 助手）：** ① validateTaskInput 失败→warning 早退；② upload change 写 selectedFile + 多文件裁剪；upload remove 复位；③ refreshOutputContractPreview：空 prompt→null 不触网；有 prompt→写 formatted；抛错→null；④ watch(prompt) 450ms debounce 触发刷新（fake timers）；⑤ submitTask：组 formData（target_url/goal/urls/constraints/model/auth/file 各字段按条件 append）+ 成功 toast；失败→isRunning=false + error；⑥ forceStop 成功/失败；⑦ trySlashBeforeSubmit：`/` 开头且命中→清空 prompt 返 true。
- **验证 / 提交：** commit `D-UI-22 useTaskForm 抽离`（最大刀，注意 CRLF 用一次性 `_patch_*.py` 按字节改）。

## Slice D-UI-23 — 死代码清理

- **方法：** 沿 D-UI-14/16——临时脚本解析顶层声明 + 多行/单行 destructure + 命名 import 本地名，对去注释 script+template 合并全文整词计数，count==1 判死；即用即删。
- **预期：** 清掉前 6 刀过度 destructure / 失效 import 残留。
- **验证：** 删后重扫 0 死候选 → vitest 全绿 → npm run build 绿（index.js 体积可能微降，tree-shake）→ ReadLints clean。
- **提交：** `D-UI-23 死代码清理`。

## Slice D-UI-24 — push 到远端

- 核对 `git log origin/feat/vspider-next..HEAD` 列出 D-UI-17~23，`git ls-remote` 比对，快进 push 到 `feat/vspider-next`（非 main）。
- **不碰** 工作区其它 agent 的未提交改动（extraction_engine/runtime.py 等）。

---

## 排序微调（执行务必遵守）

实际执行顺序：**17(clipboard) → 18(auth) → 19(finalAnswer) → 20(bottomTabs) → 21(router) → 22(taskForm) → 23(deadcode) → 24(push)**。
理由：`hasNewFinalAnswer` 归 useFinalAnswer、被 useBottomTabs 注入，故 19 先于 20；router(21) 依赖 19/20 的 setter，taskForm(22) 依赖 19 的 resetForNewRun + 18 的 selectedAuthProfiles。

## 每刀统一节奏（executing-plans）

1. 写失败/新测（stub-frame）→ 2. 跑确认红 → 3. 新建 composable + App.vue destructure 接线（CRLF 用字节级 `_patch_*.py`）→ 4. `cd vspider-ui && npx vitest run` 全绿 → 5. `npm run build` 绿 → 6. ReadLints clean → 7. 追加 backlog 一行 → 8. 单刀 commit（不 push，push 集中 D-UI-24）。

## 自检（规格覆盖）

- 残留区块表 9 项 → 全部映射到 17–22（键盘 wiring 标记可选 D-UI-25，handleCapabilityMoreAction 折入/保留）。
- 类型一致性：`setActiveBottomTab` / `applyDoneAnswer` / `resetForNewRun` / `writeToClipboard` 命名跨刀一致。
- 无占位符：每刀给出确切文件、行号、导出签名、测试清单、验证命令。
- 风险：D-UI-19/20 的 `hasNewFinalAnswer` 归属已在排序中锁定；D-UI-21/22 注入面大，靠同名 destructure 保模板/handler 零改动 + stub 测试守回归。
