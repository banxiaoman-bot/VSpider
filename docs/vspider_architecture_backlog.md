# VSpider Architecture Backlog

This backlog converts open-source learning into implementation slices for
VSpider. It favors small, reversible changes over large rewrites.

## Slice 1: Structured Action Results

Reference: browser-use `ActionResult`.

Status: initial wrapper implemented.

Goal:

- Every action should produce structured evidence the main loop can reason about.

Add:

- `visual_web_agent/action_result.py`

Fields:

- `success: bool`
- `action: str`
- `message: str`
- `error: str`
- `changed_url: bool`
- `changed_dom: bool`
- `clicked_text: str`
- `value_readbacks: list[dict]`
- `extracted_rows: int`
- `output_file: str`
- `metadata: dict`

Migration:

1. Add the dataclass/model and helper constructors.
2. Wrap existing handler return values at the dispatch boundary.
3. Convert high-value handlers first: `click`, `type`, `hover`,
   `hover_and_click`, `form_set`, `next_page`, `extract`.

Risk:

- Low if wrapper accepts legacy `None` returns.

Acceptance:

- A run log can show, for every executed action, whether it succeeded, what
  changed, and what evidence was captured.
- `hover_and_click` records the clicked menu text.
- form actions record field readbacks.
- extraction actions record output file and row count.

## Slice 2: Browser State Snapshot

Reference: browser-use `BrowserStateSummary`, UI-TARS-desktop event stream.

Status: initial snapshot implemented and attached to `observe` events.

Goal:

- One compact object for current page state.

Add:

- `visual_web_agent/browser_state.py`

Fields:

- `url`
- `title`
- `screenshot_path`
- `ax_tree_excerpt`
- `interactive_count`
- `dom_shape`
- `visible_text_excerpt`
- `tabs`
- `last_action_result`

Migration:

1. Build from data already gathered in `main.py`. Done for screenshot/AX/tabs/page summary.
2. Use it in logs and future prompts. First use: event stream `observe.browser_state`.
3. Add a debug command later.

Risk:

- Medium if it duplicates expensive screenshot/AX collection. Reuse existing
  collection results instead of recollecting.

Acceptance:

- One object can be serialized to JSON and contains URL, title, screenshot path,
  DOM shape, AX excerpt, visible text excerpt, and recent action result.
- The main loop can log a compact state summary without recomputing screenshot
  or AX Tree.

## Slice 3: Event Stream JSONL

Reference: UI-TARS-desktop event stream, browser-use history.

Status: initial stream implemented, with `verify` events for semantic macros,
auto-form readbacks, RPA completion, and done-guard failures.

Goal:

- Make every run debuggable as a chronological stream.

Add:

- `visual_web_agent/event_stream.py`

Events:

- `run_start`
- `observe`
- `decide`
- `act`
- `verify`
- `extract`
- `guard`
- `run_end`

Output:

- `logs/event_stream_<timestamp>.jsonl`

Risk:

- Low. It is append-only observability.

Acceptance:

- Each agent run creates one JSONL file.
- The stream contains at least: `run_start`, `observe`, `decide`, `act`,
  `run_end`.
- Guard interventions such as tooltip/dropdown rewrites and done guards are
  visible as `guard` events.
- Verification evidence such as form readbacks and semantic component checks is
  visible as `verify` events.

## Slice 4: Action Tool Registry

Reference: browser-use custom tools, UI-TARS-desktop tool/event boundary.

Status: initial metadata registry implemented, emitted to event stream,
attached to `act` results as `metadata.tool`, and used for first real
`hover_and_click` / `next_page` dispatch.

Goal:

- Make deterministic capabilities discoverable before moving implementations
  out of `main.py`.

Add:

- `visual_web_agent/action_registry.py`

Current tools:

- `auto_form_fill`
- `round_form_challenge`
- `date_pick`
- `cascader_pick`
- `hover_and_click`
- `tooltip_extract`
- `table_extract`
- `list_extract`
- `next_page`
- `xhr_extract`
- `visual_coordinate_fallback` (registered but disabled)

Migration:

1. Register metadata for current deterministic macros. Done.
2. Bind existing handlers without moving implementations. Started.
3. Attach tool attribution to `ActionResult` events. Done for main action
   boundaries and deterministic macro completions.
4. Route low-risk actions through registry dispatch. Done for
   `hover_and_click` and `next_page`.
5. Gradually move macro implementations behind the registry once tests cover
   each tool boundary.

Risk:

- Low while metadata-only. Medium once the registry becomes the execution path.

Acceptance:

- Each run emits a `tool_catalog` event.
- Candidate tools can be selected from the user goal.
- Registered tools include expected evidence fields for future `ActionResult`
  verification.
- `act` events can identify which registered tool/capability handled or
  corresponds to the action.
- Registry-dispatched actions emit a `tool_dispatch` event before execution.

## Slice 5: Extraction Snapshot Replay

Reference: Scrapling snapshot/cache and adaptive selection.

Status: initial compact snapshot capture and replay summary implemented.

Goal:

- Let extractor bugs be reproduced without revisiting external sites.

Add:

- `visual_web_agent/extraction_engine/snapshots.py`
- `workspace/extraction_snapshots/`

Capture:

- URL
- goal
- compact DOM list/table text
- AX extract text
- body text excerpt
- winning extractor
- output rows

Replay:

- summarize saved candidate metadata
- compare row count, unique row count, and required-field coverage
- compare two snapshots by selector fingerprint similarity and field drift
- compare latest snapshot pairs per URL/field group

Risk:

- Medium. Need to keep snapshots compact and avoid storing sensitive pages by
  default. Only save for public regression cases unless explicitly enabled.

Acceptance:

- A failed or successful extraction can save a compact public-safe snapshot.
- A replay command can inspect snapshot evidence without opening a browser.
- Replay reports row count, unique row count, required-field coverage, and
  selected extractor metadata.
- Compare reports fingerprint score, row delta, and added/removed fields.
- `compare-latest` reports recent extraction-surface drift without manually
  selecting files.

## Slice 6: Selector Fingerprints

Reference: Scrapling adaptive selector behavior.

Status: initial rowset/field fingerprint generation, offline scoring, snapshot
compare, and history-guided candidate boosting implemented.

Goal:

- Recover list/table/field extraction when classes or DOM depth shift.

Add:

- `visual_web_agent/extraction_engine/selectors.py`
- `visual_web_agent/extraction_engine/recovery.py`

Fingerprint:

- source family (`DOM_TABLE`, `DOM_LIST`, `AX_TREE`, etc.)
- field order and required field coverage
- normalized value pattern by field
- row width distribution
- compact data-shape signals
- selector-like source hint
- future: tag
- future: role/name
- future: nearby label text
- column header
- normalized text pattern
- class token set
- ancestor signature

Risk:

- Medium. False-positive selector recovery can corrupt extraction. Gate with
  row-level validation and required field checks.

Acceptance:

- For repeated rows, VSpider stores multiple independent signals:
  selector-like source hint, field layout, value patterns, and data shape.
- Offline scoring can compare a baseline fingerprint against a candidate.
- During extraction, current candidates can receive a bounded score boost when
  they match recent snapshots for the same URL/field group.
- Future selector recovery must be rejected when score/required-field coverage is
  too low.
- Regression cases still pass with selector recovery enabled.

## Slice RB1 (M5 鲁棒): extraction selector recovery 严门控重选 (done, P2)

- 能力名: recover_extraction_selectors（抽取塌陷时按强历史 baseline 给出门控恢复提示）。
- 影响层: extraction_engine/recovery.py（与既有 advisory boost 同模块，新增独立函数，未改既有签名/行为）。
- 改动: 仅当「当前抽取弱」(无 accepted 行 / 必填字段覆盖 ≤ max_current_coverage) 且「同 URL/字段组存在强 baseline」(覆盖 ≥ min_baseline_coverage 且有行) 才回 recovered=True，附 source_family / selector_like / expected_fields；否则按 current_extraction_healthy / no_baseline / no_strong_baseline 直接拒。绝不伪造行。兑现本节「Future selector recovery must be rejected when coverage too low」验收项。
- 新增 contract 字段: extraction_selector_recovery.v1（新增 advisory 结构，不改既有契约）。
- Tests: test_extraction_selector_recovery.py 5✓（塌陷恢复 / 弱部分恢复 / 健康不恢复 / 无 baseline / 弱 baseline 拒绝）；test_extraction_recovery.py 旧用例仍绿。

## Slice RB2 (M5 鲁棒): failure-replay recovery-decision 闭环 (done, P2)

- 能力名: replay_recovery_decision（把已捕获失败的 action_trace 喂回 self_healing_policy，离线断言「会真的产出恢复决策」）。
- 影响层: capability_failure_replay.py（接入 replay_capability_failure_fixture 报告 + 批处理聚合 recovery_decision_count）。
- 改动: 纯增量，不改 FIXTURE-AUDIT-1 锁定的 report['passed'] planner-feedback 门；rich bundle(带 action_trace) → has_recovery_decision=True 且 selector_missing 引向 use_similar_selector；minimal bundle(无 trace) → has_recovery_decision=False。
- 新增 contract 字段: capability_failure_recovery_decision_replay.v1（report['recovery_decision'] 子结构）。
- Tests: test_capability_failure_recovery_replay.py 6✓（含 batch 聚合 recovery_decision_count=2）；test_fixture_coverage_audit.py(FIXTURE-AUDIT-1) 回归仍绿。

## Slice RB3 (M5 鲁棒): extraction recovery 接入 live 主链路 (done, P2)

- 能力名: maybe_publish_extraction_recovery_hint（RB1 的 `recover_extraction_selectors` 此前只在测试里调、live 抽取塌陷不触发；本片把它接进 run_agent 抽取主链路，产生生产价值）。
- 影响层: extraction_engine/recovery.py（新增 live-wiring 助手）+ execution_kernel（main.py `_try_pre_extract_fast_path` 塌陷点）。
- 前置: RB1 已完成。
- 改动:
  - recovery.py 新增 `maybe_publish_extraction_recovery_hint(workflow_memory, candidates, ...)`：跑门控恢复，命中 recovered=True 时把 hint 写入 `workflow_memory[RECOVERY_HINT_MEMORY_KEY]`（键 `extraction_recovery_hint`，常量导出，向下兼容）；非 dict / None 内存容错。advisory only，绝不伪造行。
  - main.py 抽取塌陷点 `if not candidates:` 接入该助手（镜像既有 `canvas_grid_notice` 写 workflow_memory 范式），命中写 logger.info 留痕，异常 logger.debug 吞掉。两处 import 块（相对 + 绝对回退）同步加 `maybe_publish_extraction_recovery_hint`。
  - main.py 净增 ~21 行（含 import）；行数 9669 → 9690，仍 < 9700 基线守卫。
- 新增 contract 字段: RECOVERY_HINT_MEMORY_KEY = "extraction_recovery_hint"（workflow_memory 键，沿用 extraction_selector_recovery.v1 结构）。
- Tests: test_extraction_selector_recovery.py +3✓（写 workflow_memory / not-recovered 不写 / None 容错），共 8✓；test_extract_canvas_fallback.py(塌陷点源断言) 回归仍绿。
- 验证: TDD 红→绿；定向 50✓；全量 pytest 36 失败全为既有并发 UI/api 重构（stash 我方改动后同 36 失败，证明零新增）。

## Slice RB4 (M5 鲁棒): type 动作 selector cache 读路径 (done, P2)

- 能力名: type_selector_cache_read（`TypeHandler` 此前只有 E4 写回 `store("type")`、无读路径 → type 缓存是死的，selector 漂移时享受不到 click 已有的跨 run 加速/抹险）。
- 影响层: operations_plane（visual_web_agent/actions/click_and_type.py::TypeHandler）。
- 前置: E4 selector_cache click/type 集成已完成（写回侧）。
- 改动: TypeHandler 镜像 ClickHandler 的 E4 读路径——`lookup("type")` → `validate_cached_target`（可见 + 文本指纹）→ 命中用缓存 locator（`SimpleNamespace(handle=...)` 包壳，下游 `target.handle.*` 零改）跳过 `_resolve_action_target` 并 `record_hit`；stale 则 `invalidate` 降级回 SoM 漏斗；既有写回逻辑不动（命中后自然刷新条目）。新增 `from types import SimpleNamespace`。`VSPIDER_SELECTOR_CACHE=0` 时整条读路径跳过。
- 新增 contract 字段: 无（沿用 action_selector_cache.v1）。
- Tests: test_selector_cache.py 新增 TestTypeWiring 3✓（命中跳 resolve+record_hit / stale invalidate 后漏斗重存 / env 关闭走 resolve）；既有 click_text/validate/som_key 用例仍绿（共 50✓）。
- 验证: TDD 红（命中仍 resolve=1）→绿（resolve=0）；lint 零错。

## Slice 7: State Debug CLI

Reference: browser-use CLI `state`, `click`, `type`, `screenshot`.

Status: initial CLI implemented.

Goal:

- Inspect a page without running the full agent loop.

Possible commands:

```powershell
python -m visual_web_agent.debug_cli state --url "https://example.com"
python -m visual_web_agent.debug_cli screenshot --url "https://example.com"
python -m visual_web_agent.debug_cli clickable --url "https://example.com"
python -m visual_web_agent.debug_cli tools --goal "点击 Next 翻页"
```

Risk:

- Low. Keep it separate from `main.py`.

Acceptance:

- `state` prints URL, title, page data shape, and visible interactive count.
- `clickable` prints indexed visible elements with role/name/text.
- `screenshot` writes a marked screenshot using the same SoM path as the agent.
- `tools` prints registry tools and goal-selected candidates.
- It does not require running a natural-language task.

## Recommended Implementation Order

1. Slice 1: Structured Action Results
2. Slice 3: Event Stream JSONL
3. Slice 2: Browser State Snapshot
4. Slice 4: Action Tool Registry
5. Slice 5: Extraction Snapshot Replay
6. Slice 6: Selector Fingerprints
7. Slice 7: State Debug CLI

Reason:

- Action results and event stream immediately reduce debugging ambiguity.
- Snapshot replay and selector fingerprints become safer once every extractor
  decision has structured evidence.

## Regression Policy

After each slice:

1. Run `python -m py_compile` on touched modules.
2. Run list-only case discovery:

```powershell
python tests\agent_cases\run_agent_cases.py
```

3. Run at least one focused browser case matching the slice:

```powershell
python tests\agent_cases\run_agent_cases.py --run --capability tooltip
python tests\agent_cases\run_agent_cases.py --run --capability form
python tests\agent_cases\run_agent_cases.py --run --category extraction
```

4. Save report path in the development note or PR summary.

## Slice COMP-1: Completion Kernel (evidence-based stop)

Status: initial kernel + main loop wiring implemented.

Goal:

- Stop scroll/click/next_page when manifest/row/answer evidence says the task is complete.

Add:

- `visual_web_agent/completion_kernel.py`
- `tests/test_completion_kernel.py`

Acceptance:

- `evaluate_completion` returns complete for extract row target, answer memory, media manifest.
- `maybe_short_circuit_decision` rewrites scroll/next_page to done when complete.
- main.py emits `COMPLETION_KERNEL_SHORT_CIRCUIT` guard events and plan-gate allow path.

## Slice COMP-2: No-Progress Streak Guard

Status: initial tracker + main loop wiring implemented.

Goal:

- Detect scroll/next_page/wait loops that add zero new rows and mark pagination exhausted.

Add:

- `NoProgressTracker` in `completion_kernel.py`

Acceptance:

- Two consecutive no-progress scroll/page steps set `_pagination_exhausted`.
- `NO_PROGRESS_EXHAUSTED` guard event emitted.
- completion_kernel uses streak for near-target tolerance exit.

## Slice COMP-3: Planner Exit Criteria

Status: implemented.

Goal:

- Parse planner sub-goal `exit_criteria` text into typed checks and feed completion_kernel.

Add:

- `planner_exit_criteria.py` — parse/evaluate row_count, url_contains, extract_done, answer_ready, etc.
- Wire `current_subgoal_criteria(_task_plan)` into `evaluate_completion` / `maybe_short_circuit_decision` in main.py
- `semantic_router.merge_task_plan_into_route` attaches `planner_exit_criteria` snapshot

Acceptance:

- Sub-goal with "提取N条" completes when row count met even without global goal target parse.
- `tests/test_planner_exit_criteria.py` covers parse + kernel integration.

## Slice COMP-4: Completion Evidence UI

Status: implemented.

Goal:

- Surface completion_guard phase events in frontend Timeline panel.

Add:

- `broadcast_phase("completion_guard", ...)` for short-circuit, plan-gate, no-progress guards
- App.vue `latestCompletionGuard` / `completionEvidence` computed + timeline card

Acceptance:

- Live run shows guard type, evidence, reasons, no-progress streak when guards fire.
- npm run build passes.

## Slice NET-4: Network Candidate Index (capture side)

Status: implemented.

Goal:

- Index every page XHR/fetch that returns a row list as a replayable API
  candidate, persisted per run, so later phases prefer API over re-scraping DOM.

Add:

- `network_intelligence.py` — `normalize_endpoint` (mask pagination/volatile
  params), `build_candidate` (auth-header allowlist + body + bounded
  `sample`/`full_rows` + `max_text_len`), `record_candidate` /
  `list_candidates` / `summarize_candidates`, per-run jsonl under `network_root`.
- `api_server` `GET /api/runs/{run_id}/network` for the candidate index + summary.

Acceptance:

- Volatile/pagination query params masked into a stable `endpoint`.
- Auth headers (authorization / x-csrf-token) kept; cookie / user-agent dropped.
- Invalid `run_id` is path-safe (no traversal).
- `tests/test_network_intelligence.py` covers normalize / record / list /
  summarize / build / api endpoint.

## Slice NET-5: Session-aware API Replay

Status: implemented.

Goal:

- Replay a captured candidate endpoint with the live session (method, POST body,
  cookies) and judge success by `http_ok`, returning the full row list instead
  of re-driving the browser.

Add:

- `api_replay.py` — `replay_candidate` (method + POST body forwarding,
  cookie/header injection, `http_ok` judgement, full `rows`).

Acceptance:

- GET and POST candidates replay with forwarded headers/body.
- Non-2xx / non-JSON downgrades cleanly (no crash, `http_ok=False`).
- `tests/test_api_replay.py` covers method / body / http_ok / row return paths.

## Slice NET-6: DOM/API Completeness Fast Path

Status: implemented.

Goal:

- When DOM shows truncated/preview text but captured XHR/JSON is richer, replay API with session cookies instead of stopping at partial DOM extract.

Add:

- `content_completeness_guard.py` — truncation detection, candidate ranking, cookie-aware `execute_api_fast_path`
- `main.py` `_try_dom_api_fast_path` after auto_extract commit
- `capability_router` signal `full_content_preferred` + backend plan entry
- `api_replay.replay_candidate` returns full `rows` list (additive field)

Acceptance:

- DOM truncation markers + richer network candidate triggers API replay.
- Cookies from browser context forwarded on replay.
- `DOM_API_FAST_PATH` guard + `completion_guard` phase event for UI.
- `tests/test_content_completeness_guard.py` covers detect/evaluate/execute.

## Slice NET-7: Runtime Wiring + Behavior Regression

Status: implemented.

Goal:

- Wire `network_intelligence` capture into the browser substrate + main loop so
  the candidate index is populated during real runs, on an independent track
  from the legacy `--xhr` interceptor.

Add:

- `browser_env`: `configure_network_intelligence(run_id)` +
  `_record_network_candidate_safe(...)`; `_handle_xhr_response` gains a
  `network_active` track that runs even when general `--xhr` interception is off.
- `main`: `run_agent` calls `browser.configure_network_intelligence(_run_ts)`
  after wiring interceptors.
- `tests/test_network_intelligence_wiring.py` — behavior-level stub regression
  (no Playwright) on a bare `BrowserEnv`: enable/disable, no-op guards, payload
  forwarding, fingerprint dedup, reconfigure reset, missing-attr fallbacks.
  Complements the source-pin `test_browser_and_main_wiring_source_pins`.

Acceptance:

- `network_active` track records candidates independently of `_intercept_enabled`.
- Disabled run_id / empty rows are no-ops; identical `(url, schema)` deduped
  within a run; reconfigure clears the dedup set.
- `tests/test_network_intelligence_wiring.py` 11 passed; existing NET suite green.

## Slice MM-1: Image Attachment Multimodal Primitives (Phase 1)

Status: implemented (Phase 1 of 2 — primitives only; run_agent wiring deferred).

Goal:

- Make image attachments (`intent=prompt_context`) feedable to the VLM as real
  multimodal images, replacing the caption-only stub. Phase 1 lands the reusable
  primitives; Phase 2 wires them through `run_agent` + callers.

Add:

- `attachment_adapters/base.py` — `AdapterResult.image_b64: list[str]` +
  `image_count` in `to_dict`.
- `attachment_adapters/image.py` — return a base64 `data:` URL (downscale large
  images via Pillow to bound token cost) + keep a caption for text fallback;
  drop the `multimodal_feeding_not_yet_wired` reason.
- `vlm_client.VLMClient._build_user_content(screenshot, text, extra_images)` —
  assemble OpenAI/Qwen-VL content (screenshot -> extra_images -> text, with
  data-URL normalization and text-only graceful degrade); `ask()` gains an
  `extra_images` parameter that flows into the payload.

Acceptance:

- `tests/test_attachment_image_multimodal.py` 12 passed (adapter base64, payload
  ordering/normalization, text-only fallback, ask() signature).
- No production caller changed (Phase 2): `run_agent` does not yet pass
  `extra_images`; existing-run behavior is unchanged.

Next (Phase 2):

- Thread `prompt_context` image attachments through `run_agent` + api_server/CLI
  so uploaded images actually reach `vlm.ask(extra_images=...)`.

## Slice MM-2: Image Attachment Wiring into the Agent Loop (Phase 2)

Status: implemented (Phase 2 of 2 — primitives now reach the live loop).

Goal:

- Thread `prompt_context` image attachments end-to-end so an uploaded image
  actually reaches `vlm.ask(extra_images=...)` during a run, with a
  deterministic, token-bounded schedule (no per-step blind resend).

Add / change:

- `prompt_image_policy.should_include_prompt_images(step, *, policy,
  last_sent_url, current_url, early_steps=1)` — deterministic schedule.
  Policies: `adaptive` (default: first step + on page-URL change), `always`,
  `first`, `off`. Configurable via `vlm_options["prompt_image_policy"]`.
- `main.run_agent` — new `prompt_images: list[str] | None` param; reads the
  policy; the single `vlm.ask` callsite now passes
  `extra_images=(_prompt_images if scheduled else None)` and tracks
  `_last_prompt_image_url` for the adaptive page-change signal.
- `smart_batch_runner` Branch A — extracts `attachment_result.image_b64` and
  forwards it as `run_agent(prompt_images=...)` via the `_run_one` closure
  (no orchestrator change; reuses the existing `setdefault` pattern). This is
  the api_server path (api -> run_smart_batch -> run_agent).
- `attachment_adapters.adapt_attachment` — bugfix: route `prompt_context`
  images to the image adapter by **suffix** too (`_IMAGE_SUFFIXES`), not just
  mime. Uploaded files usually carry only a suffix, so the image adapter was
  previously unreachable (fell through to the text adapter), which left
  Phase 1's image path dead on the real api flow.

Acceptance:

- `tests/test_attachment_image_phase2_wiring.py` 10 passed (policy matrix,
  run_agent signature, smart_batch forwards a png as `prompt_images`, text
  attachment injects nothing).
- Backward compatible: `prompt_images` defaults to None; existing callers and
  runs are unchanged.

Out of scope (deliberate, YAGNI):

- CLI `--upload-file` keeps `set_input_files` (upload_to_page) semantics; no
  new CLI image-as-prompt flag this slice.


## Slice FITMD-1: Fit Markdown (page → LLM-friendly Markdown)

Goal:

- Borrow crawl4ai's "fit markdown": turn a full page into denoised,
  LLM-friendly Markdown for question-answering / RAG, deterministically and
  with far fewer VLM tokens than a full-page screenshot. Read-only, stub-testable.

Add / change:

- `extraction_engine/fit_markdown.py` — pure `html_to_fit_markdown(html, *,
  base_url, query, prune, bm25_threshold)`: tag + class/id boilerplate denoise
  (nav/header/footer/aside/script/ads), density pruning of link-farm blocks,
  heading/list/blockquote/code mapping, numbered link references (relative URLs
  resolved against `base_url`), optional BM25 query filter. Zero external deps
  (stdlib `html.parser`, mirrors `generic.py`).
- `page_to_markdown_action.py` — `PageToMarkdownHandler` in its **own module**
  (not appended to `actions.py` per workflow §三). Reads the live tab HTML →
  fit markdown → writes a `markdown_doc` artifact + manifest entry, mirrors the
  text into `workflow_memory`, records RPA-trail evidence. `type_value` is an
  optional BM25 focus query.
- `vlm_client.VSpiderAction` — `+1` action literal `page_to_markdown`.
- `actions.py` — bottom trigger-import so the handler self-registers (no new
  handler body added to the oversized file).
- `action_registry.py` — `ActionTool` `page_to_markdown` (capability=extract,
  zh/en aliases, evidence=output_path/word_count/link_count/source_url).
- `capability_router.py` — `_MARKDOWN_RE` + `markdown_preferred` signal +
  `page_to_markdown` backend-plan step.
- `prompts.py` / `prompt_skills.py` — `PAGE_TO_MARKDOWN_SKILL` block +
  `_PAGE_TO_MARKDOWN_TRIGGERS` + selection wiring.
- `tests/agent_cases/cases.json` — `fit_markdown_wikipedia_article` regression.

Acceptance:

- `tests/test_fit_markdown.py` 13 passed (denoise / structure / link refs /
  BM25 query / edge cases).
- `tests/test_page_to_markdown_handler.py` 8 passed (schema, registration,
  memory writeback, artifact + manifest persistence, query focus, no-page error).
- `tests/test_page_to_markdown_router.py` 5 passed (zh/en signal + plan step).
- `tests/test_page_to_markdown_prompt.py` 5 passed (skill map + prompt injection).

Out of scope (deliberate, separate slices):

- URL Seeder (sitemap / Common Crawl), deep-crawl best-first + resume_state,
  proxy-chain upgrade, chunking/cosine filtering. These are the other crawl4ai
  borrow-points and ship independently.


## Slice CRAWL-BF1: Best-first deep-crawl frontier (opt-in)

Goal:

- Borrow crawl4ai's best-first deep crawl: pop the most *relevant* link first
  so the crawler reaches on-topic pages in fewer rounds (mission §一 "高效 /
  最少回合 / 智能"). The legacy crawl is a FIFO BFS over a `deque`; best-first
  is strictly opt-in, so default behaviour (and every existing test) is
  byte-identical.

Add / change:

- `crawl_frontier.py` — new pure module (stdlib only, stub-testable):
  `normalize_keywords`, `score_url(url, keywords, *, anchor_text)` (keyword hits
  in URL path/query @1.0 + anchor @0.5), `BFSFrontier` (FIFO, deque-identical),
  `BestFirstFrontier` (max-heap; tie-breaks score → shallower depth → insertion
  FIFO; degrades to depth/FIFO when no keywords), `build_frontier(strategy, *,
  keywords, seeds)` factory (unknown strategy → BFS).
- `spider_lite.py` — `run()` swaps the raw `deque` for `build_frontier(...)`
  (push/pop/len only; BFS path unchanged). `_config` adds `crawl_strategy`
  (`crawl_strategy` / `best_first` flag, validated against {bfs, best_first},
  default bfs) and `keywords` (`keywords` / `relevance_keywords` /
  `relevance_query` / `crawl_keywords`). No agent-action / vlm_client change —
  this is crawl infra, not a new VSpiderAction.

Acceptance:

- `tests/test_crawl_frontier.py` 18 passed (keyword norm, URL scoring, BFS FIFO,
  best-first ordering + tie-breaks + no-keyword degradation, factory).
- `tests/test_spider_lite_best_first.py` 5 passed (BFS default unchanged,
  best-first visits relevant page first, full-order, relevance_query alias,
  unknown-strategy fallback).
- `tests/test_spider_lite.py` 9 passed (legacy BFS crawl behaviour intact).

Out of scope (deliberate, next slices):

- Anchor-text-enriched link extraction (richer best-first signal — shipped in
  CRAWL-BF2 below), URL Seeder (sitemap), resume_state checkpointing,
  chunking/cosine filtering.


## Slice CRAWL-BF2: Anchor-text-enriched link extraction

Goal:

- Wire the dormant anchor-text relevance signal into best-first crawl.
  CRAWL-BF1's `score_url` already weights anchor text at 0.5, but the legacy
  link extraction discarded anchor text (kept only `href`), so best-first only
  ever saw URL-path tokens. Capturing anchor text lets the frontier rank a link
  whose *URL* is opaque (e.g. `/p?id=42`) but whose *anchor* is on-topic
  (mission §一 "精准 / 高效 / 最少回合"). Default BFS is byte-identical
  (it ignores `anchor_text`).

Add / change:

- `spider_lite._LinkParser` — now collects `(href, anchor_text)` pairs:
  accumulates text between `<a href=...>` and `</a>` (incl. nested inline
  tags), whitespace-collapses on flush, and flushes a dangling unclosed `<a>`
  via a `close()` override.
- `spider_lite.extract_links_with_anchors(html, base_url) -> list[tuple[str,
  str]]` — new public function (dedup by normalized URL, first anchor wins,
  document order). `extract_links` is re-expressed on top of it and stays
  byte-identical (`list[str]`, dedup, order) so every legacy caller/test is
  untouched.
- `spider_lite.run()` — follow-links loop now iterates
  `extract_links_with_anchors(...)` and passes `anchor_text=` into
  `frontier.push(...)`. No agent-action / vlm_client change — crawl infra only.

Acceptance:

- `tests/test_link_anchor_extraction.py` 8 passed (extract_links backward
  compat, anchor capture, whitespace/nested-tag collapse, empty anchor, dedup
  keeps first anchor, unclosed trailing anchor; best-first reorders on an
  anchor-only keyword while BFS keeps document order).
- `tests/test_spider_lite.py` 9 + `tests/test_spider_lite_best_first.py` 5 +
  `tests/test_crawl_frontier.py` 18 still pass (no regression).
- `scripts/validate_y.py CRAWL-BF2 --target-test
  tests/test_link_anchor_extraction.py` → target / frontend_build / core /
  full all exit 0 (full: 2281 passed, 2 skipped).

Out of scope (deliberate, next slices):

- URL Seeder (sitemap / Common Crawl — shipped in CRAWL-SEED1 below),
  resume_state checkpointing, chunking/cosine filtering, proxy-chain upgrade.


## Slice CRAWL-SEED1: URL Seeder (sitemap / robots discovery)

Goal:

- Borrow crawl4ai's `AsyncUrlSeeder`: discover a site's URL inventory from
  `sitemap.xml` / sitemap-index / `robots.txt` so a crawl can start from the
  site's own map instead of crawling blindly from one page (mission §一
  "高效 / 通用 / 最少回合"). Pure stdlib parsing + a fetcher-injected walk,
  stub-testable (no network). Opt-in wiring keeps the default crawl unchanged.

Add / change:

- `url_seeder.py` — new pure module (stdlib only):
  `parse_sitemap_locs(xml)` (`<loc>` text via `html.parser`, urlset + index),
  `is_sitemap_index(xml)`, `sitemap_urls_from_robots(robots)` (`Sitemap:`
  directives), and `UrlSeeder(fetcher)` with `seed_from_sitemap(url, *,
  max_urls, max_sitemaps, allowed_domains, keywords)` (BFS over sitemaps,
  recurses indexes, dedup/order/cap, optional domain + `score_url` keyword
  filter) and `seed_from_robots(url, ...)`. Reuses `crawl_frontier.score_url`
  and `spider_lite.normalize_url/domain_of`; `spider_lite` imports it lazily
  (inside `run`) to avoid an import cycle.
- `spider_lite._config` — reads `seed_sitemap` (`seed_sitemap` / `sitemap`);
  `start_urls` requirement is relaxed *only* when `seed_sitemap` is present
  (default error path unchanged).
- `spider_lite._apply_sitemap_seeds(config)` — opt-in (no-op without
  `seed_sitemap`): merges discovered seeds after explicit `start_urls`
  (deduped, explicit-first) and unions their hosts into `allowed_domains` so the
  domain gate in `run` admits them. Called at the top of `run()`.

Acceptance:

- `tests/test_url_seeder.py` 12 passed (loc parse + whitespace/non-loc tags,
  index detection, robots directives, flat seed, index recursion, max_urls cap,
  domain filter, keyword filter, robots entrypoint, spider seed-only run,
  start_urls-still-required guard).
