from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, Body, HTTPException


@dataclass(frozen=True)
class CapabilityFailureFixtureApiDeps:
    build_fixture: Callable[..., dict[str, Any]]
    replay_fixture: Callable[[dict[str, Any]], dict[str, Any]]
    replay_fixtures: Callable[[list[dict[str, Any]]], dict[str, Any]]
    fixture_source: Callable[[dict[str, Any]], dict[str, Any]]
    replay_source: Callable[[dict[str, Any]], dict[str, Any]]
    list_fixture_artifacts: Callable[..., list[dict[str, Any]]]
    write_fixture_artifact: Callable[[dict[str, Any], dict[str, Any]], dict[str, str]]
    write_replay_artifact: Callable[[dict[str, Any], dict[str, Any]], dict[str, str]]
    write_batch_artifact: Callable[[dict[str, Any], dict[str, Any]], dict[str, str]]
    list_batch_artifacts: Callable[..., list[dict[str, Any]]]
    batch_history_trend: Callable[[list[dict[str, Any]]], dict[str, Any]]


def create_capability_failure_fixture_router(deps: CapabilityFailureFixtureApiDeps) -> APIRouter:
    router = APIRouter(tags=["capability_failure_fixtures"])

    @router.post("/api/capabilities/failure_fixture", summary="从 capability 失败 trace 生成回归 fixture（Y88）")
    async def build_capability_failure_fixture_route(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        tags = [str(item) for item in (payload.get("tags") or []) if str(item or "")] if isinstance(payload.get("tags"), list) else []
        fixture = deps.build_fixture(
            deps.fixture_source(payload),
            name=str(payload.get("name") or ""),
            tags=tags,
        )
        if not fixture:
            raise HTTPException(status_code=400, detail="source does not contain capability_execute_failure_bundle.v1")
        artifact = None
        if bool(payload.get("save") or payload.get("write_artifact")):
            artifact = deps.write_fixture_artifact(fixture, payload)
        return {"status": "success", "result": {"fixture": fixture, "artifact": artifact}}

    @router.get("/api/capabilities/failure_fixtures", summary="列出 capability failure fixture artifact（Y93）")
    async def list_capability_failure_fixtures_route(limit: int = 100) -> dict:
        items = deps.list_fixture_artifacts(limit=limit)
        fixtures = [{key: value for key, value in item.items() if key != "fixture"} for item in items]
        return {"status": "success", "result": {"fixture_count": len(fixtures), "fixtures": fixtures}}

    @router.post("/api/capabilities/failure_fixture/replay", summary="离线验证 capability failure fixture replay（Y91）")
    async def replay_capability_failure_fixture_route(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        report = deps.replay_fixture(deps.replay_source(payload))
        if str(report.get("fixture", {}).get("version") or "") != "capability_failure_regression_fixture.v1":
            raise HTTPException(status_code=400, detail="source does not contain capability_failure_regression_fixture.v1")
        artifact = None
        if bool(payload.get("save") or payload.get("write_artifact")):
            artifact = deps.write_replay_artifact(report, payload)
        return {"status": "success", "result": {"report": report, "artifact": artifact}}

    @router.post("/api/capabilities/failure_fixture/replay_batch", summary="批量离线验证 capability failure fixtures（Y93）")
    async def replay_capability_failure_fixture_batch_route(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        direct_fixtures = payload.get("fixtures")
        source_items: list[dict[str, Any]] = []
        if isinstance(direct_fixtures, list):
            fixtures = [dict(item) for item in direct_fixtures if isinstance(item, dict)]
            source_items = [{"name": str(item.get("name") or f"fixture_{idx + 1}")} for idx, item in enumerate(fixtures)]
        else:
            source_items = deps.list_fixture_artifacts(limit=int(payload.get("limit") or 100))
            fixtures = [dict(item.get("fixture") or {}) for item in source_items]
        report = deps.replay_fixtures(fixtures)
        for item, source in zip(report.get("items") or [], source_items):
            if isinstance(item, dict) and source.get("path"):
                item["artifact"] = {
                    "path": str(source.get("path") or ""),
                    "url": str(source.get("url") or ""),
                }
        artifact = None
        if bool(payload.get("save") or payload.get("write_artifact")):
            artifact = deps.write_batch_artifact(report, payload)
        return {"status": "success", "result": {"report": report, "artifact": artifact}}

    @router.get("/api/capabilities/failure_fixture/replay_batches", summary="列出 capability failure fixture batch replay report artifact（Y97）")
    async def list_capability_failure_fixture_replay_batches_route(limit: int = 50) -> dict:
        reports = deps.list_batch_artifacts(limit=limit)
        trend = deps.batch_history_trend(reports)
        return {"status": "success", "result": {"report_count": len(reports), "reports": reports, "trend": trend}}

    return router
