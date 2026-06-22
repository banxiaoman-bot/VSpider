"""Accessibility-tree (AX) mixin for ``BrowserEnv``.

Extracted from ``browser_env.py`` to reduce file size.
Contains CDP-based AX tree retrieval, element signature extraction,
and the ``extract_accessibility_tree`` / ``extract_page_text_via_ax_tree``
methods used by the agent loop.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page

try:
    from .browser_intercept_helpers import (
        flatten_ax_tree_for_extract,
        flatten_ax_tree,
    )
except ImportError:
    from browser_intercept_helpers import (  # type: ignore[no-redef]
        flatten_ax_tree_for_extract,
        flatten_ax_tree,
    )

logger = logging.getLogger("vspider.browser_ax")


class BrowserAXMixin:
    """Mixin providing accessibility-tree methods for ``BrowserEnv``."""

    async def _get_ax_tree_via_cdp(
        self, page: "Page", interesting_only: bool = True
    ) -> dict | None:
        """Retrieve the full AX tree via CDP ``Accessibility.getFullAXTree``."""
        cdp = None
        try:
            cdp = await page.context.new_cdp_session(page)
            result = await cdp.send("Accessibility.getFullAXTree")
            nodes = result.get("nodes", [])
            if not nodes:
                return None

            by_id: dict[str, dict] = {}
            for raw in nodes:
                nid = raw.get("nodeId")
                if nid is None:
                    continue
                role_val = (raw.get("role") or {}).get("value", "")
                name_val = (raw.get("name") or {}).get("value", "")
                val_obj = raw.get("value") or {}
                entry: dict = {"role": role_val, "name": name_val}
                if val_obj.get("value") is not None:
                    entry["value"] = str(val_obj["value"])
                for prop in raw.get("properties", []):
                    pname = prop.get("name", "")
                    pval = (prop.get("value") or {}).get("value")
                    if pname and pval is not None:
                        entry[pname] = pval
                entry["_cids"] = raw.get("childIds", [])
                entry["_ign"] = raw.get("ignored", False)
                by_id[nid] = entry

            SKIP_ROLES = frozenset(
                {"none", "presentation", "generic", "InlineTextBox", "LineBreak"}
            )

            def _to_tree(nid: str) -> dict | list | None:
                node = by_id.get(nid)
                if node is None:
                    return None
                children: list[dict] = []
                for cid in node["_cids"]:
                    child = _to_tree(cid)
                    if child is None:
                        continue
                    if isinstance(child, list):
                        children.extend(child)
                    else:
                        children.append(child)
                if interesting_only and (
                    node["_ign"] or node["role"] in SKIP_ROLES
                ):
                    return children or None
                out = {k: v for k, v in node.items() if not k.startswith("_")}
                if children:
                    out["children"] = children
                return out

            root = _to_tree(nodes[0]["nodeId"])
            if isinstance(root, list):
                return {"role": "RootWebArea", "name": "", "children": root}
            return root

        except Exception as e:
            logger.warning(f"[AX CDP] 通过 CDP 获取 AX Tree 失败: {e}")
            return None
        finally:
            if cdp:
                try:
                    await cdp.detach()
                except Exception:
                    pass

    async def _get_accessibility_signature(self, page: "Page", handle) -> tuple[str, str]:
        """Extract semantic role and accessible name via lightweight JS."""
        _AX_JS = r"""
        (el) => {
            const clean = (value) => (value || '')
                .toString()
                .replace(/\s+/g, ' ')
                .trim()
                .slice(0, 120);

            const implicitRole = (node) => {
                if (!node || !node.tagName) return '';
                const tag = node.tagName.toLowerCase();
                const type = (node.getAttribute && (node.getAttribute('type') || '') || '').toLowerCase();
                const role = clean(node.getAttribute && node.getAttribute('role'));
                if (role) return role.toLowerCase();
                if (tag === 'a' && node.hasAttribute && node.hasAttribute('href')) return 'link';
                if (tag === 'button') return 'button';
                if (tag === 'summary') return 'button';
                if (tag === 'textarea') return 'textbox';
                if (tag === 'select') return 'combobox';
                if (tag === 'input') {
                    if (['button', 'submit', 'reset'].includes(type)) return 'button';
                    if (type === 'checkbox') return 'checkbox';
                    if (type === 'radio') return 'radio';
                    if (type === 'search') return 'searchbox';
                    if (type === 'range') return 'slider';
                    return 'textbox';
                }
                if (node.isContentEditable) return 'textbox';
                if (tag === 'img') return 'img';
                if (tag === 'option') return 'option';
                return tag;
            };

            const accessibleName = (node) => {
                if (!node || !node.getAttribute) return '';
                const labelledBy = clean(node.getAttribute('aria-labelledby'));
                if (labelledBy) {
                    const parts = labelledBy.split(/\s+/)
                        .map((id) => document.getElementById(id))
                        .filter(Boolean)
                        .map((n) => clean(n.textContent));
                    const joined = clean(parts.join(' '));
                    if (joined) return joined;
                }
                const ariaLabel = clean(node.getAttribute('aria-label'));
                if (ariaLabel) return ariaLabel;
                const title = clean(node.getAttribute('title'));
                if (title) return title;
                const placeholder = clean(node.getAttribute('placeholder'));
                if (placeholder) return placeholder;
                const alt = clean(node.getAttribute('alt'));
                if (alt) return alt;
                const text = clean(node.textContent);
                if (text) return text;
                return '';
            };

            return { role: implicitRole(el), name: accessibleName(el) };
        }
        """
        role = ""
        name = ""
        try:
            raw = await handle.evaluate(_AX_JS)
            if isinstance(raw, dict):
                role = str(raw.get("role") or "").strip().lower()
                name = str(raw.get("name") or "").strip()
        except Exception as _xe:
            logger.debug(f"[RPA] AX JS signature extraction failed (non-fatal): {_xe}")

        return role, name

    def _flatten_ax_tree_for_extract(
        node: dict, out: list[str], depth: int = 0, max_depth: int = 15,
    ) -> None:
        flatten_ax_tree_for_extract(node, out, depth, max_depth)

    async def extract_page_text_via_ax_tree(self) -> str:
        """Extract full semantic text via AX tree (data extraction use-case)."""
        page = await self._ensure_active_page(reason="ax tree extraction for data")
        if not page:
            return ""

        try:
            ax_root = await self._get_ax_tree_via_cdp(page, interesting_only=False)
            if not ax_root:
                logger.warning("[AX Extract] accessibility.snapshot 返回空")
                return ""

            lines: list[str] = []
            self._flatten_ax_tree_for_extract(ax_root, lines)

            text = "\n".join(lines)
            logger.info(
                f"[AX Extract] 从 AX Tree 提取 {len(lines)} 行语义文本 "
                f"({len(text)} 字符)"
            )
            return text[:16000]

        except Exception as e:
            logger.warning(f"[AX Extract] AX Tree 提取失败: {e}")
            return ""

    def _flatten_ax_tree(
        node: dict, out: list[str], depth: int = 0, max_depth: int = 12
    ) -> None:
        flatten_ax_tree(node, out, depth, max_depth)

    async def extract_accessibility_tree(self) -> str:
        """Extract the page's accessibility semantic tree for VLM input.

        Returns a multi-line string with two sections:
          [Interactive elements (@eN mapping)]
          [Page semantic snapshot (AX Tree)]
        """
        page = await self._ensure_active_page(reason="before extract_accessibility_tree")
        if not page:
            raise RuntimeError("No active page available for AX tree extraction.")

        await self._wait_for_page_stable()
        await self._dismiss_permission_surfaces(reason="before ax tree extraction")

        _ID_MAP_JS = r"""
