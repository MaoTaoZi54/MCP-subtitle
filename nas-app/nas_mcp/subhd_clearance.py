"""SubHD 的 Cloudflare 人机挑战处理（cf_clearance 获取与复用）。

背景：subhd.tv 在 ``POST /api/sub/prepare-download`` 前挂了 Cloudflare JS
挑战。curl_cffi 只能伪装 TLS 指纹、不执行 JavaScript，因此拿不到
``cf_clearance``，下载 API 恒返回 403。本模块用可选的无头浏览器
（playwright + chromium）过一次挑战，把 cookie 与一致的 UA 落盘，供
curl_cffi 持久 Session 复用；过期或再次 403 时只续签一次。

依赖隔离：playwright 是可选依赖，本模块顶层不 import 它。未安装时
``playwright_available()`` 返回 False，调用方降级为结构化错误
（``browser_unavailable``），不影响其他站点与插件加载。

安全：cookie 值只落盘（0600），绝不写日志；错误里只保留 cookie 名与
到期时间。默认 cookie 路径为 ``<数据目录>/nas-media/subhd-clearance.json``。
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import SubhdConfig

logger = logging.getLogger("nas_mcp.subhd")

# CF 的 cf_clearance 常见有效期约 30 分钟；cookie 未带 expires 时按此兜底。
_DEFAULT_TTL_SECONDS = 1800
# 剩余有效期低于该值时视为过期，提前续签，避免请求途中失效。
_MIN_REMAINING_SECONDS = 60
# 允许注入的 Cloudflare cookie（其余 cookie 不落盘，减少敏感面）。
_CF_COOKIE_NAMES = ("cf_clearance", "__cf_bm")

_LOCK = threading.Lock()


class ClearanceError(Exception):
    """浏览器续签失败（可映射为结构化错误码）。"""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool = True,
        detail: str = "",
    ):
        self.code = code
        self.retryable = retryable
        self.detail = detail
        super().__init__(message)


@dataclass(frozen=True)
class Clearance:
    """一次成功挑战得到的浏览器凭据。"""

    cookies: tuple[tuple[str, str, str, str, float], ...]  # (name, value, domain, path, expires)
    user_agent: str
    obtained_at: float
    expires_at: float

    def is_valid(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return bool(self.cookies) and self.expires_at - _MIN_REMAINING_SECONDS > now


def playwright_available() -> bool:
    """playwright 是否可用（只查模块存在，不启动浏览器）。"""
    try:
        import importlib.util

        return importlib.util.find_spec("playwright") is not None
    except Exception:  # pragma: no cover - 极端环境下的导入异常
        return False


def _data_root() -> Path:
    for env_name in ("OCTOP_DATA_DIR", "NAS_DATA_DIR", "NAS_APP_DATA"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            return Path(raw)
    return Path.home() / ".nas-media"


def default_cookie_path() -> Path:
    return _data_root() / "nas-media" / "subhd-clearance.json"


def resolve_cookie_path(cfg: SubhdConfig) -> Path:
    """配置里的 cookie 路径；留空用数据目录默认值，相对路径按默认目录解析。"""
    raw = (cfg.browser_cookie_path or "").strip()
    if not raw:
        return default_cookie_path()
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return default_cookie_path().parent / path


def _host_of(cfg: SubhdConfig) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(cfg.base_url).hostname or "").lower()


def _domain_matches(domain: str, host: str) -> bool:
    domain = (domain or "").lstrip(".").lower()
    return bool(domain) and (host == domain or host.endswith("." + domain))


def load(cfg: SubhdConfig) -> Clearance | None:
    """读取落盘凭据；损坏或站点不匹配时返回 None（不抛异常）。"""
    path = resolve_cookie_path(cfg)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("SubHD cf_clearance 文件不可读，忽略: %s", exc)
        return None

    if not isinstance(raw, dict) or raw.get("version") != 1:
        return None
    host = _host_of(cfg)
    if raw.get("host") and raw["host"] != host:
        logger.info("SubHD cf_clearance 属于其他站点（%s），忽略", raw.get("host"))
        return None

    cookies: list[tuple[str, str, str, str, float]] = []
    for item in raw.get("cookies") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        value = str(item.get("value") or "")
        domain = str(item.get("domain") or "")
        if name not in _CF_COOKIE_NAMES or not value:
            continue
        if not _domain_matches(domain, host):
            continue
        try:
            expires = float(item.get("expires") or 0)
        except (TypeError, ValueError):
            expires = 0.0
        cookies.append((name, value, domain, str(item.get("path") or "/"), expires))
    if not cookies:
        return None

    try:
        obtained_at = float(raw.get("obtained_at") or 0)
        expires_at = float(raw.get("expires_at") or 0)
    except (TypeError, ValueError):
        return None
    return Clearance(
        cookies=tuple(cookies),
        user_agent=str(raw.get("user_agent") or ""),
        obtained_at=obtained_at,
        expires_at=expires_at,
    )


def save(cfg: SubhdConfig, clearance: Clearance) -> None:
    """原子写入凭据文件（0600）；失败只告警，不影响本次请求。"""
    path = resolve_cookie_path(cfg)
    payload = {
        "version": 1,
        "host": _host_of(cfg),
        "user_agent": clearance.user_agent,
        "obtained_at": clearance.obtained_at,
        "expires_at": clearance.expires_at,
        "cookies": [
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": cookie_path,
                "expires": expires,
            }
            for name, value, domain, cookie_path, expires in clearance.cookies
        ],
    }
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 以 0600 创建临时文件，避免 write_text 默认权限（0644）造成短暂可读窗口。
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("SubHD cf_clearance 落盘失败 %s: %s", path, exc)
        try:
            tmp.unlink()
        except OSError:
            pass


def invalidate(cfg: SubhdConfig) -> None:
    """删除已失效的落盘凭据。"""
    try:
        resolve_cookie_path(cfg).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("SubHD cf_clearance 删除失败: %s", exc)


def _scrub(text: str) -> str:
    """错误文本脱敏：去掉代理 URL 中的用户名/密码。"""
    return re.sub(r"//[^/@\s]+@", "//***@", text or "")


# 在独立子进程里执行：避免把 playwright 的同步事件循环塞进工具线程，
# 也保证浏览器崩溃/超时不会拖垮插件进程。payload 走 stdin（代理凭据不进 argv）。
_BROWSER_SCRIPT = r"""
import json
import sys
import time

