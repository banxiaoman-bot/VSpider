"""
VSpider 数据管理模块

负责将 VLM 提取或网络层拦截的结构化数据保存至 Excel 文件。
支持：
- VLM extract 提取保存（save_to_excel）
- XHR/Fetch 网络拦截数据保存（save_intercepted_data）
- 追加写入 + 自动去重
- 写入前二次过滤（FilterRule）
"""

import re
import logging
from pathlib import Path
from datetime import datetime
from typing import Callable

import pandas as pd

try:
    from .artifact_manager import register_artifact, resolve_artifact_path
except ImportError:
    from artifact_manager import register_artifact, resolve_artifact_path

logger = logging.getLogger("vspider.data")

# ANSI 颜色
_GREEN_BOLD = "\033[1;32m"
_CYAN = "\033[36m"
_RESET = "\033[0m"


# ========== 过滤规则定义 ==========

class FilterRule:
    """
    数据过滤规则。

    支持四种过滤模式：
    - regex: 正则匹配（字段值必须匹配正则表达式）
    - range: 数值范围（字段值必须在 [min, max] 区间内）
    - keyword: 关键词包含（字段值必须包含指定关键词）
    - custom: 自定义函数（传入行 dict，返回 True 保留）
    """

    def __init__(
        self,
        column: str = "",
        mode: str = "keyword",
        pattern: str = "",
        min_val: float = None,
        max_val: float = None,
        keywords: list[str] = None,
        custom_fn: Callable[[dict], bool] = None,
    ):
        self.column = column
        self.mode = mode
        self.pattern = pattern
        self.min_val = min_val
        self.max_val = max_val
        self.keywords = keywords or []
        self.custom_fn = custom_fn

    def match(self, row: dict) -> bool:
        """判断一行数据是否符合过滤条件。返回 True 表示保留。"""
        if self.mode == "custom" and self.custom_fn:
            return self.custom_fn(row)

        value = row.get(self.column)
        if value is None:
            return False

        if self.mode == "regex":
            return bool(re.search(self.pattern, str(value)))

        elif self.mode == "range":
            try:
                num = float(value)
                if self.min_val is not None and num < self.min_val:
                    return False
                if self.max_val is not None and num > self.max_val:
                    return False
                return True
            except (ValueError, TypeError):
                return False

        elif self.mode == "keyword":
            val_str = str(value)
            return any(kw in val_str for kw in self.keywords)

        return True


def apply_filters(data: list[dict], rules: list[FilterRule]) -> list[dict]:
    """
    对数据列表应用过滤规则。所有规则是 AND 关系（全部满足才保留）。
    """
    if not rules:
        return data

    filtered = []
    for row in data:
        if all(rule.match(row) for rule in rules):
            filtered.append(row)

    before = len(data)
    after = len(filtered)
    if before != after:
        logger.info(f"Filter applied: {before} rows -> {after} rows ({before - after} filtered out)")
    return filtered


# ========== 核心保存函数（通用） ==========

