# Open Source Patterns Worth Borrowing

This note tracks ideas from UI-TARS, UI-TARS-desktop, Scrapling, and
browser-use that are useful for VSpider. These projects are references, not
runtime dependencies.

## Project Fit

| Project | Best Reference Area | Fit For VSpider |
|---|---|---|
| UI-TARS | vision-action grounding, action parser, benchmark mindset | Better visual fallback and action grammar |
| UI-TARS-desktop | hybrid browser/computer agent, event stream, MCP-style integration | Better orchestration and observability |
| Scrapling | adaptive scraping, anti-bot-aware fetch/playwright layer, selector resilience | Stronger extraction engine and fallback selectors |
| browser-use | tool registry, browser state model, agent history, custom tools, CLI | Cleaner action layer and test/debug ergonomics |

## Comparative Scorecard

Score meaning: 5 = directly valuable for VSpider, 3 = useful with adaptation,
1 = mostly unrelated or risky to copy.

| Dimension | UI-TARS | UI-TARS-desktop | Scrapling | browser-use | VSpider Direction |
|---|---:|---:|---:|---:|---|
| Web form reliability | 2 | 3 | 1 | 4 | Keep VSpider deterministic form engine; borrow browser-use result model |
| Component UI handling | 3 | 4 | 1 | 3 | Keep JS adapters; add typed evidence and more probes |
| Pure data extraction | 1 | 2 | 5 | 3 | Borrow Scrapling fetch tiers and selector recovery |
| Anti-bot / stealth scraping | 1 | 3 | 5 | 4 | Centralize profile, headers, retries, proxy/session policy |
| Visual coordinate fallback | 5 | 5 | 1 | 3 | Borrow UI-TARS grammar, keep disabled until DOM/AX fail |
| Action protocol design | 5 | 4 | 1 | 5 | Standardize VSpider action/result schema |
| Browser state abstraction | 3 | 4 | 2 | 5 | Add `BrowserStateSnapshot` |
| Event/history observability | 3 | 5 | 2 | 5 | Add event stream JSONL |
| Custom tool extensibility | 2 | 4 | 2 | 5 | Turn deterministic macros into first-class tools |
| Benchmark discipline | 5 | 4 | 3 | 5 | Extend `tests/agent_cases` and reports |
| Native desktop automation | 5 | 5 | 1 | 2 | Only future optional scope, not current priority |
| Integration ecosystem / MCP | 2 | 5 | 1 | 4 | Keep as future extension point |

Main read:

- Scrapling is the best extraction reference.
- browser-use is the best agent-engineering reference.
- UI-TARS is the best visual fallback and benchmark reference.
- UI-TARS-desktop is the best hybrid/event-stream/integration reference.

## Current VSpider Position

VSpider is already strongest in website-specific hybrid automation:

- It combines screenshot/SoM, AX Tree, DOM, Playwright actions, deterministic
  JS adapters, XHR capture, Excel output, and targeted guards.
- Recent fixes proved that deterministic paths beat pure model guessing for
  forms, tooltips, dropdowns, cascaders, date pickers, RPA-style shifting forms,
  and table/list extraction.
- The largest architectural debt is not lack of raw ability. It is that state,
  action results, extractor decisions, and verification evidence are still spread
  across the main loop, action handlers, logs, and ad-hoc dictionaries.

So the right learning direction is:

1. Keep VSpider's hybrid deterministic core.
2. Borrow architecture boundaries and developer ergonomics from browser-use and
   UI-TARS-desktop.
3. Borrow extraction resilience from Scrapling.
4. Use UI-TARS-style visual grounding only as fallback.

## Capability Gap Matrix

| Capability | Current VSpider | Gap | Source To Learn From | Next Step |
|---|---|---|---|---|
| Label-bound forms | Strong | Evidence is scattered | browser-use | Structured `ActionResult.value_readbacks` |
| Component forms | Strong and improving | JS adapters still live mostly in `main.py` | browser-use + UI-TARS-desktop | Move adapters behind tool boundary |
| Hover menus | Good after fix | Needs stronger final-action verification | browser-use | `ActionResult.clicked_text` + post-action check |
| Tooltip extraction | Good after fix | Needs deterministic target mapping for more layouts | UI-TARS + browser-use | Add tooltip case variants |
| Table/list extraction | Strong | Selector memory missing | Scrapling | Selector fingerprints |
| Multi-page extraction | Good | Need replayable evidence when pagination skips | Scrapling | Extraction snapshots |
| XHR extraction | Good | Needs clearer field normalization | Scrapling | Add schema-aware normalization report |
| Anti-bot | Partial | Policy scattered across browser startup/interceptor | Scrapling + browser-use cloud ideas | Centralize fetch/browser profile policy |
| State/history | Functional | Too much in logs and ad-hoc dicts | browser-use | `BrowserStateSnapshot` + event stream |
| Visual fallback | Basic | No standard coordinate action schema | UI-TARS | Define coordinate fallback grammar |
| Regression testing | Started | No historical pass-rate trend yet | browser-use benchmark | Add report summaries over time |

