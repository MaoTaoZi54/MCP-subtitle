import http.client
import json
import threading
from urllib.parse import urlencode

import pytest

from nas_app.server import NasAppHandler, NasHTTPServer, _safe_next

TOKEN = "secret-token"


@pytest.fixture
def server(settings, registry, jobs):
    httpd = NasHTTPServer(("127.0.0.1", 0), NasAppHandler, settings, registry, jobs)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def call(server, method, path, body=None, headers=None, form=False):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=15)
    hdrs = dict(headers or {})
    payload = None
    if body is not None:
        if form:
            payload = urlencode(body).encode("utf-8")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            payload = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        hdrs["Content-Length"] = str(len(payload))
    conn.request(method, path, body=payload, headers=hdrs)
    res = conn.getresponse()
    data = res.read()
    status = res.status
    response_headers = dict(res.getheaders())
    conn.close()
    return status, response_headers, data


def auth(extra=None):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    headers.update(extra or {})
    return headers


def test_healthz_is_public(server):
    status, _, body = call(server, "GET", "/healthz")
    data = json.loads(body)
    assert status == 200
    assert data["ok"] is True
    assert data["auth_required"] is True


def test_meta_requires_token(server):
    status, _, _ = call(server, "GET", "/api/meta")
    assert status == 401
    status, _, body = call(server, "GET", "/api/meta", headers=auth())
    data = json.loads(body)
    assert status == 200
    assert data["app"]["name"]
    assert len(data["tools"]) == 28


def test_login_page_escapes_next(server):
    status, _, body = call(server, "GET", "/login?next=%2Ffoo%3Cscript%3E")
    html = body.decode("utf-8")
    assert status == 200
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_safe_next_rejects_header_injection():
    assert _safe_next("/ok?x=1") == "/ok?x=1"
    assert _safe_next("/bad\r\nX-Evil: 1") == "/"
    assert _safe_next("//evil.example") == "/"
    assert _safe_next("https://evil.example") == "/"
    assert _safe_next("/back\\slash") == "/"


def test_login_sets_cookie(server):
    status, headers, _ = call(server, "POST", "/login", body={"token": TOKEN, "next": "/#/scan"}, form=True)
    assert status == 303
    assert headers["Location"] == "/#/scan"
    cookie = headers["Set-Cookie"]
    assert "nas_app_token=" in cookie
    assert "HttpOnly" in cookie


def test_login_rejects_wrong_token(server):
    status, headers, _ = call(server, "POST", "/login", body={"token": "nope", "next": "/"}, form=True)
    assert status == 303
    assert headers["Location"].startswith("/login?error=1")


def test_csrf_header_required_for_cookie_writes(server, settings):
    _, headers, _ = call(server, "POST", "/login", body={"token": TOKEN, "next": "/"}, form=True)
    cookie = headers["Set-Cookie"].split(";")[0]
    media = settings.config_path.parent.parent / "media"
    status, _, _ = call(server, "POST", "/api/tools/nas_list_directory", body={"args": {"path": str(media)}}, headers={"Cookie": cookie})
    assert status == 403
    status, _, body = call(
        server, "POST", "/api/tools/nas_list_directory",
        body={"args": {"path": str(media)}}, headers={"Cookie": cookie, "X-Nas-App": "1"},
    )
    assert status == 200
    assert json.loads(body)["ok"] is True


def test_tool_access_denied(server, settings):
    status, _, body = call(
        server, "POST", "/api/tools/nas_list_directory",
        body={"args": {"path": str(settings.data_dir)}}, headers=auth({"X-Nas-App": "1"}),
    )
    data = json.loads(body)
    assert status == 200
    assert data["ok"] is False
    assert data["error"]["code"] == "access_denied"


def test_config_get_and_update(server, settings):
    status, _, body = call(server, "GET", "/api/config", headers=auth())
    assert status == 200
    assert json.loads(body)["enable_write"] is False
    status, _, body = call(
        server, "PUT", "/api/config",
        body={"enable_write": True, "allowed_paths": [str(settings.config_path.parent.parent / "media")]},
        headers=auth({"X-Nas-App": "1"}),
    )
    assert status == 200
    data = json.loads(body)
    assert data["enable_write"] is True
    assert data["config_path"].endswith("config.yaml")