from playwright.sync_api import sync_playwright

payload = json.loads(sys.stdin.read())
result = {"ok": False, "stage": "launch", "cookies": [], "user_agent": "", "error": ""}
try:
    with sync_playwright() as p:
        launch_kwargs = {
            "headless": payload.get("headless", True),
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        }
        proxy = payload.get("proxy") or ""
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
        browser = p.chromium.launch(**launch_kwargs)
        result["stage"] = "context"
        context = browser.new_context(
            user_agent=payload.get("user_agent") or None,
            locale="zh-CN",
            viewport={"width": 1366, "height": 900},
        )
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        page = context.new_page()
        page.set_default_timeout(payload.get("timeout_ms", 45000))
        result["stage"] = "navigate"
        page.goto(payload["url"], wait_until="domcontentloaded")
        deadline = time.time() + float(payload.get("wait_seconds", 20))
        cookies = []
        while time.time() < deadline:
            cookies = context.cookies()
            if any(c.get("name") == "cf_clearance" for c in cookies):
                break
            page.wait_for_timeout(1000)
        result["stage"] = "cookies"
        result["cookies"] = [
            {
                "name": c.get("name", ""),
                "value": c.get("value", ""),
                "domain": c.get("domain", ""),
                "path": c.get("path", "/"),
                "expires": c.get("expires", -1),
            }
            for c in cookies
            if c.get("name") in ("cf_clearance", "__cf_bm")
        ]
        result["user_agent"] = page.evaluate("navigator.userAgent") or ""
        result["url"] = page.url
        result["title"] = page.title()
        result["ok"] = any(c["name"] == "cf_clearance" for c in result["cookies"])
        browser.close()
except Exception as exc:
    result["error"] = f"{type(exc).__name__}: {exc}"
finally:
    print(json.dumps(result))