- `tests/test_spider_lite.py` 9 + `tests/test_spider_lite_best_first.py` 5 +
  `tests/test_link_anchor_extraction.py` 8 + `tests/test_crawl_frontier.py` 18
  still pass (default crawl byte-identical).

Out of scope (deliberate, next slices):

- Common Crawl / search-API seeding, HEAD-based liveness + metadata scoring,
  resume_state checkpointing, chunking/cosine filtering (shipped in FITMD-2
  below), proxy-chain upgrade.


## Slice FITMD-2: Markdown chunking + BM25 relevance filtering

Goal:

- Borrow crawl4ai's chunking strategies: split FITMD-1 fit-markdown output into
  RAG-ready chunks and rank / filter them against a query, so downstream
  question-answering / RAG feeds only the relevant slices of a long page
  (mission §一 "高效 / 精准 / 最少回合"). Pure stdlib, reuses fit_markdown's
  BM25 so scoring never drifts. Stub-testable, no consumer behaviour changed.

Add / change:

- `extraction_engine/chunking.py` — new pure module:
  `Chunk(text, index, word_count, heading)`; `chunk_markdown(md, *, strategy,
  max_words, overlap, min_words)` with strategies `heading` (split at
  `#`..`######`, window-split oversized sections, keep section heading),
  `window` (word-budget packing with overlap), `paragraph` (one per blank-line
  block); `score_chunks` / `rank_chunks` / `filter_chunks` reusing
  `fit_markdown._bm25_scores` + `_tokenize` (empty query = no-op).

Acceptance:

- `tests/test_chunking.py` 11 passed (heading sectioning + heading field,
  oversized-section split, window budget + overlap tail, paragraph mode,
  min_words drop + reindex, empty input, BM25 score ordering, rank top_k,
  filter threshold, empty-query passthrough).
- `tests/test_fit_markdown.py` 13 still pass (BM25 helpers reused, not changed).

Out of scope (deliberate, next slices):

- Wire chunks into the `page_to_markdown` handler as a `chunks.jsonl` artifact
  (needs an action-schema field — own slice), embedding/cosine chunking,
  semantic/topic segmentation, token-based (vs word) budgets.


## Slice CRAWL-RESUME1: Resumable deep-crawl checkpointing (opt-in)

Goal:

- Borrow crawl4ai's resumable deep crawl: a long crawl periodically persists
  its progress so an interrupted run resumes from the saved frontier instead of
  re-fetching everything (mission §一 "高效 / 最少回合"; §三 "能缓存就不重抓").
  Opt-in via `resume_state_path`; default crawl is byte-identical.

Add / change:

- `crawl_checkpoint.py` — new pure module: `save_checkpoint(path, state)`
  (atomic tmp-file + `os.replace`, makes parent dirs) and `load_checkpoint(path)`
  (tolerant — missing / corrupt → `None`). No crawl logic, trivially testable.
- `crawl_frontier.py` — `BFSFrontier.snapshot()` / `BestFirstFrontier.snapshot()`
  return pending `[{"url", "depth"}]`; `build_frontier(..., pending=...)`
  restores a snapshot after the seeds (best-first re-scores by URL — anchor is
  not persisted, a documented best-effort tradeoff).
- `spider_lite.py` — `_config` reads `resume_state_path` + `checkpoint_every`
  (default 1, clamped ≤1000). `run()` loads a prior checkpoint (restoring
  `seen` / `pages` / `items` / `errors` + rebuilding the frontier from
  `pending`, sets `result["resumed"]=True`), and `_maybe_checkpoint()` saves
  every `checkpoint_every` pages plus once at the end (`force`). All gated on
  `resume_state_path`, so the default path adds no keys and no I/O.

Acceptance:

- `tests/test_crawl_resume.py` 8 passed (checkpoint round-trip / missing /
  corrupt; BFS + best-first snapshot+restore; run writes checkpoint; resume
  skips already-seen URLs and continues from pending without re-fetch; default
  run writes nothing and has no `resumed` flag).
- `tests/test_crawl_frontier.py` 18 + `tests/test_spider_lite.py` 9 +
  `tests/test_spider_lite_best_first.py` 5 + `tests/test_link_anchor_extraction.py`
  8 + `tests/test_url_seeder.py` 12 still pass (default crawl byte-identical).

Out of scope (deliberate, next slices):

- Persisting page-response cache alongside the checkpoint, checkpoint
  compaction / TTL, anchor-preserving best-first resume, cross-process locking.


## Slice FITMD-3: page_to_markdown emits a chunks.jsonl artifact

Goal:

- Wire FITMD-2 chunking into the `page_to_markdown` handler end-to-end: every
  run also splits the produced fit-markdown into RAG-ready chunks and persists
  them, so a downstream RAG / QA step gets ready-to-embed slices for free
  (mission §一-A "产出形态由任务驱动" — markdown_doc + its chunk index).
  Achieved *without* a new action-schema field (chunking runs automatically
  with heading-strategy defaults), so no cross-layer `vlm_client` / `actions`
  change is needed.

Add / change:

- `page_to_markdown_action.py` — after `html_to_fit_markdown`, run
  `chunk_markdown(result.markdown, strategy="heading")`, expose `chunk_count` +
  `chunks_path` in `workflow_memory` and the RPA trail, and add
  `_persist_chunks()` which writes a `markdown_chunks` jsonl artifact (via
  `data_writers.write_jsonl`) + manifest entry when a run context is active.
  Markdown output / `markdown_doc` artifact are unchanged; chunks are additive.

Acceptance:

- `tests/test_page_to_markdown_chunks.py` 5 passed (chunk_count in memory,
  count present without run context, RPA-trail chunk_count, chunks.jsonl
  persisted with heading/index/word_count/text, markdown_doc still written
  alongside).
- `tests/test_page_to_markdown_handler.py` 8 + `_router.py` 5 + `_prompt.py` 5 +
  `tests/test_chunking.py` 11 still pass (markdown behaviour unchanged).

Out of scope (deliberate, next slices):

- An action-schema field to pick chunk strategy / max_words / query-rank at call
  time, embedding/cosine chunking, exposing chunks via the planner contract.


## Slice FITMD-4: focus query ranks + scores the chunks artifact

Goal:

- Close FITMD-3's named "query-rank at call time" gap (mission §一 "高效 / 精准").
  `page_to_markdown`'s `type_value` is already the BM25 focus query used to fit the
  markdown, but the persisted `markdown_chunks` jsonl ignored it: chunks were always
  heading-order and unscored, so a downstream RAG / QA step had to re-score to find
  the relevant slices. Make the chunk artifact query-aware. Additive + opt-in (no
  query -> byte-identical to FITMD-3); no new action-schema field (reuses type_value).

Add / change (page_to_markdown_action.py, +score_chunks import / +28 lines):

- New `_chunk_records(chunks, query)` static helper. No query -> records stay in
  document order with the original 4 keys (index/heading/word_count/text). With a
  focus query -> records are reordered most-relevant-first (BM25 via FITMD-2's
  `score_chunks`, stable on ties by original index) and each carries a rounded
  `score`; `index` still reflects the original document position.
- `execute()` builds chunk_records via the helper instead of an inline doc-order
  comprehension. Markdown output / markdown_doc artifact unchanged.

Acceptance:

- `validate_y FITMD-4` (target -> npm build -> core -> full): target
  `tests/test_page_to_markdown_chunks.py` -> **7 passed** (5 prior + 2 new: no-query
  stays doc-order & unscored; focus query ranks Beta-first with descending scores)
  -> `npm run build` ok -> core **102** -> **full 2542 passed, 2 skipped** (135s) ->
  `[validate_y] success`. ReadLints clean; `test_page_to_markdown_handler.py` 8 +
  `test_chunking.py` 11 unchanged.

Out of scope (deliberate, next slices):

- An action-schema field to pick chunk strategy / max_words / top_k / threshold at
  call time; embedding/cosine chunking; exposing the ranked chunks through the
  planner contract / output_contract resolver.


## Slice CRAWL-RESUME2: Anchor-preserving best-first resume

Goal:

- Close CRAWL-RESUME1's documented gap: the best-first frontier discarded
  anchor text after `push`, so a resumed crawl re-scored pending links by URL
  only and lost the anchor@0.5 relevance signal (CRAWL-BF2). Persist the anchor
  in the snapshot so resume re-applies it.

Add / change:

- `crawl_frontier.BestFirstFrontier` — heap entries now carry the anchor text
  (`(-score, depth, seq, url, anchor)`); `snapshot()` emits
  `{"url", "depth", "anchor_text"}`; `build_frontier(..., pending=)` re-pushes
  with `anchor_text=`. BFS is unaffected (it ignores anchor). `spider_lite`
  resume_state therefore round-trips the anchor signal with no handler change.

Acceptance:

- `tests/test_crawl_resume.py` 9 passed (added: snapshot preserves anchor and a
  restored frontier pops the anchor-only-keyword page first).
- `tests/test_crawl_frontier.py` 18 + `tests/test_spider_lite_best_first.py` 5 +
  `tests/test_link_anchor_extraction.py` 8 + `tests/test_spider_lite.py` 9 still
  pass (heap tie-break unaffected — the unique seq still decides equal scores).


## Slice CRAWL-SEED2: URL Seeder HEAD liveness probe + content-type

Goal:

- Borrow crawl4ai's URL-seeder `live_check` / metadata pass: cheaply HEAD-probe
  candidate URLs (status + content-type, no body) so dead links are dropped
  before they enter the crawl frontier (mission §一 "高效 / 准确"). Opt-in via an
  injected `head_fetcher`; absent it, everything is a no-op.

Add / change:

- `url_seeder.UrlSeeder` — `__init__(fetcher, *, head_fetcher=None)`;
  `_normalize_head(resp)` coerces a head response (dict / object,
  `status_code` + `content_type` / `headers`) to `(status, content_type)`;
  `probe_url(url) -> {url, status_code, content_type, live}` (`live` =
  `200<=status<400`, error / no-fetcher → not-live); `seed_from_sitemap` /
  `seed_from_robots` gain `live_only=` to filter to live URLs.

Acceptance:

- `tests/test_url_seeder_probe.py` 7 passed (probe live 200 / dead 404 /
  no-fetcher / error-swallow; live_only drops dead; keep-all default; live_only
  no-op without head_fetcher).
- `tests/test_url_seeder.py` 12 still pass (probe is purely additive).

Out of scope (deliberate, next slices):

- Real network HEAD backend, parallel probing, content-type → output_kind
  routing, last-modified / size metadata scoring.


## Slice PROXY-1: Proxy-chain rotation strategy (pure)

Goal:

- Borrow crawl4ai's proxy rotation (RoundRobin / failover): turn the single
  static proxy (`config.PROXY_SERVER`) into a resilient chain the browser
  substrate can rotate through (mission §一 "通用 / 遇阻即换路"). Pure rotation
  + spec parsing now; the `browser_env` wiring is a separate slice because that
  file is oversized (workflow §三).

Add / change:

- `proxy_chain.py` — new pure module: `parse_proxy(spec)` normalizes
  `host:port` / `scheme://host:port` / `user:pass@host:port` /
  `scheme://user:pass@host:port` / dict into a Playwright proxy dict
  (`{server, username?, password?}`, the shape `browser_env` already feeds
  Playwright); `ProxyChain(proxies, strategy)` with `current()` / `next()`
  (round-robin advances, failover holds until `mark_failed()`); `build_proxy_chain`
  factory (unknown strategy → round-robin, invalid specs dropped).

Acceptance:

- `tests/test_proxy_chain.py` 11 passed (parse: plain / scheme / creds /
  scheme+creds / dict / empty; round-robin cycle; failover hold + mark_failed;
  current no-advance; empty chain safe; unknown-strategy fallback; invalid-spec
  drop).

Out of scope (deliberate, next slices):

- Wire `ProxyChain` into `browser_env` launch + per-context rotation on
  block / bot-challenge, health scoring, geo/sticky-session pools.


## Slice CRAWL-SEED3: URL Seeder real HEAD fetcher (urllib)

Goal:

- Close CRAWL-SEED2's "Real network HEAD backend" gap: ship a production
  `head_fetcher` so `live_only` seeding works against a live site, not just a
  stub (mission §一 "高效 — 能不重抓就不重抓"). Opt-in; the default crawl path is
  unchanged (callers must pass `head_fetcher=default_head_fetch`).

Add / change:

- `url_seeder.default_head_fetch(url, *, timeout=10.0)` — stdlib `urllib`
  `Request(method="HEAD")` + `urlopen`; returns `{status_code, content_type}`.
  `HTTPError` (404/500…) keeps its real status via `exc.code`; network / DNS
  errors collapse to status `0` so `probe_url` reports not-live. Exported in
  `__all__`; `_normalize_head` already understands the returned dict.

Acceptance:

- `tests/test_url_seeder_probe.py` 11 passed (added 4: HEAD success, HTTPError
  keeps 404, network error → status 0, default_head_fetch wired into probe_url;
  all via a monkeypatched `urlopen`, no network).

Out of scope (deliberate, next slices):

- Parallel probing, last-modified / size metadata scoring, content-type →
  output_kind routing, HEAD→GET fallback for servers that reject HEAD.


## Slice PROXY-2: Wire proxy-chain into browser_env launch

Goal:

- Close PROXY-1's "wire ProxyChain into browser_env" gap: the browser substrate
  resolves its launch proxy from a rotatable chain instead of the single static
  `PROXY_SERVER` (mission §一 "通用 / 遇阻即换路"). Backward compatible — a lone
  `PROXY_SERVER` still works; logic lives in `proxy_chain`, not the oversized
  `browser_env` (workflow §三).

Add / change:

- `proxy_chain.build_chain_from_config(cfg)` — reads `PROXY_CHAIN`
  (list / comma-str, `round_robin` / `failover`), falling back to a one-entry
  chain from `PROXY_SERVER` (+ `PROXY_USERNAME` / `PROXY_PASSWORD`); empty chain
  when nothing is configured. `config` gains `PROXY_CHAIN` / `PROXY_STRATEGY`
  (env `VSPIDER_PROXY_CHAIN` / `VSPIDER_PROXY_STRATEGY` + `apply_run_constraints`).
  `browser_env.start()` now sets `_proxy_cfg = build_chain_from_config(config)
  .current()` and keeps `self._proxy_chain` for future re-route-on-block.

Acceptance:

- `tests/test_proxy_chain.py` 17 passed (added 6: config chain priority, csv
  split, single-server fallback, empty chain, strategy honored, missing-attrs
  safe). `tests/test_bot_challenge_extras.py` still passes (config constraints).
- Full `validate_y` green: target + npm build + core + full (2346 passed,
  2 skipped).

Out of scope (deliberate, next slices):

- Per-context rotation on block / bot-challenge (`mark_failed()` + relaunch),
  proxy health scoring, geo / sticky-session pools.


## Slice BATCH-RESUME1: Batch-row resume (skip already-successful rows)

Goal:

- Close the gap "an interrupted xlsx batch restarts from row 0": opt-in resume
  carries a prior run's successful rows back into a freshly-loaded DataFrame so
  the orchestrator's existing skip-on-成功 logic continues instead of re-running
  everything (mission §一 "高效 — 能不重抓就不重抓"; §四 长批量最怕跑一半断).
  Default off → byte-identical to the legacy full re-run.

Add / change:

- `smart_batch_runner.py` — `_row_resume_key(df)` (prefers a URL-like column,
  else "" → positional match) + `_merge_prior_progress(df, prior_df, key_col=)`
  (carries only 填报状态=="成功" by key or row position, never downgrades an
  already-success row, returns resumed_count). `run_smart_batch` /
  `run_smart_batch_sync` gain `resume: bool`; resume is also read from
  `run_constraints["resume"]`, so the API path (which already threads
  run_constraints) needs no api_server change. On resume the prior
  `<stem>_处理结果.xlsx` (stable artifact path) is merged before the run.
- `capability_router._QUEUE_RE` — adds 续跑 / 断点 / 续传 / checkpoint keywords.

Acceptance:

- `tests/test_batch_resume.py` 9 passed (key selection; keyed merge carries only
  success, retries failed, handles reorder / new rows; positional fallback +
  shorter prior; no-status guard; no-downgrade guard). Pure DataFrame, no
  agent / network.
- `tests/test_smart_batch_runner_status.py` / `_dispatch.py` /
  `test_batch_orchestrator.py` / `test_capability_router.py` still pass (resume
  is additive + opt-in).

Out of scope (deliberate, next slices):

- Frontend / API toggle to set constraints.resume; content-hash row keys for
  rows without a URL column; resuming the in-flight row (RUN-RESUME1 territory);
  DL-RESUME1 (download Range) and RUN-RESUME1 (agent mid-task checkpoint).


## Slice DL-RESUME1: Media downloader HTTP Range / If-Range resume

Goal:

- Close the gap "an interrupted media download restarts from byte 0": opt-in
  resume continues a partial download via HTTP Range instead of re-fetching the
  whole resource (mission §一 "高效 — 能不重抓就不重抓"). Default off →
  byte-identical to the legacy single-pass download.

Add / change:

- `media_harvester/downloader.py` — `download_candidate(..., resume=False)`. When
  resume, a stable per-URL `.{sha(url)[:24]}.part` (+ a `.meta` validator sidecar)
  lets a retry send `Range: bytes=<size>-` + `If-Range: <etag|last-modified>`.
  206 → append (hasher seeded from the existing part so the final sha256 matches a
  single-pass download); 200 → overwrite (server ignored range / resource changed);
  416 → discard the stale part and error. On mid-stream failure the `.part` +
  `.meta` are kept so the next call continues; on success the part is renamed to the
  content-addressed `<sha>.<ext>` and the meta removed. New helpers: `_url_part_key`,
  `_validator_from_headers`, `_read_part_validator` / `_write_part_validator`,
  `_seed_hasher_from_part`, `_discard_part`. `extra.resumed` records the append.

Acceptance:

- `tests/test_download_resume.py` 5 passed (fresh resume sends no Range; partial
  continues with Range=bytes=200- + If-Range and appends to the right sha; server
  ignoring range overwrites cleanly; mid-stream failure keeps the .part; the default
  non-resume path sends no Range and still cleans its .part on failure).
- `tests/test_media_harvester.py` + `tests/test_media_harvester_agent_hook.py` (53)
  still pass (resume is additive + opt-in).

Out of scope (deliberate, next slices):

- Wiring resume=True through the harvester / agent download hook + a run policy;
  parallel multi-part (segmented) download; disk-space / TTL GC of stale `.part`
  files; RUN-RESUME1 (agent mid-task checkpoint).


## Slice RUN-RESUME1: Agent mid-task run checkpoint (step 1 — primitive)

Goal:

- Close the last 断点重跑 gap (#3) "a crashed / killed agent run restarts from
  turn 0": lay the deterministic foundation for an agent run to persist how far
  it got and to *decide* (purely) whether a later run should resume. Default off
  → byte-identical to today (no `run_checkpoint.json` written, manifest
  `resumed_from` omitted) (mission §一 "高效 — 能不重抓就不重抓"; 长任务最怕跑一半断).

Add / change:

- New `visual_web_agent/run_checkpoint.py` (stdlib only, mirrors
  `crawl_checkpoint`): `build_checkpoint_state(...)` (pure `run_checkpoint.v1`
  dict), `save_run_checkpoint` / `load_run_checkpoint` / `clear_run_checkpoint`
  (atomic tmp + `os.replace` write; tolerant missing / corrupt → None) under
  `runs/<run_id>/run_checkpoint.json`; `ResumeDecision` + `decide_resume(prior,
  *, resume, goal, start_url)` (pure policy: resume-off / no-checkpoint /
  prior-terminal / goal|url-mismatch → fresh; else resume from the recorded turn
  exposing a `resumed_from` provenance payload); `mark_manifest_resumed`
  (read-modify-write manifest provenance).
- `io_contract/manifest.py` — `Manifest` gains an optional `resumed_from` field
  (+ `set_resumed_from` helper). `to_dict` emits the key **only when non-empty**
  so unused-resume runs stay byte-identical; `from_dict` parses it tolerantly.
- `capability_router._QUEUE_RE` already routes resume / 续跑 / 断点 / checkpoint
  (added in BATCH-RESUME1) → no router change needed.

Acceptance:

- `tests/test_run_resume.py` 21 passed (build/coerce; save-load round trip;
  missing / corrupt → None; atomic overwrite leaves no temp; clear; decide_resume
  off / no-checkpoint / terminal / goal-mismatch / url-mismatch / in-progress /
  failed-resumable; manifest resumed_from absent-by-default / set / clear /
  bad-input; mark_manifest_resumed writes + empty stays byte-identical). Pure
  I/O + pure policy, no agent / network.
- Regression: `tests/test_io_contract_persistence.py` + io_contract runtime +
  register_artifact + data_writers dispatch + media_harvester (92) still pass
  (manifest field is additive + omitted when empty). `py_compile` OK, lint clean.

Out of scope (deliberate, step 2):

- Wiring `save_run_checkpoint` into the `main.py` agent loop turn-by-turn (write a
  checkpoint each turn, read `constraints.resume` → `decide_resume`, skip
  already-`completed_steps`, `mark_manifest_resumed` on resume,
  `clear_run_checkpoint` on success); deterministic partial-run replay; a
  `resume_run` action / prompt skill. Step 1 ships only the primitive + decision
  so the loop wiring lands as a separate reviewable slice.


## Slice RUN-RESUME1 (step 2a): RunCheckpointer lifecycle glue

Goal:

- Wrap the step-1 primitives in a single stateful helper so the agent loop needs
  only a few guarded one-liners (begin / per-turn record / finish), and so the
  lifecycle (load prior → decide_resume → mark manifest provenance → write each
  turn → clear on success / persist `failed` on abort) is unit-testable WITHOUT
  the VLM loop. Opt-in via `constraints.resume`; inert + byte-identical when off.

Add / change:

- `run_checkpoint.RunCheckpointer` — `begin(run_id, *, resume, goal, start_url,
  base_dir)` (inert when `resume` falsy; else load + `decide_resume` +
  `mark_manifest_resumed` on a detected resume), `record(turn, *, phase,
  item_count, last_action, completed_steps, extra)` (atomic `in_progress` write),
  `finish(success)` (clear on success / write `failed` otherwise),
  `should_resume` property. **Every method swallows its own I/O errors** so a
  checkpoint failure can never abort the agent run.

Acceptance:

- `tests/test_run_resume.py` 26 passed (+5: disabled-inert writes nothing;
  enabled-fresh writes in_progress then clears on success; failure persists
  `failed`; resume-from-prior marks `manifest.resumed_from`; goal-mismatch
  neither resumes nor writes a manifest). `py_compile` OK, lint clean.

Out of scope (step 2b + step 3):

- Step 2b: the actual `main.py` wiring — 4 guarded call sites in `run_agent`
  (`_run_ckpt=None` init near `_run_succeeded`; `RunCheckpointer.begin(_run_ts,
  resume=bool(run_constraints.get("resume")), goal, start_url)` just before the
  `for step in range(...)` loop; `record(step, item_count=_total_extracted_rows)`
  in the step `finally`; `finish(_run_succeeded)` in the teardown `finally`).
  Held back because it edits the 850KB core loop (a core-link change) and wants
  an explicit go-ahead per repo rules.
- Step 3: actually consuming `decision.completed_steps` to skip already-done work
  (non-deterministic VLM replay) + a `resume_run` action / prompt skill.


## Slice RUN-RESUME1 (step 2b): wire RunCheckpointer into the agent loop

Goal:

- Actually call the step-2a checkpointer from `main.py::run_agent` so a real
  `resume=True` run leaves a durable per-turn `run_checkpoint.json`, records
  `manifest.resumed_from` when it detects a resumable prior, and clears the
  checkpoint on success. Opt-in; `resume` unset (default) → fully inert,
  byte-identical to before.

Add / change:

- `main.py::run_agent` — 4 guarded insertions (+27 lines, additive only):
  `_run_ckpt = None` init (next to `_run_succeeded`); `RunCheckpointer.begin(
  _run_ts, resume=bool((run_constraints or {}).get("resume")), goal, start_url)`
  just before the `for step in range(...)` loop; `_run_ckpt.record(step,
  item_count=_total_extracted_rows)` in the per-step `finally`;
  `_run_ckpt.finish(_run_succeeded)` in the run teardown `finally`. Each call is
  `if _run_ckpt is not None`-guarded and the begin/finish sites are also
  try/except-wrapped, so a checkpoint failure can never abort or alter a run. The
  pre-loop early-return fast paths sit upstream of the begin site, so they stay
  untouched (no checkpoint started → nothing to clear).

Acceptance:

- `validate_y run-resume`: target `tests/test_run_resume.py` 26 passed →
  `npm run build` ✓ → core 102 passed → **full 2386 passed, 2 skipped** (138s).
  `py_compile main.py` OK, lint clean, `main.py` EOL (CRLF) preserved, diff +27/-0.

Out of scope (step 3):

- Consuming `decision.completed_steps` / `from_turn` to actually skip already-done
  work on a resumed VLM run (non-deterministic replay); a `resume_run` action +
  prompt skill; surfacing resume state into `workflow_memory` / the planner.


## Slice RUN-RESUME1 (step 3a): seed dedup state from the prior run on resume

Goal:

- Make `resume=True` runs actually *reuse* the prior run's extracted dataset:
  rebuild the dedup seen-set + accepted row count from the last run for the same
  `(goal, start_url)` so a resumed run skips already-captured rows instead of
  re-emitting them (mission §一 "高效 — 能不重抓就不重抓"). Opt-in via
  `constraints.resume`; default-off stays byte-identical (no seam runs).

Add / change (5 commits cef43e4..576a134):

- `run_resume_index.py` (new, data_plane / cef43e4) — `compute_resume_key`
  (goal+start_url → stable 16-hex) + cross-run locator over
  `runs/_resume_index.json` via `record_run` / `lookup_last_run`. Atomic
  tmp + `os.replace`, tolerant reads, `base_dir` injectable.
- `data_sanitizer.rebuild_seen_fingerprints` (+34 / fa9d866) — reuses
  `_non_empty_values` + `_row_fingerprints` to reconstruct the dedup seen-set +
  accepted count from prior rows. Pure, deterministic, no I/O.
- `dataset_reader.py` (new, data_plane / 3575f00) — `read_dataset_rows` reads a
  prior run's xlsx/csv/jsonl/json dataset back into row dicts (NaN/blank → ''
  for fingerprint hygiene, aligned with attachment_adapters/rows.py). Tolerant:
  `[]` on missing/corrupt/unsupported.
- `resume_seed.py` (new, data_plane / 5defeb7) — orchestration façade:
  `seed_seen_from_last_run` (index lookup → manifest dataset locate → reader →
  rebuild) and `record_run_for_resume` (upsert this run + its latest dataset
  path). Every fn degrades to a fresh run on error; `base_dir` injectable.
- `main.py::run_agent` (+53 / 576a134, execution_kernel) — 3 guarded blocks gated
  on `constraints.resume`: (1) seed seen-set + total row count from the prior
  dataset before extraction, (2) register the run in `_resume_index.json` at
  start (so a mid-run crash stays resumable), (3) finalize the entry (final
  count + `completed`/`failed` status) at teardown. Each block try/except-wrapped
  → resume off is inert, a seed mishap degrades to a fresh run. No contract
  field changes.

Acceptance:

- Targeted (this session): `tests/test_run_resume_index.py` +
  `tests/test_rebuild_seen_fingerprints.py` + `tests/test_dataset_reader.py` +
  `tests/test_resume_seed.py` → **52 passed in 2.31s**. Diff +1084/-1 across 9
  files (5 src + 4 test). Branch `feat/web-extraction-suite` @ `576a134`, pushed.

Out of scope:

- Consuming `decision.completed_steps` / `from_turn` to skip the VLM action replay
  itself (non-deterministic); a `resume_run` action + prompt skill; surfacing
  resume state into `workflow_memory` / the planner; a full `validate_y run-resume`
  (npm build + core + full pytest) re-run.

## Slice RUN-RESUME1 (step 3): consume the resume decision + `resume_run` action

Goal:

- Close the step-2b / 3a "out of scope" gap: actually *consume* a resume decision
  so a resumed run continues from where it left off instead of restarting. The VLM
  loop is non-deterministic (no byte-for-byte replay), so two honest, human-like
  mechanisms: (1) surface the resume state into `workflow_memory` + a natural-language
  directive so the planner/VLM continues toward the remaining goal (re-establishing
  nav/login when needed); (2) best-effort skip the leading already-completed planner
  sub-goals. Plus a `resume_run` capability so the agent can consult "where did the
  last run stop?" on demand. Default-off / inert on a fresh run (byte-identical when
  resume is not active).

Add (2 new modules):

- `run_resume_consume.py` (new, data_plane, pure stdlib, duck-typed) — 4 helpers:
  `plan_completed_step_labels(task_plan)` (record side: ordered descriptions of
  `done` sub-goals), `resume_memory_payload(decision)` (build the `__resume_state`
  payload), `build_resume_note(decision)` (CN "断点续跑" natural-language directive),
  and `apply_completed_steps_to_plan(task_plan, completed_steps)` (best-effort skip —
  exact normalized match on the *leading* run of sub-goals, advances `current_idx`,
  marks them `done` + next `active`, never skips the final sub-goal, self-healing via
  re-observation).
- `resume_run_action.py` (new, operations_plane) — `resume_run` deterministic,
  read-only handler shipped as its own module (workflow §三, not appended to the
  oversized actions.py). Resolution order: `workflow_memory["__resume_state"]`
  (loop-published) → the active run's `run_checkpoint.json` → fresh-task status. Writes
  the result to `workflow_memory[memory_key or "resume_status"]` + mirrors it on the
  RPA trail as evidence. No page mutation (works without a live page).

Change (7 files, +117/-1):

- `main.py::run_agent` (+47/-1, execution_kernel) — 2 guarded blocks: (1) right after
  the step-2b `begin`, when `_run_ckpt.should_resume`, publish `__resume_state` +
  `apply_completed_steps_to_plan(_task_plan, decision.completed_steps)`; (2) at the
  per-turn `record`, feed `plan_completed_step_labels(_task_plan) or None` as
  `completed_steps=` (the `or None` preserves a resumed ledger on turns with no newly
  done sub-goal). Both try/except-wrapped → inert unless resume. Verified safe vs
  `merge_task_plan_into_route` (reads only sub-goal descriptions, not
  `current_idx`/status, so a post-merge skip cannot desync the route).
- `action_registry.py` (+33) — register the `resume_run` ActionTool (capability=resume,
  CN/EN aliases, tags, evidence, deterministic, read-only).
- `actions.py` (+4) — import `resume_run_action` (mirrors the page_to_markdown wiring).
- `capability_router.py` (+5) — `_RESUME_RE` regex + `resume_preferred` signal +
  `resume_run` step in `_backend_plan`.
- `prompt_skills.py` (+21) — `RESUME_RUN_SKILL` block + `SKILL_PROMPTS["resume_run"]`.
- `prompts.py` (+7) — `_RESUME_RUN_TRIGGERS` (CN/EN) + skill activation in
  `build_system_prompt`.
- `vlm_client.py` (+1) — add `resume_run` to the `VSpiderAction` action literal.

Acceptance:

- `validate_y RUN-RESUME1` (target ×4 → npm build → core → full): target
  `tests/test_run_resume_consume.py` + `tests/test_resume_run_action.py` +
  `tests/test_resume_run_router.py` + `tests/test_resume_run_prompt.py` → **32 passed**
  → `npm run build` ✓ → core **102 passed** → **full 2470 passed, 2 skipped** (140s)
  → `[validate_y] success`. `py_compile main.py` OK, lint clean, `main.py` EOL (CRLF)
  preserved. Diff +117/-1 across 7 changed files + 6 new (2 src + 4 test).

Out of scope:

- Fuzzy / semantic matching of completed-step labels (currently exact normalized
  match); resuming the VLM's per-turn action replay byte-for-byte (intentionally
  avoided — the agent re-observes each turn); cross-run resume across *different*
  goals / start URLs.


## Slice RUN-RESUME1 (step 4): unified resume switch end-to-end

Goal:

- Make the `constraints.resume` switch reachable + uniform across all three opt-in
  resume mechanisms (mission §四 "简便 — 前端/CLI/API 三入口对等"; §一-B input
  contract `constraints.resume`). Before: batch row-skip (BATCH-RESUME1) + the agent
  run checkpoint (RUN-RESUME1 step2b/3a) already read `run_constraints.resume`, but
  (a) the media-download Range/If-Range resume (DL-RESUME1) was implemented yet never
  wired, and (b) the frontend had no toggle (users had to hand-craft constraints
  JSON). One switch now turns on batch + run + download together. Default off →
  byte-identical to the legacy single-pass run.

