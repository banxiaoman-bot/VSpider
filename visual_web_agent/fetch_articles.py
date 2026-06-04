"""
fetch_articles.py

读取 VSpider 生成的 output_*.xlsx，
对每条热搜标题搜索百度新闻，提取第一条结果的摘要内容，
保存为 articles_*.xlsx。

用法：
    python fetch_articles.py                   # 自动读取最新的 output_*.xlsx
    python fetch_articles.py output_xxx.xlsx   # 指定文件
"""

import sys
import time
import re
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

try:  # SSRF guard import works whether run as a package module or a script
    from visual_web_agent.url_guard import is_url_allowed
except ImportError:  # pragma: no cover - script run from inside the package dir
    from url_guard import is_url_allowed

# ── 请求头（模拟普通浏览器，避免被拒） ────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.baidu.com/",
}

REQUEST_DELAY = 1.5   # 每条请求间隔（秒），避免触发反爬

ANTI_BOT_HINTS = [
    "百度安全验证",
    "请输入以下验证码",
    "网络不给力",
    "访问过于频繁",
]


def _get_search_dirs() -> list[Path]:
    """返回用于搜索 output_*.xlsx 的目录列表（去重后保持顺序）。"""
    cwd = Path.cwd().resolve()
    script_dir = Path(__file__).parent.resolve()
    project_root = script_dir.parent

    ordered = [cwd, script_dir, project_root]
    unique_dirs = []
    for d in ordered:
        if d not in unique_dirs:
            unique_dirs.append(d)
    return unique_dirs


def find_output_candidates() -> list[Path]:
    """查找所有候选 output_*.xlsx，按修改时间升序排序。"""
    files: list[Path] = []
    for search_dir in _get_search_dirs():
        files.extend(search_dir.glob("output_*.xlsx"))

    # 同一文件可能被多个目录引用到，先规范化去重，再按 mtime 排序
    dedup = {f.resolve(): f.resolve() for f in files}
    return sorted(dedup.values(), key=lambda p: p.stat().st_mtime)


def resolve_input_path(raw_path: str) -> Path:
    """解析命令行传入路径：支持相对 cwd / 脚本目录 / 项目根目录。"""
    candidate = Path(raw_path)
    if candidate.exists():
        return candidate.resolve()

    probe_paths = [
        Path.cwd() / raw_path,
        Path(__file__).parent / raw_path,
        Path(__file__).parent.parent / raw_path,
    ]
    for p in probe_paths:
        if p.exists():
            return p.resolve()

    search_dirs = "\n".join(f"- {d}" for d in _get_search_dirs())
    raise FileNotFoundError(
        "未找到指定输入文件："
        f"{raw_path}\n"
        "已尝试以下目录：\n"
        f"{search_dirs}"
    )


def _extract_from_html(html: str) -> str:
    """从百度搜索页面 HTML 中尽量提取第一条可用摘要。"""
    if any(hint in html for hint in ANTI_BOT_HINTS):
        return ""

    soup = BeautifulSoup(html, "html.parser")

    selectors = [
        ".c-abstract",                    # 传统摘要
        ".content-right_8Zs40",           # 新版摘要
        ".c-color-text",                  # 部分新版结果文案
        "div.result-op p",                # 卡片类结果段落
        "div.result h3 + div",            # 标题后首段文本
        "[class*='abstract']",            # 兜底
    ]

    for selector in selectors:
        items = soup.select(selector)
        if not items:
            continue

        for item in items[:5]:
            text = item.get_text(separator=" ", strip=True)
            text = re.sub(r"\s+", " ", text).strip()
            # 降低阈值，避免有效短摘要被过滤
            if len(text) >= 8:
                return text

    # 兜底：抓取首条结果标题后的文本块
    first_result = soup.select_one("div.result")
    if first_result:
        chunks = [
            re.sub(r"\s+", " ", t.get_text(" ", strip=True)).strip()
            for t in first_result.select("div, p, span")[:10]
        ]
        chunks = [c for c in chunks if len(c) >= 8]
        if chunks:
            return chunks[0]

    return ""


def _request_baidu(url: str, params: dict) -> str:
    """请求百度并返回文本，失败时返回空字符串。"""
    resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
    resp.encoding = "utf-8"
    return resp.text


def _extract_first_result_url(html: str) -> str:
    """从百度搜索结果页提取第一条普通搜索结果的 URL。"""
    soup = BeautifulSoup(html, "html.parser")
    # 百度普通结果：div.result 或 div[class*="result-op"] 下的 h3 > a
    for result_div in soup.select("div.result, div[class*='result-op']"):
        link = result_div.select_one("h3 a")
        if link:
            href = str(link.get("href", ""))
            if href.startswith("http"):
                return href
    # 兜底：直接找 h3 下第一个外链
    for h3 in soup.select("h3.t a, h3 a"):
        href = str(h3.get("href", ""))
        if href.startswith("http"):
            return href
    return ""


