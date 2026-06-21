# R2 extract 尾段 · 策略 B 评估 + R2-3d/3e/R2-4 实施计划

> **面向 AI 工作者：** 这是 `2026-06-21-extract-tail-decomposition-design.md` 的续篇。策略 A（R2-3a/3b/3c）已收官（commit d4426b6 / 31f77f8 / 8610da5 / 6905118 / 6c726fe）。本稿**重新测量 R2-3a/3b/3c 之后的残余骨架**，评估策略 B（整搬 + `ExtractOutcome`）是否仍值得做，并给出**两条新的低风险纯块刀（R2-3d/R2-3e）+ 策略 B（R2-4，可选）**的实施计划。本文件不含代码改动。

**评估结论（先行）：** 策略 B 的「面」已被策略 A 大幅削小，但**整搬仍是高风险/低收益**。建议**先做 R2-3d + R2-3e 两刀纯块外搬（策略 A++，低风险）**，把 explicit 分支收敛到只剩 ~50-60 行**纯控制流骨架**；到那一步，**策略 B 再额外搬这 50 行的边际收益不抵 `ExtractOutcome` 协议 + 状态机测试的风险，建议默认停在 A++**，除非未来出现「extract 分支必须可离线整体回放」的硬需求。

---

## 1. 残余骨架实测（R2-3a/3b/3c 之后，HEAD=6c726fe）

explicit extract 分支现 = main.py **6410 → ~6660**（~250 行，策略 A 前 ~550 行）。逐段：

| 段 | 行 | 性质 | 控制流 |
|---|---|---|---|
| choose + commit + DOM_TABLE sig | 6410-6432 | **纯**（读 candidates/url，写 7 局部 + decision） | 无 |
| zero-rows 反馈 | 6434-6443 | 已委托 R2-3a + `break` | break |
| persist（enrich/progress/save/api/snapshot/print/计数） | 6445-6524 | **纯**（写 5 局部 + _xs 计数 + decision） | 无 |
| answer 模式 done | 6525-6560 | done + `break` | task_completed |
| arm_pagination | 6561-6565 | 已委托 R2-3b | 无 |
| hard kill rows / pages | 6566-6597 | done + `break` ×2 | task_completed ×2 |
| table advance | 6599-6618 | `break` | break_batch |
| guidance | 6622-6623 | 已委托 R2-3c | 无 |
| extract guard（+progress safeguard + done） | 6625-6660 | done + `break` | task_completed |
| 分支末 | ~6660 | `break` | break_batch |

**关键观察：** 250 行里仍有 **~102 行纯块**（choose/commit 22 + persist 80）可走策略 A 模式外搬；真正不可约的控制流骨架只剩 **~50-60 行**（5 个 break/task_completed 决策点 + 各自 done/safeguard）。

---

## 2. R2-3d：choose + commit 选取块 → ExtractRuntime（策略 A++，推荐）

- **范围：** 6410-6432。
- **出入：** 读 `_candidates`、`_current_url`、`_current_extract_page_key`；写 7 个 caller 局部（`extracted`/`_new_rows`/`_dup_rows`/`_rejected_rows`/`_source_text_for_validation`/`_log_extract_text_source`/`_current_extract_page_key`）+ `decision["extracted_data"]`。调 `choose_best`/`commit`/`visible_table_signature`（皆 self）+ `hashlib.md5`。**无控制流。**
- **方法：** `async def select_and_commit_extraction(self, *, candidates, current_url, current_extract_page_key) -> ExtractCommit`，返回 dataclass `ExtractCommit(extracted, new_rows, dup_rows, rejected_rows, source_text, log_source, page_key)`。主循环解包 7 值 + `decision["extracted_data"]=...`。
- **预减：** ~20 行。**风险：低**（返回值聚合为 dataclass，无副作用外溢）。

## 3. R2-3e：persist 落盘块 → ExtractRuntime（策略 A++，推荐）

- **范围：** 6445-6524。
- **出入：** 读 `extracted`/`_new_rows`/`_dup_rows`/`_rejected_rows`/`_log_extract_text_source`/`_source_text_for_validation`/`_candidates`/`_data_shape`/`step`（输出契约/run_ts/vlm_output 经 deps）；写 caller 局部 `extracted`（api_fast 可替换）/`_progress_new_rows`/`_progress_total_rows`/`saved_path`/`_duplicate_zero_extract_streak=0` + `decision[snapshot_path/extracted_data]` + `_xs.extract_count/extracted_page_urls/extracted_page_keys`（共享）。调 enrich/record_progress/try_dom_api_fast_path/save_extraction_snapshot（self）+ save_run_dataset + print/logger。**无控制流。**
- **方法：** `async def persist_extracted_batch(self, *, extracted, new_rows, dup_rows, rejected_rows, log_source, source_text, candidates, data_shape, current_url, step) -> ExtractPersist`，返回 `ExtractPersist(extracted, saved_path, snapshot_path, progress_new_rows, progress_total_rows)`；`_duplicate_zero_extract_streak=0` 与 `decision[...]` 回写由主循环按返回值落。`_xs.*` 计数经 self.state 直接写。
- **预减：** ~75 行。**风险：低-中**（返回 5 值 + decision 回写面稍大，但仍无控制流；TDD stub save/snapshot/api）。