Add / change (1 new test + 5 changed, +24):

- `media_harvester/harvester.py` (+5) — `harvest_to_run(..., resume=False)` forwards
  `resume` into `download_candidate(resume=resume)` (DL-RESUME1's download logic was
  already complete; this threads the flag through). Docstring notes the Range behaviour.
- `media_harvester/agent_hook.py` (+2) — `maybe_run_media_harvest(..., resume=False)`
  forwards into `harvest_to_run`.
- `main.py::run_agent` (+1, execution_kernel) — the media-hook call site passes
  `resume=bool((run_constraints or {}).get("resume"))`, the same key batch + run use.
- `vspider-ui/src/composables/useTaskSubmit.js` (+4) — `buildTaskConstraints({resume})`
  emits `constraints.resume = true` when enabled.
- `vspider-ui/src/App.vue` (+12) — a "断点续跑（resume）" el-switch in the 运行约束 panel
  bound to a `resumeEnabled` ref, threaded into the `buildTaskConstraints({...})` call.
- `tests/test_media_harvest_resume_wire.py` (new) — 5 tests: harvest_to_run + the hook
  forward resume True/False and default to False (captured `download_candidate` stub).

Acceptance:

- `validate_y run-resume1-step4` (target → npm build → core → full): target
  `tests/test_media_harvest_resume_wire.py` → **5 passed** → `npm run build` ✓ → core
  **102 passed** → **full 2481 passed, 2 skipped** (136s) → `[validate_y] success`.
  `py_compile` OK, lint clean; CRLF preserved on harvester.py / main.py / App.vue.

Out of scope (deliberate, next slices):

- Per-mechanism granular toggles (one flag covers all three by design); content-hash
  row keys for batch rows without a URL column; download `.part` TTL / disk-space GC;
  surfacing `constraints.resume` into the input_contract.json schema doc + a CLI flag.


## Slice PROXY-3: proxy health scoring + rotate-on-block policy (pure)

Goal:

- Close PROXY-2's "per-context rotation on block + health scoring" gap, the pure
  half (mission §一 "通用 / 遇阻即换路"; "智能遇阻即换路"). Before:
  `ProxyChain.mark_failed()` just advanced the index — a bad proxy was retried on
  the next cycle and there was no signal for "when does a bot-challenge mean the IP
  is flagged?". Now the chain scores each proxy and quarantines repeat offenders,
  and a pure policy decides when a challenge warrants rotation. The live
  `browser_env` relaunch-on-block stays a separate slice (PROXY-4) — that file is
  oversized (workflow §三) and Playwright fixes the proxy at launch.

Add / change (proxy_chain.py +121/-3, pure):

- `ProxyChain` gains health scoring: per-proxy `{successes, failures,
  consecutive_failures, quarantined}`; `quarantine_threshold` (default 3) ctor arg;
  `report_success()` (clears quarantine); `mark_failed()` now records the failure,
  quarantines a proxy after N consecutive failures, and advances to the next
  *healthy* (non-quarantined) proxy; `_advance_to_healthy` resets all quarantine
  when the whole chain is exhausted (never stuck); `healthy_count()` + `stats()`
  snapshot. Backward compatible — with the default threshold a single failure still
  advances by one (the 17 PROXY-1/2 tests stay green).
- `should_rotate_on_challenge(result, state, *, min_encounters=2)` — pure policy
  duck-typed over `bot_challenge_guard.BotChallengeStepResult` / `BotChallengeState`:
  rotate when a challenge was detected and (a) not cleared, (b) hit the HITL ceiling
  (`max_hitl`), or (c) the same IP keeps getting challenged (`encounter_count >=`
  `min_encounters`). Not-detected → never rotate.

Acceptance:

- `validate_y proxy-3` (target → npm build → core → full): target
  `tests/test_proxy_chain.py` → **29 passed** (17 prior + 12 new: quarantine after
  threshold, report_success clears, all-quarantined reset, stats shape, empty-chain
  safe, single-failure backward-compat; policy not-detected / not-cleared / max_hitl
  / repeated-encounters / single-cleared / dict-inputs) → `npm run build` ✓ → core
  **102 passed** → **full 2493 passed, 2 skipped** (134s) → `[validate_y] success`.
  `py_compile` OK, lint clean.

Out of scope (deliberate, next slices):

- PROXY-4: wire `should_rotate_on_challenge` + `mark_failed()` into a `browser_env`
  re-route-on-block relaunch (touches the oversized substrate + run lifecycle);
  geo / sticky-session pools; per-proxy latency / bandwidth scoring; persisting
  proxy health across runs.


## Slice PROXY-4a: browser-substrate proxy reroute mechanism

Goal:

- Close the browser-substrate half of PROXY-4 (mission §一 "通用 / 遇阻即换路";
  "智能遇阻即换路"). PROXY-3 shipped the pure decision (`should_rotate_on_challenge`)
  + health rotation (`mark_failed`), but nothing on the browser side consumed them:
  the policy was dangling and `start()` rebuilt the chain on every `restart()`, which
  reset rotation back to proxy 0 — so a reroute could never actually switch IPs. Add
  the minimal, testable mechanism; the lifecycle *trigger* (PROXY-4b) stays out.

Add / change (browser_env.py, +~55 lines — thin substrate touch, logic stays in
`proxy_chain`):

- `__init__` now seeds `self._proxy_chain = None`.
- `_ensure_proxy_chain()` — build-once helper; `start()` calls it instead of an inline
  `build_chain_from_config`, so the chain (and its `_idx` / quarantine state) survives
  a `restart()` re-entry. First build is byte-identical to before.
- `reroute_proxy_on_block(url, *, result, state, reason)` — consults
  `should_rotate_on_challenge` (PROXY-3); when it says rotate AND a >1 proxy chain is
  configured, `mark_failed()` advances to the next healthy proxy and `restart()`
  relaunches with it (build-once chain carries the new `current()`). Returns True iff
  a reroute happened. No-op (False) without a usable multi-proxy chain, so single /
  no-proxy runs are unchanged. Nothing calls it yet (PROXY-4b), so zero behaviour change.

Acceptance:

- `validate_y PROXY-4a` (target -> npm build -> core -> full): target
  `tests/test_proxy_reroute.py` -> **6 passed** (build-once caches first build;
  rotation state survives; reroute rotates+restarts; policy-declines / single-proxy /
  no-chain all no-op) -> `npm run build` ok -> core **102** -> **full 2548 passed, 2
  skipped** (136s) -> `[validate_y] success`. ReadLints clean; `test_proxy_chain.py`
  29 unchanged.

Out of scope (deliberate, next slice):

- PROXY-4b: actually call `reroute_proxy_on_block` from the bot-challenge handling
  path (run lifecycle — when to trigger, retry budget, interaction with HITL /
  human_guard); geo / sticky-session pools; per-proxy latency scoring; persisting
  proxy health across runs.


## Slice PROXY-4b: wire proxy reroute into the bot-challenge lifecycle

Goal:

- Close PROXY-4a's deferred half — actually *call* the reroute mechanism from the
  agent loop so a flagged IP triggers a real IP swap (mission §一 "智能 / 遇阻即换路").
  The agent already runs `handle_bot_challenge_step` each step (passive_wait → captcha
  solver → HITL); now, after that recovery chain, consult the proxy policy and reroute
  when the IP still looks blocked — last resort *after* HITL, never instead of it.

Add / change (3 layers, minimal footprint — heavy logic stays in proxy_chain / browser_env):

- `bot_challenge_guard.BotChallengeState` (+2 pure fields): `reroute_count` +
  `max_reroute_per_run=3` — the per-run IP-swap budget.
- `browser_env.reroute_proxy_on_block` enforces the budget: when `state` carries
  `max_reroute_per_run`, a reroute past the cap is refused; each reroute bumps
  `state.reroute_count`. `state=None` (other callers) keeps the old unbounded behaviour.
- `main.py` bot-challenge block (1 call): after the existing recovery, calls
  `browser.reroute_proxy_on_block(browser.current_url, result=_bc_result, state=_bot_challenge_state)`.
  The pure `should_rotate_on_challenge` (PROXY-3) decides (not cleared / max_hitl /
  encounter ≥ 2); on a real reroute the browser restarts on the new proxy and the step
  re-screenshots so the next turn re-perceives the fresh IP. No proxy chain (the common
  case) → no-op, so default runs are unchanged.

Acceptance:

- `validate_y PROXY-4b` (target → npm build → core → full): target
  `tests/test_proxy_reroute.py` → **8 passed** (6 prior + 2 new: BotChallengeState
  budget fields default 0/3; reroute refused once `reroute_count` hits the cap, only one
  restart) → `npm run build` ok → core **102** → **full 2550 passed, 2 skipped** (135s)
  → `[validate_y] success`. `py_compile` (main/browser_env/bot_challenge_guard) OK;
  ReadLints clean; `test_proxy_chain.py` 29 + `test_bot_challenge_extras.py` unchanged.

Out of scope (deliberate, next slices):

- Rotate *before* escalating to HITL (avoid bugging the human first); geo /
  sticky-session pools; per-proxy latency / bandwidth scoring; persisting proxy health
  across runs; surfacing reroute events into the run manifest / event_stream.


## Slice BATCH-RESUME2: content-hash row keys for keyless batch resume

Goal:

- Close BATCH-RESUME1's "content-hash row keys for rows without a URL column" gap
  (mission §一 "高效 — 能不重抓就不重抓"; §四 长批量最怕跑一半断). Before: a table
  without a URL-like column fell back to *positional* resume matching, which
  silently breaks the moment rows are reordered or inserted between runs. Now a
  stable per-row content fingerprint matches prior successes order-independently;
  positional stays only as a last resort when the two frames' columns differ. Pure
  DataFrame, default path unchanged.

Add / change (smart_batch_runner.py +56, pure):

- `_row_content_hash(row, columns)` — order-independent sha256[:16] over a row's
  data cells (NaN / 'nan' / 'none' / blank → '', so reloaded frames hash stably);
  `_content_columns(df)` lists the data columns (excludes 填报状态 / 日志备注).
- `_merge_prior_progress` gains a Tier-2 content-hash pass between the URL-key tier
  and the positional fallback: when there is no usable URL key but the two frames
  share the same data columns, carry prior 成功 rows by content hash (robust to
  reorder / inserted rows; duplicate-content rows all resume idempotently). Falls
  through to positional only when the schemas differ. Backward compatible — the 9
  BATCH-RESUME1 tests stay green (their aligned data hashes the same as positional).

Acceptance:

- `validate_y batch-resume2` (target → npm build → core → full): target
  `tests/test_batch_resume.py` → **16 passed** (9 prior + 7 new: reorder match,
  inserted rows, multi-col carries-only-success, duplicate-content rows, positional
  fallback on schema mismatch, hash order-independence + blank normalization,
  content-columns exclusion) → `npm run build` ✓ → core **102 passed** → **full
  2500 passed, 2 skipped** (135s) → `[validate_y] success`. `py_compile` OK, lint clean.

Out of scope (deliberate, next slices):

- A configurable hash-column allowlist / blocklist (e.g. ignore a volatile
  timestamp column); persisting per-row keys in the result file; cross-file resume
  keyed by content across different output paths.


## Slice DL-GC1: stale download .part / .meta garbage collection

Goal:

- Close DL-RESUME1's "disk-space / TTL GC of stale .part files" gap (mission §一-B
  file governance: TTL + GC of unreferenced temp files). The resumable downloader
  leaves a stable `.{key}.part` + a `.meta` validator on disk when a download is
  interrupted, plus `download.*.part` mkstemp tempfiles that can leak on a crash —
  without a sweep they accumulate forever. Now a TTL sweep removes the stale ones
  while preserving any part still being actively appended. Default path unchanged.

Add / change (3 files, +45):

- `media_harvester/downloader.py` (+35) — `gc_stale_parts(dest, *,
  ttl_seconds=86400, now=None)`: scans `dest` for `*.part` (resumable `.{key}.part`
  + legacy `download.*.part`) and `*.part.meta` sidecars and unlinks any with mtime
  older than the TTL (default 24h); a recent / active part is preserved so an
  in-progress / resumable download is never clobbered. Returns the count removed;
  tolerant (missing dir / unlink error never raises). `now` injectable for tests.
- `media_harvester/harvester.py` (+8) — `harvest_to_run` sweeps
  `gc_stale_parts(artifacts_dir)` (best-effort, guarded) before the download loop,
  so a prior interrupted harvest of the same run cleans up its leftovers.
- `media_harvester/__init__.py` (+2) — re-export `gc_stale_parts`.

Acceptance:

- `validate_y dl-gc1` (target → npm build → core → full): target
  `tests/test_download_gc.py` → **9 passed** (stale part+meta removed, fresh kept,
  legacy download.*.part removed, non-.part artifacts untouched, TTL boundary via
  now injection, custom-TTL window, missing-dir safe, mixed counts, str path) →
  `npm run build` ✓ → core **102 passed** → **full 2509 passed, 2 skipped** (134s)
  → `[validate_y] success`. `py_compile` OK, lint clean; CRLF preserved.

Out of scope (deliberate, next slices):

- A scheduled / background GC daemon (current sweep is on-harvest only); GC of the
  content-addressed final artifacts themselves; cross-run global temp-dir GC;
  honoring a configurable retention policy from run constraints.


## Slice RUN-RESUME1 (step 5): fuzzy / semantic completed-step matching

Goal:

- Close step 3's "fuzzy / semantic matching of completed-step labels (currently
  exact normalized match)" gap (mission "类人工作模式"). On a resume the planner
  regenerates the TaskPlan, so a sub-goal's wording can drift from the recorded
  label (case, punctuation, full-width chars, spacing, a trailing clause). Exact
  match then fails to skip a genuinely-done step. Step 5 upgrades the skip matcher
  to a tolerant — but deliberately conservative — comparison. Backward compatible:
  exact matches behave exactly as before.

Add / change (run_resume_consume.py +51/-3, pure):

- `_norm2(value)` — aggressive normalize: NFKC (full-width → half-width),
  lowercase, punctuation stripped (keeps word chars incl. CJK), whitespace collapsed.
