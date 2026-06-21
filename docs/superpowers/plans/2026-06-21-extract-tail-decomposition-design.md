# R2 extract 尾段（控制流状态机）解构设计

> **面向 AI 工作者：** 这是 `docs/superpowers/plans/2026-06-18-main-py-decomposition.md` 的 R2 续刀**设计稿**（只读分析产物，本文件不含代码改动）。R2-1 / R2-2 已落地（见 backlog）；本稿专攻 R2 剩下的**控制流尾段**——即主循环 `action=="extract"` 分支里 `_choose_best_extraction_candidate` 之后那段带 `break` / `_task_completed` / `event_stream.done` 的状态机。实施时按 `executing-plans` 逐 slice TDD + `validate_y` 收口。

**结论先行：** 尾段**不建议整体外搬**（策略 B，需 `ExtractOutcome` 返回指令协议，风险高）。建议走**策略 A·拆纯子块**——把尾段里**无控制流**的反馈/探测/引导子块逐个搬进 `ExtractRuntime`，把不可约的 `break`/`_task_completed` 骨架（约 40 行）**留在主循环**。策略 B 的协议草案附在 §6 备查，不在本轮实施。

---

## 1. 现状证据（实测，2026-06-21，HEAD=31f77f8）

| 度量 | 值 | 来源 |
|---|---|---|
| `main.py` 总行数 | 9566 | R2-2 收口实测（<9700 基线） |
| 外层 step 循环 | `for step in range(1, _effective_max_steps + 1):` @2896 | grep |
| 内层动作循环 | `for _action_idx, decision in enumerate(decisions):` @5830 | grep |
| `_task_completed` 复位 | `_task_completed = False` @5814（每批次进内层循环前） | Read |
| `_task_completed` 批内检查 | @5832（`if _task_completed: break` 跳过本批剩余动作） | Read |
| `_task_completed` 终局检查 | @9154（`if _task_completed: break` 退出外层 step 循环 → run 结束） | grep |
| 显式 extract 尾段 | ~6408（`_choose_best_extraction_candidate`）→ 6958（分支末 `break`） | Read |
| auto extract 尾段 | ~5478 → ~6108（对称，结构同形） | grep |

**两级 break 协议（关键认知）：** 尾段里所有 `break` 都退**内层循环 @5830**；run 是否结束**只取决于 break 前是否置位 `_task_completed`**（由 @9154 解释）。`_run_succeeded` 随 `_task_completed` 一起决定 run 成败。这是一个干净、可形式化的两级信号。

**S1a 红利：** `_xs.*`（19 个 run 级计数器）早已收进共享 `ExtractState` 实例（S1a 完成）。因此尾段对 `_xs.*` 的写**天然跨方法可见**——搬进持同一 `_xs` 的 `ExtractRuntime` 方法零损耗。**真正跨边界的只剩 ~15 个普通 caller 局部 + 2 个控制信号。**

---

## 2. 控制流出口枚举（显式 extract 尾段）

| 行号 | 触发条件 | 置位 | 控制 | 语义 |
|---|---|---|---|---|
| 6519 | 净新增行 = 0（去重榨干） | 仅反馈/`_xs.*` | `break` | break_batch：本批止，下一步重截图，**run 继续** |
| 6636 | answer 模式抽取完成 | `_task_completed=_run_succeeded=True` + `event_stream.done` | `break` | task_completed：run 成功结束 |
| 6790 | Hard Kill 行数达标 | `_task_completed=_run_succeeded=True` | `break` | task_completed：成功 |
| 6805 | Hard Kill 页数达标 | `_task_completed=_run_succeeded=True` | `break` | task_completed：成功 |
| 6826 | DOM_TABLE 自动翻页成功 | `_xs.extract_count=0` + 反馈 | `break` | break_batch：续抽下一页 |
| 6917 | 连抽≥3 但目标未达 | `_xs.extract_count=0` + 反馈 | `break` | break_batch：强制改翻页 |
| 6955 | 连抽守卫强制结束 | `_task_completed=True`；`_run_succeeded` 经 PROGRESS SAFEGUARD 可为 `False` + `event_stream.done` | `break` | task_completed：成败由进度定 |
| 6958 | 抽取后默认收尾 | 无 | `break` | break_batch：常规批末 |

**归一：** 出口只有 **3 类**——`continue`（不 break，继续批内）、`break_batch`（break，run 继续）、`task_completed`（break + 置 `_task_completed`/`_run_succeeded`，run 结束）。auto extract 尾段（5478/5546/5680/5880…）同构，可复用同一套出口语义。

---

## 3. 上下文句柄清单

### 3.1 尾段**写**的 caller 局部（跨边界，必须回传或共享）

