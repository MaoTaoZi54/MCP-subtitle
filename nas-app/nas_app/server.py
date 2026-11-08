"""独立应用的 HTTP 服务与 Web UI 静态资源。

只依赖标准库（外加能力层必需的 PyYAML），镜像小、启动快：
- REST API：/api/...（28 个工具、配置、任务、日志）
- Web UI：/static/... + 单页 index.html
- 鉴权：NAS_APP_TOKEN → Bearer 或 HttpOnly Cookie
- 写请求要求自定义头 X-Nas-App，防止跨站表单触发
"""

from __future__ import annotations

import hmac
import html
import json
import logging
import mimetypes
import re
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from nas_mcp import __version__ as CORE_VERSION

from . import APP_DISPLAY_NAME, __version__
from .jobs import JobFinished, JobManager, JobNotFound
from .settings import AppSettings
from .toolkit import InvalidArguments, ToolRegistry

logger = logging.getLogger("nas_app.http")

MAX_BODY = 2 * 1024 * 1024
COOKIE_NAME = "nas_app_token"


class NasHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        addr: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        settings: AppSettings,
        registry: ToolRegistry,
        jobs: JobManager,
    ):
        super().__init__(addr, handler)
        self.settings = settings
        self.registry = registry
        self.jobs = jobs
        self.web_dir = settings.static_dir


