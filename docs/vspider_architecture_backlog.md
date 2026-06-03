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

