"""内置 MCP Streamable HTTP 端点。

Octop 通过自定义 MCP 连接器直接调用本应用：

    http://127.0.0.1:8670/mcp
    Authorization: Bearer <NAS_APP_TOKEN>

工具列表与 Web UI 共用 ToolRegistry；长任务交给 JobManager，避免客户端
5 分钟读超时。配置读写仍然只发生在 Web UI / REST，本模块不暴露保存配置工具。
"""

from __future__ import annotations

import hmac
import inspect
import json
import logging
import threading
import time
from typing import Annotated, Any, Callable

import anyio
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .jobs import Job, JobManager
from .settings import AppSettings
from .toolkit import ToolRegistry, ToolSpec

logger = logging.getLogger("nas_app.mcp")

LONG_RUNNING_WAIT_SECONDS = 240.0
_START_TIMEOUT_SECONDS = 10.0
_STOP_TIMEOUT_SECONDS = 8.0

INSTRUCTIONS = """\
你是 NAS 媒体工具箱（nas-app）。所有文件路径都必须位于用户已授权的白名单内；
写/删操作还受 Web UI 中 enable_write / enable_organize / enable_delete 开关限制。

- 配置、白名单、密钥只能由用户在 Web UI（http://<NAS>:8669）修改；本 MCP 没有保存配置的工具。
- 长任务（扫描、体检、整理、批量补字幕、混流等）可能返回 status=queued/running；
  此时请用 nas_job_status 轮询 job_id，不要重复提交同一任务。
- 任务结果也会出现在 Web UI 的「后台任务」里。
- 在 Octop 中工具名会带连接器前缀，例如 nas-app_nas_list_directory。
"""


def _type_for(kind: str, required: bool) -> Any:
    if kind == "array":
        base: Any = list[str] | str
    elif kind == "integer":
        base = int | str if not required else int
    elif kind == "number":
        base = float | str if not required else float
    elif kind == "boolean":
        base = bool | str if not required else bool
    else:
        base = str
    if required:
        return base
    return base | None


def _make_signature(spec: ToolSpec) -> inspect.Signature:
    """把 ToolSpec.params 转成 FastMCP 能识别的 inspect.Signature。

    可选标量额外接受 str：Octop 会把 anyOf schema 映射成字符串，
    registry._prepare_args() 再转回真正的 int/float/bool。
    """
    parameters: list[inspect.Parameter] = []
    for item in spec.params:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        kind = str(item.get("type") or "string")
        required = bool(item.get("required"))
        annotation = _type_for(kind, required)
        description = str(item.get("description") or "").strip()
        if description:
            annotation = Annotated[annotation, Field(description=description)]
        default = inspect.Parameter.empty if required else item.get("default", None)
        parameters.append(
            inspect.Parameter(
                name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=default,
                annotation=annotation,
            )
        )
    return inspect.Signature(parameters)


def _job_payload(job: Job) -> dict[str, Any]:
    pending = job.status in {"queued", "running"}
    return {
        "ok": job.status == "succeeded",
        "pending": pending,
        "job_id": job.id,
        "tool": job.tool,
        "title": job.title,
        "status": job.status,
        "result": job.result,
        "error": job.error,
        "duration_ms": job.duration_ms,
    }


def make_tool_function(
    registry: ToolRegistry, jobs: JobManager, spec: ToolSpec
) -> Callable[..., Any]:
    """为单个 ToolSpec 生成 async 包装，避免阻塞 MCP 事件循环。"""

    async def _invoke(**kwargs: Any) -> dict[str, Any]:
        args = {key: value for key, value in kwargs.items() if value is not None}
        if spec.long_running:
            job = jobs.submit(spec.name, args)
            done = await anyio.to_thread.run_sync(
                lambda: jobs.wait(job.id, LONG_RUNNING_WAIT_SECONDS)
            )
            return _job_payload(done or job)
        return await anyio.to_thread.run_sync(
            lambda: registry.call(spec.name, args)
        )

    _invoke.__name__ = spec.name
    _invoke.__qualname__ = spec.name
    _invoke.__doc__ = spec.description or spec.title
    _invoke.__signature__ = _make_signature(spec)  # type: ignore[attr-defined]
    return _invoke


def _annotations(spec: ToolSpec) -> ToolAnnotations:
    return ToolAnnotations(
        title=spec.title,
        readOnlyHint=spec.risk == "read",
        destructiveHint=spec.risk == "delete",
        openWorldHint=spec.category in {"资源", "字幕"},
    )