def test_unknown_tool_404(server):
    status, _, body = call(
        server, "POST", "/api/tools/nope",
        body={"args": {}}, headers=auth({"X-Nas-App": "1"}),
    )
    assert status == 404
    assert json.loads(body)["error"]["code"] == "unknown_tool"


def test_jobs_api(server, settings):
    media = settings.config_path.parent.parent / "media"
    status, _, body = call(
        server, "POST", "/api/jobs",
        body={"tool": "nas_list_directory", "args": {"path": str(media)}},
        headers=auth({"X-Nas-App": "1"}),
    )
    assert status == 202
    job_id = json.loads(body)["job"]["id"]
    status, _, body = call(server, "GET", f"/api/jobs/{job_id}?wait=10", headers=auth())
    data = json.loads(body)
    assert status == 200
    assert data["job"]["status"] == "succeeded"
    status, _, body = call(server, "GET", "/api/jobs", headers=auth())
    assert status == 200
    assert any(job["id"] == job_id for job in json.loads(body)["jobs"])


def test_cancel_missing_job_404(server):
    status, _, body = call(server, "POST", "/api/jobs/J-nope/cancel", headers=auth())
    assert status == 404
    assert json.loads(body)["error"]["code"] == "not_found"


def test_cancel_finished_job_409(server, settings):
    media = settings.config_path.parent.parent / "media"
    status, _, body = call(
        server, "POST", "/api/jobs",
        body={"tool": "nas_list_directory", "args": {"path": str(media)}},
        headers=auth({"X-Nas-App": "1"}),
    )
    job_id = json.loads(body)["job"]["id"]
    status, _, body = call(server, "GET", f"/api/jobs/{job_id}?wait=10", headers=auth())
    assert json.loads(body)["job"]["status"] == "succeeded"
    status, _, body = call(server, "POST", f"/api/jobs/{job_id}/cancel", headers=auth())
    assert status == 409
    assert json.loads(body)["error"]["code"] == "already_finished"


def test_cancel_requires_post(server):
    status, _, _ = call(server, "GET", "/api/jobs/J-x/cancel", headers=auth())
    assert status in {404, 405}


def test_static_traversal_blocked(server):
    for path in ("/static/../settings.py", "/static/%2e%2e/settings.py", "/static/..%2fsettings.py"):
        status, _, _ = call(server, "GET", path)
        assert status == 404, path


def test_ui_offers_pro_and_friendly_modes(server):
    status, _, body = call(server, "GET", "/", headers=auth())
    html = body.decode("utf-8")
    assert status == 200
    assert 'id="mode-switch"' in html
    assert 'data-mode="pro"' in html
    assert 'data-mode="friendly"' in html
    assert "/static/mode.js" in html


def test_mode_js_is_served(server):
    status, headers, body = call(server, "GET", "/static/mode.js")
    assert status == 200
    assert "javascript" in headers["Content-Type"]
    assert "nas-ui-mode" in body.decode("utf-8")


def test_login_page_applies_ui_mode_script(server):
    status, _, body = call(server, "GET", "/login")
    html = body.decode("utf-8")
    assert status == 200
    assert "/static/mode.js" in html
    assert "/static/login-mode.js" in html
    assert 'id="login-mode-switch"' in html


def test_login_page_friendly_text(server):
    status, _, body = call(server, "GET", "/login")
    text = body.decode("utf-8")
    assert status == 200
    assert "访问密码" in text
    assert "请输入应用访问令牌" not in text
    assert "NAS_APP_TOKEN" in text
    status, _, body = call(server, "GET", "/login?error=1")
    assert "密码不对" in body.decode("utf-8")


def test_spa_fallback(server):
    status, headers, body = call(server, "GET", "/scan", headers=auth())
    assert status == 200
    assert "text/html" in headers["Content-Type"]
    assert b"NAS \xe5\xaa\x92\xe4\xbd\x93\xe5\xb7\xa5\xe5\x85\xb7\xe7\xae\xb1" in body


def test_logs(server):
    status, _, body = call(server, "GET", "/api/logs?file=app&lines=5", headers=auth())
    data = json.loads(body)
    assert status == 200
    assert data["ok"] is True
    assert isinstance(data["lines"], list)