- 数据/计量：`extracted`、`_new_rows`、`_dup_rows`、`_rejected_rows`、`_progress_new_rows`、`_progress_total_rows`、`saved_path`
- 文本/键：`_source_text_for_validation`、`_log_extract_text_source`、`_current_extract_page_key`、`_answer_text`
- 守卫计数：`_duplicate_zero_extract_streak`、`_dedup_tripped_last_step`
- 控制信号：`_task_completed`、`_run_succeeded`
- 对象副作用：`decision["extracted_data"]`、`decision["snapshot_path"]`
- 共享态（已 OK）：所有 `_xs.*`（经 `ExtractState`，无需回传）

### 3.2 尾段**读**的句柄（构造注入面，~30）

- 运行上下文：`goal`、`vlm`、`event_stream`、`browser`、`logger`、`step`、`action`、`decision`、`_current_url`、`_data_shape`、`_candidates`
- 输出契约：`_goal_output_mode`、`_goal_output_contract`、`_run_ts`、`_vlm_output`、`_snapshot_goal`、`_requested_output_fields`
- 纯函数：`_parse_goal_target_count`、`_parse_goal_target_pages`、`_should_force_first_flip_after_successful_extract`、`_goal_needs_pagination_probe`、`_should_schedule_next_page_after_extract`、`_extraction_targets_reached`、`_format_extracted_rows_as_answer`、`_clean_user_visible_done_message`、`_record_run_answer`、`_broadcast_done_safe`、`_broadcast_log_safe`、`save_run_dataset`、`_goal_is_tooltip_extract`
- 已在 `ExtractRuntime` 的方法：`enrich_rows_with_dom_links`、`record_extract_progress`、`try_dom_api_fast_path`、`save_extraction_snapshot`、`visible_table_signature`、`probe_scroll_drain_state`、`nudge_scroll_after_duplicate_extract`、`auto_advance_table_page_via_dom`、`choose/commit/sanitize_extraction_candidate`

> 读面达 ~30 是「整搬」高风险的根因：要么塞进一个 ~30 字段 context dataclass，要么扩 `ExtractDeps`。这也是为何推荐**先拆纯子块**缩小读面，再谈整搬。

---

## 4. 策略 A·拆纯子块（推荐）

把尾段里**无 `break`/无 `_task_completed`** 的连续子块逐个搬进 `ExtractRuntime`，控制流骨架原地不动。每刀 ≤ 一个子块，TDD + validate_y，沿用 R2-1/R2-2 已验证的"参数化 per-call 差异 + 原地 mutate caller 列表 / 回传少量局部"范式。

### Slice R2-3a：零新增反馈块 → `handle_zero_new_rows_feedback(...)`
- 范围：6432–6518（`if _new_rows == 0:` 体内、**不含** 6519 的 `break`）。
- 出入：写 `_xs.first_flip_pending/block_next_page_until_drained/block_next_page_reason`（共享）；读 `goal/vlm/_data_shape/_current_url`；调 `probe_scroll_drain_state`/`nudge_scroll_after_duplicate_extract`/`expected_rows_from_data_shape`（皆已在 runtime）；**唯一跨界 caller 局部** `_duplicate_zero_extract_streak` → 入参传入、返回新值。`_dedup_tripped_last_step=True` 由主循环在调用前/后置位（留主循环）。
- 主循环：`_duplicate_zero_extract_streak = await _extract_rt.handle_zero_new_rows_feedback(streak=_duplicate_zero_extract_streak, data_shape=_data_shape, ...)` 然后保留 `break`。
- 预计净减 ~70 行。

### Slice R2-3b：翻页探测/武装块 → `arm_pagination_after_extract(...)`
- 范围：6637–6773（首翻硬约束 + pagination probe + re-arm，**不含** Hard Kill）。
- 出入：纯写 `_xs.*`（force_next_page_pending/first_flip_pending/pagination_probed/pagination_kind/pagination_hint_msg/page_is_infinite_scroll/block_next_page_*）；读 `goal/_new_rows/_log_extract_text_source/_data_shape`；调 `browser.probe_pagination`/`_should_schedule_next_page_after_extract`/`_broadcast_log_safe`。**无控制流、无跨界局部**（`_new_rows` 等只读）。最干净的一刀。
- 预计净减 ~110 行。

### Slice R2-3c：批后翻页引导块 → `inject_post_extract_pagination_guidance(...)`
- 范围：6830–6898（智能翻页/结束 `vlm.inject_error_feedback` 分支树）+ 可选 6900–6916 连抽守卫的**反馈部分**（不含 break/done）。
- 出入：纯读 `_xs.*` + `browser.find_pagination_links`，纯调 `vlm.inject_error_feedback`。无控制流、无跨界局部。
- 预计净减 ~70 行。

