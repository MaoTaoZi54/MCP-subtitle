import asyncio
import dataclasses
import inspect
import json
import socket
import typing

import pytest

pytest.importorskip("mcp")

import httpx  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import PlainTextResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from nas_app.jobs import Job  # noqa: E402
from nas_app.mcp_endpoint import (  # noqa: E402
    LONG_RUNNING_WAIT_SECONDS,
    BearerAuthMiddleware,
    McpEndpoint,
    _make_signature,
    make_tool_function,
)
from nas_app.toolkit import ToolSpec  # noqa: E402

TOKEN = "secret-token"


def test_signature_marks_optional_scalars_and_arrays(registry):
    spec = next(s for s in registry.specs() if s.name == "nas_find_files")
    sig = _make_signature(spec)

    assert sig.parameters["extensions"].annotation == (list[str] | str | None)
    assert sig.parameters["extensions"].default is None
    assert sig.parameters["recursive"].annotation == (bool | str | None)
    assert sig.parameters["recursive"].default is True
    assert sig.parameters["limit"].annotation == (int | str | None)
    assert sig.parameters["keyword"].default == ""


def test_signature_keeps_required_params_required(registry):
    spec = next(s for s in registry.specs() if s.name == "nas_list_directory")
    param = _make_signature(spec).parameters["path"]

    assert param.default is inspect.Parameter.empty
    assert typing.get_args(param.annotation)[0] is str


def test_bearer_middleware():
    async def ok(request):
        return PlainTextResponse("ok")

    app = BearerAuthMiddleware(Starlette(routes=[Route("/mcp", ok)]), TOKEN)
    client = TestClient(app)

    response = client.get("/mcp")
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")

    response = client.get("/mcp", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401

    response = client.get("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.text == "ok"


class _FakeRegistry:
    def __init__(self):
        self.called: list[tuple[str, dict]] = []

    def call(self, name, args):
        self.called.append((name, args))
        return {"ok": True, "echo": args}


class _FakeJobs:
    def __init__(self):
        self.submitted: list[tuple[str, dict]] = []
        self.waited: list[tuple[str, float]] = []

    def submit(self, tool, args):
        self.submitted.append((tool, args))
        return Job(
            id="J-test",
            tool=tool,
            title="fake",
            args=args,
            status="succeeded",
            result={"ok": True},
        )

    def wait(self, job_id, timeout=LONG_RUNNING_WAIT_SECONDS):
        self.waited.append((job_id, timeout))
        return None


def _spec(name="tool", long_running=False, params=None):
    return ToolSpec(
        name=name,
        title=name,
        category="文件",
        risk="read",
        long_running=long_running,
        fn=lambda **kwargs: {"ok": True},
        description="测试工具",
        arg_docs={},
        params=params or [],
    )


def test_long_running_goes_through_jobs():
    registry = _FakeRegistry()
    jobs = _FakeJobs()
    spec = _spec(
        name="media_cleanup",
        long_running=True,
        params=[{"name": "dry_run", "type": "boolean", "required": False, "default": True}],
    )

    result = asyncio.run(make_tool_function(registry, jobs, spec)(dry_run="false"))

    assert jobs.submitted == [("media_cleanup", {"dry_run": "false"})]
    assert jobs.waited == [("J-test", LONG_RUNNING_WAIT_SECONDS)]
    assert registry.called == []
    assert result["ok"] is True
    assert result["job_id"] == "J-test"
    assert result["pending"] is False


def test_read_tool_runs_inline_and_drops_none():
    registry = _FakeRegistry()
    jobs = _FakeJobs()
    spec = _spec(
        name="nas_list_directory",
        params=[{"name": "path", "type": "string", "required": True}],
    )

    result = asyncio.run(make_tool_function(registry, jobs, spec)(path="/media", extra=None))

    assert jobs.submitted == []
    assert registry.called == [("nas_list_directory", {"path": "/media"})]
    assert result == {"ok": True, "echo": {"path": "/media"}}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_endpoint_end_to_end(settings, registry, jobs):
    media = settings.config_path.parent.parent / "media"
    config = dataclasses.replace(settings, mcp_host="127.0.0.1", mcp_port=_free_port())
    endpoint = McpEndpoint(config, registry, jobs)
    endpoint.start()
    try:
        async def scenario():
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {TOKEN}"},
                timeout=httpx.Timeout(20, read=60),
            ) as http_client:
                async with streamable_http_client(
                    endpoint.url, http_client=http_client
                ) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        names = {tool.name for tool in tools.tools}
                        assert len(names) == 30
                        assert {"nas_job_status", "nas_job_list", "nas_list_directory"} <= names

                        result = await session.call_tool(
                            "nas_list_directory", {"path": str(media)}
                        )
                        payload = json.loads(result.content[0].text)
                        assert payload["ok"] is True
                        assert [item["name"] for item in payload["entries"]] == ["movie.mkv"]

                        listing = await session.call_tool("nas_job_list", {"limit": 5})
                        assert json.loads(listing.content[0].text)["ok"] is True

        asyncio.run(scenario())
    finally:
        endpoint.stop()


def test_endpoint_requires_token(settings, registry, jobs):
    config = dataclasses.replace(settings, token="", mcp_port=_free_port())
    endpoint = McpEndpoint(config, registry, jobs)

    with pytest.raises(RuntimeError):
        endpoint.start()
