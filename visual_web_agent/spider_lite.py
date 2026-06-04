from __future__ import annotations

import time
import uuid
import json
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.request import Request

from visual_web_agent.artifact_manager import artifact_url, register_artifact, resolve_artifact_path
from visual_web_agent.extraction_engine import generic
from visual_web_agent.page_cache import PageCacheMissError, PageResponseCache
from visual_web_agent.robots_policy import RobotsPolicyManager
from visual_web_agent.crawl_frontier import build_frontier, normalize_keywords
from visual_web_agent.url_guard import build_guarded_opener, check_url


@dataclass
class FetchResult:
    url: str
    status_code: int
    html: str


class _LinkParser(HTMLParser):
    """Collect ``(href, anchor_text)`` pairs in document order.

    Anchor text feeds the best-first frontier relevance score
    (``crawl_frontier.score_url`` weights it at 0.5). Text is accumulated
    between ``<a href=...>`` and ``</a>`` (including nested inline tags) and
    whitespace-collapsed on flush. A dangling unclosed ``<a>`` is flushed by
    :meth:`close`, so callers must ``feed`` then ``close``.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if str(tag or "").lower() != "a":
            return
        if self._href is not None:
            self._flush()
        data = {str(k).lower(): v or "" for k, v in attrs}
        href = str(data.get("href") or "").strip()
        if href:
            self._href = href
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None and data:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if str(tag or "").lower() == "a" and self._href is not None:
            self._flush()

    def _flush(self) -> None:
        if self._href is not None:
            anchor = " ".join("".join(self._text).split())
            self.links.append((self._href, anchor))
        self._href = None
        self._text = []

    def close(self) -> None:
        super().close()
        self._flush()


Fetcher = Callable[[str], FetchResult | dict[str, Any] | str]


class SpiderLiteManager:
    def __init__(self, *, robots_policy: RobotsPolicyManager | None = None, fetcher: Fetcher | None = None, page_cache: PageResponseCache | None = None) -> None:
        self.robots_policy = robots_policy or RobotsPolicyManager()
        self.fetcher = fetcher or default_fetch
        self.page_cache = page_cache or PageResponseCache()
        self.page_caches: dict[str, PageResponseCache] = {self.page_cache.session_id: self.page_cache}
        self.runs: dict[str, dict[str, Any]] = {}

    def list_runs(self) -> list[dict[str, Any]]:
        items = []
        for run in sorted(self.runs.values(), key=lambda item: item.get("created_at", 0), reverse=True):
            items.append({
                "run_id": run.get("run_id"),
                "status": run.get("status"),
                "page_count": run.get("page_count", 0),
                "item_count": run.get("item_count", 0),
                "error_count": run.get("error_count", 0),
                "created_at": run.get("created_at"),
                "finished_at": run.get("finished_at"),
            })
        return items

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self.runs.get(str(run_id or ""))

    def items(self, run_id: str, *, fields: list[str] | None = None, offset: int = 0, limit: int = 1000) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run is None:
            raise KeyError("spider run not found")
        all_items = [dict(item) if isinstance(item, dict) else {"value": item} for item in (run.get("items") or [])]
        wanted = normalize_string_list(fields or [])
        if wanted:
            all_items = [{field: item.get(field, "") for field in wanted} for item in all_items]
        start = max(0, int(offset or 0))
        size = max(1, min(int(limit or 1000), 10000))
        page = all_items[start:start + size]
        field_names: list[str] = []
        for item in all_items:
            for key in item.keys():
                if key not in field_names:
                    field_names.append(str(key))
        return {
            "run_id": str(run.get("run_id") or run_id),
            "total": len(all_items),
            "offset": start,
            "limit": size,
            "count": len(page),
            "fields": field_names,
            "items": page,
        }

    def export_feed(self, run_id: str, *, format: str = "jsonl", filename: str = "") -> dict[str, Any]:
        run = self.get_run(run_id)
        if run is None:
            raise KeyError("spider run not found")
        feed_format = str(format or "jsonl").strip().lower()
        if feed_format not in {"jsonl", "json"}:
            raise ValueError("unsupported spider feed format")
        rid = safe_run_id(str(run.get("run_id") or run_id or "spider"))
        suffix = "jsonl" if feed_format == "jsonl" else "json"
        name = str(filename or "").strip() or f"spider_{rid}_{time.strftime('%Y%m%d_%H%M%S')}.{suffix}"
        path = resolve_artifact_path(name, subdir="spider")
        path.parent.mkdir(parents=True, exist_ok=True)
        items = [dict(item) if isinstance(item, dict) else {"value": item} for item in (run.get("items") or [])]
        if feed_format == "jsonl":
            with path.open("w", encoding="utf-8", newline="\n") as fh:
                for item in items:
                    fh.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
        else:
            path.write_text(
                json.dumps({
                    "run_id": run.get("run_id"),
                    "page_count": run.get("page_count", 0),
                    "item_count": len(items),
                    "items": items,
                }, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        register_artifact(path)
        artifact = {"path": str(path), "url": artifact_url(path), "format": feed_format, "count": len(items)}
        run["artifact"] = artifact
        return artifact

    def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self._config(payload)
        self._apply_sitemap_seeds(config)
        run_id = config["run_id"]
        created_at = time.time()
        result: dict[str, Any] = {
            "run_id": run_id,
            "status": "running",
            "created_at": created_at,
            "finished_at": None,
            "config": config,
            "start_urls": list(config["start_urls"]),
            "allowed_domains": list(config["allowed_domains"]),
            "pages": [],
            "items": [],
            "errors": [],
        }
        self.runs[run_id] = result
        page_cache = self._page_cache(config)
        result["page_cache"] = page_cache.public_state()
        frontier = build_frontier(config["crawl_strategy"], keywords=config["keywords"], seeds=config["start_urls"])
        seen: set[str] = set()
        resume_state = self._load_resume_state(config)
        if resume_state is not None:
            seen.update(str(url) for url in (resume_state.get("seen") or []))
            result["pages"].extend(resume_state.get("pages") or [])
            result["items"].extend(resume_state.get("items") or [])
            result["errors"].extend(resume_state.get("errors") or [])
            frontier = build_frontier(
                config["crawl_strategy"],
                keywords=config["keywords"],
                pending=resume_state.get("pending") or [],
            )
            result["resumed"] = True
        while len(frontier) and len(result["pages"]) < config["max_pages"]:
            url, depth = frontier.pop()
            url = normalize_url(url)
            if not url or url in seen:
                continue
            seen.add(url)
            domain = domain_of(url)
            if domain not in config["allowed_domains"]:
                result["errors"].append({"url": url, "error": "domain not allowed"})
                continue
            if config["robots_txt_obey"]:
                policy = self.robots_policy.reserve_url(url, obey=True, default_delay=config["delay_seconds"])
                if not policy.get("allowed_by_robots"):
                    result["errors"].append({"url": url, "error": "blocked by robots.txt", "policy": policy})
                    continue
            try:
                fetched, fetch_source = self._fetch_with_cache(url, page_cache)
            except Exception as exc:
                result["errors"].append({"url": url, "error": str(exc)})
                continue
            page_record = {
                "url": url,
                "final_url": fetched.url,
                "status_code": fetched.status_code,
                "depth": depth,
                "bytes": len(fetched.html.encode("utf-8", errors="ignore")),
                "fetch_source": fetch_source,
            }
            result["pages"].append(page_record)
            result["items"].extend(self._extract_items(fetched, config))
            if config["follow_links"] and depth < config["max_depth"]:
                for link, anchor_text in extract_links_with_anchors(fetched.html, fetched.url):
                    if len(seen) + len(frontier) >= config["max_pages"] * 5:
                        break
                    if domain_of(link) in config["allowed_domains"] and link not in seen:
                        frontier.push(link, depth + 1, anchor_text=anchor_text)
            self._maybe_checkpoint(config, result, seen, frontier)
        self._maybe_checkpoint(config, result, seen, frontier, force=True)
        pipeline = self._apply_item_pipeline(result["items"], config["item_pipeline"])
        result["items"] = pipeline["items"]
        result["item_pipeline"] = pipeline["stats"]
        result["status"] = "success"
        result["finished_at"] = time.time()
        result["page_count"] = len(result["pages"])
        result["item_count"] = len(result["items"])
        result["error_count"] = len(result["errors"])
        result["page_cache"] = page_cache.public_state()
        result["artifact"] = None
        if config["export"]:
            result["artifact"] = self.export_feed(
                run_id,
                format=str(config.get("export_format") or "jsonl"),
                filename=str(config.get("export_filename") or ""),
            )
        return result

    def cache_state(self, session_id: str = "default") -> dict[str, Any]:
        cache = self.page_caches.get(str(session_id or "default")) or self.page_cache
        return cache.public_state()

    def cache_entries(self, session_id: str = "default") -> list[dict[str, Any]]:
        cache = self.page_caches.get(str(session_id or "default")) or self.page_cache
        return cache.list_entries()

    def _apply_item_pipeline(self, items: list[Any], pipeline_config: dict[str, Any]) -> dict[str, Any]:
        config = dict(pipeline_config or {})
        fields = normalize_string_list(config.get("fields") or [])
        required = normalize_string_list(config.get("required_fields") or [])
        dedupe_by = normalize_string_list(config.get("dedupe_by") or [])
        drop_empty = bool(config.get("drop_empty"))
        strip_strings = bool(config.get("strip_strings", True))
        try:
            max_items = max(1, min(int(config.get("max_items") or 10000), 100000))
        except Exception:
            max_items = 10000
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        dropped_empty = 0
        dropped_required = 0
        dropped_duplicate = 0
        for raw in items:
            item = dict(raw) if isinstance(raw, dict) else {"value": raw}
            if strip_strings:
                item = {str(k): clean_scalar(v) for k, v in item.items()}
            else:
                item = {str(k): v for k, v in item.items()}
            if fields:
                item = {field: item.get(field, "") for field in fields}
            if drop_empty and not any(not_empty(v) for v in item.values()):
                dropped_empty += 1
                continue
            if required and not all(not_empty(item.get(field)) for field in required):
                dropped_required += 1
                continue
            if dedupe_by:
                key = json.dumps({field: item.get(field, "") for field in dedupe_by}, ensure_ascii=False, sort_keys=True, default=str)
                if key in seen:
                    dropped_duplicate += 1
                    continue
                seen.add(key)
            out.append(item)
            if len(out) >= max_items:
                break
        return {
            "items": out,
            "stats": {
                "input_count": len(items),
                "output_count": len(out),
                "fields": fields,
                "required_fields": required,
                "dedupe_by": dedupe_by,
                "drop_empty": drop_empty,
                "strip_strings": strip_strings,
                "max_items": max_items,
                "dropped_empty": dropped_empty,
                "dropped_required": dropped_required,
                "dropped_duplicate": dropped_duplicate,
            },
        }

    def _fetch_with_cache(self, url: str, page_cache: PageResponseCache) -> tuple[FetchResult, str]:
        if page_cache.mode == "replay":
            cached = page_cache.lookup(url)
            if cached is not None:
                return FetchResult(url=cached.final_url, status_code=cached.status_code, html=cached.html), "cache"
        fetched = coerce_fetch_result(self.fetcher(url), fallback_url=url)
        if page_cache.mode == "record":
            page_cache.store(url, final_url=fetched.url, status_code=fetched.status_code, html=fetched.html)
        return fetched, "network"

    def _page_cache(self, config: dict[str, Any]) -> PageResponseCache:
        cache = PageResponseCache(
            mode=str(config.get("cache_mode") or "off"),
            cache_dir=str(config.get("cache_dir") or ".cache/page_responses"),
            session_id=str(config.get("cache_session_id") or config.get("run_id") or "default"),
            replay_fallback_on_miss=bool(config.get("cache_replay_fallback")),
        )
        self.page_caches[cache.session_id] = cache
        return cache

    def _extract_items(self, fetched: FetchResult, config: dict[str, Any]) -> list[dict[str, Any]]:
        extract_config = config.get("extract") or {}
        if not extract_config:
            return []
        if extract_config.get("selector") or extract_config.get("selector_type") or extract_config.get("type") in {"css", "xpath", "text", "regex"}:
            selected = generic.select(
                fetched.html,
                selector=str(extract_config.get("selector") or ""),
                selector_type=str(extract_config.get("selector_type") or extract_config.get("type") or "css"),
                mode=str(extract_config.get("mode") or "all"),
                output=str(extract_config.get("output") or "text"),
                attr=str(extract_config.get("attr") or ""),
                text=str(extract_config.get("text") or ""),
                regex=str(extract_config.get("regex") or ""),
                tag=str(extract_config.get("tag") or ""),
                max_results=int(extract_config.get("max_results") or 100),
                case_sensitive=bool(extract_config.get("case_sensitive")),
            )
            return [
                {"url": fetched.url, "value": value}
                for value in selected.get("results") or []
            ]
        extracted = generic.extract(
            fetched.html,
            source_type=str(extract_config.get("source_type") or "html"),
            requested_fields=extract_config.get("requested_fields") or None,
            max_rows=int(extract_config.get("max_rows") or 1000),
        )
        rows = []
        for row in extracted.get("rows") or []:
            item = dict(row)
            item.setdefault("url", fetched.url)
            rows.append(item)
        return rows

    def _apply_sitemap_seeds(self, config: dict[str, Any]) -> None:
        """Opt-in: expand ``start_urls`` from a sitemap before crawling.

        No-op unless ``seed_sitemap`` is set, so the default crawl path stays
        byte-identical. Seeds discovered via :class:`url_seeder.UrlSeeder` are
        merged after any explicit ``start_urls`` (deduped, explicit-first) and
        their hosts are unioned into ``allowed_domains`` so the domain gate in
        :meth:`run` lets them through.
        """
        if not config.get("seed_sitemap"):
            return
        from visual_web_agent.url_seeder import UrlSeeder

        seeded = UrlSeeder(self.fetcher).seed_from_sitemap(
            config["seed_sitemap"],
            max_urls=max(1, config["max_pages"]) * 5,
            allowed_domains=config["allowed_domains"] or None,
        )
        merged = list(config["start_urls"])
        seen = set(merged)
        for url in seeded:
            if url and url not in seen:
                seen.add(url)
                merged.append(url)
        config["start_urls"] = merged
        if config["allowed_domains"]:
            extra = {domain_of(url) for url in seeded if domain_of(url)}
            config["allowed_domains"] = sorted(set(config["allowed_domains"]) | extra)
        else:
            config["allowed_domains"] = sorted({domain_of(url) for url in merged if domain_of(url)})

    def _load_resume_state(self, config: dict[str, Any]) -> dict[str, Any] | None:
        """Load a prior checkpoint for ``resume_state_path`` (None if absent)."""
        path = config.get("resume_state_path")
        if not path:
            return None
        from visual_web_agent.crawl_checkpoint import load_checkpoint

        return load_checkpoint(path)

    def _maybe_checkpoint(
        self,
        config: dict[str, Any],
        result: dict[str, Any],
        seen: set[str],
        frontier: Any,
        *,
        force: bool = False,
    ) -> None:
        """Persist crawl progress to ``resume_state_path`` (opt-in, no-op off).

        Saves every ``checkpoint_every`` pages and once at the end (``force``)
        so even a crawl shorter than the cadence still leaves a checkpoint.
        """
        path = config.get("resume_state_path")
        if not path:
            return
        if not force and len(result["pages"]) % max(1, config.get("checkpoint_every", 1)):
            return
        from visual_web_agent.crawl_checkpoint import save_checkpoint

        save_checkpoint(path, {
            "run_id": config["run_id"],
            "strategy": config["crawl_strategy"],
            "keywords": config["keywords"],
            "seen": sorted(seen),
            "pending": frontier.snapshot(),
            "pages": result["pages"],
            "items": result["items"],
            "errors": result["errors"],
        })

    def _config(self, payload: dict[str, Any]) -> dict[str, Any]:
        starts = payload.get("start_urls") or payload.get("urls") or []
        if isinstance(starts, str):
            starts = [starts]
        single = payload.get("url") or payload.get("start_url")
        if single:
            starts = [single, *list(starts)]
        start_urls = [normalize_url(url) for url in starts if normalize_url(url)]
        seed_sitemap = str(payload.get("seed_sitemap") or payload.get("sitemap") or "").strip()
        if not start_urls and not seed_sitemap:
            raise ValueError("start_urls are required")
        allowed = payload.get("allowed_domains") or []
        if isinstance(allowed, str):
            allowed = [allowed]
        allowed_domains = [domain_of(str(item)) for item in allowed if domain_of(str(item))]
        if not allowed_domains:
            allowed_domains = sorted({domain_of(url) for url in start_urls if domain_of(url)})
        run_id = str(payload.get("run_id") or f"spider_{uuid.uuid4().hex[:12]}")
        return {
            "run_id": run_id,
            "start_urls": start_urls,
            "allowed_domains": allowed_domains,
            "max_depth": max(0, min(int(payload.get("max_depth") or 0), 10)),
            "max_pages": max(1, min(int(payload.get("max_pages") or 10), 1000)),
            "follow_links": bool(payload.get("follow_links", True)),
            "robots_txt_obey": bool(payload.get("robots_txt_obey") or payload.get("obey_robots")),
            "delay_seconds": max(0.0, float(payload.get("delay_seconds") or 0.0) + float(payload.get("delay_ms") or 0.0) / 1000.0),
            "extract": dict(payload.get("extract") or {}),
            "cache_mode": str(payload.get("cache_mode") or payload.get("page_cache_mode") or "off"),
            "cache_dir": str(payload.get("cache_dir") or ".cache/page_responses"),
            "cache_session_id": str(payload.get("cache_session_id") or run_id),
            "cache_replay_fallback": bool(payload.get("cache_replay_fallback")),
            "export": bool(payload.get("export")),
            "export_format": str(payload.get("export_format") or payload.get("feed_format") or "jsonl"),
            "export_filename": str(payload.get("export_filename") or payload.get("filename") or ""),
            "item_pipeline": self._item_pipeline_config(payload),
            "crawl_strategy": _crawl_strategy(payload),
            "keywords": normalize_keywords(payload.get("keywords") or payload.get("relevance_keywords") or payload.get("relevance_query") or payload.get("crawl_keywords") or ""),
            "seed_sitemap": seed_sitemap,
            "resume_state_path": str(payload.get("resume_state_path") or payload.get("resume_state") or "").strip(),
            "checkpoint_every": max(1, min(int(payload.get("checkpoint_every") or 1), 1000)),
        }

    def _item_pipeline_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        pipeline = dict(payload.get("item_pipeline") or payload.get("pipeline") or {})
        if "fields" not in pipeline and payload.get("item_fields") is not None:
            pipeline["fields"] = payload.get("item_fields")
        if "required_fields" not in pipeline and payload.get("required_fields") is not None:
            pipeline["required_fields"] = payload.get("required_fields")
        if "dedupe_by" not in pipeline and payload.get("dedupe_by") is not None:
            pipeline["dedupe_by"] = payload.get("dedupe_by")
        if "drop_empty" not in pipeline and payload.get("drop_empty_items") is not None:
            pipeline["drop_empty"] = payload.get("drop_empty_items")
        if "max_items" not in pipeline and payload.get("max_items") is not None:
            pipeline["max_items"] = payload.get("max_items")
        return pipeline


_VALID_CRAWL_STRATEGIES = {"bfs", "best_first"}


def _crawl_strategy(payload: dict[str, Any]) -> str:
    strat = str(payload.get("crawl_strategy") or ("best_first" if payload.get("best_first") else "bfs")).strip().lower()
    return strat if strat in _VALID_CRAWL_STRATEGIES else "bfs"


def default_fetch(url: str) -> FetchResult:
    check_url(url)  # SSRF guard: reject private/loopback/metadata hosts + non-http schemes
    req = Request(str(url), headers={"User-Agent": "VSpider-SpiderLite/1.0"})
    with build_guarded_opener().open(req, timeout=15) as resp:
        raw = resp.read(2_000_000)
        content_type = resp.headers.get("content-type", "")
        charset = "utf-8"
        for part in content_type.split(";"):
            if "charset=" in part.lower():
                charset = part.split("=", 1)[1].strip() or "utf-8"
        html = raw.decode(charset, errors="replace")
        return FetchResult(url=resp.geturl(), status_code=int(getattr(resp, "status", 200) or 200), html=html)


def coerce_fetch_result(value: FetchResult | dict[str, Any] | str, *, fallback_url: str) -> FetchResult:
    if isinstance(value, FetchResult):
        return value
    if isinstance(value, dict):
        return FetchResult(
            url=str(value.get("url") or value.get("final_url") or fallback_url),
            status_code=int(value.get("status_code") or value.get("status") or 200),
            html=str(value.get("html") or value.get("text") or ""),
        )
    return FetchResult(url=fallback_url, status_code=200, html=str(value or ""))


def extract_links(html: str, base_url: str) -> list[str]:
    return [url for url, _anchor in extract_links_with_anchors(html, base_url)]


def extract_links_with_anchors(html: str, base_url: str) -> list[tuple[str, str]]:
    parser = _LinkParser()
    parser.feed(str(html or ""))
    parser.close()
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, anchor in parser.links:
        joined = normalize_url(urljoin(str(base_url or ""), href))
        if joined and joined not in seen:
            seen.add(joined)
            out.append((joined, anchor))
    return out


def normalize_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    clean, _fragment = urldefrag(text)
    parsed = urlparse(clean)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return clean


def domain_of(url: str) -> str:
    text = str(url or "").strip().lower()
    parsed = urlparse(text if "://" in text else f"http://{text}")
    return parsed.netloc.split("@")[-1].split(":", 1)[0]


def safe_run_id(value: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in str(value or "spider"))
    return text.strip("._-") or "spider"


def normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        parts = [str(part).strip() for part in value]
    else:
        parts = [str(value).strip()]
    out: list[str] = []
    for part in parts:
        if part and part not in out:
            out.append(part)
    return out


def clean_scalar(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.split())
    return value


def not_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True