### 不动（不可约骨架，留主循环）
6408–6431（choose/commit）、6519/6636/6790/6805/6826/6917/6955/6958 的 `break`/`_task_completed`/`event_stream.done` 决策点、6534–6600 的 persist（可作 R2-3d 评估，但它写 `saved_path/_progress_*/decision` 多个局部，回传面较大，**列为后续可选**）。

**策略 A 终态：** extract 分支瘦身 ~250–350 行，骨架只剩"choose → commit → 三类出口 break"约 40–60 行，可读性大增，且**全程零控制流外搬、零 `ExtractOutcome`**，风险等同 R2-1/R2-2（低）。

---

## 5. 策略 A slice 依赖与验证

```
R2-3b（最干净，纯 _xs 写）
  ↓
R2-3a（回传 streak 一个局部）
  ↓
R2-3c（纯 vlm 反馈）
  ↓
[可选] R2-3d persist 块（回传面大，单独评估）
```

- 每刀：先 TDD 写失败测试（stub `vlm`/`browser`/已 mock 的 runtime 方法，断"哪些 `_xs.*` 被置位 / 哪些反馈被注入 / streak 回传值"），红→绿；再 `python scripts/validate_y.py R2-3x --target-test tests/test_extract_runtime.py --skip-build`（UI build 归并发 agent，pure-Python 切片跳过）。
- 收口：全量 pytest 0 新增失败（用 `git stash` 仅本切片文件证实 UI 失败为 HEAD 既有）；main.py < 9700；三文件 CLEAN_CRLF；backlog 记一行。

---

## 6. 策略 B·整搬协议草案（**备查，不在本轮实施**）

若将来要把整个 extract 分支搬进 `ExtractRuntime.run_extract_tail(ctx) -> ExtractOutcome`：

```python
@dataclass
class ExtractContext:
    decision: dict
    step: int
    candidates: list
    data_shape: dict
    current_url: str
    current_extract_page_key: str
    duplicate_zero_extract_streak: int
    # + 输出契约/纯函数句柄经 self.deps 注入，不进 ctx

@dataclass
class ExtractOutcome:
    control: Literal["continue", "break_batch", "task_completed"]
    run_succeeded: bool = False           # 仅 control=="task_completed" 有意义
    extracted: list = field(default_factory=list)
    new_rows: int = 0
    dup_rows: int = 0
    rejected_rows: int = 0
    source_text_for_validation: str = ""
    log_extract_text_source: str = ""
    current_extract_page_key: str = ""
    duplicate_zero_extract_streak: int = 0
    dedup_tripped_last_step: bool = False
    saved_path: str = ""
    answer_text: str = ""
    decision_patch: dict = field(default_factory=dict)  # 回写 decision[...]
```

主循环解释器（替换 6408–6958 整段）：
```python
_outcome = await _extract_rt.run_extract_tail(_ctx)
extracted = _outcome.extracted
_duplicate_zero_extract_streak = _outcome.duplicate_zero_extract_streak
_dedup_tripped_last_step = _outcome.dedup_tripped_last_step
decision.update(_outcome.decision_patch)
# ... 其余局部回写 ...
if _outcome.control == "task_completed":
    _task_completed = True
    _run_succeeded = _outcome.run_succeeded
    break
elif _outcome.control == "break_batch":
    break
# control == "continue" → 落到分支末 break（当前语义本就批末 break）
```

**为何缓做：** ①回传面 15 局部，逐一对齐易漏；②抽取状态机的翻页/去重/finish 分支在 Windows 无真站难以 stub 全覆盖（验证不充分）；③`event_stream.done` 的 step/metadata 与主循环耦合。**先做策略 A 把读面/行数砍下来，B 的协议字段也会随之收窄，届时再评估。**

---

## 7. 风险

1. **共享态漏改**：`_xs.*` 经 `ExtractState` 已共享，搬子块时只要持同一 `_xs` 即安全；新发现的跨界 caller 局部一律"入参 + 回传"，禁止隐式闭包捕获。
2. **CRLF 字节级 patch**：跨空行大块搬移写 `_patch_r2_3x.py` 按字节读改写回，patch 完即删（工程规范强制）。
3. **并发 UI agent**：本系列纯 `extraction_engine/`+`main.py`，与 App.vue 零交集；提交只 `git add` 本切片文件，**禁用 `-A`**（避免 R2-1 卷入事故重演）。
4. **行为漂移**：每刀纯平移，先录基线断言再搬；auto 与 explicit 两路若共用新方法，须分别用各自 per-call 参数验证两路不串味。

## 8. 自检

- [x] 控制流出口全枚举（8 出口 → 3 类语义，附行号）。
- [x] 跨界数据面盘清（15 caller 局部 + `_xs` 共享 + ~30 读句柄）。
- [x] 给出低风险落地路径（策略 A 三刀，依赖序明确，验证命令具体）。
- [x] 高风险整搬留协议草案 + 缓做理由，不破坏"加字段不删改"契约。
