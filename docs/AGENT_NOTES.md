# Agent Workflow Notes

A running list of non-obvious traps + recommended techniques discovered
while pair-programming on this codebase. Read this before making large
edits — it will save real time.

> **Audience**: Cascade / Claude Code / any future LLM-driven dev agent.

---

## 1. Line endings: this repo is **CRLF (Windows)**

The `.py` files in `visual_web_agent/` are saved with `\r\n` line
endings. Most LLM `edit` tools (`edit`, `multi_edit`, `replace_in_file`)
do byte-level comparison against an `old_string` that the model
constructed with `\n` only. The result: **the edit silently fails with
"string not found"** even when the rendered text looks identical.

### Symptoms

- `edit` tool reports `string not found` after 1-3 retries on text you
  literally just read with `read_file`.
- The blank lines (PEP 8's two-blank-line rule between top-level
  definitions) are most fragile because the agent often drops one.

### Diagnosis

```python
import pathlib
data = pathlib.Path("visual_web_agent/actions.py").read_bytes()
print("CRLF" if b"\r\n" in data[:5000] else "LF")
```

### Fix: byte-level patch script

For any insertion that crosses a blank-line boundary or replaces >5
lines, **skip the edit tool** and write a small `_patch_<feature>.py`
script that:

1. Reads the file as bytes (`read_bytes()`).
2. Detects line endings (`b"\r\n" in sample` -> use CRLF, else LF).
3. Builds the new block in `\n`, then `replace("\n", line_ending)`.
4. Anchors on a unique sentinel byte sequence (e.g. a decorator + class
   name in one go: `b'@ActionRegistry.register("switch_tab")\r\nclass SwitchTabHandler'`).
5. Writes back. Verifies idempotency by checking a sentinel marker.
6. **Delete the script after the patch lands**. It's a one-shot, not a
   build artifact.

A reference implementation: see commit history of any of the recent
"row_action / extract_row / tree_check / fetch_link_content HTTP
status" patches.

### Don't

- Don't try to override the editor's view with raw `\r\n` strings — the
  tool's quoting layer often strips them.
- Don't hand-count blank lines from the read_file output. The line
  prefix `123→` hides whether trailing blank lines have CR or not.

---

## 2. PowerShell is hostile to `python -c`

PowerShell 5.x/7.x rewrites quotes inside `python -c "..."` in ways
that break:

- Triple-quoted strings (`"""..."""` becomes `""..."`)
- Backslash + special char (`\s` in a JS regex becomes a flagged escape)
- Mixed `'` and `"` for inner quoting

### Fix

Write a tiny `.py` file and `python file.py` instead. Two extra writes,
zero quoting drama. The same applies to `node -e`, `bash -c` etc.

### Don't

- Don't pipe with `tail`, `head`, `grep` — none of those are PowerShell
  built-ins on a default Windows install.

---

## 3. Skill-prompt dispatch lives inside `build_system_prompt`

There's no public `select_skills` function. Skill keywords are checked
via `_text_has_any(haystack, _XXX_TRIGGERS)` and `skills.append(...)`
inside `visual_web_agent/prompts.py::build_system_prompt`.

To verify which skills get pulled for a goal:

```python
from visual_web_agent.prompts import build_system_prompt
prompt = build_system_prompt(goal="...", browser_state="")
"## Skill: Row Action" in prompt  # row_action loaded?
```

The H2 header text on each skill block is a stable fingerprint suitable
for tests (see `tests/test_row_action_and_dialogs.py::_SKILL_FINGERPRINTS`).

---

## 4. Action handler patterns to reuse

When adding a new deterministic action that operates on a text-located
DOM target, copy the **row_action v2** template:

1. Iterate `page.frames` (main first, then iframes) — admin panels.
2. Try a chain of **selector candidates** (`tr` / `[role=row]` / Element
   / Ant / Naive / generic) — gives free framework support.
3. On multi-match, pick the entry with the **shortest text length** —
   cheap deterministic disambiguation, no LLM needed.
4. Use `_click_locator_with_js_fallback(...)` for clicks (handles
   actionability stalls + popper-mask interference).
5. Push a one-line summary into `browser._tab_switch_notice` so the next
   VLM screenshot turn knows what happened (this is the **only** way
   the model can see deterministic-action outcomes; vision-only is
   usually unreliable).
6. `browser.rpa_trail.append(ctx.with_rpa_meta({...}))` for replay.

The v2 template also adds:

7. Optional `||confirm` 3rd segment for follow-up modals (see
   `RowActionHandler._CONFIRM_LABELS`).
8. Canvas/SVG fallback hint pointing VLM at `data_export` skill when no
   DOM table matched anywhere.

Symmetric read-only counterpart: see `extract_row` (writes to
`workflow_memory`).
Tree-checkbox toggle: see `tree_check` (idempotent — re-running a
"check" on an already-checked node is a no-op).

---

## 5. Tests live in `tests/`, regression bar is high

- Current count: **1162 passing**, 1 deselected (Windows temp-dir
  permission unrelated to repo code).
- After every non-trivial change, run:
  ```powershell
  python -m pytest tests/ --no-header -q `
    --ignore=tests/test_debug_cli.py `
    --deselect tests/test_agent_skills.py::test_html_logger_can_use_shared_run_id `
    -p no:cacheprovider
  ```
- New deterministic actions need stub-frame regression tests (no
  Playwright runtime needed). See
  `tests/test_extract_row_and_tree_check.py::_StubLocator` for a copy-paste base.

---

## 6. The deselected test is **not** the agent's problem

`test_html_logger_can_use_shared_run_id` fails on Windows with
`PermissionError: 'C:\\Users\\<user>\\AppData\\Local\\Temp\\pytest-of-<user>'`.
This is `pytest`'s temp-dir cleanup racing with antivirus. Always
deselect it, never try to "fix" it from inside the repo.

---

## 7. When in doubt, prefer many small commits to one big one

The codebase is large (~5500-line `actions.py`, ~1100-line
`prompts.py`, ~830-line `prompt_skills.py`). A surgical patch to a
single handler + its prompt + its tests is reviewable. A 600-line
omnibus refactor is not.
