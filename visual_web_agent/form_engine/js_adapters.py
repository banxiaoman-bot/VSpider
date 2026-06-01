"""JS adapter manifest for component-aware form execution.

The current browser execution code still lives in the agent while this module
defines the stable adapter vocabulary. New component families should be added
here first, then wired into the JS executor with field-level readback.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FormAdapter:
    name: str
    selectors: tuple[str, ...]
    capabilities: tuple[str, ...]


ADAPTERS: tuple[FormAdapter, ...] = (
    FormAdapter(
        name="native_input",
        selectors=("input:not([type=hidden])", "textarea", "[contenteditable=true]"),
        capabilities=("fill", "readback", "input/change/blur events"),
    ),
    FormAdapter(
        name="native_select",
        selectors=("select",),
        capabilities=("select option by label/value", "readback selected text"),
    ),
    FormAdapter(
        name="choice",
        selectors=("input[type=checkbox]", "input[type=radio]", "[role=checkbox]", "[role=radio]", "label"),
        capabilities=("checkbox", "radio", "label click", "checked readback"),
    ),
    FormAdapter(
        name="switch",
        selectors=(".el-switch", "[role=switch]"),
        capabilities=("toggle", "aria/class checked readback"),
    ),
    FormAdapter(
        name="date_picker",
        selectors=(".el-picker-panel", ".ant-picker-dropdown", ".n-date-panel", "[role=dialog]"),
        capabilities=("open picker", "month navigation", "day click", "date readback"),
    ),
    FormAdapter(
        name="component_select",
        selectors=(".el-select", ".ant-select", ".n-select", "[role=combobox]"),
        capabilities=("open wrapper", "visible option click", "visible value readback"),
    ),
    FormAdapter(
        name="autocomplete",
        selectors=("[aria-autocomplete]", "[role=combobox]", "[id^=react-select]"),
        capabilities=("type query", "wait options", "select suggestion", "token readback"),
    ),
)


OPTION_SELECTORS: tuple[str, ...] = (
    ".el-select-dropdown__item",
    ".ant-select-item-option",
    "[role=option]",
    "[id^='react-select'][id*='option']",
    ".css-yt9ioa-option",
    ".css-1n7v3ny-option",
    "label",
    "li",
    "span",
    "button",
)


def adapter_names() -> tuple[str, ...]:
    return tuple(adapter.name for adapter in ADAPTERS)
