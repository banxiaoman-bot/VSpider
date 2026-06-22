# dismiss_consent —— Cookie/同意墙确定性关闭能力设计

> 面向 AI 工作者：本文是新能力 `dismiss_consent` 的**设计稿**（brainstorming 产物，本文件不含运行代码）。已与用户确认：**触发模型 = c（显式动作 + 自动守卫），分两刀落，先做 (a) 显式动作**；**核心机制 = 方案1 分层命中（known-CMP 选择器 + 多语肯定文本）**。实施按 `writing-plans` 出计划 → TDD 逐步实现 → `validate_y` 收口。

## 1. 背景与目标

### 痛点（实测证据）
- 当前对 cookie 同意墙/广告/登录遮挡**没有确定性能力**，唯一处理是 VLM 视觉提示 `prompt_skills.py:255`「extract 前若有广告、cookie 横幅、登录弹窗等遮挡，先 Escape/click 关闭或 remove_element」+ 通用 `close_overlay` 修复动作（`capability_router.py:96`，仅在 click_intercepted 时触发）。
- 这直接违反使命四字铁律 **#1 准确**：「能用 DOM/AX/Playwright/JS 确定性路径，绝不依赖 VLM 视觉猜测」。

### 目标
新增确定性能力 `dismiss_consent`：用 **DOM/JS 确定性命中**主流 CMP（Consent Management Platform）的「接受全部」按钮并点击，**校验弹层消失**作为 evidence，解锁后续一切交互/抽取。对标 `next_page` 的分层 locator 范式（准确、有界、可 stub 回归）。

### 非目标（本能力不做）
- 不做「拒绝/最小化授权」语义（爬取要的是解锁内容，默认接受全部）。
- 不做任意营销弹窗/订阅弹窗的泛化关闭（YAGNI；那是 `remove_element` / 未来泛遮罩启发式的范畴）。
- 不做验证码/登录墙（走既有 `auth_harvester` / `human_guard`）。

## 2. 范围：两刀切分

| 刀 | slice | 内容 | 触碰层 | 风险 |
| --- | --- | --- | --- | --- |
| **第一刀（本设计主体）** | DC-1 | `dismiss_consent` 作为**显式动作**：VLM/路由决定调用，注册 + handler + 路由词 + prompt + stub 测试 + backlog | vlm_models / actions / action_registry / capability_router / prompt_skills / tests | 低 |
| 第二刀（本设计仅预告，单独实施） | DC-2 | **自动前置守卫**：在 extract/perception 前置链自动调一次（幂等 no-op 复用 DC-1 handler） | main.py / phases | 中（碰主循环，单独切片 + 单独 brainstorming/plan） |

DC-1 handler 从一开始就设计成**幂等、无副作用、可被程序化调用**，为 DC-2 自动接线零改造复用。

## 3. 触发模型（已定 c-分两刀先 a）

- DC-1：动作进 `VSpiderAction.action` 枚举，VLM 在看到同意墙时输出 `{"action":"dismiss_consent","target_id":0,"type_value":""}`；capability_router 在 goal/页面含 cookie/consent 关键字时也可提示。**无必填参数**。
- DC-2（后续）：`run_agent` 在每次 extract 前/首次 perception 后自动调用一次 handler；命中即关，未命中干净返回，零回合浪费。

## 4. 核心机制（方案1 分层命中）

执行顺序（命中即停，全程有界）：

### L1 · known-CMP 框架选择器（最高精度）
按主流 CMP 维护一张「root 容器 + 接受按钮」选择器表，主文档 + 子 iframe 都探。代表（非穷举，实施时收进常量表）：

