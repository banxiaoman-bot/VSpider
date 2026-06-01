"""Prompt 模板外置加载器。

将核心系统提示词从 Python 字符串拆分为独立的 .md 文件，
便于非开发人员编辑和版本管理，同时支持运行时热加载。

用法：
    from prompts_templates import load_template, load_all_templates

    # 加载单个模板
    text = load_template("system_prompt")

    # 加载全部模板并拼接
    full_prompt = load_all_templates(["system_prompt", "json_schema", "extract_rules"])
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent
_CACHE: dict[str, str] = {}


def load_template(name: str, *, use_cache: bool = True) -> str:
    """加载指定名称的 .md 模板文件。

    Args:
        name: 模板文件名（不含 .md 后缀）
        use_cache: 是否使用内存缓存（生产环境建议 True）

    Returns:
        模板内容文本

    Raises:
        FileNotFoundError: 模板文件不存在时
    """
    if use_cache and name in _CACHE:
        return _CACHE[name]

    filepath = _TEMPLATE_DIR / f"{name}.md"
    if not filepath.exists():
        raise FileNotFoundError(f"Prompt template not found: {filepath}")

    content = filepath.read_text(encoding="utf-8").strip()
    if use_cache:
        _CACHE[name] = content
    return content


def load_all_templates(
    names: list[str],
    separator: str = "\n\n",
    *,
    use_cache: bool = True,
) -> str:
    """按顺序加载多个模板并拼接。

    Args:
        names: 模板名称列表
        separator: 拼接分隔符
        use_cache: 是否使用缓存

    Returns:
        拼接后的完整文本
    """
    parts = []
    for name in names:
        try:
            parts.append(load_template(name, use_cache=use_cache))
        except FileNotFoundError:
            logger.warning(f"[PROMPT TEMPLATE] Template '{name}' not found, skipped.")
    return separator.join(parts)


def list_templates() -> list[str]:
    """列出所有可用的模板名称。"""
    return [
        f.stem for f in _TEMPLATE_DIR.glob("*.md")
        if f.is_file()
    ]


def reload_cache() -> None:
    """清除缓存（热加载场景使用）。"""
    _CACHE.clear()


def get_template_path(name: str) -> Optional[Path]:
    """获取模板文件路径（用于编辑器集成）。"""
    filepath = _TEMPLATE_DIR / f"{name}.md"
    return filepath if filepath.exists() else None