- `_fuzzy_match(a, b)` — tiered, deterministic: exact `_norm` equality (the step-3
  behaviour) → `_norm2` equality (case / full-width / punctuation insensitive) →
  whitespace-squashed equality (spacing insensitive) → squashed leading-prefix
  (`len >= 4`) so a re-worded / extended sub-goal ("搜索 python" vs "搜索 Python
  并点开") still resumes. Reordered or merely token-overlapping phrases never match.
- `apply_completed_steps_to_plan` now skips a leading sub-goal when it `_fuzzy_match`es
  *any* recorded completed step (was: exact-normalized set membership). All other
  guards unchanged (never skips the final sub-goal; self-healing re-observation).

Acceptance:

- `validate_y run-resume1-step5` (target → npm build → core → full): target
  `tests/test_run_resume_consume.py` + `tests/test_resume_run_integration.py` →
  **32 passed** (14 prior consume + 6 integration + 12 new: full-width/case,
  spacing, punctuation, leading-prefix skips; reordered + token-overlap rejected;
  helper exact/full-width/prefix/too-short/blank/unrelated) → `npm run build` ✓ →
  core **102 passed** → **full 2521 passed, 2 skipped** (134s) → `[validate_y]
  success`. `py_compile` OK, lint clean.

Out of scope (deliberate, next slices):

- Embedding / semantic-similarity matching (would add a model dependency — the
  squashed-prefix heuristic is pure stdlib); token-set / edit-distance fuzzy
  matching (rejected as too loose for a skip decision); matching non-leading
  (out-of-order) completed steps.


## Slice SEED-NEXT: URL seeder HEAD→GET fallback + content-type→output_kind routing

Goal:

- Close CRAWL-SEED3's "HEAD→GET fallback for servers that reject HEAD" +
  "content-type → output_kind routing" gaps (mission §一 "高效 / 准确"; §一-A output
  contract). Some servers answer HEAD with 405 / 501, so a HEAD-only liveness probe
  wrongly drops a live URL; and a probe that knows the content-type can already hint
  the output_kind the run will produce. Both additive; the default path is unchanged.

Add / change (url_seeder.py +110/-17, pure / stdlib):

- `default_head_fetch` refactored over a shared `_urllib_probe(method=...)` helper;
  when the HEAD status is 405 / 501 (`_HEAD_REJECTED_STATUSES`) it retries with a
  `Range: bytes=0-0` GET so liveness + content-type resolve without downloading the
  body. 200 / 404 / network-0 paths are byte-identical to before.
- `content_type_to_output_kind(content_type)` — pure, tolerant (strips `;charset`,
  lowercases): `image|video|audio/* -> media_*`; pdf / zip / 7z / rar / gzip / tar ->
  media_pdf / media_archive; csv / xls(x) -> dataset_rows; json / jsonl / ndjson ->
  dataset_records; html / xhtml -> html_snapshot; other text -> code_or_text; else
  file_generic; "" -> "".
- `UrlSeeder.probe_url` now also returns `output_kind` (inferred from the
  content-type), so a liveness probe doubles as an output_contract hint.

Acceptance:

- `validate_y seed-next` (target → npm build → core → full): target
  `tests/test_url_seeder_probe.py` → **22 passed** (11 prior + 11 new: HEAD 405/501
  → GET fallback, Range header sent, 200 no-fallback; content_type mapping across
  media / docs / data / charset / text / unknown / empty; probe_url surfaces
  output_kind) → `npm run build` ✓ → core **102 passed** → **full 2532 passed, 2
  skipped** (133s) → `[validate_y] success`. `py_compile` OK, lint clean. (probe_url
  now carries an extra `output_kind` field — the one exact-match probe test updated.)

Out of scope (deliberate, next slices):

- GET-fallback on other reject codes (403 / 400 are ambiguous — auth vs method);
  parallel probing; last-modified / size metadata scoring; wiring probe_url's
  output_kind into the run's output_contract resolver.


## Slice SEED-PARALLEL: URL seeder batch + parallel HEAD probing

Goal:

- Close SEED-NEXT's "parallel probing" gap (mission §一 "高效 — 最少回合"). `live_only`
  seeding probed one URL at a time, so a sitemap with N candidates paid N sequential
  HEAD round-trips. Batch the probes and run them concurrently while keeping results
  deterministic. Additive + opt-in; the default crawl path (no `live_only`) is unchanged.

Add / change (url_seeder.py +44/-2, pure / stdlib):

- `UrlSeeder.probe_urls(urls, *, concurrency=8)` — batch wrapper over `probe_url`.
  `concurrency <= 1` probes serially; otherwise a `ThreadPoolExecutor` of up to
  `concurrency` workers (capped at `_MAX_PROBE_CONCURRENCY=32` and the URL count) runs
  the HEAD probes. **Output order always matches input order** (results mapped back by
  index via `as_completed`, not completion order); a per-URL failure is swallowed (that
  URL reported not-live via a pre-seeded placeholder) so one dead probe never aborts the
  batch.
- `seed_from_sitemap` / `seed_from_robots` gain `probe_concurrency=8`; the `live_only`
  filter now calls `probe_urls(pages, concurrency=probe_concurrency)` and zips metas back
  to pages instead of a serial `probe_url` comprehension. Filtering result is identical
  to the serial path (same order, same drops) — only execution is parallelized.

Acceptance:

- `validate_y seed-parallel` (target → npm build → core → full): target
  `tests/test_url_seeder_probe.py` → **30 passed** (22 prior + 8 new: probe_urls empty /
  serial-order / concurrency<1-serial / parallel-order-preserving / barrier-proves-real-
  parallelism / error-swallowing; sitemap + robots `live_only` parallel drop-dead) →
  `npm run build` ✓ → core **102 passed** → **full 2540 passed, 2 skipped** (135s) →
  `[validate_y] success`. ReadLints clean.

Out of scope (deliberate, next slices):

- async/await probing (stays threaded to match the sync `Fetcher` contract); per-host
  rate limiting / politeness delay between probes; retry-with-backoff on transient 5xx;
  surfacing probe metadata (output_kind / content-type) into the seeded frontier scoring.


## Slice E1C-3B: cross-system physical session switch (acquire -> launch -> rebind -> release)

Goal:

- Mission §一-B cross_system: when a run hops between systems (workflow_graph
  `cross_system` risk_flag) the reactive loop should really switch the active
  BrowserEnv to the target system's isolated context, not just navigate within one
  shared browser. Build the substrate + a flag-gated physical swap on top of the
  E1c-1/2/3a SessionRouter, fully additive (`VSPIDER_CROSS_SYSTEM_SWITCH` default
  off -> byte-identical single-system behaviour).

Add / change (all behind the flag; 6 commits fe9ad79..2064766):

- 3b-1 `SessionRouter.acquire_for_switch` (session_router.py): pool-acquire the target
  session (idempotent per run/system/auth) + stage the target-domain storage_state
  subset; directive enriched with session_id / staged / cookie_count.
- 3b-2 reactive-loop wiring (main.py hot-loop hook + finally): snapshot live
  storage_state, acquire_for_switch, emit `session_switch` + a `confirm_active` readback
  `session_switch_verified`; flag-gated `release_all()` in the run finally (acquire never
  leaks).
- 3b-2a `BrowserEnv.start(user_data_dir_override=...)` + new pure `browser_profile.py`
  `resolve_user_data_dir`: per-system isolated Chromium profile so two persistent contexts
  never collide on launch_persistent_context's single-instance lock.
- 3b-2b `SessionRouter.launch_for_switch`: async-start the target session on its isolated
  profile, apply the system's storage_state subset, read back final_url + cookie/origin
  counts.
- 3b-2c `SessionRouter.activate_switch` + `_launched` guard + main.py rebind: a forward hop
  launches the target session once and rebinds the loop-local `browser` handle (closures
  follow via Python cell capture -- verified, no value-capture sites); a hop back to the
  home system rebinds to the primary lease; emit `session_switch_activated`.

Acceptance:

- Per-slice TDD (red -> green) + `validate_y E1c-3b-{1,2,2a,2b,2c}` each four gates green
  (target -> npm build -> core -> full). Router suite 21 -> 38; new pure browser_profile
  suite 5. Full suite 2588 -> 2610 passed, 2 skipped. ReadLints clean.

Out of scope (deliberate, next slices):

- Path-2 navigation-layer interception (intercept A->B nav so A's page is truly preserved,
  vs path-1's post-hoc rebind); switch-back restoring A's exact page state; profile-dir
  GC/TTL; folding `VSPIDER_CROSS_SYSTEM_SWITCH` + pool caps into a single
  `cross_system_config`; an `_env_flag` helper to de-dup the inline truthy env parse.


## Slice A1-1: navigation-layer cross-system goto interception (path-2, preserve A)

Goal:

- Mission §一 "准确": E1c-3b's path-1 physical switch is *post-hoc* -- GotoHandler
  runs `page.goto(url)` on the home page first (clobbering system A's page), then the
  reactive loop's `run_system_tracker.observe` detects the A->B hop and rebinds, so a
  switch *back* to A finds the home browser already navigated away. A1 moves the
  decision *before* navigation: when a goto targets a different planned system the home
  page is NOT navigated (its state is preserved) and the loop switches to B's isolated
  session instead. Fully additive behind `VSPIDER_CROSS_SYSTEM_SWITCH` (default off ->
  session_router never threaded -> byte-identical goto).

Add / change (3 layers, all flag-gated):

- session_router.py (pure: +`_host_of` / `system_for_url` / `plan_goto_interception`):
  `system_for_url` resolves a URL to a planned system by host / longest-domain match;
  `plan_goto_interception(target_url, current_system_id)` returns a directive whose
  `should_intercept` is True only for a *known* target system differing from a *known*
  current system (blank current = first nav establishing home, same-system, and
  unknown-domain all fall through to a normal goto). No pool / browser.
- actions.py: ActionContext gains a `session_router` field; GotoHandler, before its
  multi-tab interceptor, plans the interception and -- on should_intercept -- records
  `browser._pending_cross_system_goto`, pushes a `_tab_switch_notice`, and returns
  WITHOUT running `page.goto` (home page preserved).
- browser_env.py: threads `session_router=getattr(self,"_session_router",None)` into
  every ActionContext.
- main.py: attaches `_session_router` to the browser after start (flag-gated only); a
  new hot-loop consumer (after the observe block) drains `_pending_cross_system_goto`,
  reuses acquire_for_switch -> activate_switch -> rebind, lands the target browser on
  the URL, and primes the tracker so path-1 doesn't re-fire the same hop next step.

Acceptance:

- TDD red->green per layer. New pure suite `TestSessionRouterPlanGotoInterception` (8) +
  stub-frame `tests/test_goto_cross_system.py` (5: intercept-not-navigate; same-system /
  no-router / unknown-domain / blank-current all normal goto). `test_session_router.py`
  38 -> 46. py_compile + ReadLints clean on session_router / actions / browser_env / main.
- `validate_y a1-1` (target = both new files -> npm build -> core -> full): target ok ->
  `npm run build` ok -> core ok -> **full 2610 -> 2623 passed, 2 skipped** (140.69s) ->
  `[validate_y] success`.

Out of scope (deliberate, next slices):

- switch-back restoring A's exact page state; the first forward hop double-navigates
  (launch_for_switch's start + the consumer's goto) -- only the revisit path strictly
  needs the consumer goto; profile-dir GC/TTL; folding the flag + pool caps into
  `cross_system_config`; an `_env_flag` helper to de-dup the now-three inline truthy env
  parses; cross-system RPA replay of an intercepted goto.


## Slice A2: switch-back preserves the target system's exact page state

Goal:

- A1-1's reactive-loop consumer ALWAYS re-ran goto on the rebound target browser,
  which (a) on a switch-back to home re-navigated the home page A1 had deliberately
  preserved -- destroying its exact in-page state -- and (b) on a forward first hop
  double-navigated (launch_for_switch's start + the consumer's goto). A2 makes the
  re-navigation conditional so an already-there browser is left untouched. Still fully
  behind VSPIDER_CROSS_SYSTEM_SWITCH (default off -> consumer never runs).

Add / change:

- session_router.py (pure): `_norm_url` (drop fragment, strip trailing slash, keep
  query) + `should_renavigate(current_url, target_url)` -> False when the target
  browser is already on the URL (preserve exact state), True otherwise (blank target
  -> False; blank / unknown current -> True).
- main.py: the consumer's goto is now guarded by `should_renavigate`; when the nav is
  skipped it emits `session_switch_page_preserved` evidence instead of re-navigating.

Acceptance:

- TDD red->green. New pure suite `TestSessionRouterShouldRenavigate` (7: identical /
  trailing-slash / fragment -> no nav; different path / query -> nav; blank target ->
  no nav; blank current -> nav). `test_session_router.py` 46 -> 53.
- `validate_y a2` (target -> npm build -> core -> full): all four gates green, full
  2623 -> 2630 passed, 2 skipped. py_compile + ReadLints clean.

Out of scope (deliberate, next slices):

- restoring scroll / form / JS state across a genuine reload when the URL actually
  changes; profile-dir GC/TTL; folding the flag + pool caps into `cross_system_config`;
  an `_env_flag` helper to de-dup the three inline truthy env parses.


## Slice PROFILE-GC: TTL garbage-collection of cross-system isolated profile dirs

Goal:

- E1c-3b launches each non-home system on its own Chromium profile at
  `<BROWSER_USER_DATA_DIR>/sys_<system_id>` (so persistent contexts don't collide on
  the single-instance lock). Across many runs these `sys_*` dirs accumulate on disk
  unbounded (mission §一 "简便" / governance, mirroring §一-B temp_uploads TTL+GC).
  Sweep stale ones before a run launches new ones. Behind VSPIDER_CROSS_SYSTEM_SWITCH
  (default off -> never runs).

Add / change:

- browser_profile.py: pure `select_stale_profile_dirs(entries, *, ttl_seconds, now,
  keep=())` picks the `sys_*` dirs older than the TTL and not in `keep` (only `sys_*`
  are ever eligible; the primary profile and non-sys entries are untouchable; ttl<=0
  disables; input order kept). Bounded IO `gc_profile_dirs(base_dir, *, ttl_seconds,
  now=None, keep=(), dry_run=False)` lists immediate sub-dirs, delegates selection to
  the pure fn, and shutil.rmtree's the stale ones best-effort (per-dir failure
  swallowed). Blank / missing base -> no-op.
- main.py: at run start (flag-gated), GC `<BROWSER_USER_DATA_DIR>` with the TTL from
  VSPIDER_PROFILE_TTL_HOURS (default 24h) and emit a `profile_gc` event when any dir
  is removed. Active profiles keep a fresh mtime so a concurrent run survives the cut.

Acceptance:

- TDD red->green. test_browser_profile.py 5 -> 15 (6 pure select: stale / fresh /
  non-sys / keep / ttl<=0 / order; 4 IO gc via tmp_path: removes-only-stale-sys /
  dry-run / keep / blank-or-missing-base). `validate_y profile-gc` four gates green,
  full 2630 -> 2640 passed, 2 skipped. py_compile + ReadLints clean.

Out of scope (deliberate, next slices):

- size-based caps (only mtime TTL here); GC of the primary profile; a background
  sweeper thread (runs once at start); per-system keep-set wiring from the live pool;
  folding the flag + TTL + pool caps into `cross_system_config`; the `_env_flag` helper.


## Slice CROSS-SYSTEM-CONFIG: fold the inline cross-system env parses into one entry

Goal:

- Mission §四 contracts / §一-B cross_system: the cross-system feature's env knobs were
  parsed inline and duplicated -- five copies of
  `os.getenv("VSPIDER_CROSS_SYSTEM_SWITCH","").strip().lower() in ("1","true","yes","on")`
  plus an inline `VSPIDER_PROFILE_TTL_HOURS` float-parse in `main.py`, and two clamped
  `_env_int` pool-cap reads in `browser_session_pool`. Fold them into one pure leaf
  module so every gate reads the same rule. Pure refactor: the default-off path stays
  byte-identical (every accessor is a live env read, same timing as the old inline parse).

Add / change:

- cross_system_config.py (new, pure leaf -- imports only stdlib): generic helpers
  `env_flag` (verbatim of the truthy parse) / `env_int` (verbatim of pool `_env_int`) /
  `env_float` (verbatim of the TTL parse); named accessors `cross_system_enabled()` /
  `profile_ttl_hours()` / `session_pool_caps()`; immutable `CrossSystemConfig.from_env()`
  snapshot for callers wanting one object.
- main.py: the five flag gates -> `cross_system_enabled()`; the GC TTL parse ->
  `profile_ttl_hours()` (helpers imported once at run-function scope, before all gates).
- browser_session_pool.py: `_resolve_default_pool` -> `session_pool_caps()`; `_env_int`
  kept as a thin alias delegating to `cross_system_config.env_int` (single source of
  truth, signature preserved for any caller/test).

Acceptance:

- TDD red->green. New `tests/test_cross_system_config.py` (38: env_flag truthy/blank/
  case/non-truthy; env_int clamp hi/lo/garbage/in-range; env_float zero-kept/blank/
  garbage; named accessors + caps clamp; CrossSystemConfig from_env defaults/overrides/
  frozen).
- `validate_y cross-system-config` four gates green: target 53 -> npm build -> core 102
  -> full 2640 -> 2678 passed, 2 skipped (142.50s). py_compile + ReadLints clean on
  cross_system_config / main / browser_session_pool.

Out of scope (deliberate, next slices):

- a frozen run-scoped snapshot threaded through the loop (accessors stay live-read to
  preserve byte-identical timing); migrating non-cross-system env reads to the same
  helper; cross-system RPA replay.


## Slice RPA-XSYS: cross-system RPA replay (system-aware fast-path replay)

Goal:

- Mission §一-B cross_system / §二 execution_kernel: the RPA fast-path replay
  (`_replay_rpa`) drove every cached step on a single `browser._page`, so a trail
  spanning >1 planned system (recorded after A1/A2 cross-system goto) replayed the
  B-system steps on A's page and failed. Make replay system-aware: tag each cached
  step with the planned system it ran in, and switch the active browser to a step's
  system before replaying it -- reusing the same router acquire->activate->rebind path
  the reactive loop already uses. Flag-gated on the run's SessionRouter (attached only
  when VSPIDER_CROSS_SYSTEM_SWITCH is on) -> byte-identical when off.

Add / change:

- actions.py `ActionContext.with_rpa_meta`: when a `session_router` is attached, stamp
  `step["system_id"] = session_router.system_for_url(browser.current_url)` (setdefault,
  never clobbers an override). Router absent / unplanned URL -> no key.
- main.py `_compact_rpa_trail._same_step`: also compare `system_id`, so two identical
  actions in different systems are NOT merged (a merge would drop a system hop).
- main.py `_replay_switch_system` (new async helper): snapshot storage_state, ask the
  router for a switch directive (`acquire_for_switch`), `activate_switch` (launch/rebind
  the target system's isolated pooled session), land on the hop URL only if
  `should_renavigate` (A2 page-state preserve). Best-effort: any failure returns the
  inputs unchanged so replay proceeds on the current browser.
- main.py `_replay_rpa`: new `session_router=None` param (auto-threaded off
  `browser._session_router` when omitted -> no hot call-site edits); track the active
  system; before each step whose `system_id` differs, call `_replay_switch_system` and
  rebind the local `browser`. All inert when router is None.

Acceptance:

- TDD red->green. New `tests/test_rpa_cross_system_replay.py` (8: with_rpa_meta stamp /
  no-router / unplanned-url + override; compact keeps-cross-system / merges-same-system;
  replay switches-at-boundary / no-switch-same-system / no-router-never-switches).
- `validate_y rpa-xsys` four gates green: target 8 -> npm build -> core 102 -> full
  2678 -> 2686 passed, 2 skipped (138.55s). py_compile + ReadLints clean on main /
  actions / the new test.

Out of scope (deliberate, next slices):

- re-emitting `session_switch` events into the run event_stream during replay (the
  helper logs only); partial-replay (`_replay_ready_rpa_steps`) system-boundary cursor
  accounting; per-system workflow_memory isolation during replay.


## Slice SSRF-GUARD: server-side outbound URL guard (SSRF defense across all fetchers)

Reference: mission §一 准确/通用 (one guard, every fetch site); web SSRF (OWASP A10).

Goal:

- VSpider fetches user / sitemap / API-supplied URLs server-side (`spider_lite`,
  `url_seeder`, `media_harvester`, `api_replay`, `fetch_articles`, `main.py` RPA/Sheet
  fetchers). Without a guard a crafted URL can point the server at internal-only targets
  -- loopback `127.0.0.1`, RFC1918 (`10/8`, `172.16/12`, `192.168/16`), link-local
  `169.254.0.0/16` (cloud-metadata `169.254.169.254`), IPv6 `::1` / `fc00::/7` -- or a
  non-HTTP scheme (`file://`, `gopher://`, `data:`). Centralise one SSRF guard and route
  every server-side fetch (and every redirect hop) through it.

Add / change:

- `url_guard.py` (new, pure leaf -- stdlib only): `check_url(url)` raises `UrlGuardError`
  / `is_url_allowed(url)` -> bool. Policy (secure-by-default, offline-safe): scheme must
  be http/https; literal private/loopback/link-local/reserved/multicast/unspecified IPs
  rejected with no DNS; static hostname blocklist (`localhost`, `*.localhost`, cloud-
  metadata names); optional DNS resolution (injectable `resolver` / `VSPIDER_SSRF_RESOLVE_DNS=1`)
  rejects a public name that resolves to a private address (DNS-rebinding). Escape hatch
  `VSPIDER_ALLOW_PRIVATE_URLS=1` for local dev (the metadata endpoint stays blocked even
  then). `_GuardedRedirectHandler` + `build_guarded_opener` re-check every urllib 3xx hop;
  `guard_httpx_request` is an httpx `request` event-hook that checks every hop.
- Wiring landed across four linear commits: `spider_lite` / `url_seeder` /
  `media_harvester` (SSRF-GUARD-1); `api_replay` & `fetch_articles` + harden `url_guard`
  (SSRF-GUARD-2); redirect-hop guard into all auto-following fetchers (SSRF-GUARD-3);
  `main.py` RPA/Sheet fetchers + `url_seeder` probe (SSRF-GUARD-4).

Acceptance:

- Targeted: `test_url_guard.py` + `test_url_guard_wiring.py` + `test_url_guard_redirect.py`
  = 70 passed. Full suite 2785 passed, 2 skipped, 1 deselected (135.73s) -- green
  (exit 0).
- Code-health / multi-agent pollution audit clean: a single `url_guard.py` module, the
  guard API defined exactly once, all six fetcher call-sites import the one module
  (no self-rolled guard), no merge-conflict markers, four linear commits SSRF-GUARD-1..4
  on `feat/web-extraction-suite`, no cross-branch divergence (`HEAD..master=0`), clean
  working tree.

Out of scope (deliberate, next slices):

- DNS resolution on by default (kept opt-in so the offline test-suite stays hermetic);
  a per-call allow-list of sanctioned internal hosts; an SSRF guard for browser-context
  navigations (this slice covers the server-side python fetchers, not Playwright
  `page.goto`); rate-limit / response size-cap on guarded fetches.

## STEALTH-1: UA <-> Client Hints consistency (Cloudflare anti-bot P0)

Why: the launch UA was hard-coded to `Chrome/124` while the real Playwright
Chromium build drifts ahead, and only the `User-Agent` header was spoofed -- the
low-entropy Client Hints (`Sec-CH-UA` / `Sec-CH-UA-Platform`) the browser
auto-sends still carried the real version/OS. Cloudflare cross-checks UA against
Client Hints, so the mismatch was itself a bot signal.

Add / change:

- `stealth_profile.py` (new, pure leaf + thin probe): `detect_chromium_major`
  shells out to `<chromium> --version` once (always falls back to
  `DEFAULT_CHROME_MAJOR`); `build_user_agent` / `build_sec_ch_ua` /
  `build_client_hints` / `build_profile` emit a UA whose Chrome major + platform
  match the `Sec-CH-UA` brand list and `Sec-CH-UA-Platform` exactly.
- `browser_env.py` wiring: derive `_STEALTH_UA` from the probed real Chromium
  version and call `context.set_extra_http_headers(profile.client_hints)` right
  after the stealth init script, so UA + hints agree at the context level.

Acceptance:

- Targeted `test_stealth_profile.py` = 15 passed. Full suite 2873 passed,
  2 skipped (139.47s) via `validate_y.py STEALTH-1 --skip-build` (frontend deps
  not installed; backend-only change). `browser_env` import smoke green.

Out of scope (next slices): P1 passive-wait -> `cf_clearance`/redirect-cookie
poll in `bot_challenge_guard.py`; P2 remove the hand-written `navigator.plugins`
patch that may double-patch with `playwright_stealth`; CDP
`Emulation.setUserAgentOverride` with full `userAgentMetadata` (fullVersionList).

## STEALTH-2: passive-wait early release on cf_clearance / URL move (Cloudflare P1)

Why: Phase 1 of `handle_bot_challenge_step` only re-ran the DOM probe and always
burned up to `passive_wait_seconds` (15s) before escalating. It missed the
earliest reliable pass signal -- the `cf_clearance` cookie Cloudflare sets the
moment a challenge clears -- and the address bar leaving the interstitial.

Add / change:

- `bot_challenge_guard.py`: new pure leaves `clearance_from_cookies(cookies)` and
  `url_left_challenge(current_url, original_url)` (the latter guards against the
  same-URL Turnstile widget so a bare URL is not mistaken for a pass). New async
  helpers `_read_cookies` (getattr-guarded `context.cookies()`) and
  `_detect_clearance_signal`. The Phase 1 loop now releases on either signal
  before the DOM probe, otherwise falls back to the existing probe-None check.
- Fully back-compatible: stub browsers without `_context`/`current_url` skip the
  new signals and keep the old behaviour.

Acceptance:

- Targeted `test_bot_challenge_guard.py` = 16 passed (7 existing + 9 new: pure
  signal functions + cookie/URL early-release stub flows). Full suite 2884
  passed, 2 skipped (138.06s) via `validate_y.py STEALTH-2 --skip-build`.

Out of scope (next): P2 remove the hand-written `navigator.plugins` double-patch;
CDP `setUserAgentOverride` userAgentMetadata; reroute-on-block tuning.

## Slice INTENT-OVERRIDE-1: 附件 intent 用户显式覆盖（前端选择器 → input_contract 落盘）

Why: 输入契约 §一-B 要求 `attachments[i].intent` "必推断 + 用户可覆盖"，但
`/api/start_batch` 从未暴露覆盖位——前端上传 PDF 想"作为上下文阅读"却被推断成
`upload_to_page` 时用户无路可走，违反"禁止把上传文件只当批处理 DataFrame"约束。

Add / change:

- `vspider-ui/composables/useAttachmentIntent.js`（新）: auto + 4 个用户可选
  intent（batch_rows/upload_to_page/prompt_context/media_source；unknown 仅属
  推断域）；`appendAttachmentIntentToFormData` 仅在真实覆盖时附加字段。
  `App.vue` 上传区加 el-select（选文件后出现，移除/换文件重置 auto）。
- `api_server.py /api/start_batch`: 新 Form 字段 `attachment_intent`
  （空/auto=自动推断，非法值快速失败）；响应新增 `attachment_intent_source`
  （user|inferred）；用户覆盖随 queue item（`_enqueue_task`）、worker
  （`_run_batch_kwargs`）、retry（从 input_contract.json 的 intent 复现）、
  recover（snapshot 白名单）全链路透传。
- `smart_batch_runner.py`: `run_smart_batch(_sync)` 新 kwarg
  `attachment_intent` → `_persist_io_contracts_safe`（intent 落盘进
  input_contract.json）+ `_dispatch_attachment(intent_override=)`（覆盖优先于
  推断，路由 adapter 按用户意图走）。
- `io_contract/__init__.py` 导出 `ATTACHMENT_INTENTS`；`queue_state.py`
  `_execution_task` 白名单补 `attachment_intent`（字段只加不删）。

Acceptance:

- 新 `tests/test_attachment_intent_override.py` 12 passed：dispatcher 覆盖优先/
  非法回退、契约落盘、API 校验 + 入队 + auto 推断回显、_run_batch_task 转发、
  retry/recover 保持、source wiring。前端 `useAttachmentIntent.test.js` 8 passed
  （vitest 共 17）。`validate_y.py attachment_intent_override` 全链路 success：
  target 161 + build + core + full 2920 passed, 2 skipped (141.81s)。
- 测试坑：TestClient POST `/api/start_batch` 会真启 `_queue_worker` 循环且
  TestClient 等 background task 完成 → 永久挂起；用例需预置 fake idle worker
  （`_block_worker_autostart`）。直调 `start_batch()` 时 Form 默认值是 `Form`
  对象，已加 `isinstance(attachment_intent, str)` 防御（对齐 constraints/urls）。

## STEALTH-3: CDP setUserAgentOverride with full userAgentMetadata (Cloudflare P2)

Why: header injection (STEALTH-1) fixes the HTTP-side Sec-CH-UA but never
populates the JS-side `navigator.userAgentData`, and workers / cross-origin
iframes issue requests outside `set_extra_http_headers`. A spoofed UA with
empty or mismatched high-entropy hints (platform/platformVersion/bitness/
fullVersionList) is itself a bot tell for Cloudflare's JS challenge.

Add / change:

- `stealth_profile.py`: pure leaves `_navigator_platform` (Win32 / MacIntel /
  Linux x86_64), `_platform_version`, `build_ua_metadata` (brands = major-only,
  fullVersionList = reduced `{major}.0.0.0` so no real build leaks) and
  `build_cdp_ua_override(profile)` (acceptLanguage only when explicitly given,
  so the context locale is never clobbered by default).
- `browser_env.py` wiring: `self._stealth_profile` stashed in `start()`;
  `_register_page` awaits new `_apply_cdp_ua_override(page)` once per page
  (initial + tabs + popups), driving `Emulation.setUserAgentOverride` over a
  fresh CDP session. Best-effort try/except so registration never breaks.

Acceptance:

- Targeted `test_stealth_profile.py` (28) + new `test_stealth3_cdp_wiring.py`
  (3 stub-frame: override params identity / missing-profile no-op / CDP failure
  swallowed) = 31 passed. Core 4 suites passed. Full suite 2919 passed,
  2 skipped, 1 failed via `validate_y.py STEALTH-3 --skip-build` -- the single
  failure (`test_task_queue.py::test_start_batch_queues_when_current_task_is_running`,
  AttributeError at `api_server.py:3175 attachment_intent.strip`) comes from
  another agent's uncommitted attachment-intent WIP in `api_server.py`,
  untouched by this slice (stealth files do not import that path).

Out of scope (next): reroute-on-block tuning; optional acceptLanguage
alignment with context locale once a locale policy exists.

## Slice EXTRACT-COMPLEX-1: complex-structure extraction hardening (done)

Layer: data_plane (extraction_engine/generic.py) + intent_planning (semantic_macros/cascader_pick.py).
Driven by a 24-scenario capability probe (2026-06-10): 9 GAPs found, all fixed.

- _TableParser rewritten: per-<table> grid stack (nested tables no longer
  shred outer rows) + rowspan/colspan occupancy expansion (span clamped at 100).
- Header pipeline: thead/all-th rows are trusted signals; stacked header rows
  merge leaf-first; duplicate names get _2 suffixes; headerless tables only
  promote row0 when a column flips text->numeric (data tables keep all rows).
- _extract_json_rows: dict-quality weighting (x3) so record lists beat longer
  scalar noise lists (trace/tags).
- select() CSS: direct-child combinator > (spaced and compact forms).
- cascader_pick.parse: strips trailing CJK punctuation from path segments;
  new 级联选择： trigger (lookahead-guarded, math comparisons stay unhijacked).

Tests: tests/test_extract_complex_tables.py. Probe scripts:
.pttmp/probe_extract_complex.py, .pttmp/probe_fill_complex.py
(24/24 PASS post-fix).

Same-slice follow-up: extract_html_tables_all() + extract(all_tables=True)
+ additive table_count/tables result fields + /api/extractor/run
passthrough, so multi-table pages are no longer silently reduced to the
largest table (default pick unchanged for existing callers).

Known/accepted: mixed-arrow paths need a trigger keyword; data_manager
XHR legacy xlsx default is fixed by Slice XHR-INTERCEPT-CONTRACT-1 below.

## Slice XHR-INTERCEPT-CONTRACT-1: intercept track honours output_contract (done)

Layer: data_plane (data_manager.py) + browser_substrate (browser_env.py) + main wiring.
Closes the audit finding "XHR intercept still hard-codes xlsx" (mission 1-A iron rule).

- save_intercepted_data(..., output_contract=None): resolves the dataset
  container (xlsx/csv/jsonl) from the contract; suffix rewritten accordingly;
  manifest mime/extra follow the container. No contract -> legacy xlsx.
- Non-dataset containers (files_folder/html/...) fall back to jsonl so rows
  stay rows instead of masquerading as a spreadsheet.
- _save_dataframe_to_excel generalised with container-aware read/append/write
  (_read_existing_frame / _write_frame); dedup + tooltip upsert untouched.
- BrowserEnv: _intercept_output_contract + configure_interceptor(output_contract=)
  + set_interceptor_output_contract() (late refresh, keeps dedup state).
- main.py: passes _initial_output_contract at configure time and refreshes
  with _goal_output_contract right after goal-contract resolution.

Tests: tests/test_xhr_intercept_contract.py (13 cases: container resolution,
suffix rewrite, jsonl/csv append+dedup, policy fallback, wiring, dedup-state
preservation). Full suite 2951 passed / 2 skipped.

## Slice FORM-IFRAME-1: form_set reaches iframes + open shadow DOM (done)

Layer: operations_plane (actions.py form_set handler + binding JS).
Closes the audit finding "form_set/auto_form only evaluate the main document".

- Binding JS gains deepQueryAll(): querySelectorAll that descends open shadow
  roots; wired into allControls / allVisible / findLabelHits / label[for]
  resolution (root-aware getElementById + CSS.escape fallback) / marker sweep.
- _form_set_with_frames(): main document first; only when the main doc reports
  label_or_control_not_found does it probe every child iframe (detached and
  throwing frames skipped). Returns (result, scope) so opener clicks, dropdown
  option picks and the readback all run in the found frame's document.
- Frame-local click_point is never fed to page.mouse (viewport-coords mismatch);
  dropdown pick falls back to the top document for teleporting widget libraries.
- rpa_trail gains additive frame_url evidence field (empty for main document).

Tests: tests/test_form_set_iframe_shadow.py (10 cases: fallback ordering,
found-but-failed stays put, detached/raising frames, evidence field, shadow
anchors). Live-browser probe (shadow form + srcdoc iframe + ghost label)
10/10 PASS pre-removal.

Out of scope (next): auto_form batch macro in main.py (same gap, separate
slice); closed shadow roots (unreachable by design); cross-origin iframes
(Playwright frame.evaluate still works, but widget popups teleported to the
top document cannot be clicked from the frame scope alone).

## Slice FORM-IFRAME-2: auto_form macro reaches iframes + open shadow DOM (done)

Layer: intent_planning (main.py _try_auto_form_fill bound-controls path).
Closes the weakness-list item "auto_form batch macro: same gap as
FORM-IFRAME-1" by porting the proven pattern to the deterministic macro.

- Bound-controls JS gains deepQueryAll() (mirrors actions.py): allVisible now
  descends open shadow roots, so scope detection, control harvesting, label
  nodes and the submit-button sweep all reach web-component forms.
- _auto_form_fill_bound_controls_with_frames(): main document first; only a
  full miss (every requested field field_not_found/invalid_result) probes
  child iframes (detached/raising frames skipped). Partial hits and
  complex_component_requires_form_set_or_macro stay in the main document so
  the macro never guesses across frames. Both call sites (repeat loop +
  single pass) route through the wrapper; result carries additive frame_url.
- _auto_form_result_found_nothing(): explicit predicate so submit_not_found,
  unsupported_control and verification failures never trigger frame probing.

Tests: tests/test_auto_form_iframe_shadow.py (18 cases: fallback ordering,
partial-hit/complex-component stay-put, predicate edge cases incl.
submit_not_found, detached/raising frames, shadow + caller wiring anchors).

Out of scope (next): component-aware fallback JS in _try_auto_form_fill
(secondary path, light-DOM only); bulk table extraction iframe sweep;
virtual-scroll list capture; rich-text structured write.

## Slice EXTRACT-IFRAME-1: bulk table extraction sweeps child iframes (done)

Layer: data_plane (main.py run_agent DOM table harvest).
Closes the weakness-list item "bulk table extraction never scans iframes"
(admin consoles routinely render the data grid inside one).

- New module-level _evaluate_rows_with_frame_fallback(page, js, log_tag):
  evaluates row-harvesting JS in the main document first; on an empty/non-list
  result (or a crashing main document) probes every child iframe, skipping
  detached/raising frames, and returns the first non-empty list. Frame hits
  log the frame URL as evidence.
- _extract_visible_table_rows_via_dom now routes through the helper with its
  table JS hoisted to a local constant; scoring/header heuristics unchanged.

Tests: tests/test_extract_table_iframe.py (10 cases: fallback ordering,
main-document crash still probes frames, non-list normalisation,
detached/raising frames, run_agent wiring anchor).

Out of scope (next): list/card extraction iframe sweep; table autopager +
signature inside frames (pagination still main-document only); shadow-DOM
table hosts; virtual-scroll capture; rich-text structured write.

## Slice FORM-RICHTEXT-1: rich-text editors get structured writes (done)

Layer: operations_plane (actions.py form_set binding JS).
Closes the weakness-list item "rich-text editors only receive a flat
textContent write" (which desyncs Quill's Delta model, never reaches
TinyMCE, and drops all line structure).

- setRichTextValue() replaces the bare textContent branch in setNativeValue,
  with a strict priority order: Quill API (container.__quill / Quill.find ->
  setText) -> TinyMCE registry (tinymce.get / editors -> setContent + fire
  change) -> CKEditor 5 (ckeditorInstance.setData) -> generic real input
  chain (select-all + execCommand insertText, which fires the beforeinput
  chain ProseMirror/Slate/Lexical listen to) -> structured paragraph HTML
  (escaped, blank-line-separated <p> blocks with <br> line breaks).
- Multi-line values now keep paragraph/line structure in every fallback tier
  instead of collapsing into one flat text node.

Tests: tests/test_form_richtext_write.py (9 source anchors incl. fallback
priority order + Python-escaping guard for the JS regexes). Live-browser
probe (generic contenteditable / Quill API double / multiline) 3/3 PASS
pre-removal; the probe also caught a real regex-escaping bug before commit.

Out of scope (next): auto_form macro's contenteditable branch in main.py
(still flat textContent); TinyMCE classic iframe mode via editor API (the
frame sweep reaches the body and uses the generic chain instead);
virtual-scroll list capture.

## Slice EXTRACT-VSCROLL-1: virtual-scroll lists become harvestable (done)

Layer: data_plane / browser_substrate (new visual_web_agent/virtual_scroll.py
+ main.py dedup-nudge wiring).
Closes the weakness-list item "virtual-scroll list extraction is weak".

Root cause: windowed lists (react-window, vue-virtual-scroller, ag-grid,
Element Plus virtual table) keep a constant row window inside an inner
scroller - window.scrollBy never moves it and body innerText length stays
flat while rows are recycled, so _nudge_scroll_after_duplicate_extract
reported a dead end and the VLM extract->scroll loop terminated early.

- New module virtual_scroll.py (kept out of the oversized main.py):
  findScroller picks the largest visible overflow-y scrollable container
  (incl. .el-scrollbar__wrap / .ag-body-viewport / [class*=virtual] hosts);
  nudge_virtual_scroll() scrolls it one step (dispatching a real scroll
  event, which windowed renderers listen to) and reports progress via a
  first/last row-text signature diff plus moved/remaining evidence.
- _nudge_scroll_after_duplicate_extract falls back to the virtual-scroll
  nudge when the legacy window scroll yields no body growth; window-scroll
  behaviour for normal pages is unchanged.

Tests: tests/test_virtual_scroll_nudge.py (12 cases: progress/bottom/window
fallback, contained evaluate errors, non-dict normalisation, settle wait,
JS anchors, run_agent wiring). Live-browser probe with a true windowed list
(600 rows, recycled DOM): window-scroll no-op confirmed, capture loop got
600/600 unique rows in 71 passes, clean bottom detection - 4/4 PASS
pre-removal.

Out of scope (next): a dedicated deterministic capture-loop action that
accumulates rows across nudges in one shot (today the existing VLM
extract->scroll->dedup loop drives accumulation); horizontal virtual
scrollers; iframe-hosted virtual lists (combine with the frame sweep).

## Slice EXTRACT-IFRAME-2: list/card extraction sweeps child iframes (done)

Layer: data_plane (main.py list harvest + the shared frame-fallback helper).
Closes the P1 weakness "list/card extraction never scans iframes" (tables
got the sweep in EXTRACT-IFRAME-1; lists were still main-document only).

- _evaluate_rows_with_frame_fallback gains an optional payload_empty
  predicate so dict payloads ({rows, sourceText}) ride the same sweep;
  default behaviour (list payloads) is unchanged and the original 10 stub
  cases still pass untouched. A raising predicate degrades to "empty"
  instead of crashing the harvest.
- _extract_list_rows_via_dom hoists its JS to a local constant and routes
  through the helper with log_tag=EXTRACT DOM LIST; the >=2-rows acceptance
  rule and row normalisation stay outside the sweep, so a partial main-doc
  hit (1 row) never triggers cross-frame guessing.

Tests: tests/test_extract_table_iframe.py extended 10 -> 15 cases (dict
payload hit/fallback/all-empty/raising predicate + list wiring anchors).

Out of scope (next): _extract_body_text_for_semantic_cards (main-document
body text only - semantic cards inside iframes still get their rows but
lose the auxiliary body context); virtual-scroll capture fast-path.

## Slice EXTRACT-VSCROLL-2: deterministic one-shot virtual-list capture (done)

Layer: data_plane (virtual_scroll.py capture loop + main.py pre-extract).
Closes the P1 item "dedicated deterministic capture loop" from
EXTRACT-VSCROLL-1's out-of-scope list: bulk goals on virtualised lists used
to burn ~1 VLM round per viewport (extract -> scroll -> dedup); now a single
deterministic call drains the scroller before the planner ever runs.

- virtual_scroll.py gains VIRTUAL_LIST_ROWS_JS (rows inside the dominant
  scroller; nested wrapper rows skipped; cells split out for tr/[role=row])
  and capture_virtual_list_rows(): alternates snapshot + nudge, dedupes
  recycled rows by text, stops on bottom (complete=True), max_rows cap
  (complete=False) or max_passes; snapshot errors degrade per-pass.
- _try_pre_extract_fast_path runs the capture only when both static
  harvests (table + list) fall short of target_count AND the scroll-drain
  probe reports container_can_scroll; the result joins candidate selection
  as VSCROLL_LIST and rides the existing schema-filter/landing chain.

Tests: tests/test_virtual_scroll_nudge.py extended 12 -> 18 (capture
dedupe/cap/first-pass-exit/error-degrade, rows-JS anchors, pre-extract
wiring incl. the drain-probe gate). Live probe: 600-row windowed list
captured 600/600 in 71 passes complete=True; max_rows=50 cap returns 50
with complete=False - 2/2 PASS pre-removal. The probe also confirmed an
unscrollable container exits on pass 1 (no spin).

Out of scope (next): exposing the capture as a mid-run action for VLM use;
horizontal scrollers; iframe-hosted virtual lists (combine with the frame
sweep); semantic field mapping for captured text rows (rides DOM_CARDS
today only via candidate competition).

## Slice FORM-RICHTEXT-2: macro rich-text writes + fallback shadow reach (done)

Layer: intent_planning (main.py auto_form macro JS, both passes).
Closes two backlog items: the macro's contenteditable branch was a flat
textContent write, and the component-aware fallback JS was light-DOM only.

- Bound-controls JS gains the same setRichTextValue tier chain as actions.py
  (Quill API -> TinyMCE registry -> CKEditor 5 -> execCommand insertText ->
  escaped paragraph HTML); the contenteditable branch routes through it.
- Component-aware fallback's allVisible now rides deepQueryAll, so the
  secondary pass also reaches open shadow roots.
- Verify fix (found by the live probe): valueOf joined aria-label into the
  readback, so every labelled control failed verify ('macro quill text
  Notes' != 'macro quill text') and the macro declined to VLM despite
  writing correctly. contenteditable now reads innerText only; value/
  textContent are read directly; aria-label only remains as the empty-value
  fallback.

Tests: tests/test_auto_form_richtext.py (9 source anchors: tier order,
escaping guard, valueOf semantics, fallback shadow reach). Live probe:
macro filled a Quill double via the API + a plain contenteditable, and the
bound-controls pass reached an open-shadow input end-to-end - PASS
pre-removal (probe surfaced the valueOf bug before commit).

Out of scope (next): table autopager inside frames; field-name case
contract lock; TinyMCE classic iframe via editor API.

## Slice EXTRACT-IFRAME-3: table autopager sweeps scopes (done)

Layer: data_plane (main.py run_agent table autopager + signature helper).
Closes the P2 weakness "iframe tables can be harvested (EXTRACT-IFRAME-1)
but not paged": the autopager clicked and verified in the main document
only, so iframe-hosted multi-page tables yielded just their first page.

- _visible_table_signature(reason, scope=None): explicit scope (Page or
  Frame) bypasses the active-page lookup; legacy callers unchanged.
- _auto_advance_table_page_via_dom hoists the pager + wait JS and walks
  scopes = [page, *child frames] (detached skipped). Per scope: empty
  signature -> next scope (nothing clicked yet); pager not_found -> next
  scope; a real click -> verify in that same scope (wait_for_function +
  after-signature) and STOP - probing further scopes after a click risks
  double-paging. DataTables API / next-button / numeric-pager heuristics
  unchanged inside the JS.

Tests: tests/test_table_autopager_frames.py (8 source anchors: scope
parameter, hoisted JS, sweep order, empty-signature advance, post-click
termination, legacy main-only flow gone). Live probe: host page with the
paged table inside an iframe - main-doc signature empty / frame signature
ROW-1..3, Next only resolvable in the frame, frame-scoped wait saw page 2
(ROW-4..6) - 3/3 PASS pre-removal.

Out of scope (next): field-name case contract lock; cross-frame pagination
counters in tableInfo (dt-info) remain main-document in DATA_SIGNATURE_JS.

## Slice EXTRACT-FIELD-CASE-1: field-name case contract locked (done, no fix needed)

Layer: data_plane (page_data_controller + extraction_engine contract).
Investigates the functional-test observation "engine lowercases table
headers (Order -> order); downstream consumers must be case-tolerant".

Audit result: the schema-matching chain is ALREADY case-insensitive end to
end - normalize_field_key lowercases and strips separators
(page_data_controller.py:103), requested_field_coverage normalises both
row keys and requested fields through _field_aliases with bidirectional
substring matching (:156), and filter_undercomplete_rows inherits that
(:189). The functional-test miss was the test script reading dict keys
directly, bypassing the normalise layer - not a product defect.

Deliverable: tests/test_field_key_case_contract.py (13 cases) locks the
contract on both sides - normalize_field_key equivalences (case,
separators, CJK passthrough), capitalised-goal vs lowercase-engine-key
coverage in both directions, filter keep/drop stats, and the engine's
lowercase-header production feeding coverage end to end. A future
"optimisation" reintroducing case-sensitive matching now fails loudly.

Out of scope: documenting {{column}} placeholder case rules for batch
runner templates (input_contract layer, separate concern).

## Slice EXTRACT-VSCROLL-3: iframe-hosted virtual lists reach the capture (done)

Layer: data_plane (virtual_scroll.py scope sweep + main.py pre-extract gate).
Closes the P2 item "iframe-hosted virtual lists (combine with the frame
sweep)" from EXTRACT-VSCROLL-1/2: the pre-extract drain probe sees the main
document only, so a virtualised list inside an iframe never reached the
deterministic capture - bulk goals fell back to one VLM round per viewport.

- virtual_scroll.py gains find_virtual_list_scope(page, include_main=True):
  one lightweight VIRTUAL_LIST_SIGNATURE_JS evaluate per scope; a hit needs
  found=True plus scroll headroom (remaining > 0 - fully rendered lists are
  already covered by the static frame sweeps). Main document first
  (optional), then child frames in document order; detached and raising
  frames are skipped (EXTRACT-IFRAME sweep pattern). The returned scope
  feeds capture_virtual_list_rows as-is: Frames satisfy its
  evaluate/wait_for_timeout contract unchanged.
- main.py pre-extract: when the drain probe misses (no main-document
  scroller), sweep child frames via find_virtual_list_scope(...,
  include_main=False) and run the capture on the hit Frame. The
  main-document path (drain-probe gate -> capture on the page) is
  byte-for-byte unchanged.

Tests: tests/test_virtual_scroll_nudge.py extended 18 -> 26 (scope sweep:
main hit stops sweep, frame hit + url/remaining evidence, include_main=False
skips main, detached/raising skipped, zero-headroom miss, non-dict probe,
frame-scope capture; wiring anchors incl. the preserved drain gate). Live
probe: host page with no scrollable container + 600-row windowed list in an
iframe - main-doc probe miss / frame sweep hit (remaining=17520) / capture
600/600 unique in 44 passes complete=True - 3/3 PASS pre-removal.

Out of scope (next): exposing the capture as a mid-run action for VLM use;
horizontal scrollers; nested iframes hosting the scroller inside another
iframe ride page.frames already (flat enumeration) but were not live-probed;
semantic field mapping for captured text rows.

## Slice FORM-RICHTEXT-3: TinyMCE classic iframe writes (done)

Layer: intent_planning (main.py auto_form bound-controls macro JS).
Closes the FORM-RICHTEXT-2 out-of-scope item "TinyMCE classic iframe via
editor API": classic mode hides the textarea (display:none) and renders
into an <id>_ifr editor iframe, so the bound-controls pass saw no control
for the labelled field and the whole form fell through to the VLM.

- Binding: iframe[id$="_ifr"] / iframe.tox-edit-area__iframe join the
  bindable-control selector; a for= attribute that targets the hidden
  textarea falls back to the <forId>_ifr stand-in.
- Writing: the TinyMCE registry lookup strips the _ifr suffix to find the
  editor under its textarea id, and editor matching also accepts
  getContainer().contains(el) (getBody() lives in the iframe document, so
  cross-document contains() is always false). API-less same-origin editor
  iframes get structured paragraphs written into the body directly
  (iframe_structured_paragraphs - the main-document execCommand path cannot
  reach an iframe body), with input/change events dispatched.
- Readback: verify reads contentDocument.body.innerText for iframes; the
  write gate admits iframe controls.

Tests: tests/test_auto_form_richtext.py 9 -> 17 (binding selectors, for_attr
stand-in, registry suffix strip, container match, iframe branch ordered
ahead of exec_insert_text, setNativeValue routing, write gate, readback).
Live probe: real TinyMCE 6 classic via CDN - macro ok, iframe body and
editor.getContent({format:'text'}) both read the value back (registry API
path); offline simulated classic - for_attr stand-in binding, exactly one
setContent call (suffix strip), API-less iframe direct write with
insertText event + <p> paragraphs - 6/6 + 3/3 PASS pre-removal.

Out of scope (next): cross-origin editor iframes (no contentDocument access;
needs a frame-level form_set pass); TinyMCE inline mode already rides the
contenteditable branch; Froala/Summernote registry APIs.

## Slice VSCROLL-ACTION-1: mid-run deterministic virtual-list capture (done)

Layer: operations_plane (action_registry + capability_router + prompts) +
data_plane handler module. Closes the P3 item "exposing the capture as a
mid-run action for VLM use" from EXTRACT-VSCROLL-2/3: the deterministic
capture ran only in the pre-extract fast-path (before the planner starts),
so a virtualised list discovered mid-run - behind a login, a navigation, a
tab switch - still cost one VLM round per viewport.

- vscroll_capture_action.py (new module per workflow rule §三 - actions.py
  is oversized): @ActionRegistry.register("vscroll_capture") handler wires
  find_virtual_list_scope (main doc first, then same-origin child frames)
  into capture_virtual_list_rows; rows persist as a dataset_rows jsonl
  artifact + manifest entry (run-aware, skipped for ad-hoc calls) and land
  in workflow_memory (default key vscroll_rows) with row_count / complete /
  passes / container / where / frame_url evidence, stamped onto rpa_trail.
  type_value is an optional row cap (junk falls back to 2000, ceiling
  20000); no-hit and zero-row captures raise ActionExecutionError so the
  VLM reroutes instead of retrying.
- Six-step wiring: VSpiderAction literal (vlm_client), ActionTool with
  zh/en aliases + evidence (action_registry), _VSCROLL_RE signal
  vscroll_capture_preferred + plan step (capability_router), trigger
  tuple + skill block injection (prompts / prompt_skills), import hook in
  actions.py triggering registration.

Tests: tests/test_vscroll_capture_action.py (21: schema literal, handler +
tool registration with goal scoring, router signal/plan 中英文 + unrelated
guard, prompt-skill registration/injection guard, handler stubs: main-doc
end-to-end with trail evidence, frame fallback with frame_url, no-hit and
empty-capture errors, type_value cap + junk fallback, jsonl persistence
under an active run). Live probe: true windowed list (600 rows, recycled
DOM) - 600/600 in 53 passes complete=True, jsonl artifact 600 lines, row
order intact; plain page raised; type_value=50 capped with complete=False -
9/9 PASS pre-removal.

Out of scope (next): horizontal scrollers; semantic field mapping for
captured text rows; an agent_case probe site exercising the mid-run call
(pre-extract path already live-probed in EXTRACT-VSCROLL-2/3).

## Slice DATA-SIG-FRAME-1: next_page sees iframe-hosted DataTables (done)

Layer: operations_plane (actions.py NextPageHandler). Closes the P3 item
"DATA_SIGNATURE_JS dt-info paging counter across frames": every next_page
layer ran in the main document only, so an iframe-hosted DataTables widget
was triple-blind - the L-1/L3.5 probes never found its Next button, the
data signature never saw its dt-info counter, and an iframe-internal page
flip (main url/text unchanged) was always judged "page did not change".

- _page_data_signature: when the main document shows no table evidence
  (no tableRows, tablePageTotal=None), sweep child frames with the same
  DATA_SIGNATURE_JS and merge the first table-bearing frame's fields
  (tableInfo / tablePage* / tableRows; rowSignature when non-empty) into
  the main signature plus a tableFrameUrl evidence key. Main url/bodyText
  stay authoritative; main-table pages short-circuit (frames not probed).
- _wait_for_pagination_change: an unchanged main shell now consults
  pagination_moved(before_data, after_data) before failing, so the merged
  dt-info window move (1-10 -> 11-20) or frame row-signature change
  confirms the flip.
- _js_mark_pagination_candidate_with_frames: probe the main document
  first, then non-detached child frames (probe JS is evaluate-only, so
  Frames satisfy its contract); both the L-1 and L3.5 layers ride the
  sweep and click the marked candidate in the hit scope, stamping
  frame=<url> into the strategy trail.

Tests: tests/test_next_page_frame_signature.py (12: main-table
short-circuit, frame merge fields + authority, raising/detached skip,
no-table passthrough, unchanged-shell pagination via merged window move,
static-data still fails, probe sweep main-hit/frame-hit/all-miss, wiring
anchors locking both call sites + the data-consult branch order). Live
probe: host shell + srcdoc iframe with a DataTables-style pager - probe
clicked Next inside the frame (strategy frame='about:srcdoc'), dt-info
1-10 -> 11-20 -> 21-30 over two next_page calls with the host shell
byte-identical - 4/4 PASS pre-removal.

Out of scope (next): CSS/text/role locator layers (L1-L3) stay
main-document (wide-net selectors risk cross-frame misclicks; the scored
probe covers component-library pagers inside frames); URL mutation is
meaningless for iframe-internal paging; nested iframes ride page.frames
flat enumeration but were not live-probed.

## Slice VSCROLL-H-1: horizontal virtual card strips become harvestable (done)

Layer: data_plane (virtual_scroll.py shared scroller finder + axis
threading). Closes the P3 item "horizontal scrollers" from
EXTRACT-VSCROLL-2/3: the shared findScroller only accepted overflow-y
containers with vertical headroom, so a horizontally virtualised card
strip / film-strip (recycled DOM, scrollLeft windowing) was invisible to
the signature probe, the nudge, and the capture - per-viewport VLM rounds
again.

- _FIND_SCROLLER_JS_SNIPPET: scrollableRect scores both axes - vertical
  keeps the legacy gate (w>=160, h>=120, overflow-y + scrollHeight
  headroom), horizontal accepts shorter-but-wide boxes (w>=240, h>=80,
  overflow-x + scrollWidth headroom). findScroller returns {el, axis};
  vertical hits always outrank horizontal ones, so any page that used to
  pick a vertical scroller still picks it (legacy priority preserved).
  carousel/strip class hints join the candidate selector.
- All three consumer scripts ride the axis: SIGNATURE reports
  axis + scrollLeft-based remaining, NUDGE steps scrollLeft by 0.85x
  clientWidth on x-hits (window fallback stays vertical), ROWS_JS adds
  [class*="card"] to the row selector and stamps axis into the snapshot.
- Python threading (all additive): the scope probe, nudge_virtual_scroll,
  capture_virtual_list_rows, and the vscroll_capture action evidence/
  rpa_trail all carry axis ('y' default), so artifacts and the VLM can
  tell which direction was drained.

Tests: tests/test_virtual_scroll_nudge.py 26 -> 34 (axis anchors in all
three scripts, vertical-outranks-horizontal anchor, scrollLeft step
anchor, card selector, nudge axis propagation + vertical default, capture
meta axis, scope probe axis); test_vscroll_capture_action.py rides along
(21 green, evidence gains axis). Live probe: 60-card horizontal windowed
strip - 60/60 in 14 passes complete=True axis=x, card order intact;
vertical 600/600 regression intact (axis=y); page with both axes picks
the vertical scroller - 8/8 PASS pre-removal.

Out of scope (next): column-virtualised wide grids (ag-grid column
virtualisation needs per-row horizontal stitching, a different harvest
semantic); bidirectional grids (x+y virtualised simultaneously); RTL
strips (scrollLeft sign conventions differ per engine).

## Slice VSCROLL-FIELDS-1: captured rows map onto named header fields (done)

Layer: data_plane (virtual_scroll.py header harvest + pure mapper, wired
into the action handler and the pre-extract candidate). Closes the P3
item "semantic field mapping for captured text rows": the capture emitted
anonymous {text, cells} rows, so dataset_rows artifacts and the candidate
ranking saw nameless columns even when the table had a perfectly good
header row.

- VIRTUAL_LIST_ROWS_JS: harvests column headers near the scroller -
  headers usually live OUTSIDE it (sticky thead / sibling header wrapper),
  so the lookup walks up to the closest table/.el-table/.ant-table/
  .n-data-table/.ag-root/[role=grid|table|treegrid] container and tries
  thead th|td, then [role=columnheader], then .ag-header-cell-text
  (capped at 40). capture_virtual_list_rows keeps the first non-empty
  headers list and reports it in the result (additive).
- map_captured_rows_to_fields(rows, headers): pure, dependency-free
  positional zip - duplicate header names get _N suffixes, blank/overflow
  positions fall back to col_N, cell-less rows keep their {text} form,
  junk rows are skipped.
- Consumers: vscroll_capture persists/exposes mapped rows (memory rows +
  jsonl artifact are named dicts; headers + header_count join the
  evidence/trail); the pre-extract VSCROLL_LIST candidate maps before
  ranking, while source_text keeps the raw text lines.

Tests: tests/test_virtual_scroll_nudge.py 34 -> 44 (header-selector
anchors, first-non-empty header carry + empty default, zip/overflow/
duplicate/blank/text-only/junk mapper table, pre-extract wiring anchor);
test_vscroll_capture_action.py 21 -> 22 (named-field end-to-end with
header_count trail evidence). Live probe: virtual table with sticky thead
outside the scroller - headers [Name, Office, Salary], 200/200 rows, memory
and jsonl rows are named dicts; headerless card list regression keeps
{text} rows - 8/8 PASS pre-removal.

Out of scope (next): alias-matching harvested headers onto the user's
requested fields (planner-side field coverage already normalises);
header harvest for horizontal strips (cards rarely have column headers);
colspan/rowspan header grids.

## Slice EXTRACT-SHADOW-1: bulk table harvest reaches open shadow roots (done)

Layer: data_plane (main.py pre-extract DOM_TABLE harvest + autopager table
signature). Closes the P3 item "shadow tables": component-library tables
(Lit / Stencil / vanilla custom elements) render inside open shadow roots,
which plain document.querySelectorAll('table') never sees - the DOM_TABLE
candidate came back empty and the whole page fell through to screenshot
extraction; the autopager's signature verification was blind on the same
pages.

- _table_rows_js: gains the deepQueryAll walker (same recursion the form
  fallback already ships in actions.deepQueryAll) and harvests tables via
  deepQueryAll('table'); header selection, row parsing, and scoring are
  untouched - light-DOM tables produce byte-identical results.
- _visible_table_signature: same walker, so autopager verification sees
  the same table the harvest extracted (paging a shadow table now has a
  verifiable signature).
- Both scripts still ride _evaluate_rows_with_frame_fallback / scoped
  evaluate, so iframe + shadow combinations compose (each frame's
  document is walked independently).

Tests: tests/test_extract_shadow_table.py (4 source anchors: harvest uses
deepQueryAll('table') and the naive query is gone, signature uses the
walker, both scripts carry their own copy - they evaluate separately, and
the walker recurses from the document root). Live probe extracted the
REAL scripts out of run_agent source and evaluated them: custom element
with a 30-row shadow table - 30/30 named rows, signature sees shadow rows,
control confirms plain querySelectorAll counts 0 tables there; light-DOM
3/3 regression byte-intact - 7/7 PASS pre-removal.

Out of scope (next): closed shadow roots (no JS access by design);
shadow-hosted pager buttons for the autopager click path (the signature
now verifies, but _auto_advance_table_page_via_dom locators stay
light-DOM); list/card harvest inside shadow roots (different selector
family); declarative shadow DOM SSR edge cases.

## Slice EXTRACT-CANVAS-1: canvas grids get an explicit declaration (done)

Layer: operations_plane (main.py pre-extract empty-candidate branch +
probe helper). Closes the P3 item "canvas grid fallback declaration":
sheet engines (Luckysheet / Univer / Handsontable canvas mode, ECharts/
AntV dashboards) paint rows onto a canvas, so every deterministic harvest
legitimately returns empty - the fast path just logged "no deterministic
candidates" and the planner burned VLM rounds rediscovering that there is
no DOM to extract. row_action already had a failure-time canvas hint; the
extraction entry point had nothing.

- _detect_canvas_grid(reason): one evaluate - large canvas/svg only
  (>= 500x300, sparklines ignored), scored by viewport coverage plus a
  grid-like bonus when the element/parent class or id mentions
  grid|table|sheet|spread|cell|excel|luckysheet|univer|handsontable.
  Returns evidence (tag/width/height/coverage/grid_like/class_hint/
  canvas_count) or {} - probe failures stay quiet.
- Empty-candidate branch: declares BEFORE giving up - evidence + fallback
  guidance (export/download button via data_export > switch to a DOM
  table/list view > screenshot extraction with an explicit accuracy
  caveat) published to workflow_memory["canvas_grid_notice"], so the
  planner sees it on the very next turn. Deterministic candidates present
  -> probe never runs (zero overhead on normal pages).

Tests: tests/test_extract_canvas_fallback.py (5 source anchors: probe
helper + size thresholds, grid-like scoring, declare-before-give-up
ordering, memory key + guidance, quiet failure). Live probe extracted the
REAL _canvas_js from run_agent source: canvas-painted 14x6 sheet inside a
luckysheet-like container - found, grid_like, coverage 0.55, evidence
fields; plain text page and small sparkline page both found=False -
5/5 PASS pre-removal.

Out of scope (next): auto-clicking the export button (stays a planner
decision - guidance only); OCR-based canvas cell reading; WebGL grids
where getBoundingClientRect underreports the painted area.

## Slice XSYS-E2E-1: dual-login cross-system relay e2e (done)

Layer: execution_kernel (test + live evidence only - no production code
changed; the unit suites already pinned SessionRouter / pool / executor in
isolation, what was missing is one continuous dual-login scenario plus
real-browser proof of the isolation the mission rule demands).

- tests/test_xsys_dual_login_e2e.py (7): one run, system_1 (alpha.example
  / alpha_admin) -> system_2 (beta.example / beta_ops) -> home, driven
  through the REAL router + pool + storage-apply code (only BrowserEnv is
  stubbed). Pins: cookie AND origin-localStorage subsets are mutually
  exclusive with foreign domains never leaking; unknown system gets the
  empty state; the forward hop launches one isolated profile whose
  context receives ONLY the beta login cookie; the home hop returns the
  home lease without launching a pool session; both sessions coexist
  under one run_id and release together; repeat hops reuse the session
  (A->B->A->B does not stack contexts).
- Live probe (real Playwright, one chromium instance): merged dual-login
  state split via router.storage_state_for_system and injected into two
  real contexts - storage_state() readback mutually exclusive (cookies +
  origins, tracker junk never leaks); renderer-level proof on one shared
  127.0.0.1 origin - page A sees only session_a + its own localStorage
  token, page B sees only session_b and reads null - 7/7 PASS
  pre-removal.

Out of scope (next): data relay assertions through WorkflowDataEdge once
the kernel consumes it (route_executor today only stamps system metadata;
no false e2e for unwired plumbing); live two-site login flows with real
credential vaults; cross-instance (multi-process) session pools.

## Slice FIXTURE-AUDIT-1: failure-fixture coverage audited and locked (done)

Layer: audit close-out (tests only - the audit ran the real chain and
found no blocking gap; the findings are frozen as regression locks).

Audit scope: capability_execute_failure_bundle.v1 production
(api_server._capability_execute_failure_bundle: failure_code >
non-action_failed warning > action_error fallback), fixture build/write
(capability_failure_fixture.py), offline replay chain
(capability_failure_replay.py: planner_feedback -> route_task ->
execution_plan, 13 checks), and the failure-code vocabularies
(_FAILURE_REPAIR_HINTS repair layer vs the planner-feedback mappings).

Findings (all verified by execution, not by reading):
- all 9 mapped codes + the action_error producer fallback + a completely
  unmapped code build fixtures and replay GREEN (11/11) - the fallback
  paths (default repair actions, default avoid list, generic preferred
  capabilities) keep the loop closed for unknown codes;
- preferred_capabilities are IDENTICAL per code across both layers; the
  avoid vocabularies differ by wording by design (route_task overlays the
  repair hints over the feedback, repair wins) but every code has a
  specific avoid list in both layers - no blocking drift;
- disk round-trip (write -> read -> replay) and the batch report
  aggregation (top_primary_failures buckets) hold.

Locks: tests/test_fixture_coverage_audit.py (18: parametrized
green-replay loop over all 11 codes, the conscious 9-code list, per-code
layer alignment, specific avoid lists, unmapped-code defaults, disk
round-trip + bundleless rejection, batch aggregation). Adding a failure
code to one table but not the other, breaking a fallback, or bending a
replay check now fails loudly.

Out of scope (next): api_server bundle-extraction unit tests (api layer
owns those; importing api_server in unit tests drags the FastAPI app);
live fixture capture from a real failing run (covered implicitly by
capability_failure fixtures written during agent_case runs); fixture TTL
/ pruning policy for workspace artifact growth.

## Slice EXTRACT-SHADOW-2: list/card harvest reaches open shadow roots (done)

Layer: data_plane (main.py structured DOM_LIST harvest + compact
list-text feed). Closes the EXTRACT-SHADOW-1 out-of-scope item "list/card
harvest inside shadow roots": both list scripts queried the light DOM
only, so card components rendered inside open shadow roots produced zero
DOM_LIST candidates and zero compact list text - shadow card pages fell
through to screenshot extraction even after SHADOW-1 fixed the tables.

- _extract_list_rows_via_dom (structured rows) and
  _extract_compact_list_text_via_dom (compact text feed) both gain the
  deepQueryAll walker; their direct-selector sweeps AND container-repeat
  sweeps ride it. Candidate scoring, meta parsing (points/comments/time/
  rating/summary), link classification, and dedup are untouched -
  light-DOM results stay byte-identical.
- Both scripts still ride their frame fallbacks, so iframe + shadow
  combinations compose.

Tests: tests/test_extract_shadow_list.py (3 source anchors: both
direct-selector sweeps walk shadow roots and the naive query is gone,
both container sweeps ditto, >= 4 walker copies across the harvest
scripts - each evaluates separately). Live probe extracted the REAL
_list_rows_js from run_agent source: custom element with 8 shadow cards -
8/8 harvested with titles/points/links parsed, control naive query counts
0 items there; light-DOM 4/4 regression intact - 7/7 PASS pre-removal.

Out of scope (next): shadow-hosted pager controls (AUTOPAGER-SHADOW-1);
closed shadow roots; slotted light-DOM content projected into shadow
layouts (innerText follows the flattened tree, believed covered, not
live-probed).

## Slice AUTOPAGER-SHADOW-1: table autopager reaches open shadow roots (done)

Layer: data_plane (main.py _auto_advance_table_page_via_dom scripts).
Closes the SHADOW-1/2 out-of-scope item "shadow-hosted pager controls":
the harvest and the before-signature already walked shadow roots, but the
autopager itself stayed light-DOM on BOTH sides - the DataTables sweep
and the Next/numeric candidate scan never saw a shadow pager (no click),
and _pager_wait_js confirmed flips against light-DOM tables only, so even
a successfully clicked shadow pager always "timed out" and the autopager
reported failure on a page that actually flipped (signature parity bug
left dormant by SHADOW-1).

- _pager_js: deepQueryAll for the DataTables API sweep and the
  button/link candidate scan; in-page el.click() works inside open
  shadow roots as-is. Light-DOM candidate order unchanged (document
  nodes enumerate first).
- _pager_wait_js: same walker, restoring parity with the deep
  before-signature - the after-flip check now sees the same shadow table
  the signature hashed.

Tests: tests/test_autopager_shadow.py (4 source anchors: DataTables sweep
+ candidate scan walk shadow roots with naive queries gone, the flip
confirmation keeps parity, both scripts carry their own walker). Live
probe extracted the REAL _pager_js / _pager_wait_js / signature JS:
custom element hosting a 40-row paginated table + pager fully inside an
open shadow root - Next found and clicked (dom_next_button), flip 1->2
CONFIRMED via the deep wait, second flip 2->3 confirmed; light-DOM pager
regression clicked + confirmed - 6/6 PASS pre-removal.

Out of scope (next): the scope loop already sweeps child frames, so
iframe x shadow pagers compose but were not live-probed together; closed
shadow roots; pagers rendered in a DIFFERENT shadow root than their table
(cross-root aria-controls linking).

## Slice FORM-RICHTEXT-4: cross-origin editor iframes rescued via frame tree (done)

Layer: intent_planning (main.py macro JS + rescue pass) + operations_plane
(actions.py form_set). Closes the FORM-RICHTEXT-3 out-of-scope item
"cross-origin editor iframes" after a probe-backed audit found three
non-blocking issues: (a) the macro's setNativeValue dropped
setRichTextValue's return value, so a cross-origin write failure still
pushed ok:true into results while verify failed on the same field
(misleading evidence); (b) the only escape was the full VLM fallback even
when every other field had written+verified fine; (c) the single-field
form_set path had none of the FORM-RICHTEXT-3 iframe support, so even
same-origin classic TinyMCE could not bind there.

- Evidence (main.py macro JS): setNativeValue returns the write method
  (iframe branch propagates, contenteditable returns it, native path
  returns 'native_value'); the write loop checks for 'iframe_unreachable'
  and pushes ok:false with reason + frameId/frameSrc coordinates instead
  of a fake ok:true.
- Rescue (main.py Python): _auto_form_rescue_unreachable_iframes runs
  inside _auto_form_fill_bound_controls_with_frames on failures carrying
  iframe_unreachable items - resolves the Playwright frame (by iframe id,
  then by URL), writes structured paragraphs via frame.evaluate (a
  cross-origin body is scriptable through the frame tree even when
  main-document JS is not), verifies the readback, then reruns the macro
  with preset_labels so rescued fields bind (exclusivity) but skip the
  in-macro write+verify; preset hits satisfy the verification gate and
  submit semantics stay in the macro. Any miss returns the original
  result (VLM fallback unchanged as backstop). Evidence lands in
  frame_level_writes on the rerun result.
- form_set (actions.py): mirrors the macro - editor iframes join
  controlSelector, label_for falls back to the <forId>_ifr stand-in
  (label_for_ifr), the TinyMCE registry lookup strips _ifr and matches
  getContainer().contains(), API-less same-origin iframes get structured
  paragraphs written into the body ahead of the execCommand path,
  setNativeValue routes iframe hosts to the rich-text writer, and
  readValue reads contentDocument.body.innerText back - so same-origin
  classic editors work end-to-end and cross-origin ones produce an honest
  value_mismatch with observed='' instead of fake evidence.

Tests: tests/test_auto_form_richtext.py 17 -> 36 (evidence anchors, preset
binding order + verification gate, rescue stub matrix: pass-throughs,
preset rerun wiring, id->URL frame resolution fallback, evaluate failure,
readback mismatch, missing frame, label-drift guard);
tests/test_form_richtext_write.py 10 -> 17 (binding selectors, _ifr
stand-in, registry strip, container match, iframe-before-exec ordering,
setNativeValue routing, body readback). Live probe (two local origins,
REAL source functions): macro first pass surfaces iframe_unreachable with
frame coordinates; wrapper rescue writes through the cross-origin frame,
reruns with preset_frame_write, Title + submit handled by the rerun, body
text verified; form_set binds same-origin classic via label_for_ifr,
prefers a registered TinyMCE API (exactly one setContent after suffix
strip), and fails honestly cross-origin - 16/16 PASS pre-removal.

Out of scope (next): editor iframes nested inside child frames (the
rescue resolves against the top-level page only - closed by
FORM-RICHTEXT-5); sandboxed iframes that block scripting entirely;
Froala/Summernote registry APIs; rescue for the component-aware fallback
path (macro-only today).

## Slice FORM-RICHTEXT-5: nested-frame editors rescued via sweep path (done)

Layer: intent_planning (main.py rescue pass + frames wrapper). Closes the
FORM-RICHTEXT-4 out-of-scope item "editor iframes nested inside child
frames": when the form lives in a same-origin child frame A and its
editor iframe B is cross-origin, the sweep entered A and surfaced the
honest iframe_unreachable evidence, but the sweep-hit path returned
without rescuing - the whole form still fell back to the VLM even though
B is reachable through Playwright's frame tree (audit probe 8/8
confirmed: page.frames flattens nesting, the URL fallback of
_resolve_editor_frame already finds B; the id lookup is main-document
only by design).

