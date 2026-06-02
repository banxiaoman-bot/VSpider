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