(() => {
    const marked = Array.from(document.querySelectorAll('[data-som-id]'));
    marked.sort((a, b) => {
        const ai = parseInt(a.getAttribute('data-som-id'), 10);
        const bi = parseInt(b.getAttribute('data-som-id'), 10);
        return (isNaN(ai) ? 0 : ai) - (isNaN(bi) ? 0 : bi);
    });

    function deriveRole(el) {
        const explicit = el.getAttribute('role');
        if (explicit) return explicit.trim();
        const tag = el.tagName.toLowerCase();
        if (tag === 'a' && el.hasAttribute('href')) return 'link';
        if (tag === 'button') return 'button';
        if (tag === 'input') {
            const t = (el.getAttribute('type') || 'text').toLowerCase();
            if (['button', 'submit', 'reset'].includes(t)) return 'button';
            if (t === 'checkbox') return 'checkbox';
            if (t === 'radio') return 'radio';
            if (t === 'search') return 'searchbox';
            if (t === 'range') return 'slider';
            return 'textbox';
        }
        if (tag === 'textarea') return 'textbox';
        if (tag === 'select') return 'combobox';
        if (el.isContentEditable) return 'textbox';
        return tag;
    }

    function deriveName(el) {
        const ariaLabelledBy = el.getAttribute('aria-labelledby');
        if (ariaLabelledBy) {
            const parts = ariaLabelledBy.split(/\s+/)
                .map(id => document.getElementById(id))
                .filter(Boolean)
                .map(n => (n.textContent || '').trim());
            const joined = parts.join(' ').trim();
            if (joined) return joined;
        }
        const ariaLabel = el.getAttribute('aria-label');
        if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();
        const title = el.getAttribute('title');
        if (title && title.trim()) return title.trim();
        const placeholder = el.getAttribute('placeholder');
        if (placeholder && placeholder.trim()) return placeholder.trim();
        const txt = (el.textContent || '').replace(/\s+/g, ' ').trim();
        if (txt) return txt;
        const alt = el.getAttribute('alt');
        if (alt && alt.trim()) return alt.trim();
        return '';
    }

    return marked.map(el => ({
        id: el.getAttribute('data-som-id'),
        role: deriveRole(el),
        name: deriveName(el).slice(0, 80),
        value: (el.value !== undefined && el.value !== null && String(el.value).trim())
            ? String(el.value).slice(0, 40) : '',
        disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
        checked: el.getAttribute('aria-checked') === 'true' || el.checked === true,
        selected: el.selected === true || el.getAttribute('aria-selected') === 'true',
        expanded: el.getAttribute('aria-expanded'),
        required: el.required === true || el.getAttribute('aria-required') === 'true',
        readonly: el.readOnly === true || el.getAttribute('aria-readonly') === 'true',
    }));
})()
"""
        id_rows: list[dict] = []
        try:
            raw = await page.evaluate(_ID_MAP_JS)
            if isinstance(raw, list):
                id_rows = [r for r in raw if isinstance(r, dict) and r.get("id")]
        except Exception as e:
            logger.debug(f"[AX Tree] ID 映射段提取失败: {e}")

        _INTERACTIVE_ROLES = {
            "button", "link", "textbox", "searchbox", "combobox",
            "checkbox", "radio", "switch", "slider",
            "tab", "menuitem", "menuitemcheckbox", "menuitemradio",
            "option", "treeitem",
        }

        self.element_mapping.clear()
        compact_lines: list[str] = []
        for row in id_rows:
            role = (row.get("role") or "").strip().lower()
            if role not in _INTERACTIVE_ROLES:
                continue
            try:
                som_id_int = int(row["id"])
            except (TypeError, ValueError):
                continue
            ref = f"@e{som_id_int}"
            name = (row.get("name") or "").strip()
            value = (row.get("value") or "").strip()
            states: list[str] = []
            if row.get("disabled"):
                states.append("disabled")
            if row.get("checked"):
                states.append("checked")
            if row.get("selected"):
                states.append("selected")
            expanded = row.get("expanded")
            if expanded in ("true", "false"):
                states.append(f"expanded={expanded}")
            if row.get("required"):
                states.append("required")
            if row.get("readonly"):
                states.append("readonly")

            parts = [ref, f"[{role or '?'}]"]
            if name:
                parts.append(f'"{name}"')
            if value:
                parts.append(f'value="{value}"')
            if states:
                parts.append("{" + ",".join(states) + "}")
            compact_lines.append(" ".join(parts))
            self.element_mapping[ref] = {
                "som_id": som_id_int,
                "role": role,
                "name": name,
                "selector": f'[data-som-id="{som_id_int}"]',
            }

        ax_lines: list[str] = []
        try:
            ax_root = await self._get_ax_tree_via_cdp(page, interesting_only=True)
            if ax_root:
                self._flatten_ax_tree(ax_root, ax_lines, depth=0, max_depth=12)
        except Exception as e:
            logger.debug(f"[AX Tree] CDP snapshot 失败，跳过语义段: {e}")

        MAX_SEMANTIC_LINES = 120
        truncated_semantic = ax_lines[:MAX_SEMANTIC_LINES]

        sections: list[str] = []
        if compact_lines:
            sections.append(
                "【可交互元素 (@eN 语义快照)】\n"
                "格式：@eN [role] \"name\" value=\"...\" {states}；@eN 中的数字 = 截图红框序号。\n"
                "操作时 target_id 直接填这个数字（如 @e5 → target_id=5）。\n"
                + "\n".join(compact_lines)
            )
        if truncated_semantic:
            hint = ""
            if len(ax_lines) > MAX_SEMANTIC_LINES:
                hint = f"\n...[AX Tree 过长，已截断 {len(ax_lines) - MAX_SEMANTIC_LINES} 行]..."
            sections.append(
                "【页面语义快照 (AX Tree)】\n" + "\n".join(truncated_semantic) + hint
            )

        if not sections:
            logger.warning(
                "[AX Tree] @eN 映射段与语义段均为空；mark_and_screenshot 可能未注入 data-som-id"
            )
            return ""

        logger.info(
            f"[AX Tree] Emitted {len(compact_lines)} @eN refs + "
            f"{len(truncated_semantic)}/{len(ax_lines)} semantic lines"
        )
        return "\n\n".join(sections)