"""


def _subprocess_env() -> dict[str, str]:
    """确保子进程能找到 playwright（父进程可能通过运行时 sys.path 注入）。"""
    env = dict(os.environ)
    extra: list[str] = []
    try:
        import playwright

        extra.append(str(Path(playwright.__file__).resolve().parent.parent))
    except Exception:  # pragma: no cover
        pass
    if extra:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(extra + ([existing] if existing else []))
    return env


def _classify_launch_error(stderr: str) -> ClearanceError:
    text = _scrub(stderr.strip())
    low = text.lower()
    if (
        "executable doesn't exist" in low
        or "host system is missing dependencies" in low
        or "error while loading shared libraries" in low
        or "playwright was just installed" in low
    ):
        return ClearanceError(
            "playwright 已安装但 chromium 浏览器不可用，请先执行 playwright install chromium",
            code="browser_unavailable",
            retryable=False,
            detail=text[-400:],
        )
    return ClearanceError(
        "无头浏览器启动失败",
        code="browser_unavailable",
        retryable=False,
        detail=text[-400:],
    )


def _run_browser(
    cfg: SubhdConfig,
    url: str,
    *,
    timeout_seconds: float,
) -> Clearance:
    payload = {
        "url": url,
        "user_agent": cfg.user_agent,
        "proxy": cfg.browser_proxy or "",
        "headless": bool(cfg.browser_headless),
        "timeout_ms": int(max(5.0, timeout_seconds) * 1000),
        "wait_seconds": max(10.0, timeout_seconds - 10.0),
    }
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _BROWSER_SCRIPT],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            timeout=timeout_seconds + 20,
            env=_subprocess_env(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClearanceError(
            f"无头浏览器超时（>{timeout_seconds:.0f}s）",
            code="browser_unavailable",
            retryable=True,
            detail=_scrub(str(exc)),
        ) from exc
    except OSError as exc:
        raise ClearanceError(
            f"无法启动浏览器子进程: {exc}",
            code="browser_unavailable",
            retryable=False,
        ) from exc

    stdout = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
    if not stdout:
        raise _classify_launch_error(stderr or f"exit={proc.returncode}")
    try:
        data = json.loads(stdout.splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise ClearanceError(
            "浏览器返回内容无法解析",
            code="browser_unavailable",
            retryable=True,
            detail=_scrub(stdout[-400:]),
        ) from exc

    if not data.get("ok"):
        stage = str(data.get("stage") or "unknown")
        err = _scrub(str(data.get("error") or ""))
        low = err.lower()
        if stage in ("launch", "context") and (
            "executable doesn't exist" in low
            or "missing dependencies" in low
            or "shared libraries" in low
        ):
            raise _classify_launch_error(err)
        title = _scrub(str(data.get("title") or ""))[:120]
        raise ClearanceError(
            f"无头浏览器未能通过 Cloudflare 挑战（阶段: {stage}）",
            code="cloudflare_challenge",
            retryable=True,
            detail=(err or f"title={title!r}")[-400:],
        )

    now = time.time()
    cookies: list[tuple[str, str, str, str, float]] = []
    expires_at = 0.0
    for item in data.get("cookies") or []:
        name = str(item.get("name") or "")
        value = str(item.get("value") or "")
        if name not in _CF_COOKIE_NAMES or not value:
            continue
        try:
            expires = float(item.get("expires") or 0)
        except (TypeError, ValueError):
            expires = 0.0
        if expires <= 0:
            expires = now + _DEFAULT_TTL_SECONDS
        cookies.append(
            (name, value, str(item.get("domain") or ""), str(item.get("path") or "/"), expires)
        )
        if name == "cf_clearance":
            expires_at = expires
    if not any(c[0] == "cf_clearance" for c in cookies):
        raise ClearanceError(
            "无头浏览器未取得 cf_clearance",
            code="cloudflare_challenge",
            retryable=True,
            detail=_scrub(str(data.get("title") or ""))[:200],
        )
    if expires_at <= now:
        expires_at = now + _DEFAULT_TTL_SECONDS

    return Clearance(
        cookies=tuple(cookies),
        user_agent=str(data.get("user_agent") or cfg.user_agent),
        obtained_at=now,
        expires_at=expires_at,
    )


def acquire(cfg: SubhdConfig, url: str, *, force: bool = False) -> Clearance:
    """获取可用凭据：优先复用落盘，过期或 force 时跑一次浏览器。

    并发安全：同一进程内串行；拿到锁后再次检查落盘，避免两个并发下载
    同时启动浏览器。
    """
    if not cfg.browser_renew_enabled:
        raise ClearanceError(
            "浏览器续签已关闭（subhd.browser_renew_enabled=false）",
            code="browser_unavailable",
            retryable=False,
        )
    if not playwright_available():
        raise ClearanceError(
            "未安装 playwright，无法自动通过 Cloudflare 挑战（pip install playwright 后"
            "再执行 playwright install chromium）",
            code="browser_unavailable",
            retryable=False,
        )

    with _LOCK:
        stored = load(cfg)
        if stored is not None and not force and stored.is_valid():
            return stored
        try:
            clearance = _run_browser(
                cfg, url, timeout_seconds=cfg.browser_timeout_seconds
            )
        except ClearanceError as exc:
            if stored is not None and exc.code == "cloudflare_challenge":
                raise ClearanceError(
                    str(exc),
                    code="clearance_expired",
                    retryable=True,
                    detail=exc.detail,
                ) from exc
            raise
        save(cfg, clearance)
        logger.info(
            "SubHD cf_clearance 已更新（到期 %s）",
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(clearance.expires_at)),
        )
        return clearance
