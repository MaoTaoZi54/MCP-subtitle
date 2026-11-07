import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CMD = ROOT / "packaging" / "fnos" / "cmd"
BASH = shutil.which("bash")

pytestmark = [
    pytest.mark.skipif(BASH is None, reason="需要 bash 才能测试飞牛生命周期脚本"),
    pytest.mark.skipif(
        os.name == "nt",
        reason="TRIM_DATA_ACCESSIBLE_PATHS 用冒号分隔，与 Windows 盘符冲突；请在飞牛/Linux 上运行",
    ),
]

FAKE_DOCKER = """#!/bin/bash
state_file="{state}"
echo "docker $*" >> "{log}"
case "$1" in
  image) exit 0 ;;
  inspect)
    if [ "$3" = "{{{{.State.Running}}}}" ]; then
      [ -f "$state_file" ] && echo true || echo false
    else
      [ -f "$state_file" ] && echo healthy || echo missing
    fi
    exit 0 ;;
  run) touch "$state_file"; exit 0 ;;
  stop|rm) rm -f "$state_file"; exit 0 ;;
  pull|load|logs) exit 0 ;;
  *) exit 0 ;;
esac
"""


@pytest.fixture
def fnos(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    state = tmp_path / "running"
    log = tmp_path / "docker.log"
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER.format(state=state, log=log), encoding="utf-8")
    docker.chmod(0o755)
    dest = tmp_path / "target"
    (dest / "docker").mkdir(parents=True)
    (dest / "docker" / "image.conf").write_text("IMAGE=mock:0.2.0\n", encoding="utf-8")
    etc = tmp_path / "etc"
    var = tmp_path / "var"
    logs = tmp_path / "logs"
    logs.mkdir()
    media1 = tmp_path / "media1"
    media2 = tmp_path / "media2"
    media1.mkdir()
    media2.mkdir()
    # 默认端口 8670 在已安装应用的机器上会被真实 MCP 占用；测试用空闲端口，
    # 否则启动脚本会走「禁用 MCP」分支，无法验证端口映射。
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        mcp_port = sock.getsockname()[1]
    env = {
        **os.environ,
        "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
        "TRIM_APPDEST": str(dest),
        "TRIM_PKGETC": str(etc),
        "TRIM_PKGVAR": str(var),
        "TRIM_TEMP_LOGFILE": str(logs / "user.log"),
        "TRIM_DATA_ACCESSIBLE_PATHS": f"{media1}:{media2}",
        "TRIM_SERVICE_PORT": "8669",
        "NAS_MCP_PORT": str(mcp_port),
    }
    return {
        "env": env,
        "tmp": tmp_path,
        "etc": etc,
        "var": var,
        "media": (media1, media2),
        "log": log,
        "state": state,
        "mcp_port": mcp_port,
    }


def run_cmd(name, env, *args, extra=None):
    merged = dict(env)
    merged.update(extra or {})
    return subprocess.run(
        [BASH, str(CMD / name), *args],
        env=merged,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def test_install_and_lifecycle(fnos):
    env = fnos["env"]
    result = run_cmd("install_init", env, extra={"wizard_token": "0123456789"})
    assert result.returncode == 0, result.stderr
    assert (fnos["etc"] / "token").read_text() == "0123456789"
    assert (fnos["etc"] / "port").read_text() == "8669"
    assert (fnos["etc"] / "mcp_port").read_text() == str(fnos["mcp_port"])

    result = run_cmd("main", env, "start")
    assert result.returncode == 0, result.stderr
    log = fnos["log"].read_text()
    run_line = [line for line in log.splitlines() if line.startswith("docker run")][-1]
    media1, media2 = fnos["media"]
    assert f"-v {media1}:{media1}" in run_line
    assert f"-v {media2}:{media2}" in run_line
    assert f"-v {fnos['var']}:/data" in run_line
    assert f"NAS_APP_ALLOWED_PATHS={media1},{media2}" in run_line
    assert "NAS_APP_TOKEN=0123456789" in run_line
    assert "mock:0.2.0" in run_line
    assert f"-p 127.0.0.1:{fnos['mcp_port']}:8670" in run_line
    assert "NAS_APP_MCP_ENABLED=1" in run_line
    assert "NAS_APP_MCP_HOST=0.0.0.0" in run_line

    assert run_cmd("main", env, "status").returncode == 0
    assert run_cmd("main", env, "stop").returncode == 0
    assert run_cmd("main", env, "status").returncode == 3


def test_short_token_rejected(fnos):
    result = run_cmd("install_init", fnos["env"], extra={"wizard_token": "short"})
    assert result.returncode == 1
    assert not (fnos["etc"] / "token").exists()


def test_start_without_authorized_paths_fails(fnos):
    env = dict(fnos["env"])
    env["TRIM_DATA_ACCESSIBLE_PATHS"] = ""
    result = run_cmd("install_init", env, extra={"wizard_token": "0123456789"})
    assert result.returncode == 0
    result = run_cmd("main", env, "start")
    assert result.returncode == 1
    message = (fnos["tmp"] / "logs" / "user.log").read_text(encoding="utf-8")
    assert "授权" in message


def test_config_callback_updates_token_and_restarts(fnos):
    env = fnos["env"]
    run_cmd("install_init", env, extra={"wizard_token": "0123456789"})
    run_cmd("main", env, "start")
    assert run_cmd("config_init", env).returncode == 0
    result = run_cmd("config_callback", env, extra={"wizard_token": "abcdefghij"})
    assert result.returncode == 0
    assert (fnos["etc"] / "token").read_text() == "abcdefghij"
    log = fnos["log"].read_text()
    assert log.count("docker run") >= 2


def test_uninstall_removes_data_only_when_asked(fnos):
    env = fnos["env"]
    run_cmd("install_init", env, extra={"wizard_token": "0123456789"})
    (fnos["var"] / "config.yaml").write_text("enable_write: false\n", encoding="utf-8")
    assert run_cmd("uninstall_init", env, extra={"wizard_remove_data": "false"}).returncode == 0
    assert (fnos["var"] / "config.yaml").is_file()
    assert run_cmd("uninstall_init", env, extra={"wizard_remove_data": "true"}).returncode == 0
    assert not fnos["var"].exists()
