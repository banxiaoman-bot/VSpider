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
