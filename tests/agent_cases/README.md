# Agent Capability Cases

This folder contains public-site regression probes for VSpider's generic agent
capabilities. They are not production dependencies and are not imported by the
main agent loop.

The sites in `cases.json` are only stable, public examples that exercise broad
behaviors:

- component forms: input, select, date picker, switch, checkbox, radio
- hover-trigger dropdown menus
- tooltip extraction
- cascader / multi-level menus
- dynamic label-bound forms
- autocomplete form fields
- list, table, pagination, XHR, and Excel extraction

## List Cases

```powershell
python tests\agent_cases\run_agent_cases.py
```

Filter by capability or category:

```powershell
python tests\agent_cases\run_agent_cases.py --capability form
python tests\agent_cases\run_agent_cases.py --category extraction
python tests\agent_cases\run_agent_cases.py --id demoqa_student_registration
```

## Run Cases

Running opens real browsers and uses the configured VLM, so it requires network
access and valid model configuration.

```powershell
python tests\agent_cases\run_agent_cases.py --run --id demoqa_student_registration
python tests\agent_cases\run_agent_cases.py --run --category interaction
python tests\agent_cases\run_agent_cases.py --run
```

Reports are saved under:

```text
workspace/agent_case_reports/
```

Each result also includes a `diagnostics` block built from the run's event
stream and output workbook. It summarizes selected tools, dispatched tools,
macro/extraction sources, guard events, verification events, action failures,
the last browser state, output fields, and a small row preview. Failed cases
also get a coarse `failure_type` such as `browser_start_failed`,
`agent_return_mismatch`, `min_rows_not_met`, or `field_missing`.

Run reports also include recent extraction snapshot drift comparisons. This is
useful for spotting cases where a site still returns data, but the source,
row count, or requested-field coverage changed between runs.

```powershell
python tests\agent_cases\run_agent_cases.py --run --category extraction --snapshot-drift-limit 5
```

Runtime skill replay snapshots can also be checked as an offline gate. This is
useful before opening browsers: it verifies that recent skill snapshots still
match the same skill and satisfy their row/field/verification contract.

```powershell
python tests\agent_cases\run_agent_cases.py --skill-replay-check-only --skill-replay-check-limit 20
python tests\agent_cases\run_agent_cases.py --run --category interaction --skill-replay-check-limit 20
```

Browser runs also execute the runtime skill lint gate before opening the first
page. Use `--skill-lint-only` to run that gate by itself, or
`--skip-skill-lint` for temporary debugging runs.

```powershell
python tests\agent_cases\run_agent_cases.py --skill-lint-only --skill-lint-include-files --skill-lint-strict
```

New deterministic interaction skills can be started from a template. The
scaffold writes the skill module, a focused match test, and registry hints:

```powershell
python -m visual_web_agent.skills scaffold --name "demo widget" --aliases "example.com/widget,widget" --url-patterns "example.com/widget" --goal-patterns "widget" --fields "title,url" --expected-rows 2
```

Pass `--register` to also add the generated skill to the default runtime
registry.

Registered skills can be checked without opening a browser:

```powershell
python -m visual_web_agent.skills lint
python -m visual_web_agent.skills lint --include-files --strict
```

Targeted local perception can inspect only the likely elements for a task goal,
which is useful before falling back to full-page SoM/AX perception:

```powershell
python -m visual_web_agent.debug_cli probe --url "https://example.com" --goal "click More information link" --kind link --json
```

The same probe is also registered as a non-mutating runtime action named
`targeted_probe`. It can be selected by the agent when it needs compact
input/button/link/table/dialog candidates before choosing a concrete action.

Generated extraction workbooks remain under:

```text
workspace/artifacts/
```

## Why These Sites?

They are probes, not assumptions about your future work. A passing Element Plus
form case means the engine can handle a family of Vue/component-library forms;
a passing DataTables case means table extraction plus Next pagination still
works. Your production sites can be completely different.