- _auto_form_rescue_unreachable_iframes gains rerun_scope (default page):
  the macro rerun executes on the host frame A where the bindings, the
  sibling fields and the submit button live; frame RESOLUTION stays on
  the page (flat frame tree). The rerun inherits the frame_url evidence
  from the swept result.
- The sweep-hit branch of _auto_form_fill_bound_controls_with_frames now
  routes through the rescue with rerun_scope=frame. ok results and
  failures without iframe_unreachable items pass through the rescue
  no-op unchanged, so same-origin nested editors keep their existing
  single-pass behaviour.

Tests: tests/test_auto_form_richtext.py 36 -> 42 (sweep wiring anchor,
rerun-scope routing, page default, frame_url inheritance, end-to-end
wrapper stub chain main-miss -> sweep-hit -> rescue -> preset rerun on
the host frame, ok-sweep-hit no-op lock). Live probe (three-layer real
pages, REAL source functions): nested cross-origin editor end-to-end ok
with rescue evidence + preset rerun + Title/submit inside frame A;
same-origin nested regression (no rescue); top-level cross-origin
regression (RICHTEXT-4 path) - 10/10 PASS pre-removal.

Out of scope (next): editors nested deeper than one URL-distinct level
with duplicate URLs (first match wins); srcdoc editor iframes (no URL to
match, id lookup cannot cross documents - closed by FORM-RICHTEXT-6);
sandboxed iframes; rescue for the component-aware fallback path.

## Slice FORM-RICHTEXT-6: srcdoc editor iframes located via stamped token (done)

Layer: intent_planning (main.py macro JS + rescue resolution). Closes the
FORM-RICHTEXT-5 out-of-scope item "srcdoc editor iframes": a srcdoc
editor iframe exposes no src attribute and every srcdoc frame reports
the about:srcdoc URL, so both coordinates the rescue relied on (id, src)
could come up empty and the field fell back to the VLM even when the
frame itself was scriptable (the typical case: <iframe srcdoc
sandbox="allow-scripts"> blocks contentDocument from the parent but
still runs Playwright evaluate). Duplicate-URL ambiguity from the
RICHTEXT-5 out-of-scope list collapses for the same reason: about:srcdoc
never identifies a frame.

- The macro JS stamps the unreachable iframe host with a
  data-vspider-frame-token attribute at report time (reused if already
  present, so retries never stack tokens; attribute stamping lives on
  the parent-document element and works regardless of the frame's
  sandbox). The failing result now carries frameToken plus a
  frameSrcdoc flag alongside the existing frameId/frameSrc.
- _resolve_editor_frame gains a frame_token lookup that runs before the
  id and src paths: it queries the page first, then the flat frame list
  (selectors pierce open shadow roots but not frame boundaries, so the
  walk is what keeps RICHTEXT-5 nested hosts reachable). Old fixtures
  without frameToken skip the new path entirely; a stale token falls
  back to id/src untouched.

Tests: tests/test_auto_form_richtext.py 42 -> 48 (token + srcdoc-flag JS
anchors, token reuse order lock, page-level token resolution with
about:srcdoc evidence, nested-host token walk via a querying host frame
stub, stale-token fallback to src, legacy fixtures emit no token query,
unresolvable srcdoc keeps the original result).

## Slice S1-S3 (M1 准确收口): select/upload 证据链 + 清默认 xlsx (done)

Layer: operations_plane (actions.py handlers) + data_plane (data_manager /
browser_env intercept track / fetch_articles).

- S1 SelectHandler: removed the silent select_option(index=0) fallback
  (used to mis-pick the first option without reporting); label -> value
  miss now fails loudly. Added selected-option readback evidence
  (selectedOptions text/value vs requested type_value, mismatch raises);
  rpa_trail entry gains method/verified/observed. Custom widgets where
  the handle is not a SELECT (readback null) skip verification.
- S2 UploadHandler: after set_input_files, input.files is read back as
  page_echo evidence; empty/mismatched echo raises instead of claiming
  success. rpa_trail entry added (uploaded_file / page_echo / verified).
- S3 default-xlsx cleanup: _resolve_intercept_container no-contract and
  unknown-container fallbacks now return jsonl (rows stay rows, never a
  blind xlsx); _save_dataframe_to_excel invalid-container fallback jsonl;
  intercept default filename "output.xlsx" -> "output" (suffix follows
  container) in browser_env; main._xhr_saved_row_count made
  container-aware (probes jsonl/csv/xlsx); fetch_articles.py reads and
  writes output_*.{xlsx,csv,jsonl} following the input container.

Tests: tests/test_select_upload_evidence.py (new, 8 cases: label/value
readback, no index-0 fallback, mismatch fails loudly, non-select skip,
upload echo match/empty/wrong-file); tests/test_xhr_intercept_contract.py
updated (no-contract -> jsonl, xlsx append via explicit contract).

## Slice S4-S5 (M1 准确收口): snapshot 一等 action + type 富文本复用 (done)

Layer: operations_plane (new snapshot_actions.py module + actions.py
TypeHandler) + intent_planning (action_registry / capability_router /
prompts / prompt_skills wiring).

- S4 html_snapshot / screenshot first-class actions (new module
  visual_web_agent/snapshot_actions.py, six-step paradigm): persist the
  page HTML / a viewport-or-full-page PNG through resolve_output_path
  (prefers runs/<id>/artifacts/) + register_artifact, so snapshots land
  in manifest.json (kind=html_snapshot / screenshot) instead of living
  outside the trusted artifact index. Empty HTML / empty PNG raise.
  Memory writeback {path, size, source_url[, full_page]}; rpa_trail
  evidence carries output_kind/output_path/size/verified. Wired:
  VSpiderAction literals, ActionTool entries (capability=snapshot),
  capability_router _SNAPSHOT_RE -> snapshot_preferred signal ->
  page_snapshot backend-plan row, prompts _SNAPSHOT_TRIGGERS ->
  SNAPSHOT_SKILL block.
- S5 TypeHandler contenteditable reuse: before the keyboard path, the
  handler probes el.isContentEditable; richtext hosts route through
  _TYPE_RICHTEXT_WRITE_JS (mirrors form_set's setRichTextValue adapters:
  Quill -> TinyMCE -> CKEditor5 -> execCommand insertText -> structured
  paragraphs) with an innerText readback check (mismatch raises), so raw
  keyboard.type no longer desyncs editor document models. Plain inputs
  keep the original Ctrl+A/Backspace/type path byte-identical. The trail
  entry gains method/verified when the richtext branch ran.

Tests: tests/test_snapshot_and_type_richtext.py (new, 15 cases: schema +
handler/tool registration, SNAPSHOT_SKILL wiring, _SNAPSHOT_RE routing,
html_snapshot persist/register/memory/trail + filename hint + empty-html
+ missing-page, screenshot viewport/full/empty-bytes, type richtext
editor-API routing + readback mismatch + plain-input keyboard path).

## Slice BOT-CHL: anti-bot challenge route + full-loop fixture regression

- 能力名: bot_challenge_guard (Cloudflare/Turnstile/reCAPTCHA/hCaptcha/滑块/风控)。
- 影响层: intent_planning (capability_router 新增 `_CHALLENGE_RE` + `bot_challenge`
  信号 → backend_plan / fallback_chain 注入 bot_challenge_guard，仅在 auth/反爬
  goal 触发，普通抓取 goal 不注入)；runtime_guards (capability_manifest 注册
  `bot_challenge_guard` CapabilitySpec；planner_contract 新增 risk_flag
  `anti_bot_challenge_guarded`)。main loop 感知阶段早已固定调用
  handle_bot_challenge_step（每 step probe → 被动等待 → 可选第三方解 → HITL →
  storage_state 复用 → 代理重路由），本 slice 把对应的确定性路由契约补齐。
- 新增 contract 字段: signals.bot_challenge；risk_flags.anti_bot_challenge_guarded；
  capability bot_challenge_guard（向下兼容，仅新增）。
- Tests: tests/test_bot_challenge_vendor_routing.py（19 例：vendor 分类分流、
  reCAPTCHA/hCaptcha 跳过 solver 直接 HITL、Cloudflare/captcha solver 成功/失败
  分支、max_hitl 预算、router 反爬 goal 路由 + 普通 goal 不路由）；
  tests/test_agent_loop_stub_e2e.py（2 例：VLM stub 驱动 run_agent 全链路跑
  Alpha fixture 站点，make_plan/ask 被真实调用、done 收口返回 True、落 run 痕迹）。

## Slice BOT-CHL-2: cross-system anti-bot scenarios on the dual-system fixture

- 影响层: 仅测试资产（tests/），运行时零改动。
- tests/scenario_site.py: Beta 订单系统新增可选 WAF 盾
  （make_beta_handler(store, challenge=True)，默认关闭、既有消费方不受影响）：
  无 cf_clearance cookie 的 GET/POST 一律 403 Cloudflare 风格 interstitial
  （"Just a moment..." + #challenge-form + data-ray），页面 JS 700ms 后经
  /cdn-cgi/challenge 自动过盾——该端点颁发 cf_clearance 并 302 回原路径；
  store 记 waf_blocks / waf_clearances 供断言。