## 4. 策略 A++ 终态

做完 R2-3d/3e，explicit 分支收敛为（伪码，~50-60 行）：
```
commit = await rt.select_and_commit_extraction(...)
decision["extracted_data"] = commit.extracted
if commit.new_rows == 0:
    _dedup_tripped_last_step = True
    _dup_zero = await rt.handle_zero_new_rows_feedback(...)   # R2-3a
    break
persist = await rt.persist_extracted_batch(...)               # R2-3e
decision["snapshot_path"] = persist.snapshot_path; _dup_zero = 0
if answer: ...done...; _task_completed=_run_succeeded=True; break
await rt.arm_pagination_after_extract(...)                    # R2-3b
if hard_kill_rows: ...; _task_completed=_run_succeeded=True; break
if hard_kill_pages: ...; _task_completed=_run_succeeded=True; break
if need_more_table_pages and advanced: ...; break
rt.inject_post_extract_pagination_guidance()                 # R2-3c
if extract_count>=3: ...guard...; (break | done+break)
break
```
这是干净、可读的**编排骨架**——每行都是「调已测方法」或「控制流决策」。main.py 预计再减 ~95 行（9291 → ~9196）。

## 5. R2-4：策略 B（ExtractOutcome 整搬，**可选 / 默认缓做**）

若未来需要 extract 分支**整体离线回放 / 单元覆盖控制流**，再走 B：把 §4 骨架整体搬进 `run_extract_tail(ctx) -> ExtractOutcome`。

- **协议**（较设计稿 §6 收窄，因纯块已外移）：
```python
@dataclass
class ExtractOutcome:
    control: Literal["break_batch", "task_completed"]
    run_succeeded: bool = False
    dedup_tripped_last_step: bool = False
    duplicate_zero_extract_streak: int = 0
    decision_patch: dict = field(default_factory=dict)  # extracted_data / snapshot_path
```
- **主循环解释器**：`_outcome = await rt.run_extract_tail(ctx); decision.update(_outcome.decision_patch); _dedup_tripped_last_step=_outcome.dedup_tripped_last_step; _duplicate_zero_extract_streak=_outcome.duplicate_zero_extract_streak; if control=="task_completed": _task_completed=True; _run_succeeded=_outcome.run_succeeded; break;` 否则 `break`（break_batch 与分支末 break 等价）。
- **ctx**：需 `decision/step/candidates/data_shape/current_url/current_extract_page_key`（其余经 deps）。
- **风险（仍高）：** ① `event_stream.done` 的 step/metadata 与主循环耦合需进 deps/ctx；② 控制流状态机（answer/hardkill/table/guard 五出口）Windows 无真站难充分单测，只能 stub 分支；③ 回报面虽收窄到 ~4 字段，但仍需逐一对齐。**收益仅 ~50 行**。

## 6. 实施顺序与验证

```
R2-3d (choose/commit, 低风险)
  ↓
R2-3e (persist, 低-中风险)
  ↓  ← 建议停在此（策略 A++ 终态）
[可选] R2-4 (ExtractOutcome 整搬, 高风险, 仅硬需求时)
```
- 每刀：TDD 先写失败测试（stub frame + mock save/snapshot/api/vlm）→ 红→绿；字节级 `_patch_*.py` 搬移即用即删；`python scripts/validate_y.py R2-3x --target-test tests/test_extract_runtime.py --skip-build`；域回归（extraction+pagination 17 文件）+ 全量 pytest 0 新增失败（`git stash` 仅本切片证）；CLEAN_CRLF；main.py <9700；backlog 记一行。
- 每刀仍须先**逐行对比 auto 路径**（R2-3a/3b/3c 已证两路全面漂移）：choose/commit 与 persist 在 auto 路径（5178/5186 + 5240-5283）大概率同样漂移 → 预期仍单点搬，不强行 dedup。

## 7. 自检
- [x] 残余骨架逐段实测（10 段，标注纯/控制流 + 行号）。
- [x] 给出两条低风险纯块刀（R2-3d/3e）+ 方法签名 + 预减行数。
- [x] 策略 B 重评估：面已收窄但仍高风险低收益，给出缓做判据（硬需求触发）。
- [x] 协议/解释器/ctx/风险齐全；不破坏「加字段不删改」契约。
