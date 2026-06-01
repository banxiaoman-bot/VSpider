from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


@dataclass
class RobotsRules:
    user_agent: str = "*"
    allow: list[str] = field(default_factory=list)
    disallow: list[str] = field(default_factory=list)
    crawl_delay: float | None = None
    request_rate: tuple[int, float] | None = None


class RobotsPolicyManager:
    def __init__(self) -> None:
        self.rules_by_domain: dict[str, RobotsRules] = {}
        self.next_allowed_at: dict[str, float] = {}

    def set_robots(self, domain: str, text: str, *, user_agent: str = "*") -> dict[str, Any]:
        key = self._domain(domain)
        if not key:
            raise ValueError("domain is required")
        rules = parse_robots(text, user_agent=user_agent)
        self.rules_by_domain[key] = rules
        return self.public_rules(key)

    def public_rules(self, domain: str) -> dict[str, Any]:
        key = self._domain(domain)
        rules = self.rules_by_domain.get(key, RobotsRules())
        return {
            "domain": key,
            "user_agent": rules.user_agent,
            "allow": list(rules.allow),
            "disallow": list(rules.disallow),
            "crawl_delay": rules.crawl_delay,
            "request_rate": {
                "requests": rules.request_rate[0],
                "seconds": rules.request_rate[1],
            } if rules.request_rate else None,
            "next_allowed_at": self.next_allowed_at.get(key, 0.0),
        }

    def check_url(self, url: str, *, obey: bool = True, now: float | None = None) -> dict[str, Any]:
        parsed = urlparse(str(url or ""))
        domain = self._domain(parsed.netloc)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        if not domain:
            raise ValueError("valid http(s) url is required")
        rules = self.rules_by_domain.get(domain, RobotsRules())
        allowed_by_robots = True if not obey else is_allowed(path, rules)
        ts = time.time() if now is None else float(now)
        next_at = float(self.next_allowed_at.get(domain, 0.0))
        wait_seconds = max(0.0, next_at - ts)
        return {
            "url": str(url or ""),
            "domain": domain,
            "path": path,
            "allowed": bool(allowed_by_robots and wait_seconds <= 0),
            "allowed_by_robots": bool(allowed_by_robots),
            "throttled": wait_seconds > 0,
            "wait_seconds": wait_seconds,
            "next_allowed_at": next_at,
            "rules": self.public_rules(domain),
        }

    def reserve_url(self, url: str, *, obey: bool = True, now: float | None = None, default_delay: float = 0.0) -> dict[str, Any]:
        ts = time.time() if now is None else float(now)
        check = self.check_url(url, obey=obey, now=ts)
        if not check["allowed_by_robots"]:
            return {**check, "reserved": False}
        domain = str(check["domain"])
        rules = self.rules_by_domain.get(domain, RobotsRules())
        delay = delay_seconds(rules, default_delay=default_delay)
        start_at = max(ts, float(self.next_allowed_at.get(domain, 0.0)))
        self.next_allowed_at[domain] = start_at + delay
        updated = self.check_url(url, obey=obey, now=ts)
        return {**updated, "reserved": True, "reserved_at": start_at, "delay_seconds": delay}

    @staticmethod
    def _domain(value: str) -> str:
        text = str(value or "").strip().lower()
        if "://" in text:
            text = urlparse(text).netloc
        return text.split("@")[-1].split(":", 1)[0]


def parse_robots(text: str, *, user_agent: str = "*") -> RobotsRules:
    wanted = str(user_agent or "*").lower()
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    agents: list[str] = []
    directives: list[tuple[str, str]] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        k = key.strip().lower()
        v = value.strip()
        if k == "user-agent":
            if agents and directives:
                groups.append((agents, directives))
                directives = []
            agents.append(v.lower())
        elif agents:
            directives.append((k, v))
    if agents:
        groups.append((agents, directives))
    chosen = _choose_group(groups, wanted)
    rules = RobotsRules(user_agent=user_agent)
    for key, value in chosen:
        if key == "allow" and value:
            rules.allow.append(value)
        elif key == "disallow":
            if value:
                rules.disallow.append(value)
        elif key == "crawl-delay":
            try:
                rules.crawl_delay = max(0.0, float(value))
            except ValueError:
                pass
        elif key == "request-rate":
            parsed = _parse_request_rate(value)
            if parsed is not None:
                rules.request_rate = parsed
    return rules


def is_allowed(path: str, rules: RobotsRules) -> bool:
    target = str(path or "/") or "/"
    allow_match = _longest_prefix(target, rules.allow)
    disallow_match = _longest_prefix(target, rules.disallow)
    if disallow_match == "":
        return True
    if len(allow_match) >= len(disallow_match) and allow_match:
        return True
    return False


def delay_seconds(rules: RobotsRules, *, default_delay: float = 0.0) -> float:
    if rules.crawl_delay is not None:
        return max(0.0, float(rules.crawl_delay))
    if rules.request_rate:
        requests, seconds = rules.request_rate
        if requests > 0:
            return max(0.0, float(seconds) / float(requests))
    return max(0.0, float(default_delay or 0.0))


def _choose_group(groups: list[tuple[list[str], list[tuple[str, str]]]], wanted: str) -> list[tuple[str, str]]:
    fallback: list[tuple[str, str]] = []
    for agents, directives in groups:
        if any(agent == wanted for agent in agents):
            return directives
        if any(agent == "*" for agent in agents):
            fallback = directives
    return fallback


def _longest_prefix(path: str, patterns: list[str]) -> str:
    best = ""
    for pattern in patterns:
        p = str(pattern or "")
        if p and path.startswith(p) and len(p) > len(best):
            best = p
    return best


def _parse_request_rate(value: str) -> tuple[int, float] | None:
    raw = str(value or "").strip()
    if "/" not in raw:
        return None
    left, right = raw.split("/", 1)
    try:
        requests = int(left.strip())
        seconds = float(right.strip())
    except ValueError:
        return None
    if requests <= 0 or seconds < 0:
        return None
    return requests, seconds