def _save_dataframe_to_excel(
    df_new: pd.DataFrame,
    filepath: Path,
    unique_key: str | list[str] = None,
) -> tuple[str, int]:
    """
    内部通用函数：将 DataFrame 追加写入 Excel 文件，支持去重。

    Returns:
        (文件绝对路径, 总行数)
    """
    # 添加提取时间戳
    df_new["_extracted_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 追加或新建
    if filepath.exists():
        try:
            df_existing = pd.read_excel(filepath, engine="openpyxl")
            df_combined = pd.concat([df_existing, df_new], ignore_index=True)
        except Exception as e:
            logger.warning(f"Failed to read existing file, overwriting: {e}")
            df_combined = df_new
    else:
        df_combined = df_new

    # 去重
    if unique_key:
        # 显式 unique_key：精确按指定列去重
        subset = [unique_key] if isinstance(unique_key, str) else unique_key
        valid_cols = [c for c in subset if c in df_combined.columns]
        if valid_cols:
            before_dedup = len(df_combined)
            df_combined = df_combined.drop_duplicates(
                subset=valid_cols, keep="last"
            ).reset_index(drop=True)
            removed = before_dedup - len(df_combined)
            if removed > 0:
                logger.info(f"Dedup by {valid_cols}: removed {removed} duplicates")
    else:
        # 自动 hash 去重：保护快速翻页场景下的"AJAX 未完成 + 上一页 DOM 仍存"导致的重复落盘
        # 适用 DataTables / 动态表格等 URL 不变的翻页场景（fast-pagination race）
        # 哈希基于所有数据列（剥离 _extracted_at 等系统列），保证内容相同的行被识别为重复
        _SYSTEM_COLS = {"_extracted_at", "_row_hash"}
        _data_cols = [c for c in df_combined.columns if c not in _SYSTEM_COLS]
        if _data_cols and len(df_combined) >= 2:
            import hashlib
            def _row_fingerprint(row):
                # 按列名稳定排序后串接成字符串，再 sha1 截 16 字符
                _vals = [f"{c}={row[c]!s}" for c in sorted(_data_cols)]
                return hashlib.sha1("\x1f".join(_vals).encode("utf-8")).hexdigest()[:16]
            try:
                _hashes = df_combined.apply(_row_fingerprint, axis=1)
                before_dedup = len(df_combined)
                _keep_mask = ~_hashes.duplicated(keep="last")
                df_combined = df_combined[_keep_mask].reset_index(drop=True)
                removed = before_dedup - len(df_combined)
                if removed > 0:
                    logger.info(
                        f"[AUTO HASH DEDUP] 自动去重移除 {removed} 行重复数据"
                        f"（疑似快翻页 AJAX race），保留最新副本"
                    )
            except Exception as _hash_err:
                logger.warning(f"[AUTO HASH DEDUP] 失败忽略：{_hash_err}")

    # 写入
    df_combined.to_excel(filepath, index=False, engine="openpyxl")
    return str(filepath.resolve()), len(df_combined)


# ========== VLM extract 数据保存 ==========

def save_to_excel(
    data,
    filename: str = "output.xlsx",
    filters: list[FilterRule] = None,
    unique_key: str | list[str] = None,
) -> str:
    """
    将 VLM extract 动作提取的数据保存至 Excel。

    Args:
        data: dict / list[dict] / list
        filename: 输出文件名
        filters: 可选过滤规则
        unique_key: 去重字段
    """
    filepath = resolve_artifact_path(filename)

    # 统一转换为 list[dict]
    if isinstance(data, dict):
        data_list = [data]
    elif isinstance(data, list):
        if len(data) == 0:
            logger.warning("Extracted data is empty, skipping save.")
            return str(filepath.resolve())
        if isinstance(data[0], dict):
            data_list = data
        else:
            data_list = [{"value": v} for v in data]
    else:
        data_list = [{"raw_data": str(data)}]

    # 过滤
    if filters:
        data_list = apply_filters(data_list, filters)
        if not data_list:
            logger.warning("All data filtered out by rules, nothing to save.")
            return str(filepath.resolve())

    df_new = pd.DataFrame(data_list)
    abs_path, total = _save_dataframe_to_excel(df_new, filepath, unique_key)
    logger.info(f"[VLM Extract] Saved to: {abs_path} (total {total} rows)")
    register_artifact(abs_path)
    return abs_path


# ========== XHR/Fetch 网络拦截数据保存 ==========

def save_intercepted_data(
    json_list: list[dict],
    filename: str = "output.xlsx",
    unique_key: str | list[str] = None,
) -> str:
    """
    将网络层拦截到的 JSON 数据保存至 Excel。

    专为 XHR/Fetch 拦截场景设计：
    - 接收 list[dict] 格式的纯 JSON 数据
    - 自动追加到已有文件
    - 支持按 unique_key 去重（防止翻页重复拦截）
    - 打印醒目的绿色日志

    Args:
        json_list: 拦截到的字典列表（API 响应中的数据行）
        filename: 输出文件名（默认 output.xlsx）
        unique_key: 去重字段（如 "id"、"order_no" 等）

    Returns:
        保存的文件绝对路径
    """
    filepath = resolve_artifact_path(filename)

    if not json_list or len(json_list) == 0:
        logger.warning("[XHR Intercept] Empty data list, skipping save.")
        return str(filepath.resolve())

    df_new = pd.DataFrame(json_list)
    new_count = len(df_new)

    abs_path, total = _save_dataframe_to_excel(df_new, filepath, unique_key)

    # 醒目的绿色成功日志
    print(
        f"\n{_GREEN_BOLD}[XHR INTERCEPT]{_RESET} "
        f"{_CYAN}Successfully intercepted and saved {new_count} records. "
        f"Total: {total} rows in {filepath.name}{_RESET}\n"
    )
    logger.info(
        f"[XHR Intercept] Saved {new_count} new records -> {abs_path} "
        f"(total {total} rows)"
    )
    register_artifact(abs_path)
    return abs_path