## Scrapling Patterns

Scrapling is most useful for extraction-heavy tasks, not for replacing the
interactive agent loop.

Scrapling references that matter:

- `Fetcher`, `AsyncFetcher`, `StealthyFetcher`, and `DynamicFetcher` split
  extraction into HTTP/static, stealth HTTP/browser-like, and real browser tiers.
- Adaptive selection can save a selector on a successful scrape and later
  relocate similar elements when the page changes.
- Spider mode supports concurrent crawling, pause/resume, proxy rotation,
  blocked-request detection, robots.txt handling, development cache, and exports.
- It has selector flexibility: CSS, XPath, text search, regex search, and
  similar-element discovery.

Borrow:

- Adaptive selector fallback: store multiple equivalent selectors for a
  previously extracted field, then recover when classes or DOM depth change.
- Fetcher tiering: try cheap HTTP/static extraction before using a full browser
  when the task is pure data extraction.
- Anti-bot posture: centralize headers, browser profile, stealth, and retry
  policy for extraction pages.
- Page snapshot cache: keep compact HTML/text snapshots with extraction results
  so failures can be replayed without revisiting the site.

Avoid:

- Replacing Playwright interaction with scraping-only logic. VSpider still needs
  real browser actions for forms, hover, date pickers, login, and dynamic UI.

Suggested VSpider modules:

- `visual_web_agent/extraction_engine/selectors.py`
- `visual_web_agent/extraction_engine/fetchers.py`
- `visual_web_agent/extraction_engine/snapshots.py`

Concrete VSpider adoption:

- Add `ExtractorEvidence` rows that record which extractor won:
  `DOM_TABLE`, `DOM_LIST`, `AX_TREE`, `XHR`, `VLM_VIEWPORT`, or `STATIC_FETCH`.
- Save selector fingerprints for stable repeated rows:
  tag path, nearby labels, text signature, class tokens, role/name, and column
  headers.
- Add static fetch only for obvious read-only extraction tasks; never use it for
  login, forms, hover, date pickers, upload, or state-changing actions.
- Add development snapshot replay so extraction fixes can be tested from saved
  HTML/text without repeatedly hitting external sites.

Directly useful Scrapling ideas without adding Scrapling as a dependency:

1. Keep multiple selectors per field, not one selector.
2. Treat selector recovery as a scored match, not a boolean.
3. Store enough page context to debug extraction offline.
4. Split pure extraction from real browser interaction early.
5. Make anti-bot handling a policy object instead of scattered flags.

Dependency decision:

- Do not add Scrapling now.
- Re-evaluate only if VSpider needs a full crawling/spider subsystem with
  rotating proxies and static fetch at scale.

## browser-use Patterns

browser-use is most useful for agent architecture and developer experience.

browser-use references that matter:

- Its prompt forces structured fields such as `thinking`,
  `evaluation_previous_goal`, `memory`, `next_goal`, and an action list.
- `ActionResult` is the unit of action feedback and can carry extracted content,
  long-term memory, error, and metadata.
- `BrowserStateSummary` centralizes URL/title/tabs/DOM/screenshot-like state.
- `Tools().action(...)` gives a clean custom-tool registration surface.
- Its CLI exposes quick browser inspection commands like `state`, `click`,
  `type`, and `screenshot`.

Borrow:

- Typed action results: every action should return a structured result with
  `success`, `message`, `changed_state`, `extracted_content`, and `error`.
- Tool/action registry as public API: let custom tools be added without editing
  the central loop.
- Browser state object: expose URL, tabs, interactive elements, DOM summary,
  screenshot path, and last action result as one object.
- Agent history stream: persist compact per-step state, action, result, and
  verification, then feed this back into planning.
- CLI state command: add a lightweight command to inspect current page state and
  clickable elements during debugging.

Avoid:

- Moving all domain-specific deterministic logic into the LLM. VSpider's recent
  wins came from deterministic form, tooltip, cascader, table, and pagination
  paths with readback.

Suggested VSpider modules:

- `visual_web_agent/action_result.py`
- `visual_web_agent/action_registry.py`
- `visual_web_agent/browser_state.py`
- `visual_web_agent/event_stream.py`

Concrete VSpider adoption:

- Introduce a VSpider `ActionResult` without rewriting all handlers at once:
  legacy handlers can return `None`, and a wrapper can normalize that into a
  structured result.
- Move `last action succeeded?` evidence out of free-text logs and into fields:
  `changed_url`, `changed_dom`, `value_readbacks`, `clicked_text`,
  `output_file`, `output_rows`, `warnings`.
- Add a `state` debug command that prints URL, title, visible interactive
  elements, recent action result, and page data shape.
- Keep deterministic tools first-class: `auto_form_fill`, `hover_and_click`,
  `cascader_pick`, `date_pick`, `table_extract`, `next_page` should be tools,
  not prompt-only suggestions.

Directly useful browser-use ideas without adding browser-use as a dependency:

1. Use a structured action-result object everywhere.
2. Give every action a concise human-readable memory string.
3. Keep the previous action evaluation explicit: success, failure, or uncertain.
4. Provide a lightweight CLI to inspect browser state.
5. Keep custom tool registration simple enough that new deterministic macros do
   not require editing the central loop.

Dependency decision:

- Do not replace VSpider with browser-use.
- Keep browser-use as architecture reference. VSpider already has specialized
  deterministic tools that are more reliable for the tested forms/extraction
  cases.

## UI-TARS Patterns

UI-TARS and UI-TARS-desktop are useful for the visual-agent side.

UI-TARS references that matter:

- UI-TARS emphasizes GUI grounding and coordinate processing for native/visual
  environments.
- UI-TARS reports benchmark results across OSWorld, WebVoyager,
  Online-Mind2Web, Android World, and ScreenSpot-style grounding tasks.
- UI-TARS-desktop / Agent TARS exposes a hybrid browser agent that can use GUI,
  DOM, or hybrid strategies, plus protocol-driven event streams and MCP.

Borrow:

- Canonical action grammar for click, type, scroll, drag, hotkey, wait, done.
- Coordinate fallback when DOM/AX cannot identify an element.
- Benchmark discipline: keep stable ability probes and compare pass rate over
  time, not just individual manual runs.
- Event stream visualizer idea: one timeline of observe, decide, act, verify.

Avoid:

- Pure coordinate-first interaction on websites when DOM/AX/Playwright can act
  more precisely.

Directly useful UI-TARS ideas without adding UI-TARS as a dependency:

1. Define a canonical visual action grammar:
   `click_point`, `drag_point`, `scroll`, `hotkey`, `type`, `wait`, `done`.
2. Normalize coordinates against screenshot size and viewport scale.
3. Log why visual fallback was used so it does not mask DOM/AX regressions.
4. Add visual grounding probes to the regression suite later.

Dependency decision:

- Do not add UI-TARS runtime now.
- Add an adapter boundary so a UI-TARS-like model can be plugged in later for
  canvas/native/remote-desktop tasks.

## Regression Case Mapping

Current `tests/agent_cases/cases.json` maps to the open-source lessons like this:

| Case | Capability | Open Source Lesson |
|---|---|---|
| `element_form_basic` | Component form | browser-use result evidence + VSpider deterministic tools |
| `element_dropdown_hover_click` | Hover menu | browser-use action result, UI-TARS action grammar |
| `element_tooltip_four_directions` | Tooltip extraction | UI-TARS grounding + VSpider deterministic hover readback |
| `element_cascader_basic` | Multi-level component | DOM/JS deterministic adapter, not pure vision |
| `element_date_picker_enter_date` | Relative date picker | Deterministic macro with readback |
| `rpa_challenge_once` | Dynamic label layout | Label geometry binding and field readback |
| `demoqa_student_registration` | Autocomplete form | Component-aware JS adapter |
| `hn_algolia_ai_agent_50` | List/XHR extraction | Scrapling-style fetch tier and schema normalization |
| `douban_top250_26` | List extraction + pagination | Scrapling-style selector memory |
| `datatables_30_next` | Table + Next pagination | Extraction evidence + pagination verification |

The key is not the sites. The key is the behavioral coverage.

## Architecture Boundary Proposal

Borrowed ideas should enter VSpider through narrow boundaries:

```text
visual_web_agent/
  action_registry.py        # browser-use inspired deterministic tool catalog
  action_result.py          # browser-use inspired result evidence
  browser_state.py          # browser-use inspired state snapshot
  event_stream.py           # UI-TARS-desktop inspired event timeline
  extraction_engine/
    selectors.py            # Scrapling inspired selector fingerprints
    snapshots.py            # Scrapling inspired replay snapshots
    fetchers.py             # Scrapling inspired static/dynamic fetch tier
  visual_fallback/
    action_grammar.py       # UI-TARS inspired coordinate grammar
```