- tests/test_cross_system_bot_challenge.py（7 例）:
  C1 HTTP 层拦截/颁发/放行（GET+POST 同拦）；
  C2 _PROBE_JS 真 Chromium 探针：interstitial→cloudflare、Alpha 干净页与
  过盾后 Beta 页→无挑战；
  C3 handle_bot_challenge_step 走 async Chromium 全链路，被动等待窗口内靠
  cf_clearance 自行通过（零 HITL；async loop 跑在 worker 线程，避开 sync
  Playwright 占用主线程 event loop）；
  C4 跨系统接力：Alpha 抓 SKU → Beta 过盾→登录→下单写回 store，
  cf_clearance / beta_session 仅存于 Beta origin、不泄漏 Alpha；
  C5 HITL 失败路径（store.waf_autopass=False 渲染无自动过盾脚本的顽固
  interstitial）：HITL 调用但人工未解（cleared=False/action=hitl）、人工经
  /cdn-cgi/challenge 手动过盾（cleared_after_hitl=True）、HITL 预算耗尽
  （action=max_hitl、回调不触发）、HITL 回调抛异常 guard 不崩溃。

## Slice S6-S11 (M2 跨系统贯通): input_contract → graph → session → data bus 一条龙

- 能力名: cross_system_contract_chain（契约声明系统 → 图谱携带 auth_profile →
  会话预取 → WorkflowDataEdge 运行时数据接力 → 开关默认开启）。
- 影响层: intent_planning + execution_kernel + data_plane。
- S6 workflow_graph.py: `WorkflowSystem` 新增 `auth_profile` 字段（to_dict 同步，
  仅新增向下兼容）；`_build_systems` 新增最高优先级候选源
  `_contract_url_candidates`（读 route/context 的 input_contract.urls[]，显式
  system_id 按 id 去重、可同域双系统，声明的 auth_profile 隐含 auth_required）；
  sessions 继承具体 profile（无声明保持 required/default 旧语义）。
  SessionRouter.SystemAuthPlan 原本就读 systems[].auth_profile，自此不再恒 auto。
- S7 io_contract/input_contract.py: 新增 `assign_system_ids`——多 host 契约给
  system_id=auto 的条目按 host 派发确定性 `sys_<host_slug>`（单 host 保持 auto，
  字节不变）；build_input_contract 收口处统一调用。main.py route 调用点改为
  读回 runs/<id>/input_contract.json 并以 context.input_contract 注入
  route_capabilities_for_task（capability_api 路径原本已注入）。
- S8 新模块 workflow_data_bus.py（workflow_data_bus.v1）: 按 workflow_graph
  data_edges 建 run 内存总线，publish（按节点/按 capability，含
  extractor_select→generic_extractor 别名）→ consume（一次性投递，可 peek）；
  snapshot 永不含 payload；get_run_bus/clear_run_bus 跨调用共享同 run 总线。
  route_executor 接线：完成态 `_finish` 经 `_wire_data_bus` 把 result 发布到
  对应节点出边，返回结构新增 `data_handoff`（run_id/published_edges/bus 快照）。
- S9 session_router.py: 新增 `pre_acquire_sessions`（与 route_executor
  session_plan 同形 + session_id/acquired/error，逐系统容错）；main.py 在
  session_router 构建后、flag=on 且图谱含 >1 web 系统时预取全部规划会话并
  emit `session_plan_preacquired` 事件。
- S10 tests/test_xsys_contract_to_relay_e2e.py: flag=on 单场景全链 E2E——
  双 URL 契约 → 图谱系统/节点归属/auth_profile → 预取 2 会话 → A→B 切换仅注入
  B 的 cookie 子集 → bus 把 A 行集内存接力给 B（一次性投递、快照无 payload）
  → release_all 同 run 收口。
- S11 cross_system_config.py: `cross_system_enabled` 默认改 **on**
  （env_flag default=True），`VSPIDER_CROSS_SYSTEM_SWITCH=0/false/no/off` 退出；
  CrossSystemConfig.enabled 默认 True；模块/函数文档同步。
- 新增 contract 字段: workflow_graph.systems[].auth_profile；
  route_executor 返回 data_handoff；event_stream 新事件 session_plan_preacquired
  （均仅新增，向下兼容）。
- Tests: tests/test_workflow_graph_input_contract.py（S6+S7，15 例）、
  tests/test_workflow_data_bus.py（S8，17 例）、test_session_router.py 新增
  TestSessionRouterPreAcquire（S9，4 例）、tests/test_xsys_contract_to_relay_e2e.py
  （S10+S11，2 例）、test_cross_system_config.py 默认值翻转 + opt-out 矩阵。
  validate_y s6_s11_cross_system: 定向 112✓ / npm build✓；全量 pytest
  3284 通过 0 失败（排除并行重构中的 test_timeline_replay_search.py，同 M1）。

## Slice S12-S14 (M3 高效): api_replay 自动翻页 + 增量爬取 + spider 并发 (done)

- 能力名: api_replay_pagination / incremental_crawl / spider_concurrency。
- 影响层: data_plane（api_replay / incremental_state 新模块 / spider_lite）+
  execution_kernel 接线（route_executor / api_server）。
- S12 api_replay.py: 新增 `paginate_replay`——page 数值与 cursor/next_url 双
  模式自动翻页聚合（_CURSOR_KEYS/_NEXT_CURSOR_KEYS/_NEXT_URL_KEYS 识别游标），
  行级签名去重；停止条件：empty_page / duplicate_page / short_page /
  repeated_cursor / repeated_next_url / target_reached / http_error /
  max_pages（truncated 标记），返回 stop_reason + 各页证据并合并落 artifact。
  route_executor 接线：payload 显式 paginate/auto_paginate 或
  target_count > page_size 时自动走翻页路径（max_pages 默认 20 上限 100）；
  api_server `/replay` 端点新增 paginate/max_pages 参数（仅新增向下兼容）。
- S13 新模块 incremental_state.py（incremental_state.v1）: `IncrementalStore`
  按 scope（默认首个 allowed_domain）落盘已交付行签名（item_signature 全行或
  key_fields 子集 sha 截断），`filter_new` + `commit` 实现跨 run 去重续抓。
  spider_lite 接线 `_apply_incremental_filter`：`incremental=True` opt-in
  （默认 off 字节不变），result 新增 incremental 统计
  （new_count/skipped_known/key_fields/store 状态）。
- S14 spider_lite.py: 抓取循环重构为批量漏斗——`_pop_eligible_batch`
  （seen/domain/robots 闸门保持原序，每轮至少消费一个 frontier 条目防空转）+
  `_fetch_batch`（仅网络段进 ThreadPoolExecutor；replay 查缓存与 record 写
  缓存都留在主线程，PageResponseCache 无需加锁；批内顺序保持，错误单行记
  errors 不杀 run）。`concurrency` payload opt-in（默认 1 = 原串行语义，
  上限 16，max_concurrency 别名）；result 新增 `fetch_stats`
  （network/cache/concurrency，仅新增）。BFS 批量弹出仍是层序，与串行
  结果逐项一致（test 锁定）。
- Tests: tests/test_api_replay_pagination.py（S12，page/cursor/next_url、
  去重、各停止条件、route_executor/api_server 接线）、
  tests/test_incremental_state.py（S13，签名/scope/key_fields、跨 run 去重、
  spider 接线、off 路径字节不变）、tests/test_spider_lite_concurrency.py
  （S14，7 例：并发结果与串行逐项一致、真并发重叠断言、max_pages 预算、
  record→replay 缓存协同、批内错误隔离、clamp 1..16、串行 fetch_stats）。
  validate_y S14: 定向✓ / npm build✓；全量 pytest 3313 通过 0 失败
  （排除并行 App.vue 组件化重构钉源码的 test_timeline_replay_search.py /
  test_capability_router.py::test_capability_router_source_wiring /
  test_frontend_component_split_y126.py 单例，字符串已迁至未提交的
  CapabilityPlanPane.vue 等新组件）。

## Slice P0-P1 (M3 高效前置): 仓库卫生核验 + 感知段抽离 phases/perception.py (done)

- 能力名: perception_phase_split（main.py 拆分第一刀，E1-E3 的地基）。
- 影响层: execution_kernel（main.py）+ 工程基建（P0 零运行时改动）。
- P0 仓库卫生: 逐项核验计划清单 11 类运行时产物路径（runs/ workspace/
  temp_uploads/ browser_data/ logs/ screenshots/ .tmp_*/ .pytest_cache/
  __pycache__/ 等）`git check-ignore -v` 全部命中已提交的 .gitignore 条目，
  git status 仅剩源码改动——无需新增条目，验收即通过。
- P1 感知段平移: main.py 每回合「图文双模态融合」感知块（tabs/page 摘要 →
  SoM 截图含 no-active-page 重启重试 → bot_challenge 钩子（HITL 过盾重截/
  auth harvest/代理重路由重截）→ AX 提取 + A11y Enhancer + 15000 截断 →
  BrowserStateSnapshot 组装 → event_stream.observe → ax_block 拼接）整段
  165 行原样平移至新模块 visual_web_agent/phases/perception.py
  （PerceptionPhase.run → PerceptionSnapshot），main.py 原位置一行委托 +
  字段回填（净 -140 行）。CRLF 大块按工程规范走字节级 _patch 脚本，patch
  后即删。纯平移零行为改动，零新契约字段。
- Tests: tests/test_phase_perception_split.py（4 例：snapshot 字段与拆分前
  主路径一致 url/title/screenshot_path/ax_excerpt/interactive_count、observe
  事件单发且携带 browser_state、no-active-page 重启重试一次、AX 失败回退
  SCREENSHOT_ONLY）；tests/test_agent_loop_stub_e2e.py 2 例真 Chromium
  全链路保持绿（经委托路径）。
  validate_y P1: 定向✓ / npm build✓；全量 pytest 3336 通过 0 新增失败
  （沿用 S14 口径排除并行 UI 重构预先存在的 5 例，已在 HEAD 干净 worktree
  复核均预先失败）。
- Out of scope (next): E1 感知复用（PerceptionPhase 持 last_signature，
  改为循环外单例）、E2 局部 SoM、E3 AX 增量 diff。

## Slice E1 (M3 高效): 感知复用 perception_reuse (done)

- 能力名: perception_reuse（DOM 签名未变即跳过截图 + SoM 重注入）。
- 影响层: browser_substrate（browser_env.py 探针）+ execution_kernel
  （phases/perception.py 复用决策、main.py 持久化实例）+ 契约
  （event_stream observe 仅新增字段）。
- browser_env.py 新增 `dom_signature()`：一次轻量 evaluate 取
  `href + DOM 节点数 + body 文本长度 + 可交互元素数` 拼 sha1；失败 / 无页面
  安静返回空串，调用方视为"无法判定"走全量感知，探针绝不影响主链路。
- PerceptionPhase 改为有状态（main.py 循环外单例 `_perception_phase`，
  CRLF 字节 patch 两处）：每回合先探签名，满足「签名非空且与上轮相同 +
  上一动作 ActionResult.changed_url/changed_dom 均 False（dict 形态兼容）+
  连续复用 < 3」则跳过 tabs/截图/SoM/AX/snapshot 整段，复用上一轮
  PerceptionSnapshot（dataclasses.replace 拷贝，step 更新、
  browser_state.metadata 标 perception_reused=True），observe 事件照发。
  逃生阀：连续复用满 3 回合强制全量一次并清零计数，防签名碰撞死视。
  无 dom_signature 的旧 stub / 旧 browser 自动恒走全量（向下兼容）。
- 新增 contract 字段: event_stream observe 事件 `perception_reused: bool`
  （默认 False，仅新增向下兼容）。
- Tests: tests/test_perception_reuse.py（8 例：同签名第二轮零截图/零 AX/
  零 tabs 且快照复用标记、签名变更走全量、changed_dom/changed_url
  （对象 + dict 形态）强制全量、逃生阀第 4 轮强制全量后计数复位再复用、
  无探针 browser 恒全量、dom_signature sha1 确定性 + 安静回退、
  observe 字段落盘断言）；TDD 先红后绿。
  验证: 定向 8✓ + P1 拆分回归 4✓ + test_agent_loop_stub_e2e 真 Chromium
  2✓；全量 pytest 3354 通过 0 新增失败（沿用 S14/P1 口径排除并行 UI
  重构预先存在的 5 例）；本 slice 零前端改动，npm build 沿用 P1 绿。
- Out of scope (next): E2 局部 SoM（viewport scope）、E3 AX 增量 diff、
  E7 基线把 perception_reused 计入效率指标。

## Slice E2 (M3 高效): 局部 SoM partial_som (done)

- 能力名: partial_som（默认只标视口内元素，scroll/首回合/逃生阀回退全页）。
- 影响层: browser_substrate（som_inject_v6.js scope 三选区 + browser_env.py
  透传 scope + 幻觉校验带视口提示）+ execution_kernel（phases/perception.py
  _decide_scope 选区策略）+ prompts（prompts.py 局部 SoM skill 区块）。
- JS 层: som_inject_v6.js 入参 `{scope: "viewport"|"full"|{selector}}`；
  viewport 模式在 Layer 3 视口边界检查 + isNotOccluded + isOccludedFinal 均
  裁掉视口外元素；full 放行视口外；selector 只扫容器子树。向下兼容纯数字
  startIndex（= viewport，历史行为不变）。
- Python 层: browser_env.mark_and_screenshot 透传 scope，_last_som_scope
  记录选区供幻觉校验；perception.py _decide_scope 默认 viewport，以下情形
  回退 full——首回合、E1 逃生阀回合、上一动作为滚动/翻页、VLM 显式请求
  全页（_request_full_som 一次性 flag，消费后复位）。滚动/翻页同时阻止 E1
  复用（_action_was_scroll_or_paging 检测，DOM 签名常不变但视口已移动）。
- 幻觉校验: viewport 局部标注下命中越界 ID → 报错补「本轮为视口局部 SoM，
  目标可能在视口外——先 smooth_scroll 把它移入视口再操作」提示。
- Prompts: prompts.py 新增「🔭 局部 SoM」skill 区块（六步范式第 4 步），
  说明红框默认只覆盖视口、目标不在红框里先 scroll、首回合/翻页后自动全页。
- 新增 contract 字段: 无新增 event_stream 字段（scope 信息通过
  _last_som_scope 内部记录，不外泄契约）。
- Tests: tests/test_partial_som.py（12 例）：
  - JS 选区 5 例（真 Chromium 长页 fixture）：full 标全页、viewport ⊊ full
    且排除折叠下方、legacy 数字参数= viewport、无参数= viewport、selector
    只标容器子树。
  - 感知层选区决策 5 例（stub，无需 Chromium）：首回合 full、稳定回合
    viewport、scroll 后强制 full、逃生阀回合 full、VLM 请求一次性消费。
  - 幻觉 scope 提示 2 例：viewport 下越界 ID 含「视口+scroll」提示、
    full 下无视口提示。
  验证: 定向 12✓ + P1 拆分回归 4✓ + E1 感知复用回归 8✓ +
  test_agent_loop_stub_e2e 真 Chromium 2✓；全量 pytest 3380 通过
  0 新增失败（沿用 S14/P1/E1 口径排除并行 UI 重构预先存在的 5 例）。
- Out of scope (next): E3 AX 增量 diff、E4 selector 缓存、E5 VLM 回合
  预算、E6 api_replay 优先、E7 效率基线 benchmark。

## Slice E3 (M3 高效): AX 摘要增量 diff ax_incremental (done)

- 能力名: ax_incremental（非首回合/非逃生阀回合只输出 AX 变化行）。
- 影响层: execution_kernel（phases/perception.py）。
- PerceptionPhase 新增 `_last_ax_lines: set[str] | None` 状态，持有上一轮
  全量 AX 提取的行集合（仅在全量感知回合更新，E1 复用回合不更新）。
- 新增 `_compute_ax_diff(current_text, force_full)` 方法：首回合
  （`_last_ax_lines is None`）或 E1 逃生阀强制全量回合返回全量 AX 文本；
  否则返回增量 diff——`[AX 增量] N 行不变 | +M 新增 | -K 已消失` header +
  仅新增行与消失行正文。AX 提取失败（空串）安静跳过 diff。
- `run()` 在 A11y Enhancer + 截断之后、browser_state 组装之前调用
  `_compute_ax_diff`，将结果回写 `ax_tree_text` 变量——downstream 的
  browser_state、event_stream.observe、ax_block prompt 渲染统一使用 diff
  输出，observe 事件 ax_lines 自然缩小。
- ax_block prompt 渲染双模板：全量走原「页面无障碍语义树」header；增量走
  「AX Tree 增量变化」header + 「未变内容沿用上轮」提示。
- 新增 contract 字段: 无（diff 形态通过文本内容表达，不新增 event_stream
  字段，向下兼容）。
- Tests: tests/test_ax_incremental.py（7 例：首轮全量、同 AX 无变化标记、
  不同 AX 增量 diff 仅含新增/消失行且 observe 体积下降、逃生阀回合全量、
  AX 失败无 diff、复用后 diff 仍对齐上次全量、ax_block 增量 header）；
  TDD 先红后绿。
  验证: 定向 7✓ + P1 拆分回归 4✓ + E1 感知复用回归 8✓ + E2 局部 SoM 12✓；
  全部 31 例通过 0 失败。
- Out of scope (next): E7 效率基线 benchmark。

## Slice E4 (M3 高效): selector 跨 run 动作缓存 selector_action_cache (done)

- 能力名: selector_action_cache（批量填表/重复访问场景，首 run 发现的
  已验证 selector→action 对跨 run 持久缓存，后续 run 直接 replay 跳 VLM）。
- 影响层: data_plane（新模块 visual_web_agent/selector_action_cache.py）。
- 新增 SelectorActionCache 类：per-host JSON 文件持久化，键 =
  (page_path_pattern, selector_fingerprint)；支持 max_entries 上限驱逐
  + max_age_s 老化驱逐；原子写 (tmp + os.replace)。
- 仅 rpa_trail.verified=True 的动作才入缓存（准确铁律：无证据不缓存）。
- capability_router 增强：`_apply_capture_fixture_probe` 同时探测
  selector_action_cache，命中设 `signals.selector_cache_available`；
  `_fallback_chain` 在 browser_interaction 链首位插入
  selector_action_cache 步骤。
- 新增 contract 字段: 无（缓存为旁路加速，不新增 event_stream 字段）。
- Tests: tests/test_selector_cache_vlm_budget_e6.py TestSelectorActionCache
  + TestSelectorFingerprint + TestPathPattern（14 例：miss/hit/跨 query
  命中/跨 path miss/eviction/age eviction/clear/stats/factory）。

### E4 扩展: selector_cache.py click/type 集成 (done)

- selector_cache.py 新增 `som_cache_key(som_elements, target_id)` 从 SoM
  元素提取 role::name 缓存键 + `validate_cached_target(page, entry,
  expected_name)` 通过 visibility + innerText/aria-label 校验缓存元素。
- ClickHandler: 读路径 — `_resolve_action_target` 前按 SoM 元素 role+name
  查缓存，命中即跳过 SoM 解析直接用缓存 CSS selector（evidence 链不缺：
  xpath / ax_signature 仍从缓存元素采集），rpa_trail 记 `method=selector_cache`；
  写回 — 首次成功点击后 `derive_selector` 存入缓存。
- TypeHandler: 写回 — 成功输入后 `derive_selector` 存入缓存（读路径保守
  暂不接入，type 场景下 SoM 解析开销更低且视口锁定预检依赖 target 对象）。
- Tests: tests/test_selector_cache.py 扩展 +9 例（TestSomCacheKey 4 例 +
  TestValidateCachedTarget 5 例）；既有 14 例 + click_text 4 例不掉。
  验证: 42 例通过（E4 23 + E3 7 + E1 8 + P1 4）0 失败。

## Slice E5 (M3 高效): VLM 回合预算 vlm_budget (done)

- 能力名: vlm_budget（per-run VLM 调用/token 预算计量 + 软硬限制）。
- 影响层: model_plane（新模块 visual_web_agent/vlm_budget.py + vlm_client.py 接线）。
- 新增 VlmBudget 类 + BudgetConfig：BudgetConfig(max_calls, max_tokens,
  soft_ratio) 控制预算；VlmBudget.record() 每步 VLM 调用后累计
  prompt/completion tokens + latency；软限警告走 broadcast_phase；
  硬限抛 VlmBudgetExhausted 让 run 优雅终止。
- VLMClient.__init__ 初始化 self._budget（默认无限制 = 零开销）；
  VLMClient.ask() 成功返回后从 response.usage 提取 token 计数并 record；
  新增 configure_budget() + budget_summary 属性供 main/API 设定和查询。
- 新增 contract 字段: 无（budget 事件通过已有 broadcast_phase 推送，
  不新增 event_stream 字段）。
- Tests: tests/test_selector_cache_vlm_budget_e6.py TestVlmBudget
  （10 例：increment/多步累计/hard_limit_calls/hard_limit_tokens/
  no_limit/remaining/summary/step_log/soft_warning）。

## Slice E6 (M3 高效): api_replay 路由优先级提升 (done)

- 能力名: api_replay_priority_lift（capability_router 增强：探测
  selector_action_cache 并注入信号 + fallback_chain 优先 replay）。
- 影响层: intent_planning（capability_router.py）。
- `_apply_capture_fixture_probe` 扩展：原来只探测 network_intelligence
  capture index（api 场景），现在同时探测 selector_action_cache（form/
  browser_interaction 场景）；selector 缓存命中设
  `signals.selector_cache_available` + `strategy_context.selector_action_cache`
  证据。
- `_fallback_chain` 增强：browser_interaction 且有 selector 缓存时，
  在 browser_backend_abstraction 之前插入 selector_action_cache 步骤，
  让已验证的 selector 动作优先于 VLM 视觉探测。
- 新增 contract 字段: 无。
- Tests: tests/test_selector_cache_vlm_budget_e6.py TestE6Routing
  （3 例：api_goal fallback 含 api_replay/network_intelligence + selector
  cache signal 注入 + form 信号正确识别）；capability_router 回归 41/41 全绿。

## Slice E7 (M3 高效): 效率基线 benchmark efficiency_baseline (done)

- 能力名: efficiency_baseline（锁住 E1-E3 感知优化收益，防回退）。
- 影响层: 仅测试资产。
- tests/test_efficiency_baseline.py 3 例：
  - 8 回合序列基线（20 行 AX 树 stub，T1 全量 + T2-T4 复用 + T5 逃生阀
    + T6/T8 签名变更 diff + T7 复用）：断言 full_perception ≤ 4、reuse ≥ 3、
    total_ax_chars < naive×50%、screenshot ≤ 5。
  - AX diff 体积缩减断言（第二轮 ax_lines < 第一轮）。
  - 逃生阀后 AX diff 基线重置（全量非增量）。
  任何后续 PR 导致效率指标超出基线即测试红。
- 验证: E7 3✓ + E3 7✓ + E1 8✓ + P1 4✓ + E2 12✓ + E4 23✓ = 57 通过 0 失败。

## M3「高效」里程碑收口

P0✓ P1✓ E1✓ E2✓ E3✓ E4✓ E5✓ E6✓ E7✓ — 全部 9 个 slice 完成。
宗旨「高效」四个分句覆盖：最少回合(E5)、局部感知(E1/E2/E3/E7)、
API replay(E6)、缓存不重抓(E4)。效率不以牺牲准确性为代价
（E1 逃生阀、E2 full 回退、E4 命中仍出全套 evidence）。

---

# M4「通用·治理」里程碑

## Slice G0 (M4 通用): 清理 actions.py.bak (done)

- actions.py.bak 已在 actions/ 拆分后删除，Glob 确认不存在。
- 验证: 无残留文件。

## Slice G1 (M4 通用): main.py → phases/startup.py (done)

- 能力名: startup_phase_split（RunContext + 循环守卫工厂）。
- 影响层: main.py 初始化段 → phases/startup.py 纯平移。
- 新模块 `visual_web_agent/phases/startup.py`：
  - `RunContext` 数据类：run_ts / vlm_output / xhr_output / goal_output_mode 等核心状态。
  - `prepare_run_identity(run_id=)` → RunContext：run_id 清洗、时间戳、输出文件名。
  - `init_loop_guards(vlm)` → LoopGuards：Judge / LoopDetector / FailureStats / Tracker reset。
- main.py 两处委托：
  - run_id 计算 → `_run_ctx = _prepare_run_id(run_id=run_id)`
  - Guard 创建 → `_guards = _init_guards(vlm)` + 字段回填
- 适配 test_phase_event_persistence 断言（_run_ts 来源兼容直接/RunContext）。
- 新增 contract 字段: 无。
- Tests: tests/test_phase_startup_split.py 7 例（RunContext 字段/默认值、LoopGuards 完整性/配置、run_id 清洗）。
- 验证: G1 7✓ + P1 4✓ + e2e 2✓ + M3 全系 53✓ + 全量 pytest 3491 passed 0 failed 2 skipped。

## Slice G2 (M4 通用): main.py → phases/planning.py (done)

- 能力名: planning_phase_split（PlanningPhase — Planner / Reflector 状态机）。
- 影响层: main.py Planner/Reflector 段 → phases/planning.py 纯平移。
- 新模块 `visual_web_agent/phases/planning.py`：
  - `PlanningPhase` 类：task_plan / reflect_count / steps_since_reflect / abort_requested 等状态。
  - `make_initial_plan(vlm, goal, ...)` async → TaskPlan | None：起手子目标生成，失败静默降级。
  - `maybe_reflect(vlm, current_url, goal, ...)` async → ReflectDecision | None：信号驱动反思。
- main.py 三处委托：
  - 状态变量 → `_planning = _PlanningPhase()`
  - 起手 Plan → `_planning.make_initial_plan(...)`
  - 循环内 Reflector → `_planning.maybe_reflect(...)`
- 新增 contract 字段: 无。
- Tests: tests/test_phase_planning_split.py 6 例。

## Slice G3 (M4 通用): main.py → phases/decision.py (done)

- 能力名: decision_phase_split（VLM ask 封装 + prompt image policy）。
- 影响层: main.py VLM 决策调用 → phases/decision.py 纯平移。
- 新模块 `visual_web_agent/phases/decision.py`：
  - `VlmDecisionResult` 数据类：decisions + prompt_images_sent。
  - `make_vlm_decision(vlm, ...)` async → VlmDecisionResult：封装 vlm.ask + prompt image policy。
- main.py 一处委托：vlm.ask → `_vlm_decide(...)`
- 新增 contract 字段: 无。
- Tests: tests/test_phase_decision_split.py 3 例。
- 验证: G2 6✓ + G3 3✓ + G1 7✓ + P1 4✓ + e2e 2✓ = 22 通过 0 失败；
  全量 pytest（排除 api_server.py 预存在问题）3399 passed 0 failed 2 skipped。

## Slice G4 (M4 通用): main.py → phases/action_dispatch.py (done)

- 能力名: action_dispatch_phase_split（PostDecisionGuards — repeat / zero-target 守卫）。
- 影响层: main.py 后决策守卫段 → phases/action_dispatch.py 纯平移。
- 新模块 `visual_web_agent/phases/action_dispatch.py`：
  - `PostDecisionGuards` 类：prev_action_sig / repeat_action_count / consecutive_zero_target 状态。
  - `apply_repeat_guard(decisions, vlm)` → GuardResult：两阶段重复动作检测（软警告→硬劫持+子目标推进）。
  - `apply_zero_target_guard(decisions, vlm)` → GuardResult：ZERO_TARGET_DOWNGRADE 连续计数+最后通牒。
  - `record_action_sig(decisions)`：记录最终 action signature 供下一步重复检测。
  - `GuardResult` 数据类：decisions + repeat_warned + repeat_hijacked + zero_target_ultimatum。
- main.py 四处委托：
  - 初始化 → `_post_decision_guards = _PDG()`
  - 重复守卫 → `_rg_result = _post_decision_guards.apply_repeat_guard(...)`
  - 零目标守卫 → `_zt_result = _post_decision_guards.apply_zero_target_guard(...)`
  - 签名记录 → `_post_decision_guards.record_action_sig(decisions)`
- 新增 contract 字段: 无。
- Tests: tests/test_phase_action_dispatch_split.py 16 例。

## Slice G5 (M4 通用): main.py → phases/finalization.py (done)

- 能力名: finalization_phase_split（run 结束清理：io_contract / checkpoint / resume / event_stream / html_trace / browser release）。
- 影响层: main.py finally 块 → phases/finalization.py 纯平移。
- 新模块 `visual_web_agent/phases/finalization.py`：
  - `finalize_run(...)` async：统一 run 结束清理流程，每个子步骤独立 try/except 不阻塞后续。
  - `build_run_end_metadata(...)` → dict：组装 event_stream.run_end 的 metadata。
  - `_clear_io_contract()` / `_clear_phase_log()` / `_finish_checkpoint()` / `_record_resume()`：清理辅助函数。
  - `RunEndMetadata` 数据类。
- main.py finally 块顶部一处委托：`await _finalize_run(...)`，原有内联代码保留作防御性回退。
- 新增 contract 字段: 无。
- Tests: tests/test_phase_finalization_split.py 15 例。
- 验证: G4 16✓ + G5 15✓ + G2 6✓ + G3 3✓ + G1 7✓ + P1 4✓ = 51 通过 0 失败；
  全量 pytest 3505 passed（26 预存在排序/UI 失败）0 新增失败 2 skipped。

## Slice G6 (M4 通用): browser_env.py → som_injector.py (done)

- 能力名: som_injector_split（SoM 注入引擎：JS 评估 + 元素收集 + 重试 + 性能遥测）。
- 影响层: browser_env.py SoM 注入段 → som_injector.py 纯平移。
- 新模块 `visual_web_agent/som_injector.py`：
  - `SomInjector` 类：持有 som_js 脚本。
  - `inject_across_frames(frames, ...)` async → (SomResult, input_descriptions)：跨 frame 评估 SoM JS + 元素收集。
  - `retry_injection(page, ...)` async → 零元素重试逻辑。
  - `SomResult` 数据类：elements / total_elements / injected_frames / duration_ms / is_heavy / retried。
- 新增 contract 字段: 无。
- Tests: tests/test_som_injector_split.py 10 例。

## Slice G7 (M4 通用): api_server.py → api_routes/ (done, phase 1)