| CMP | 接受全部选择器 |
| --- | --- |
| OneTrust | `#onetrust-accept-btn-handler`, `#accept-recommended-btn-handler` |
| Cookiebot | `#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll`, `#CybotCookiebotDialogBodyButtonAccept` |
| TrustArc | `#truste-consent-button`（横幅常在 iframe `.truste_popframe` / `iframe[src*="trustarc"]`） |
| Quantcast Choice | `.qc-cmp2-summary-buttons button[mode="primary"]` |
| Didomi | `#didomi-notice-agree-button` |
| Usercentrics | `[data-testid="uc-accept-all-button"]`, `button#uc-btn-accept-banner`（含 shadow root `#usercentrics-root`） |
| Osano | `.osano-cm-accept-all` |
| CookieYes | `.cky-btn-accept` |
| Complianz | `.cmplz-accept` |
| Cookie Consent(classic) | `.cc-allow`, `.cc-btn` |
| Klaro | `.cm-btn-success`, `.cm-btn-accept-all` |
| Termly | `[data-tid="banner-accept"]` |
| Borlabs | `#BorlabsCookieBoxSaveButton`, `a[data-cookie-accept-all]` |
| Sourcepoint | iframe `iframe[id^="sp_message_iframe"]` → `button[title="Accept"]`, `.sp_choice_type_11` |
| WP GDPR Cookie | `#wt-cli-accept-all-btn`, `.wt-cli-accept-all-btn` |

### L2 · 多语肯定文本（CMP 未知时兜底，作用域受限）
在**疑似同意容器**内（`[role=dialog]`、`[class*=cookie/consent/cmp/gdpr/privacy]`、视口底/顶 fixed 高 z-index 遮罩、known-CMP root）做**整词肯定文本**匹配：
- 肯定词（命中即点）：`Accept all` / `Accept All Cookies` / `I Accept` / `I Agree` / `Agree` / `Allow all` / `Got it` / `OK` / `Accept` · `接受全部` / `全部接受` / `同意` / `同意全部` / `允许全部` / `我知道了` / `同意并继续` · `Alle akzeptieren` / `Akzeptieren` · `Tout accepter` / `Accepter` · `Aceptar todo` · `同意する` / `すべて同意` · `모두 동의`
- **排除词（绝不点）**：`Reject` / `Decline` / `Manage` / `Settings` / `Preferences` / `Customize` / `Only necessary` · `拒绝` / `管理` / `设置` / `仅必要` / `自定义`

作用域受限 + 整词匹配 + 排除词，防止误点页面里无关的 "OK"/"同意"。

### iframe 兜底
TrustArc/Quantcast/Sourcepoint 等常把同意 UI 放 iframe。复用 `next_page` 已验证的「主文档探不到→遍历 child frames 再探」范式（`page.frames`，跳过 detached）。

### 证据 / 校验（成功判定）
点击后短等（domcontentloaded + 0.4s），**重新探测同一 root/遮罩是否仍可见**：
- 不再可见 → 成功，记 `consent_dismissed.v1` evidence（cmp / strategy / selector / frame_url）。
- 仍可见 → 该候选失败，继续下一候选。

### 有界 / 幂等
- 单趟扫描，命中并校验消失即返回；最多 **1 次重试**（点完没消失时换次优候选）。
- **无同意墙 → 干净 no-op 成功返回**（不抛错），DC-2 自动守卫可低成本反复调用。
- 全程无循环（不违反 loop_detector 约束）。

## 5. 接口设计

### 动作
- `action = "dismiss_consent"`，`target_id=0`，`type_value=""`（无必填参数；预留 `type_value` 未来可传 `reject`/`accept` 切换，DC-1 只实现默认 accept）。

### Handler 返回 + evidence
- 返回 `Optional[Page]`（与现有 handler 一致，正常 `None`）。
- `browser.rpa_trail.append(ctx.with_rpa_meta({...}))` 记：`action=dismiss_consent`、`cmp`（命中框架名 / `text` / `none`）、`strategy`（`known_cmp` / `accept_text` / `noop`）、`selector`、`frame_url`、`dismissed`(bool)。

### 新增 contract 字段
- `consent_dismissed.v1`：`{cmp, strategy, selector, frame_url, dismissed, scanned_frames}`（只加不删，向下兼容）。

## 6. 落地映射（六步范式 → 精确文件）