class NasAppHandler(BaseHTTPRequestHandler):
    server_version = f"nas-app/{__version__}"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ HTTP
    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        logger.debug("%s %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------------ 主流程
    def _handle(self, method: str) -> None:
        started = time.monotonic()
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        try:
            if path == "/healthz":
                self._json(
                    200,
                    {
                        "ok": True,
                        "app": "nas-app",
                        "version": __version__,
                        "core_version": CORE_VERSION,
                        "auth_required": self.server.settings.auth_required,
                    },
                )
                return

            if path == "/login":
                if method == "POST":
                    self._login(query)
                else:
                    self._login_page(query)
                return

            if path in {"/logout", "/api/logout"}:
                self._logout()
                return

            if path == "/favicon.ico":
                icon = self.server.web_dir / "icon.svg"
                if icon.is_file():
                    self._bytes(200, icon.read_bytes(), "image/svg+xml")
                else:
                    self._bytes(204, b"", "text/plain")
                return

            if path.startswith("/static/"):
                self._static(path[len("/static/"):])
                return

            authorized = self._authorized()
            if not authorized:
                if path.startswith("/api/"):
                    self._json(401, {"ok": False, "error": {"code": "unauthorized", "message": "需要访问令牌"}})
                else:
                    target = path if path.startswith("/") else "/"
                    self._redirect(f"/login?next={quote(target)}")
                return

            if method in {"POST", "PUT", "DELETE"} and path.startswith("/api/"):
                header = self.headers.get("X-Nas-App", "")
                bearer = self.headers.get("Authorization", "").lower().startswith("bearer ")
                if header != "1" and not bearer:
                    self._json(403, {"ok": False, "error": {"code": "csrf", "message": "缺少 X-Nas-App 请求头"}})
                    return

            if method == "GET" and path in {"/", "/index.html"}:
                self._index()
                return
            if method == "GET" and path == "/api/meta":
                self._meta()
                return
            if method == "GET" and path == "/api/overview":
                self._json(200, self.server.registry.overview())
                return
            if path == "/api/config":
                if method == "GET":
                    self._json(200, self.server.registry.config_view())
                elif method == "PUT":
                    body = self._body_json()
                    if body is None:
                        return
                    try:
                        view = self.server.registry.save_config(body)
                    except InvalidArguments as exc:
                        self._json(400, {"ok": False, "error": {"code": "invalid_arguments", "message": str(exc)}})
                        return
                    self._json(200, view)
                else:
                    self._method_not_allowed()
                return
            if method == "GET" and path == "/api/tools":
                self._json(200, {
                    "ok": True,
                    "tools": [spec.public() for spec in self.server.registry.specs()],
                })
                return
            match = re.fullmatch(r"/api/tools/([A-Za-z0-9_\-]+)", path)
            if match:
                if method != "POST":
                    self._method_not_allowed()
                    return
                body = self._body_json()
                if body is None:
                    return
                name = match.group(1)
                args = body.get("args", body) if isinstance(body, dict) else body
                if not isinstance(args, dict):
                    self._json(400, {"ok": False, "error": {"code": "invalid_arguments", "message": "请求体必须是 JSON 对象"}})
                    return
                try:
                    spec = self.server.registry.get_spec(name)
                except InvalidArguments as exc:
                    self._json(404, {"ok": False, "error": {"code": "unknown_tool", "message": str(exc)}})
                    return
                if spec.long_running and query.get("async", ["0"])[0] in {"1", "true"}:
                    job = self.server.jobs.submit(name, args)
                    self._json(202, {"ok": True, "job": self.server.jobs.as_payload(job)})
                    return
                self._json(200, self.server.registry.call(name, args))
                return
            if path == "/api/jobs":
                if method == "GET":
                    limit = self._int_query(query, "limit", 50, 1, 200)
                    self._json(200, {
                        "ok": True,
                        "jobs": [
                            self.server.jobs.as_payload(job, include_result=False)
                            for job in self.server.jobs.list(limit)
                        ],
                    })
                elif method == "POST":
                    body = self._body_json()
                    if body is None:
                        return
                    tool = body.get("tool")
                    args = body.get("args") or {}
                    if not isinstance(tool, str) or not isinstance(args, dict):
                        self._json(400, {"ok": False, "error": {"code": "invalid_arguments", "message": "需要 tool 字符串与 args 对象"}})
                        return
                    try:
                        self.server.registry.get_spec(tool)
                    except InvalidArguments as exc:
                        self._json(404, {"ok": False, "error": {"code": "unknown_tool", "message": str(exc)}})
                        return
                    job = self.server.jobs.submit(tool, args)
                    self._json(202, {"ok": True, "job": self.server.jobs.as_payload(job)})
                else:
                    self._method_not_allowed()
                return
            match = re.fullmatch(r"/api/jobs/([A-Za-z0-9_\-]+)/cancel", path)
            if match:
                if method != "POST":
                    self._method_not_allowed()
                    return
                try:
                    job = self.server.jobs.cancel(match.group(1))
                except JobNotFound as exc:
                    self._json(404, {"ok": False, "error": {"code": exc.code, "message": exc.message}})
                    return
                except JobFinished as exc:
                    self._json(409, {"ok": False, "error": {"code": exc.code, "message": exc.message}})
                    return
                self._json(200, {"ok": True, "job": self.server.jobs.as_payload(job)})
                return
            match = re.fullmatch(r"/api/jobs/([A-Za-z0-9_\-]+)", path)
            if match and method == "GET":
                wait = self._int_query(query, "wait", 0, 0, 60)
                job = (
                    self.server.jobs.wait(match.group(1), timeout=wait)
                    if wait
                    else self.server.jobs.get(match.group(1))
                )
                if job is None:
                    self._json(404, {"ok": False, "error": {"code": "not_found", "message": "任务不存在"}})
                    return
                self._json(200, {"ok": True, "job": self.server.jobs.as_payload(job)})
                return
            if path == "/api/logs" and method == "GET":
                self._logs(query)
                return
            if path.startswith("/api/"):
                self._json(404, {"ok": False, "error": {"code": "not_found", "message": f"未知接口 {path}"}})
                return
            # 单页应用回退
            self._index()
        except BrokenPipeError:
            return
        except Exception as exc:  # noqa: BLE001 - 顶层边界
            logger.exception("请求处理失败 %s %s", method, path)
            try:
                self._json(500, {"ok": False, "error": {"code": "internal_error", "message": f"{type(exc).__name__}: {exc}"}})
            except OSError:
                pass
        finally:
            logger.debug("%s %s %.3fs", method, path, time.monotonic() - started)

    # ------------------------------------------------------------------ 鉴权
    def _authorized(self) -> bool:
        token = self.server.settings.token
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            if hmac.compare_digest(auth[7:].strip(), token):
                return True
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:  # noqa: BLE001
            return False
        morsel = cookie.get(COOKIE_NAME)
        return bool(morsel and hmac.compare_digest(morsel.value, token))

    def _login_page(self, query: dict[str, list[str]]) -> None:
        next_path = html.escape(_safe_next((query.get("next") or ["/"])[0]))
        error = "<p class='error'>密码不对，再试试</p>" if (query.get("error") or [""])[0] else ""
        html_page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 · {APP_DISPLAY_NAME}</title>
<meta name="theme-color" content="#fbf4ee" id="theme-color">
<script src="/static/mode.js"></script>
<script src="/static/login-mode.js"></script>
<link rel="stylesheet" href="/static/style.css">
</head><body class="login-body">
<main class="login-card">
  <div class="brand"><img src="/static/icon.svg" alt=""><div class="brand-text"><strong>{APP_DISPLAY_NAME}</strong><small class="brand-tagline">把媒体库打理得清清爽爽</small></div></div>
  <p class="muted">请输入访问密码。</p>
  <div class="mode-switch" id="login-mode-switch" role="group" aria-label="界面风格">
    <span class="mode-label">界面</span>
    <button type="button" data-mode="pro" aria-pressed="false">专业版</button>
    <button type="button" data-mode="friendly" aria-pressed="true">友好版</button>
  </div>
  {error}
  <form method="post" action="/login">
    <input type="hidden" name="next" value="{next_path}">
    <label>访问密码<input type="password" name="token" autofocus required autocomplete="current-password"></label>
    <button type="submit" class="primary">进入</button>
  </form>
  <details class="login-hint">
    <summary>这是什么密码？</summary>
    <p class="muted">就是安装时在飞牛安装向导里设置的访问密码（环境变量 NAS_APP_TOKEN）。</p>
  </details>
</main></body></html>"""
        self._bytes(200, html_page.encode("utf-8"), "text/html; charset=utf-8")

    def _login(self, query: dict[str, list[str]]) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 65536:
            self._json(413, {"ok": False, "error": {"code": "too_large", "message": "请求体过大"}})
            return
        raw = self.rfile.read(length).decode("utf-8", "replace")
        form = parse_qs(raw)
        token = (form.get("token") or [""])[0]
        next_path = _safe_next((form.get("next") or ["/"])[0])
        expected = self.server.settings.token
        if not expected or hmac.compare_digest(token, expected):
            cookie = f"{COOKIE_NAME}={quote(token)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"
            self.send_response(303)
            self.send_header("Location", next_path)
            self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        time.sleep(0.5)
        self._redirect(f"/login?error=1&next={quote(next_path)}")

    def _logout(self) -> None:
        self.send_response(303)
        self.send_header("Location", "/login")
        self.send_header("Set-Cookie", f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ------------------------------------------------------------------ 页面与静态
    def _index(self) -> None:
        path = self.server.web_dir / "index.html"
        if not path.is_file():
            self._json(500, {"ok": False, "error": {"code": "ui_missing", "message": "Web UI 文件缺失"}})
            return
        self._bytes(
            200,
            path.read_bytes(),
            "text/html; charset=utf-8",
            extra={"Cache-Control": "no-cache"},
        )

    def _static(self, rel: str) -> None:
        if not rel or ".." in Path(rel).parts:
            self._json(404, {"ok": False, "error": {"code": "not_found", "message": "文件不存在"}})
            return
        base = self.server.web_dir.resolve()
        target = (base / rel).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            self._json(404, {"ok": False, "error": {"code": "not_found", "message": "文件不存在"}})
            return
        mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if mime.startswith("text/") or mime in {"application/javascript", "image/svg+xml"}:
            mime += "; charset=utf-8"
        self._bytes(200, target.read_bytes(), mime, extra={"Cache-Control": "no-cache"})

    # ------------------------------------------------------------------ API 辅助
    def _meta(self) -> None:
        settings = self.server.settings
        self._json(200, {
            "ok": True,
            "app": {
                "name": APP_DISPLAY_NAME,
                "version": __version__,
                "core_version": CORE_VERSION,
            },
            "auth_required": settings.auth_required,
            "data_dir": str(settings.data_dir),
            "config_path": str(settings.config_path),
            "tools": [spec.public() for spec in self.server.registry.specs()],
        })

    def _logs(self, query: dict[str, list[str]]) -> None:
        lines = self._int_query(query, "lines", 200, 1, 2000)
        which = (query.get("file") or ["app"])[0]
        if which == "audit":
            try:
                cfg = self.server.registry.get_config()
                path = Path(cfg.organizer.audit_log_file).expanduser()
                if not path.is_absolute():
                    path = Path.cwd() / path
            except Exception as exc:  # noqa: BLE001
                self._json(400, {"ok": False, "error": {"code": "config_error", "message": str(exc)}})
                return
        else:
            path = self.server.settings.log_file
        if not path.is_file():
            self._json(200, {"ok": True, "file": str(path), "lines": [], "message": "日志文件尚不存在"})
            return
        try:
            text = _tail_text(path, 256 * 1024)
        except OSError as exc:
            self._json(500, {"ok": False, "error": {"code": "io_error", "message": str(exc)}})
            return
        tail = text.splitlines()[-lines:]
        self._json(200, {"ok": True, "file": str(path), "lines": tail})

    def _body_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            self._json(413, {"ok": False, "error": {"code": "too_large", "message": "请求体超过 2MB"}})
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._json(400, {"ok": False, "error": {"code": "invalid_json", "message": str(exc)}})
            return None
        if not isinstance(data, dict):
            self._json(400, {"ok": False, "error": {"code": "invalid_json", "message": "顶层必须是对象"}})
            return None
        return data

    @staticmethod
    def _int_query(query: dict[str, list[str]], key: str, default: int, low: int, high: int) -> int:
        try:
            value = int((query.get(key) or [default])[0])
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    def _method_not_allowed(self) -> None:
        self._json(405, {"ok": False, "error": {"code": "method_not_allowed", "message": "方法不允许"}})

    # ------------------------------------------------------------------ 响应
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._bytes(status, body, "application/json; charset=utf-8")

    def _bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        if content_type.startswith("application/json") or content_type.startswith("text/html"):
            self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()


def _safe_next(value: str) -> str:
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return "/"
    return value


def _tail_text(path: Path, max_bytes: int) -> str:
    with path.open("rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - max_bytes))
        data = fh.read()
    return data.decode("utf-8", "replace")


def serve(settings: AppSettings, registry: ToolRegistry, jobs: JobManager) -> None:
    httpd = NasHTTPServer((settings.host, settings.port), NasAppHandler, settings, registry, jobs)
    logger.info("NAS 媒体工具箱 %s 已启动：http://%s:%s", __version__, settings.host, settings.port)
    logger.info("访问令牌：%s", "已启用" if settings.auth_required else "未启用（仅内网使用）")
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在退出")
    finally:
        httpd.server_close()