- 能力名: api_routes_split（API 路由按业务域拆分，phase 1: spider routes）。
- 影响层: api_server.py spider 路由段 → api_routes/spider_api.py 纯平移。
- 新模块 `api_routes/__init__.py` + `api_routes/spider_api.py`：
  - `register_spider_routes(app, spider_lite=)` → 挂载全部 7 个 /api/spider/* 端点。
  - 路由: run / runs / page_cache / page_cache/entries / export / items / get_run。
- 新增 contract 字段: 无。
- Tests: tests/test_api_routes_spider_split.py 8 例。
- 验证: G6 10✓ + G7 8✓ + G4-G5 31✓ + G2-G3 9✓ + G1 7✓ + P1 4✓ = 69 通过 0 失败。

## Slice C1 (M4 契约): manifest.json 写入闭环 (done)

- 能力名: manifest_enforcement（确认所有 data_writer 均通过 finalize_file_artifact 写入 manifest）。
- 现状验证：全部 7 个 writer（xlsx/csv/jsonl/json/markdown/html/files_folder）+ _base.py 已调用 finalize_file_artifact → append_manifest_item 链路。dispatch.py 路由全 8 种 container。
- 新增 contract 字段: 无（produced_by 已在 ManifestItem.v1）。
- Tests: tests/test_manifest_enforcement.py 9 例（源码锚定 + dispatch 覆盖 + 数据模型）。

## Slice C2 (M4 契约): media_harvester 真下载贯通 (done)

- 能力名: media_download_enforcement（确认 media_harvester 真下载文件 + 写 manifest，不走 URL-as-row）。
- 现状验证：harvester.py 调用 download_candidate + write_manifest；agent_hook.py 识别 6 种 media output_kind；HarvestReport 含 manifest_appended 计数。
- Tests: tests/test_manifest_enforcement.py 6 例（源码锚定 + 数据模型 + 序列化）。
- 验证: C1 9✓ + C2 9✓ + manifest roundtrip 3✓ = 18 通过 0 失败（合并在 test_manifest_enforcement.py）。

## Slice B1 (M4 收口): 体量基线 + M4 收口 (done)

- 能力名: file_size_baseline（体量回归防线 + M4 完整性验证）。
- 新增 tests/test_file_size_baseline.py 6 例：
  - main.py < 18000 行（从 18593 降至 17595，锁定 -1000 行减量）
  - browser_env.py < 5000 行（4769，锁定）
  - api_server.py < 2500 行（2136，锁定）
  - phases/ 包含全部 6 个子模块
  - som_injector.py 存在
  - api_routes/ 包含 spider_api.py
- M4 里程碑收口：G0-G7 + C1-C2 + B1 共 12 个 slice 全部完成。

## Slice C3 (M5 契约): output_contract 兜底消除 (done)

- 能力名: output_contract_enforcement（消除 save_run_dataset 中绕过 save_artifact 的 legacy 路径）。
- 影响层: data_writers/dispatch.py save_run_dataset → save_artifact 统一路由。
- 改动:
  - 新增 `_merge_and_dedupe_xlsx` + `_coerce_to_rows`：读已有 xlsx、合并、按 unique_key 去重。
  - 支持 tooltip 动态 key 和普通列名 key 两种去重。
  - 删除 `save_to_excel` 回退分支：所有路径经 `save_artifact` → `finalize_file_artifact` → manifest。
  - L187 `or "files_folder"` 已确认无需改动（非 `or "xlsx"`）。
- 新增 contract 字段: 无。
- Tests: tests/test_data_writers_dispatch.py 更新 1 例 + 新增 1 例（dedup 回归）。
- 验证: 30 passed（dispatch）+ 3850 passed 全量 0 failed。

## Slice T1 (M5 治理): test 文件归位 (done)

- 能力名: test_file_consolidation（visual_web_agent/ 下 10 个 test_*.py 归入 tests/）。
- 改动:
  - 删除 7 个 ad-hoc 调试脚本（test_baidu_login / test_focus / test_wrapper_click / test_som / test_som_baidu / test_eval / test_mb）。
  - 迁移 test_data_manager.py → tests/test_data_manager_alignment.py（修复 import）。
  - 合并 test_data_sanitizer.py 内容到 tests/test_data_sanitizer.py（+10 个测试）。
  - 迁移 test_new_modules.py → tests/test_new_modules.py（修复 12 处 bare import + 2 处 Path）。
- 新增 contract 字段: 无。
- Tests: 全量从 3766 → 3850（+84 个归位测试），0 failed。

## Slice R1 (M5 注册表): action_registry 元数据 + 派发扩展 (done)

- 能力名: registry_metadata_expansion（VLM 原语元数据注册 + 热循环派发白名单扩展）。
- 影响层: action_registry.py + main.py `_registry_dispatch_actions`。
- 改动:
  - `_registry_dispatch_actions` 新增 `date_pick` / `cascader_pick`（已绑定 handler 但缺白名单）。
  - 新增 16 个 VLM 原语元数据注册：click_text / type_text / scroll_page / click_point / click_new_tab / press_key / find_text / wait_action / goto_url / switch_tab / close_tab / select_option / done_signal / ask_human / drag_and_drop / save_to_memory。
  - 仅元数据注册，执行路径不变（仍走 browser.execute_action）。
- 新增 contract 字段: 无。
- Tests: 63 passed（registry + router）+ 3850 passed 全量 0 failed。

## Slice R2 (M5 注册表): extract 抽离 main.py (pending, P1)

- 能力名: extract_action_extraction（~800 行 extract 逻辑从 main.py 移入 actions/extract_action.py）。
- 影响层: main.py ~L12601-13366 → actions/extract_action.py。
- 计划: 3 步——① 定义 ExtractContext + run_extract 签名; ② 移代码; ③ 接入注册表。
- 风险: 高（闭包大量局部变量，核心数据路径）。
- 前置: C3 + R1 已完成。

## Slice S1a (M5 拆分): ExtractState 归集抽取计数器 (done, P1)

- 能力名: extract_state_grouping（run_agent 内 ~19 个 run 级抽取计数器收进共享 `ExtractState` 实例，破解 extraction 闭包 / 主 step 循环双向耦合，为 S1b-S1d 平移 extraction 闭包铺路）。
- 影响层: data_plane（新建 `extraction_engine/runtime.py` + `main.py::run_agent`）。
- 改动:
  - 新建 `extraction_engine/runtime.py::ExtractState`（dataclass，19 字段；set 字段用 `field(default_factory=set)` 防可变默认 bug）。
  - `main.py:2284-2311` 用 `_xs = ExtractState()` 替换 19 处局部初始化；run_agent 全函数体（闭包段 + 主循环段）对这 19 个 `_xxx` 标识符的读写字节级 patch 改写为 `_xs.xxx`（共 317 处引用，0 处遗留裸引用，`nonlocal` 声明同步清理）。
  - 纯平移零行为改动：`ExtractState` 字段名 = 原局部名去前导下划线。
- 新增 contract 字段: 无。
- Tests: `tests/test_extract_runtime.py` 4✓（标量默认值 / set 默认空 / 别名共享可变可见 / 每实例独立 set）；`main.py` `py_compile` 通过；全量 pytest 3879 passed / 2 skipped / 0 failed。
- 计划: `docs/superpowers/plans/2026-06-18-main-py-decomposition.md`（S1a→S1e / S2 / S3 / B）。

## Slice S1b (M5 拆分): DOM 抽取读闭包 → ExtractRuntime 方法 (done, P1)

- 能力名: extract_runtime_dom_readers（6 个只读 DOM 抽取闭包从 `run_agent` 平移进 `extraction_engine/runtime.py::ExtractRuntime`，构造注入 `ExtractDeps`，主循环调用点经薄包装保持稳定）。
- 影响层: data_plane（`extraction_engine/runtime.py` + `main.py::run_agent`）。
- 前置: S1a 已完成（`ExtractState` 共享计数器就位）。
- 改动:
  - 新建 `ExtractDeps`（`browser` / `logger` / `evaluate_rows_with_frame_fallback` / `normalize_extracted_row_fields`）+ `ExtractRuntime`（持 `ExtractDeps` + 共享 `ExtractState`）。
  - 6 个闭包平移为方法：`extract_list_rows_via_dom` / `extract_visible_table_rows_via_dom` / `visible_table_signature` / `auto_advance_table_page_via_dom` / `probe_scroll_drain_state` / `detect_canvas_grid`（内联 JS 整段照搬，`browser`→`self.deps.browser`，跨方法调用→`self.<method>`；6 者均只读探针，不写 state）。
  - `main.py:3520-3528` 构造 `_extract_deps` + `_extract_rt`；原 6 个闭包改为薄委托包装（签名不变，所有调用点零改动）；闭包体移出后 main.py 净减约 695 行。
  - 纯平移零行为改动。
- 新增 contract 字段: 无。
- Tests: `tests/test_extract_runtime.py` 追加 10 例（stub-frame：list/table 读取 / signature 显式 scope / autopager 翻页 / scroll-drain / canvas found+not-found），合计 14✓；6 个源码扫描测试（`test_autopager_shadow` / `test_extract_canvas_fallback` / `test_extract_shadow_list` / `test_extract_shadow_table` / `test_extract_table_iframe` / `test_table_autopager_frames`）改为同时 `inspect.getsource(run_agent)+ExtractRuntime`（JS 标记随闭包迁移）；`py_compile` 通过；`validate_y S1b` 全绿（target 14 / build / core 112 / 全量 pytest 3889 passed / 2 skipped / 0 failed）。
- 计划: `docs/superpowers/plans/2026-06-18-main-py-decomposition.md`（S1b）。

## Slice S1c (M5 拆分): 抽取候选仲裁 + 字段归一闭包 → ExtractRuntime 方法 (done, P1)

- 能力名: extract_runtime_arbiter_normalizer（14 个仲裁/归一/字段工具闭包从 `run_agent` 平移进 `ExtractRuntime`，run 级句柄经 `ExtractDeps` 注入，模块助手直接 import）。
- 影响层: data_plane（`extraction_engine/runtime.py` + `main.py::run_agent`）。
- 前置: S1b 已完成。
- 改动:
  - 平移 14 闭包为方法：`sanitize_extraction_candidate` / `expected_rows_from_data_shape` / `candidate_min_expected_rows` / `is_under_yield_viewport_candidate` / `choose_best_extraction_candidate` / `commit_extraction_candidate` / `record_extract_progress` / `field_aliases` / `requested_field_coverage` / `project_row_to_requested_fields` / `normalize_extracted_row_fields` / `first_int_value` / `classify_url_role` / `is_probable_url`。
  - `ExtractDeps` 增 4 个 run 级句柄（`goal` / `goal_output_mode` / `requested_output_fields` / `data_controller`）；移除 S1b 的 `normalize_extracted_row_fields` 回调（normalize 升为方法，S1b reader 改调 `self.normalize_extracted_row_fields`）。
  - `runtime.py` 直接 import 模块助手：`_parse_goal_target_count` / `_goal_is_tooltip_extract` / `_normalize_output_field_key`（`phases.goal_parser`）、`sanitize_extracted_rows` / `extract_tooltip_primary_key`（`data_sanitizer`）。
  - `main.py` 14 闭包改薄委托包装（签名不变，所有调用点零改动）；闭包体移出后净减约 410 行。字节级 patch（CRLF，`_patch_s1c_*.py` 即用即删）；纯平移零行为改动。
- 新增 contract 字段: 无。
- Tests: `tests/test_extract_runtime.py` 追加 13 例，合计 27✓；`py_compile` 通过；全量 pytest **S1c 新增 0 失败**（3880 passed，+13 S1c 新测全过；当时另有并发 UI agent 的 App.vue 重构失败，与本切片无关）。
- 提交: `3109036`（仅 Python；当时 `backlog.md` 被并发 UI agent 占用，本条目延后补录）。
- 计划: `docs/superpowers/plans/2026-06-18-main-py-decomposition.md`（S1c）。

## Slice S1d (M5 拆分): 抽取快路径 + 文本/收尾相邻闭包 → ExtractRuntime 方法 (done, P1)

- 能力名: extract_runtime_fastpath_text（11 个快路径/文本抽取闭包 + 2 个传递链接助手从 `run_agent` 平移进 `ExtractRuntime`；3 个 nonlocal 收尾闭包按设计留 `main.py`）。
- 影响层: data_plane（`extraction_engine/runtime.py` + `main.py::run_agent`）。
- 前置: S1c 已完成。
- 改动:
  - 平移 13 方法：`capture_body_text_excerpt` / `save_extraction_snapshot` / `try_dom_api_fast_path` / `xhr_saved_row_count` / `xhr_target_reached` / `enrich_rows_with_dom_links` / `extract_compact_list_text_via_dom` / `extract_full_page_text_for_data` / `extract_body_text_for_semantic_cards` / `inspect_click_target_for_extract_nav_guard` / `nudge_scroll_after_duplicate_extract` / `compact_link_match_text` / `row_primary_link_text`。
  - 留在 `main.py` 的 3 个 nonlocal finalizer：`_finish_if_xhr_target_reached` / `_finish_if_file_download_completed` / `_try_pre_extract_fast_path`（写 run 控制流标志，属 run 生命周期；调用图确认 finalizer→可搬方法为单向依赖，留 3 搬 13 安全）。
  - `ExtractDeps` 增 6 个 run 级句柄（`event_stream` / `run_ts` / `snapshot_goal` / `goal_output_contract` / `vlm_output` / `enable_xhr`，均带默认值，兼容既有测试构造）；`runtime.py` import `maybe_save_snapshot` / `save_run_dataset` / `resolve_artifact_path` / `nudge_virtual_scroll` / `_goal_is_bulk_extraction`。
  - 2 个带 `step` 的闭包（snapshot / dom_api_fast_path）：`step` 经 `main.py` wrapper 转发为方法参数（行为不变）。
  - `main.py` 13 闭包改薄委托包装；闭包体移出后净减约 600 行。字节级 patch（CRLF，`_patch_s1d_*.py` 即用即删）；纯平移零行为改动。
- 新增 contract 字段: 无。
- Tests: `tests/test_extract_runtime.py` 38✓（+11 S1d）；`tests/test_virtual_scroll_nudge.py::TestMainWiring::test_dedup_nudge_falls_back_to_virtual_scroll` 因 nudge 搬家改读 `run_agent`+`ExtractRuntime` 源，44✓；抽取子集 67✓；全量 pytest **S1d 新增 0 失败**（30 个失败全是并发 UI/api 重构，与本切片无关）。
- 提交: `6c5cfbb`（仅 Python；本条目延后补录）。
- 计划: `docs/superpowers/plans/2026-06-18-main-py-decomposition.md`（S1d）。

## Slice S1e (M5 拆分收口): 抽取子系统体量基线 + S1 收口 (done, P1)

- 能力名: extraction_subsystem_baseline（S1a-S1d 把 extraction 子系统从 `run_agent` 切到 `extraction_engine/runtime.py` 后的体量回归守卫 + S1 阶段收口）。
- 影响层: data_plane / 测试基线（`tests/test_file_size_baseline.py`）。
- 前置: S1a-S1d 已完成（`ExtractRuntime` 现 33 方法：6 DOM 读 + 14 仲裁/归一 + 13 快路径/文本）。
- 改动:
  - `test_file_size_baseline.py`：main.py 行数守卫 18000 收紧为 10200（实测 10019，锁定 S1 减约 1600 行的成果；S2/S3 再逐步收紧）；新增 `test_extraction_runtime_carved_out` 断言 `runtime.py` 存在且含 `ExtractState`/`ExtractDeps`/`ExtractRuntime`。
  - 与计划偏差: 计划估算 main.py `<9500`，实际 10019——因 3 个 nonlocal 收尾闭包（`_finish_if_xhr_target_reached` / `_finish_if_file_download_completed` / `_try_pre_extract_fast_path`）按设计留在 main.py（属 run 生命周期，待日后 RunState/phases 切片再迁），且原行数估算用 `Measure-Object`（CRLF 下偏低 ~350 行）。
- 新增 contract 字段: 无。
- Tests: `tests/test_file_size_baseline.py` 7✓。
- S1 阶段小结: extraction 闭包族（DOM 读 / 仲裁归一 / 快路径文本）已全量迁入 `ExtractRuntime`（33 方法）；main.py `run_agent` 由 ~11600 降至 10019（Python 行）；S1a-S1d 四刀均纯平移零行为改动，每刀经 ExtractRuntime 单测 + 抽取回归 + 全量 pytest 零新增 Python 失败验证。
- 计划: `docs/superpowers/plans/2026-06-18-main-py-decomposition.md`（S1e）。

## Slice S2 (M5 拆分): 动作分发迁注册表 (done, P1 — 早由 actions/ 达成)

- 能力名: action_dispatch_to_registry（计划拟新建 `actions/dispatch_table.py` 把 `run_agent` 内 `if action == ...` 分发链迁出）。
- 影响层: operations_plane（`action_registry.py` + `actions/`）。
- 前置: S1 已完成。
- 结论: **跳过新建 `dispatch_table.py`，目标早已由既有 `actions/` 模块化达成**。复核 `main.py` 残留的 `action ==` 全是**循环控制守卫**（强制 next_page / 首翻 / click_text handoff / done 后处理等 run_agent step 逻辑），属生命周期控制流，**不是动作分发**；动作分发本就走 `action_registry` + `actions/<cap>.py` handler。再建 `dispatch_table.py` 为冗余层，按「能用既有就不新增」铁律不做。
- 与计划偏差: 计划的 S2a 建 `dispatch_table.py` 判定为冗余，已征用户同意「跳过 S2 转 S3」。
- 新增 contract 字段: 无。
- Tests: 无（无代码改动，仅复核 + 决策）。
- 计划: `docs/superpowers/plans/2026-06-18-main-py-decomposition.md`（S2，重定义为「已达成」）。

## Slice S3 (M5 拆分): 起手/handoff 闭包迁 phases/setup.py (done, P1)

- 能力名: setup_phase_extraction（`run_agent` 起手段 9 个闭包平移进 `phases/setup.py::SetupTools`）。
- 影响层: execution_kernel（`visual_web_agent/main.py` → `visual_web_agent/phases/setup.py`）。
- 前置: S1 已完成、S2 已跳过。
- 改动:
  - 平移 9 闭包 → `SetupTools(SetupDeps)`：`browser_action_tool` / `targeted_probe_tool` / `try_targeted_click_text_handoff` / `resolve_type_value_for_handoff` / `try_targeted_type_handoff` / `check_stop` / `abort_if_stale_auth` / `with_tool_metadata` / `recover_active_page`。
  - 无 nonlocal（不同于 S1d finalizer）：`_check_stop` / `_recover_active_page` 靠 raise 异常控制流，方法内 raise 行为不变。
  - `SetupDeps` 注入 browser / goal / vlm / logger / event_stream / stop_event / start_url / action_registry / selected_tools + 2 个传递回调 `_broadcast_log_safe` / `_broadcast_done_safe`；模块助手在 setup.py import。
  - 接线点：`_browser_action_tool` 原 `action_registry.bind("next_page", ...)` 改绑 `_setup.browser_action_tool`；`_setup` 在 action_registry 之后、bind 之前构造（deps 就绪）。main.py 闭包改薄包装保签名（含 3 个 registry bind 零改），净减约 280 行。字节级 patch（CRLF，即用即删）；纯平移零行为改动。
- 新增 contract 字段: 无。
- Tests: `tests/test_phase_setup_split.py` 10✓；run_agent import + 抽取 sanity 92✓；全量 pytest S3 零新增 Python 失败（35 失败全是并发 UI/api 重构，与本切片无关）。
- 提交: `4b759b0`（新建 `phases/setup.py` + main.py + test_phase_setup_split.py，4 文件 +610/−383）。
- 计划: `docs/superpowers/plans/2026-06-18-main-py-decomposition.md`（S3）。

## Slice B (M5 拆分收口): main.py 体量基线刷新 + S2/S3 收口 (done, P1)

- 能力名: main_py_baseline_refresh（S3 后 `main.py` 体量回归守卫收紧 + S2/S3 阶段收口留痕）。
- 影响层: 测试基线（`tests/test_file_size_baseline.py`）+ 文档（本 backlog）。
- 前置: S3 已完成。
- 改动:
  - `test_file_size_baseline.py`：`main.py` 行数守卫由 10200 收紧为 **9700**（实测 9669，锁定 S3 减约 280 行的成果），`test_main_py_below_10200` → `test_main_py_below_9700`；新增 `test_phase_setup_carved_out` 断言 `phases/setup.py` 存在且含 `SetupDeps` / `SetupTools`（对齐 S1 的 `test_extraction_runtime_carved_out`）。
  - 本 backlog 补录 S2（跳过/已达成）、S3（`4b759b0`）、B（本条）三条。
- 与计划偏差: 计划终态 `main.py < 5500` **不可达**——因 S2 的 ~6380 行 dispatch 早不在 `main.py`（前序 `actions/` 重构已迁），计划行数前提过时。9669 为 S1+S3 后的真实终态。
- main.py 解构整体收官: S1（extraction→ExtractRuntime，5 commit）+ S2（dispatch 早由 actions/ 达成，跳过）+ S3（setup→phases/setup.py，`4b759b0`）；`main.py` ~11600 → **9669**（Python，累计 −~1930）。
- 新增 contract 字段: 无。
- Tests: `tests/test_file_size_baseline.py` 8✓（含新增 `test_phase_setup_carved_out`）。
- 计划: `docs/superpowers/plans/2026-06-18-main-py-decomposition.md`（收口 B）。

## Slice C4 (M5 契约): input_contract 落地 (done, P1)

- 能力名: input_contract_enforcement（每个 run 产出 input_contract.json）。
- 影响层: main.py run 初始化 → io_contract/persistence.py。
- 前置: C3 已完成。
- 改动: 三大入口均已落 input_contract.json——
  - CLI / 即席 / replay: main.py:870 `ensure_input_contract_skeleton`（仅缺失时写，smart_batch 的富契约仍优先）。
  - 批处理 / API: smart_batch_runner._persist_io_contracts_safe → write_input_contract（api_server._run_batch_task → run_smart_batch_sync）。
  - 能力 API: capability_api.py:133 write_input_contract（带 run_id 时）。
  - 落盘前 secret_redaction 脱敏；atomic temp + os.replace 写。
- 新增 contract 字段: 无（沿用 input_contract.v1 既有结构）。
- Tests: test_io_contract_persistence.py + test_io_contract.py + test_workflow_graph_input_contract.py 全绿。

## Slice X1 (M5 调度): 跨系统 context 切换 (done, P2)

- 能力名: cross_system_dispatch（按 system_id 切 BrowserSession context / storage_state）。
- 影响层: workflow_graph cross_system risk_flag → execution_kernel（main.py 热循环 + phases/rpa_replay.py）。
- 前置: C4 已完成。
- 改动: cross_system_enabled() 默认 True——
  - main.py:4912-5129 E1b 运行时系统观测 → E1c-3b acquire_for_switch + storage_state 暂存 → 物理 rebind active browser → E1c-A1 goto 拦截切换。
  - session_router / browser_session_pool / run_system_tracker 支撑按 (run, system, auth) 三元组切 BrowserSession。
  - RPA 回放 phases/rpa_replay.py 同步按 system_id 切 browser（_replay_switch_system）。
  - Bugfix: rpa_replay.py 的 `semantic_macros` import 误指 `phases/` 包，改为对齐同目录 auto_form.py 的三级 import（首选父包 `visual_web_agent.semantic_macros`）；否则跨系统 RPA 回放在运行时 ModuleNotFoundError（X1 默认开 → 真实命中）。
- 新增 contract 字段: 无。
- Tests: 修前 test_rpa_cross_system_replay 3 failed（ModuleNotFoundError）；修后跨系统全量 307 passed（session_router / browser_session_pool / route_executor_cross_system / xsys_dual_login_e2e / xsys_contract_to_relay_e2e / goto_cross_system / run_system_tracker / rpa_xsys_event_stream）。

## Slice G8 (M5 拆分): form_engine 抽离 main.py (pending, P3)

- 能力名: form_engine_extraction（~3500 行 form 引擎从 main.py 移入 phases/ 或独立模块）。
- 影响层: main.py → form_engine.py / phases/form.py。
- 前置: R2 已完成。

## Slice SEARCH-NAV (M4 通用): 搜索优先 + 落地去广告导航 (done, P1)

- 能力名: open_top_search_result（无 URL 落到 SERP 时，确定性打开首个非广告有机结果并导航）。
- 影响层: search_result_guards.py（top-N 探针 + is_ad_redirect_url + open_top_organic_result）→ search_nav_action.py（handler）→ action_registry / capability_router / prompt_skills / prompt_helpers / prompts / vlm_models。
- 关键设计: 复用既有有机结果挑选器（adRe 广告过滤 + 排 nav/侧栏/搜索引擎自链）；新增落地二次校验（doubleclick / gclid / aclk 等）+ 候选轮替。
- 回归: tests/test_search_nav_action.py（stub-frame：广告 href 跳过 / 落地广告跳转跳过 / 同搜索域跳过 / 全广告失败 / 无候选）。
- 风险: 低（新增能力，未改既有动作语义；guard 触发条件未放宽）。

## Slice D-UI-1 (M4 简便): markdown 渲染器从 App.vue 抽离 (done, P3)

- 能力名: markdown_render_extraction（轻量 Markdown 渲染器从 App.vue <script setup> 抽成可复用/可测模块）。
- 影响层: vspider-ui/src/App.vue → vspider-ui/src/composables/markdownRender.js（纯函数 escapeHtml + renderMarkdown）。
- 改动: App.vue 删除内联 89 行（注释 + escapeHtml + renderMarkdown），改为 `import { renderMarkdown }`；两处 computed（finalAnswerHtml / displayedFinalAnswerHtml）调用点不变；行为零变更。
- 动机: App.vue 仍 ~4150 行巨兽（mission §三 文件体量警戒线）；renderMarkdown 此前困在 script setup 内无法单测，抽出后可测、可复用（§四 可测）。
- 新增 contract 字段: 无。
- Tests: 新增 vspider-ui/tests/markdownRender.test.js 11 例（escapeHtml / 标题 / 粗斜体 / 行内+围栏代码 / 链接白名单+XSS 兜底 / 列表 / 段落）；vspider-ui vitest 28 passed；npm run build 绿（1684 模块，+1）。
- 风险: 低（纯函数平移，无逻辑变更；index bundle 体积不变 187.67kB）。
- 后续 slice 候选: useModelSettings（模型配置）/ useWebSocket（WS 连接）/ usePhaseTrace（相位轨迹导出+过滤）/ useKeyboardCommand（快捷键+命令面板）/ scoped CSS 抽离（~981 行）。

## Slice D-UI-2 (M4 简便): 模型配置从 App.vue 抽离 (done, P3)

- 能力名: model_settings_extraction（VLM + 语义模型配置状态与持久化抽成 composable）。
- 影响层: App.vue → composables/useModelSettings.js（selectedModel/Semantic、temperature/maxTokens、base/apiKey、远程模型拉取、load/save、selectedModelType）。
- 改动: App.vue 改为 `import { useModelSettings }` 解构；localStorage 白名单持久化（apiKey 永不落盘）；行为零变更。
- 新增 contract 字段: 无。
- Tests: tests/useModelSettings.test.js（selectedModelType 推断 / 持久化白名单 round-trip / fetchRemoteModels happy+guard）。
- 风险: 低（响应式状态平移，无逻辑变更）。

## Slice D-UI-3 (M4 简便 + M2 高效): WebSocket 生命周期从 App.vue 抽离 (done, P3)

- 能力名: websocket_lifecycle_extraction（连接 / 自动重连 / 卸载清理抽成 composable，业务消息路由经 onMessage 回调留 App.vue）。
- 影响层: App.vue → composables/useWebSocket.js；App.vue 删除 socket/reconnectTimer/isUnmounted/wsStatus 与 wsUrl import，onMounted→connectWebSocket()、onUnmounted→disconnectWebSocket()；原 onmessage 体抽成 handleSocketMessage（仍持有全部组件状态）。
- 改动: 行为逐字保留（连接守卫 OPEN/CONNECTING、2s 重连、卸载阻断重连）。
- 新增 contract 字段: 无。
- Tests: tests/useWebSocket.test.js 10 例（mock WebSocket + 假定时器：open/message/error/重连/连接去重/卸载守卫）；vitest 38 passed；npm run build 绿（1686 模块）。
- 风险: 低（生命周期平移，message 路由不动）。

## Slice D-UI-4 (M4 简便 + M1 准确): 相位轨迹逻辑共享化 + App.vue 死代码清除 (done, P2)

- 能力名: phase_trace_shared_composable + appvue_dead_phase_removal。
- 影响层: 新增 composables/usePhaseTrace.js（filteredPhaseEvents / phaseTimelineGroups / phaseSummary / phaseFilterOptions / phaseStatsSorted / severityFilterOptions / phaseFilterActive / completionEvidence + 纯助手 phaseChipStyle·Label·Detail / phaseSparklinePath / formatPhaseStatMs / toggle·reset）；TimelinePanel.vue 改用 `usePhaseTrace(toRef(props,'phaseEvents'))`，删内联 ~220 行。
- 关键发现: App.vue 内同名相位过滤/统计/chip cluster 自上次 TimelinePanel 组件化后已无任何活引用（template 起于 2920 行，全文 grep 证实仅互相引用），系遗留死代码 → 删除 ~250 行（A1 状态声明 / A2 filteredPhaseEvents / A3 大块 ~10KB / A4–A7 四处 reset 死写）。
- 新增 contract 字段: 无。
- Tests: tests/usePhaseTrace.test.js 16 例（过滤/toggle/reset/summary/stats 排序 null 下沉/severity/分组/completion/chip 助手 + notice_severity 升级）；vitest 54 passed（6 文件）；npm run build 绿（1687 模块，index js 188.54→187.26kB）。
- 风险: 低-中（TimelinePanel 为唯一活路径，行为逐字保留；App.vue 删除经全文 grep + build 双重确认无悬空引用）。

## Slice D-UI-5 (M4 简便 + M1 准确): 全局键盘快捷键派发器从 App.vue 抽离 + 修复 Ctrl+E bug (done, P2)

- 能力名: keyboard_command_extraction（全局 keydown 派发器抽成可单测的纯 resolver + 监听生命周期 composable）。
- 影响层: 新增 composables/useKeyboardCommand.js（纯 `resolveKeyboardAction(event, ctx)` + `isTypingTarget` + `useKeyboardCommand({getContext, actions})` 自挂/卸 window keydown）；App.vue 删除 `_isTypingTarget` + `handleGlobalKeydown`（~135 行）与 onMounted/onUnmounted 的 add/removeEventListener，改为注入 keyboardActions + getKeyboardContext。
- 改动: 派发逻辑逐字保留（primary=ctrl||meta、Ctrl+Enter/终端 Ctrl+F 输入态仍生效、其余输入态抑制、Ctrl+数字越界不拦截、相位/失败对话框方向键、终端搜索 Esc/Enter、timeline/capability 标签 Ctrl+E 与 End/Home）。
- 顺带修复既有 bug: 原 handleGlobalKeydown 在 timeline 标签 Ctrl+E 调用 `exportPhaseEventsAsJsonl()`，但该函数自 TimelinePanel 组件化后 App.vue 内从未定义（HEAD 即如此，Ctrl+E 会 ReferenceError）→ 现 TimelinePanel `defineExpose` 暴露 `exportPhaseEventsAsJsonl`，App.vue 经 `timelinePanelRef.value?.exportPhaseEventsAsJsonl()` 调用，快捷键恢复可用。
- 备注: 命令面板（slash 命令）此前已是独立 composable `useSlashCommand` + `CommandPalette.vue` + `registerBuiltinCommands`，本 slice 不动其 17-依赖薄接线（避免 omnibus）。
- 新增 contract 字段: 无。
- Tests: 新增 tests/useKeyboardCommand.test.js 15 例（isTypingTarget + resolver 穷举：输入态豁免/抑制、全局键、Ctrl+数字越界、对话框方向键、终端搜索、标签级导出/滚动、未绑定键）；vitest 69 passed（7 文件）；npm run build 绿（1688 模块）。
- 风险: 低（resolver 纯函数可测且逐字对照原 if-chain；动作经注入，listener 生命周期由 composable 自管；bug 修复为既有缺陷的最小闭合）。

## Slice D-UI-6 (M4 简便): App.vue scoped 样式抽到外部 CSS (done, P3)

- 能力名: appvue_scoped_css_externalize（App.vue 的 981 行 `<style scoped>` 抽到 styles/app.css，沿用仓内 `<style src scoped>` 组件样式约定，如 TimelinePanel/FinalAnswerPane 等）。
- 影响层: App.vue → styles/app.css；App.vue 的 `<style scoped>…</style>`（原 2914–3895）整体替换为 `<style src="./styles/app.css" scoped></style>`。
- 改动: 行为零变更——Vue SFC 编译器对 `<style src scoped>` 仍按 App.vue 组件作用域加 data-v 哈希，`:global()` 等特性照常；App.vue 由 ~3896 行降至 ~2915 行。
- 新增 contract 字段: 无。
- Tests: 无新增（纯 CSS 改动）；vitest 69 passed 不变；npm run build 绿，**index CSS 87.68kB 字节完全不变**（证明样式零丢失、作用域完整），index js 188.02kB 不变。
- 风险: 低（纯样式平移 + 既有 src-scoped 约定；CSS bundle 字节一致双重确认）。

## 方向D（前端 App.vue 拆分）阶段小结

- D-UI-1 markdownRender / D-UI-2 useModelSettings / D-UI-3 useWebSocket / D-UI-4 usePhaseTrace(共享化+死代码清除) / D-UI-5 useKeyboardCommand(+Ctrl+E bug 修复) / D-UI-6 scoped CSS 外置。
- 累计：App.vue 从 ~4150 行降至 ~2915 行（script ~2900→约 1930，<style> 981 行外置）；新增 6 个 composable + 1 个 styles/app.css；vspider-ui vitest 由 28 → 69 例；每步 npm run build 均绿。
- 后续候选：useTaskSubmit 链路已部分独立（composables/useTaskSubmit.js）；可继续按 phase-trace 模式审查 App.vue 是否还有「组件化后遗留死代码」；或抽 capability-trace 相关 computed 群。

## Slice D-UI-7 (M1 准确 + M4 简便): App.vue 组件化遗留死代码全量清除 (done, P2)

- 能力名: appvue_dead_code_sweep（迭代死代码分析 + 全量删除上次组件化后遗留、无任何活引用的顶层符号）。
- 影响层: 仅 App.vue `<script setup>`（template / style / 子组件均未动）。
- 审计方法: 脚本提取 240 个顶层声明 → 统计「去注释全文（script+template）仅定义处出现」(count==1=直接死) → 迭代消除「引用全部落在死块内」者(传递死)；App.vue 无 defineExpose，模板按 rfind 取根 `</template>` 避免具名插槽截断。
- 结论与删除: 34 个死符号、合并 14 个区间、共删 **322 行**（含紧邻注释/空行）。分布：Final Answer(→FinalAnswerPane.vue)、Timeline(→TimelinePanel.vue)、Capability 执行遥测(→CapabilityExecutionTelemetry/ReplayPane)、authProfileNames(→AuthDialog)。代表：completionEvidence/latestCompletionGuard、displayedFinalAnswerHtml/copyFinalAnswerToClipboard/exportFinalAnswerAsMarkdown、capabilityExecution{Attempts/Checks/Verification/RuntimeAfter/...}、scrollTimelineToTop/Bottom/_timelineWrap/_isAtBottom 等。
- 新增 contract 字段: 无。
- Tests: 删后全文 grep 34 死名 0 残留；活邻符号（finalAnswerText/Expanded/Status、timelineAutoScroll/timelinePanelRef、authProfileOptions 等）存活；ReadLints clean；npm run build 绿（1688 模块，index js **188.02→185.54kB**，-2.48kB）；vitest 69 passed 不变。
- App.vue 行数: 2915 → **2593 行**（-322）。本会话累计 ~4150 → 2593（-1557）。
- 风险: 低（迭代分析 + count==1 全文双重确认无活引用；仅删 script 死块，template/style 零触碰；build+vitest+lint 三绿）。

## Slice D-UI-8 (M4 简便 + M1 准确): capability-trace 派生层抽离 + 修复跨组件 ReferenceError (done, P2)

- 能力名: capability_trace_extraction（route/execute 事件 → intent/plan/rows/summary/health 等 44 个派生 computed 抽成 useCapabilityTrace）+ phase_preview_format 共享化。
- 影响层: 新增 composables/useCapabilityTrace.js（`useCapabilityTrace(phaseEvents)` 返回 44 computed + 过滤态 capabilityTraceFilter/SearchQuery + 内含 buildPhaseEventJsonString 纯助手）；新增 composables/phasePreviewFormat.js（formatPhasePreviewSeverity/Ts 共享纯函数）；App.vue 改用 destructure 接线、删除 capabilityTraceUtils 死 import；FailedRunsPane.vue 改从 phasePreviewFormat 导入（去重）。
- 抽离方法: 脚本依已验证精确行跨度**逐字提取** 601-746 + 834-1130 + 1168-1189（跳过 browserRuntime* 活代码），零转写；composable 保持原定义顺序（computed 惰性求值，引用关系不变）。
- 顺带修复既有 bug: capabilityTraceRows 自 HEAD 起即引用「从未在 App.vue 定义」的 formatPhasePreviewSeverity/Ts（仅 FailedRunsPane 有定义），命中即 ReferenceError。抽离后由 TDD 冒烟测试暴露 → 抽 phasePreviewFormat 共享 util 修复。
- 新增 contract 字段: 无。
- Tests: 新增 tests/useCapabilityTrace.test.js 9 例（选取/intent/rows/summary/filter/反应性 + 全 46 返回值「求值不抛错」冒烟，含空事件）；vitest 78 passed（8 文件）；npm run build 绿（1690 模块）。
- App.vue 行数: 2593 → **2146 行**（-447）。本会话累计 ~4150 → 2146（约 -48%）。
- 风险: 低-中（44 computed 逐字平移 + 依赖分析 + 冒烟测试穷举求值确认无悬空引用；跨组件 bug 一并闭合）。

## Slice D-UI-9 (M4 简便): 时间线离线回放抽离 useTimelineReplay (done, P3)

- 能力名: timeline_replay_extraction（phase_<id>.jsonl 解析 + 进入/退出回放状态机抽成 composable）。
- 影响层: 新增 composables/useTimelineReplay.js —— 模块级纯函数 `parsePhaseReplayJsonl(text)`（容忍空行/坏 JSON/非 dict，支持 detail.phase_event 解包与单 capability_execute_trace 文档）+ `useTimelineReplay({ phaseEvents, hasNewCapability, setActiveBottomTab })`（持 replayMode/replaySourceName/replayInputRef/replayImportTarget + triggerReplayImport/exitReplayMode/handleTimelineImportReplay）。App.vue destructure 接线，`setActiveBottomTab` 以 wrapper arrow 延迟引用（其定义晚于接线点）。
- 改动: 逐字平移，行为不变（WS replayMode 门控、resetForNewTask 复位、capability/timeline 双入口均经返回 ref/函数保持）。
- 备注: 既有现象——App.vue 的 replayInputRef 从未在模板绑定，triggerReplayImport 实为 no-op（capability 回放导入入口未接），本 slice 原样保留（不在本次修），仅记录。
- 新增 contract 字段: 无。
- Tests: 新增 tests/useTimelineReplay.test.js 9 例（解析器空行/坏行/非 dict/detail 解包/单 trace 文档 + 状态机导入/容错/切 tab/退出）；vitest 87 passed（9 文件）；npm run build 绿（1691 模块）。
- App.vue 行数: 2146 → **2033 行**（-113）。本会话累计 ~4150 → 2033（约 -51%）。
- 风险: 低（纯解析器 + 状态机平移，依赖经注入，单测覆盖解析与状态迁移）。

## Slice D-UI-10 (M4 简便): capability fixture/efficiency 重放子系统抽离 useCapabilityFixtureReplay (done, P2)

- 能力名: capability_fixture_replay_extraction（failure fixture / efficiency feedback 的生成·重放·拉取·批量重放 + 派生 computed + capabilityReplayPaneProps 打包，整体抽成 composable）。
- 影响层: 新增 composables/useCapabilityFixtureReplay.js（50 个符号：17 state + 25 派生 computed + buildSummaryText + capabilityReplayPaneProps + copySummary + 7 异步动作）。App.vue 仅 destructure 9 个（capabilityReplayPaneProps + 8 动作），并删除现已不用的 buildFailureFixtureBatchReplaySummaryText import。
- 依赖注入: url / prompt（ref 直传）、fetchArtifacts / writeToClipboard（wrapper arrow，后者 _writeToClipboard 定义晚于接线点）、latestCapabilityExecute / capabilityExecutionFailureBundle / capabilityExecutionEfficiencyCorrelationReport（来自 useCapabilityTrace 的 computed，接线点在其 destructure 之后）。
- 抽离方法: 脚本逐字提取 3 个连续块（state 154-170 / computed 633-718 / 函数 940-1230），`_writeToClipboard(` → `writeToClipboard(` 重命名；apiFetch / ElMessage / buildFailureFixtureBatchReplaySummaryText 改由 composable 直接 import。
- 新增 contract 字段: 无。
- Tests: 新增 tests/useCapabilityFixtureReplay.test.js 5 例（smoke：capabilityReplayPaneProps 打包遍历所有派生 computed 不抛错；generate/replay/replayEfficiency 非法输入 guard 不发网络；generate happy path：POST→refetch artifacts+library→success toast，apiFetch/ElMessage mock）；vitest 92 passed（10 文件）；npm run build 绿（1692 模块）。
- App.vue 行数: 2033 → **1644 行**（-389）。本会话累计 ~4150 → 1644（约 -60%）。
- 风险: 中（副作用最重的一块：网络/toast/跨组件依赖；但依赖经注入 + smoke 穷举求值 + guard/happy 双测覆盖，build+vitest 三绿）。

## Slice D-UI-11 (M4 简便): 浏览器运行时状态子系统抽离 useBrowserRuntimeStatus (done, P3)

- 能力名: browser_runtime_status_extraction（browser pool 运行时状态：2 state + 拉取动作 + 7 派生展示 computed 整体抽成 composable）。
- 影响层: 新增 composables/useBrowserRuntimeStatus.js（`useBrowserRuntimeStatus({ appendLog })` 返回 browserRuntimeStatus/browserRuntimeLoading + fetchBrowserRuntimeStatus + 7 computed：browserRuntime/Capacity/BackendSummary/StatusClass/Label/HealthLabel/HealthCacheLabel）。App.vue destructure 全部 10 个返回值。
- 依赖注入: appendLog（来自 createTerminalLogBuffer，第 91 行就绪，接线点在其后）；apiFetch 由 composable 直接 import。
- 抽离方法: 逐字平移 3 处（state 135-136 / 动作 fetchBrowserRuntimeStatus / 7 computed），零转写；调用点（handleSocketMessage / submitTask / onMounted 共 3 处 fetchBrowserRuntimeStatus()）与模板 5 绑定（CapabilityRuntimePanel 的 browser-runtime / -class / -label / backend-summary / capacity / health-label / health-cache-label）零改动。
- 新增 contract 字段: 无。
- Tests: 新增 tests/useBrowserRuntimeStatus.test.js 6 例（默认空对象+route-only/unknown；status→class+中文标签 6 例；health/cache 标签变体；fetch happy 存 runtime+toggle loading；re-entrancy guard 不发网络；error 经 appendLog 告警且 status 不变）；vitest 98 passed（11 文件）；npm run build 绿（1693 模块，index js 190.32kB）。
- App.vue 行数: 1644 → **1602 行**（-42）。本会话累计 ~4150 → 1602。
- 风险: 低（纯 computed + 单 fetch action 平移，依赖经注入，guard/happy/error 三路 + 标签映射穷举覆盖；build+vitest+lint 三绿）。

## Slice D-UI-12 (M4 简便): capability-trace 导出/摘要/复制三件套抽离 useCapabilityTraceExport (done, P2)

- 能力名: capability_trace_export_extraction（exportCapabilityTraceAsJsonl / buildCapabilityTraceSummaryText / copyCapabilityTraceSummary 三个动作整体抽成 composable）。
- 影响层: 新增 composables/useCapabilityTraceExport.js（`useCapabilityTraceExport({ trace, writeToClipboard })` 返回 export/copy 两动作 + 内部 buildSummaryText）。
- 依赖注入: trace（整个 useCapabilityTrace(phaseEvents) 返回对象，composable 内 destructure 出 22 个所需 computed —— 保持函数体逐字不变）；writeToClipboard（wrapper arrow `(text) => _writeToClipboard(text)`，_writeToClipboard 定义晚于接线点，延迟查找）；ElMessage 由 composable 直接 import。
- 抽离方法: App.vue 把 `const { … } = useCapabilityTrace(phaseEvents)` 改为先 `const capabilityTrace = useCapabilityTrace(phaseEvents)` 捕获整袋、再 `} = capabilityTrace` 解构（原有名字零改）；3 个函数逐字平移（`_writeToClipboard` → `writeToClipboard`）；App.vue 仅 destructure 2 个对外动作（buildSummaryText 仅 composable 内部用，不再 destructure 避免未用变量）。调用点（keyboardActions.exportCapability / 模板 @export-jsonl / CapabilityReplayPane copySummary）零改动。
- 新增 contract 字段: 无。
- Tests: 新增 tests/useCapabilityTraceExport.test.js 8 例（空 trace 双 guard / buildSummaryText header+health+intent+alignment / 可选 crawl+runtime 段 / off-plan "not in plan" / copy happy 写剪贴板+toast / copy 失败不 toast / export DOM-stub 验证 _ts 剥离+文件名 capability_trace_<stamp>.jsonl+导出计数 toast）；vitest 106 passed（12 文件）；npm run build 绿（1694 模块）。
- App.vue 行数: 1602 → **1440 行**（-162）。本会话累计 ~4150 → 1440（约 -65%）。
- 风险: 低-中（buildSummaryText ~120 行多分支逐字平移，靠整袋注入零转写；guard/happy/可选段 + DOM-stub export 覆盖，build+vitest+lint 三绿）。

## Slice D-UI-13 (M4 简便): HITL 人工介入/表单子系统抽离 useHitlForm (done, P2)

- 能力名: hitl_form_extraction（human-in-the-loop 介入/前端补填表单：8 state + isBotChallengeHitl computed + 3 动作整体抽成 composable）。
- 影响层: 新增 composables/useHitlForm.js（`useHitlForm({ appendLog })` 返回 8 state：isHumanInterventionRequired/humanInterventionReason/hitlScreenshot + hitlForm{Visible/Fields/Reason/Screenshot/Loading} + isBotChallengeHitl computed + resumeAgentExecution/submitHitlForm/skipHitlForm）。
- 依赖注入: appendLog（注入）；apiFetch / ElMessage / isBotChallengeReason（后者来自 ./useTaskSubmit）由 composable 直接 import。
- 抽离方法: 8 state + 1 computed + 3 函数逐字平移；App.vue destructure 全部 12 个返回值。**关键**：WS 消息处理器 handleSocketMessage（295/410-427）直接写这些 ref（`isHumanInterventionRequired.value = true` 等），destructure 后同名变量，**零改动**；模板 7 处绑定（HitlFormDialog v-model/reason/fields/screenshot/loading/@submit/@skip + sp-hitl-notice 的 isBotChallengeHitl/resumeAgentExecution）零改动。顺带删除 App.vue 现已无用的 isBotChallengeReason import（其唯一引用随 computed 迁出）。
- 新增 contract 字段: 无。
- Tests: 新增 tests/useHitlForm.test.js 7 例（默认惰性态 / isBotChallengeHitl 跟随 reason（mock useTaskSubmit）/ resume happy 清介入+toast / resume error 保留 flag / submit happy 隐表单+字段计数 / submit error 复位 loading / skip 隐表单+日志且不发网络）；vitest 113 passed（13 文件）；npm run build 绿（index js 191.96kB）。
- App.vue 行数: 1440 → **1394 行**（-46）。本会话累计 ~4150 → 1394（约 -66%）。
- 风险: 低（state/computed/3 action 逐字平移，WS 写入点经同名 ref 零改；resume/submit/skip 的 happy+error+guard 全覆盖，build+vitest+lint 三绿）。

## Slice D-UI-14 (M1 准确 + M4 简便): D-UI-11~13 抽离后遗留死 destructure 清除 (done, P3)

- 能力名: appvue_dead_destructure_sweep（D-UI-11/12/13 抽离子系统后，App.vue 残留的「destructure 出来但已无任何活引用」的符号全量删除）。
- 影响层: 仅 App.vue `<script setup>` 的三处 destructure（template / style / 子组件 / 各 composable 均未动）。
- 审计方法: 临时脚本（即用即删）解析 `<script setup>` 顶层声明 + 多行 destructure 名（含 alias），对「去注释的 script+template 合并全文」做 `\b<name>\b` 计数，count==1（仅定义处）判死；删后再跑一次脚本确认 0 死候选（迭代收敛）。
- 结论与删除: 删 **21 个死 destructure**——① useCapabilityTrace 袋里 18 个（capabilityModelRoles + 10 个 runtime/action issue 系列 + 4 个 crawl-efficiency 系列 + 3 个 efficiency-correlation alignment/rootcauses/actions），均为 D-UI-12 把 buildCapabilityTraceSummaryText 迁出后失去活引用者；② useBrowserRuntimeStatus 袋里 browserRuntimeStatus/browserRuntimeLoading（D-UI-11 过度 destructure，实际只在 composable 内部用）；③ useModelSettings 袋里 saveModelSettings（早已无引用）。保留 capabilityTraceJson / capabilityExecutionFailureBundle / capabilityExecutionEfficiencyCorrelationReport / capabilityAuditFindings 等仍被模板或下游 composable 接线参数引用者。
- 新增 contract 字段: 无。
- Tests: 无新增（纯删除、零行为改动）；既有 vitest 113 passed（13 文件）不变；删后脚本 0 死候选；ReadLints clean；npm run build 绿（index js 191.96→**191.14kB**，-0.8kB 死 computed 摇树）。
- App.vue 行数: 1394 → **1373 行**（-21）。顶层声明 189 → 168。本会话累计 ~4150 → 1373（约 -67%）。
- 风险: 低（仅删 count==1 的 destructure 名，script/template/composable 零触碰；脚本迭代确认 + build+vitest+lint 三绿）。

## Slice R2-1 (M5 拆分): extract data_shape 探针去重 → ExtractRuntime (done, P1)

- 能力名: extract_data_shape_dedup（auto/explicit 两处 extract 路径重复的「data_shape 探针 + 稠密页 scroll-drain override」收敛为 `ExtractRuntime.compute_data_shape_with_drain(reason)`）。
- 影响层: data_plane（`extraction_engine/runtime.py` + `main.py::run_agent`）。R2「安全增量」首刀：只搬真·无控制流子段；控制流尾段（8 break + 4 `_task_completed` + `event_stream.done`）按设计留主循环。
- 前置: S1 已完成（`ExtractRuntime`/`ExtractState` 就位）。
- 改动:
  - 新增方法 `ExtractRuntime.compute_data_shape_with_drain(reason) -> dict`：`browser.probe_data_shape()` → 稠密（`expected_rows_from_data_shape>=10`）时附 `physically_drained`/`drain_state`，probe 异常吞掉返回 `{}`。逐字平移零行为改动。
  - `main.py` 两处 19 行内联块（auto extract / explicit extract，仅 drain reason 串与缩进不同）改为单行委托；净减约 32 行；`_drain_state` 局部移除经全量引用核验安全（下游 5418/6816 均先重赋后读）。
- 新增 contract 字段: 无。
- Tests: `tests/test_extract_runtime.py` 追加 3 例（sparse 不附 override / dense 附 drain_state / probe 异常返回 {}），先验红（AttributeError）后转绿，合计 41✓；`py_compile` 通过，CLEAN_CRLF 无混行；main.py 9669→9658 行（<9700 基线仍绿）。
- 验证: 全量 pytest **36 failed / 3893 passed / 2 skipped**——36 失败全是并发 UI agent 的 App.vue source-wiring/keyboard/timeline/timer 断言（已 `git stash` 证实 HEAD 无本切片改动时同样失败），与本切片零关联；**本切片 0 新增失败**。
- 计划: `docs/superpowers/plans/2026-06-18-main-py-decomposition.md`（R2 安全增量首刀；下刀候选：candidate 采集段 6344-6517 → `gather_candidates`）。

## Slice R2-2 (M5 拆分): extract DOM list/card 候选采集去重 → ExtractRuntime (done, P1)

- 能力名: extract_dom_list_card_dedup（auto/explicit 两处 extract 路径重复的「DOM_CARDS 语义卡 + DOM_LIST 列表候选采集」整块收敛为 `ExtractRuntime.gather_dom_list_card_candidates(candidates, *, ...)`）。
- 影响层: data_plane（`extraction_engine/runtime.py` + `main.py::run_agent`）。R2「安全增量」第二刀：真·无控制流子段（仅 append 到调用方 candidates 列表），控制流尾段仍留主循环。
- 前置: R2-1 已完成（`compute_data_shape_with_drain` 就位）。
- 改动:
  - 新增方法 `ExtractRuntime.gather_dom_list_card_candidates(candidates, *, dom_list_rows, dom_list_text, data_shape, card_base_texts, fallback_source_text, body_text_reason) -> None`：`extract_semantic_card_rows` → 空则 body_text 二次兜底 → append `DOM_CARDS`（若有）+ `DOM_LIST`。三处 per-call 差异（card 前置文本 / source_text 兜底串 / body 探针 reason）参数化；原地 mutate 调用方 candidates 列表，不碰其它 caller 局部。逐字平移零行为改动。
  - `main.py` 两处约 50 行内联块（auto extract ~5122 / explicit extract ~6344）改为单次 `await _extract_rt.gather_dom_list_card_candidates(...)` 委托；净减约 92 行；移除 `_dom_card_source_text`/`_dom_card_rows`/`_dom_card_text`/`_dom_card_body_text` 四个局部经全量引用核验安全（grep main.py 0 残留下游引用）。
- 新增 contract 字段: 无。
- Tests: `tests/test_extract_runtime.py` 追加 5 例（空 rows noop / 无卡仅 DOM_LIST / 有卡 DOM_CARDS+DOM_LIST 顺序 / body_text 二次兜底命中 / DOM_LIST source_text 回退 fallback），monkeypatch `extract_semantic_card_rows` + passthrough sanitize 隔离重逻辑，断分支不断 sanitize 内部；合计 46✓；`py_compile` 通过，三文件 CLEAN_CRLF 无混行；main.py 9658→9566 行（<9700 基线仍绿）。
- 验证: 全量 pytest **36 failed / 3938 passed / 2 skipped**——36 失败全是并发 UI agent 的 App.vue source-wiring/keyboard/timeline/timer 断言（已 `git stash` 仅本切片 3 文件后重跑同样 36 failed/110 passed，证实 HEAD 无本切片改动时同样失败），与本切片零关联；**本切片 0 新增失败**。
- 计划: `docs/superpowers/plans/2026-06-18-main-py-decomposition.md`（R2 安全增量第二刀；下刀候选：candidate 采集段控制流尾段——需 `ExtractOutcome` 返回指令协议，风险高）。

## Slice R2-3b (M5 拆分): extract 显式路径分页器探测/武装块 → ExtractRuntime (done, P1)

- 能力名: extract_arm_pagination（显式 extract 路径的「首翻硬约束 + 分页器探测 + re-arm」整块收敛为 `ExtractRuntime.arm_pagination_after_extract(*, new_rows, log_extract_text_source, data_shape)`）。
- 影响层: data_plane（`extraction_engine/runtime.py` + `main.py::run_agent`）。R2「安全增量」第三刀：纯 `_xs.*` 写、无控制流（无 break / _task_completed）。
- 前置: R2-2 已完成；设计稿 `docs/superpowers/plans/2026-06-21-extract-tail-decomposition-design.md`（本刀 = 策略 A 第一刀）。
- 关键发现: auto-extract 路径有同名块但**已与 explicit 漂移**（extract_source 入参 / drain reason 串 / has-paginator 低产 hint 文案 / infinite 文案 四处不同），逐字不一致**不能合并**；故本刀**只搬显式块（单点），auto 块原地不动**，守「逐字平移零行为」铁律。
- 改动:
  - 新增方法 `arm_pagination_after_extract`：import `phases.pagination_helpers` 三函数；新增 `ExtractDeps.broadcast_log_safe`（默认 noop，main 注入 `_broadcast_log_safe`）；`browser.probe_pagination` / `expected_rows_from_data_shape` / `probe_scroll_drain_state` / `_parse_goal_target_count` 经 self 访问；逐字平移 explicit 块。
  - `main.py` 字节级 patch（`_patch_r2_3b.py` 即用即删）：137 行内联块 → 5 行委托；移除 7 个块内局部（`_force_probe_reason`/`_should_force_probe_next`/`_probe`/`_cands`/`_names`/`_drain_state`/`_rearm_target`，grep 确认 explicit 路径 0 残留，仅 auto 块仍引用）。
- 新增 contract 字段: 无。
- Tests: `tests/test_extract_runtime.py` 追加 4 例（first-flip 置位 / has-paginator 武装 next_page / 无分页器标记 infinite / 已知分页器目标未达 re-arm），含 goal premise 断言，先验红（AttributeError）后转绿，合计 50✓；extraction+pagination 17 文件域回归 262✓；`py_compile` 通过，三文件 CLEAN_CRLF 无混行；main.py 9566→9435 行（<9700 基线仍绿）。
- 验证: 全量 pytest **32 failed / 3946 passed / 2 skipped**——32 失败全是并发 UI agent 的 App.vue source-wiring/keyboard/timeline 断言（较 R2-2 的 36 少 4，因 UI agent 同期修了 timer_cleanup/browser_pool），与本切片零关联；**本切片 0 新增失败**（域回归 262✓ 佐证）。
- 计划: `docs/superpowers/plans/2026-06-21-extract-tail-decomposition-design.md`（策略 A 第一刀；下刀候选：R2-3a 零新增反馈块 6432-6518 → `handle_zero_new_rows_feedback`）。

## Slice R2-3a (M5 拆分): extract 显式路径零新增去重反馈块 → ExtractRuntime (done, P1)

- 能力名: extract_zero_rows_feedback（显式 extract 路径「净新增 0 行」的去重反馈块——目标已达提示 / 有前页+触底探测+first_flip / dense 页 block_next_page / 通用 nudge——收敛为 `ExtractRuntime.handle_zero_new_rows_feedback(*, duplicate_zero_extract_streak, dup_rows, rejected_rows, current_url, data_shape) -> int`）。
- 影响层: data_plane（`extraction_engine/runtime.py` + `main.py::run_agent`）。R2「安全增量」第四刀：写 `_xs.*` 分页旗标 + 回传 streak；`if _new_rows==0` 守卫 / `_dedup_tripped_last_step=True` / `break` 留主循环。
- 前置: R2-3b 已完成；设计稿同 R2-3b（策略 A 第二刀）。
- 关键发现: auto-extract 路径零行块（5196-5229，~33 行，`continue` 收尾，仅 dense/通用两分支）与 explicit（~86 行，`break` 收尾，含目标已达/有前页触底/first_flip/dense 多分支）**结构性漂移**，**不能合并**；本刀只搬显式块（单点），auto 块原地不动。
- 改动:
  - 新增方法 `handle_zero_new_rows_feedback`：逐字平移 explicit 块；`_parse_goal_target_count` 经 import，`probe_scroll_drain_state`/`expected_rows_from_data_shape`/`nudge_scroll_after_duplicate_extract` 经 self；新增 `ExtractDeps.vlm`（main 注入 `vlm`）。
  - `main.py` 字节级 patch（`_patch_r2_3a.py` 即用即删）：85 行块 → 7 行委托；移除 7 个块内局部（`_target_count_pre`/`_pre_reached`/`_has_prior_extract_page`/`_physically_drained`/`_drain_reason`/`_remaining_hint`/`_expected_dense_rows`，grep 确认 0 残留）。
- 新增 contract 字段: 无。
- Tests: `tests/test_extract_runtime.py` 追加 4 例（目标已达提 done / 有前页触底武装 first_flip / 有前页未触底 nudge / dense 页 block_next_page），mock probe/nudge 隔离浏览器，先验红（AttributeError）后转绿，合计 54✓；extraction+pagination 17 文件域回归 266✓；`py_compile` 通过，CLEAN_CRLF；main.py 9435→9358 行（<9700）。
- 验证: 全量 pytest 后台核验（App.vue UI 失败均为并发 agent 在制品，与本切片零关联）；域回归 266✓ + 悬挂引用 0 残留，**本切片 0 新增失败**。
- 计划: `docs/superpowers/plans/2026-06-21-extract-tail-decomposition-design.md`（策略 A 第二刀；下刀候选：R2-3c 批后翻页引导块 → `inject_post_extract_pagination_guidance`）。

## Slice BBR-1 (M3 通用): 三后端任务分流路由骨架纳管 BrowserBackendRouter (done, P2)

- 能力名: browser_backend_routing（在 `build_default_browser_backend` 扁平 env/kind 开关之上，按任务类型在 chromium / lightpanda / cloakbrowser 三 persona 间确定性分流；契约 `browser_backend_route.v1`）。
- 影响层: browser_substrate（新增 `visual_web_agent/browser_backend_router.py`，仅 import 既有 `browser_backend.BrowserBackend` / `build_default_browser_backend`，**无运行时接线**——本切片只纳管骨架+回归，不改 browser_pool/control 调用方）。
- 设计: 两段式路由——① 意图排序（纯任务类型打分，availability 无关，top=preferred）；② 可用性闸门（preferred 顺序里首个 available=selected，余者 fallback_chain）。硬规则: vision/screenshot 永不路由 lightpanda（无渲染）；高 anti-bot 偏好 cloakbrowser，不可用降级 chromium stealth；bulk 无像素采集偏好 lightpanda 吞吐；`explicit_backend` / env `VSPIDER_BROWSER_BACKEND` 覆盖。persona→slot: chromium→playwright_chromium / lightpanda→remote_playwright / cloakbrowser→stealth_browser（保留位，`build_backend_for` 对 cloak 显式 NotImplementedError）。
- 新增 contract 字段: `browser_backend_route.v1`（preferred / selected / backend_slot / preference_order / fallback_chain / task_profile / candidates / reasons）。
- Tests: 新增 `tests/test_browser_backend_router.py`（5 类 35 例: alias 归一 / 路由决策 8 场景含 vision 排除 lightpanda + bulk 降级 + 高 anti-bot 降级 + explicit 覆盖 + render 安全压过 explicit / 默认 persona env 闸门 / `build_backend_for` persona→kind 映射 + cloak NotImplementedError + unknown ValueError）；注入固定 persona 与 monkeypatch 工厂，环境无关、不构造真实浏览器。
- 验证: `python -m pytest tests/test_browser_backend_router.py` → **35 passed, exit 0**。
- 风险: 极低（新模块未被任何生产代码引用，grep 全仓 0 外部 import；纯确定性数据驱动路由，不触碰既有 browser 调用方）。
- 后续（未做，独立切片）: 接线 = browser_pool/browser_control 建后端时改走 `router.route()`→`build()`；lightpanda CDP endpoint 实测；cloakbrowser slot 实装。

## Slice D-UI-17 (M4 简便): 剪贴板写入工具从 App.vue 抽离 useClipboard (done, P3)

- 能力名: clipboard_write_extraction（App.vue 内联 `_writeToClipboard` 抽成 `composables/useClipboard.js::writeToClipboard`：navigator.clipboard 优先 + textarea/execCommand 回退 + ElMessage 错误反馈）。
- 影响层: 仅 vspider-ui（App.vue → composables/useClipboard.js）；两处注入点（useCapabilityFixtureReplay / useCapabilityTraceExport）由 `(text) => _writeToClipboard(text)` 改为同名 shorthand `writeToClipboard`。
- 新增 contract 字段: 无。
- Tests: 新增 `tests/useClipboard.test.js` 4 例（空串→false / navigator.clipboard 路径 / textarea+execCommand 回退 / 异常→ElMessage.error+false），vi.stubGlobal 注入 navigator/document；先验红（模块缺失）后转绿。
- 验证: vitest 15 文件 / 126 passed（+4）；npm run build 绿（index js 191.70kB）；ReadLints clean；App.vue 1353→1332 行（-21），仍纯 CRLF。
- 风险: 低（叶子工具，行为逐字平移；无结构化 pytest 断言 _writeToClipboard）。

## Slice D-UI-18 (M4 简便): Auth Profile + Captcha Solver 状态从 App.vue 抽离 useAuthProfiles (done, P3)

- 能力名: auth_profiles_extraction（App.vue 内联的 auth profile 选择 + captcha solver 状态抽成 composables/useAuthProfiles.js）。
- 影响层: 仅 vspider-ui（App.vue → composables/useAuthProfiles.js）；5 个 ref（authDialogOpen/authProfileOptions/selectedAuthProfiles/captchaSolverEnabled/captchaSolverProvider）+ 3 函数（loadAuthProfiles/loadCaptchaSolverStatus/useAuthProfile）迁入，App.vue 同名 destructure 接线；模板 8 处 + onMounted 2 处 + submitTask 1 处 + slash 1 处调用点逐字不变。
- 新增 contract 字段: 无。
- Tests: 新增 tests/useAuthProfiles.test.js 6 例（loadAuthProfiles happy/非success warn / loadCaptchaSolverStatus happy/非success早退/error吞掉 console.warn / useAuthProfile 去重+忽略空）。
- 验证: vitest 16 文件 / 132 passed（+6）；npm run build 绿（index js 192.14kB）；ReadLints clean；App.vue 1332→1305 行（-27），仍纯 CRLF。
- 风险: 低（自包含 API 子系统，行为逐字平移）。

## Slice D-UI-19 (M4 简便): Final Answer 面板状态从 App.vue 抽离 useFinalAnswer (done, P2)

- 能力名: final_answer_extraction（taskResult/finalAnswer* 状态 + finalAnswerHtml computed + 自动切 Tab watch + done 答案类型解析 applyDoneAnswer + 新任务复位 resetForNewRun 抽成 composables/useFinalAnswer.js）。
- 影响层: 仅 vspider-ui（App.vue → composables/useFinalAnswer.js）；activeBottomTab + artifactsGrewSinceSubmit 注入；renderMarkdown import 随 finalAnswerHtml 迁入 composable（App.vue 去掉 markdownRender + computed import）。
- 关键决策: finalAnswerCopyState/finalAnswerCopyTimer（vestigial copy 反馈）保留在 App.vue（与 onUnmounted 清理同处），不入本 composable —— 保住 test_app_vue_timer_cleanup.py 对 onUnmounted 内 finalAnswerCopyTimer 的结构断言；finalAnswerExpanded 仅 resetForNewRun 写、App.vue 无读，内置不外暴露。
- 新增 contract 字段: 无。
- Tests: 新增 tests/useFinalAnswer.test.js 9 例（applyDoneAnswer 显式file/启发式file-text/去重早退/domain白名单(text)/finalAnswerHtml渲染 + resetForNewRun + watch 切final/切artifacts/同type不抢Tab）。
- 验证: vitest 17 文件 / 141 passed（+9）；npm run build 绿（index js 192.54kB）；ReadLints clean；test_app_vue_timer_cleanup.py 3 passed（onUnmounted pin 完好）；App.vue 1305→1252 行（-53），仍纯 CRLF。
- 风险: 中-低（done 解析 + watch 行为逐字平移 + stub 测试守回归；onUnmounted 结构断言已验证）。