def _register_job_tools(mcp: FastMCP, jobs: JobManager) -> None:
    async def nas_job_status(job_id: str) -> dict[str, Any]:
        """查询一个后台任务的当前状态与结果。"""
        job = jobs.get(job_id.strip())
        if job is None:
            return {
                "ok": False,
                "error": {"code": "not_found", "message": f"任务不存在: {job_id}"},
            }
        return _job_payload(job)

    async def nas_job_list(limit: int = 20) -> dict[str, Any]:
        """列出最近的后台任务（新到旧，最多 100 条）。"""
        count = max(1, min(int(limit), 100))
        return {"ok": True, "jobs": [_job_payload(job) for job in jobs.list(count)]}

    mcp.add_tool(
        nas_job_status,
        name="nas_job_status",
        title="任务状态",
        description=nas_job_status.__doc__,
        annotations=ToolAnnotations(
            title="任务状态", readOnlyHint=True, openWorldHint=False
        ),
        structured_output=False,
    )
    mcp.add_tool(
        nas_job_list,
        name="nas_job_list",
        title="任务列表",
        description=nas_job_list.__doc__,
        annotations=ToolAnnotations(
            title="任务列表", readOnlyHint=True, openWorldHint=False
        ),
        structured_output=False,
    )


def build_mcp_server(
    settings: AppSettings, registry: ToolRegistry, jobs: JobManager
) -> FastMCP:
    mcp = FastMCP(
        name="nas-app",
        instructions=INSTRUCTIONS,
        host=settings.mcp_host,
        port=settings.mcp_port,
        streamable_http_path=settings.mcp_path,
        json_response=True,
        stateless_http=True,
        log_level="WARNING",
    )
    for spec in registry.specs():
        mcp.add_tool(
            make_tool_function(registry, jobs, spec),
            name=spec.name,
            title=spec.title,
            description=spec.description or spec.title,
            annotations=_annotations(spec),
            structured_output=False,
        )
    _register_job_tools(mcp, jobs)
    return mcp


class BearerAuthMiddleware:
    """只接受 Authorization: Bearer <token> 的纯 ASGI 中间件。"""

    def __init__(self, app: Any, token: str):
        self.app = app
        self._expected = f"Bearer {token}".encode("utf-8")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"authorization", b"")
        if not hmac.compare_digest(provided, self._expected):
            body = json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "unauthorized",
                        "message": "缺少或错误的访问令牌",
                    },
                },
                ensure_ascii=False,
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"www-authenticate", b'Bearer realm="nas-app"'),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


class McpEndpoint:
    """在 daemon 线程里运行 uvicorn，主线程继续跑原有 stdlib HTTP 服务。"""

    def __init__(self, settings: AppSettings, registry: ToolRegistry, jobs: JobManager):
        self.settings = settings
        self.registry = registry
        self.jobs = jobs
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._mcp: FastMCP | None = None
        display_host = (
            "127.0.0.1"
            if settings.mcp_host in {"0.0.0.0", "::", "[::]"}
            else settings.mcp_host
        )
        self.url = f"http://{display_host}:{settings.mcp_port}{settings.mcp_path}"

    @property
    def started(self) -> bool:
        return bool(self._server and self._server.started)

    def start(self) -> None:
        if not self.settings.token:
            raise RuntimeError("MCP 必须配置 NAS_APP_TOKEN 才能启动")
        mcp = build_mcp_server(self.settings, self.registry, self.jobs)
        app: Any = mcp.streamable_http_app()
        app = BearerAuthMiddleware(app, self.settings.token)
        config = uvicorn.Config(
            app,
            host=self.settings.mcp_host,
            port=self.settings.mcp_port,
            log_level="warning",
            access_log=False,
            lifespan="on",
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, name="nas-app-mcp", daemon=True)
        self._mcp = mcp
        self._server = server
        self._thread = thread
        thread.start()

        deadline = time.monotonic() + _START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if server.started:
                logger.info("MCP 已启动：%s（Bearer 鉴权已启用）", self.url)
                return
            if not thread.is_alive():
                self._server = None
                self._thread = None
                raise RuntimeError("MCP 线程提前退出，端口可能已被占用")
            time.sleep(0.05)
        self.stop()
        raise RuntimeError(f"MCP 启动超时（{_START_TIMEOUT_SECONDS:.0f}s）")

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.should_exit = True
        if thread is not None and thread.is_alive():
            thread.join(timeout=_STOP_TIMEOUT_SECONDS)
        if thread is not None and thread.is_alive():
            logger.warning("MCP 线程未在 %.0fs 内退出", _STOP_TIMEOUT_SECONDS)

    def serve_forever(self) -> None:
        """阻塞运行（测试/独立诊断用）；正常应用走 start()/stop()。"""
        if not self.settings.token:
            raise RuntimeError("MCP 必须配置 NAS_APP_TOKEN 才能启动")
        mcp = build_mcp_server(self.settings, self.registry, self.jobs)
        app: Any = BearerAuthMiddleware(mcp.streamable_http_app(), self.settings.token)
        self._mcp = mcp
        uvicorn.run(
            app,
            host=self.settings.mcp_host,
            port=self.settings.mcp_port,
            log_level="warning",
            access_log=False,
            lifespan="on",
        )