| 步 | 文件 | 改动 |
| --- | --- | --- |
| 0 动作枚举 | `vlm_models.py` (Literal @182-214) | 追加 `"dismiss_consent",  # 同意墙确定性关闭` |
| 1 注册元数据 | `action_registry.py::build_default_action_registry` | `register(ActionTool(name="dismiss_consent", capability=..., aliases=("cookie","consent","accept all","同意","接受全部","我知道了","gdpr"), tags=("overlay","consent","unblock"), evidence=("consent_dismissed.v1",), risk="low"))` |
| 2 handler | `actions/page_ops.py` | `@ActionRegistry.register("dismiss_consent")` class `DismissConsentHandler(ActionHandler)`，`execute(ctx)` 走 L1→L2→iframe→校验，复用 `_click_locator_with_js_fallback` |
| 3 绑定派发 | `main.py` (@961-968) | `action_registry.bind("dismiss_consent", _browser_action_tool)` + `_registry_dispatch_actions` 集合加 `"dismiss_consent"` |
| 4 路由 | `capability_router.py` | 加中英文正则关键字（cookie/consent/同意/接受全部/gdpr…）→ 提示 `dismiss_consent` |
| 5 prompt | `prompt_skills.py` | 加 skill 区块：触发词 + 「遇同意墙优先 `dismiss_consent`（确定性）而非 click_point 视觉点」行为约定；同时把 :255 那条从「Escape/click」升级为「优先 dismiss_consent」 |
| 6 测试+回归 | `tests/test_dismiss_consent.py` + 1 条 agent_case | stub-frame 注入假 CMP DOM，断言点中正确按钮 + 弹层消失判定 + 排除词不误点 + 无墙 no-op |
| 7 文档 | `docs/vspider_architecture_backlog.md` | 记一行 Slice DC-1 |

## 7. 错误处理
- 任一候选点击异常 → 吞掉记 debug，继续下一候选（不让单候选异常炸整个 handler）。
- 全部未命中 / 无同意墙 → **不抛错**，返回 noop 成功（evidence `strategy=noop, dismissed=false`）。
- iframe detached / evaluate 失败 → 跳过该 frame。
- 与 `remove_element` 的边界：dismiss_consent 只点「接受」按钮；铲除残留遮罩仍归 `remove_element`。

## 8. 测试计划（stub-frame，对标 `tests/test_extract_row_and_tree_check.py::_StubLocator`）
1. **known-CMP 命中**：stub page 注入 `#onetrust-accept-btn-handler` → 断言被点 + dismissed。
2. **accept-text 兜底**：无 known 选择器，dialog 内有「接受全部」按钮 → 命中。
3. **排除词不误点**：dialog 内只有「拒绝/管理」→ 不点，返回 noop。
4. **iframe 兜底**：CMP 按钮在 child frame → 遍历 frames 命中。
5. **无墙 no-op**：干净页面 → 不抛错，dismissed=false。
6. **校验消失**：点击后 stub 标记 root 隐藏 → dismissed=true；保持可见 → 该候选判失败。

验证：`python scripts/validate_y.py DC-1 --target-test tests/test_dismiss_consent.py`（定向测试→build→核心 pytest→全量 pytest）。

## 9. 风险
1. **误点非同意按钮**：L2 作用域受限 + 整词 + 排除词三重防护；known-CMP 优先于文本兜底。
2. **CRLF 字节级 patch**：`actions/page_ops.py` 是 CRLF；跨空行大块插入写 `_patch_dc1.py` 按字节改写回，patch 完即删（工程规范强制）。
3. **并发会话冲突**：本切片只碰 vlm_models/actions/action_registry/capability_router/prompt_skills/main.py 绑定段 + 新测试文件；提交只 `git add` 本切片文件，**禁用 `-A`**（避免卷入并发会话改动）。`main.py` 仅在 @961-968 绑定段加 2 行，与 R2/BBR 段无交集。
4. **行为漂移**：DC-1 是纯新增动作，默认不自动触发（DC-2 才接前置链），对既有流程零影响。

## 10. 自检（规格）
- [x] 占位符：无 TODO/待定。
- [x] 一致性：触发模型(c-先a) 与范围(DC-1 显式/DC-2 守卫) 全篇一致；机制(方案1) 与落地映射一致。
- [x] 范围：DC-1 单一实现计划可覆盖；DC-2 明确拆出，不混入。
- [x] 模糊性：accept vs reject 已明确默认 accept + 排除词；成功判定已明确为「弹层消失」。

## 11. DC-2 预告（自动守卫，单独实施）
- 在 `run_agent` extract 前置 / 首屏 perception 后调用 `dismiss_consent` handler 一次（幂等）。
- 需评估调用点（避免每步都跑，建议每个新 URL/首屏一次 + extract 前一次）+ 与 loop/wait guard 协同。
- 单独走 brainstorming → writing-plans。
