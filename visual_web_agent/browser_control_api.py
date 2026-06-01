from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, Body, HTTPException


@dataclass(frozen=True)
class BrowserControlApiDeps:
    browser_control: Callable[[], Any]
    http_exception_detail: Callable[[Exception], Any]


def create_browser_control_router(deps: BrowserControlApiDeps) -> APIRouter:
    router = APIRouter(tags=["browser_control"])

    def control() -> Any:
        return deps.browser_control()

    def detail(exc: Exception) -> Any:
        return deps.http_exception_detail(exc)

    @router.get("/api/browser_control/sessions", summary="列出低层浏览器控制会话（Y18）")
    async def list_browser_control_sessions() -> dict:
        return {
            "status": "success",
            "sessions": control().list_sessions(),
        }

    @router.get("/api/browser_control/backend", summary="查看浏览器控制后端适配层状态（Y47）")
    async def get_browser_control_backend() -> dict:
        return {
            "status": "success",
            "result": control().backend_status(),
        }

    @router.post("/api/browser_control/tabs", summary="列出浏览器控制标签页（Y20）")
    async def browser_control_tabs(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().tabs(
                session_id=str(payload.get("session_id") or "default"),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/tab/new", summary="新建浏览器控制标签页（Y20）")
    async def browser_control_tab_new(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().tab_new(
                str(payload.get("url") or ""),
                session_id=str(payload.get("session_id") or "default"),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/tab/switch", summary="切换浏览器控制标签页（Y20）")
    async def browser_control_tab_switch(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().tab_switch(
                int(payload.get("index") or 0),
                session_id=str(payload.get("session_id") or "default"),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/tab/close", summary="关闭浏览器控制标签页（Y20）")
    async def browser_control_tab_close(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().tab_close(
                int(payload["index"]) if "index" in payload else None,
                session_id=str(payload.get("session_id") or "default"),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/open", summary="打开浏览器控制会话并导航（Y18）")
    async def browser_control_open(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().open(
                str(payload.get("url") or ""),
                session_id=str(payload.get("session_id") or "default"),
                headed=bool(payload.get("headed")),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/snapshot", summary="获取浏览器控制快照（Y18）")
    async def browser_control_snapshot(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().snapshot(
                session_id=str(payload.get("session_id") or "default"),
                interactive=bool(payload.get("interactive", True)),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "snapshot": result}

    @router.post("/api/browser_control/find", summary="使用语义 locator 查找并操作元素（Y21）")
    async def browser_control_find(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().find(
                str(payload.get("strategy") or ""),
                str(payload.get("query") or ""),
                session_id=str(payload.get("session_id") or "default"),
                action=str(payload.get("action") or "text"),
                name=str(payload.get("name") or ""),
                value=str(payload.get("value") or ""),
                attr=str(payload.get("attr") or ""),
                index=int(payload.get("index") or 0),
                exact=bool(payload.get("exact", True)),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/selector", summary="生成浏览器控制 ref 的稳定选择器（Y26）")
    async def browser_control_selector(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().selector(
                str(payload.get("ref") or ""),
                session_id=str(payload.get("session_id") or "default"),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/similar", summary="查找与浏览器控制 ref 相似的元素（Y26）")
    async def browser_control_similar(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().similar(
                str(payload.get("ref") or ""),
                session_id=str(payload.get("session_id") or "default"),
                limit=int(payload.get("limit") or 20),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/click", summary="点击浏览器控制快照 ref（Y18）")
    async def browser_control_click(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().click(
                str(payload.get("ref") or ""),
                session_id=str(payload.get("session_id") or "default"),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/dblclick", summary="双击浏览器控制快照 ref（Y19）")
    async def browser_control_dblclick(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().dblclick(
                str(payload.get("ref") or ""),
                session_id=str(payload.get("session_id") or "default"),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/focus", summary="聚焦浏览器控制快照 ref（Y19）")
    async def browser_control_focus(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().focus(
                str(payload.get("ref") or ""),
                session_id=str(payload.get("session_id") or "default"),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/hover", summary="悬停浏览器控制快照 ref（Y18）")
    async def browser_control_hover(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().hover(
                str(payload.get("ref") or ""),
                session_id=str(payload.get("session_id") or "default"),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/fill", summary="填充浏览器控制快照 ref（Y18）")
    async def browser_control_fill(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().fill(
                str(payload.get("ref") or ""),
                str(payload.get("text") or ""),
                session_id=str(payload.get("session_id") or "default"),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/check", summary="勾选浏览器控制快照 ref（Y19）")
    async def browser_control_check(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().check(
                str(payload.get("ref") or ""),
                checked=bool(payload.get("checked", True)),
                session_id=str(payload.get("session_id") or "default"),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/uncheck", summary="取消勾选浏览器控制快照 ref（Y19）")
    async def browser_control_uncheck(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().check(
                str(payload.get("ref") or ""),
                checked=False,
                session_id=str(payload.get("session_id") or "default"),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/select", summary="选择下拉框选项（Y19）")
    async def browser_control_select(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().select(
                str(payload.get("ref") or ""),
                str(payload.get("value") or ""),
                session_id=str(payload.get("session_id") or "default"),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/scrollintoview", summary="滚动元素进入视口（Y19）")
    async def browser_control_scroll_into_view(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().scroll_into_view(
                str(payload.get("ref") or ""),
                session_id=str(payload.get("session_id") or "default"),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/upload", summary="上传文件到 input ref（Y19）")
    async def browser_control_upload(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().upload(
                str(payload.get("ref") or ""),
                payload.get("paths") or payload.get("path") or "",
                session_id=str(payload.get("session_id") or "default"),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/type", summary="输入文本到浏览器控制快照 ref（Y18）")
    async def browser_control_type(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().type(
                str(payload.get("ref") or ""),
                str(payload.get("text") or ""),
                session_id=str(payload.get("session_id") or "default"),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/press", summary="按键浏览器控制会话（Y18）")
    async def browser_control_press(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().press(
                str(payload.get("key") or ""),
                ref=str(payload.get("ref") or ""),
                session_id=str(payload.get("session_id") or "default"),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/scroll", summary="滚动浏览器控制页面（Y18）")
    async def browser_control_scroll(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().scroll(
                str(payload.get("direction") or "down"),
                int(payload.get("amount") or 500),
                session_id=str(payload.get("session_id") or "default"),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/wait", summary="等待浏览器控制页面状态（Y18）")
    async def browser_control_wait(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().wait(
                session_id=str(payload.get("session_id") or "default"),
                ms=int(payload.get("ms") or 0),
                ref=str(payload.get("ref") or ""),
                text=str(payload.get("text") or ""),
                load_state=str(payload.get("load_state") or ""),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/eval", summary="执行浏览器控制 JavaScript（Y18）")
    async def browser_control_eval(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().evaluate(
                str(payload.get("script") or ""),
                session_id=str(payload.get("session_id") or "default"),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/navigate", summary="浏览器控制后退/前进/刷新（Y18）")
    async def browser_control_navigate(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().navigate(
                str(payload.get("action") or ""),
                session_id=str(payload.get("session_id") or "default"),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/get", summary="读取浏览器控制页面信息（Y18）")
    async def browser_control_get(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().get(
                str(payload.get("kind") or ""),
                ref=str(payload.get("ref") or ""),
                attr=str(payload.get("attr") or ""),
                selector=str(payload.get("selector") or ""),
                session_id=str(payload.get("session_id") or "default"),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/is", summary="检查浏览器控制元素状态（Y19）")
    async def browser_control_is(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().is_state(
                str(payload.get("kind") or ""),
                str(payload.get("ref") or ""),
                session_id=str(payload.get("session_id") or "default"),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/screenshot", summary="浏览器控制截图（Y18）")
    async def browser_control_screenshot(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().screenshot(
                session_id=str(payload.get("session_id") or "default"),
                path=str(payload.get("path") or ""),
                full_page=bool(payload.get("full_page")),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/pdf", summary="浏览器控制导出 PDF（Y19）")
    async def browser_control_pdf(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().pdf(
                session_id=str(payload.get("session_id") or "default"),
                path=str(payload.get("path") or ""),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/cookies", summary="读取浏览器控制 Cookie（Y20）")
    async def browser_control_cookies(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().cookies(
                session_id=str(payload.get("session_id") or "default"),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/cookies/set", summary="设置浏览器控制 Cookie（Y20）")
    async def browser_control_cookies_set(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().cookies_set(
                str(payload.get("name") or ""),
                str(payload.get("value") or ""),
                url=str(payload.get("url") or ""),
                session_id=str(payload.get("session_id") or "default"),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/cookies/clear", summary="清空浏览器控制 Cookie（Y20）")
    async def browser_control_cookies_clear(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().cookies_clear(
                session_id=str(payload.get("session_id") or "default"),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/storage/local", summary="读取/写入浏览器控制 localStorage（Y20）")
    async def browser_control_storage_local(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().storage_local(
                session_id=str(payload.get("session_id") or "default"),
                key=str(payload.get("key") or ""),
                value=str(payload["value"]) if "value" in payload else None,
                clear=bool(payload.get("clear")),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/console", summary="读取浏览器控制 console 消息（Y20）")
    async def browser_control_console(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = control().console(
                session_id=str(payload.get("session_id") or "default"),
                clear=bool(payload.get("clear")),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/errors", summary="读取浏览器控制页面错误（Y20）")
    async def browser_control_errors(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = control().errors(
                session_id=str(payload.get("session_id") or "default"),
                clear=bool(payload.get("clear")),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/network/requests", summary="读取浏览器控制网络请求（Y20）")
    async def browser_control_network_requests(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = control().network_requests(
                session_id=str(payload.get("session_id") or "default"),
                filter_text=str(payload.get("filter") or ""),
                clear=bool(payload.get("clear")),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    @router.post("/api/browser_control/close", summary="关闭浏览器控制会话（Y18）")
    async def browser_control_close(payload: dict[str, Any] = Body(default_factory=dict)) -> dict:
        try:
            result = await control().close(str(payload.get("session_id") or "default"))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=detail(exc)) from exc
        return {"status": "success", "result": result}

    return router