Do not put these directly into `main.py`. The point of learning from these
projects is to reduce central-loop pressure, not add more branches to it.

## Priority Roadmap

### P0: Already Started

- Capability regression cases live in `tests/agent_cases/`.
- Component-aware form engine boundary lives in `visual_web_agent/form_engine/`.
- Tooltip/dropdown false-positive guard has been tightened.
- `ActionResult`, `EventStream`, and `BrowserStateSnapshot` have first-pass
  implementations, with state snapshots attached to observe events and verify
  events attached to high-value completion checks.
- `ActionRegistry` now provides a first-pass deterministic tool catalog and
  emits selected candidate tools at run start; `act` events can now carry
  `metadata.tool` attribution. `hover_and_click` and `next_page` now route
  through registry dispatch with `tool_dispatch` events.
- `debug_cli` now exposes state/clickable/screenshot/tools inspection commands
  so a page can be inspected without running the full natural-language loop.
- `extraction_engine.snapshots` now saves compact extraction evidence and can
  replay row/field coverage summaries offline.
- `extraction_engine.selectors` now builds rowset/field fingerprints and scores
  fingerprint similarity as the first step toward selector recovery.
- `extraction_engine.recovery` now uses recent public-safe snapshots as advisory
  memory, adding bounded score boosts to current extraction candidates that
  match the same URL/field group.
- Snapshot replay now includes `compare` for fingerprint score, row delta, and
  field drift between two saved extraction surfaces.
- Snapshot replay also includes `compare-latest`, grouping snapshots by
  URL/field set to surface recent extraction drift automatically.

### P1: High Leverage

1. Continue migrating action handlers to return richer `ActionResult` evidence.
2. Add extraction selector memory for repeated table/list pages.
3. Add a command to list current page clickable elements without running a full
   task.
4. Gradually move deterministic macros from `main.py` behind the small
   tool/action registry.

Suggested order:

1. Broaden `ActionResult` coverage for click/type/hover/extract/form actions.
2. Keep enriching `BrowserStateSnapshot` using already-collected screenshot, AX Tree,
   DOM shape, URL, and page title.
3. Extend event stream JSONL coverage for `observe`, `decide`, `act`, `verify`,
   `extract`, `guard`, `done`.
4. CLI/debug command that reuses `BrowserEnv` and `BrowserStateSnapshot`.

### P2: Extraction Strength

1. Add a static fetch path for pure extraction tasks.
2. Add HTML snapshot replay for extractor development.
3. Add selector self-healing for fields and repeated list/table rows.

Suggested order:

1. Snapshot current DOM/text when extraction succeeds or fails.
2. Build offline replay runner for snapshots.
3. Add selector fingerprints and recovery against snapshots.
4. Only then add static fetch tier, because replay/fingerprints make it testable.

### P3: Visual Fallback

1. Standardize visual coordinate fallback actions.
2. Add screenshot-based verification plugins for non-DOM UI.
3. Add optional event stream viewer for debugging long runs.

Suggested order:

1. Standardize coordinate action grammar but keep it disabled by default.
2. Use visual fallback only after DOM/AX/JS adapter paths fail.
3. Log fallback reason aggressively so it does not hide selector regressions.

## What Not To Do

- Do not add Scrapling as a hard dependency just because it is strong. VSpider's
  extraction code can borrow the architecture first.
- Do not replace VSpider's form/date/tooltip/dropdown deterministic paths with
  browser-use style LLM-only planning.
- Do not move to coordinate-first control for web pages. It is useful for canvas,
  remote desktop, or native UI, but weaker than DOM/AX for normal websites.
- Do not create a second agent loop. Improve the existing loop with better
  state/result boundaries.

## Decision Summary

| Need | Best Source | VSpider Action |
|---|---|---|
| More reliable scraping after DOM changes | Scrapling | Selector fingerprints + snapshot replay |
| Cleaner action feedback | browser-use | Add `ActionResult` and structured history |
| Better debugging | browser-use + UI-TARS-desktop | `state` command + event stream JSONL |
| Better benchmark discipline | browser-use + UI-TARS | Extend `tests/agent_cases` pass-rate reports |
| Better visual fallback | UI-TARS | Standard coordinate action grammar |
| Browser/DOM hybrid strategy | UI-TARS-desktop | Keep deterministic DOM/AX/JS first, visual fallback second |

## Practical Rule

VSpider should stay hybrid:

1. Use deterministic DOM/AX/JS adapters when the page is a website.
2. Use browser interaction for real user workflows.
3. Use scraping/fetching when the task is pure extraction.
4. Use visual coordinate fallback only when semantic and DOM paths fail.