def _guarded_get(url: str, *, headers: dict, timeout: float, max_redirects: int = 5):
    """``requests.get`` that re-checks every redirect hop against the SSRF guard.

    ``requests`` follows 3xx itself, so a public article URL could bounce the
    fetch onto an internal / cloud-metadata host. Follow manually, validating
    each hop with :func:`is_url_allowed`; return ``None`` once a hop is blocked
    (the caller then yields an empty body).
    """
    current = url
    for _ in range(max_redirects + 1):
        if not is_url_allowed(current, resolve_dns=True):
            return None
        resp = requests.get(current, headers=headers, timeout=timeout, allow_redirects=False)
        location = resp.headers.get("location") if resp.is_redirect else None
        if location:
            current = requests.compat.urljoin(current, location)
            continue
        return resp
    return None


def _fetch_article_body(article_url: str) -> str:
    """访问文章页面，提取 <p> 段落文本，返回前 400 字。"""
    # SSRF guard (initial + every redirect hop): article URLs come from
    # search results (untrusted) and can 30x onto internal targets.
    try:
        resp = _guarded_get(article_url, headers=HEADERS, timeout=10)
        if resp is None:
            return ""
        resp.encoding = resp.apparent_encoding or "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
        # 移除 script/style/导航干扰
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        paragraphs = [
            re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip()
            for p in soup.find_all("p")
        ]
        paragraphs = [p for p in paragraphs if len(p) >= 20]
        combined = " ".join(paragraphs[:6])
        return combined[:400] if combined else ""
    except Exception:
        return ""


def search_baidu_snippet(title: str) -> str:
    """
    在百度搜索 title，提取第一条结果的摘要文字。
    返回摘要字符串，失败时返回空字符串。

    三层策略：
      1. 普通搜索页 → CSS 选择器提取摘要（适合百科/知识图谱）
      2. 新闻垂搜页 → 同上（适合有新闻聚合的事件）
      3. 追踪第一条结果 URL → 直接抓文章正文（适合纯新闻事件）
    """
    url = "https://www.baidu.com/s"
    params = {"wd": title, "rn": "5", "ie": "utf-8"}
    try:
        html = _request_baidu(url, params)
        snippet = _extract_from_html(html)
        if snippet:
            return snippet

        # 第二层：切到新闻垂搜再抓一次
        news_params = {"tn": "news", "word": title, "rtt": "1", "ie": "utf-8"}
        news_html = _request_baidu(url, news_params)
        snippet = _extract_from_html(news_html)
        if snippet:
            return snippet

        # 第三层：追踪第一条搜索结果 URL → 抓文章正文
        article_url = _extract_first_result_url(html) or _extract_first_result_url(news_html)
        if article_url:
            return _fetch_article_body(article_url)

        return ""
    except Exception as e:
        print(f"  [WARN] 搜索失败: {e}")
        return ""


def main():
    # 确定输入文件
    if len(sys.argv) > 1:
        input_path = resolve_input_path(sys.argv[1])
    else:
        candidates = find_output_candidates()
        if not candidates:
            search_dirs = "\n".join(f"- {d}" for d in _get_search_dirs())
            print(
                "未找到 output_*.xlsx，请先运行 VSpider。\n"
                "搜索目录：\n"
                f"{search_dirs}"
            )
            sys.exit(1)
        elif len(candidates) == 1:
            input_path = candidates[0]
        elif not sys.stdin.isatty():
            # 在非交互环境（如任务/CI）下，自动使用最新文件，避免 input() 阻塞
            input_path = candidates[-1]
            print(f"检测到非交互环境，自动使用最新文件：{input_path.name}")
        else:
            print("\n发现多个文件，请选择：")
            for i, f in enumerate(candidates):
                row_count = len(pd.read_excel(f, engine="openpyxl"))
                print(f"  [{i}] {f.name}  ({row_count} 行)")
            print(f"  [Enter] 默认选最新：{candidates[-1].name}")
            choice = input("\n输入序号：").strip()
            input_path = candidates[int(choice)] if choice.isdigit() and int(choice) < len(candidates) else candidates[-1]

    print(f"\n读取文件: {input_path}")
    df = pd.read_excel(input_path, engine="openpyxl")

    if "title" not in df.columns:
        print(f"[ERROR] 文件中没有 'title' 列，现有列：{list(df.columns)}")
        sys.exit(1)

    print(f"共 {len(df)} 条记录，开始抓取正文摘要...\n")

    snippets = []
    for idx, (_, row) in enumerate(df.iterrows(), start=1):
        title = str(row["title"]).strip()
        rank_val = row.get("rank", idx)
        try:
            rank = int(rank_val) if pd.notna(rank_val) else idx
        except Exception:
            rank = idx
        print(f"  [{rank}] {title}")

        snippet = search_baidu_snippet(title)
        snippets.append(snippet)

        if snippet:
            preview = snippet[:60] + ("..." if len(snippet) > 60 else "")
            print(f"       → {preview}")
        else:
            print("       → (未获取到摘要)")

        time.sleep(REQUEST_DELAY)

    df["content"] = snippets

    # 输出文件与 output_*.xlsx 放在同一目录
    ts_part = input_path.stem.replace("output_", "")
    output_path = input_path.parent / f"articles_{ts_part}.xlsx"
    df.to_excel(output_path, index=False, engine="openpyxl")
    print(f"\n保存完成 → {output_path.resolve()}")


if __name__ == "__main__":
    main()
